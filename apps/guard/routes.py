# -*- coding: utf-8 -*-
"""
apps/guard/routes.py — /api/guard/* 路由层（内容护栏）

    GET    /api/guard/rules              完整配置
    PUT    /api/guard/rules              局部更新总开关 / 拒答话术
    POST   /api/guard/categories         新增类别
    PUT    /api/guard/categories/<id>    改名 / 启停
    DELETE /api/guard/categories/<id>    删除类别（内置拒绝）
    POST   /api/guard/keywords           新增关键词（单个对象 / 数组批量）
    PUT    /api/guard/keywords/<id>      修改关键词
    DELETE /api/guard/keywords/<id>      删除关键词
    GET    /api/guard/export             导出配置
    POST   /api/guard/import             导入配置（默认整体替换）
    POST   /api/guard/check              调试检测（不落库）

约定（沿用既有风格）：
    · 每个路由体 try/except 包住，详情写 logger.exception("中文描述")；
    · 对外只回 api_ok / api_fail（HTTP 200，业务码在 body.code），**绝不回传原始报错**。
"""
from flask import request

from apps import api_fail, api_ok, logger

from . import guard_bp
from . import services as guard_services

#: 可局部更新的关键词字段白名单（避免把无关字段写进配置）
_KEYWORD_FIELDS = ("word", "mode", "priority", "enabled", "category", "alias")


def _json_body() -> dict:
    """读取 JSON 请求体；非法或不是对象时一律返回 {}"""
    try:
        data = request.get_json(force=True, silent=True)
    except Exception:
        data = None
    return data if isinstance(data, dict) else {}


