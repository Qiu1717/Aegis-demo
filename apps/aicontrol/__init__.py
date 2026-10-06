"""aicontrol 模块初始化"""
from flask import Blueprint

aicontrol_bp = Blueprint("aicontrol", __name__, url_prefix="/api/aicontrol")

from . import routes  # noqa: E402,F401
