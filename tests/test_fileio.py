# -*- coding: utf-8 -*-
"""
tests/test_fileio.py — 原子写 / 任务级备份 / 多处匹配必报错

验收点（T03）：
    · `edit_file` 多处匹配 → 必报 `MULTIPLE_MATCH`，且**不修改文件、不产生备份**
    · 写前必有 `.aegis_backup/<task_id>/<rel>`
    · 备份失败 → 绝不裸写
    · 原子写：不留 `.aegis-tmp-*` 残留
"""
import os

import pytest

from apps.agent import fileio
from apps.agent.fileio import BACKUP_ROOT
from apps.agent.sandbox import SandboxError, open_workspace
from apps.agent.schemas import ToolError
from apps.agent.trace import ACTION_CREATED, ACTION_MODIFIED, ChangeSet

TASK_ID = "t-abc123"


def _backup_abs(ws, rel, task_id=TASK_ID):
    return ws.root / BACKUP_ROOT / task_id / rel


# ── edit_file：多处匹配必报错 ──────────────────────────────
def test_edit_multiple_match_raises_and_keeps_file(ws, ws_root):
    rel = "dup.py"
    original = "line A\nmarker\nline B\nmarker\nline C\n"
    (ws_root / rel).write_text(original, encoding="utf-8")
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))

    with pytest.raises(ToolError) as exc:
        fileio.edit_file(ws, cs, rel, "marker", "REPLACED")

    assert exc.value.code == "MULTIPLE_MATCH"
    # 文件必须原封不动
    assert (ws_root / rel).read_text(encoding="utf-8") == original
    # 不得产生任何备份、不得登记任何变更
    assert cs.records == []
    assert not _backup_abs(ws, rel).exists()
    # 错误信息里要给出可用线索（处数 / 行号 / 旧错误码）
    assert (exc.value.detail or {}).get("count") == 2
    assert (exc.value.detail or {}).get("lines")
    assert (exc.value.detail or {}).get("legacy_code") == "MATCH_NOT_UNIQUE"


def test_edit_multiple_match_with_occurrence_ok(ws, ws_root):
    rel = "dup.py"
    (ws_root / rel).write_text("a\nx\nb\nx\n", encoding="utf-8")
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    out = fileio.edit_file(ws, cs, rel, "x", "Y", occurrence=2)
    assert out["action"] == ACTION_MODIFIED
    assert (ws_root / rel).read_text(encoding="utf-8") == "a\nx\nb\nY\n"


def test_edit_no_match_raises(ws, ws_root):
    rel = "app.py"
    before = (ws_root / rel).read_text(encoding="utf-8")
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    with pytest.raises(ToolError) as exc:
        fileio.edit_file(ws, cs, rel, "NOT-EXIST", "z")
    assert exc.value.code == "MATCH_NOT_FOUND"
    assert (ws_root / rel).read_text(encoding="utf-8") == before


def test_edit_occurrence_out_of_range(ws, ws_root):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    with pytest.raises(ToolError) as exc:
        fileio.edit_file(ws, cs, "app.py", "hello", "z", occurrence=9)
    assert exc.value.code == "INVALID_ARGUMENT"


def test_edit_out_of_workspace_rejected(ws, ws_root):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    with pytest.raises(SandboxError) as exc:
        fileio.edit_file(ws, cs, "../outside/secret.txt", "TOP", "X")
    assert exc.value.code == "PATH_OUT_OF_WORKSPACE"


# ── 写前必有备份 ──────────────────────────────────────────
def test_write_creates_backup_before_overwrite(ws, ws_root):
    rel = "app.py"
    original = (ws_root / rel).read_text(encoding="utf-8")
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))

    out = fileio.write_file(ws, cs, rel, "print('changed')\n")

    backup = _backup_abs(ws, rel)
    assert backup.is_file(), "写前备份缺失：%s" % backup
    assert backup.read_text(encoding="utf-8") == original
    assert out["action"] == ACTION_MODIFIED
    assert out["backup_path"] == "%s/%s/%s" % (BACKUP_ROOT, TASK_ID, rel)
    assert out["diff"] and "+print('changed')" in out["diff"]
    assert out["stats"]["added"] >= 1


def test_edit_creates_backup(ws, ws_root):
    rel = "sub/mod.py"
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    out = fileio.edit_file(ws, cs, rel, "X = 1", "X = 2")
    backup = _backup_abs(ws, rel)
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == "X = 1\n"
    assert out["backup_path"] == "%s/%s/%s" % (BACKUP_ROOT, TASK_ID, rel)


def test_new_file_action_created_no_backup(ws, ws_root):
    """新建文件：action=created，撤销靠删除，因此无需备份"""
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    out = fileio.write_file(ws, cs, "new/brand.py", "print(1)\n")
    assert out["action"] == ACTION_CREATED
    assert (ws_root / "new" / "brand.py").read_text(encoding="utf-8") == "print(1)\n"


