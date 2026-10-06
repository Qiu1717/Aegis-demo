# -*- coding: utf-8 -*-
"""
apps/agent/prompts.py — Agent 系统提示词（原生工具版 / JSON 回退版）与工作区摘要

两套通道（设计 §4.2.2）：
    通道 A（原生 function calling）：system = AGENT_SYSTEM_BASE + 工作区摘要
    通道 B（不支持 tools 的模型）  ：system = 上述 + JSON_FALLBACK_SUFFIX

提示词里**不出现任何密钥**，也不出现任何绝对路径以外的敏感信息。
"""
from typing import List, Optional

from .sandbox import Workspace

# ── Agent 系统提示词主体 ──────────────────────────────────
AGENT_SYSTEM_BASE = """你是 Aegis，一个运行在用户本机上的编码 Agent。

你能读取用户的代码库、修改文件、执行命令，并把「读 → 改 → 验证 → 修正」的循环一次跑完。

工作原则：
1. **先看清再动手**：不确定文件结构时，先用 list_dir / search_code 定位，再 read_file 确认原文。
2. **最小改动**：能精确替换就不用整文件覆盖；不要把用户的代码风格改成你的风格。
3. **改完要验证**：涉及可运行的改动时，优先用 run_command 跑测试或脚本确认结果；失败就根据报错修正后再跑。
4. **诚实汇报**：没做到就说没做到，不要编造运行结果；不要声称修改了你没有实际修改的文件。
5. **不要越界**：只能读写工作区内的文件；工作区之外的路径一律不许碰，用户也不会批准。
6. **危险操作先说明**：删除、格式化、强制推送等命令会被系统拦截，不要反复尝试绕过。

工具使用约定：
- 相对路径一律相对于工作区根目录，不要传绝对路径。
- 一次只做一件事，串行推进；每步之间用上一步的真实结果做判断。
- 工具报错时先读懂错误信息（例如 old_text 不唯一、文件不存在），修好参数再重试，不要盲改。

回复风格：用中文，简洁专业。最终答复请说明「改了哪些文件、做了什么验证、还有什么没做」。
"""

#: JSON 回退通道的后缀（不支持原生 function calling 的模型）
JSON_FALLBACK_SUFFIX = """
【工具调用格式约定】
你不能调用原生函数工具，必须改用 JSON 来表达工具调用。规则如下：

1. 当你需要调用工具时，**只输出一个 ```json 代码块**，内容形如：
```json
{"thought":"简短说明你为什么要用这个工具","tool":"read_file","args":{"path":"main.py"}}
```
2. 当你认为任务已经完成、或需要先向用户确认时，输出：
```json
{"thought":"简短总结","final":"给用户的最终答复"}
```
3. 可用工具只有：read_file、list_dir、search_code、write_file、edit_file、run_command。
   - read_file:    {"path":"相对路径","start_line":1,"end_line":100}
   - list_dir:     {"path":"相对目录","max_depth":2}
   - search_code:  {"pattern":"关键词或正则","file_glob":"*.py","regex":false,"max_results":50}
   - write_file:   {"path":"相对路径","content":"完整文件内容"}
   - edit_file:    {"path":"相对路径","old_text":"必须唯一的原文","new_text":"新文本"}
   - run_command:  {"command":"python -m pytest -q","timeout":120}
4. 除了这个 JSON 代码块之外**不要输出任何其他内容**（不要解释、不要加前后缀）。
5. 一次只输出一个 JSON 对象，等系统把工具结果回给你之后再决定下一步。
6. JSON 内的字符串必须用双引号包裹，内容中的引号与反斜杠必须用 \\ 转义，
   禁止输出未转义的换行（换行写成 \\n）。
7. 如果你只想直接回答用户（不需要调用任何工具），优先使用
   {"thought":"...","final":"你的答复"}；万一忘了包裹，系统也会把你输出的
   纯文本直接当作答复转交给用户，不会报错。
"""

#: JSON 回退通道里 web_search 的工具行（仅在联网开关打开时插入）
JSON_FALLBACK_WEB_SEARCH_LINE = (
    '   - web_search:  {"query":"要检索的关键词或问题","max_results":8}')

#: 回退通道里点名的工具清单（关闭联网时不出现 web_search）
FALLBACK_TOOL_ENUM_LOCAL = (
    "可用工具只有：read_file、list_dir、search_code、write_file、edit_file、run_command。")
FALLBACK_TOOL_ENUM_WEB = (
    "可用工具只有：read_file、list_dir、search_code、write_file、edit_file、"
    "run_command、web_search。")


def build_json_fallback_suffix(web_enabled: bool = False) -> str:
    """
    生成 JSON 回退通道的工具约定段。

    :param web_enabled: 是否允许联网检索；打开时把 web_search 补进工具清单，
                        关闭时清单里不出现它，模型不会产出该工具调用。
    """
    suffix = JSON_FALLBACK_SUFFIX.strip()
    if not web_enabled:
        return suffix
    suffix = suffix.replace(FALLBACK_TOOL_ENUM_LOCAL, FALLBACK_TOOL_ENUM_WEB)
    marker = "   - run_command:"
    start = suffix.find(marker)
    if start < 0:
        return suffix + "\n" + JSON_FALLBACK_WEB_SEARCH_LINE
    end = suffix.find("\n", start)
    if end < 0:
        return suffix + "\n" + JSON_FALLBACK_WEB_SEARCH_LINE
    return suffix[:end] + "\n" + JSON_FALLBACK_WEB_SEARCH_LINE + suffix[end:]


