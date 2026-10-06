"""websearch 模块初始化"""
from flask import Blueprint

search_bp = Blueprint("websearch", __name__, url_prefix="/api/search")

from . import routes  # noqa: E402,F401