def _truthy(value, default: bool = False) -> bool:
    """把 replace / overwrite 之类开关参数转成布尔（无法识别时用 default）"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "on", "replace"):
        return True
    if text in ("false", "0", "no", "n", "off", "merge"):
        return False
    return default


# ── 总配置 ────────────────────────────────────────────────
@guard_bp.route("/rules", methods=["GET"])
def get_rules():
    """读取完整配置（含类别、关键词、总开关、拒答话术）"""
    try:
        return api_ok(guard_services.list_rules())
    except Exception:
        logger.exception("读取内容护栏配置失败")
        return api_fail("读取护栏配置失败，请查看服务日志", code=500)


@guard_bp.route("/rules", methods=["PUT"])
def update_rules():
    """局部更新总配置：目前只接受 enabled 与 refusal_message"""
    try:
        data = _json_body()
        if "enabled" not in data and "refusal_message" not in data:
            return api_fail("没有需要更新的字段", code=400)

        rules = guard_services.load()
        if "enabled" in data:
            value = data.get("enabled")
            if not isinstance(value, bool):
                return api_fail("enabled 必须是布尔值", code=400)
            rules["enabled"] = value
        if "refusal_message" in data:
            message = (data.get("refusal_message") or "").strip()
            if not message:
                return api_fail("拒答话术不能为空", code=400)
            rules["refusal_message"] = message[:200]

        if not guard_services.save_rules(rules):
            return api_fail("保存失败，请查看服务日志", code=500)
        return api_ok(guard_services.list_rules(), msg="已保存")
    except Exception:
        logger.exception("更新内容护栏总配置失败")
        return api_fail("保存护栏配置失败，请查看服务日志", code=500)


# ── 类别 CRUD ─────────────────────────────────────────────
@guard_bp.route("/categories", methods=["POST"])
def add_category():
    """新增自定义类别"""
    try:
        data = _json_body()
        result = guard_services.add_category(
            label=data.get("label", data.get("name")),
            enabled=data.get("enabled", True),
            category_id=data.get("id"),
        )
        if not result.get("ok"):
            return api_fail(result.get("reason") or "新增类别失败", code=400)
        return api_ok(result, msg="类别已新增")
    except Exception:
        logger.exception("新增护栏类别失败")
        return api_fail("新增类别失败，请查看服务日志", code=500)


@guard_bp.route("/categories/<category_id>", methods=["PUT"])
def update_category(category_id):
    """修改类别（改名 / 启停）"""
    try:
        data = _json_body()
        if "label" not in data and "enabled" not in data:
            return api_fail("没有需要更新的字段", code=400)
        result = guard_services.update_category(
            category_id,
            label=data.get("label", data.get("name")),
            enabled=data.get("enabled"),
        )
        if not result.get("ok"):
            return api_fail(result.get("reason") or "更新类别失败", code=400)
        return api_ok(result, msg="类别已更新")
    except Exception:
        logger.exception("更新护栏类别失败 category_id=%s", category_id)
        return api_fail("更新类别失败，请查看服务日志", code=500)


@guard_bp.route("/categories/<category_id>", methods=["DELETE"])
def delete_category(category_id):
    """删除类别（内置类别拒绝删除，只能停用）"""
    try:
        result = guard_services.delete_category(category_id)
        if not result.get("ok"):
            reason = result.get("reason") or "删除类别失败"
            code = 400
            if "不可删除" in reason:
                code = 403
            return api_fail(reason, code=code)
        return api_ok(result, msg="类别已删除")
    except Exception:
        logger.exception("删除护栏类别失败 category_id=%s", category_id)
        return api_fail("删除类别失败，请查看服务日志", code=500)


# ── 关键词 CRUD ───────────────────────────────────────────
@guard_bp.route("/keywords", methods=["POST"])
def add_keywords():
    """
    新增关键词：请求体可以是
        · 单个对象 {"word": "...", ...}
        · 数组     [{...}, {...}]
        · 包装对象 {"keywords": [...]}
    """
    try:
        raw = None
        try:
            raw = request.get_json(force=True, silent=True)
        except Exception:
            raw = None

        if isinstance(raw, dict) and isinstance(raw.get("keywords"), list):
            items = raw.get("keywords")
        elif isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict) and raw:
            items = [raw]
        else:
            return api_fail("请求体必须是关键词对象或数组", code=400)

        if not items:
            return api_fail("缺少关键词内容", code=400)

        result = guard_services.add_keywords(items)
        if not result.get("ok"):
            return api_fail(result.get("reason") or "新增关键词失败", code=400,
                            data={"skipped": result.get("skipped") or []})

        skipped = result.get("skipped") or []
        total = len(result.get("added") or [])
        msg = "已新增 %d 条关键词" % total
        if skipped:
            msg += "，%d 条未通过校验" % len(skipped)
        return api_ok(result, msg=msg)
    except Exception:
        logger.exception("新增护栏关键词失败")
        return api_fail("新增关键词失败，请查看服务日志", code=500)


@guard_bp.route("/keywords/<keyword_id>", methods=["PUT"])
def update_keyword(keyword_id):
    """局部修改关键词（word / mode / priority / enabled / category / alias）"""
    try:
        data = _json_body()
        fields = {key: data[key] for key in _KEYWORD_FIELDS if key in data}
        if not fields:
            return api_fail("没有需要更新的字段", code=400)

        result = guard_services.update_keyword(keyword_id, **fields)
        if not result.get("ok"):
            return api_fail(result.get("reason") or "更新关键词失败", code=400)
        return api_ok(result, msg="关键词已更新")
    except Exception:
        logger.exception("更新护栏关键词失败 keyword_id=%s", keyword_id)
        return api_fail("更新关键词失败，请查看服务日志", code=500)


@guard_bp.route("/keywords/<keyword_id>", methods=["DELETE"])
def delete_keyword(keyword_id):
    """删除关键词"""
    try:
        result = guard_services.delete_keyword(keyword_id)
        if not result.get("ok"):
            return api_fail(result.get("reason") or "删除关键词失败", code=400)
        return api_ok(result, msg="关键词已删除")
    except Exception:
        logger.exception("删除护栏关键词失败 keyword_id=%s", keyword_id)
        return api_fail("删除关键词失败，请查看服务日志", code=500)


# ── 导入 / 导出 ───────────────────────────────────────────
@guard_bp.route("/export", methods=["GET"])
def export_rules():
    """导出当前配置（payload 与规则文件同构，可直接作为 /import 的输入）"""
    try:
        result = guard_services.export_rules()
        if not result.get("ok"):
            return api_fail(result.get("reason") or "导出失败", code=500)
        return api_ok(result)
    except Exception:
        logger.exception("导出内容护栏配置失败")
        return api_fail("导出护栏配置失败，请查看服务日志", code=500)


@guard_bp.route("/import", methods=["POST"])
def import_rules():
    """
    导入配置。

    请求体：
        {"payload": {...}, "replace": true}   或直接就是配置对象本身
    replace=true（默认）整体替换；replace=false / mode="merge" 为合并。
    """
    try:
        try:
            body = request.get_json(force=True, silent=True)
        except Exception:
            body = None
        if not isinstance(body, dict):
            return api_fail("导入内容必须是一个 JSON 对象", code=400)

        payload = body.get("payload", body)
        if "replace" in body:
            replace = _truthy(body.get("replace"), True)
        else:
            replace = str(body.get("mode") or "replace").strip().lower() != "merge"

        result = guard_services.import_rules(payload, replace=replace)
        if not result.get("ok"):
            return api_fail(result.get("reason") or "导入失败", code=400)
        return api_ok(result, msg="导入成功，共 %d 个类别 / %d 条关键词"
                      % (result.get("categories", 0), result.get("keywords", 0)))
    except Exception:
        logger.exception("导入内容护栏配置失败")
        return api_fail("导入护栏配置失败，请查看服务日志", code=500)


# ── 调试检测（不落库）─────────────────────────────────────
@guard_bp.route("/check", methods=["POST"])
def check_text():
    """
    只做检测不修改任何配置，返回命中的规则信息（调试 / 规则编排时使用）。

    注意：命中结果同样会写一条审计日志（与正式拦截一致，便于排障）。
    """
    try:
        data = _json_body()
        text = data.get("text", data.get("message"))
        if text is None:
            text = ""
        result = guard_services.check(text, session_id=data.get("session_id"))
        return api_ok(result)
    except Exception:
        logger.exception("护栏调试检测失败")
        return api_fail("检测失败，请查看服务日志", code=500)
