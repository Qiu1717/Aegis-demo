# -*- coding: utf-8 -*-
"""
apps/agent/routes.py — /api/agent/* 路由层（设计 §5.4）

    POST /api/agent/run       启动 Agent 任务，返回 SSE（text/event-stream）
    POST /api/agent/approve   提交审批决策，唤醒挂起的 Runner
    POST /api/agent/cancel    取消任务（置 cancel_event + 杀子进程）
    GET  /api/agent/changes   查询变更清单
    POST /api/agent/undo      回滚变更（整任务或单条）
    GET  /api/agent/tools     工具元信息（调试用）

约定（沿用现有风格）：
    · 路由体一律 try/except 包住，详情用 logger.exception("中文描述") 记日志；
    · 对外只回 api_ok / api_fail，**绝不回传 str(e)**；
    · 错误码复用 apps/security/errors.py::classify，不另造一套。

⚠️ 审批挂起依赖 Flask 的 `threaded=True`：SSE 线程在 `Event.wait()` 阻塞时，
   /api/agent/approve 必须能被**另一个**工作线程处理，否则死锁（设计 §4.3.3）。
"""
import json
import re
import threading
from typing import Dict

from flask import Response, request

from apps import CONFIG, api_fail, api_ok, logger
from apps.aichat import services as aichat_services
from apps.settings import services as settings_services
from apps.skills import services as skills_services
from apps.guard import services as guard_services

from . import agent_bp
from . import attachments
from .approval import (
    DECISION_ALLOW_ONCE,
    DECISION_ALLOW_SESSION,
    DECISION_DENY,
    approval_manager,
)
from .fileio import read_text_file
from .runner import AgentRunner, cancel_task, get_runner
from .sandbox import SandboxError, open_workspace
from .schemas import TOOL_LABELS, TOOL_RISK, ToolError, get_tool_schema, tool_names

#: 已结束任务的 ChangeSet 快照（按 task_id 保留，便于 /changes 与 /undo 事后查询）
_CHANGE_SETS: Dict[str, object] = {}
_CHANGE_SETS_LOCK = threading.RLock()

#: 最多保留多少个历史任务的变更清单（防止无限增长）
MAX_SNAPSHOTS = 50


def _sse(obj) -> str:
    """格式化为 SSE data 行（与 aichat 现有写法一致）"""
    return "data: %s\n\n" % json.dumps(obj, ensure_ascii=False)


def _remember_changeset(task_id: str, changeset) -> None:
    """任务结束后留存变更清单（超出上限时丢掉最旧的）"""
    with _CHANGE_SETS_LOCK:
        _CHANGE_SETS[task_id] = changeset
        while len(_CHANGE_SETS) > MAX_SNAPSHOTS:
            try:
                _CHANGE_SETS.pop(next(iter(_CHANGE_SETS)))
            except StopIteration:
                break


def _find_changeset(task_id: str):
    """优先取运行中的任务，其次取已结束任务的快照"""
    runner = get_runner(task_id)
    if runner is not None:
        return runner.changeset
    with _CHANGE_SETS_LOCK:
        return _CHANGE_SETS.get(task_id)


def _agent_config() -> dict:
    """Agent 运行参数（来自 config.json / config.user.json 的 agent 段）"""
    section = CONFIG.get("agent")
    return dict(section) if isinstance(section, dict) else {}


def _blocked_response(session_id, message):
    """
    内容护栏命中时的统一应答（apps/guard）：先落库，再回一条 SSE 事件流。

    事件结构与 AgentRunner 正常结束完全一致（chunk → run_end → done），
    前端无需改动即可把拒答话术渲染成一条普通助手回答，发送状态也能正常复位。
    """
    refusal = guard_services.refusal_message()
    sid = _ensure_session(session_id)
    if sid:
        try:
            aichat_services.save_message(sid, "user", message)
            aichat_services.save_message(sid, "assistant", refusal)
        except Exception:
            logger.exception("保存护栏拦截消息失败 session_id=%s", sid)
    else:
        sid = session_id

    events = guard_services.blocked_stream_events(refusal, sid)

    def generate_blocked():
        for event in events:
            yield _sse(event)

    return Response(generate_blocked(), mimetype="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection": "keep-alive",
                    })


