# -*- coding: utf-8 -*-
"""
apps/guard — 用户可配置的 AI 回答限制规则（内容护栏）

用途：
    让用户自行决定「哪些话题不允许 AI 回答」，而不是写死在代码里。
    规则 → 类别（built_in 5 个，不可删除）+ 关键词（可批量维护，支持别名与优先级）。
    对话入口（agent / aichat）在调用大模型之前做一次 check()，
    命中则直接返回用户配置的「拒答话术」，不再打扰模型。

分层（沿用既有约定）：
    · services.py  持久化 + 匹配引擎 + 审计日志（不依赖 Flask）
    · routes.py    HTTP 层，只做参数校验与统一响应

对外接口（全部 /api/guard 下，鉴权沿用 main.py 的 before_request）：
    GET    /rules              读取完整配置
    PUT    /rules              局部更新总开关 / 拒答话术
    POST   /categories         新增类别
    PUT    /categories/<id>    改名 / 启停
    DELETE /categories/<id>    删除类别（内置类别拒绝）
    POST   /keywords           新增关键词（单个对象 或 数组批量）
    PUT    /keywords/<id>      修改关键词
    DELETE /keywords/<id>      删除关键词
    GET    /export             导出配置
    POST   /import             导入配置（默认整体替换，可选合并）
    POST   /check              调试用：只检测不落库

响应约定：
    一律 api_ok / api_fail（HTTP 200，业务码在 body.code），
    异常详情只写 logger，**绝不回传原始报错**。
"""
from flask import Blueprint

guard_bp = Blueprint("guard", __name__, url_prefix="/api/guard")

from . import routes  # noqa: E402,F401  注册路由（蓝图已在上方声明）
