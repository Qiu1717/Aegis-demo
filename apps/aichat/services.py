"""
aichat 模块 - 服务层
负责会话管理、消息存储、合规校验、大模型流式调用
"""
import json
import re as _re
from datetime import datetime

from apps import query, execute, CONFIG, logger
from apps.llm import get_client
from apps.aicontrol.services import check_compliance
from apps.websearch import services as search_svc
from apps.websearch import weather as wx_svc
from . import memory as mem


# ── 联网检索增强 ─────────────────────────────────────────
# 触发条件：前端「联网搜索」开关开启（use_web=True）。
# 开关开启时每条消息都先联网检索再回答（纯闲聊问候除外，避免浪费与延迟）；
# 开关关闭时完全不发起任何网络请求，仅基于本地知识作答。
_TRIVIAL_RE = _re.compile(
    r"^\s*(你好|您好|嗨|哈喽|哈罗|hello|hi|hey|"
    r"谢谢|多谢|感谢|再见|拜拜|晚安|早安|午安|"
    r"你是谁|你叫什么名字?|你是什么|介绍一下你自己|你是机器人吗|"
    r"嗯+|哦+|好的?|收到|测试)[!！？?。~～\s]*$", _re.I)


def _is_trivial_chat(query):
    """纯问候/闲聊无需联网检索。"""
    return bool(_TRIVIAL_RE.match((query or "").strip()))


# ── 结果相关性过滤 ────────────────────────────────────────
# 关键词提取与停用词统一由 websearch 层提供（单一实现，避免两处不一致）
def _keywords(query):
    return search_svc.extract_keywords(query)


def _filter_results(query, results, min_score=None):
    """按与查询的关键词重合度过滤搜索结果，剔除无关/低相关条目。

    评分 = 命中关键词个数（中文 2-gram + 英文词元，已剔除停用词）。
    无任何内容词重合的结果直接丢弃；全部被丢弃时返回空列表（此时宁可不注入
    参考、让模型基于自身知识回答，也不注入噪声）。

    min_score：最低命中数。关键词越多说明实体越具体，阈值相应提高，
    避免像「最近」这类残留高频词单独把噪声结果放进上下文。
    """
    kws = _keywords(query)
    if not kws:
        return results
    if min_score is None:
        min_score = 2 if len(kws) >= 3 else 1
    scored = []
    for r in results:
        text = ((r.get("title") or "") + " " + (r.get("snippet") or "")).lower()
        score = sum(1 for k in kws if k in text)
        if score >= min_score:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored]


# 附加在实体后的「意图词」，本身不含实体信息，有时会劣化搜索引擎的召回
_INTENT_TAIL_RE = _re.compile(
    r"(时间|日期|安排|行程|地点|场地|地址|票价|价格|门票|购票|官方|官网|电话|"
    r"什么时候|多少|怎么样|介绍|详情|信息)")


def _core_variant(q):
    """把「实体 + 意图词」压缩成纯实体短语，用于结果全部无关时的二次检索。

    例：'薛之谦演唱会 时间' -> '薛之谦演唱会'（去掉意图词后召回质量明显更高）。
    无可用变体时返回空串。
    """
    parts = [p for p in (q or "").split() if p]
    if len(parts) > 1:
        core = max(parts, key=len)
        return core if len(core) >= 2 else ""
    if parts:
        stripped = _INTENT_TAIL_RE.sub("", parts[0]).strip()
        return stripped if stripped and stripped != parts[0] and len(stripped) >= 2 else ""
    return ""