def _blocked_verdict(message, session_id=None) -> dict:
    """
    调用内容护栏做检测。

    护栏是旁路能力：引擎自身返回 {}, 或调用过程抛异常，统统按「未命中」处理，
    绝不能因为护栏故障把正常问答拖成 500。
    """
    try:
        verdict = guard_services.check_safely(message, session_id=session_id)
    except Exception:
        logger.exception("内容护栏调用失败，按未命中继续")
        return {}
    return verdict if isinstance(verdict, dict) else {}


def _ensure_session(session_id):
    """
    确保会话存在：给了就校验，没给就新建。

    :return: session_id；失败返回 None
    """
    if session_id:
        try:
            session = aichat_services.get_session(session_id)
        except Exception:
            logger.exception("查询会话失败 session_id=%s", session_id)
            return None
        return session.get("session_id") if session else None
    try:
        return aichat_services.create_session("新任务")
    except Exception:
        logger.exception("创建会话失败")
        return None


# ── 启动 Agent 任务（SSE）──────────────────────────────────
@agent_bp.route("/attachments", methods=["POST"])
def upload_attachment():
    try:
        if request.content_length and request.content_length > attachments.MAX_BYTES + 65536:
            return api_fail("文件不能超过 2 MiB", code=400)
        return api_ok(attachments.save_upload(request.files.get("file")), msg="文件已添加")
    except ValueError as e:
        return api_fail(str(e), code=400)
    except Exception:
        logger.exception("添加文件失败")
        return api_fail("添加文件失败，请查看日志", code=500)


@agent_bp.route("/attachments/<attachment_id>", methods=["DELETE"])
def remove_attachment(attachment_id):
    try:
        attachments.remove(attachment_id)
        return api_ok({}, msg="附件已移除")
    except ValueError as e:
        return api_fail(str(e), code=400)
    except Exception:
        logger.exception("移除附件失败")
        return api_fail("移除附件失败", code=500)


@agent_bp.route("/appearance", methods=["GET", "PUT"])
def appearance():
    try:
        cfg = settings_services.read_user_config()
        values = {"theme": "system", "font_size": 14, "sidebar_collapsed": False, "task_details": True}
        values.update(cfg.get("appearance") or {})
        if request.method == "PUT":
            updates = request.get_json() or {}
            for key, value in updates.items():
                if key == "theme" and value in ("system", "light", "dark"):
                    pass
                elif key == "font_size" and type(value) is int and 12 <= value <= 20:
                    pass
                elif key in ("sidebar_collapsed", "task_details") and type(value) is bool:
                    pass
                else:
                    return api_fail("外观设置无效", code=400)
            values.update(updates)
            cfg["appearance"] = values
            if not settings_services.write_user_config(cfg):
                return api_fail("外观设置保存失败", code=500)
        return api_ok(values)
    except Exception:
        logger.exception("外观设置失败")
        return api_fail("外观设置失败，请查看日志", code=500)


