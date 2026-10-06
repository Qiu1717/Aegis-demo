# -*- coding: utf-8 -*-
"""
apps/guard/services.py — 内容护栏规则的持久化、匹配引擎与审计日志

模块边界
--------
本模块**不依赖 Flask**，只依赖标准库与 apps.paths / apps.logger，
可以在路由层、脚本、打包产物中安全导入。

持久化
------
规则文件：<data_dir>/guard_rules.json（data_dir 见 apps.paths，支持 AEGIS_DATA_DIR 覆盖）。
写入沿用 apps/aichat/state.py 的原子写模式：ensure_dirs → 写 .tmp → os.replace。
文件缺失 / 损坏 / 顶层非对象 / 字段非法时一律**回退到默认规则**
（内置 5 个类别 + 一批种子关键词），细节写 logger，**绝不向上抛异常**。

数据结构（version=1）
--------------------
    {
      "version": 1,
      "enabled": true,
      "refusal_message": "该问题涉及受限内容，我无法回答",
      "categories": [{"id": str, "label": str, "built_in": bool, "enabled": bool}],
      "keywords": [{
          "id": str, "word": str, "mode": "exact"|"fuzzy", "priority": int,
          "enabled": bool, "category": str, "alias": [str, ...]
      }]
    }

匹配引擎
--------
见 normalize() / compact() / check()：
NFKC（全角转半角）→ 去零宽字符 → 去全部空白 → 转小写得到「归一化串」；
再去掉一切非字母/数字/汉字字符得到「紧凑串」，用于对抗 `枪-支`、`枪*支` 这类插入符号的变体。
关键词本体与 alias 变体同样归一化后参与匹配；
`fuzzy` 在归一化串或紧凑串中子串命中即可，`exact` 走等值 / 词边界判定；
按 priority 从高到低依次判定，命中即返回（便于审计定位到具体规则）。
规则在进程内按「文件 mtime + size」缓存，变化才重载；单次匹配为纯字符串操作，O(关键词数)。
"""
import json
import os
import re
import threading
import unicodedata
import uuid
from datetime import datetime
from typing import Any, Dict, List, NamedTuple, Optional, Pattern, Tuple

from apps import logger, paths

# ── 常量 ──────────────────────────────────────────────────
#: 当前支持的规则文件版本号（导入时会校验）
RULES_VERSION = 1

#: 规则文件名（落在用户数据目录下）
RULES_FILENAME = "guard_rules.json"

#: 默认拒答话术
DEFAULT_REFUSAL_MESSAGE = "该问题涉及受限内容，我无法回答"

#: 匹配模式：精确 / 模糊
MODE_EXACT = "exact"
MODE_FUZZY = "fuzzy"
MODES = (MODE_EXACT, MODE_FUZZY)

#: 审计日志里保留的提问片段长度上限（避免落盘过长或敏感内容）
MAX_AUDIT_SNIPPET = 60

#: 单次批量新增关键词的条数上限
MAX_BATCH = 500

#: 关键词 / 类别字段长度上限
MAX_WORD_LEN = 120
MAX_LABEL_LEN = 64
MAX_ID_LEN = 64
MAX_MESSAGE_LEN = 200
MAX_ALIAS = 50

#: 默认优先级
DEFAULT_PRIORITY = 100

_ZERO_WIDTH_CHARS = (
    "\u00ad"                       # 软连字符 SOFT HYPHEN
    "\u180e"                       # 蒙古文元音分隔符
    "\u200b\u200c\u200d\u200e\u200f"  # 零宽空格/不连字/连字/LRM/RLM
    "\u2028\u2029"                 # 行/段分隔符
    "\u202a\u202b\u202c\u202d\u202e"  # 双向文本控制符
    "\u2060\u2061\u2062\u2063\u2064"  # 不可见运算符
    "\u206a\u206b\u206c\u206d\u206e\u206f"
    "\ufeff"                       # BOM / 零宽不换行空格
)

#: 零宽 / 不可见字符（用于对抗「关键\u200b词」这类绕过）
_ZERO_WIDTH_RE = re.compile("[%s]" % re.escape(_ZERO_WIDTH_CHARS))

#: 未命中（放行）时的返回结构模板
MISS_RESULT: Dict[str, Any] = {
    "blocked": False,
    "reason": "",
    "category": "",
    "category_label": "",
    "keyword": "",
    "keyword_id": "",
    "matched": "",
    "mode": "",
    "priority": 0,
}


# ── 文本归一化（对抗常见绕过）──────────────────────────────
def normalize_forms(text: Any) -> Tuple[str, str, str]:
    """
    一次性算出三种匹配形态，避免重复计算：

    :return: (归一化串, 紧凑串, 保分隔符串)
        归一化串   NFKC → 去零宽 → 去全部空白 → 小写（模糊匹配与中文精确匹配用）
        紧凑串     归一化串再去掉一切标点符号（对抗 `枪-支` 这类分隔绕过）
        保分隔符串 NFKC → 去零宽 → 小写，但把标点 / 空白**替换为单个空格**。
                  它是英文 exact 模式词边界判定的专用文本：
                  若直接用去掉空白的归一化串，"I love Python" 会变成
                  "ilovepython"，词边界随之消失、永远匹配不上。
    """
    base = unicodedata.normalize("NFKC", "" if text is None else str(text))
    base = _ZERO_WIDTH_RE.sub("", base).lower()
    norm = "".join(base.split())
    spaced = " ".join("".join(ch if ch.isalnum() else " " for ch in base).split())
    return norm, compact(norm), spaced


