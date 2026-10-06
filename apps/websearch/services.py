"""
websearch 业务层
提供联网搜索能力，返回结构化结果：
    { position, title, url, snippet, source, published_time }

设计要点：
- 多服务商抽象，与 llm 层思路一致：通过 config + 环境变量注入密钥，无需改动代码即可切换。
- provider 取值：
    "auto"  (默认) -> 有 TAVILY_API_KEY 用 Tavily；否则有 BRAVE_API_KEY 用 Brave；
                     否则用 Bing（cn.bing.com HTML，免密钥，国内可直连）；最后回退 DuckDuckGo
    "tavily" / "brave" / "bing" / "ddg"
- Bing（cn.bing.com 搜索结果页）免密钥、国内网络通常可直连，作为默认开箱即用的搜索引擎；
  DuckDuckGo 在本环境常被出口策略拦截，故仅作为最后回退。
- Tavily/Brave 仅在有 key 时启用，质量更高。
- 统一异常：网络/解析失败抛出 RuntimeError，由 routes 捕获并返回友好错误。
"""
import os
import re
import json
import urllib.parse
from html import unescape as _unescape
from html.parser import HTMLParser

import requests

from apps import CONFIG, logger


# ── 配置 ──────────────────────────────────────────────
def _search_cfg():
    return CONFIG.get("search", {}) or {}


def _resolve_provider(requested=None):
    """根据请求 provider / config / 环境变量决定实际使用的搜索引擎。"""
    cfg = _search_cfg()
    requested = (requested or cfg.get("provider") or "auto").strip().lower()

    if requested in ("tavily", "brave", "bing", "ddg"):
        return requested

    # auto：按优先级选择已就绪的服务商
    tavily_key = os.environ.get(cfg.get("tavily_api_key_env", "TAVILY_API_KEY"), "").strip()
    brave_key = os.environ.get(cfg.get("brave_api_key_env", "BRAVE_API_KEY"), "").strip()
    if tavily_key:
        return "tavily"
    if brave_key:
        return "brave"
    # Bing 免密钥、国内可直连，作为默认搜索源；DDG 仅作最后回退
    return "bing"


def search_status():
    """供 /api/status 与 /api/search/status 使用，不暴露密钥。"""
    cfg = _search_cfg()
    tavily_key = os.environ.get(cfg.get("tavily_api_key_env", "TAVILY_API_KEY"), "").strip()
    brave_key = os.environ.get(cfg.get("brave_api_key_env", "BRAVE_API_KEY"), "").strip()
    active = _resolve_provider(cfg.get("provider"))
    available = ["bing", "ddg"]
    if tavily_key:
        available.append("tavily")
    if brave_key:
        available.append("brave")
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "configured_provider": cfg.get("provider", "auto"),
        "active_provider": active,
        "available_providers": available,
        "max_results": int(cfg.get("max_results", 10)),
    }


# ── 公共工具 ──────────────────────────────────────────
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _host_of(url):
    try:
        netloc = urllib.parse.urlparse(url).netloc or ""
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


_DATE_RE = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b([A-Z][a-z]{2,8})\s+\d{1,2},?\s+\d{4}\b"),
    re.compile(r"\b(\d{1,2}\s+(?:小时|天|周|个月|年)前)\b"),
    re.compile(r"\b(\d{1,2}\s+(?:hours|days|weeks|months|years)\s+ago)\b", re.I),
]


def _extract_date(text):
    """从摘要文本中尽力提取时间信息；提取不到返回空串。"""
    if not text:
        return ""
    for rx in _DATE_RE:
        m = rx.search(text)
        if m:
            return m.group(0).strip()
    return ""


def _normalize(results, provider):
    """把各服务商原始结果归一化为统一结构。"""
    out = []
    for i, r in enumerate(results, start=1):
        url = (r.get("url") or "").strip()
        title = (r.get("title") or "").strip() or "(无标题)"
        snippet = (r.get("snippet") or "").strip()
        published = r.get("published_time") or _extract_date(snippet)
        out.append({
            "position": i,
            "title": title,
            "url": url,
            "snippet": snippet,
            "source": _host_of(url),
            "published_time": published,
        })
    return out


