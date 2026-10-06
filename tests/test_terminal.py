# -*- coding: utf-8 -*-
"""
tests/test_terminal.py — 子进程命令执行（安全模型第③道闸的执行侧）

验收点（T03）：
    · 单条命令超时 120s（默认），可配 1–600
    · stdout / stderr 各截断 20000 字符并置 truncated=true
    · cwd 恒为工作区根目录，`cd` 前缀被拒
    · 命令中出现工作区之外的绝对路径 → 直接拒绝
    · 用户可中断（cancel_event）→ 杀进程，不得悬挂
"""
import os
import sys
import threading
import time

import pytest

from apps.agent.schemas import ToolError
from apps.agent.terminal import (DEFAULT_OUTPUT_LIMIT, DEFAULT_TIMEOUT,
                                 run_command)

#: 注意：不能写 python 的**绝对路径**（那会被越界检测拦下——这是正确行为），
#: 这里用 PATH 上的 `py` 启动器，等价于模型输出的普通命令。
PY = "py -3.10" if os.name == "nt" else sys.executable
IS_WIN = os.name == "nt"


def _py(code):
    return '%s -c "%s"' % (PY, code)


# ── 基本执行 ──────────────────────────────────────────────
def test_run_simple_command(ws):
    out = run_command(ws, _py("print('hi')"), timeout=30)
    assert out["exit_code"] == 0
    assert "hi" in out["stdout"]
    assert out["cwd"] == str(ws.root)
    assert out["truncated"] is False


def test_cwd_is_workspace_root(ws, ws_root):
    out = run_command(ws, _py("import os;print(os.getcwd())"), timeout=30)
    assert os.path.realpath(out["stdout"].strip()) == os.path.realpath(str(ws.root))


def test_nonzero_exit_code(ws):
    out = run_command(ws, _py("import sys;sys.exit(3)"), timeout=30)
    assert out["exit_code"] == 3


def test_stderr_captured(ws):
    out = run_command(ws, _py("import sys;sys.stderr.write('oops')"), timeout=30)
    assert "oops" in out["stderr"]


# ── 默认与边界 ────────────────────────────────────────────
def test_defaults_match_design():
    assert DEFAULT_TIMEOUT == 120
    assert DEFAULT_OUTPUT_LIMIT == 20000


def test_timeout_kills_process(ws):
    started = time.time()
    out = run_command(ws, _py("import time;time.sleep(30)"), timeout=2)
    elapsed = time.time() - started
    assert out["timed_out"] is True
    assert elapsed < 20, "超时未生效，进程跑满了（当前 %.1fs）" % elapsed


def test_timeout_is_clamped(ws):
    """timeout 必须被夹到 1–600，非法值退回默认"""
    out = run_command(ws, _py("print(1)"), timeout=99999)
    assert out["exit_code"] == 0
    out2 = run_command(ws, _py("print(1)"), timeout="abc")
    assert out2["exit_code"] == 0
    out3 = run_command(ws, _py("print(1)"), timeout=-5)
    assert out3["exit_code"] == 0


# ── 输出截断 ──────────────────────────────────────────────
def test_stdout_truncated_at_20000(ws):
    code = "print('x'*%d)" % (DEFAULT_OUTPUT_LIMIT * 2)
    out = run_command(ws, _py(code), timeout=60)
    assert out["truncated"] is True
    assert len(out["stdout"]) <= DEFAULT_OUTPUT_LIMIT + 100
    assert "输出已截断" in out["stdout"]
    assert out["truncated_at"] == DEFAULT_OUTPUT_LIMIT


def test_stderr_truncated_at_20000(ws):
    code = "import sys;sys.stderr.write('e'*%d)" % (DEFAULT_OUTPUT_LIMIT * 2)
    out = run_command(ws, _py(code), timeout=60)
    assert out["truncated"] is True
    assert len(out["stderr"]) <= DEFAULT_OUTPUT_LIMIT + 100


