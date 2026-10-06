"""
aicontrol 模块 - 路由层
提供合规规则的查询、批量保存接口

异常处理约定：
    业务代码抛出的异常一律写入服务端日志，接口只返回通用文案，
    满足 PRD 8.3.3「后端统一接口异常捕获，避免原始报错信息暴露」。
"""
from flask import request

from apps import api_ok, api_fail, logger
from apps.aicontrol import aicontrol_bp
from . import services


@aicontrol_bp.route("/rules", methods=["GET"])
def get_rules():
    """获取全部合规规则"""
    try:
        rules = services.get_all_rules()
        return api_ok(rules)
    except Exception:
        logger.exception("获取合规规则失败")
        return api_fail("获取规则失败，请稍后重试", code=500)


@aicontrol_bp.route("/rules", methods=["POST"])
def save_rules():
    """批量保存全量规则（全量替换）"""
    try:
        data = request.get_json(force=True)
        rules = data.get("rules", [])
        if not isinstance(rules, list):
            return api_fail("参数格式错误：rules 必须为数组")
        services.save_rules(rules)
        # 保存后返回最新列表，保证前端与数据库一致
        latest = services.get_all_rules()
        return api_ok(latest, msg="保存成功")
    except Exception:
        logger.exception("保存合规规则失败")
        return api_fail("保存规则失败，请稍后重试", code=500)
