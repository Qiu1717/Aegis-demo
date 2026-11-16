# -*- coding: utf-8 -*-
"""
apps/agent/fileio.py — 文件读写：原子写 + 任务级备份 + unified diff

写文件的**唯一**合法路径（设计 §4.4.3）：

    1. 沙箱校验路径（越界直接抛 SandboxError）
    2. 文件已存在 → 复制到 .aegis_backup/<task_id>/<rel>（后续写入独立备份）
       文件不存在 → 记 action=created（撤销时删除）
    3. 写同目录临时文件 <name>.aegis-tmp-<rand>
    4. os.replace(tmp, target)          # 原子替换，避免半截文件
    5. 记 ChangeRecord 到 ChangeSet（并落库）
    6. 生成 unified diff（difflib，context=3）

**备份失败 → 中止写入（`BACKUP_FAILED`），绝不裸写。**
"""
import difflib
import hashlib
import os
import uuid
from pathlib import Path
from typing import Optional, Tuple

from .sandbox import MAX_FILE_BYTES, SandboxError, Workspace
from .schemas import (ERR_MATCH_NOT_UNIQUE, ERR_MULTIPLE_MATCH, ToolError)
from .trace import ACTION_CREATED, ACTION_MODIFIED, ChangeSet

#: 备份根目录（相对工作区）
BACKUP_ROOT = ".aegis_backup"

#: 单文件读取的行数上限（超过自动截断并置 truncated=true）
DEFAULT_MAX_LINES = 2000


# ── 读取 ──────────────────────────────────────────────────
def read_text_file(ws: Workspace, rel: str, start_line: Optional[int] = None,
                   end_line: Optional[int] = None,
                   max_lines: int = DEFAULT_MAX_LINES) -> dict:
    """
    读取工作区内的文本文件（带行号）。

    :raises SandboxError: 路径越界
    :raises ToolError: FILE_NOT_FOUND / NOT_A_FILE / TOO_LARGE / ENCODE_ERROR
    """
    target = ws.resolve(rel)
    if not target.exists():
        raise ToolError("FILE_NOT_FOUND", "文件不存在：%s" % rel)
    if not target.is_file():
        raise ToolError("NOT_A_FILE", "该路径不是文件：%s" % rel)

    try:
        size = target.stat().st_size
    except OSError:
        size = 0
    if size > MAX_FILE_BYTES:
        raise ToolError("TOO_LARGE",
                        "文件过大（%.1f MB），已拒绝读取，请改用 search_code"
                        % (size / 1024 / 1024))

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError("ENCODE_ERROR", "文件不是 UTF-8 文本（可能是二进制文件）：%s" % rel)
    except OSError:
        raise ToolError("ENCODE_ERROR", "文件读取失败：%s" % rel)

    lines = text.splitlines()
    total = len(lines)

    start = 1
    if start_line is not None:
        try:
            start = max(1, int(start_line))
        except (TypeError, ValueError):
            start = 1
    end = total
    if end_line is not None:
        try:
            end = min(total, max(start, int(end_line)))
        except (TypeError, ValueError):
            end = total
    if start > total:
        start, end = total, total

    truncated = False
    if (end - start + 1) > max_lines:
        end = start + max_lines - 1
        truncated = True

    numbered = ["%d\t%s" % (i, lines[i - 1]) for i in range(start, end + 1)]
    return {
        "path": ws.rel_path(target),
        "total_lines": total,
        "start_line": start,
        "end_line": end,
        "truncated": truncated,
        "content": "\n".join(numbered),
    }


# ── 备份 ──────────────────────────────────────────────────
def backup_rel_path(task_id: str, rel: str) -> str:
    """备份文件相对工作区的路径"""
    clean = str(rel).replace("\\", "/").lstrip("/")
    return "%s/%s/%s" % (BACKUP_ROOT, task_id, clean)


def backup_file(ws: Workspace, task_id: str, rel: str) -> Tuple[str, str]:
    """
    备份一个即将被写入的文件。

    :return: (backup_path 相对路径 或 "", action)
    :raises ToolError: BACKUP_FAILED（备份失败即中止写入）
    """
    target = ws.resolve(rel)
    clean_rel = ws.rel_path(target)

    if not target.exists():
        return "", ACTION_CREATED

    try:
        task = str(task_id)
        if not task or task in (".", "..") or any(c in task for c in "/\\:"):
            raise ValueError("任务 ID 不能包含路径")
        backup_abs = ws.resolve(backup_rel_path(task, clean_rel))
        backup_abs.parent.mkdir(parents=True, exist_ok=True)
        with open(str(target), "rb") as src:
            data = src.read()
        try:
            dst = open(str(backup_abs), "xb")
        except FileExistsError:
            if not backup_abs.is_file():
                raise OSError("备份落点不是文件")
            # 首份路径保持兼容；后续快照独立保存，绝不覆盖旧快照。
            backup_abs = ws.resolve(str(backup_abs) + "." + uuid.uuid4().hex)
            dst = open(str(backup_abs), "xb")
        with dst:
            dst.write(data)
    except (OSError, ValueError, SandboxError):
        raise ToolError("BACKUP_FAILED",
                        "备份失败，已中止写入（绝不裸写）：%s" % clean_rel)

    return ws.rel_path(backup_abs), ACTION_MODIFIED


