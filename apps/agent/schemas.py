# -*- coding: utf-8 -*-
"""
apps/agent/schemas.py — 工具集的 OpenAI function JSON Schema

统一规范（设计 §4.1）：
    read_file / list_dir / search_code / write_file / edit_file / run_command
    web_search（联网搜索，复用 apps/websearch 后端能力，受 use_web 开关控制）
    PRD 里的 grep_search、replace_in_file 作为同义别名登记在 TOOL_ALIASES，
    模型若输出旧名会自动归一（低成本换取鲁棒性）。

每个工具都有：
    · OpenAI function Schema（name / description / parameters / required）
    · risk 分级：readonly（自动放行）/ write（需审批）/ dangerous（必须二次确认且不可豁免）
"""
from typing import Any, Dict, List, Optional

# ── 风险分级 ──────────────────────────────────────────────
RISK_READONLY = "readonly"
RISK_WRITE = "write"
RISK_DANGEROUS = "dangerous"
RISK_UNKNOWN = "unknown"


#: old_text 匹配到多处时的错误码
#: （设计文档 §4.1 的示例里写作 MATCH_NOT_UNIQUE，两者同义；
#:   这里以任务书点名的 MULTIPLE_MATCH 为准，并在 detail.legacy_code 里带上另一个，
#:   便于任何一方的校验都能命中。）
ERR_MULTIPLE_MATCH = "MULTIPLE_MATCH"
ERR_MATCH_NOT_UNIQUE = "MATCH_NOT_UNIQUE"


class ToolError(Exception):
    """
    工具执行失败的统一异常（工具契约的一部分）。

    fileio / terminal 只抛这一种异常，
    tools.dispatch 统一转成 {"ok": False, "error": {code, message, detail}}。
    """

    def __init__(self, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.detail is not None:
            error["detail"] = self.detail
        return error

# ── 六个工具的 JSON Schema ────────────────────────────────
READ_FILE_SCHEMA: Dict[str, Any] = {
    "name": "read_file",
    "description": "读取工作区内一个文本文件的内容（带行号）。用于查看源码、配置、日志。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "相对工作区根目录的路径，如 apps/agent/tools.py"},
            "start_line": {"type": "integer", "minimum": 1,
                           "description": "起始行（含），默认 1"},
            "end_line": {"type": "integer", "minimum": 1,
                         "description": "结束行（含），默认文件末尾"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}

LIST_DIR_SCHEMA: Dict[str, Any] = {
    "name": "list_dir",
    "description": "列出工作区内某个目录的文件树（自动忽略 .git/node_modules/__pycache__/.venv 等）。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "相对目录，默认 '.'（工作区根）"},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 5,
                          "description": "递归深度，默认 2"},
            "max_entries": {"type": "integer", "minimum": 10, "maximum": 2000,
                            "description": "最多返回条目数，默认 300"},
        },
        "required": [],
        "additionalProperties": False,
    },
}

SEARCH_CODE_SCHEMA: Dict[str, Any] = {
    "name": "search_code",
    "description": "在工作区内检索代码/文本（关键词或正则），返回 文件:行号:内容。",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "关键词或正则表达式"},
            "path": {"type": "string", "description": "检索起始目录，默认 '.'"},
            "file_glob": {"type": "string",
                          "description": "文件名过滤，如 '*.py'，默认 '*'"},
            "regex": {"type": "boolean",
                      "description": "pattern 是否为正则，默认 false"},
            "case_sensitive": {"type": "boolean", "description": "是否区分大小写，默认 false"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500,
                            "description": "最多返回条数，默认 50"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
}