@agent_bp.route("/run", methods=["POST"])
def run():
    """启动一次 Agent 任务；返回 SSE 事件流"""
    try:
        data = request.get_json(force=True) or {}
        session_id = data.get("session_id")
        message = (data.get("message") or "").strip()
        workspace_raw = (data.get("workspace") or "").strip()
        auto_approve_write = bool(data.get("auto_approve_write", False))
        # 权限模式：请求未带时取已保存的设置（默认 approve）
        permission_mode = str(data.get("permission_mode") or "").strip().lower()
        if permission_mode not in ("full", "approve", "plan", "chat"):
            try:
                from apps.settings import services as settings_services
                permission_mode = settings_services.get_permission_mode()
            except Exception:
                permission_mode = "approve"
        # 联网搜索开关：决定 web_search 工具是否进入本次任务的工具表。
        # 缺省 **开启**（True）—— 与前端默认开启保持一致，也让老调用方
        # （不带该字段的请求）行为不变；关闭时完全不会发起网络请求。
        use_web = data.get("use_web")
        use_web = True if use_web is None else bool(use_web)

        if not message:
            return api_fail("任务内容不能为空")

        try:
            message = attachments.task_message(message, data.get("attachment_ids"))
        except (ValueError, TypeError):
            return api_fail("附件不可用或超出限制，请重新添加文件（每轮最多 8 个、总计 8 MiB）", code=400)

        # ── 内容护栏（apps/guard）：命中即拒答，不再构造 AgentRunner ──
        verdict = _blocked_verdict(message, session_id=session_id)
        if verdict.get("blocked"):
            logger.info("Agent 任务被内容护栏拦截 session_id=%s category=%s keyword=%s",
                        session_id, verdict.get("category") or "-",
                        verdict.get("keyword") or "-")
            return _blocked_response(session_id, message)

        # 工作区：请求未带时回退到「已保存的工作区」（避免前端状态过期导致误报未设置）
        if not workspace_raw:
            try:
                from apps.settings import services as settings_services
                workspace_raw = str(settings_services.get_workspace().get("path") or "").strip()
            except Exception:
                workspace_raw = ""
        # 统一对话窗口：未设置工作区时不再报错，降级为「仅保留对话」——
        # 纯聊天不依赖工作区，服务端硬禁用一切文件/命令工具（安全边界不变）。
        workspace = None
        if not workspace_raw:
            permission_mode = "chat"
        else:
            try:
                workspace = open_workspace(workspace_raw)
            except SandboxError as e:
                logger.info("工作区校验失败：%s / %s", e.code, e.message)
                return api_fail(e.message or "工作区不可用，请选择存在的目录", code=400)
            except Exception:
                logger.exception("打开工作区失败")
                return api_fail("工作区不可用，请选择存在的目录", code=400)

        session_id = _ensure_session(session_id)
        if not session_id:
            return api_fail("会话不存在", code=404)

        try:
            aichat_services.update_session_title_if_default(session_id, data.get("message") or "")
            session = aichat_services.get_session(session_id)
            aichat_services.save_message(session_id, "user", message)
            history = aichat_services.list_messages(session_id)
        except Exception:
            logger.exception("保存 Agent 任务消息失败")
            return api_fail("保存任务失败，请查看服务日志", code=500)

        system_prompt = ""
        try:
            system_prompt = (CONFIG.get("llm") or {}).get("system_prompt") or ""
        except Exception:
            system_prompt = ""

        # 思考量级：请求显式指定则优先，否则用已保存的设置（默认 balanced）
        thinking_level = (data.get("thinking_level") or "").strip().lower()
        if thinking_level not in ("light", "balanced", "max"):
            try:
                thinking_level = settings_services.get_thinking_level()
            except Exception:
                thinking_level = "balanced"

        # 已启用技能 → 注入本次任务的 system 提示词（含内置 + 自定义）
        try:
            enabled_skills = skills_services.get_enabled_skills()
        except Exception:
            logger.exception("读取已启用技能失败")
            enabled_skills = []

        runner = AgentRunner(
            workspace=workspace,
            user_task=message,
            session_id=session_id,
            history=history,
            cancel_event=threading.Event(),
            auto_approve_write=auto_approve_write,
            system_prompt=system_prompt,
            config=_agent_config(),
            permission_mode=permission_mode,
            thinking_level=thinking_level,
            skills=enabled_skills,
            use_web=use_web,
        )

        def generate():
            try:
                for event in runner.run():
                    if event.get("type") == "run_start":
                        event["session_id"] = session_id
                        event["session_title"] = session["session_title"]
                    yield _sse(event)
            except Exception:
                logger.exception("Agent 事件流异常中断 task_id=%s", runner.task_id)
                yield _sse({"type": "error", "code": "SERVER_ERROR",
                            "content": "任务执行异常中断，请查看服务日志",
                            "reason": "任务执行异常中断，请查看服务日志",
                            "action": "请重试，或到设置中更换模型服务",
                            "need_settings": False})
            finally:
                # 任务结束（含被消费者提前关闭）后留存变更清单，供 /changes 与 /undo 使用
                _remember_changeset(runner.task_id, runner.changeset)

        return Response(generate(), mimetype="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache",
                            "X-Accel-Buffering": "no",
                            "Connection": "keep-alive",
                        })
    except Exception:
        logger.exception("启动 Agent 任务失败")
        return api_fail("启动 Agent 任务失败，请查看服务日志", code=500)


# ── 审批决策（在另一个线程唤醒挂起的 Runner）───────────────
@agent_bp.route("/approve", methods=["POST"])
def approve():
    """提交审批决策：allow_once / allow_session / deny"""
    try:
        data = request.get_json(force=True) or {}
        request_id = (data.get("request_id") or "").strip()
        decision = (data.get("decision") or "").strip()
        task_id = (data.get("task_id") or "").strip()

        if not request_id:
            return api_fail("缺少 request_id")
        if decision not in (DECISION_ALLOW_ONCE, DECISION_ALLOW_SESSION, DECISION_DENY):
            return api_fail("decision 取值不合法")

        ok = approval_manager.resolve(request_id, decision)
        if not ok:
            return api_fail("该审批请求不存在或已处理", code=404)

        logger.info("审批已处理 request_id=%s decision=%s task_id=%s",
                    request_id, decision, task_id)
        return api_ok({"request_id": request_id, "decision": decision,
                       "task_id": task_id}, msg="已提交")
    except Exception:
        logger.exception("处理审批决策失败")
        return api_fail("处理审批决策失败，请查看服务日志", code=500)