def test_small_output_not_truncated(ws):
    out = run_command(ws, _py("print('a'*100)"), timeout=30)
    assert out["truncated"] is False


# ── 拦截规则 ──────────────────────────────────────────────
def test_empty_command_rejected(ws):
    with pytest.raises(ToolError) as exc:
        run_command(ws, "   ", timeout=5)
    assert exc.value.code == "INVALID_ARGUMENT"


def test_cd_prefix_rejected(ws):
    for cmd in ("cd ..", "chdir /tmp", "pushd ..", "popd"):
        with pytest.raises(ToolError) as exc:
            run_command(ws, cmd, timeout=5)
        assert exc.value.code == "INVALID_ARGUMENT", cmd


def test_outside_absolute_path_rejected(ws, outside_root):
    cmd = "python %s" % (outside_root / "secret.txt")
    with pytest.raises(ToolError) as exc:
        run_command(ws, cmd, timeout=5)
    assert exc.value.code == "PATH_OUT_OF_WORKSPACE"


def test_outside_absolute_path_quoted_rejected(ws, outside_root):
    cmd = 'python "%s"' % (outside_root / "secret.txt")
    with pytest.raises(ToolError) as exc:
        run_command(ws, cmd, timeout=5)
    assert exc.value.code == "PATH_OUT_OF_WORKSPACE"


def test_outside_posix_path_rejected(ws):
    with pytest.raises(ToolError) as exc:
        run_command(ws, "cat /etc/passwd", timeout=5)
    assert exc.value.code == "PATH_OUT_OF_WORKSPACE"


# ── 可中断 ────────────────────────────────────────────────
def test_cancel_event_interrupts(ws):
    ev = threading.Event()

    def cancel_soon():
        time.sleep(0.5)
        ev.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.time()
    out = run_command(ws, _py("import time;time.sleep(30)"), timeout=60,
                      cancel_event=ev)
    assert out["interrupted"] is True
    assert time.time() - started < 20


def test_cancel_before_start(ws):
    ev = threading.Event()
    ev.set()
    out = run_command(ws, _py("print('x')"), timeout=30, cancel_event=ev)
    # 命令太短可能已经跑完，但只要不悬挂即可
    assert out["exit_code"] is not None


# ── 实时输出回调 ──────────────────────────────────────────
def test_on_output_callback(ws):
    seen = []
    run_command(ws, _py("print('live')"), timeout=30,
                on_output=lambda stream, text: seen.append((stream, text)))
    assert any("live" in t for _s, t in seen)


# ── 【安全】相对路径逃逸：期望被拦截 ────────────────────────
def test_relative_path_escape_must_be_rejected(ws, outside_root):
    """
    验收要求「越界路径 100% 拒绝」。

    只读白名单（`cat` / `type` / `head` / `find` …）只做了**绝对路径**越界检测，
    相对路径 `../outside/secret.txt` 会被判为 readonly 并自动放行，
    从而读到工作区之外的文件内容 —— 这是沙箱逃逸。
    """
    rel = os.path.relpath(str(outside_root / "secret.txt"), str(ws.root))
    cmd = ("type %s" % rel) if IS_WIN else ("cat %s" % rel)

    with pytest.raises(ToolError) as exc:
        run_command(ws, cmd, timeout=30)
    assert exc.value.code == "PATH_OUT_OF_WORKSPACE", (
        "相对路径越界未被拦截，命令 `%s` 被放行了" % cmd)


def test_relative_path_escape_not_auto_approved_by_classifier(ws, outside_root):
    """同上：分级器不应把越界读命令判为 readonly 自动放行"""
    from apps.agent.approval import classify_command

    rel = os.path.relpath(str(outside_root / "secret.txt"), str(ws.root))
    cmd = ("type %s" % rel) if IS_WIN else ("cat %s" % rel)
    info = classify_command(cmd, ws)
    assert info["risk"] != "readonly", (
        "越界读命令 %r 被判为只读自动放行：%s" % (cmd, info))