#: 联网搜索（复用 apps/websearch/services.py::web_search，只读、自动放行）
#: max_results 上限 20 与后端统一（web_search 内部也做了同样的收口）。
#: 默认 Model Hint 给 8：既保证信息量，又不至于把上下文撑爆（回填上限 6000 字符）。
WEB_SEARCH_SCHEMA: Dict[str, Any] = {
    "name": "web_search",
    "description": (
        "联网搜索：按关键词或自然语言问题检索互联网，返回标题、链接与摘要。"
        "用于查阅最新资料、第三方库用法、报错解决方案等本地代码库里没有的信息；"
        "纯本地代码问题请优先用 search_code / read_file。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "检索词，尽量精炼（如 'Flask SSE 客户端收不到 done 事件'）"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20,
                            "description": "最多返回条数，默认 8"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

WRITE_FILE_SCHEMA: Dict[str, Any] = {
    "name": "write_file",
    "description": "新建文件或整文件覆盖写入（会先备份原文件）。小改动请优先用 edit_file。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区根目录的文件路径"},
            "content": {"type": "string", "description": "完整文件内容"},
            "create_dirs": {"type": "boolean",
                            "description": "父目录不存在时自动创建，默认 true"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
}

EDIT_FILE_SCHEMA: Dict[str, Any] = {
    "name": "edit_file",
    "description": "对文件做精确字符串替换（old_text 必须在文件中唯一匹配，否则报错不猜测）。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区根目录的文件路径"},
            "old_text": {"type": "string",
                         "description": "待替换的原文（含缩进，必须唯一）"},
            "new_text": {"type": "string", "description": "替换后的新文本"},
            "occurrence": {"type": "integer", "minimum": 1,
                           "description": "匹配到多处时指定第几处（不填则要求唯一）"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    },
}

RUN_COMMAND_SCHEMA: Dict[str, Any] = {
    "name": "run_command",
    "description": "在工作区根目录下执行一条终端命令（始终以工作区为 cwd，禁止 cd 逃逸）。",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string",
                        "description": "要执行的命令原文，如 'python -m pytest -q'"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 600,
                        "description": "超时秒数，默认 120"},
            "risk": {"type": "string", "enum": ["auto", "read", "write"],
                     "description": "风险声明；auto 由后端判定"},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}

READ_SKILL_RESOURCE_SCHEMA: Dict[str, Any] = {
    "name": "read_skill_resource",
    "description": "只读获取用户已启用技能包中的参考文档、脚本源代码或文本资源；不会执行脚本或读取任意本机文件。",
    "parameters": {
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "已启用技能的标识"},
            "relative_path": {"type": "string", "description": "包内相对路径，如 references/guide.md"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["skill_id", "relative_path"],
        "additionalProperties": False,
    },
}

#: 发给大模型的完整 tools 数组（OpenAI function calling 格式）
#: 含 web_search；运行时由 build_tool_schemas(web_enabled) 按联网开关裁剪。
TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {"type": "function", "function": schema}
    for schema in (
        READ_FILE_SCHEMA,
        LIST_DIR_SCHEMA,
        SEARCH_CODE_SCHEMA,
        WEB_SEARCH_SCHEMA,
        WRITE_FILE_SCHEMA,
        EDIT_FILE_SCHEMA,
        RUN_COMMAND_SCHEMA,
        READ_SKILL_RESOURCE_SCHEMA,
    )
]

#: 工具名 -> Schema（裸 function 定义，便于校验必填项）
TOOL_SCHEMA_MAP: Dict[str, Dict[str, Any]] = {
    schema["name"]: schema for schema in (
        READ_FILE_SCHEMA,
        LIST_DIR_SCHEMA,
        SEARCH_CODE_SCHEMA,
        WEB_SEARCH_SCHEMA,
        WRITE_FILE_SCHEMA,
        EDIT_FILE_SCHEMA,
        RUN_COMMAND_SCHEMA,
        READ_SKILL_RESOURCE_SCHEMA,
    )
}

#: 联网工具名（关闭联网时要从暴露给模型的工具集里剔除）
WEB_TOOL_NAME = "web_search"

#: 本地工具名（顺序即暴露顺序）
LOCAL_TOOL_NAMES: List[str] = [
    "read_file", "list_dir", "search_code",
    "write_file", "edit_file", "run_command",
]


def build_tool_schemas(web_enabled: bool = True,
                       local_enabled: bool = True,
                       skill_enabled: bool = False) -> List[Dict[str, Any]]:
    """
    按「联网开关」与「是否需要工作区上下文」裁剪发给模型的 tools 数组。

    - 关闭联网时 web_search 完全不出现在工具表里，模型不会调用它，
      因此整个任务期间一个网络请求都不会发出；
    - 判断出本次提问与工作区无关时（如问天气、闲聊），本地文件/目录工具
      也不下发，避免为了一句无关提问就去扫工作区的目录和文件。

    :param web_enabled: 是否允许联网检索
    :param local_enabled: 是否需要本地工作区工具（read_file/list_dir 等）
    :return: OpenAI function calling 格式的 tools 数组
    """
    names: List[str] = []
    if local_enabled:
        names.extend(LOCAL_TOOL_NAMES)
    if web_enabled:
        names.append(WEB_TOOL_NAME)
    if skill_enabled:
        names.append("read_skill_resource")
    return [{"type": "function", "function": TOOL_SCHEMA_MAP[name]}
            for name in names if name in TOOL_SCHEMA_MAP]