def normalize(text: Any) -> str:
    """
    归一化文本：NFKC → 去零宽字符 → 去全部空白 → 转小写。

    四步各自的作用：
        NFKC     全角字符、兼容字符归一（ＱＱ → QQ，① → 1）
        去零宽   拆掉插入的不可见字符（关\u200b键词 → 关键词）
        去空白   拆掉空格 / 制表 / 换行（枪 支 → 枪支）
        转小写   英文大小写混写统一（PoRn → porn）
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    value = unicodedata.normalize("NFKC", text)
    value = _ZERO_WIDTH_RE.sub("", value)
    value = "".join(value.split())
    return value.lower()


def compact(text: str) -> str:
    """
    紧凑串：只保留字母 / 数字 / 汉字（str.isalnum 对汉字为真），
    丢弃一切标点与符号，用于对抗 `枪-支`、`枪*支`、`枪.支` 这类分隔绕过。

    入参应当是已经 normalize() 过的字符串。
    """
    return "".join(ch for ch in text if ch.isalnum())


def _has_cjk(text: str) -> bool:
    """是否包含中日韩字符（含假名）；有则不按英文词边界处理"""
    for ch in text:
        code = ord(ch)
        if (0x3040 <= code <= 0x30FF          # 日文假名
                or 0x3400 <= code <= 0x4DBF   # 扩展 A
                or 0x4E00 <= code <= 0x9FFF   # 中日韩统一表意文字
                or 0xF900 <= code <= 0xFAFF   # 兼容表意文字
                or 0xAC00 <= code <= 0xD7AF):  # 谚文音节
            return True
    return False


def audit_snippet(text: Any, limit: int = MAX_AUDIT_SNIPPET) -> str:
    """把提问压成一行并截断到 limit 个字符（超出部分用 … 标记），供审计日志使用"""
    raw = "" if text is None else str(text)
    snippet = " ".join(raw.split())
    if len(snippet) > limit:
        snippet = snippet[:limit] + "…"
    return snippet


# ── 默认规则（内置类别 + 种子关键词）──────────────────────
def _default_categories() -> List[Dict[str, Any]]:
    """内置 5 个类别（built_in=True，禁止删除，可停用）"""
    return [
        {"id": "porn", "label": "涉黄", "built_in": True, "enabled": True},
        {"id": "violence", "label": "涉暴", "built_in": True, "enabled": True},
        {"id": "crime", "label": "违法犯罪", "built_in": True, "enabled": True},
        {"id": "privacy", "label": "隐私泄露", "built_in": True, "enabled": True},
        {"id": "politics", "label": "政治敏感", "built_in": True, "enabled": True},
    ]


def _default_keywords() -> List[Dict[str, Any]]:
    """
    种子关键词：覆盖「枪支购买 / 军事基地位置 / 制毒（冰毒）」等典型受限提问，
    并为英文大小写混写、全角输入、插入符号等变体留出 alias 兜底。

    word 是规则本体，alias 是等价写法；两者都参与匹配，归一化发生在编译期。
    """
    return [
        # ── 违法犯罪：枪支 ──────────────────────────────────
        {"id": "k_gun_buy", "word": "购买枪支", "mode": MODE_FUZZY,
         "priority": 100, "enabled": True, "category": "crime",
         "alias": ["买到枪支", "买枪支", "枪支购买", "购买枪械", "买真枪",
                   "买到真枪", "搞到枪支", "弄一把枪", "哪里可以买到枪",
                   "怎么买到枪", "枪支购买渠道"]},
        {"id": "k_gun_make", "word": "制造枪支", "mode": MODE_FUZZY,
         "priority": 100, "enabled": True, "category": "crime",
         "alias": ["自制枪支", "组装枪支", "改装枪支", "3d打印枪支", "仿制枪支"]},
        # ── 违法犯罪：毒品（冰毒）──────────────────────────
        {"id": "k_drug_make", "word": "制作冰毒", "mode": MODE_FUZZY,
         "priority": 100, "enabled": True, "category": "crime",
         "alias": ["制造冰毒", "冰毒制作", "制冰毒", "制取冰毒", "如何做冰毒",
                   "怎么做冰毒", "冰毒配方", "冰毒的合成", "冰 毒", "冰Du",
                   "IceDu", "甲基苯丙胺"]},
        {"id": "k_drug_trade", "word": "贩卖毒品", "mode": MODE_FUZZY,
         "priority": 90, "enabled": True, "category": "crime",
         "alias": ["毒品交易", "毒品买卖", "购买毒品", "冰毒批发", "毒品货源"]},
        # ── 违法犯罪：爆炸物 / 入侵 ────────────────────────
        {"id": "k_bomb_make", "word": "制造炸弹", "mode": MODE_FUZZY,
         "priority": 100, "enabled": True, "category": "crime",
         "alias": ["制作炸弹", "自制炸药", "炸药的配方", "怎么制作炸药",
                   "如何制造爆炸物"]},
        {"id": "k_hack", "word": "入侵他人电脑", "mode": MODE_FUZZY,
         "priority": 80, "enabled": True, "category": "crime",
         "alias": ["黑客入侵网站", "入侵别人的服务器", "制作病毒木马",
                   "ddos攻击工具", "盗取他人账号"]},
        # ── 涉暴 ───────────────────────────────────────────
        {"id": "k_kill", "word": "如何杀人", "mode": MODE_FUZZY,
         "priority": 100, "enabled": True, "category": "violence",
         "alias": ["怎么杀人", "杀人方法", "杀人的手法", "毒死别人的方法",
                   "投毒的方法"]},
        {"id": "k_selfharm", "word": "自杀的方法", "mode": MODE_FUZZY,
         "priority": 100, "enabled": True, "category": "violence",
         "alias": ["如何自杀", "轻生的方式", "自残的方法"]},
        # ── 隐私泄露 ───────────────────────────────────────
        {"id": "k_doxx", "word": "人肉搜索", "mode": MODE_FUZZY,
         "priority": 90, "enabled": True, "category": "privacy",
         "alias": ["人肉别人", "开盒他人", "通过手机号查到住址",
                   "身份证号查住址", "查他人隐私信息", "定位他人手机"]},
        # ── 涉黄 ───────────────────────────────────────────
        {"id": "k_porn_video", "word": "色情视频资源", "mode": MODE_FUZZY,
         "priority": 80, "enabled": True, "category": "porn",
         "alias": ["成人影片下载", "色情网站入口", "黄色网址大全",
                   "色情直播地址"]},
        {"id": "k_porn_general", "word": "儿童色情", "mode": MODE_FUZZY,
         "priority": 100, "enabled": True, "category": "porn",
         "alias": ["未成年色情", "雏妓"]},
        # ── 政治敏感：军事设施位置 ─────────────────────────
        {"id": "k_mil_base", "word": "军事基地的位置", "mode": MODE_FUZZY,
         "priority": 90, "enabled": True, "category": "politics",
         "alias": ["军事基地在哪里", "军事基地分别在哪里", "军事基地分布",
                   "军事基地都分布在", "军事基地位置查询", "军事禁区位置",
                   "导弹发射井位置", "军用机场位置", "部队驻地查询"]},
        {"id": "k_politics_extreme", "word": "颠覆国家政权", "mode": MODE_FUZZY,
         "priority": 100, "enabled": True, "category": "politics",
         "alias": ["推翻国家政权", "分裂国家的办法", "非法集会游行组织"]},
    ]


def _default_rules() -> Dict[str, Any]:
    """全新安装的默认规则（每次调用返回新的深层副本，调用方可随意修改）"""
    return {
        "version": RULES_VERSION,
        "enabled": True,
        "refusal_message": DEFAULT_REFUSAL_MESSAGE,
        "categories": _default_categories(),
        "keywords": _default_keywords(),
    }


# ── 字段工具 ──────────────────────────────────────────────
def _clean_str(value: Any, default: str = "", max_len: int = MAX_WORD_LEN) -> str:
    """去空白 + 截断；空则回落到 default"""
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    return text[:max_len]


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """把各种写法（bool / 0,1 / "true" / "yes" / "on"）转成布尔"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "on"):
        return True
    if text in ("false", "0", "no", "n", "off"):
        return False
    return default