def test_write_out_of_workspace_rejected(ws):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    with pytest.raises(SandboxError) as exc:
        fileio.write_file(ws, cs, "../outside/evil.txt", "pwned")
    assert exc.value.code == "PATH_OUT_OF_WORKSPACE"


# ── 备份失败 → 绝不裸写 ───────────────────────────────────
def test_write_aborted_when_backup_fails(ws, ws_root, monkeypatch):
    rel = "app.py"
    original = (ws_root / rel).read_text(encoding="utf-8")
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))

    def boom(*a, **kw):
        raise ToolError("BACKUP_FAILED", "备份失败（模拟）")

    monkeypatch.setattr(fileio, "backup_file", boom)
    with pytest.raises(ToolError) as exc:
        fileio.write_file(ws, cs, rel, "MUST NOT BE WRITTEN")
    assert exc.value.code == "BACKUP_FAILED"
    assert (ws_root / rel).read_text(encoding="utf-8") == original, "备份失败却写入了文件！"


def test_edit_aborted_when_backup_fails(ws, ws_root, monkeypatch):
    """app.py 里 'hello' 出现两次，这里用 sub/mod.py 保证唯一匹配"""
    rel = "sub/mod.py"
    original = (ws_root / rel).read_text(encoding="utf-8")
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))

    monkeypatch.setattr(fileio, "backup_file",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            ToolError("BACKUP_FAILED", "备份失败（模拟）")))
    with pytest.raises(ToolError) as exc:
        fileio.edit_file(ws, cs, rel, "X = 1", "X = 42")
    assert exc.value.code == "BACKUP_FAILED"
    assert (ws_root / rel).read_text(encoding="utf-8") == original, "备份失败却写入了文件！"


def test_backup_failure_on_io_error(ws, ws_root):
    """
    真实 OSError（备份位被目录占用）必须升级成 BACKUP_FAILED，而不是裸写。
    """
    rel = "sub/mod.py"
    original = (ws_root / rel).read_text(encoding="utf-8")
    # 把备份落点变成一个目录 → open(..., 'wb') 抛 IsADirectoryError(OSError)
    (_backup_abs(ws, rel)).mkdir(parents=True, exist_ok=True)
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))

    with pytest.raises(ToolError) as exc:
        fileio.backup_file(ws, TASK_ID, rel)
    assert exc.value.code == "BACKUP_FAILED"
    assert (ws_root / rel).read_text(encoding="utf-8") == original


# ── 原子写：不留临时文件 ───────────────────────────────────
def test_atomic_write_leaves_no_tmp(ws, ws_root):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    fileio.write_file(ws, cs, "atomic.py", "ok\n")
    leftovers = [p.name for p in (ws_root).iterdir() if ".aegis-tmp" in p.name]
    assert leftovers == [], "临时文件未清理：%s" % leftovers


# ── 读取 ──────────────────────────────────────────────────
def test_read_file_with_line_numbers(ws):
    out = fileio.read_text_file(ws, "app.py")
    assert out["total_lines"] == 2
    assert out["content"].startswith("1\t")
    assert out["truncated"] is False


def test_read_file_range_and_truncation(ws, ws_root):
    (ws_root / "big.py").write_text("\n".join("l%d" % i for i in range(1, 51)),
                                    encoding="utf-8")
    out = fileio.read_text_file(ws, "big.py", start_line=5, end_line=10)
    assert out["start_line"] == 5 and out["end_line"] == 10

    out2 = fileio.read_text_file(ws, "big.py", max_lines=3)
    assert out2["truncated"] is True
    assert out2["end_line"] == 3


def test_read_missing_file(ws):
    with pytest.raises(ToolError) as exc:
        fileio.read_text_file(ws, "nope.py")
    assert exc.value.code == "FILE_NOT_FOUND"


def test_read_dir_as_file(ws):
    with pytest.raises(ToolError) as exc:
        fileio.read_text_file(ws, "sub")
    assert exc.value.code == "NOT_A_FILE"


def test_read_binary_file(ws, ws_root):
    (ws_root / "b.bin").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ToolError) as exc:
        fileio.read_text_file(ws, "b.bin")
    assert exc.value.code == "ENCODE_ERROR"


def test_read_too_large(ws, ws_root, monkeypatch):
    monkeypatch.setattr(fileio, "MAX_FILE_BYTES", 10)
    with pytest.raises(ToolError) as exc:
        fileio.read_text_file(ws, "app.py")
    assert exc.value.code == "TOO_LARGE"