#: 旧名 / 常见别名 -> 规范名（模型输出旧名时自动归一）
TOOL_ALIASES: Dict[str, str] = {
    "grep_search": "search_code",
    "search": "search_code",
    "grep": "search_code",
    "replace_in_file": "edit_file",
    "patch_file": "edit_file",
    "create_file": "write_file",
    "ls": "list_dir",
    "cat": "read_file",
    "search_web": "web_search",
    "websearch": "web_search",
    "internet_search": "web_search",
}

#: 工具的中文标签（UI 展示）
TOOL_LABELS: Dict[str, str] = {
    "read_skill_resource": "读取技能资源",
    "read_file": "读取文件",
    "list_dir": "列出目录",
    "search_code": "检索代码",
    "web_search": "联网搜索",
    "write_file": "写入文件",
    "edit_file": "修改文件",
    "run_command": "执行命令",
}

#: 工具的基础风险分级（run_command 实际风险由 approval.classify_command 动态判定）
TOOL_RISK: Dict[str, str] = {
    "read_skill_resource": RISK_READONLY,
    "read_file": RISK_READONLY,
    "list_dir": RISK_READONLY,
    "search_code": RISK_READONLY,
    "web_search": RISK_READONLY,
    "write_file": RISK_WRITE,
    "edit_file": RISK_WRITE,
    "run_command": RISK_UNKNOWN,
}


def tool_names() -> List[str]:
    """返回全部规范工具名"""
    return list(TOOL_SCHEMA_MAP.keys())


def normalize_tool_name(name: Optional[str]) -> Optional[str]:
    """
    把模型输出的工具名归一到规范名。

    :return: 规范名；无法识别时原样返回（交给调用方报 INVALID_ARGUMENT）
    """
    if not name or not isinstance(name, str):
        return None
    key = name.strip()
    if key in TOOL_SCHEMA_MAP:
        return key
    return TOOL_ALIASES.get(key.lower(), key)


def get_tool_schema(name: Optional[str]) -> Optional[Dict[str, Any]]:
    """取某个工具的 function Schema（先做别名归一）"""
    normalized = normalize_tool_name(name)
    if not normalized:
        return None
    return TOOL_SCHEMA_MAP.get(normalized)


def required_args(name: str) -> List[str]:
    """某工具的必填参数列表"""
    schema = get_tool_schema(name)
    if not schema:
        return []
    return list(schema.get("required") or [])


def validate_args(name: str, args: Dict[str, Any]) -> Optional[str]:
    """
    校验参数是否满足 Schema 必填项。

    :return: None 表示通过；否则返回中文错误说明
    """
    if not isinstance(args, dict):
        return "参数 args 必须是一个对象"
    missing = [k for k in required_args(name) if k not in args or args[k] in (None, "")]
    if missing:
        return "缺少必填参数：%s" % "、".join(missing)
    return None


def tool_label(name: Optional[str]) -> str:
    """工具的中文标签（未知名返回原名）"""
    normalized = normalize_tool_name(name)
    return TOOL_LABELS.get(normalized or "", normalized or name or "")


def summarize_args(tool: str, args: Dict[str, Any]) -> str:
    """
    生成一条人类可读的参数摘要（用于工具卡片 / 审批标题）。

    全部内容都来自模型输出，调用方在拼 HTML 时必须转义；
    这里只做长度裁剪，不做任何 HTML 处理。
    """
    if not isinstance(args, dict):
        return ""

    def clip(value: Any, limit: int = 60) -> str:
        text = value if isinstance(value, str) else str(value)
        text = text.replace("\n", " ").strip()
        return text if len(text) <= limit else text[:limit] + "…"

    path = clip(args.get("path", "")) if args.get("path") else ""
    if tool in ("read_file", "write_file", "edit_file"):
        return "%s %s" % (tool_label(tool), path) if path else tool_label(tool)
    if tool == "list_dir":
        return "%s %s" % (tool_label(tool), path or ".")
    if tool == "search_code":
        return '%s "%s"' % (tool_label(tool), clip(args.get("pattern", ""), 40))
    if tool == "web_search":
        return '%s "%s"' % (tool_label(tool), clip(args.get("query", ""), 40))
    if tool == "run_command":
        return "%s：%s" % (tool_label(tool), clip(args.get("command", ""), 80))
    return tool_label(tool)
