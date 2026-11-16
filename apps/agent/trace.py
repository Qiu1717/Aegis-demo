# -*- coding: utf-8 -*-
"""
apps/agent/trace.py — Agent 轨迹落库与变更清单（AEG-A11 / AEG-A12）

两件事：
    1. 变更记录：`ChangeRecord` / `ChangeSet`，支撑「文件变更」面板与一键撤销；
       撤销时**逆序**回滚：modified→还原备份，created→删除，deleted→还原。
    2. 轨迹落库：复用 `ai_chat_message`，用 `message_type` 区分类型、
       用 `metadata`（JSON 字符串）承载结构化载荷（工具调用、diff、变更记录）。

SQL 一律用 `%s` 占位符（`apps.execute()` 内部会转成 `?` 适配 SQLite）。
metadata 列由 `apps/__init__.py::_init_sqlite` 的兼容迁移补上；
若目标库（如 MySQL）确实没有该列，则自动降级为不带 metadata 的写入，绝不报错。
"""
import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional

import apps
from apps import logger
from .sandbox import SandboxError, Workspace

# ── message_type 取值（在既有 'text' 之上扩展）─────────────
MSG_TEXT = "text"
MSG_TOOL_CALL = "tool_call"
MSG_TOOL_RESULT = "tool_result"
MSG_FILE_CHANGE = "file_change"
MSG_AGENT_STEP = "agent_step"
MSG_NOTICE = "notice"

# ── 变更动作 ──────────────────────────────────────────────
ACTION_CREATED = "created"
ACTION_MODIFIED = "modified"
ACTION_DELETED = "deleted"


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def dumps(value: Any) -> Optional[str]:
    """把结构化载荷序列化成 JSON 字符串（失败返回 None，绝不抛异常）"""
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        logger.warning("轨迹 metadata 序列化失败，按空处理")
        return None


# ── 变更记录 ──────────────────────────────────────────────
@dataclass
class ChangeRecord:
    change_id: int
    task_id: str
    path: str                       # 相对工作区
    action: str                     # created | modified | deleted
    backup_path: Optional[str] = None
    bytes: int = 0
    diff: Optional[str] = None
    created_at: str = field(default_factory=_now_str)
    before_hash: Optional[str] = None
    after_hash: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "change_id": self.change_id,
            "task_id": self.task_id,
            "path": self.path,
            "action": self.action,
            "backup_path": self.backup_path,
            "bytes": self.bytes,
            "diff": self.diff,
            "created_at": self.created_at,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
        }


@dataclass
class ChangeSet:
    """一个任务内的全部文件变更，支持整体 / 单条撤销"""
    task_id: str
    workspace: str
    records: List[ChangeRecord] = field(default_factory=list)
    _seq: int = 0

    def next_id(self) -> int:
        self._seq += 1
        return self._seq

    def add(self, path: str, action: str, backup_path: Optional[str] = None,
            size: int = 0, diff: Optional[str] = None,
            before_hash: Optional[str] = None, after_hash: Optional[str] = None) -> ChangeRecord:
        record = ChangeRecord(
            change_id=self.next_id(),
            task_id=self.task_id,
            path=path,
            action=action,
            backup_path=backup_path,
            bytes=int(size or 0),
            diff=diff,
            before_hash=before_hash,
            after_hash=after_hash,
        )
        self.records.append(record)
        return record

    def get(self, change_id: int) -> Optional[ChangeRecord]:
        for record in self.records:
            if record.change_id == int(change_id):
                return record
        return None

    def to_list(self) -> List[dict]:
        return [r.to_dict() for r in self.records]

    # ── 撤销 ──────────────────────────────────────────────
    def undo(self, change_id: Optional[int] = None) -> dict:
        """
        逆序回滚变更。

        :param change_id: 指定则只撤销这一条；否则撤销全部
        :return: {"restored": n, "removed": n, "failed": [{"change_id":..,"reason":..}]}
        """
        targets = list(self.records)
        if change_id is not None:
            only = self.get(change_id)
            targets = [only] if only else []
        targets = list(reversed(targets))

        restored = 0
        removed = 0
        failed: List[dict] = []
        done_ids = set()

        for record in targets:
            tmp_path = None
            try:
                ws = Workspace(self.workspace)
                target = ws.resolve(record.path)
                for newer in self.records:
                    if newer.change_id <= record.change_id or newer.change_id in done_ids:
                        continue
                    try:
                        newer_target = ws.resolve(newer.path)
                    except (SandboxError, OSError, ValueError):
                        continue  # 无效记录单独报告，不阻塞其他文件。
                    if newer_target == target:
                        raise ValueError("同一文件存在较新变更，请先撤销较新条目")
                if record.action not in (ACTION_CREATED, ACTION_MODIFIED, ACTION_DELETED):
                    raise ValueError("未知变更类型，无法安全撤销")
                if ((record.action != ACTION_DELETED and not record.after_hash)
                        or (record.action != ACTION_CREATED and not record.before_hash)):
                    raise ValueError("旧变更记录缺少内容校验信息，请手动核对备份后还原，已拒绝自动撤销")
                if record.action == ACTION_DELETED:
                    if target.exists():
                        raise ValueError("文件已被重新创建，已保留当前文件")
                elif not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != record.after_hash:
                    raise ValueError("文件在该次变更后已被修改或删除，已保留当前状态")
                if record.action == ACTION_CREATED:
                    target.unlink()
                    removed += 1
                    done_ids.add(record.change_id)
                    continue

                backup = ws.resolve(record.backup_path) if record.backup_path else None
                if not backup or not backup.is_file():
                    raise ValueError("备份文件缺失，无法还原")
                if hashlib.sha256(backup.read_bytes()).hexdigest() != record.before_hash:
                    raise ValueError("备份内容已改变，已拒绝还原")
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = target.with_name(target.name + ".aegis-tmp-" + uuid.uuid4().hex)
                shutil.copy2(backup, tmp_path)
                os.replace(tmp_path, target)
                restored += 1
                done_ids.add(record.change_id)
            except (ValueError, SandboxError) as e:
                reason = e.message if isinstance(e, SandboxError) else str(e)
                logger.warning("撤销变更失败 change_id=%s：%s", record.change_id, reason)
                failed.append({"change_id": record.change_id, "reason": reason})
            except OSError as e:
                logger.warning("撤销变更失败 change_id=%s：%s", record.change_id, e)
                failed.append({"change_id": record.change_id,
                               "reason": "文件系统错误，请查看日志"})
            finally:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("撤销临时文件清理失败 change_id=%s", record.change_id)

        # 成功撤销的条目从清单里剔除，避免重复撤销
        if done_ids:
            self.records = [r for r in self.records if r.change_id not in done_ids]
        return {"restored": restored, "removed": removed, "failed": failed}