# ── 查询改写 / 关键词提取（长自然语言问句 -> 检索词）──────
# 搜索引擎（尤其 HTML 版）对「一长句自然语言」理解差，常抓住开头的
# 高频/歧义词（如「最近」）当作检索词，返回完全无关的结果。
# 因此先把口语化的问句压缩成「实体 + 核心名词」的关键词查询。
_QUERY_NOISE = sorted(set([
    # 疑问/口语框架（长词优先替换，避免「最近一场」被「最近」拆坏）
    "最近一场", "下一场", "上一场", "第一场", "一场", "什么时候", "什么时候开始",
    "是什么时候", "是怎么回事", "是什么", "怎么样", "怎样的", "怎样", "如何",
    "是多少", "有多少", "多少", "几号", "哪天", "多久", "是多",
    "请问", "请帮", "帮我", "帮忙", "帮查", "查一下", "查询", "查查", "搜一下",
    "搜索", "找一下", "告诉我", "我想知道", "想知道", "想了解", "了解一下",
    "有没有", "是不是", "的话", "的时候",
    # 时间/程度噪声词（本身无信息量，且极易误导搜索引擎排序）
    "时候", "最近", "最新", "现在", "当前", "目前", "今天", "今日", "此刻", "实时",
    "相关", "情况", "方面", "这些", "那些", "这个", "那个", "一些",
    # 单字虚词
    "的", "了", "着", "吗", "呢", "吧", "啊", "呀", "哦", "嘛",
]), key=len, reverse=True)


def rewrite_query(text):
    """把自然语言问句改写为精简的检索关键词（保留实体与核心名词）。"""
    q = (text or "").strip()
    if not q:
        return q
    s = q
    for w in sorted(_QUERY_NOISE, key=len, reverse=True):
        s = s.replace(w, " ")
    parts = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9._+-]*", s)
    out = " ".join(parts).strip()
    return out if len(out) >= 2 else q


_STOP_KW = set(
    "的了吗呢吧啊呀哦是在有和与及对对于关于我这那哪怎么怎么样如何什么为什么"
    "请问一下还有没有你们自己他们它们这个那个现在今天目前最新最近"
    "可以能够应该帮我帮助需要希望告诉介绍推荐查找查询搜索一些非常很挺"
    .replace(" ", ""))


def extract_keywords(text):
    """提取内容关键词：中文 2-gram + 英文/数字词元，并剔除停用词。
    停用词只排除「无信息量的疑问/时间/程度词」，让相关性由实体内容词决定。"""
    q = (text or "").lower()
    kws = set()
    for seg in _CJK_RE.findall(q):
        if len(seg) == 1:
            kws.add(seg)
        else:
            for i in range(len(seg) - 1):
                kws.add(seg[i:i + 2])
    for tok in re.findall(r"[a-z0-9][a-z0-9._+-]*", q):
        kws.add(tok)
    return kws - _STOP_KW


# ── 正文摘要抓取（snippet 常无关键数据，需取页面正文补充）──
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


# 承载「具体数据」的句式特征：日期、时间、价格、温度等
_DATEISH_RE = re.compile(
    r"\d{4}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
    r"\d{1,2}:\d{2}|\d+\s*元|\d+\s*万|\d+\s*℃|\d+\s*度|\d+\s*%"
)


def fetch_page_excerpt(url, keywords, timeout=6, max_sentences=3, max_chars=400):
    """抓取结果页正文，挑出含关键词或含具体数据（日期/时间/价格）的句子。

    搜索引擎摘要常常只有站点介绍、没有实际数值（如演唱会日期、票价、开票时间），
    仅靠 snippet 模型仍无法回答。因此补抓正文并按「含实体词 / 含日期数字」打分排序，
    为模型提供可引用的具体信息。
    """
    if not url:
        return ""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        enc = resp.encoding
        if not enc or enc.lower() == "iso-8859-1":
            enc = "utf-8"
        html = resp.content.decode(enc, "ignore")
        html = _SCRIPT_RE.sub(" ", html)
        text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html))
        text = _unescape(text)

        kws = set(keywords or ())
        scored = []
        for s in re.split(r"(?<=[。！？!?；;])", text):
            s = s.strip()
            if len(s) < 8:
                continue
            score = 0
            if kws and any(k in s for k in kws):
                score += 2
            if _DATEISH_RE.search(s):
                score += 3          # 含具体数值的句子优先（这才是模型需要的答案素材）
            if score:
                scored.append((score, len(scored), s))
            if len(scored) >= max_sentences * 6:
                break
        # 先按分值降序，同分保持原文顺序，避免拼出的摘录语义跳跃
        scored.sort(key=lambda x: (-x[0], x[1]))
        picked = [s for _, _, s in scored[:max_sentences]]
        return " ".join(picked)[:max_chars]
    except Exception:
        return ""