#: 达到最大步数时强制总结用的指令
FORCE_SUMMARY_INSTRUCTION = (
    "你已经达到本次任务的最大步数上限。请立即停止调用工具，"
    "用一段话总结：①已经完成的改动；②验证结果；③还没完成的部分与建议的下一步。")

#: 参数解析失败时回填给模型的提示（最多重试 2 次）
PARSE_RETRY_HINT = (
    "上一条输出无法解析成合法的工具调用。请只输出一个 ```json 代码块，"
    "格式严格为 {\"thought\":\"...\",\"tool\":\"<工具名>\",\"args\":{...}}"
    " 或 {\"thought\":\"...\",\"final\":\"最终答复\"}，不要输出其它内容。")

#: 工具参数非法时的提示
INVALID_ARGUMENT_HINT = (
    "工具参数不合法或缺失必填项，请检查后重新输出完整的工具调用。")


def workspace_summary(ws: Optional[Workspace], max_depth: int = 2,
                      max_entries: int = 120) -> str:
    """
    生成工作区摘要（目录树 + 根目录），注入 system 提示词。

    失败时返回空串，绝不抛异常（提示词只是辅助信息）。
    """
    if not ws:
        return ""
    try:
        _entries, tree, truncated = ws.walk(
            ".", max_depth=max_depth, max_entries=max_entries)
    except Exception:
        return ""
    if not tree:
        return ""
    head = "工作区根目录：%s\n目录结构（已忽略 .git/node_modules/__pycache__/.venv 等）：\n" % ws.root
    tail = "\n（目录树已截断，需要更多内容请用 list_dir 展开）" if truncated else ""
    return head + tree + tail


def build_system_prompt(ws: Optional[Workspace] = None,
                        native_tools: bool = True,
                        base_prompt: str = "",
                        thinking_level: str = "",
                        skills: Optional[List[dict]] = None,
                        web_enabled: bool = False) -> str:
    """
    组装完整的 Agent system 提示词。

    :param ws: 工作区（用于注入目录摘要）
    :param native_tools: 是否走原生 function calling 通道
    :param base_prompt: 来自 config.json 的既有 system_prompt（可为空）
    :param thinking_level: 思考量级 light / balanced / max（决定回复深度倾向）
    :param skills: 已启用的技能清单 [{name, description, instructions}]
    :param web_enabled: 是否允许联网检索（决定回退通道的工具清单里是否有 web_search）
    """
    parts = []
    if base_prompt:
        parts.append(base_prompt.strip())
    parts.append(AGENT_SYSTEM_BASE.strip())

    hint = THINKING_HINTS.get(str(thinking_level or "").strip().lower(), "")
    if hint:
        parts.append(hint)

    # 工具使用原则：无关问题（天气/闲聊等）不应触发工作区检索
    parts.append(TOOL_USAGE_RULE.strip())

    summary = workspace_summary(ws)
    if summary:
        parts.append(summary)

    skill_section = build_skills_section(skills)
    if skill_section:
        parts.append(skill_section)

    if not native_tools:
        parts.append(build_json_fallback_suffix(web_enabled))

    return "\n\n".join(p for p in parts if p)


#: 工具使用原则：先判断是否需要工作区上下文，避免无关问题也去扫目录 / 文件
TOOL_USAGE_RULE = """【工具使用原则】
1. 先判断用户的问题是否需要本地工作区上下文：
   · 需要：涉及代码、文件、目录、项目结构、报错排查、改动实现等；
   · 不需要：天气、新闻、汇率、股价、常识、翻译、闲聊、通用知识问答等。
2. 只有「需要」时才使用 read_file / list_dir / search_code；
   天气、新闻、实时信息类问题优先用 web_search 联网检索，或直接回答。
3. 不要为了「了解一下项目」就先扫一遍目录——用户没问工作区，就不要碰工作区。
4. 能用一次工具回答清楚的，不要连续多次检索。"""

#: 思考量级 → 回复深度倾向（light 省 token / max 强推理）
THINKING_HINTS = {
    "light": "【思考量级：轻量级】直接给结论，尽量一步到位；减少探索与多步验证，节省时间与 token。",
    "balanced": "",
    "max": "【思考量级：极致性能】先充分分析再动手；关键改动务必验证，"
           "必要时多跑测试与交叉检查，允许更多步骤与更长的推理。",
}


def build_skills_section(skills: Optional[List[dict]]) -> str:
    """
    已启用技能 → system 提示词注入段。

    约定模型使用技能后在回复开头标注 [技能:名称]，runner 据此发 skill_used
    事件供前端展示调用状态。
    """
    if not skills:
        return ""
    lines = ["【可用技能】用户启用了以下技能；当需求与技能描述匹配时按其说明执行，"
             "并在回复开头用 [技能:技能名称] 标注："]
    for s in skills:
        name = str(s.get("name") or "").strip()
        desc = str(s.get("description") or "").strip()
        ins = str(s.get("instructions") or "").strip()
        if not name:
            continue
        line = "- %s" % name
        if desc:
            line += "（%s）" % desc
        if ins:
            line += "：%s" % ins
        lines.append(line)
    return "\n".join(lines)


def build_final_instruction(user_task: str, workspace: str = "") -> str:
    """组装用户任务消息（带上工作区说明，便于模型理解相对路径）"""
    task = (user_task or "").strip()
    if not workspace:
        return task
    return "工作区：%s\n\n任务：%s" % (workspace, task)
