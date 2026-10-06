"""
Aegis (aegis) - 应用启动入口
职责：创建 Flask 应用、注册 Blueprint、配置 CORS、注册页面路由
"""
import os
import time
from flask import (Flask, abort, g, make_response, render_template, request,
                   send_from_directory)
from flask_cors import CORS

from apps import logger, CONFIG, api_ok
from apps import paths
from apps.aicontrol import aicontrol_bp
from apps.aichat import aichat_bp
from apps.skills import skills_bp
from apps.guard import guard_bp
from apps.websearch import search_bp
from apps.settings import settings_bp
from apps.agent import agent_bp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

#: 仅允许本机来源跨域（AEG-D01：防止恶意网页驱动本地 Agent）
_LOCAL_ORIGIN_PATTERN = r"http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?"

#: 本地鉴权请求头
TOKEN_HEADER = "X-Aegis-Token"

#: 进程内 token 单例（优先复用桌面壳写入 runtime.json 的那个）
_TOKEN = ""


def get_token():
    """
    取得本次运行的本地鉴权 token。

    取值顺序：内存单例 → runtime.json（由 desktop/server.py 写入）。
    两者都没有时才新生成一个并写入 runtime.json（仅运行时文件，进程退出即失效，
    绝不写入 config.json / config.user.json）。
    """
    global _TOKEN
    if _TOKEN:
        return _TOKEN

    token = ""
    try:
        token = str(paths.runtime_get("token", "") or "")
    except Exception:
        token = ""

    if not token:
        token = paths.new_token()
        try:
            if not paths.runtime_set("token", token):
                logger.warning("写入 runtime.json 失败，token 仅存在于本进程内存")
        except Exception:
            logger.warning("写入 runtime.json 异常，token 仅存在于本进程内存")

    _TOKEN = token
    return _TOKEN


def set_token(token):
    """由桌面壳在起服务前注入 token（避免与 runtime.json 不一致）"""
    global _TOKEN
    _TOKEN = str(token or "")
    return _TOKEN


def _static_version() -> str:
    """
    静态资源版本戳（供模板拼 ?v= 强制刷新缓存）。

    取「最新修改的前端文件 mtime」而非应用版本号 —— 应用版本不变但前端代码
    改动时（本项目打包后仍会热替换 _internal 下的 js/css），版本号不变会导致
    浏览器/WebView 继续用旧缓存，用户看到「改了没生效」。

    :return: 形如 "1750000000" 的短字符串
    """
    static_dir = os.path.join(BASE_DIR, "static")
    newest = 0.0
    try:
        for root, _dirs, files in os.walk(static_dir):
            for name in files:
                if not name.endswith((".js", ".css")):
                    continue
                try:
                    mt = os.path.getmtime(os.path.join(root, name))
                    if mt > newest:
                        newest = mt
                except OSError:
                    continue
    except OSError:
        pass
    return str(int(newest) if newest else 0)