def _build_web_grounding(query, max_results, timeout):
    """尝试联网检索并把结果拼装为可注入 system 的参考文本。

    优先尝试天气快捷通道（Open-Meteo 实时数据，弥补搜索摘要无实际数值的短板），
    再叠加通用搜索结果；摘要不含具体数值时补抓页面正文。
    天气通道内部自兜底（失败返回 None）；搜索失败不影响已取得的天气数据，
    两者皆无时返回 (None, 0, None)。
    """
    blocks = []
    count = 0
    provider = None
    gcfg = (CONFIG.get("search", {}) or {}).get("web_grounding", {}) or {}
    enrich_top = int(gcfg.get("excerpt_top", 2))

    # ── 天气快捷通道（实时气象数据）──
    wx_text = wx_svc.fetch_weather_text(query, timeout=timeout)
    if wx_text:
        blocks.append("[实时气象数据]\n" + wx_text)
        count += 1

    # ── 通用搜索（失败不放弃已取得的天气数据）──
    try:
        data = search_svc.web_search(query, max_results=max_results, provider="auto")
    except Exception as e:
        logger.warning("通用搜索失败（保留已有联网数据）: %s", e)
        data = None
    if data:
        results = data.get("results") or []
        provider = data.get("provider")
        # 用「改写后的检索词」做相关性判断：它只保留实体与核心名词，
        # 比原始口语问句更精确，可避免「最近」之类的词把无关结果放进来。
        filter_base = data.get("effective_query") or query
        results = _filter_results(filter_base, results)

        # 结果全部被判为无关时，用「纯实体短语」再检索一次。
        # 搜索引擎对夹带意图词的长查询常返回泛化页面（如「薛之谦演唱会 时间」
        # 只回薛姓字典条目），去掉意图词后的实体词召回质量更高。
        if not results:
            variant = _core_variant(filter_base)
            if variant:
                try:
                    data2 = search_svc.web_search(variant, max_results=max_results,
                                                  provider="auto")
                    base2 = data2.get("effective_query") or variant
                    results = _filter_results(base2, data2.get("results") or [])
                    if results:
                        provider = data2.get("provider")
                        filter_base = base2
                        logger.info("二次检索命中（实体短语 %r）：%d 条",
                                    variant, len(results))
                except Exception as e:
                    logger.warning("二次检索失败（忽略）: %s", e)

        if results:
            kws = search_svc.extract_keywords(filter_base)
            lines = []
            for i, r in enumerate(results):
                snippet = (r.get("snippet") or "").strip()
                # 摘要过短或不含具体数值时，补抓页面正文（仅前 N 条，控制延迟）
                if i < enrich_top and not _re.search(r"\d", snippet) and len(snippet) < 120:
                    excerpt = search_svc.fetch_page_excerpt(
                        r.get("url"), kws, timeout=min(timeout, 5))
                    if excerpt:
                        snippet = (snippet + " …… " + excerpt) if snippet else excerpt
                lines.append("- {title}\n  {snippet}\n  来源：{url}".format(
                    title=r.get("title", ""), snippet=snippet, url=r.get("url", "")))
            blocks.append("[搜索结果]\n" + "\n".join(lines))
            count += len(results)

    if not blocks:
        return None, 0, provider

    text = ("以下是联网检索到的实时信息（仅供参考，请优先依据这些结果回答用户，"
            "并在回答中注明信息来源；若检索内容与问题无关可忽略）：\n"
            + "\n\n".join(blocks))
    return text, count, provider

# ── 会话管理 ──────────────────────────────────────────────

def list_sessions():
    """查询全部会话，置顶优先 + 最后消息时间倒序"""
    return query(
        "SELECT session_id, user_id, session_title, created_time, "
        "is_pinned, last_message_time, updated_time "
        "FROM ai_chat_session ORDER BY is_pinned DESC, "
        "last_message_time DESC, created_time DESC"
    )


def get_session(session_id):
    """查询单个会话"""
    rows = query(
        "SELECT session_id, user_id, session_title, created_time, "
        "is_pinned, last_message_time, updated_time "
        "FROM ai_chat_session WHERE session_id = %s",
        (session_id,)
    )
    return rows[0] if rows else None


def create_session(title="新对话"):
    """创建新会话，返回 session_id"""
    sid = execute(
        "INSERT INTO ai_chat_session (user_id, session_title, is_pinned) "
        "VALUES (1, %s, 0)",
        (title,)
    )
    return sid


def rename_session(session_id, title):
    """重命名会话"""
    execute(
        "UPDATE ai_chat_session SET session_title = %s WHERE session_id = %s",
        (title, session_id)
    )
    return True


def toggle_pin(session_id, is_pinned):
    """置顶 / 取消置顶"""
    execute(
        "UPDATE ai_chat_session SET is_pinned = %s WHERE session_id = %s",
        (1 if is_pinned else 0, session_id)
    )
    return True


def delete_session(session_id):
    """删除会话（消息表通过外键级联删除）"""
    execute("DELETE FROM ai_chat_session WHERE session_id = %s", (session_id,))
    return True


# ── 消息管理 ──────────────────────────────────────────────

def list_messages(session_id):
    """
    查询会话全部历史消息，按序号排序。

    优先带上 `metadata` 列（Agent 轨迹用）；若目标库尚未做该列迁移，
    自动降级为旧结构查询，保证既有功能不受影响。
    """
    base_cols = ("message_id, session_id, message_role, message_type, "
                 "message_content, sequence_no, created_time")
    try:
        return query(
            "SELECT " + base_cols + ", metadata "
            "FROM ai_chat_message WHERE session_id = %s "
            "ORDER BY sequence_no ASC, message_id ASC",
            (session_id,)
        )
    except Exception:
        logger.info("metadata 列不可用，按旧结构返回历史消息 session_id=%s", session_id)
        return query(
            "SELECT " + base_cols + " "
            "FROM ai_chat_message WHERE session_id = %s "
            "ORDER BY sequence_no ASC, message_id ASC",
            (session_id,)
        )


