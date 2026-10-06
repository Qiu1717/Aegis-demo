# -*- coding: utf-8 -*-
"""
apps/skills/routes.py — /api/skills/* 路由（Skill 注册中心）

    GET    /api/skills            技能清单（含内置）
    POST   /api/skills            注册自定义技能 {name, description, instructions}
    PUT    /api/skills/<id>       更新（启用/禁用/改名/改描述/改指令）
    DELETE /api/skills/<id>       删除（内置技能拒绝）

沿用现有约定：try/except + logger + api_ok/api_fail，不向前端暴露原始报错。
"""
from flask import Blueprint, request

from apps import api_fail, api_ok, logger

skills_bp = Blueprint("skills", __name__, url_prefix="/api/skills")

from . import services as skills_services


@skills_bp.route("", methods=["GET"])
def list_skills():
    """技能清单"""
    try:
        return api_ok({"skills": skills_services.list_skills()})
    except Exception:
        logger.exception("读取技能清单失败")
        return api_fail("读取技能清单失败，请查看服务日志", code=500)


@skills_bp.route("", methods=["POST"])
def register_skill():
    """注册自定义技能"""
    try:
        data = request.get_json(force=True) or {}
        result = skills_services.register_skill(
            name=data.get("name"),
            description=data.get("description"),
            instructions=data.get("instructions"),
            skill_id=data.get("id"),
            enabled=bool(data.get("enabled", False)),
        )
        if not result.get("ok"):
            return api_fail(result.get("reason") or "注册技能失败", code=400)
        return api_ok(result, msg="技能已注册")
    except Exception:
        logger.exception("注册技能失败")
        return api_fail("注册技能失败，请查看服务日志", code=500)


@skills_bp.route("/import", methods=["POST"])
def import_skills():
    """批量导入技能（来自本地 JSON 文件或技能包）。

    请求体：单个技能对象 / 裸数组 / {"skills": [...]} 三种形态之一；
    可选 overwrite=true 覆盖同名 id 的自定义技能。
    逐条校验：成功的入库，失败的原因随 skipped 一并返回，不中断其余条目。
    """
    try:
        data = request.get_json(force=True) or {}
        payload = data.get("payload", data)
        result = skills_services.import_skills(
            payload, overwrite=bool(data.get("overwrite", False))
        )
        if not result.get("ok"):
            # 部分失败也算失败，但要把 skipped 明细带回去给前端展示
            return api_fail(result.get("reason") or "导入技能失败",
                            code=400, data={"skipped": result.get("skipped", [])})
        return api_ok(result, msg="成功导入 %d 个技能" % result.get("total", 0))
    except Exception:
        logger.exception("导入技能失败")
        return api_fail("导入技能失败，请查看服务日志", code=500)


@skills_bp.route("/<skill_id>", methods=["PUT"])
def update_skill(skill_id):
    """更新技能（启用/禁用等）"""
    try:
        data = request.get_json(force=True) or {}
        fields = {}
        for key in ("enabled", "name", "description", "instructions"):
            if key in data:
                fields[key] = data.get(key)
        result = skills_services.update_skill(skill_id, **fields)
        if not result.get("ok"):
            return api_fail(result.get("reason") or "更新技能失败", code=400)
        return api_ok(result, msg="技能已更新")
    except Exception:
        logger.exception("更新技能失败 skill_id=%s", skill_id)
        return api_fail("更新技能失败，请查看服务日志", code=500)


@skills_bp.route("/<skill_id>", methods=["DELETE"])
def delete_skill(skill_id):
    """删除自定义技能（内置技能拒绝）"""
    try:
        result = skills_services.delete_skill(skill_id)
        if not result.get("ok"):
            return api_fail(result.get("reason") or "删除技能失败", code=400)
        return api_ok(result, msg="技能已删除")
    except Exception:
        logger.exception("删除技能失败 skill_id=%s", skill_id)
        return api_fail("删除技能失败，请查看服务日志", code=500)
