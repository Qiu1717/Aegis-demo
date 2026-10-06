# -*- coding: utf-8 -*-
"""
apps/settings — 运行时配置（模型服务商 / 密钥 / 工作区）

对前端暴露 /api/settings/* 一组接口（AEG-K01 ~ AEG-K05）：
    GET/PUT  /providers   读取（掩码）/ 保存服务商配置
    POST     /test        测试连接
    GET/PUT  /workspace   读取 / 设置工作区
    GET      /bootstrap   首次启动引导所需的一切信息
    GET      /export      导出不含密钥的配置模板

说明：本包在导入时会把 keystore.load_key 注册给 apps.llm.providers，
从而让「GUI 里填的密钥」立刻参与 build_provider 的密钥解析（见设计 §6.2）。
"""
from flask import Blueprint

settings_bp = Blueprint("settings", __name__, url_prefix="/api/settings")

# routes 里会通过 settings_bp 注册路由，必须在本模块末尾导入
from . import routes  # noqa: E402,F401

__all__ = ["settings_bp"]