# ── 预览（不落盘）─────────────────────────────────────────
def test_preview_edit_does_not_touch_disk(ws, ws_root):
    rel = "sub/mod.py"
    before = (ws_root / rel).read_text(encoding="utf-8")
    out = fileio.preview_edit(ws, rel, "X = 1", "X = 99")
    assert out["ok"] is True and out["count"] == 1
    assert (ws_root / rel).read_text(encoding="utf-8") == before


def test_preview_edit_multiple_match(ws, ws_root):
    (ws_root / "dup.py").write_text("x\nx\n", encoding="utf-8")
    out = fileio.preview_edit(ws, "dup.py", "x", "y")
    assert out["ok"] is False
    assert out["error"]["code"] == "MULTIPLE_MATCH"


def test_preview_edit_out_of_workspace(ws):
    out = fileio.preview_edit(ws, "../outside/secret.txt", "TOP", "X")
    assert out["ok"] is False
    assert out["error"]["code"] == "PATH_OUT_OF_WORKSPACE"


# ── diff ──────────────────────────────────────────────────
def test_diff_stats():
    d = fileio.make_diff("a\nb\n", "a\nc\n", "x.py")
    s = fileio.diff_stats(d)
    assert s == {"added": 1, "removed": 1}


# ── 撤销 ──────────────────────────────────────────────────
def test_undo_restores_from_backup(ws, ws_root):
    rel = "app.py"
    original = (ws_root / rel).read_text(encoding="utf-8")
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    fileio.write_file(ws, cs, rel, "CHANGED\n")
    assert (ws_root / rel).read_text(encoding="utf-8") == "CHANGED\n"
    out = cs.undo()
    assert out == {"restored": 1, "removed": 0, "failed": []}
    assert (ws_root / rel).read_text(encoding="utf-8") == original


def test_undo_created_removes_file(ws, ws_root):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    fileio.write_file(ws, cs, "tmp_new.py", "x")
    assert (ws_root / "tmp_new.py").exists()
    out = cs.undo()
    assert out["removed"] == 1
    assert not (ws_root / "tmp_new.py").exists()


def test_repeated_write_keeps_each_backup_and_undo_restores_original(ws, ws_root):
    original = (ws_root / "app.py").read_bytes()
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    first = fileio.write_file(ws, cs, "app.py", "first\n")
    second = fileio.edit_file(ws, cs, "app.py", "first", "second")
    assert first["backup_path"] != second["backup_path"]
    assert (ws_root / first["backup_path"]).read_bytes() == original
    assert (ws_root / second["backup_path"]).read_bytes() == b"first\n"
    assert cs.undo() == {"restored": 2, "removed": 0, "failed": []}
    assert (ws_root / "app.py").read_bytes() == original
    assert cs.records == []


def test_each_write_can_be_undone_then_created_file_removed(ws, ws_root):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    first = fileio.write_file(ws, cs, "new.py", "first\n")
    second = fileio.write_file(ws, cs, "new.py", "second\n")
    assert cs.undo(second["change_id"]) == {"restored": 1, "removed": 0, "failed": []}
    assert (ws_root / "new.py").read_bytes() == b"first\n"
    assert cs.undo(first["change_id"]) == {"restored": 0, "removed": 1, "failed": []}
    assert not (ws_root / "new.py").exists()


@pytest.mark.parametrize("rel", ["app.py", "created.py"])
def test_undo_preserves_later_manual_edits(ws, ws_root, rel):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    change = fileio.write_file(ws, cs, rel, "agent\n")
    (ws_root / rel).write_bytes(b"manual\n")
    out = cs.undo()
    assert out["restored"] == out["removed"] == 0
    assert [f["change_id"] for f in out["failed"]] == [change["change_id"]]
    assert (ws_root / rel).read_bytes() == b"manual\n"
    assert len(cs.records) == 1


def test_undo_older_change_rejected_even_if_contents_match(ws, ws_root):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    first = fileio.write_file(ws, cs, "app.py", "same\n")
    fileio.write_file(ws, cs, "app.py", "same\n")
    out = cs.undo(first["change_id"])
    assert len(out["failed"]) == 1
    assert (ws_root / "app.py").read_bytes() == b"same\n"
    assert len(cs.records) == 2


def test_partial_undo_removes_successful_records_and_keeps_blocked_chain(ws, ws_root):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    first = fileio.write_file(ws, cs, "app.py", "first\n")
    second = fileio.write_file(ws, cs, "app.py", "second\n")
    fileio.write_file(ws, cs, "created.py", "created\n")
    (ws_root / "app.py").write_bytes(b"manual\n")
    out = cs.undo()
    assert out["removed"] == 1 and out["restored"] == 0
    assert {f["change_id"] for f in out["failed"]} == {first["change_id"], second["change_id"]}
    assert [r.change_id for r in cs.records] == [first["change_id"], second["change_id"]]
    assert (ws_root / "app.py").read_bytes() == b"manual\n"
    assert not (ws_root / "created.py").exists()
    (ws_root / "app.py").write_bytes(b"second\n")
    assert cs.undo() == {"restored": 2, "removed": 0, "failed": []}


