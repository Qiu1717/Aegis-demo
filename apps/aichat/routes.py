"""
aichat 模块 - 路由层
提供会话管理、消息查询、聊天对话（SSE流式）接口

异常处理约定：
    异常详情只写服务端日志，接口返回通用文案，
    满足 PRD 8.3.3「后端统一接口异常捕获，避免原始报错信息暴露」。
"""
import json

from flask import request, Response

from apps import api_ok, api_fail, logger
from apps.aichat import aichat_bp
from apps.guard import services as guard_services
from . import services
from . import memory as mem
from . import state


def _parse_metadata(raw):
    """
    把 `metadata` 列（JSON 字符串）解析成对象。

    已经是 dict / 为空 / 解析失败时都返回 None，绝不因脏数据让接口报错。
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ── 会话管理接口 ──────────────────────────────────────────

@aichat_bp.route("/sessions", methods=["GET"])
def get_sessions():
    """获取全部会话列表（批量合并归档标志 archived，默认 False）"""
    try:
        sessions = services.list_sessions()
        archived = state.archived_map()
        for item in sessions or []:
            if not isinstance(item, dict):
                continue
            sid = item.get("session_id")
            try:
                key = str(int(sid)) if sid is not None else None
            except (TypeError, ValueError):
                key = None
            item["archived"] = bool(archived.get(key)) if key else False
        return api_ok(sessions)
    except Exception:
        logger.exception("获取会话列表失败")
        return api_fail("获取会话列表失败，请稍后重试", code=500)


@aichat_bp.route("/sessions", methods=["POST"])
def create_session():
    """创建新会话"""
    try:
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "新对话").strip() or "新对话"
        sid = services.create_session(title)
        session = services.get_session(sid)
        return api_ok(session, msg="创建成功")
    except Exception:
        logger.exception("创建会话失败")
        return api_fail("创建会话失败，请稍后重试", code=500)


@aichat_bp.route("/sessions/<int:session_id>", methods=["PUT"])
def update_session(session_id):
    """更新会话（重命名 / 置顶）"""
    try:
        data = request.get_json(force=True)
        action = data.get("action")

        if action == "rename":
            title = (data.get("title") or "").strip()
            if not title:
                return api_fail("标题不能为空")
            services.rename_session(session_id, title)
            return api_ok(services.get_session(session_id), msg="重命名成功")

        elif action == "pin":
            is_pinned = data.get("is_pinned", False)
            services.toggle_pin(session_id, is_pinned)
            return api_ok(services.get_session(session_id),
                          msg="置顶成功" if is_pinned else "取消置顶成功")

        else:
            return api_fail("不支持的操作类型")
    except Exception:
        logger.exception("更新会话失败 session_id=%s", session_id)
        return api_fail("更新会话失败，请稍后重试", code=500)


@aichat_bp.route("/sessions/<int:session_id>", methods=["DELETE"])
def delete_session(session_id):
    """删除会话（级联删除消息，并清理本地状态条目）"""
    try:
        session = services.get_session(session_id)
        if not session:
            return api_fail("会话不存在", code=404)
        services.delete_session(session_id)
        state.forget(session_id)
        return api_ok(msg="删除成功")
    except Exception:
        logger.exception("删除会话失败 session_id=%s", session_id)
        return api_fail("删除会话失败，请稍后重试", code=500)


@aichat_bp.route("/sessions/<int:session_id>/archive", methods=["PUT"])
def archive_session(session_id):
    """归档 / 取消归档会话（状态持久化到本地文件）"""
    try:
        data = request.get_json(silent=True) or {}
        if "archived" not in data:
            return api_fail("缺少参数 archived", code=400)
        archived = data.get("archived")
        if not isinstance(archived, bool):
            return api_fail("参数 archived 必须为布尔值（true / false）", code=400)

        if not services.get_session(session_id):
            return api_fail("会话不存在", code=404)

        if not state.set_archived(session_id, archived):
            return api_fail("归档状态保存失败，请稍后重试", code=500)

        return api_ok({"session_id": session_id, "archived": bool(archived)},
                      msg="已归档" if archived else "已取消归档")
    except Exception:
        logger.exception("归档会话失败 session_id=%s", session_id)
        return api_fail("归档操作失败，请稍后重试", code=500)


# ── 消息查询接口 ──────────────────────────────────────────

@aichat_bp.route("/sessions/<int:session_id>/messages", methods=["GET"])
def get_messages(session_id):
    """
    获取指定会话的全部历史消息

    返回体在原有字段基础上**额外**携带 `metadata`（解析后的对象，无则为 None），
    Agent 轨迹（工具调用 / 文件变更）用它承载结构化载荷；
    旧前端只认 message_content / message_role 等字段，不受影响。
    """
    try:
        messages = services.list_messages(session_id)
        for item in messages or []:
            if not isinstance(item, dict):
                continue
            item["metadata"] = _parse_metadata(item.get("metadata"))
        return api_ok(messages)
    except Exception:
        logger.exception("获取消息失败 session_id=%s", session_id)
        return api_fail("获取消息失败，请稍后重试", code=500)


# ── 内容护栏接入（apps/guard）─────────────────────────────

def _sse(obj) -> str:
    """格式化为 SSE data 行（与 services 内部写法保持一致）"""
    return "data: %s\n\n" % json.dumps(obj, ensure_ascii=False)


def _guard_verdict(message, session_id=None) -> dict:
    """
    调用内容护栏做检测。

    护栏是旁路能力：引擎返回空结果或调用过程抛异常，一律按「未命中」处理，
    绝不因为护栏故障把正常问答拖成 500。
    """
    try:
        verdict = guard_services.check_safely(message, session_id=session_id)
    except Exception:
        logger.exception("内容护栏调用失败，按未命中继续")
        return {}
    return verdict if isinstance(verdict, dict) else {}


def _persist_blocked(session_id, message, refusal) -> None:
    """把被拦截的提问与拒答话术落库（保持上下文连贯，失败只记日志）"""
    try:
        services.update_session_title_if_default(session_id, message)
        services.save_message(session_id, "user", message)
        services.save_message(session_id, "assistant", refusal)
    except Exception:
        logger.exception("保存护栏拦截消息失败 session_id=%s", session_id)


def _blocked_stream_response(session_id, message):
    """内容护栏命中时返回 SSE 拒答流（事件结构与正常回答完全同构）"""
    refusal = guard_services.refusal_message()
    _persist_blocked(session_id, message, refusal)

    def generate_blocked():
        for event in guard_services.blocked_stream_events(refusal, session_id):
            yield _sse(event)

    return Response(generate_blocked(), mimetype="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection": "keep-alive",
                    })


# ── 聊天对话接口 ──────────────────────────────────────────

@aichat_bp.route("/chat", methods=["POST"])
def chat():
    """流式聊天接口（SSE）"""
    try:
        data = request.get_json(force=True)
        session_id = data.get("session_id")
        message = (data.get("message") or "").strip()
        use_web = bool(data.get("use_web", True))

        if not session_id:
            return api_fail("缺少 session_id")
        if not message:
            return api_fail("消息内容不能为空")

        # 确保会话存在
        session = services.get_session(session_id)
        if not session:
            return api_fail("会话不存在", code=404)

        # ── 内容护栏（apps/guard）：命中时把拒答话术当作一条普通助手回答返回 ──
        verdict = _guard_verdict(message, session_id=session_id)
        if verdict.get("blocked"):
            logger.info("聊天被内容护栏拦截 session_id=%s category=%s keyword=%s",
                        session_id, verdict.get("category") or "-",
                        verdict.get("keyword") or "-")
            return _blocked_stream_response(session_id, message)

        def generate():
            for event in services.chat_stream(session_id, message, use_web=use_web):
                yield event

        return Response(generate(), mimetype="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache",
                            "X-Accel-Buffering": "no",
                            "Connection": "keep-alive",
                        })
    except Exception:
        logger.exception("聊天请求失败")
        return api_fail("聊天请求失败，请稍后重试", code=500)


@aichat_bp.route("/chat/normal", methods=["POST"])
def chat_normal():
    """普通聊天接口（非流式，备用）"""
    try:
        data = request.get_json(force=True)
        session_id = data.get("session_id")
        message = (data.get("message") or "").strip()
        use_web = bool(data.get("use_web", True))

        if not session_id or not message:
            return api_fail("参数不完整")

        session = services.get_session(session_id)
        if not session:
            return api_fail("会话不存在", code=404)

        # ── 内容护栏（apps/guard）：命中时直接返回拒答话术，不调用大模型 ──
        verdict = _guard_verdict(message, session_id=session_id)
        if verdict.get("blocked"):
            refusal = guard_services.refusal_message()
            _persist_blocked(session_id, message, refusal)
            return api_ok({"compliant": False, "ai_reply": refusal, "blocked": True,
                           "reason": verdict.get("reason") or "",
                           "session_id": session_id})

        result = services.chat_normal(session_id, message, use_web=use_web)
        return api_ok(result)
    except Exception:
        logger.exception("聊天请求失败")
        return api_fail("聊天请求失败，请稍后重试", code=500)


# ── 长期记忆接口（跨会话 Context Memory）──────────────────

@aichat_bp.route("/memory", methods=["GET"])
def get_memory():
    """获取用户全部长期记忆"""
    try:
        return api_ok(mem.get_user_memory(1))
    except Exception:
        logger.exception("获取长期记忆失败")
        return api_fail("获取长期记忆失败，请稍后重试", code=500)


@aichat_bp.route("/memory", methods=["POST"])
def post_memory():
    """新增/更新一条长期记忆（upsert）"""
    try:
        data = request.get_json(force=True) or {}
        key = (data.get("key") or "").strip()
        value = (data.get("value") or "").strip()
        if not key or not value:
            return api_fail("key 与 value 均不能为空")
        category = (data.get("category") or "general").strip() or "general"
        importance = int(data.get("importance", 2))
        item = mem.set_user_memory(1, key, value, category, importance)
        return api_ok(item, msg="已保存")
    except Exception:
        logger.exception("保存长期记忆失败")
        return api_fail("保存失败，请稍后重试", code=500)


@aichat_bp.route("/memory/<path:key>", methods=["DELETE"])
def delete_memory(key):
    """删除单条记忆"""
    try:
        mem.delete_user_memory(1, key)
        return api_ok(msg="已删除")
    except Exception:
        logger.exception("删除长期记忆失败")
        return api_fail("删除失败，请稍后重试", code=500)


@aichat_bp.route("/memory", methods=["DELETE"])
def clear_memory():
    """清空用户全部记忆"""
    try:
        mem.clear_user_memory(1)
        return api_ok(msg="已清空")
    except Exception:
        logger.exception("清空长期记忆失败")
        return api_fail("清空失败，请稍后重试", code=500)


@aichat_bp.route("/memory/prune", methods=["POST"])
def prune_memory():
    """按策略清理低重要度 + 过期记忆"""
    try:
        data = request.get_json(silent=True) or {}
        removed = mem.prune_user_memory(
            1, data.get("ttl_days"), data.get("low_importance"))
        return api_ok({"removed": removed}, msg="清理完成")
    except Exception:
        logger.exception("清理长期记忆失败")
        return api_fail("清理失败，请稍后重试", code=500)
