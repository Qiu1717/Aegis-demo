# -*- coding: utf-8 -*-
"""
apps/skills — 外部 Skill 接入机制（1.4.0）

一个 Skill = 一段带名称与描述的指令（prompt 型），注册后可启用/禁用；
Agent 启动任务时会把「已启用技能」注入 system 提示词，模型按需采用，
回复中以「[技能:名称]」标注时前端会展示技能调用状态。

扩展点（后续新增 skill 类型时只改这里）：
    · 目前仅支持 "prompt" 型（指令注入）；
    · 预留 SKILL_TYPES：后续可加 "http"（Webhook 工具）、"mcp"（外部工具服务）等，
      届时在 services.validate_skill / runner 注入层按 type 分发即可；
    · 内置技能在 BUILT_IN_SKILLS 中声明（不可删除，可禁用）。
"""
from .routes import skills_bp  # noqa: F401
