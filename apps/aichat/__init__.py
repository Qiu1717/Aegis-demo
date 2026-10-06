"""aichat 模块初始化"""
from flask import Blueprint

aichat_bp = Blueprint("aichat", __name__, url_prefix="/api/aichat")

from . import routes  # noqa: E402,F401