@pytest.mark.parametrize("action", [ACTION_CREATED, ACTION_MODIFIED])
def test_legacy_records_without_hashes_are_not_automatically_undone(ws, ws_root, action):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    backup = ws_root / ".aegis_backup" / TASK_ID / "app.py"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"old\n")
    cs.add("app.py", action, ws.rel_path(backup))
    before = (ws_root / "app.py").read_bytes()
    out = cs.undo()
    assert len(out["failed"]) == 1
    assert (ws_root / "app.py").read_bytes() == before
    assert len(cs.records) == 1


def test_undo_rejects_corrupted_backup(ws, ws_root):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    change = fileio.write_file(ws, cs, "app.py", "agent\n")
    (ws_root / change["backup_path"]).write_bytes(b"corrupted\n")
    out = cs.undo()
    assert len(out["failed"]) == 1
    assert (ws_root / "app.py").read_bytes() == b"agent\n"


@pytest.mark.parametrize("field", ["path", "backup_path"])
def test_undo_revalidates_record_paths(ws, ws_root, outside_root, field):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    fileio.write_file(ws, cs, "app.py", "agent\n")
    setattr(cs.records[0], field, "../outside/secret.txt")
    before = (outside_root / "secret.txt").read_bytes()
    out = cs.undo()
    assert len(out["failed"]) == 1
    assert (outside_root / "secret.txt").read_bytes() == before
    assert (ws_root / "app.py").read_bytes() == b"agent\n"


@pytest.mark.parametrize("task_id", ["../outside", "a/../../outside", ".."])
def test_backup_rejects_task_id_path_traversal(ws, ws_root, outside_root, task_id):
    before = (ws_root / "app.py").read_bytes()
    with pytest.raises(ToolError) as exc:
        fileio.backup_file(ws, task_id, "app.py")
    assert exc.value.code == "BACKUP_FAILED"
    assert (ws_root / "app.py").read_bytes() == before
    assert not (outside_root / "app.py").exists()


def test_undo_restore_failure_keeps_target_and_record(ws, ws_root, monkeypatch):
    import apps.agent.trace as trace
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    fileio.write_file(ws, cs, "app.py", "agent\n")
    def interrupted_copy(source, destination):
        with open(destination, "wb") as output:
            output.write(b"partial")
        raise OSError("simulated copy failure")
    monkeypatch.setattr(trace.shutil, "copy2", interrupted_copy)
    out = cs.undo()
    assert len(out["failed"]) == 1
    assert (ws_root / "app.py").read_bytes() == b"agent\n"
    assert len(cs.records) == 1
    assert not list(ws_root.glob("*.aegis-tmp-*"))


def test_undo_stops_at_manual_edit_between_agent_writes(ws, ws_root):
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    first = fileio.write_file(ws, cs, "app.py", "first\n")
    (ws_root / "app.py").write_bytes(b"manual\n")
    fileio.write_file(ws, cs, "app.py", "second\n")
    out = cs.undo()
    assert out["restored"] == 1
    assert [f["change_id"] for f in out["failed"]] == [first["change_id"]]
    assert (ws_root / "app.py").read_bytes() == b"manual\n"
    assert [r.change_id for r in cs.records] == [first["change_id"]]


def test_backup_rejects_symlink_outside_workspace(ws, ws_root, outside_root):
    try:
        (ws_root / BACKUP_ROOT).symlink_to(outside_root, target_is_directory=True)
    except OSError:
        pytest.skip("当前用户无创建符号链接权限")
    with pytest.raises(ToolError) as exc:
        fileio.backup_file(ws, TASK_ID, "app.py")
    assert exc.value.code == "BACKUP_FAILED"
    assert not (outside_root / TASK_ID).exists()


def test_invalid_record_does_not_block_undo_of_another_file(ws, ws_root):
    original = (ws_root / "app.py").read_bytes()
    cs = ChangeSet(task_id=TASK_ID, workspace=str(ws.root))
    fileio.write_file(ws, cs, "app.py", "agent\n")
    invalid = fileio.write_file(ws, cs, "other.py", "other\n")
    cs.records[-1].path = "../outside/secret.txt"
    out = cs.undo()
    assert out["restored"] == 1
    assert [f["change_id"] for f in out["failed"]] == [invalid["change_id"]]
    assert (ws_root / "app.py").read_bytes() == original
    assert [r.change_id for r in cs.records] == [invalid["change_id"]]