# ── 取消任务 ───────────────────────────────────────────────
@agent_bp.route("/cancel", methods=["POST"])
def cancel():
    """取消任务：置 cancel_event（会杀掉在跑的子进程）+ 挂起审批按拒绝处理"""
    try:
        data = request.get_json(force=True) or {}
        task_id = (data.get("task_id") or "").strip()
        if not task_id:
            return api_fail("缺少 task_id")

        ok = cancel_task(task_id)
        if not ok:
            return api_fail("任务不存在或已结束", code=404)
        return api_ok({"task_id": task_id, "cancelled": True}, msg="已取消")
    except Exception:
        logger.exception("取消 Agent 任务失败")
        return api_fail("取消任务失败，请查看服务日志", code=500)


# ── 暂停 / 继续（协作式：暂停后循环在下一步阻塞直到继续）────────
@agent_bp.route("/pause", methods=["POST"])
def pause():
    """暂停一个运行中的任务（清除 pause_event，循环在步首阻塞）"""
    try:
        data = request.get_json(force=True) or {}
        task_id = (data.get("task_id") or "").strip()
        if not task_id:
            return api_fail("缺少 task_id")
        runner = get_runner(task_id)
        if not runner:
            return api_fail("任务不存在或已结束", code=404)
        runner.pause_event.clear()
        return api_ok({"task_id": task_id, "paused": True}, msg="已暂停")
    except Exception:
        logger.exception("暂停 Agent 任务失败")
        return api_fail("暂停失败，请查看服务日志", code=500)


@agent_bp.route("/resume", methods=["POST"])
def resume():
    """继续一个被暂停的任务（置位 pause_event，循环解除阻塞）"""
    try:
        data = request.get_json(force=True) or {}
        task_id = (data.get("task_id") or "").strip()
        if not task_id:
            return api_fail("缺少 task_id")
        runner = get_runner(task_id)
        if not runner:
            return api_fail("任务不存在或已结束", code=404)
        runner.pause_event.set()
        return api_ok({"task_id": task_id, "resumed": True}, msg="已继续")
    except Exception:
        logger.exception("继续 Agent 任务失败")
        return api_fail("继续失败，请查看服务日志", code=500)


# ── 变更清单 ───────────────────────────────────────────────
@agent_bp.route("/changes", methods=["GET"])
def changes():
    """查询某任务的变更清单"""
    try:
        task_id = (request.args.get("task_id") or "").strip()
        if not task_id:
            return api_fail("缺少 task_id")

        changeset = _find_changeset(task_id)
        if changeset is None:
            return api_fail("任务不存在或已结束", code=404)

        records = changeset.to_list()
        return api_ok({"task_id": task_id, "count": len(records), "changes": records})
    except Exception:
        logger.exception("查询变更清单失败")
        return api_fail("查询变更清单失败，请查看服务日志", code=500)


# ── 工作区检索与文件预览（只读；供左栏搜索与产物面板定位）──────
def _saved_workspace():
    """读取已保存的工作区（打开失败/未设置返回 None）"""
    try:
        from apps.settings import services as settings_services
        path = str(settings_services.get_workspace().get("path") or "").strip()
    except Exception:
        path = ""
    if not path:
        return None
    try:
        return open_workspace(path)
    except Exception:
        return None