# ── DuckDuckGo（免密钥 HTML 端点）──────────────────────
class _DDGParser(HTMLParser):
    """解析 DuckDuckGo HTML 结果页。

    兼容两种常见结构：
      - 标题：<a class="result__a" href="URL">TITLE</a>
      - 摘要：<a class="result__snippet">…</a> 或 <div class="result__snippet">…</div>
    """
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._cur = None
        self._field = None      # "title" | "snippet"
        self._buf = []
        self._snip_tag = None

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        cls = " " + attrs_d.get("class", "") + " "
        if " result__a " in cls:
            self._cur = {"title": "", "url": attrs_d.get("href", ""), "snippet": ""}
            self._field = "title"
            self._buf = []
        elif " result__snippet " in cls and self._cur is not None:
            self._field = "snippet"
            self._snip_tag = tag
            self._buf = []

    def handle_data(self, data):
        if self._field in ("title", "snippet"):
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._field == "title" and tag == "a":
            if self._cur is not None:
                self._cur["title"] = "".join(self._buf).strip()
            self._field = None
        elif self._field == "snippet" and tag == self._snip_tag:
            if self._cur is not None:
                self._cur["snippet"] = "".join(self._buf).strip()
                self.results.append(self._cur)
                self._cur = None
            self._field = None
            self._snip_tag = None


def _search_ddg(query, max_results, timeout):
    """DuckDuckGo 免密钥搜索：先试 HTML 端点，失败再试 Lite 端点。
    任一端点网络异常则标记 had_error（由上层转为「网络异常」提示）；
    若可达但无结果则返回空列表（由上层展示「无结果」空态）。"""
    endpoints = [
        ("post", "https://html.duckduckgo.com/html/", {"q": query, "kl": "cn-zh"}),
        ("get", "https://lite.duckduckgo.com/lite/", {"q": query}),
    ]
    had_error = False
    for method, url, payload in endpoints:
        try:
            if method == "post":
                resp = requests.post(url, data=payload, headers=_HEADERS, timeout=timeout)
            else:
                resp = requests.get(url, params=payload, headers=_HEADERS, timeout=timeout)
            resp.raise_for_status()
            parser = _DDGParser()
            parser.feed(resp.text)
            results = parser.results[:max_results]
            if results:
                return results
        except requests.RequestException:
            had_error = True
    if had_error:
        raise requests.RequestException("DuckDuckGo 网络请求失败")
    return []


# ── Bing（免密钥 HTML，国内可直连）──────────────────────
# 结果块结构（cn.bing.com/search）：
#   <li class="b_algo"> ...
#     <h2 class=""><a ... href="URL">TITLE（含 <strong>）</a></h2>
#     <div class="b_caption"><p class="b_lineclamp2">SNIPPET</p></div>
#   知识卡片类结果则使用 <a class="tilk" aria-label="TITLE" href="URL">
_BING_BLOCK_SPLIT = re.compile(r'<li class="b_algo"[^>]*>')
_BING_H2_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
# 知识卡片类结果：<a class="tilk" ... aria-label="TITLE" ... href="URL">
_BING_TILK_HREF_RE = re.compile(r'<a class="tilk"[^>]*href="([^"]+)"', re.S)
_BING_TILK_LABEL_RE = re.compile(r'<a class="tilk"[^>]*aria-label="([^"]*)"', re.S)
_BING_SNIP_RE = re.compile(r'<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', re.S)


def _bing_clean(s):
    return _unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _search_bing(query, max_results, timeout):
    """Bing 免密钥搜索（cn.bing.com 结果页 HTML 解析）。

    成功返回原始结果列表；可达但无结果返回空列表；网络异常向上抛 RequestException。
    """
    params = {"q": query, "setlang": "zh-CN", "mkt": "zh-CN", "ensearch": "0"}
    resp = requests.get("https://cn.bing.com/search",
                        params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"  # Bing 中文页显式按 UTF-8 解码，避免误判为 GBK

    # 按 b_algo 块切分（比非贪婪 </li> 更稳健，可避免嵌套 <li> 提前截断）
    segments = _BING_BLOCK_SPLIT.split(resp.text)
    raw = []
    for seg in segments[1:]:
        nxt = seg.find('<li class="b_algo"')
        if nxt != -1:
            seg = seg[:nxt]

        m = _BING_H2_RE.search(seg)
        if m:
            url = m.group(1).strip()
            title = _bing_clean(m.group(2))
        else:
            mt_href = _BING_TILK_HREF_RE.search(seg)
            if not mt_href:
                continue
            url = mt_href.group(1).strip()
            mt_label = _BING_TILK_LABEL_RE.search(seg)
            title = _bing_clean(mt_label.group(1)) if mt_label else ""
        if not url.startswith("http"):
            continue

        sm = _BING_SNIP_RE.search(seg)
        snippet = _bing_clean(sm.group(1)) if sm else ""
        raw.append({"title": title or "(无标题)", "url": url,
                    "snippet": snippet, "published_time": ""})
        if len(raw) >= max_results:
            break
    return raw


# ── Tavily（需 API Key）────────────────────────────────
def _search_tavily(query, max_results, timeout, api_key):
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_images": False,
        },
        headers=_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = []
    for r in data.get("results", [])[:max_results]:
        raw.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
            "published_time": r.get("published_date") or "",
        })
    return raw


