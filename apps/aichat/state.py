# -*- coding: utf-8 -*-
"""
apps/aichat/state.py — 会话级状态的本地文件存储

设计：
    为在「不改动数据库 schema、不引入迁移」的前提下支撑会话归档，
    将会话级状态（当前仅 archived）持久化到数据目录下的
    conversation_state.json。该文件与 config.user.json 同位（%APPDATA%/Aegis），
    采用相同的原子写模式（tmp + os.replace），损坏时自动降级为空状态，绝不抛异常。

    归档状态与数据库中的置顶（is_pinned）正交：本模块只负责 archived，
    不触碰任何 pin 逻辑；置顶仍由 ai_chat_session.is_pinned 承载。
"""
import json
import os
from typing import Any, Dict, Optional

from apps import paths, logger


def state_path() -> str:
    """会话状态文件路径：<data_dir>/conversation_state.json"""
    return str(paths.data_dir() / "conversation_state.json")


def load() -> Dict[str, Any]:
    """
    读取会话状态文件。

    文件不存在 / 损坏 / 顶层非 dict 时返回 {}，绝不抛异常（细节写日志）。
    """
    path = state_path()
    try:
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        logger.exception("读取会话状态文件失败，按空状态处理 path=%s", path)
        return {}


def save(data: Dict[str, Any]) -> bool:
    """
    原子写会话状态文件（先写 .tmp 再 os.replace）。

    失败返回 False，绝不抛异常（细节写日志）。
    """
    try:
        paths.ensure_dirs()
        target = state_path()
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data or {}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return True
    except OSError:
        logger.exception("写入会话状态文件失败 path=%s", state_path())
        return False


def _normalize_key(session_id) -> Optional[str]:
    """把 session_id 归一化为字符串 key；非法返回 None。"""
    try:
        return str(int(session_id))
    except (TypeError, ValueError):
        return None


def archived_map() -> Dict[str, bool]:
    """
    返回 {str_id: True} 的归档映射，供列表接口批量合并。

    仅归档中的会话出现在映射里（取消归档的条目会被移除），避免状态文件无限增长。
    """
    data = load()
    result: Dict[str, bool] = {}
    for key, value in data.items():
        if value:
            result[key] = True
    return result


def is_archived(session_id) -> bool:
    """判断单个会话是否已归档（key 不存在时返回 False）。"""
    key = _normalize_key(session_id)
    if key is None:
        return False
    return bool(load().get(key))


def set_archived(session_id, archived: bool) -> bool:
    """
    写入并持久化某会话的归档状态。

    archived=True 时记录该 key；archived=False 时移除该 key（保持文件精简）。
    返回是否成功；非法 session_id 直接返回 False（写日志）。
    """
    key = _normalize_key(session_id)
    if key is None:
        logger.warning("非法的 session_id，无法设置归档状态：%r", session_id)
        return False
    data = load()
    if archived:
        data[key] = True
    else:
        data.pop(key, None)
    return save(data)


def forget(session_id) -> bool:
    """
    删除会话时清理对应状态条目，避免状态文件无限增长。

    该会话本就无状态条目时视作成功；返回是否成功。
    """
    key = _normalize_key(session_id)
    if key is None:
        return False
    data = load()
    if key not in data:
        return True
    data.pop(key, None)
    return save(data)