@agent_bp.route("/workspace/search", methods=["GET"])
def workspace_search():
    """
    在当前工作区内跨目录检索：文件名 + 文件内容（大小写不敏感；关键词或正则）。

    ?q=关键词 & regex=false & glob=*（文件通配，如 *.py） & limit=80
    """
    try:
        q = (request.args.get("q") or "").strip()
        if not q:
            return api_fail("搜索内容不能为空")
        use_regex = (request.args.get("regex") or "false").lower() == "true"
        file_glob = (request.args.get("glob") or "*").strip() or "*"
        try:
            limit = max(1, min(200, int(request.args.get("limit") or 80)))
        except (TypeError, ValueError):
            limit = 80

        ws = _saved_workspace()
        if ws is None:
            return api_fail("未设置工作区，请先选择工作区目录", code=400)

        rx = None
        if use_regex:
            try:
                rx = re.compile(q, re.IGNORECASE)
            except re.error:
                return api_fail("正则表达式不合法：%s" % q[:40])
        needle = q.lower()

        matches = []
        truncated = False
        for file_path in ws.iter_files(".", file_glob):
            rel = ws.rel_path(file_path)
            # ① 文件名匹配
            hit_name = rx.search(rel) is not None if rx else needle in rel.lower()
            if hit_name:
                matches.append({"kind": "name", "path": rel, "line": 0, "text": ""})
                if len(matches) >= limit:
                    truncated = True
                    break
            # ② 内容匹配（非 UTF-8 文本跳过，与 search_code 行为一致）
            try:
                text = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError, ValueError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                hit = rx.search(line) is not None if rx else needle in line.lower()
                if not hit:
                    continue
                matches.append({"kind": "content", "path": rel, "line": lineno,
                                "text": line[:200] if len(line) > 200 else line})
                if len(matches) >= limit:
                    truncated = True
                    break
            if truncated:
                break

        return api_ok({"pattern": q, "matches": matches,
                       "total": len(matches), "truncated": truncated})
    except Exception:
        logger.exception("工作区检索失败")
        return api_fail("工作区检索失败，请查看服务日志", code=500)


@agent_bp.route("/workspace/file", methods=["GET"])
def workspace_file():
    """读取工作区内一个文本文件（带行号），供搜索结果/产物点击预览"""
    try:
        rel = (request.args.get("path") or "").strip()
        if not rel:
            return api_fail("缺少 path 参数")

        ws = _saved_workspace()
        if ws is None:
            return api_fail("未设置工作区，请先选择工作区目录", code=400)

        def _int_arg(name):
            try:
                value = int(request.args.get(name) or 0)
                return value or None
            except (TypeError, ValueError):
                return None

        result = read_text_file(ws, rel, start_line=_int_arg("start_line"),
                                end_line=_int_arg("end_line"), max_lines=1200)
        # 结构化行（前端高亮与定位用）：read_text_file 的 content 形如 "行号\t内容"
        lines = []
        for row in (result.get("content") or "").split("\n"):
            no, _, text = row.partition("\t")
            lines.append({"no": int(no) if no.isdigit() else 0, "text": text})
        result["lines"] = lines
        return api_ok(result)
    except SandboxError as e:
        logger.info("读取工作区文件被拒：%s / %s", e.code, e.message)
        return api_fail(e.message or "路径超出工作区范围", code=400)
    except ToolError as e:
        return api_fail(e.message or "文件读取失败", code=400)
    except Exception:
        logger.exception("读取工作区文件失败")
        return api_fail("读取文件失败，请查看服务日志", code=500)


# ── 回滚 ───────────────────────────────────────────────────
@agent_bp.route("/undo", methods=["POST"])
def undo():
    """回滚变更：传 task_id 撤销全部，或再传 change_id 只撤销单条"""
    try:
        data = request.get_json(force=True) or {}
        task_id = (data.get("task_id") or "").strip()
        change_id = data.get("change_id")

        if not task_id:
            return api_fail("缺少 task_id")

        changeset = _find_changeset(task_id)
        if changeset is None:
            return api_fail("任务不存在或已结束，无法撤销", code=404)

        try:
            change_id = int(change_id) if change_id not in (None, "") else None
        except (TypeError, ValueError):
            return api_fail("change_id 不合法")

        outcome = changeset.undo(change_id)
        failed = outcome.get("failed") or []
        if failed:
            return api_ok({"task_id": task_id, **outcome},
                          msg="已回滚，但有 %d 项失败，请查看服务日志" % len(failed))
        return api_ok({"task_id": task_id, **outcome}, msg="已回滚")
    except Exception:
        logger.exception("回滚变更失败")
        return api_fail("回滚失败，请查看服务日志", code=500)


# ── 工具元信息（调试用）────────────────────────────────────
@agent_bp.route("/tools", methods=["GET"])
def tools():
    """返回全部工具的元信息（不含任何密钥）"""
    try:
        items = []
        for name in tool_names():
            schema = get_tool_schema(name) or {}
            items.append({
                "name": name,
                "label": TOOL_LABELS.get(name, name),
                "risk": TOOL_RISK.get(name, "unknown"),
                "required": list(schema.get("required") or []),
                "description": schema.get("description") or "",
            })
        return api_ok({"count": len(items), "tools": items})
    except Exception:
        logger.exception("查询工具列表失败")
        return api_fail("查询工具列表失败，请查看服务日志", code=500)