# ── 原子写 ────────────────────────────────────────────────
def atomic_write(target: Path, content: str) -> None:
    """同目录临时文件 + os.replace 原子替换"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = "%s.aegis-tmp-%s" % (target.name, uuid.uuid4().hex[:8])
    tmp_path = target.parent / tmp_name
    try:
        with open(str(tmp_path), "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(str(tmp_path), str(target))
    except OSError:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise ToolError("WRITE_FAILED", "写入文件失败：%s" % target.name)


# ── diff ──────────────────────────────────────────────────
def make_diff(old_text: str, new_text: str, rel: str, context: int = 3) -> str:
    """生成 unified diff（上下文 3 行）"""
    old_lines = (old_text or "").splitlines(keepends=True)
    new_lines = (new_text or "").splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile="a/%s" % rel, tofile="b/%s" % rel, n=context,
    )
    return "".join(diff)


def diff_stats(diff: str) -> dict:
    """统计 diff 的增删行数"""
    added = 0
    removed = 0
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"added": added, "removed": removed}


# ── 写入 ──────────────────────────────────────────────────
def write_file(ws: Workspace, changeset: ChangeSet, rel: str, content: str,
               create_dirs: bool = True) -> dict:
    """
    新建或整文件覆盖写入。

    :return: 工具返回体里的 data 段
    """
    target = ws.resolve(rel)
    if target.exists() and target.is_dir():
        raise ToolError("NOT_A_FILE", "该路径是一个目录，不能写入：%s" % rel)

    old_text = ""
    if target.exists():
        try:
            old_text = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            old_text = ""

    backup_rel, action = backup_file(ws, changeset.task_id, rel)
    before_hash = hashlib.sha256(ws.resolve(backup_rel).read_bytes()).hexdigest() if backup_rel else None

    if not create_dirs and not target.parent.exists():
        raise ToolError("INVALID_ARGUMENT",
                        "父目录不存在且未允许自动创建：%s" % rel)

    atomic_write(target, content or "")

    try:
        size = target.stat().st_size
    except OSError:
        size = len((content or "").encode("utf-8"))

    clean_rel = ws.rel_path(target)
    diff = make_diff(old_text, content or "", clean_rel) if old_text else None
    record = changeset.add(
        path=clean_rel,
        action=action,
        backup_path=backup_rel or None,
        size=size,
        diff=diff,
        before_hash=before_hash,
        after_hash=hashlib.sha256((content or "").encode("utf-8")).hexdigest(),
    )
    return {
        "path": clean_rel,
        "action": action,
        "bytes": size,
        "backup_path": backup_rel or None,
        "diff": diff,
        "stats": diff_stats(diff) if diff else {"added": 0, "removed": 0},
        "change_id": record.change_id,
    }


def edit_file(ws: Workspace, changeset: ChangeSet, rel: str, old_text: str,
              new_text: str, occurrence: Optional[int] = None) -> dict:
    """
    精确字符串替换。

    **old_text 匹配到多处且未指定 occurrence 时必须报错 `MATCH_NOT_UNIQUE`，
    绝不擅自替换**（AG-03②）。
    """
    target = ws.resolve(rel)
    if not target.exists():
        raise ToolError("FILE_NOT_FOUND", "文件不存在：%s" % rel)
    if not target.is_file():
        raise ToolError("NOT_A_FILE", "该路径不是文件：%s" % rel)

    if not old_text:
        raise ToolError("INVALID_ARGUMENT", "old_text 不能为空")

    try:
        original = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError("ENCODE_ERROR", "文件不是 UTF-8 文本：%s" % rel)
    except OSError:
        raise ToolError("ENCODE_ERROR", "文件读取失败：%s" % rel)

    count = original.count(old_text)
    if count == 0:
        raise ToolError("MATCH_NOT_FOUND", "old_text 在文件中未找到，请检查缩进与换行是否一致")

    if count > 1 and occurrence is None:
        lines = _match_lines(original, old_text)
        raise ToolError(
            ERR_MULTIPLE_MATCH,
            "old_text 在文件中匹配到 %d 处，已拒绝修改。请补充更多上下文使其唯一，"
            "或指定 occurrence。" % count,
            detail={"count": count, "lines": lines[:10],
                    "legacy_code": ERR_MATCH_NOT_UNIQUE},
        )

    index = 0
    if occurrence is not None:
        try:
            index = int(occurrence) - 1
        except (TypeError, ValueError):
            index = 0
        if index < 0 or index >= count:
            raise ToolError("INVALID_ARGUMENT",
                            "occurrence 超出范围：共匹配 %d 处" % count)

    updated = _replace_nth(original, old_text, new_text, index)

    backup_rel, action = backup_file(ws, changeset.task_id, rel)
    before_hash = hashlib.sha256(ws.resolve(backup_rel).read_bytes()).hexdigest() if backup_rel else None
    atomic_write(target, updated)

    try:
        size = target.stat().st_size
    except OSError:
        size = len(updated.encode("utf-8"))

    clean_rel = ws.rel_path(target)
    diff = make_diff(original, updated, clean_rel)
    record = changeset.add(
        path=clean_rel,
        action=action,
        backup_path=backup_rel or None,
        size=size,
        diff=diff,
        before_hash=before_hash,
        after_hash=hashlib.sha256(updated.encode("utf-8")).hexdigest(),
    )
    return {
        "path": clean_rel,
        "action": action,
        "backup_path": backup_rel or None,
        "diff": diff,
        "stats": diff_stats(diff),
        "bytes": size,
        "change_id": record.change_id,
    }


def preview_edit(ws: Workspace, rel: str, old_text: str, new_text: str,
                 occurrence: Optional[int] = None) -> dict:
    """
    预演一次 edit_file（**不落盘**），用于审批卡片展示 diff。

    :return: {"ok": True, "diff":..., "stats":..., "count": n}
             或 {"ok": False, "error": {...}}
    """
    try:
        target = ws.resolve(rel)
        if not target.exists():
            return {"ok": False, "error": {"code": "FILE_NOT_FOUND",
                                           "message": "文件不存在：%s" % rel}}
        if not target.is_file():
            return {"ok": False, "error": {"code": "NOT_A_FILE",
                                           "message": "该路径不是文件：%s" % rel}}
        original = target.read_text(encoding="utf-8")
    except SandboxError as e:
        return {"ok": False, "error": e.to_dict()}
    except UnicodeDecodeError:
        return {"ok": False, "error": {"code": "ENCODE_ERROR",
                                       "message": "文件不是 UTF-8 文本：%s" % rel}}
    except OSError:
        return {"ok": False, "error": {"code": "ENCODE_ERROR",
                                       "message": "文件读取失败：%s" % rel}}

    if not old_text:
        return {"ok": False, "error": {"code": "INVALID_ARGUMENT",
                                       "message": "old_text 不能为空"}}

    count = original.count(old_text)
    if count == 0:
        return {"ok": False, "error": {"code": "MATCH_NOT_FOUND",
                                       "message": "old_text 在文件中未找到"}}
    if count > 1 and occurrence is None:
        return {"ok": False,
                "error": {"code": ERR_MULTIPLE_MATCH,
                          "message": "old_text 匹配到 %d 处，需要更多上下文" % count,
                          "detail": {"count": count,
                                     "lines": _match_lines(original, old_text)[:10],
                                     "legacy_code": ERR_MATCH_NOT_UNIQUE}}}

    index = 0
    if occurrence is not None:
        try:
            index = int(occurrence) - 1
        except (TypeError, ValueError):
            index = 0
        if index < 0 or index >= count:
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT",
                                           "message": "occurrence 超出范围"}}

    updated = _replace_nth(original, old_text, new_text, index)
    diff = make_diff(original, updated, ws.rel_path(target))
    return {"ok": True, "diff": diff, "stats": diff_stats(diff), "count": count}


def preview_write(ws: Workspace, rel: str, content: str) -> dict:
    """预演一次 write_file（不落盘），用于审批卡片展示 diff"""
    try:
        target = ws.resolve(rel)
    except SandboxError as e:
        return {"ok": False, "error": e.to_dict()}

    original = ""
    if target.exists() and target.is_file():
        try:
            original = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            original = ""
    diff = make_diff(original, content or "", ws.rel_path(target)) if original else None
    return {
        "ok": True,
        "diff": diff,
        "stats": diff_stats(diff) if diff else {"added": 0, "removed": 0},
        "action": ACTION_MODIFIED if original else ACTION_CREATED,
    }


# ── 内部小工具 ────────────────────────────────────────────
def _match_lines(text: str, needle: str) -> list:
    """返回 needle 在 text 中每次出现的行号（1 基）"""
    lines: list = []
    pos = text.find(needle)
    while pos != -1:
        lines.append(text.count("\n", 0, pos) + 1)
        pos = text.find(needle, pos + 1)
        if len(lines) >= 20:
            break
    return lines


def _replace_nth(text: str, old: str, new: str, index: int) -> str:
    """只替换第 index 次（0 基）出现的位置"""
    if index <= 0:
        return text.replace(old, new, 1)
    pos = -1
    for _ in range(index + 1):
        pos = text.find(old, pos + 1)
        if pos == -1:
            return text
    return text[:pos] + new + text[pos + len(old):]