def _coerce_int(value: Any, default: int = DEFAULT_PRIORITY,
                low: int = -1000, high: int = 1000) -> int:
    """转成 int 并夹在区间内；非法值回落到 default"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _coerce_mode(value: Any) -> str:
    """模式归一化：只接受 exact / fuzzy，其他一律 fuzzy"""
    mode = _clean_str(value, "", 16).lower()
    return mode if mode in MODES else MODE_FUZZY


def _new_id(prefix: str) -> str:
    """生成短 ID（形如 k_9f3c1a2b / c_1b2c3d4e）"""
    return "%s_%s" % (prefix, uuid.uuid4().hex[:8])


def _clean_alias(value: Any) -> List[str]:
    """alias 归一化：去重、去空、限条数与长度"""
    if not isinstance(value, (list, tuple)):
        return []
    result: List[str] = []
    for item in value:
        text = _clean_str(item, "", MAX_WORD_LEN)
        if text and text not in result:
            result.append(text)
        if len(result) >= MAX_ALIAS:
            break
    return result


# ── 校验 / 规范化 ─────────────────────────────────────────
def _sanitize_category(raw: Any, index: int = 0) -> Tuple[Optional[Dict[str, Any]], str]:
    """校验并规范化一个类别；不合法返回 (None, 中文原因)"""
    if not isinstance(raw, dict):
        return None, "categories[%d] 必须是对象" % index
    label = _clean_str(raw.get("label", raw.get("name")), "", MAX_LABEL_LEN)
    if not label:
        return None, "categories[%d] 的名称不能为空" % index
    cid = _clean_str(raw.get("id", raw.get("category_id")), "", MAX_ID_LEN)
    return {
        "id": cid or _new_id("c"),
        "label": label,
        "built_in": _coerce_bool(raw.get("built_in"), False),
        "enabled": _coerce_bool(raw.get("enabled"), True),
    }, ""


def _sanitize_keyword(raw: Any, index: int = 0,
                      category_ids: Optional[set] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    校验并规范化一个关键词；不合法返回 (None, 中文原因)。

    category_ids 非空时会校验 category 是否指向存在的类别（导入时的枚举校验）。
    """
    if not isinstance(raw, dict):
        return None, "keywords[%d] 必须是对象" % index
    word = _clean_str(raw.get("word", raw.get("text")), "", MAX_WORD_LEN)
    if not word:
        return None, "keywords[%d] 的关键词内容不能为空" % index
    if not normalize(word):
        return None, "keywords[%d] 的关键词内容无效" % index

    mode = _clean_str(raw.get("mode"), "", 16).lower()
    if raw.get("mode") is not None and mode not in MODES:
        return None, "keywords[%d] 的 mode 只能是 exact 或 fuzzy" % index
    if "priority" in raw:
        try:
            int(raw.get("priority"))
        except (TypeError, ValueError):
            return None, "keywords[%d] 的 priority 必须是整数" % index
    enabled_raw = raw.get("enabled")
    if enabled_raw is not None and not isinstance(enabled_raw, bool) \
            and not isinstance(enabled_raw, (int, float)) \
            and str(enabled_raw).strip().lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        return None, "keywords[%d] 的 enabled 必须是布尔值" % index
    alias = raw.get("alias")
    if alias is not None and not isinstance(alias, (list, tuple)):
        return None, "keywords[%d] 的 alias 必须是数组" % index

    category = _clean_str(raw.get("category"), "", MAX_ID_LEN)
    if category and category_ids is not None and category not in category_ids:
        return None, "keywords[%d] 的 category「%s」不存在" % (index, category)

    return {
        "id": _clean_str(raw.get("id", raw.get("keyword_id")), "", MAX_ID_LEN) or _new_id("k"),
        "word": word,
        "mode": _coerce_mode(raw.get("mode")),
        "priority": _coerce_int(raw.get("priority"), DEFAULT_PRIORITY),
        "enabled": _coerce_bool(raw.get("enabled"), True),
        "category": category,
        "alias": _clean_alias(alias),
    }, ""