def _next_seq(session_id):
    """获取会话内下一个消息序号"""
    rows = query(
        "SELECT COALESCE(MAX(sequence_no), 0) AS max_seq "
        "FROM ai_chat_message WHERE session_id = %s",
        (session_id,)
    )
    return (rows[0]["max_seq"] + 1) if rows else 1


def save_message(session_id, role, content, message_type="text", metadata=None):
    """
    保存一条消息，返回 message_id。

    新增的两个可选参数服务于 Agent 轨迹（设计 §4.4.4）：
        message_type  消息类型，默认 'text'（tool_call / tool_result / file_change ...）
        metadata      结构化载荷的 **JSON 字符串**（由调用方序列化后传入）
    两者都不传时，行为与改造前**完全一致**。
    """
    seq = _next_seq(session_id)
    msg_type = message_type or "text"

    if metadata:
        try:
            mid = execute(
                "INSERT INTO ai_chat_message "
                "(session_id, user_id, message_role, message_type, "
                " message_content, sequence_no, metadata) "
                "VALUES (%s, 1, %s, %s, %s, %s, %s)",
                (session_id, role, msg_type, content, seq, metadata)
            )
        except Exception:
            # 目标库没有 metadata 列时降级，绝不因为增强字段写入失败而丢消息
            logger.exception("写入 metadata 失败，降级为不带 metadata 的写入")
            mid = execute(
                "INSERT INTO ai_chat_message "
                "(session_id, user_id, message_role, message_type, "
                " message_content, sequence_no) "
                "VALUES (%s, 1, %s, %s, %s, %s)",
                (session_id, role, msg_type, content, seq)
            )
    else:
        mid = execute(
            "INSERT INTO ai_chat_message "
            "(session_id, user_id, message_role, message_type, "
            " message_content, sequence_no) "
            "VALUES (%s, 1, %s, %s, %s, %s)",
            (session_id, role, msg_type, content, seq)
        )
    # 更新会话最后消息时间（用 Python 时间戳兼容 MySQL / SQLite）
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute(
        "UPDATE ai_chat_session SET last_message_time = %s "
        "WHERE session_id = %s",
        (now_str, session_id)
    )
    return mid


def update_session_title_if_default(session_id, message):
    """如果会话标题为默认值，用首条消息自动生成标题"""
    session = get_session(session_id)
    if session and session["session_title"] in ("新对话", "新会话"):
        title = message[:20].replace("\n", " ").strip()
        if not title:
            title = "新对话"
        rename_session(session_id, title)


# ── 大模型调用 ────────────────────────────────────────────
# 服务商选型、故障切换、本地模式应答全部下沉到 apps/llm，
# 本层只负责编排业务流程。

def _build_llm_messages(history, system_extra=None):
    """
    组装大模型 messages 列表
    system 提示词与上下文长度统一由 config.json 控制；
    system_extra 用于注入跨会话长期记忆。
    """
    return get_client().build_messages(history, system_extra=system_extra)