# ── 落库 ──────────────────────────────────────────────────
def _next_seq(session_id) -> int:
    try:
        rows = apps.query(
            "SELECT COALESCE(MAX(sequence_no), 0) AS max_seq "
            "FROM ai_chat_message WHERE session_id = %s",
            (session_id,),
        )
        return (rows[0]["max_seq"] + 1) if rows else 1
    except Exception:
        logger.exception("计算消息序号失败，回退为 0")
        return 0


def save_message(session_id, role: str, content: str,
                 message_type: str = MSG_TEXT, metadata: Any = None) -> Optional[int]:
    """
    保存一条消息（可带结构化 metadata）。

    默认行为与既有 `aichat.services.save_message` 完全一致（message_type='text'）。
    """
    try:
        from apps.aichat import services as aichat_services
        return aichat_services.save_message(
            session_id, role, content,
            message_type=message_type,
            metadata=dumps(metadata),
        )
    except Exception:
        logger.exception("保存会话消息失败 session_id=%s", session_id)
        return None


def persist_change(session_id, record: ChangeRecord,
                   message_content: str = "") -> Optional[int]:
    """
    把一条变更记录落库（`ai_chat_message` + `metadata` JSON）。

    使用 `apps.execute()` 且 SQL 走 `%s` 占位符（内部自动适配 SQLite）。
    """
    seq = _next_seq(session_id)
    payload = record.to_dict()
    text = message_content or ("%s %s" % (record.action, record.path))
    try:
        return apps.execute(
            "INSERT INTO ai_chat_message "
            "(session_id, user_id, message_role, message_type, "
            " message_content, sequence_no, metadata) "
            "VALUES (%s, 1, %s, %s, %s, %s, %s)",
            (session_id, "assistant", MSG_FILE_CHANGE, text, seq, dumps(payload)),
        )
    except Exception:
        # 旧库没有 metadata 列 → 降级为不带 metadata 的写入
        logger.exception("落库变更记录失败（含 metadata），改用旧结构重试")
        try:
            return apps.execute(
                "INSERT INTO ai_chat_message "
                "(session_id, user_id, message_role, message_type, "
                " message_content, sequence_no) "
                "VALUES (%s, 1, %s, %s, %s, %s)",
                (session_id, "assistant", MSG_FILE_CHANGE, text, seq),
            )
        except Exception:
            logger.exception("落库变更记录失败")
            return None


def persist_trace(session_id, role: str, message_type: str,
                  content: str, metadata: Any = None) -> Optional[int]:
    """落库一条非文本轨迹（工具调用 / 工具结果 / 步骤 / 提示）"""
    seq = _next_seq(session_id)
    try:
        return apps.execute(
            "INSERT INTO ai_chat_message "
            "(session_id, user_id, message_role, message_type, "
            " message_content, sequence_no, metadata) "
            "VALUES (%s, 1, %s, %s, %s, %s, %s)",
            (session_id, role, message_type, content, seq, dumps(metadata)),
        )
    except Exception:
        logger.exception("落库 Agent 轨迹失败（含 metadata），改用旧结构重试")
        try:
            return apps.execute(
                "INSERT INTO ai_chat_message "
                "(session_id, user_id, message_role, message_type, "
                " message_content, sequence_no) "
                "VALUES (%s, 1, %s, %s, %s, %s)",
                (session_id, role, message_type, content, seq),
            )
        except Exception:
            logger.exception("落库 Agent 轨迹失败")
            return None
