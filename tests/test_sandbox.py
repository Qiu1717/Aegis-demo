# -*- coding: utf-8 -*-
"""
tests/test_sandbox.py — 工作区路径沙箱（安全模型第②道闸）

验收点（T03）：**越界路径 100% 拒绝**
    · `../` / `..\\` 相对逃逸
    · 指向工作区外的绝对路径
    · 符号链接逃逸（软链接指向工作区外）
    · Windows 大小写 / 盘符差异不得绕过
    · 工作区本身必须存在且是目录（不做默认猜测）
"""
import os
import sys

import pytest

from apps.agent.sandbox import SandboxError, Workspace, open_workspace


# ── 越界：相对路径逃逸 ────────────────────────────────────
@pytest.mark.parametrize("rel", [
    "../outside/secret.txt",
    "..\\outside\\secret.txt",
    "../secret.txt",
    "sub/../../outside/secret.txt",
    "sub/../../../outside/deep/secret2.txt",
    "./../outside/secret.txt",
    "..",
    "../",
])
def test_relative_escape_rejected(ws, rel):
    with pytest.raises(SandboxError) as exc:
        ws.resolve(rel)
    assert exc.value.code == "PATH_OUT_OF_WORKSPACE"


# ── 越界：绝对路径 ────────────────────────────────────────
def test_absolute_outside_rejected(ws, outside_root):
    target = os.path.join(str(outside_root), "secret.txt")
    with pytest.raises(SandboxError) as exc:
        ws.resolve(target)
    assert exc.value.code == "PATH_OUT_OF_WORKSPACE"


def test_absolute_outside_deep_rejected(ws, outside_root):
    target = os.path.join(str(outside_root), "deep", "secret2.txt")
    with pytest.raises(SandboxError) as exc:
        ws.resolve(target)
    assert exc.value.code == "PATH_OUT_OF_WORKSPACE"


def test_foreign_drive_path_rejected(ws):
    """盘符差异：另一个驱动器的路径必须被拒"""
    for raw in ("D:/other/secret.txt", "D:\\other\\secret.txt", "Z:/x/y.txt"):
        with pytest.raises(SandboxError) as exc:
            ws.resolve(raw)
        assert exc.value.code == "PATH_OUT_OF_WORKSPACE", raw


def test_posix_root_path_rejected(ws):
    for raw in ("/etc/passwd", "/tmp/evil.py"):
        with pytest.raises(SandboxError) as exc:
            ws.resolve(raw)
        assert exc.value.code == "PATH_OUT_OF_WORKSPACE", raw


# ── 越界：符号链接逃逸 ────────────────────────────────────
def _try_symlink(link_path, target):
    try:
        os.symlink(str(target), str(link_path))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("当前环境不支持创建符号链接（Windows 需开发者模式/管理员）")
    return link_path


def test_symlink_file_escape_rejected(ws, ws_root, outside_root):
    _try_symlink(ws_root / "link.txt", outside_root / "secret.txt")
    with pytest.raises(SandboxError) as exc:
        ws.resolve("link.txt")
    assert exc.value.code == "PATH_OUT_OF_WORKSPACE"


def test_symlink_dir_escape_rejected(ws, ws_root, outside_root):
    _try_symlink(ws_root / "linkdir", outside_root)
    with pytest.raises(SandboxError) as exc:
        ws.resolve("linkdir/secret.txt")
    assert exc.value.code == "PATH_OUT_OF_WORKSPACE"


# ── Windows 大小写差异：只能命中工作区内，不得逃逸 ────────────
def test_case_variation_stays_inside(ws, ws_root):
    resolved = ws.resolve("SUB/MOD.PY")
    assert ws.is_inside(resolved)
    # 归一化后必须能回到工作区内的相对路径
    assert ws.rel_path(resolved).lower().replace("\\", "/") == "sub/mod.py"


def test_normcase_comparison(ws):
    """is_inside 对大小写差异的路径同样判定为「在内」"""
    variant = os.path.join(str(ws.root), "SUB")
    assert ws.is_inside(variant) is True


def test_sibling_dir_with_prefix_name_not_inside(tmp_path):
    """`/ws2` 不能以 `/ws` 为前缀被误判为「在内」"""
    root = tmp_path / "ws"
    root.mkdir()
    sibling = tmp_path / "ws2"
    sibling.mkdir()
    w = open_workspace(str(root))
    assert w.is_inside(sibling) is False
    assert w.is_inside(root / "child") is True


# ── 正常路径必须放行 ──────────────────────────────────────
def test_inside_paths_allowed(ws, ws_root):
    assert ws.resolve(".") == ws.root
    assert ws.resolve("") == ws.root
    assert ws.resolve("app.py") == ws.root / "app.py"
    assert ws.resolve("sub/mod.py") == ws.root / "sub" / "mod.py"
    assert ws.resolve("sub/../app.py") == ws.root / "app.py"


def test_absolute_inside_allowed(ws, ws_root):
    """工作区内的绝对路径应当被接受"""
    p = ws.resolve(str(ws_root / "sub" / "mod.py"))
    assert p == ws.root / "sub" / "mod.py"


# ── 工作区本身校验（不做默认猜测）──────────────────────────
def test_empty_workspace_rejected():
    for raw in ("", "   ", None):
        with pytest.raises(SandboxError) as exc:
            open_workspace(raw)
        assert exc.value.code == "WORKSPACE_EMPTY", repr(raw)


def test_missing_workspace_rejected(tmp_path):
    with pytest.raises(SandboxError) as exc:
        open_workspace(str(tmp_path / "nope"))
    assert exc.value.code == "WORKSPACE_NOT_FOUND"


def test_file_as_workspace_rejected(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(SandboxError) as exc:
        open_workspace(str(f))
    assert exc.value.code == "WORKSPACE_NOT_DIR"


def test_workspace_strips_quotes(tmp_path):
    w = open_workspace('"%s"' % tmp_path)
    assert w.root == tmp_path.resolve()


# ── 忽略规则 ──────────────────────────────────────────────
@pytest.mark.parametrize("name", [
    ".git/config", "node_modules/x/y.js", "__pycache__/a.pyc",
    ".aegis_backup/t-1/app.py", ".venv/lib/site.py", "dist/app.exe",
])
def test_ignored_paths(ws, name):
    assert ws.is_ignored(ws.root / name) is True


def test_normal_file_not_ignored(ws):
    assert ws.is_ignored(ws.root / "app.py") is False


def test_walk_excludes_ignored(ws_root, ws):
    (ws_root / ".git").mkdir()
    (ws_root / ".git" / "HEAD").write_text("ref: x", encoding="utf-8")
    (ws_root / "node_modules").mkdir()
    entries, tree, truncated = ws.walk(".", max_depth=2)
    names = [e["name"] for e in entries]
    assert not any(n.startswith(".git") for n in names)
    assert not any(n.startswith("node_modules") for n in names)
    assert any(n.endswith("app.py") for n in names)


def test_iter_files_skips_backup(ws_root, ws):
    (ws_root / ".aegis_backup" / "t-1").mkdir(parents=True)
    (ws_root / ".aegis_backup" / "t-1" / "app.py").write_text("old", encoding="utf-8")
    found = [p.name for p in ws.iter_files(".")]
    assert "app.py" in found
    assert all(".aegis_backup" not in str(p) for p in ws.iter_files("."))
