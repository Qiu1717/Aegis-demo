"""
prepublish_check.py — 发布前敏感信息与产物洁净度校验

用途：在 **git commit / 打包 / 推送** 之前运行，发现问题立即以非零退出码中止。

检查项：
  1. 敏感文件是否会被提交（.env、密钥库、数据库、日志、本地配置…）
  2. 已跟踪文件里是否含真实密钥（按服务商前缀 + 通用形态）
  3. 待提交的变更内容里是否含密钥
  4. 安装包产物是否为「干净未配置」状态（不含本地运行数据）

用法：
    python prepublish_check.py            # 全量检查
    python prepublish_check.py --staged  # 只检查暂存区（提交前用）
    python prepublish_check.py --dist     # 额外检查 dist 产物洁净度
"""
from __future__ import print_function

import argparse
import json
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))

# ── 1. 绝不允许被跟踪的文件（路径模式）────────────────────────
FORBIDDEN_PATHS = [
    ".env",                       # 本地密钥（.env.example 除外）
    "*.db", "*.sqlite", "*.sqlite3",
    "secrets.enc", "keys.index.json", "keys.json",
    "config.user.json",
    "aegis.db", "aiagent.db",
    "*.log",
    "id_rsa*", "*.pem", "*.key", "*.p12", "*.pfx",    "credentials.json", "token.json",
]

# 这些目录整体排除
FORBIDDEN_DIRS = [
    ".venv", "venv", ".venv-build",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "build", "dist", "Output",
    "logs", "_qa_tmp",
]

