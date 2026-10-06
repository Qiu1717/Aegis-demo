# -*- coding: utf-8 -*-
"""
apps/agent — 编码 Agent 模块（读代码 / 改文件 / 跑命令 / 多步自主迭代）

本阶段（T03）只提供内核与工具层：
    schemas.py   6 个工具的 OpenAI function JSON Schema
    sandbox.py   工作区路径沙箱
    approval.py  风险分级 + 人在环路审批（挂起 / 恢复）
    trace.py     变更记录 ChangeRecord / ChangeSet + 轨迹落库
    fileio.py    原子写 + 备份 + unified diff
    terminal.py  子进程命令执行（超时 / 截断 / 可中断）
    tools.py     六个工具实现与 dispatch
    prompts.py   Agent 系统提示词（原生 tools 版 / JSON 回退版）
    parser.py    双通道工具调用解析
    runner.py    25 步编排循环 + SSE 事件产出
    routes.py    /api/agent/* 路由层（run / approve / cancel / changes / undo / tools）

⚠️ 导入顺序：Blueprint 必须先建，再 import routes（routes 会 `from . import agent_bp`），
   因此下面的 `from . import routes` 刻意放在文件末尾并标注 noqa。
"""
from flask import Blueprint

#: Agent 模块 Blueprint（T04 已在 main.py 中注册）
agent_bp = Blueprint("agent", __name__, url_prefix="/api/agent")

__all__ = ["agent_bp"]

# 注册路由（必须放在 agent_bp 定义之后）
from . import routes  # noqa: E402,F401
