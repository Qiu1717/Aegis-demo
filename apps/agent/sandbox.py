# -*- coding: utf-8 -*-
"""
apps/agent/sandbox.py — 工作区路径沙箱（安全模型第②道闸）

核心不变量：**任何文件/命令路径归一化后必须落在工作区内**。

    p = (root / rel).resolve()      # resolve() 展开 .. 与符号链接
    p.relative_to(root)             # 越界即抛 SandboxError

· 符号链接：`Path.resolve()` 会展开最终目标，展开后再比较，
  因此软链接指向外部会被拒绝（AG-10②）。
· Windows 大小写 / 驱动器差异：统一 `os.path.normcase` 后比较。
· 工作区由请求体传入，服务端二次校验「存在且是目录」；
  未传或不存在则拒绝启动任务，**不做默认猜测**（设计 §4.4.1 / Q1）。
"""
import os
import re
from pathlib import Path
from typing import Iterator, List, Optional, Tuple


class SandboxError(Exception):
    """路径越界或不被允许"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


#: 目录忽略规则（.aegis_backup 必须忽略，避免 Agent 读到自己的备份）
DEFAULT_IGNORE_DIRS = {
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".aegis_backup",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".egg-info",
}

#: 文件忽略规则（按后缀）
DEFAULT_IGNORE_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".png", ".jpg",
    ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar", ".7z",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".db", ".sqlite3",
}

#: 单个文件读取上限（超过直接拒绝，建议改用 search_code）
MAX_FILE_BYTES = 2 * 1024 * 1024

#: Windows 绝对路径，如 C:\foo、D:/bar
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
#: POSIX 绝对路径
_POSIX_ABS_RE = re.compile(r"^/")


class Workspace:
    """
    一个授权工作区。

    :param root: 工作区根目录（必须存在且是目录）
    """

    def __init__(self, root):
        raw = str(root or "").strip().strip('"')
        if not raw:
            raise SandboxError("WORKSPACE_EMPTY", "工作区路径为空")
        try:
            path = Path(os.path.expanduser(raw)).resolve()
        except (OSError, ValueError):
            raise SandboxError("WORKSPACE_INVALID", "工作区路径不合法")
        if not path.exists():
            raise SandboxError("WORKSPACE_NOT_FOUND", "工作区目录不存在：%s" % raw)
        if not path.is_dir():
            raise SandboxError("WORKSPACE_NOT_DIR", "工作区路径不是目录：%s" % raw)
        # 可读性校验：打开目录句柄试探一下（不遍历内容），无权限时给出明确提示
        try:
            handle = os.scandir(str(path))
            handle.close()
        except PermissionError:
            raise SandboxError("WORKSPACE_NOT_READABLE",
                               "工作区目录不可读（当前账号无访问权限）：%s" % raw)
        except OSError as e:
            raise SandboxError("WORKSPACE_NOT_READABLE",
                               "工作区目录无法访问：%s（%s）" % (raw, e.strerror or "I/O 错误"))
        self.root = path

    # ── 归一化与越界判定 ──────────────────────────────────
    def _norm(self, p) -> str:
        """统一大小写 / 驱动器，消除 Windows 与 POSIX 差异"""
        return os.path.normcase(os.path.normpath(str(p)))

    def is_inside(self, p) -> bool:
        """路径是否落在工作区内"""
        try:
            Path(p).relative_to(self.root)
            return True
        except ValueError:
            pass
        # relative_to 在部分场景（大小写差异）会失败，再用字符串前缀兜一次
        root_norm = self._norm(self.root)
        path_norm = self._norm(p)
        return path_norm == root_norm or path_norm.startswith(root_norm + os.sep)

    def _probe_symlinks(self, rel_parts) -> None:
        """
        逐级探测符号链接：任何一级指向工作区外即拒绝。

        覆盖 `Path.resolve()` 的盲区——悬挂符号链接（目标不存在）不会被
        realpath 展开，路径判定会误判为「在工作区内」，而后续真实文件
        操作会被 OS 跟随链接越界。这里用 os.readlink 取真实指向判定；
        无法验证的链接一律按越界处理（fail-closed）。
        """
        probe = self.root
        for part in rel_parts:
            if part in (".", ".."):
                continue
            probe = probe / part
            try:
                # 直接尝试读链接：同时覆盖符号链接与 NTFS junction；
                # 普通文件/目录读链接会抛 OSError，按非链接处理
                linked = os.readlink(str(probe))
                # Windows 链接目标可能带 \\?\ 扩展前缀（junction 常见），
                # 不剥离会导致与工作区根比较失败而误判越界
                s = str(linked)
                if s.startswith("\\\\?\\UNC\\"):
                    s = "\\\\" + s[8:]
                elif s.startswith("\\\\?\\"):
                    s = s[4:]
                real = Path(s)
                real = real.resolve() if real.is_absolute() \
                    else (probe.parent / real).resolve()
            except (OSError, ValueError):
                continue
            if not self.is_inside(real):
                raise SandboxError(
                    "PATH_OUT_OF_WORKSPACE", "路径经符号链接越界，已拒绝")

    def resolve(self, rel: str = ".") -> Path:
        """
        把相对路径解析为工作区内的绝对路径。

        :raises SandboxError: PATH_OUT_OF_WORKSPACE（含 `..` 逃逸与符号链接跨越）
        """
        raw = str(rel if rel not in (None, "") else ".").strip()
        if not raw:
            raw = "."
        raw = raw.replace("\\", "/")

        # 传入绝对路径时先尝试转成相对：只有落在工作区内才允许
        if _WIN_ABS_RE.match(raw) or _POSIX_ABS_RE.match(raw):
            try:
                candidate = Path(os.path.normpath(raw)).resolve()
            except (OSError, ValueError):
                raise SandboxError("PATH_OUT_OF_WORKSPACE", "路径超出工作区范围，已拒绝")
            if not self.is_inside(candidate):
                raise SandboxError("PATH_OUT_OF_WORKSPACE", "路径超出工作区范围，已拒绝")
            try:
                rel_parts = Path(os.path.relpath(str(candidate), str(self.root))).parts
            except (OSError, ValueError):
                rel_parts = ()
            self._probe_symlinks(rel_parts)
            return candidate

        try:
            target = (self.root / raw).resolve()
        except (OSError, ValueError):
            raise SandboxError("PATH_OUT_OF_WORKSPACE", "路径超出工作区范围，已拒绝")

        if not self.is_inside(target):
            raise SandboxError("PATH_OUT_OF_WORKSPACE", "路径超出工作区范围，已拒绝")
        self._probe_symlinks(Path(raw).parts)
        return target

    def rel_path(self, p) -> str:
        """绝对路径 -> 相对工作区的 POSIX 风格路径"""
        try:
            return Path(p).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return os.path.basename(str(p))

    # ── 忽略规则 ──────────────────────────────────────────
    def is_ignored(self, p) -> bool:
        """路径任一层级命中忽略规则即跳过"""
        try:
            parts = Path(p).parts
        except (OSError, ValueError):
            return True
        for part in parts:
            if part in DEFAULT_IGNORE_DIRS:
                return True
        name = Path(p).name
        if name.startswith(".aegis_backup"):
            return True
        if Path(name).suffix.lower() in DEFAULT_IGNORE_SUFFIXES:
            return True
        if name.endswith(".aegis-tmp"):
            return True
        if ".aegis-tmp-" in name:
            return True
        return False

    # ── 目录树 ────────────────────────────────────────────
    def walk(self, rel: str = ".", max_depth: int = 2,
             max_entries: int = 300) -> Tuple[List[dict], str, bool]:
        """
        列出目录树。

        :return: (entries, tree_text, truncated)
        """
        base = self.resolve(rel)
        if not base.is_dir():
            raise SandboxError("NOT_A_DIR", "该路径不是目录：%s" % rel)

        entries: List[dict] = []
        lines: List[str] = []
        truncated = False

        def visit(dir_path: Path, depth: int, prefix: str) -> None:
            nonlocal truncated
            if depth > max_depth or truncated:
                return
            try:
                children = sorted(
                    [c for c in dir_path.iterdir() if not self.is_ignored(c)],
                    key=lambda c: (c.is_file(), c.name.lower()),
                )
            except OSError:
                return
            for child in children:
                if len(entries) >= max_entries:
                    truncated = True
                    return
                is_dir = child.is_dir()
                entries.append({
                    "name": self.rel_path(child),
                    "type": "dir" if is_dir else "file",
                    "size": None if is_dir else _safe_size(child),
                })
                lines.append("%s%s%s" % (prefix, child.name, "/" if is_dir else ""))
                if is_dir:
                    visit(child, depth + 1, prefix + "  ")

        visit(base, 1, "")
        return entries, ("\n".join(lines) + ("\n" if lines else "")), truncated

    def iter_files(self, rel: str = ".", file_glob: str = "*",
                   max_files: int = 5000) -> Iterator[Path]:
        """遍历工作区内可用于检索的文件（自动跳过忽略项与超大文件）"""
        base = self.resolve(rel)
        if not base.is_dir():
            return
        count = 0
        for dirpath, dirnames, filenames in os.walk(str(base)):
            # 就地裁剪，避免进入 .git / node_modules 等巨型目录
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in DEFAULT_IGNORE_DIRS and not self.is_ignored(Path(dirpath) / d)
            )
            for name in sorted(filenames):
                if count >= max_files:
                    return
                full = Path(dirpath) / name
                if self.is_ignored(full):
                    continue
                if file_glob and file_glob != "*":
                    try:
                        if not Path(name).match(file_glob):
                            continue
                    except (ValueError, OSError):
                        continue
                count += 1
                yield full


def _safe_size(p: Path) -> Optional[int]:
    try:
        return p.stat().st_size
    except OSError:
        return None


def open_workspace(path: str) -> Workspace:
    """
    打开（并校验）一个工作区。

    :raises SandboxError: 路径为空 / 不存在 / 不是目录
    """
    return Workspace(path)
