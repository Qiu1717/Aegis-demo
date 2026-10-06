# -*- coding: utf-8 -*-
"""
apps/llm/offline.py — 本地模式应答引擎（未配置大模型服务时生效）

用途：
    当所有云端服务商都不可用（欠费 / 无密钥 / 断网 / 限流）时，
    由本模块给出仍然体面的回答，保证对话界面不会抛错、不会空白。

它不是一个大模型，能力边界明确；对于处理不了的问题会诚实说明当前处于降级模式。
"""
import ast
import operator
import re
from datetime import datetime

_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

DEGRADED_HINT = ""


# ── 安全的四则运算求值 ────────────────────────────────────
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node):
    """只允许数字与 +-*/ 等运算符，绝不允许函数调用或属性访问"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("不支持的表达式")


_ARITH_RE = re.compile(r"^[\s0-9+\-*/().%]+$")
# 允许以数字或左括号开头，这样 (12+8)*3 这类带前置括号的算式也能被提取
_ARITH_TOKEN_RE = re.compile(r"[0-9(][0-9\s+\-*/().%]*")


def _longest_expr(text: str) -> str:
    """从自然语句里挑出最长的一段算术表达式"""
    best = ""
    for m in _ARITH_TOKEN_RE.finditer(text):
        seg = m.group(0).strip()
        if len(seg) > len(best) and re.search(r"[+\-*/]", seg):
            best = seg
    return best


def try_calculate(text: str):
    """
    尝试识别并计算算术表达式，支持从整句中提取
    例："1+1等于几" / "帮我算 12*(3+4)" / "100除以8"
    :return: 计算结果字符串；不是算术题则返回 None
    """
    expr = (text or "").strip()
    if not expr:
        return None

    expr = (expr.replace("×", "*").replace("÷", "/").replace("^", "**")
            .replace("乘以", "*").replace("除以", "/")
            .replace("乘", "*").replace("加", "+").replace("减", "-"))

    # 整句就是一个表达式时直接用，否则从句中提取最长的一段
    candidate = expr if _ARITH_RE.match(expr) else _longest_expr(expr)
    if not candidate:
        return None

    candidate = candidate.strip().strip("=?？").strip()
    candidate = re.sub(r"\s+", " ", candidate)
    if len(candidate) < 3 or not re.search(r"\d", candidate):
        return None
    if not re.search(r"[+\-*/]", candidate):
        return None

    try:
        result = _safe_eval(ast.parse(candidate, mode="eval"))
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError):
        return None
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return f"{candidate} = {result}"


# ── 规则匹配 ──────────────────────────────────────────────
def _has_any(text: str, words) -> bool:
    return any(w in text for w in words)


_RECALL_RE = re.compile(
    r"(记得我|我叫什么|我的名字|你还记得|我住|我来自|我的偏好|我喜欢|我是什么|我是做|我的职业|关于我)"
)


def _recall_from_memory(user_text: str, memory_text: str) -> str:
    """
    当用户询问「你还记得我吗 / 我叫什么」等时，从长期记忆文本中召回作答。
    memory_text 形如「【用户长期记忆】\n- 姓名：小明\n- 所在地：北京」。
    """
    if not memory_text or not _RECALL_RE.search(user_text or ""):
        return None
    body = memory_text.split("】", 1)[1].strip() if "】" in memory_text else memory_text.strip()
    lines = [ln.strip().lstrip("- ").strip()
             for ln in body.splitlines() if ln.strip().startswith("-")]
    if not lines:
        return None
    return ("我记得关于你的一些信息：\n"
            + "\n".join("· " + ln for ln in lines)
            + "\n\n（这是跨会话保留的长期记忆，方便我更好地为你服务。）")


def generate(user_text: str, memory_text: str = None) -> str:
    """
    离线应答主入口
    :param user_text: 用户消息
    :param memory_text: 可注入的用户长期记忆文本（用于召回）
    :return: 回复文本
    """
    # 优先处理「记忆召回」类问题（即便离线也能用上跨会话记忆）
    recall = _recall_from_memory(user_text, memory_text) if memory_text else None
    if recall:
        return recall

    t = (user_text or "").strip()
    lower = t.lower()
    now = datetime.now()

    # 1. 身份类
    if _has_any(t, ["你是谁", "你叫什么", "自我介绍", "介绍一下你", "什么模型", "谁开发的"]):
        return (
            "我是 Aegis。当前尚未接入大模型服务，我暂时以本地模式应答，能力有限；"
            "在设置中配置任意一家服务商的 API Key 后即可恢复完整能力。"
        )

    # 2. 问候类
    if lower in ("hi", "hello", "你好", "您好", "嗨", "早上好", "晚上好", "下午好", "test", "测试"):
        return (
            "你好！我能收到你的消息，通信链路是正常的。\n\n"
            "但目前尚未接入大模型服务，"
            "所以我暂时只能处理问候、时间、算数这几类简单问题。"
            f"{DEGRADED_HINT}"
        )

    # 3. 算术
    calc = try_calculate(t)
    if calc:
        return f"{calc}\n\n（由本地规则引擎计算，未经过大模型。）"

    # 4. 时间日期
    if _has_any(t, ["几点", "当前时间", "现在时间", "时间"]):
        return f"现在是 {now.strftime('%Y-%m-%d %H:%M:%S')}，{_WEEKDAYS[now.weekday()]}。"
    if _has_any(t, ["今天几号", "今天是", "日期", "星期几", "周几", "什么日子"]):
        return f"今天是 {now.strftime('%Y年%m月%d日')}，{_WEEKDAYS[now.weekday()]}。"

    # 5. 明确需要联网能力的话题
    if _has_any(t, ["天气", "新闻", "股价", "汇率", "今天发生了什么", "热搜"]):
        return (
            "这类实时信息需要联网查询能力，而当前大模型服务未接通，我拿不到外部数据。\n\n"
            "建议稍后接入免费模型再试（参考 docs/大模型配置指南.md）。"
        )

    # 6. 兜底
    return (
        "我收到了你的问题，但当前所有大模型服务都不可用，无法给出真正有价值的回答。\n\n"
        "可能的原因：\n"
        "· 还没有配置任何 API Key\n"
        "· 账户余额不足 / 额度用尽（例如此前的阿里云百炼）\n"
        "· 本机网络无法访问模型服务\n\n"
        "好消息是：换用免费服务不需要花一分钱。最快的做法是注册智谱 open.bigmodel.cn，"
        "拿到 Key 后填进环境变量 ZHIPU_API_KEY 重启即可，GLM-4-Flash 对开发者永久免费。"
        f"{DEGRADED_HINT}"
    )