def chat_stream(session_id, user_message, use_web=True):
    """
    聊天主流程（流式），生成器产出 SSE 事件
    1. 合规校验  2. 保存用户消息  3. 组装上下文（含联网检索增强）
    4. 调用大模型（自动挑选可用服务商）  5. 保存AI回复

    use_web: 是否启用「联网检索增强」（由前端「联网」开关控制，默认开）。
    """
    block_msg = CONFIG.get("compliance", {}).get(
        "block_message", "你的问题存在不合规内容，我无法给予回复，抱歉！")

    # ── 1. 合规校验 ──
    compliant, _ = check_compliance(user_message)
    if not compliant:
        yield _sse({"type": "blocked", "content": block_msg})
        yield _sse({"type": "done", "session_id": session_id})
        return

    # ── 2. 保存用户消息 ──
    update_session_title_if_default(session_id, user_message)
    save_message(session_id, "user", user_message)

    # ── 3. 组装历史上下文（含跨会话长期记忆）──
    history = list_messages(session_id)
    system_extra = mem.get_user_memory_text(1)

    # ── 3.5 联网检索增强：开关开启即先检索再回答（纯问候闲聊除外）──
    web_ok = False
    web_count = 0
    web_provider = None
    scfg = (CONFIG.get("search", {}) or {}).get("web_grounding", {}) or {}
    web_enabled = bool(scfg.get("enabled", True)) and use_web
    grounding_attempted = web_enabled and not _is_trivial_chat(user_message)
    if grounding_attempted:
        try:
            web_text, web_count, web_provider = _build_web_grounding(
                user_message, int(scfg.get("max_results", 5)), int(scfg.get("timeout", 8)))
            if web_text:
                system_extra = (system_extra or "") + "\n\n[联网检索参考]\n" + web_text
                web_ok = True
        except Exception:
            # 联网失败不影响对话：降级为基于已有知识回答
            logger.exception("联网检索增强失败（已忽略，降级为离线回答）session_id=%s", session_id)

    llm_messages = _build_llm_messages(history, system_extra=system_extra)

    # 向前端播报联网状态：仅在实际发起过检索时下发（不写入回复内容、不入库）
    if grounding_attempted:
        yield _sse({"type": "web", "ok": web_ok, "count": web_count,
                    "provider": web_provider})

    # ── 4. 调用大模型流式输出 ──
    ai_reply_parts = []
    try:
        for event in get_client().stream(llm_messages, user_text=user_message,
                                          memory_text=system_extra):
            etype = event.get("type")

            if etype == "delta":
                ai_reply_parts.append(event["content"])
                yield _sse({"type": "chunk", "content": event["content"]})

            elif etype == "notice":
                ai_reply_parts.append(event["content"])
                yield _sse({"type": "notice", "content": event["content"]})

            elif etype == "provider":
                # 告知前端当前实际生效的模型，用于刷新角标
                yield _sse({"type": "provider",
                            "label": event.get("label"),
                            "model": event.get("model")})

            elif etype == "error":
                yield _sse({"type": "error", "content": event["content"]})
                break

    except Exception:
        # 异常详情只写日志，前端只给通用提示（PRD 8.3.3）
        logger.exception("大模型调用异常 session_id=%s", session_id)
        yield _sse({"type": "error", "content": "大模型调用异常，请稍后重试。"})
        partial = "".join(ai_reply_parts)
        if partial:
            save_message(session_id, "assistant", partial)
        yield _sse({"type": "done", "session_id": session_id})
        return

    # ── 5. 保存AI回复 ──
    ai_reply = "".join(ai_reply_parts)
    if ai_reply:
        save_message(session_id, "assistant", ai_reply)

    # ── 6. 启发式抽取长期记忆（best-effort，失败不影响主流程）──
    try:
        mem.extract_memory_from_text(user_message, 1)
    except Exception:
        logger.exception("长期记忆抽取失败（已忽略）")

    yield _sse({"type": "done", "session_id": session_id,
                "ai_reply": ai_reply[:200]})


def chat_normal(session_id, user_message, use_web=True):
    """
    聊天主流程（非流式），返回完整结果
    """
    block_msg = CONFIG.get("compliance", {}).get(
        "block_message", "你的问题存在不合规内容，我无法给予回复，抱歉！")

    compliant, hit_rule = check_compliance(user_message)
    if not compliant:
        return {"compliant": False, "ai_reply": block_msg, "hit_rule": hit_rule}

    update_session_title_if_default(session_id, user_message)
    save_message(session_id, "user", user_message)

    history = list_messages(session_id)
    system_extra = mem.get_user_memory_text(1)

    # 联网检索增强：开关开启即检索（纯问候闲聊除外）；关闭则零网络请求
    scfg = (CONFIG.get("search", {}) or {}).get("web_grounding", {}) or {}
    web_enabled = bool(scfg.get("enabled", True)) and use_web
    if web_enabled and not _is_trivial_chat(user_message):
        try:
            web_text, _, _ = _build_web_grounding(
                user_message, int(scfg.get("max_results", 5)), int(scfg.get("timeout", 8)))
            if web_text:
                system_extra = (system_extra or "") + "\n\n[联网检索参考]\n" + web_text
        except Exception:
            logger.exception("联网检索增强失败（已忽略）session_id=%s", session_id)

    llm_messages = _build_llm_messages(history, system_extra=system_extra)
    ai_reply, provider_label = get_client().complete(
        llm_messages, user_text=user_message, memory_text=system_extra)

    if ai_reply:
        save_message(session_id, "assistant", ai_reply)

    try:
        mem.extract_memory_from_text(user_message, 1)
    except Exception:
        logger.exception("长期记忆抽取失败（已忽略）")

    return {"compliant": True, "ai_reply": ai_reply,
            "provider": provider_label}


def _sse(obj):
    """格式化为 SSE data 行"""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
