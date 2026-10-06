# -*- coding: utf-8 -*-
"""
apps/agent/terminal.py — 子进程命令执行（安全模型第③道闸的执行侧）

约束（设计 §4.4.2）：
    · cwd 恒等于工作区根目录，禁止 `cd` / `chdir` 前缀逃逸；
    · 单条命令默认超时 120 秒（1–600 可配）；
    · stdout / stderr 各截断 20000 字符并置 `truncated=true`；
    · 用户可中断（cancel_event）→ 杀进程树；
    · 编码：UTF-8 → GBK 回退，`errors="replace"`，并注入 PYTHONIOENCODING=utf-8；
    · 命令中出现工作区之外的绝对路径 → 直接拒绝（PATH_OUT_OF_WORKSPACE）。
"""
import os
import re
import signal
import subprocess
import threading
import time
from typing import Callable, List, Optional

from .approval import find_outside_paths
from .sandbox import SandboxError, Workspace
from .schemas import ToolError

#: 默认超时（秒）
DEFAULT_TIMEOUT = 120
#: 单条输出截断上限（字符）
DEFAULT_OUTPUT_LIMIT = 20000

#: 禁止的目录切换前缀
_CD_RE = re.compile(r"^\s*(cd|chdir|pushd|popd)\b", re.IGNORECASE)


def _decode(raw: bytes) -> str:
    """UTF-8 → GBK 回退解码，绝不因编码问题丢输出"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk", errors="replace")
        except (UnicodeDecodeError, LookupError):
            return raw.decode("utf-8", errors="replace")


def _truncate(text: str, limit: int = DEFAULT_OUTPUT_LIMIT):
    """截断超长输出（保留提示，避免模型/前端被撑爆）"""
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...（输出已截断，仅显示前 %d 字符）" % limit, True


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀掉进程树（Windows taskkill /F /T，POSIX killpg）"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _reader(pipe, sink: List[str], stream: str,
            on_output: Optional[Callable[[str, str], None]]) -> None:
    """读线程：逐行读取并即时回调（用于右栏终端实时追加）"""
    try:
        for raw in iter(pipe.readline, b""):
            text = _decode(raw)
            if not text:
                continue
            sink.append(text)
            if on_output:
                try:
                    on_output(stream, text)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def run_command(ws: Workspace, command: str, timeout: int = DEFAULT_TIMEOUT,
                cancel_event=None, on_output: Optional[Callable[[str, str], None]] = None,
                output_limit: int = DEFAULT_OUTPUT_LIMIT) -> dict:
    """
    在工作区根目录执行一条命令。

    :param ws: 工作区（决定 cwd）
    :param command: 命令原文
    :param timeout: 超时秒数（会被夹到 1–600）
    :param cancel_event: threading.Event，置位即中断
    :param on_output: 回调 (stream, text)，用于实时推送 terminal 事件
    :return: 工具返回体里的 data 段
    :raises ToolError: INVALID_ARGUMENT / PATH_OUT_OF_WORKSPACE / SPAWN_FAILED
    """
    cmd = (command or "").strip()
    if not cmd:
        raise ToolError("INVALID_ARGUMENT", "命令为空")

    if _CD_RE.match(cmd):
        raise ToolError("INVALID_ARGUMENT",
                        "不允许切换目录（cd/chdir），所有命令都在工作区根目录执行")

    outside = find_outside_paths(cmd, ws)
    if outside:
        raise ToolError("PATH_OUT_OF_WORKSPACE",
                        "命令中包含工作区之外的绝对路径：%s" % "、".join(outside[:3]),
                        detail={"paths": outside[:5]})

    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1, min(600, timeout))

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    popen_kwargs = {}
    if os.name == "posix":
        # 独立进程组，便于 killpg 一次性杀掉整棵子树
        popen_kwargs["start_new_session"] = True

    started = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(ws.root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs
        )
    except (OSError, ValueError):
        raise ToolError("SPAWN_FAILED", "命令启动失败，请检查命令是否可执行")

    out_buf: List[str] = []
    err_buf: List[str] = []
    threads = [
        threading.Thread(target=_reader, args=(proc.stdout, out_buf, "stdout", on_output),
                         daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, err_buf, "stderr", on_output),
                         daemon=True),
    ]
    for t in threads:
        t.start()

    timed_out = False
    interrupted = False
    deadline = started + timeout

    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            _kill_tree(proc)
            interrupted = True
            break
        if time.time() >= deadline:
            _kill_tree(proc)
            timed_out = True
            break
        time.sleep(0.1)

    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    for t in threads:
        t.join(timeout=5)

    duration_ms = int((time.time() - started) * 1000)
    exit_code = proc.returncode if proc.returncode is not None else -1

    stdout_text, out_cut = _truncate("".join(out_buf), output_limit)
    stderr_text, err_cut = _truncate("".join(err_buf), output_limit)

    return {
        "command": cmd,
        "cwd": str(ws.root),
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "truncated": bool(out_cut or err_cut),
        "truncated_at": output_limit,
    }


def build_shell_error(exc: BaseException) -> ToolError:
    """把沙箱/系统异常转成统一的 ToolError"""
    if isinstance(exc, SandboxError):
        return ToolError(exc.code, exc.message)
    if isinstance(exc, ToolError):
        return exc
    return ToolError("SPAWN_FAILED", "命令执行异常，请查看日志")