# ── Brave（需 API Key）─────────────────────────────────
def _search_brave(query, max_results, timeout, api_key):
    resp = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": max_results, "search_lang": "zh-hans"},
        headers={**_HEADERS, "X-Subscription-Token": api_key,
                 "Accept": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = []
    for r in data.get("web", {}).get("results", [])[:max_results]:
        raw.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("description", ""),
            "published_time": r.get("page_age") or r.get("age") or "",
        })
    return raw


# ── 统一入口 ──────────────────────────────────────────
def _invoke_provider(prov, query, max_results, timeout, cfg):
    """调用指定服务商，返回未归一化的原始结果列表。"""
    if prov == "tavily":
        key = os.environ.get(cfg.get("tavily_api_key_env", "TAVILY_API_KEY"), "").strip()
        return _search_tavily(query, max_results, timeout, key)
    if prov == "brave":
        key = os.environ.get(cfg.get("brave_api_key_env", "BRAVE_API_KEY"), "").strip()
        return _search_brave(query, max_results, timeout, key)
    if prov == "bing":
        return _search_bing(query, max_results, timeout)
    return _search_ddg(query, max_results, timeout)


def _fallback_order(prov):
    """主引擎失败 / 空结果时的备用顺序（免密钥引擎之间互备）。"""
    order = [prov]
    if prov in ("bing", "ddg"):
        order += [p for p in ("bing", "ddg") if p != prov]
    return order


def web_search(query, max_results=None, provider=None):
    """
    执行联网搜索，返回：
        { "provider", "query", "effective_query", "count", "results" }

    容错设计（对应「妥善处理连接失败等异常情况」）：
    - 主引擎返回空结果或网络异常时，自动按备用顺序尝试其它免密钥引擎
      （Bing 偶发限流返回空页时可由 DDG 兜底，反之亦然）；
    - 所有引擎均网络异常 -> 抛 RuntimeError("网络请求失败…")，由路由转 502；
    - 有引擎可达但确无结果 -> 正常返回空结果（前端展示「无结果」空态）。
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("搜索内容不能为空")

    cfg = _search_cfg()
    prov = _resolve_provider(provider)
    if max_results is None or max_results <= 0:
        max_results = int(cfg.get("max_results", 10))
    max_results = min(max_results, 20)
    timeout = int(cfg.get("timeout", 10))

    reached = False          # 是否有任一引擎成功建立连接并返回
    results = []
    used_provider = prov
    effective = query

    for p in _fallback_order(prov):
        # 检索词改写：HTML 版搜索引擎（Bing/DDG）对长自然语言问句理解差，
        # 常抓住开头的歧义词（如「最近」）当检索词；先压缩为「实体 + 核心名词」。
        # API 版服务商（Tavily/Brave）本身支持自然语言，保持原句不改写。
        eff = query if p in ("tavily", "brave") else (rewrite_query(query) or query)
        # 备用引擎给更短的超时：避免主引擎空结果时被备用引擎的长时间超时拖慢
        att_timeout = timeout if p == prov else min(timeout, 5)
        try:
            raw = _invoke_provider(p, eff, max_results, att_timeout, cfg)
        except requests.RequestException as e:
            logger.warning("搜索请求失败 provider=%s: %s", p, e)
            continue
        except Exception:
            logger.exception("搜索处理异常 provider=%s", p)
            continue
        reached = True
        if raw:
            results = _normalize(raw, p)
            used_provider = p
            effective = eff
            break

    if not reached:
        raise RuntimeError("网络请求失败，请检查网络连接后重试")

    return {
        "provider": used_provider,
        "query": query,
        "effective_query": effective,
        "count": len(results),
        "results": results,
    }