# ── 2. 密钥形态（按服务商前缀 + 通用高熵串）──────────────────
# 只匹配「明显是密钥」的形态，避免误报正常的代码标识符
SECRET_PATTERNS = [
    ("OpenAI 风格", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("Anthropic 风格", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35,}\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("智谱 Key", re.compile(r"\b[a-f0-9]{32}\.[a-f0-9]{16}\b")),
    ("DeepSeek Key", re.compile(r"\bsk-[a-f0-9]{32,}\b")),
]

# 赋值形式：xxx_KEY = "..." / "api_key": "..."
ASSIGN_FORMS = [
    re.compile(r"""(?i)(api[_-]?key|secret|token|password|passwd|app[_-]?secret)["']?\s*[:=]\s*["']([A-Za-z0-9_\-\.]{16,})["']"""),
]

# 明确不算密钥的占位值
ALLOW_PLACEHOLDERS = {
    "", "your_api_key", "your-api-key", "yourapikey", "changeme", "change_me",
    "xxx", "placeholder", "none", "null", "true", "false",
    "<明文或留空>", "sk-...", "...", "***", "todo", "your_token",
    "your_password", "<掩码>", "masked", "sk-fake-key-0000000000",
    "test", "demo", "example", "abc123", "123456",
}

# ── 测试样本豁免 ────────────────────────────────────────────
# 校验器的自测文件里**故意埋了**形似真密钥的样本（否则无法验证自己抓得住）。
# 这些是公开的示例串，不是真实凭据，可辨识特征：
#   - AWS 官方文档里的示例 Access Key（含 IOSFODNN 字样）
#   - 明写的测试桩前缀（sk-fake-key-…）
#   - 连续的 abcdefghij / 1234567890 / 重复单字符
#   - sk-... 这类掩码
# 策略：**仅对测试文件生效**，且逐条列出可辨识特征，
# 任何不含这些特征的密钥（即便是真实密钥）仍会被拦下。
#
# 注意：本文件的注释与常量里也**不得写出完整密钥串** —— 否则本文件
# 扫自己就会误报。以下均为片段，非完整串。
TEST_SAMPLE_MARKERS = (
    "abcdefghij", "ABCDEFGHIJ",
    "IOSFODNN",                      # AWS 示例串的特征片段
    "1234567890",
    "sk-fake-key", "sk-...",
    "PLACEHOLDER", "YOURKEY", "fakekey", "fake-key",
)


def _is_test_file(rel):
    return _norm(rel).startswith("tests/")

# 这些目录整体排除（**扫描时跳过**，不代表仓库里不能有）
SKIP_DIRS = set(FORBIDDEN_DIRS) | {".git", "tests"}

# 需要扫描密钥的文本文件后缀
SCAN_EXT = (".py", ".js", ".json", ".html", ".css", ".md", ".txt",
            ".toml", ".cfg", ".ini", ".yml", ".yaml", ".bat", ".iss", ".spec")


def _norm(path):
    """统一成 posix 风格、大小写不敏感的比对键"""
    return path.replace("\\", "/").lower()


def _is_forbidden(path):
    """
    判断是否属于「绝不能进仓库」的文件。

    注意与 SKIP_DIRS 区分：SKIP_DIRS 是「扫描时跳过」（如 tests 是项目资产，
    要提交、但没必要扫密钥）；这里是「真的不该存在」——密钥、数据库、构建产物。
    """
    p = _norm(path)
    base = p.rsplit("/", 1)[-1]
    for d in FORBIDDEN_DIRS:
        if ("/%s/" % d) in ("/" + p) or p.startswith(d + "/"):
            return d
    for pat in FORBIDDEN_PATHS:
        if pat.startswith("*") and pat.endswith("*"):
            # 双侧通配，如 *secret*
            frag = pat.strip("*").lower()
            if frag and frag in base:
                return pat
        elif pat.startswith("*"):
            # 后缀通配，如 *.db
            if base.endswith(pat[1:]):
                return pat
        elif pat.endswith("*"):
            # 前缀通配，如 id_rsa*
            if base.startswith(pat[:-1]):
                return pat
        elif base == pat:
            return pat
    return None


def _git(*args):
    try:
        r = subprocess.run(["git"] + list(args), cwd=BASE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=90)
        return r.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _tracked_files():
    out = _git("ls-files")
    return [f for f in out.splitlines() if f.strip()]


def _iter_scannable(files):
    for rel in files:
        full = os.path.join(BASE, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            continue
        if rel.lower().endswith(SCAN_EXT) or base_is_env(rel):
            yield rel, full


def base_is_env(rel):
    name = rel.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name == ".env" or name.startswith(".env.")


def _scan_text(rel, text):
    """返回 [(类型, 行号, 片段)]，已剔除占位值与测试样本"""
    hits = []
    is_test = _is_test_file(rel)
    for lineno, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        # 跳过注释行里明显是举例的
        for name, pat in SECRET_PATTERNS:
            m = pat.search(line)
            if m:
                frag = m.group(0)
                # 测试文件 + 可辨识的测试样本 → 跳过
                if is_test and any(mk in frag for mk in TEST_SAMPLE_MARKERS):
                    continue
                hits.append((name, lineno, frag[:12] + "…"))
        for pat in ASSIGN_FORMS:
            for m in pat.finditer(line):
                val = m.group(2)
                if val.lower() in ALLOW_PLACEHOLDERS:
                    continue
                if val.endswith("...") or "..." in val:
                    continue
                if is_test and any(mk in val for mk in TEST_SAMPLE_MARKERS):
                    continue
                hits.append(("赋值形式的密钥", lineno, m.group(1) + "=" + val[:8] + "…"))
    return hits


def check_forbidden_files(files, label, only_tracked=False):
    """
    检查是否有敏感文件会被提交。

    only_tracked=True 时（检查 git 跟踪内容）：任何敏感文件都算问题。
    only_tracked=False 时（检查工作区）：本地存在 .env / *.db 是**正常的**
    开发配置，只要它没被 git 跟踪就不算问题 —— 否则本地无法开发。
    """
    bad = []
    for rel in files:
        why = _is_forbidden(rel)
        if not why:
            continue
        if not only_tracked and not _is_tracked(rel):
            continue          # 本地有但没进 git —— 正常
        bad.append((rel, why))
    if bad:
        print("  [FAIL] %s 中包含敏感/不应提交的文件：" % label)
        for rel, why in bad:
            print("         %-52s (%s)" % (rel, why))
    else:
        print("  [OK] %s 无敏感文件" % label)
    return bad


def _is_tracked(rel):
    out = _git("ls-files", "--error-unmatch", rel)
    return bool(out)


def check_secrets(files, label):
    total = 0
    for rel, full in _iter_scannable(files):
        try:
            text = open(full, "r", encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        hits = _scan_text(rel, text)
        if hits:
            total += len(hits)
            print("  [FAIL] %s：" % rel)
            for name, lineno, frag in hits[:5]:
                print("         L%-5d %-16s %s" % (lineno, name, frag))
    if not total:
        print("  [OK] %s 未发现真实密钥" % label)
    return total


def check_dist_clean(dist_dir):
    """安装包/免安装目录必须是干净未配置状态"""
    problems = []
    if not os.path.isdir(dist_dir):
        print("  [SKIP] 产物目录不存在：%s" % dist_dir)
        return problems
    # 1) 不该有任何数据库 / 用户配置 / 密钥文件
    banned_names = ("aegis.db", "aiagent.db", "config.user.json",
                    "secrets.enc", "keys.index.json", "runtime.json")
    for root, _dirs, files in os.walk(dist_dir):
        for f in files:
            if f.lower() in banned_names:
                problems.append(os.path.join(root, f))
    # 2) 模板里不能渲染出真实密钥
    for root, _dirs, files in os.walk(dist_dir):
        for f in files:
            if not f.endswith((".js", ".json", ".html", ".py")):
                continue
            p = os.path.join(root, f)
            try:
                text = open(p, "r", encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            for name, pat in SECRET_PATTERNS:
                if pat.search(text):
                    problems.append("%s 内含 %s" % (p, name))
    if problems:
        print("  [FAIL] 产物不干净：")
        for p in problems[:10]:
            print("         %s" % p)
    else:
        print("  [OK] 产物为干净未配置状态（无数据库/无用户配置/无密钥）")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true",
                    help="只检查 git 暂存区（提交前用）")
    ap.add_argument("--dist", action="store_true",
                    help="额外检查 dist 产物洁净度")
    args = ap.parse_args()

    print("=" * 62)
    print("发布前安全校验  —  %s" % BASE)
    print("=" * 62)

    failed = 0

    print()
    print("[1] 仓库已跟踪文件")
    tracked = _tracked_files()
    if not tracked:
        print("  (尚未 git add，先用 --staged 或直接全量扫描)")
        tracked = []
    failed += len(check_forbidden_files(tracked, "已跟踪文件", only_tracked=True))
    failed += check_secrets(tracked, "已跟踪文件")

    print()
    print("[2] 工作区全部文件（本地开发配置允许存在，只要不被 git 跟踪）")
    all_files = []
    for root, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d != ".git"]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), BASE).replace("\\", "/")
            all_files.append(rel)
    failed += len(check_forbidden_files(all_files, "工作区", only_tracked=False))
    failed += check_secrets(all_files, "工作区")

    if args.staged:
        print()
        print("[3] Git 暂存区")
        st = _git("diff", "--cached", "--name-only")
        staged = [f for f in st.splitlines() if f.strip()]
        if not staged:
            print("  (暂存区为空)")
        failed += len(check_forbidden_files(staged, "暂存区"))
        failed += check_secrets(staged, "暂存区")

    if args.dist:
        print()
        print("[4] 安装包产物洁净度")
        failed += len(check_dist_clean(os.path.join(BASE, "dist", "Aegis")))

    print()
    print("=" * 62)
    if failed:
        print("校验未通过：发现 %d 处问题，已中止发布。" % failed)
        print("请处理后重新运行。")
        return 1
    print("校验通过：未发现敏感信息，可安全发布。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
