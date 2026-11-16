# -*- coding: utf-8 -*-
"""
apps/settings/routes.py — /api/settings/* 路由

约定（沿用现有风格）：
    · 路由体一律 try/except 包住；
    · 详情用 logger.exception("中文描述") 记日志；
    · 对外只回 api_ok / api_fail，**绝不回传 str(e)**。
"""
from flask import request

from apps import api_fail, api_ok, logger

from . import services, settings_bp


def _json_body() -> dict:
    """安全取 JSON 请求体（非 JSON 时返回空 dict）"""
    try:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@settings_bp.route("/providers", methods=["GET"])
def get_providers():
    """读取服务商配置（密钥一律掩码）"""
    try:
        return api_ok(services.get_providers())
    except Exception:
        logger.exception("读取模型服务商配置失败")
        return api_fail("读取模型服务商配置失败，请查看服务日志", code=500)


@settings_bp.route("/providers", methods=["PUT"])
def put_providers():
    """保存服务商配置（密钥写系统凭据区，保存即生效）"""
    try:
        payload = _json_body()
        if not payload:
            return api_fail("请求内容为空", code=400)
        data = services.save_providers(payload)
        if not data.get("ok", True):
            err = data.get("error") or {}
            return api_fail(err.get("reason") or "保存失败，请查看服务日志",
                            code=500,
                            data={"code": err.get("code"),
                                  "action": err.get("action")})
        if data.get("key_failed"):
            return api_ok(data, msg="配置已保存，但密钥未能写入本机密钥库，请检查系统凭据管理器是否可用")
        return api_ok(data, msg="配置已保存并立即生效")
    except Exception:
        logger.exception("保存模型服务商配置失败")
        return api_fail("保存失败，请查看服务日志", code=500)


@settings_bp.route("/models", methods=["POST"])
def list_models():
    """拉取某个服务商可用的模型清单（OpenAI 兼容端点的 GET {base_url}/models）

    供设置页「自定义厂商」表单用：既能拉取远端清单，也能在端点不支持时
    回退到预设候选，用户始终可以手动填写模型名。
    """
    try:
        body = _json_body()
        provider_key = (body.get("provider_key") or body.get("key") or "custom").strip()
        result = services.list_models(
            provider_key or "custom",
            api_key=body.get("api_key"),
            base_url=body.get("base_url"),
        )
        # 失败也用 code=0：这属于「预期内的业务结果」（端点可能不实现 /models）
        return api_ok(result, msg=("已获取 %d 个模型" % len(result.get("models") or []))
                      if result.get("ok") else ((result.get("error") or {}).get("reason") or "拉取模型失败"))
    except Exception:
        logger.exception("拉取模型列表失败")
        return api_fail("拉取模型列表失败，请查看服务日志", code=500)


@settings_bp.route("/test", methods=["POST"])
def test_connection():
    """测试与某个服务商的连通性（不依赖 provider_chain）"""
    try:
        body = _json_body()
        provider_key = (body.get("provider_key") or body.get("key") or "").strip()
        if not provider_key:
            return api_fail("缺少服务商标识", code=400)

        result = services.test_connection(
            provider_key,
            api_key=body.get("api_key"),
            base_url=body.get("base_url"),
            model=body.get("model"),
            timeout=body.get("timeout"),
        )
        # 失败也用 code=0 返回：这是「预期内的业务结果」，不是接口错误
        return api_ok(result, msg="连接成功" if result.get("ok") else "连接失败")
    except Exception:
        logger.exception("测试模型连接失败")
        return api_fail("测试连接失败，请查看服务日志", code=500)


@settings_bp.route("/workspace", methods=["GET"])
def get_workspace():
    """读取当前工作区"""
    try:
        return api_ok(services.get_workspace())
    except Exception:
        logger.exception("读取工作区失败")
        return api_fail("读取工作区失败，请查看服务日志", code=500)


@settings_bp.route("/workspace", methods=["PUT"])
def put_workspace():
    """设置工作区（后端二次校验存在性）"""
    try:
        body = _json_body()
        path = (body.get("path") or body.get("workspace") or "").strip()
        if not path:
            return api_fail("工作区路径不能为空", code=400)
        result = services.set_workspace(path)
        if not result.get("ok"):
            return api_fail(result.get("reason") or "工作区路径无效", code=400)
        return api_ok(result, msg="工作区已设置")
    except Exception:
        logger.exception("设置工作区失败")
        return api_fail("设置工作区失败，请查看服务日志", code=500)


@settings_bp.route("/permission", methods=["GET"])
def get_permission():
    """读取当前权限模式（full / approve / plan / chat）"""
    try:
        return api_ok({"mode": services.get_permission_mode(),
                       "modes": list(services.PERMISSION_MODES),
                       "default": services.DEFAULT_PERMISSION_MODE})
    except Exception:
        logger.exception("读取权限模式失败")
        return api_fail("读取权限设置失败，请查看服务日志", code=500)


@settings_bp.route("/permission", methods=["PUT"])
def put_permission():
    """保存权限模式（持久化，重启后保留）"""
    try:
        body = _json_body()
        mode = (body.get("mode") or "").strip().lower()
        result = services.set_permission_mode(mode)
        if not result.get("ok"):
            return api_fail(result.get("reason") or "权限模式无效", code=400,
                            data={"mode": result.get("mode")})
        return api_ok(result, msg="权限设置已保存")
    except Exception:
        logger.exception("保存权限模式失败")
        return api_fail("保存权限设置失败，请查看服务日志", code=500)


@settings_bp.route("/thinking", methods=["GET"])
def get_thinking():
    """读取当前思考量级（light / balanced / max）及其标签"""
    try:
        return api_ok({"level": services.get_thinking_level(),
                       "levels": list(services.THINKING_LEVELS),
                       "labels": services.THINKING_LABELS,
                       "default": services.DEFAULT_THINKING_LEVEL})
    except Exception:
        logger.exception("读取思考量级失败")
        return api_fail("读取思考量级失败，请查看服务日志", code=500)


@settings_bp.route("/thinking", methods=["PUT"])
def put_thinking():
    """保存思考量级（持久化，重启后保留）"""
    try:
        body = _json_body()
        level = (body.get("level") or "").strip().lower()
        result = services.set_thinking_level(level)
        if not result.get("ok"):
            return api_fail(result.get("reason") or "思考量级无效", code=400,
                            data={"level": result.get("level")})
        return api_ok(result, msg="思考量级已保存")
    except Exception:
        logger.exception("保存思考量级失败")
        return api_fail("保存思考量级失败，请查看服务日志", code=500)


@settings_bp.route("/bootstrap", methods=["GET"])
def bootstrap():
    """首次启动引导所需信息（是否需要配置、可用预设、推荐服务商）"""
    try:
        return api_ok(services.bootstrap())
    except Exception:
        logger.exception("读取启动引导信息失败")
        return api_fail("读取启动引导信息失败，请查看服务日志", code=500)


@settings_bp.route("/export", methods=["GET"])
def export_config():
    """导出不含密钥的配置模板"""
    try:
        return api_ok(services.export_config())
    except Exception:
        logger.exception("导出配置失败")
        return api_fail("导出配置失败，请查看服务日志", code=500)
