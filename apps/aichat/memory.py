# -*- coding: utf-8 -*-
"""
apps/aichat/memory.py — 跨会话长期记忆（Context Memory / Long-term Memory）

职责：
    在「会话内短期记忆」（ai_chat_message 表）之外，提供一层
    「用户级长期记忆」（ai_user_memory 表），让 Aegis 能在不同会话之间
    记住关于用户的事实（姓名、所在地、偏好、职业等），并在每次对话时
    注入到 system 提示词中自然运用。

设计边界：
    · 存储（storage）   ：ai_user_memory，按 (user_id, memory_key) 唯一
    · 读取（read）      ：get_user_memory / get_user_memory_text（按 token 预算裁剪后用于注入）
    · 更新（update）    ：set_user_memory（upsert）；对话后启发式抽取 extract_memory_from_text
    · 清理（cleanup）   ：delete_user_memory / clear_user_memory / prune_user_memory（低重要度 + 过期衰减）
                          + 容量上限保护（max_items，超出按重要度最低、最旧优先淘汰）

说明：
    · 自动抽取默认走「启发式正则」，零额外大模型调用、零成本，契合免费模型偏好；
      如需更丰富抽取可开启 config.memory.auto_extract（预留给 LLM 抽取，默认关闭）。
    · 表结构对 MySQL / SQLite 双适配，ensure_memory_table() 幂等创建。
"""
import re
from datetime import datetime

from apps import query, execute, logger, CONFIG, _detect_db_type

_MEMORY_CFG = CONFIG.get("memory", {}) or {}
ENABLED = bool(_MEMORY_CFG.get("enabled", True))
MAX_ITEMS = int(_MEMORY_CFG.get("max_items", 200))
TTL_DAYS = int(_MEMORY_CFG.get("ttl_days", 180))
LOW_IMP = int(_MEMORY_CFG.get("low_importance_threshold", 1))

# 类别 -> 中文展示标签（同时作为注入文本的前缀）
CATEGORY_LABELS = {
    "name": "姓名",
    "location": "所在地",
    "preference": "偏好",
    "occupation": "职业",
    "project": "项目",
    "general": "其他",
}

