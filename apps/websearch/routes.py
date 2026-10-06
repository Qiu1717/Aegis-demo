"""
websearch 模块 - 路由层
提供联网搜索接口：
    POST /api/search        { q, max_results?, provider? } -> 结构化搜索结果
    GET  /api/search/status                                -> 当前搜索引擎状态

异常处理约定：与 aichat 一致，异常详情只写服务端日志，接口返回通用文案。
"""
from flask import request

from apps import api_ok, api_fail, logger
from apps.websearch import search_bp
from . import services


@search_bp.route("", methods=["POST"])
def search():
    """发起一次联网搜索，返回结构化结果列表。"""
    try:
        data = request.get_json(silent=True) or {}
        q = (data.get("q") or "").strip()
        if not q:
            return api_fail("搜索内容不能为空")

        try:
            max_results = int(data.get("max_results") or 0)
        except (TypeError, ValueError):
            max_results = 0
        provider = data.get("provider") or None

        result = services.web_search(q, max_results=max_results, provider=provider)
        return api_ok(result)
    except ValueError as e:
        return api_fail(str(e))
    except RuntimeError as e:
        # 网络/解析兜底：返回友好文案，不暴露内部细节
        logger.warning("搜索失败: %s", e)
        return api_fail(str(e) or "搜索失败，请稍后重试", code=502)
    except Exception:
        logger.exception("搜索请求失败")
        return api_fail("搜索失败，请稍后重试", code=500)


@search_bp.route("/status", methods=["GET"])
def status():
    """返回当前搜索引擎配置与可用性（不含密钥）。"""
    try:
        return api_ok(services.search_status())
    except Exception:
        logger.exception("获取搜索状态失败")
        return api_fail("获取搜索状态失败", code=500)
