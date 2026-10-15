"""
test_prepublish_check.py — 校验器自身的阳性测试

一个从不报错的校验器等于没有校验器。这里用「故意埋雷」验证它真能抓到：
  1. 伪造形似真密钥的文本 → 密钥扫描必须命中
  2. 伪造本该被忽略的敏感文件名 → 文件检查必须命中
  3. 确认正常内容（占位符、示例配置）不会被误报
  4. 确认测试目录的豁免**不会**变成安全漏洞

⚠ 本文件刻意**不在源码里写出任何完整的密钥串** —— 所有样本都由片段在运行时
   拼接而成。否则本文件会被校验器（正确地）判为含密钥，导致发布流程被自己
   的测试样本卡死。这本身就是一道真实的自检：校验器扫自己的测试文件必须干净。

既可 standalone 运行，也兼容 pytest 收集：
  python tests/test_prepublish_check.py
  pytest tests/ -q
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import prepublish_check as P


# ── 运行时拼接的样本 ─────────────────────────────────────────
# 刻意拆成碎片：源码里搜不到完整串，但拼出来的结果与真密钥同构。
_FRAG_A = "sk-"          # OpenAI / DeepSeek 系前缀
_FRAG_B = "abcdefgh"     # 可辨识的假字面量（非随机，更易确认是样本）
_FRAG_C = "ij0123"       # 同上
_FRAG_D = "456789"
_FRAG_E = "wxyz"          # 补足长度用

#: 形似 OpenAI / DeepSeek 密钥
KEY_OPENAI = _FRAG_A + _FRAG_B + _FRAG_C + _FRAG_D
#: 形似 Google API Key（AIza + 35 位）
KEY_GOOGLE = "AI" + "za" + "B" * 35
#: 形似 AWS Access Key（AKIA + **正好 16 位**大写字母数字）
KEY_AWS = "AK" + "IA" + "IOSFODNN7EXAM" + "PLE"
#: 形似 GitHub Token（ghp_ + **至少 36 位**字母数字）
KEY_GITHUB = "gh" + "p_" + _FRAG_B + _FRAG_C + _FRAG_D + _FRAG_E + _FRAG_C + _FRAG_B
#: 形似 Slack Token
KEY_SLACK = "xo" + "xb-" + "1234567890-abcdefghijklmnop"

#: 形似「随机但合规」的密钥 —— **不含任何可辨识样本特征**，
#: 用于验证 tests/ 豁免不会放过真实泄漏。
KEY_LOOKS_REAL = _FRAG_A + "9f8e7d6c5b4a39281706f5e4d3c2b1a0"

#: 赋值形式样本（值部分 >= 16 位，触发 ASSIGN_FORMS）
VAL_ASSIGN = "9f8e7d6c5b4a3928" + "1706f5e4"
VAL_SECRET = "a1b2c3d4e5f6" + "g7h8i9j0k1l2"


# ── pytest 用例 ──────────────────────────────────────────────
def test_catch_secret_shapes():
    """各类服务商密钥形态都必须抓到"""
    keys = {
        "OpenAI": KEY_OPENAI,
        "Google": KEY_GOOGLE,
        "AWS": KEY_AWS,
        "GitHub": KEY_GITHUB,
        "Slack": KEY_SLACK,
    }
    for name, key in keys.items():
        hits = P._scan_text("t.py", 'KEY = "%s"\n' % key)
        assert hits, "未能抓到 %s 形态的密钥" % name


def test_catch_assignment_forms():
    """赋值形式的密钥必须抓到"""
    assert P._scan_text("c.py", 'self.api_key = "%s"' % VAL_ASSIGN)
    assert P._scan_text("c.py", 'app_secret: "%s"' % VAL_SECRET)


def test_no_false_positives():
    """正常内容不能误报"""
    safe = [
        'api_key = ""',
        'api_key = "your_api_key"',
        'api_key = "sk-..."',
        "# 说明：填写 api_key 密钥",
        'MODEL = "deepseek-chat"',
        'base_url = "https://api.deepseek.com/v1"',
        'api_key = "sk-fake-key-0000000000"',
        'DEEPSEEK_API_KEY = ""',
        'token = "changeme"',
    ]
    for line in safe:
        hits = P._scan_text("x.py", line)
        assert not hits, "误报：%s -> %s" % (line, hits)


def test_test_file_exemption_scoped():
    """豁免只对 tests/ 生效：其他目录里的同样内容必须照报"""
    line = 'KEY = "%s"\n' % KEY_OPENAI
    # tests/ 下：可辨识的样本被豁免
    assert not P._scan_text("tests/test_x.py", line), "测试文件里的样本应被豁免"
    # 以下各目录：同样内容必须被抓
    for rel in ("apps/llm/providers.py", "config.py",
                "static/js/settings.js", "docs/README.md"):
        hits = P._scan_text(rel, line)
        assert hits, "%s 下的相同内容必须被抓到" % rel


def test_real_key_in_test_dir_still_caught():
    """安全底线：tests/ 里的「像真密钥」的内容仍必须被抓

    豁免只认可辨识的测试特征。若有人把真密钥粘进测试目录，
    它不含这些特征 → 必须被拦下。否则 tests/ 会变成安全黑洞。
    """
    line = 'KEY = "%s"\n' % KEY_LOOKS_REAL
    hits = P._scan_text("tests/test_leak.py", line)
    assert hits, "测试文件里的真实密钥必须被抓到"
    # 赋值形式同样不能漏
    hits = P._scan_text("tests/test_leak.py",
                        'self.api_key = "%s"' % ("7a6b5c4d3e2f" + "1a0b9c8d7e6f5a4b"))
    assert hits, "测试文件里的真实密钥（赋值形式）必须被抓到"


def test_forbidden_file_detection():
    """敏感文件必须被判定出来"""
    for f in [".env", "aegis.db", "aiagent.db", "config.user.json",
              "secrets.enc", "keys.index.json", "app.log", "id_rsa",
              "server.pem", "credentials.json", "mykey.p12"]:
        assert P._is_forbidden(f) is not None, "未判定为敏感：%s" % f


def test_allowlist_not_flagged():
    """正常项目文件不应被误判"""
    for f in [".env.example", "tests/test_runner.py", "apps/main.py",
              "config/config.json", "static/js/settings.js",
              "docs/大模型配置指南.md", "prepublish_check.py",
              "README.md", "CHANGELOG.md"]:
        assert P._is_forbidden(f) is None, "被误判：%s" % f


def test_this_file_has_no_literal_keys():
    """自检：本文件源码里不得出现任何完整密钥串

    这是最关键的一条 —— 如果这里挂了，说明校验器会把干净的测试文件
    判成含密钥，发布流程将被自己的测试样本卡死。
    """
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    hits = P._scan_text("tests/test_prepublish_check.py", src)
    assert not hits, "本文件源码含完整密钥串，校验器会误报自身：%s" % hits


# ── standalone 模式（直接 python 运行时的可读输出）────────────
def _main():
    ok = 0
    fail = 0

    def show(name, cond):
        nonlocal ok, fail
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if cond:
            ok += 1
        else:
            fail += 1

    print("=== 1. 密钥扫描：能否抓到各类密钥形态 ===")
    for name, key in [("OpenAI 风格", KEY_OPENAI), ("Google API Key", KEY_GOOGLE),
                      ("AWS Access Key", KEY_AWS), ("GitHub Token", KEY_GITHUB),
                      ("Slack Token", KEY_SLACK)]:
        show("能抓到 %s" % name, bool(P._scan_text("t.py", 'KEY = "%s"\n' % key)))

    print()
    print("=== 2. 赋值形式的密钥 ===")
    show("能抓到 api_key 赋值",
         bool(P._scan_text("c.py", 'self.api_key = "%s"' % VAL_ASSIGN)))
    show("能抓到 app_secret 赋值",
         bool(P._scan_text("c.py", 'app_secret: "%s"' % VAL_SECRET)))

    print()
    print("=== 3. 正常内容不被误报 ===")
    for line in ['api_key = ""', 'api_key = "your_api_key"', 'api_key = "sk-..."',
                 "# 说明：填写 api_key 密钥", 'MODEL = "deepseek-chat"',
                 'base_url = "https://api.deepseek.com/v1"',
                 'api_key = "sk-fake-key-0000000000"', 'DEEPSEEK_API_KEY = ""',
                 'token = "changeme"']:
        show("不误报: %s" % line[:46], not P._scan_text("x.py", line))

    print()
    print("=== 4. 敏感文件判定 ===")
    for f in [".env", "aegis.db", "aiagent.db", "config.user.json",
              "secrets.enc", "keys.index.json", "app.log", "id_rsa",
              "server.pem", "credentials.json", "mykey.p12"]:
        show("判定为敏感: %s" % f, P._is_forbidden(f) is not None)

    print()
    print("=== 5. 正常文件不误判 ===")
    for f in [".env.example", "tests/test_runner.py", "apps/main.py",
              "config/config.json", "static/js/settings.js",
              "docs/大模型配置指南.md", "prepublish_check.py",
              "README.md", "CHANGELOG.md"]:
        show("不应误判: %s" % f, P._is_forbidden(f) is None)

    print()
    print("=== 6. tests/ 豁免不是安全黑洞 ===")
    sample_line = 'KEY = "%s"\n' % KEY_OPENAI
    show("tests/ 里的可辨识样本被豁免",
         not P._scan_text("tests/test_x.py", sample_line))
    show("非 tests/ 目录里的同样内容照报",
         bool(P._scan_text("apps/llm/providers.py", sample_line)))
    show("tests/ 里的真格式密钥仍被抓",
         bool(P._scan_text("tests/test_leak.py", 'KEY = "%s"\n' % KEY_LOOKS_REAL)))

    print()
    print("=== 7. 自检：本测试文件自身不含完整密钥串 ===")
    here = os.path.abspath(__file__)
    src = open(here, encoding="utf-8").read()
    self_hits = P._scan_text("tests/test_prepublish_check.py", src)
    show("校验器扫描本文件无命中", not self_hits)
    if self_hits:
        for h in self_hits[:5]:
            print("        %s L%s %s" % h)

    print()
    print("=" * 56)
    print("校验器自测：%d passed, %d failed" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(_main())