def _dedupe_ids(items: List[Dict[str, Any]], prefix: str) -> None:
    """原地去重自增 ID（重复或缺失的重新生成），保证后续 CRUD 定位唯一"""
    seen = set()
    for item in items:
        cid = item.get("id") or ""
        if not cid or cid in seen:
            cid = _new_id(prefix)
            item["id"] = cid
        seen.add(cid)


def _sanitize_rules(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    把任意来源的 dict 洗成合法规则结构。

    策略：**尽量宽容** —— 顶层缺失的字段补默认值，单条非法条目被丢弃并记录日志，
    而不是整体失败（用户手写配置时少踩坑）；但 mode / category 这类**枚举**必须合法。
    """
    if not isinstance(raw, dict):
        return _default_rules()

    enabled_raw = raw.get("enabled")
    if enabled_raw is not None and not isinstance(enabled_raw, bool) \
            and not isinstance(enabled_raw, (int, float)) \
            and str(enabled_raw).strip().lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        logger.warning("内容护栏配置 enabled 字段类型非法，按 True 处理")

    version_raw = raw.get("version", RULES_VERSION)
    try:
        version = int(version_raw)
    except (TypeError, ValueError):
        version = RULES_VERSION

    categories: List[Dict[str, Any]] = []
    raw_categories = raw.get("categories")
    if isinstance(raw_categories, list):
        for index, item in enumerate(raw_categories):
            category, reason = _sanitize_category(item, index)
            if category is None:
                logger.warning("丢弃非法类别配置：%s", reason)
                continue
            categories.append(category)
    elif raw_categories is not None:
        logger.warning("内容护栏配置 categories 不是数组，已回退为内置类别")
    if not categories:
        categories = _default_categories()
    _dedupe_ids(categories, "c")
    category_ids = {c["id"] for c in categories}

    keywords: List[Dict[str, Any]] = []
    raw_keywords = raw.get("keywords")
    if isinstance(raw_keywords, list):
        for index, item in enumerate(raw_keywords):
            keyword, reason = _sanitize_keyword(item, index, category_ids)
            if keyword is None:
                logger.warning("丢弃非法关键词配置：%s", reason)
                continue
            keywords.append(keyword)
    elif raw_keywords is not None:
        logger.warning("内容护栏配置 keywords 不是数组，已回退为种子关键词")
    if raw_keywords is None and isinstance(raw, dict) and "keywords" not in raw:
        # 只有明确没写过 keywords 才回填种子（避免用户清空后又被塞回来）
        keywords = _default_keywords()
    _dedupe_ids(keywords, "k")

    return {
        "version": version,
        "enabled": _coerce_bool(raw.get("enabled"), True),
        "refusal_message": _clean_str(raw.get("refusal_message"), DEFAULT_REFUSAL_MESSAGE,
                                      MAX_MESSAGE_LEN),
        "categories": categories,
        "keywords": keywords,
    }


# ── 读写（原子写，失败不抛）───────────────────────────────
def rules_path() -> str:
    """规则文件路径：<data_dir>/guard_rules.json"""
    return str(paths.data_dir() / RULES_FILENAME)


def load() -> Dict[str, Any]:
    """
    读取并规范化规则。

    文件不存在 / 损坏 / 顶层非对象 / 校验异常时回退到默认规则并记日志，
    **绝不向上抛异常**。
    """
    path = rules_path()
    existed = False
    raw = None
    try:
        if os.path.isfile(path):
            existed = True
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
    except (OSError, ValueError):
        logger.exception("读取内容护栏配置失败，回退默认规则 path=%s", path)
        return _default_rules()

    if isinstance(raw, dict):
        try:
            return _sanitize_rules(raw)
        except Exception:
            logger.exception("校验内容护栏配置失败，回退默认规则 path=%s", path)
            return _default_rules()

    if existed:
        logger.warning("内容护栏配置顶层不是 JSON 对象，回退默认规则 path=%s", path)
    return _default_rules()


def save_rules(data: Dict[str, Any]) -> bool:
    """
    原子写规则文件（ensure_dirs → 写 .tmp → os.replace），并让缓存失效。

    写入前会先 _sanitize_rules 一遍，保证落盘内容永远是合法结构。
    失败返回 False 并记日志，**绝不向上抛异常**。
    """
    try:
        rules = _sanitize_rules(data if isinstance(data, dict) else {})
    except Exception:
        logger.exception("校验内容护栏配置失败，未写入文件")
        return False

    try:
        paths.ensure_dirs()
        target = rules_path()
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        invalidate_cache()
        return True
    except (OSError, TypeError, ValueError):
        logger.exception("写入内容护栏配置失败 path=%s", rules_path())
        return False


# ── 缓存（按文件 mtime + size 变化重载）───────────────────
class _Rule(NamedTuple):
    """编译后的单条匹配规则（一个关键词本体或一条 alias 变体）"""

    variant: str                    # 归一化后的匹配串
    compact: str                    # 紧凑串（去标点）
    has_cjk: bool                   # 是否含中日韩字符
    pattern: Optional[Pattern]      # 英文 exact 模式用的词边界正则
    mode: str                       # exact / fuzzy
    priority: int
    keyword_id: str
    word: str                       # 命中的关键词本体（审计展示用）
    category: str                   # 类别 id


_CACHE_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {"path": "", "mtime": None, "size": -1, "rules": {}, "items": []}


def invalidate_cache() -> None:
    """清空规则缓存（写操作后调用，下一次访问会重新读盘）"""
    with _CACHE_LOCK:
        _CACHE.update({"path": "", "mtime": None, "size": -1, "rules": {}, "items": []})


def _file_stat() -> Tuple[Optional[float], int]:
    """取规则文件的 (mtime, size)；不存在时返回 (None, -1)"""
    try:
        stat = os.stat(rules_path())
        return stat.st_mtime, int(stat.st_size)
    except OSError:
        return None, -1


def _compile(rules: Dict[str, Any]) -> List[_Rule]:
    """
    把规则编译成可快速匹配的规则表：

        · 跳过全局关闭之外的所有 disabled 关键词；
        · 类别被停用（enabled=false）时，其下关键词不参与匹配；
        · 无 category 或 category 指向不存在类别的关键词视为「无类别」，始终参与匹配；
        · 关键词本体与 alias 变体各生成一条规则，按 priority 降序排列。
    """
    category_enabled: Dict[str, bool] = {}
    for category in rules.get("categories") or []:
        if not isinstance(category, dict):
            continue
        cid = category.get("id") or ""
        category_enabled[cid] = bool(category.get("enabled", True))

    compiled: List[_Rule] = []
    for keyword in rules.get("keywords") or []:
        if not isinstance(keyword, dict):
            continue
        if not bool(keyword.get("enabled", True)):
            continue
        cid = keyword.get("category") or ""
        if cid and cid in category_enabled and not category_enabled[cid]:
            continue

        word = _clean_str(keyword.get("word"), "", MAX_WORD_LEN)
        variants = [("word", word)]
        for alias in keyword.get("alias") or []:
            if isinstance(alias, str) and alias.strip():
                variants.append(("alias", alias.strip()))

        mode = _coerce_mode(keyword.get("mode"))
        priority = _coerce_int(keyword.get("priority"), DEFAULT_PRIORITY)
        for _origin, variant_text in variants:
            variant = normalize(variant_text)
            if not variant:
                continue
            compact_text = compact(variant)
            cjk = _has_cjk(variant)
            pattern = None
            if mode == MODE_EXACT and not cjk:
                pattern = re.compile(r"(?<!\w)%s(?!\w)" % re.escape(variant))
            compiled.append(_Rule(
                variant=variant,
                compact=compact_text,
                has_cjk=cjk,
                pattern=pattern,
                mode=mode,
                priority=priority,
                keyword_id=str(keyword.get("id") or ""),
                word=word,
                category=cid,
            ))

    compiled.sort(key=lambda item: -item.priority)
    return compiled


def get_rules() -> Dict[str, Any]:
    """
    带缓存地读取规则：文件内容（mtime + size）没变就直接返回进程内副本。

    读盘或编译失败时返回**默认规则**，绝不抛异常。
    """
    path = rules_path()
    mtime, size = _file_stat()
    with _CACHE_LOCK:
        cached = _CACHE
        if cached["path"] == path and cached["mtime"] == mtime \
                and cached["size"] == size and cached["rules"]:
            return cached["rules"]
        rules = load()
        items = _compile(rules)
        cached.update({"path": path, "mtime": mtime, "size": size,
                       "rules": rules, "items": items})
        return rules


def list_rules() -> Dict[str, Any]:
    """对外读取完整配置（含类别、关键词、总开关、拒答话术）"""
    return get_rules()


# ── 匹配引擎 ──────────────────────────────────────────────
def _category_label(rules: Dict[str, Any], cid: str) -> str:
    """取类别展示名；找不到时返回空串"""
    for category in rules.get("categories") or []:
        if isinstance(category, dict) and (category.get("id") or "") == cid:
            return str(category.get("label") or "")
    return ""


def _items() -> List[_Rule]:
    """取编译好的规则表（顺带保证缓存已就绪）"""
    get_rules()
    with _CACHE_LOCK:
        return list(_CACHE["items"])


def _hit(rule: _Rule, norm: str, compact_text: str, spaced: str) -> bool:
    """
    单条规则是否命中。

    :param norm: 归一化串（去空白）
    :param compact_text: 紧凑串（再去标点）
    :param spaced: 保分隔符串（英文词边界判定用）
    """
    if rule.mode == MODE_EXACT:
        if norm == rule.variant:
            return True
        if rule.has_cjk:
            # 中文没有词边界概念，按子串判定（同时天然覆盖「拆字加空格」的绕过）
            return rule.variant in norm
        # 英文：必须在保分隔符串上按词边界出现（cpython 不算命中 python）
        return rule.pattern is not None and rule.pattern.search(spaced) is not None
    # fuzzy：归一化串或紧凑串中子串命中即可
    if rule.variant and rule.variant in norm:
        return True
    return bool(rule.compact) and rule.compact in compact_text


def _audit(raw_text: Any, session_id: Any, result: Dict[str, Any]) -> None:
    """落一条审计日志（含截断后的提问片段，避免长文本或敏感内容全量落盘）"""
    logger.info(
        "内容护栏拦截 at=%s session_id=%s category=%s(%s) keyword=%s matched=%s "
        "mode=%s priority=%s snippet=%s",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "" if session_id is None else session_id,
        result.get("category") or "-",
        result.get("category_label") or "-",
        result.get("keyword") or "-",
        result.get("matched") or "-",
        result.get("mode") or "-",
        result.get("priority") or 0,
        audit_snippet(raw_text),
    )


def check(text: Any, session_id: Any = None) -> Dict[str, Any]:
    """
    检测一段文本是否命中护栏规则。

    :param text: 待检测文本（用户提问）
    :param session_id: 会话 ID，仅用于审计日志
    :return: {"blocked": bool, "reason": str, "category": str, "category_label": str,
              "keyword": str, "keyword_id": str, "matched": str, "mode": str,
              "priority": int}

    行为：
        · 总开关关闭 → 直接放行（blocked=False）；
        · 按 priority 从高到低判定，命中即返回，因此可以精确定位是哪条规则生效；
        · 引擎内部任何异常都按「未命中」处理并记日志，**绝不向上抛异常**。
    """
    try:
        rules = get_rules()
        if not bool(rules.get("enabled", True)):
            return dict(MISS_RESULT)

        raw = "" if text is None else str(text)
        norm, compact_text, spaced = normalize_forms(raw)
        if not norm:
            return dict(MISS_RESULT)

        for rule in _items():
            if not _hit(rule, norm, compact_text, spaced):
                continue
            result = dict(MISS_RESULT)
            result.update({
                "blocked": True,
                "reason": "命中受限类别「%s」的关键词「%s」" % (
                    _category_label(rules, rule.category) or rule.category or "未分类",
                    rule.word),
                "category": rule.category,
                "category_label": _category_label(rules, rule.category),
                "keyword": rule.word,
                "keyword_id": rule.keyword_id,
                "matched": rule.variant,
                "mode": rule.mode,
                "priority": rule.priority,
            })
            _audit(raw, session_id, result)
            return result
        return dict(MISS_RESULT)
    except Exception:
        logger.exception("内容护栏匹配失败，按未命中处理")
        return dict(MISS_RESULT)


def check_safely(text: Any, session_id: Any = None) -> Dict[str, Any]:
    """
    check() 的兜底包装：任何异常都被吞掉并按「未命中」返回。

    对话入口一律调用它 —— 护栏是旁路能力，出问题时必须让正常问答继续跑，
    绝不能把 500 抛给用户。
    """
    try:
        result = check(text, session_id=session_id)
        return result if isinstance(result, dict) else dict(MISS_RESULT)
    except Exception:
        logger.exception("调用内容护栏失败，按未命中继续")
        return dict(MISS_RESULT)


def refusal_message() -> str:
    """当前配置的拒答话术（读不到配置时返回内置默认值）"""
    try:
        message = _clean_str(get_rules().get("refusal_message"), "", MAX_MESSAGE_LEN)
    except Exception:
        logger.exception("读取拒答话术失败，使用默认话术")
        message = ""
    return message or DEFAULT_REFUSAL_MESSAGE


def blocked_stream_events(refusal: str, session_id: Any = None) -> List[Dict[str, Any]]:
    """
    构造「被拦截」时回给前端的 SSE 事件序列。

    刻意与 AgentRunner 正常结束的事件结构保持一致（chunk → run_end → done），
    前端现有渲染链路无需改动即可把拒答话术显示成一条普通助手回答，
    发送状态也能正常复位。
    """
    message = refusal or DEFAULT_REFUSAL_MESSAGE
    return [
        {"type": "chunk", "content": message},
        {"type": "run_end",
         "stats": {"steps": 0, "tool_calls": 0, "changes": 0,
                   "stopped_by": "blocked", "elapsed_ms": 0},
         "blocked": True},
        {"type": "done", "session_id": session_id},
    ]


# ── 类别 CRUD ─────────────────────────────────────────────
def _result(ok: bool, reason: str = "", **extra: Any) -> Dict[str, Any]:
    """统一的 CRUD 返回结构"""
    data: Dict[str, Any] = {"ok": ok, "reason": reason}
    data.update(extra)
    return data


def add_category(label: Any, enabled: bool = True,
                 category_id: Any = None) -> Dict[str, Any]:
    """新增类别；成功返回 {"ok": True, "category": {...}}"""
    text = _clean_str(label, "", MAX_LABEL_LEN)
    if not text:
        return _result(False, "类别名称不能为空")

    rules = load()
    cid = _clean_str(category_id, "", MAX_ID_LEN) or _new_id("c")
    for item in rules.get("categories") or []:
        if (item.get("id") or "") == cid:
            return _result(False, "类别 ID 已存在")
        if (item.get("label") or "") == text:
            return _result(False, "类别名称已存在")

    category = {"id": cid, "label": text, "built_in": False,
                "enabled": _coerce_bool(enabled, True)}
    rules.setdefault("categories", []).append(category)
    if not save_rules(rules):
        return _result(False, "保存失败，请查看服务日志")
    return _result(True, category=category)


def update_category(category_id: Any, label: Any = None,
                    enabled: Any = None) -> Dict[str, Any]:
    """修改类别（改名 / 启停）；两者都不传视为无需更新"""
    cid = _clean_str(category_id, "", MAX_ID_LEN)
    if not cid:
        return _result(False, "缺少类别 ID")

    rules = load()
    target = None
    for item in rules.get("categories") or []:
        if (item.get("id") or "") == cid:
            target = item
            break
    if target is None:
        return _result(False, "类别不存在")

    if label is not None:
        text = _clean_str(label, "", MAX_LABEL_LEN)
        if not text:
            return _result(False, "类别名称不能为空")
        for item in rules.get("categories") or []:
            if item is not target and (item.get("label") or "") == text:
                return _result(False, "类别名称已存在")
        target["label"] = text
    if enabled is not None:
        target["enabled"] = _coerce_bool(enabled, bool(target.get("enabled", True)))

    if not save_rules(rules):
        return _result(False, "保存失败，请查看服务日志")
    return _result(True, category=target)


def delete_category(category_id: Any) -> Dict[str, Any]:
    """删除类别；**内置类别禁止删除**（返回明确原因）"""
    cid = _clean_str(category_id, "", MAX_ID_LEN)
    if not cid:
        return _result(False, "缺少类别 ID")

    rules = load()
    categories = rules.get("categories") or []
    target = None
    for item in categories:
        if (item.get("id") or "") == cid:
            target = item
            break
    if target is None:
        return _result(False, "类别不存在")
    if target.get("built_in"):
        return _result(False, "内置类别不可删除，可改为停用")

    remaining = [c for c in categories if (c.get("id") or "") != cid]
    fallback = remaining[0]["id"] if remaining else ""
    for keyword in rules.get("keywords") or []:
        if isinstance(keyword, dict) and (keyword.get("category") or "") == cid:
            keyword["category"] = fallback
    rules["categories"] = remaining

    if not save_rules(rules):
        return _result(False, "保存失败，请查看服务日志")
    return _result(True, category=target, moved_keywords_to=fallback)


# ── 关键词 CRUD ───────────────────────────────────────────
def _categories_of(rules: Dict[str, Any]) -> set:
    """当前规则里所有类别 ID 的集合"""
    result = set()
    for category in rules.get("categories") or []:
        if isinstance(category, dict):
            result.add(category.get("id") or "")
    return result


def _keyword_dict(raw: Any, index: int,
                  category_ids: Optional[set]) -> Tuple[Optional[Dict[str, Any]], str]:
    """把请求体里的一个关键词对象洗成标准结构（复用导入期的同一套校验）"""
    return _sanitize_keyword(raw, index, category_ids)


def add_keyword(word: Any, mode: str = MODE_FUZZY, priority: int = DEFAULT_PRIORITY,
                enabled: bool = True, category: Any = "",
                alias: Any = None, keyword_id: Any = None) -> Dict[str, Any]:
    """新增单个关键词；成功返回 {"ok": True, "keyword": {...}}"""
    payload = {
        "word": word,
        "mode": mode,
        "priority": priority,
        "enabled": enabled,
        "category": category,
        "alias": alias,
        "id": keyword_id,
    }
    result = add_keywords([payload])
    if not result.get("ok"):
        return _result(False, result.get("reason") or "新增关键词失败",
                       skipped=result.get("skipped") or [])
    added = result.get("added") or []
    if not added:
        return _result(False, result.get("reason") or "新增关键词失败",
                       skipped=result.get("skipped") or [])
    return _result(True, keyword=added[0])


def add_keywords(items: Any) -> Dict[str, Any]:
    """
    批量新增关键词。

    :param items: 数组，每个元素是关键词对象
                  {"word": str, "mode": "exact"|"fuzzy", "priority": int,
                   "enabled": bool, "category": str, "alias": [str]}
    :return: {"ok": True, "added": [...], "skipped": [{"index": int, "reason": str}]}
             单条非法只跳过该条并把原因放进 skipped，**不影响其余条目**。
    """
    if not isinstance(items, (list, tuple)):
        return _result(False, "请求体必须是关键词对象或数组")
    if not items:
        return _result(False, "缺少关键词内容")
    if len(items) > MAX_BATCH:
        return _result(False, "单次最多新增 %d 条关键词" % MAX_BATCH)

    rules = load()
    category_ids = _categories_of(rules)
    existing_ids = {kw.get("id") for kw in rules.get("keywords") or []
                    if isinstance(kw, dict)}

    added: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for index, raw in enumerate(items):
        keyword, reason = _keyword_dict(raw, index, category_ids)
        if keyword is None:
            skipped.append({"index": index, "reason": reason})
            continue
        if keyword["id"] in existing_ids:
            keyword["id"] = _new_id("k")
        existing_ids.add(keyword["id"])
        added.append(keyword)

    if not added:
        return _result(False, skipped[0]["reason"] if skipped else "新增关键词失败",
                       skipped=skipped)

    rules.setdefault("keywords", []).extend(added)
    if not save_rules(rules):
        return _result(False, "保存失败，请查看服务日志")
    return _result(True, added=added, skipped=skipped)


def update_keyword(keyword_id: Any, word: Any = None, mode: Any = None,
                   priority: Any = None, enabled: Any = None,
                   category: Any = None, alias: Any = None) -> Dict[str, Any]:
    """
    局部更新一个关键词（只改显式传入的字段）。

    category 必须是已存在的类别 ID（传 "" 表示挂到「未分类」）。
    """
    kid = _clean_str(keyword_id, "", MAX_ID_LEN)
    if not kid:
        return _result(False, "缺少关键词 ID")

    rules = load()
    target = None
    for item in rules.get("keywords") or []:
        if (item.get("id") or "") == kid:
            target = item
            break
    if target is None:
        return _result(False, "关键词不存在")

    if word is not None:
        text = _clean_str(word, "", MAX_WORD_LEN)
        if not text or not normalize(text):
            return _result(False, "关键词内容不能为空")
        target["word"] = text
    if mode is not None:
        text = _clean_str(mode, "", 16).lower()
        if text not in MODES:
            return _result(False, "mode 只能是 exact 或 fuzzy")
        target["mode"] = text
    if priority is not None:
        try:
            number = int(priority)
        except (TypeError, ValueError):
            return _result(False, "priority 必须是整数")
        target["priority"] = _coerce_int(number, DEFAULT_PRIORITY)
    if enabled is not None:
        target["enabled"] = _coerce_bool(enabled, bool(target.get("enabled", True)))
    if alias is not None:
        if not isinstance(alias, (list, tuple)):
            return _result(False, "alias 必须是数组")
        target["alias"] = _clean_alias(alias)
    if category is not None:
        cid = _clean_str(category, "", MAX_ID_LEN)
        if cid and cid not in _categories_of(rules):
            return _result(False, "所属类别不存在")
        target["category"] = cid

    if not save_rules(rules):
        return _result(False, "保存失败，请查看服务日志")
    return _result(True, keyword=target)


def delete_keyword(keyword_id: Any) -> Dict[str, Any]:
    """删除一个关键词"""
    kid = _clean_str(keyword_id, "", MAX_ID_LEN)
    if not kid:
        return _result(False, "缺少关键词 ID")

    rules = load()
    keywords = rules.get("keywords") or []
    target = None
    for item in keywords:
        if (item.get("id") or "") == kid:
            target = item
            break
    if target is None:
        return _result(False, "关键词不存在")

    rules["keywords"] = [kw for kw in keywords if (kw.get("id") or "") != kid]
    if not save_rules(rules):
        return _result(False, "保存失败，请查看服务日志")
    return _result(True, keyword=target)


# ── 导入 / 导出 ───────────────────────────────────────────
def export_rules() -> Dict[str, Any]:
    """
    导出当前完整配置。

    :return: {"ok": True, "filename": str, "payload": {...}}
             payload 与 rules 文件同构，导出后再导入可完全还原。
    """
    try:
        rules = get_rules()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return _result(True,
                       filename="guard_rules-%s.json" % stamp,
                       payload=rules,
                       exported_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        logger.exception("导出内容护栏配置失败")
        return _result(False, "导出失败，请查看服务日志")


def import_rules(payload: Any, replace: bool = True) -> Dict[str, Any]:
    """
    导入配置：**先严格校验，再整体替换（默认）或合并**。

    策略说明（两种方式都由本函数支持，由参数 replace 决定）：
        · replace=True（默认，**整体替换**）：payload 校验通过后，其 categories /
          keywords / refusal_message / enabled 完整覆盖现有配置，结果可预期，
          「导出 → 再导入」能够精确还原；
        · replace=False（**合并**）：保留现有条目，按 id 去重（同 id 视为已存在而跳过），
          只把新的类别与关键词追加进来，适合做「合并别人的规则包」。

    校验项：
        · 顶层必须是 JSON 对象；
        · version 必须存在且等于 1（不支持的旧/新版本直接拒绝并说明）；
        · enabled 必须是布尔，refusal_message 必须是字符串；
        · categories / keywords 必须是数组，元素必须是对象；
        · 每条类别必须有非空 label；每条关键词必须有非空 word；
        · mode 枚举值只能是 exact / fuzzy；priority 必须是整数；
        · keywords[].category 必须存在于本次导入的（或合并后存在的）类别中。
    任何一条不满足即整体拒绝并返回**中文错误说明**，绝不半途写入。
    """
    if not isinstance(payload, dict):
        return _result(False, "导入内容必须是一个 JSON 对象")

    version_raw = payload.get("version")
    try:
        version = int(version_raw)
    except (TypeError, ValueError):
        return _result(False, "配置缺少合法的 version 字段（当前仅支持 version=%d）"
                       % RULES_VERSION)
    if version != RULES_VERSION:
        return _result(False, "不支持的配置版本 version=%s（当前仅支持 version=%d）"
                       % (version, RULES_VERSION))

    if "enabled" in payload and not isinstance(payload.get("enabled"), bool):
        return _result(False, "enabled 必须是布尔值")
    if "refusal_message" in payload and not isinstance(payload.get("refusal_message"), str):
        return _result(False, "refusal_message 必须是字符串")
    if "categories" in payload and not isinstance(payload.get("categories"), list):
        return _result(False, "categories 必须是数组")
    if "keywords" in payload and not isinstance(payload.get("keywords"), list):
        return _result(False, "keywords 必须是数组")

    incoming_categories: List[Dict[str, Any]] = []
    raw_categories = payload.get("categories") or []
    for index, item in enumerate(raw_categories):
        category, reason = _sanitize_category(item, index)
        if category is None:
            return _result(False, "导入失败：%s" % reason)
        incoming_categories.append(category)

    current = load() if not replace else None
    if replace:
        known_ids = {c["id"] for c in incoming_categories}
        base: Dict[str, Any] = {
            "version": RULES_VERSION,
            "enabled": _coerce_bool(payload.get("enabled"), True),
            "refusal_message": _clean_str(payload.get("refusal_message"),
                                          DEFAULT_REFUSAL_MESSAGE, MAX_MESSAGE_LEN),
            "categories": incoming_categories,
            "keywords": [],
        }
        incoming_keywords = payload.get("keywords") or []
    else:
        merged_categories = list(current.get("categories") or [])
        known_ids = {c.get("id") for c in merged_categories if isinstance(c, dict)}
        for category in incoming_categories:
            if category["id"] not in known_ids:
                merged_categories.append(category)
                known_ids.add(category["id"])
        base = {
            "version": RULES_VERSION,
            "enabled": _coerce_bool(payload.get("enabled"),
                                    bool(current.get("enabled", True))),
            "refusal_message": _clean_str(
                payload.get("refusal_message") or current.get("refusal_message"),
                DEFAULT_REFUSAL_MESSAGE, MAX_MESSAGE_LEN),
            "categories": merged_categories,
            "keywords": list(current.get("keywords") or []),
        }
        existing_ids = {kw.get("id") for kw in base["keywords"] if isinstance(kw, dict)}
        incoming_keywords = [kw for kw in (payload.get("keywords") or [])
                             if not (isinstance(kw, dict)
                                     and (kw.get("id") or "") in existing_ids)]

    incoming_keywords = list(incoming_keywords)
    index = 0
    for item in incoming_keywords:
        keyword, reason = _sanitize_keyword(item, index, known_ids)
        if keyword is None:
            return _result(False, "导入失败：%s" % reason)
        base["keywords"].append(keyword)
        index += 1

    _dedupe_ids(base["categories"], "c")
    _dedupe_ids(base["keywords"], "k")

    if not save_rules(base):
        return _result(False, "保存失败，请查看服务日志")

    logger.info("内容护栏配置导入完成 replace=%s categories=%d keywords=%d",
                bool(replace), len(base["categories"]), len(base["keywords"]))
    return _result(True, payload=base, replaced=bool(replace),
                   categories=len(base["categories"]),
                   keywords=len(base["keywords"]))