def create_app():
    app = Flask(__name__,
                template_folder=os.path.join(BASE_DIR, "templates"),
                static_folder=os.path.join(BASE_DIR, "static"))

    # ── 跨域支持（收紧为本机来源）──────────────────────────
    CORS(app, resources={r"/api/*": {"origins": _LOCAL_ORIGIN_PATTERN}})

    # ── 请求分发日志（PDD 3.2.2：统一处理请求分发、日志记录）──
    @app.before_request
    def log_request_start():
        g._start_ts = time.time()

    @app.before_request
    def guard_local_api():
        """
        本地鉴权（安全模型第①道闸）：所有 /api/* 必须携带 X-Aegis-Token。
        页面与静态资源放行；预检请求（OPTIONS）放行，由 CORS 负责来源校验。
        """
        path = request.path or ""
        if not path.startswith("/api/"):
            return None
        if request.method == "OPTIONS":
            return None

        expected = get_token()
        if not expected:
            # 拿不到 token 时不做拦截，避免把整个应用锁死
            return None
        if request.headers.get(TOKEN_HEADER, "") == expected:
            return None

        from apps import api_fail
        logger.warning("拒绝未授权请求：%s %s", request.method, path)
        return api_fail("未授权的访问请求，请在 Aegis 窗口内操作", code=401), 401

    @app.after_request
    def log_request_done(resp):
        # 静态资源量大，不记日志
        if not request.path.startswith("/static/"):
            cost = int((time.time() - getattr(g, "_start_ts", time.time())) * 1000)
            logger.info("%s %s -> %s (%dms)",
                        request.method, request.path, resp.status_code, cost)
        return resp

    # ── 注册 Blueprint ─────────────────────────────────────
    app.register_blueprint(aicontrol_bp)
    app.register_blueprint(aichat_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(settings_bp)
    # T04：Agent 内核已就绪，注册 /api/agent/*（run / approve / cancel / changes / undo / tools）
    app.register_blueprint(agent_bp)
    # 1.4.0：外部 Skill 注册中心（/api/skills）
    app.register_blueprint(skills_bp)
    # 内容护栏：用户可配置的 AI 回答限制规则（/api/guard）
    app.register_blueprint(guard_bp)

    # ── 本地鉴权 token 下发（仅限本机访问，不落任何配置文件）──
    @app.route("/token.js")
    def token_js():
        if not paths.is_local_address(request.remote_addr):
            abort(403)
        body = 'window.AEGIS_TOKEN = "%s";\n' % get_token()
        resp = make_response(body, 200)
        resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ── 模板全局上下文：让页面显示当前真正生效的模型 ────────
    @app.context_processor
    def inject_global():
        from apps.llm import get_active_label
        try:
            label = get_active_label()
        except Exception:
            label = "Aegis"
        return {"powered_by": label, "aegis_token": get_token()}

    # ── 运行状态自检 ───────────────────────────────────────
    @app.route("/api/status")
    def api_status():
        """返回各模型服务商的可用情况，用于排查配置问题"""
        from apps import api_ok, api_fail
        from apps.llm import get_status
        from apps.aichat import memory as mem
        from apps.websearch import services as search_svc
        try:
            status = get_status()
            status["memory"] = {
                "enabled": mem.ENABLED,
                "auto_extract": mem._MEMORY_CFG.get("auto_extract", False),
                "max_items": mem.MAX_ITEMS,
                "ttl_days": mem.TTL_DAYS,
                "count": len(mem.get_user_memory(1)),
            }
            status["search"] = search_svc.search_status()
            return api_ok(status)
        except Exception:
            logger.exception("获取运行状态失败")
            return api_fail("获取运行状态失败，请查看服务日志", code=500)

    # ── 页面路由 ───────────────────────────────────────────
    @app.route("/")
    def index():
        # 首页直达 Aegis 智能体工作台 /workspace（历史侧边栏 + 任务执行 + 对话）。
        # 三栏工作台 /aegis 仍保留为完整产物面板入口；旧聊天页 /aichat 行为兼容。
        from flask import redirect
        return redirect("/workspace", code=302)

    @app.route("/aicontrol")
    def aicontrol_page():
        return render_template("aicontrol/aicontrol_main.html",
                           static_ver=_static_version())

    @app.route("/aichat")
    def aichat_page():
        return render_template("aichat/aichat_main.html",
                           static_ver=_static_version())

    @app.route("/aichat/chat")
    def aichat_chat_page():
        return render_template("aichat/aichat_chat.html",
                           static_ver=_static_version())

    @app.route("/aegis")
    def aegis_page():
        """Aegis 编码 Agent 工作台（三栏：任务 / 时间线 / 变更）"""
        # 环境检测日志：排查「桌面端被误判为浏览器」类问题。
        # WebView2 的 UA 与 Edge 浏览器几乎一致（无独立标识），
        # 因此 UA 只作参考；真正的桌面判定在前端看 window.pywebview.platform。
        ua = request.headers.get("User-Agent", "")
        env_debug = bool((CONFIG.get("desktop") or {}).get("env_debug", False))
        if env_debug:
            logger.info("打开 Aegis 工作台 UA=%s", ua)
        else:
            logger.info("打开 Aegis 工作台 UA=%s", ua[:120])
        return render_template("agent/aegis_main.html",
                           static_ver=_static_version())

    @app.route("/skills")
    def skills_page():
        """技能中心（独立页面）。

        刻意做成独立路由页面而不是浮层弹窗：浮层需要 z-index 层级与半透明遮罩，
        容易遮挡主页面、背景透视、文字重叠；独立页面自带不透明背景与独立布局容器，
        层级天然隔离。数据全部来自 apps/skills 的真实读写（持久化到用户配置文件）。
        """
        logger.info("打开技能中心")
        return render_template("agent/skills.html",
                           static_ver=_static_version())

    @app.route("/workspace")
    def workspace_page():
        """Aegis 智能体工作台（历史侧边栏 + 任务执行 + 对话区 + 底部输入区）。

        视觉遵循统一设计语言：8px 栅格、品牌主紫 #7C3AED、
        语义色（探索蓝 / 构建紫 / 审查绿 / 修复橙）、圆角 12-8-6、
        150ms 悬停过渡、聚焦主色外发光、200ms 淡入切换，并响应式适配移动端。
        与三栏的 /aegis 并存：/aegis 保留完整产物面板与变更清单，
        本页聚焦会话、任务执行状态与智能体配置入口，两者共用同一套后端接口。
        """
        logger.info("打开 Aegis 智能体工作台")
        return render_template("agent/aegis_workspace.html",
                               static_ver=_static_version())

    @app.route("/api/env")
    def api_env():
        """环境检测信息：供前端控制台与排障使用（不做任何鉴权外泄，仅本机可读）"""
        ua = request.headers.get("User-Agent", "")
        env_debug = bool((CONFIG.get("desktop") or {}).get("env_debug", False))
        info = {
            "ua": ua[:200],
            "ua_is_edge_like": "Edg/" in ua,
            # WebView2 与 Edge 的 UA 无可靠差异，桌面判定以前端 window.pywebview 为准
            "desktop_hint": None,
            "env_debug": env_debug,
        }
        if env_debug:
            logger.info("环境检测请求 UA=%s Referer=%s", ua, request.headers.get("Referer", ""))
        return api_ok(info)

    # ── 统一异常处理（PRD 8.3 / PDD 8.4：不向前端暴露原始报错）──
    @app.errorhandler(500)
    def server_error(e):
        from apps import api_fail
        logger.exception("服务器内部错误")
        return api_fail("服务器内部错误，请稍后重试", code=500)

    @app.errorhandler(404)
    def not_found(e):
        from apps import api_fail
        return api_fail("接口不存在", code=404)

    @app.errorhandler(Exception)
    def unhandled(e):
        from apps import api_fail
        from werkzeug.exceptions import HTTPException
        # 404/405 等标准 HTTP 错误交给上面的专用处理器，不在这里吞掉
        if isinstance(e, HTTPException):
            return e
        logger.exception("未捕获异常: %s %s", request.method, request.path)
        return api_fail("服务器内部错误，请稍后重试", code=500)

    return app


if __name__ == "__main__":
    from apps import CONFIG

    app = create_app()
    app_cfg = CONFIG.get("app", {})
    # 桌面端安全基线：只监听回环地址、关闭调试器；
    # threaded=True 是硬要求 —— 审批请求与 SSE 请求必须能并发（设计 §1.2）。
    chosen_port = int(app_cfg.get("port", 5000) or 5000)
    try:
        picked = paths.pick_port(chosen_port)
        if picked:
            chosen_port = picked
        else:
            logger.warning("端口 %d 及候选范围均被占用，仍尝试监听 %d",
                           chosen_port, chosen_port)
    except Exception:
        logger.exception("探测端口失败，直接使用配置端口")

    app.run(
        host=app_cfg.get("host", "127.0.0.1"),
        port=chosen_port,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