# ── 表初始化（幂等，双数据库适配）─────────────────────────
def ensure_memory_table():
    """确保 ai_user_memory 表存在（MySQL / SQLite 各自 DDL）"""
    db = _detect_db_type()
    if db == "mysql":
        execute(
            "CREATE TABLE IF NOT EXISTS ai_user_memory ("
            "  memory_id BIGINT PRIMARY KEY AUTO_INCREMENT,"
            "  user_id INT NOT NULL DEFAULT 1,"
            "  memory_key VARCHAR(64) NOT NULL,"
            "  memory_value TEXT NOT NULL,"
            "  category VARCHAR(32) NOT NULL DEFAULT 'general',"
            "  importance INT NOT NULL DEFAULT 2,"
            "  created_time DATETIME DEFAULT CURRENT_TIMESTAMP,"
            "  updated_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
            "  UNIQUE KEY uk_user_key (user_id, memory_key)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
    else:
        execute(
            "CREATE TABLE IF NOT EXISTS ai_user_memory ("
            "  memory_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  user_id INTEGER NOT NULL DEFAULT 1,"
            "  memory_key VARCHAR(64) NOT NULL,"
            "  memory_value TEXT NOT NULL,"
            "  category VARCHAR(32) NOT NULL DEFAULT 'general',"
            "  importance INTEGER NOT NULL DEFAULT 2,"
            "  created_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),"
            "  updated_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),"
            "  UNIQUE(user_id, memory_key)"
            ")"
        )


# ── 读取 ───────────────────────────────────────────────
def get_user_memory(user_id=1):
    """返回用户全部长期记忆（按重要度降序、更新时间倒序）"""
    ensure_memory_table()
    return query(
        "SELECT memory_key, memory_value, category, importance, updated_time "
        "FROM ai_user_memory WHERE user_id=%s "
        "ORDER BY importance DESC, updated_time DESC",
        (user_id,),
    )


def get_user_memory_text(user_id=1, budget_tokens=None):
    """
    生成用于注入 system 提示词的长期记忆文本（已按 token 预算裁剪）。
    无记忆或功能关闭时返回空字符串。
    """
    if not ENABLED:
        return ""
    rows = get_user_memory(user_id)
    if not rows:
        return ""

    if budget_tokens is None:
        budget_tokens = int(CONFIG.get("llm", {}).get("max_memory_tokens", 600))

    from apps.llm.client import estimate_tokens
    lines = []
    used = estimate_tokens("【用户长期记忆】")
    for r in rows:
        cat = r.get("category") or "general"
        label = CATEGORY_LABELS.get(cat, "其他")
        line = "- {}：{}".format(label, r.get("memory_value") or "")
        t = estimate_tokens(line)
        # 已放不下了就停止（至少保留第一条，避免空块）
        if lines and used + t > budget_tokens:
            break
        lines.append(line)
        used += t

    if not lines:
        return ""
    return ("【用户长期记忆】（跨会话保留，请在回答中自然运用，不要机械复述）\n"
            + "\n".join(lines))


# ── 更新（upsert）────────────────────────────────────────
def set_user_memory(user_id, key, value, category="general", importance=2):
    """写入/更新一条长期记忆（按 key 幂等 upsert）"""
    ensure_memory_table()
    key = (key or "").strip()
    value = (value or "").strip()
    if not key or not value:
        return None
    category = (category or "general").strip() or "general"
    importance = int(importance)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    existing = query(
        "SELECT memory_id FROM ai_user_memory WHERE user_id=%s AND memory_key=%s",
        (user_id, key),
    )
    if existing:
        execute(
            "UPDATE ai_user_memory SET memory_value=%s, category=%s, "
            "importance=%s, updated_time=%s "
            "WHERE user_id=%s AND memory_key=%s",
            (value, category, importance, now, user_id, key),
        )
    else:
        _enforce_cap(user_id)
        execute(
            "INSERT INTO ai_user_memory "
            "(user_id, memory_key, memory_value, category, importance, updated_time) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, key, value, category, importance, now),
        )
    return {"memory_key": key, "memory_value": value,
            "category": category, "importance": importance}


def _enforce_cap(user_id):
    """容量上限保护：超出 max_items 时淘汰「重要度最低 + 最旧」的一条"""
    rows = query("SELECT COUNT(*) AS c FROM ai_user_memory WHERE user_id=%s", (user_id,))
    c = rows[0]["c"] if rows else 0
    if c < MAX_ITEMS:
        return
    execute(
        "DELETE FROM ai_user_memory WHERE user_id=%s "
        "ORDER BY importance ASC, updated_time ASC LIMIT 1",
        (user_id,),
    )


# ── 清理 ───────────────────────────────────────────────
def delete_user_memory(user_id, key):
    """删除单条记忆"""
    ensure_memory_table()
    execute(
        "DELETE FROM ai_user_memory WHERE user_id=%s AND memory_key=%s",
        (user_id, key),
    )
    return True


def clear_user_memory(user_id):
    """清空用户全部记忆"""
    ensure_memory_table()
    execute("DELETE FROM ai_user_memory WHERE user_id=%s", (user_id,))
    return True


def prune_user_memory(user_id=1, ttl_days=None, low_importance=None):
    """
    按策略清理：删除「重要度 <= 阈值 且 超过 TTL 天未更新」的记忆。
    返回被清理的条数。
    """
    ensure_memory_table()
    ttl = TTL_DAYS if ttl_days is None else int(ttl_days)
    low = LOW_IMP if low_importance is None else int(low_importance)
    db = _detect_db_type()

    if db == "mysql":
        before = query(
            "SELECT COUNT(*) AS c FROM ai_user_memory "
            "WHERE user_id=%s AND importance <= %s "
            "AND updated_time < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (user_id, low, ttl),
        )
        execute(
            "DELETE FROM ai_user_memory "
            "WHERE user_id=%s AND importance <= %s "
            "AND updated_time < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (user_id, low, ttl),
        )
    else:
        before = query(
            "SELECT COUNT(*) AS c FROM ai_user_memory "
            "WHERE user_id=? AND importance <= ? "
            "AND updated_time < datetime('now','localtime', ?)",
            (user_id, low, "-{} days".format(ttl)),
        )
        execute(
            "DELETE FROM ai_user_memory "
            "WHERE user_id=? AND importance <= ? "
            "AND updated_time < datetime('now','localtime', ?)",
            (user_id, low, "-{} days".format(ttl)),
        )
    return before[0]["c"] if before else 0


# ── 启发式自动抽取（零成本，对话后调用）──────────────────
# 只匹配高置信度的自我介绍/偏好陈述，避免长文误抽。
_NAME_RE = re.compile(
    r"(?:我叫|我的名字是|我名字叫|本人叫|本人是)\s*"
    r"([一-龥A-Za-z0-9_]{1,12})"
)
_OCC_RE = re.compile(
    r"我是\s*([一-龥A-Za-z0-9_]{1,12})"
    r"(?:工程师|学生|老师|教师|程序员|开发|医生|护士|设计师|产品经理|研究员|律师|会计)"
)
_LOC_RE = re.compile(
    r"(?:我(?:住|家)在|我来自|我在)\s*([一-龥A-Za-z0-9·()（）]{1,20})"
)
_PREF_RE = re.compile(
    r"(?:我喜欢|我的爱好是|我爱好|我偏好|我习惯)\s*([一-龥A-Za-z0-9_，,、]{1,30})"
)


def extract_memory_from_text(text, user_id=1):
    """
    从一条用户消息中启发式抽取可长期保留的事实，并写入记忆。
    仅处理普通短陈述句；疑问句、超长文本直接跳过，避免误抽。
    返回本次新增/更新的 [(category, value, importance), ...]。
    """
    if not ENABLED:
        return []
    text = (text or "").strip()
    if not text or len(text) > 200:
        return []
    if text.rstrip().endswith(("?", "？")):
        return []

    found = []

    def grab(rex, cat, imp):
        m = rex.search(text)
        if m:
            val = m.group(1).strip("，,。、 ")
            if val:
                found.append((cat, val, imp))

    grab(_NAME_RE, "name", 3)
    grab(_OCC_RE, "occupation", 2)
    grab(_LOC_RE, "location", 2)
    grab(_PREF_RE, "preference", 2)

    for cat, val, imp in found:
        try:
            set_user_memory(user_id, cat, val, category=cat, importance=imp)
        except Exception:
            logger.exception("启发式记忆抽取写入失败")
    return found
