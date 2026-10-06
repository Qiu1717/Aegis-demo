"""
apps 包初始化模块
提供全局配置加载、数据库连接、统一响应等共享工具
数据库支持 MySQL（生产/Docker）和 SQLite（本地开发回退）
"""
import os
import re
import json
import logging
import pymysql
import sqlite3

from . import paths

# ── 日志 ──────────────────────────────────────────────────
# 约定：异常详情只写日志，绝不回传给前端（PRD 8.3 / PDD 8.4 安全约束）
logger = logging.getLogger("aiagent")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

# ── 配置加载 ──────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_BASE_DIR, "config", "config.json")

#: 用户级配置（%APPDATA%\Aegis\config.user.json 等），优先级高于仓库默认配置
_USER_CONFIG_PATH = str(paths.user_config_path())


def _load_dotenv(path=None):
    """
    读取项目根目录的 .env 并注入环境变量（不引入第三方依赖）。
    系统里已存在的同名环境变量优先，方便临时覆盖。
    """
    dotenv = path or os.path.join(_BASE_DIR, ".env")
    if not os.path.isfile(dotenv):
        return
    loaded = 0
    try:
        with open(dotenv, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
                    loaded += 1
        if loaded:
            print(f"[ENV] 已从 .env 载入 {loaded} 项环境变量", flush=True)
    except OSError as e:
        print(f"[WARN] 读取 .env 失败：{e}")


def _read_json_file(path):
    """安全读取 JSON 文件；不存在或损坏时返回 None（不抛异常）"""
    try:
        if not path or not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError) as e:
        print(f"[WARN] 读取配置 {path} 失败：{e}")
        return None


def _deep_merge(base, override):
    """
    把 override 深合并进 base（override 优先）。
    注意：原地修改并返回 base，保证 CONFIG 的引用不被替换。
    """
    if not isinstance(base, dict) or not isinstance(override, dict):
        return base
    for key, value in override.items():
        old = base.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            _deep_merge(old, value)
        else:
            base[key] = value
    return base


def _normalize_user_cfg(user_cfg):
    """
    兼容两种用户配置写法：
      A) {"llm": {...}}                                  与 config.json 同构（推荐）
      B) {"providers": {...}, "provider_chain": [...]}   顶层扁平写法（设计 §6.1.1）
    B 会被搬到 llm 段下，保证深合并后 CONFIG["llm"] 生效。
    """
    if not isinstance(user_cfg, dict):
        return {}

    llm = user_cfg.get("llm")
    llm = dict(llm) if isinstance(llm, dict) else {}

    moved = False
    for top_key in ("providers", "provider_chain", "active_provider"):
        value = user_cfg.get(top_key)
        if value is None or top_key in llm:
            continue
        if isinstance(value, (dict, list, str)):
            llm[top_key] = value
            moved = True

    if not moved:
        return user_cfg

    normalized = dict(user_cfg)
    normalized["llm"] = llm
    return normalized


def load_config():
    """
    读取配置：config.json（仓库默认）← config.user.json（用户级，优先）深合并，
    再用环境变量注入数据库等敏感字段。
    """
    cfg = _read_json_file(_CONFIG_PATH) or {}
    user_cfg = _normalize_user_cfg(_read_json_file(_USER_CONFIG_PATH) or {})
    if user_cfg:
        cfg = _deep_merge(cfg, user_cfg)

    llm_cfg = cfg.setdefault("llm", {})
    providers_cfg = llm_cfg.setdefault("providers", {})

    # ── 自定义端点：LLM_BASE_URL + LLM_API_KEY 可临时接管全部请求 ──
    env_base = os.environ.get("LLM_BASE_URL", "").strip()
    env_key = os.environ.get("LLM_API_KEY", "").strip()
    if env_base and env_key:
        custom = providers_cfg.setdefault("custom", {})
        custom.update({"enabled": True, "base_url": env_base, "api_key": env_key})
        if os.environ.get("LLM_MODEL", "").strip():
            custom["model"] = os.environ["LLM_MODEL"].strip()
        chain = llm_cfg.get("provider_chain") or []
        if "custom" not in chain:
            llm_cfg["provider_chain"] = ["custom"] + list(chain)

    # ── 旧版本遗留变量提示 ──
    if os.environ.get("DOUBAO_API_KEY"):
        print("[WARN] DOUBAO_API_KEY 已废弃，请改用 ZHIPU_API_KEY / "
              "MODELSCOPE_API_KEY / SILICONFLOW_API_KEY 等变量，"
              "详见 docs/大模型配置指南.md")

    # ── 数据库 ──
    if os.environ.get("DB_HOST"):
        cfg["database"]["host"] = os.environ["DB_HOST"]
    if os.environ.get("DB_PORT"):
        cfg["database"]["port"] = int(os.environ["DB_PORT"])
    if os.environ.get("DB_USER"):
        cfg["database"]["user"] = os.environ["DB_USER"]
    if os.environ.get("DB_PASSWORD"):
        cfg["database"]["password"] = os.environ["DB_PASSWORD"]
    if os.environ.get("DB_NAME"):
        cfg["database"]["database"] = os.environ["DB_NAME"]

    return cfg


# 先加载 .env（本地开发用），再读取配置
_load_dotenv()
CONFIG = load_config()


def reload_config():
    """
    重新加载配置并**原地**更新全局 CONFIG。

    必须用 clear()+update 而不是重新赋值：大量模块已经 `from apps import CONFIG`
    持有了这个字典的引用，重新绑定变量会让旧引用全部失效（设计 §12.6）。

    随后重置 LLM 客户端，使「保存即生效」真正生效。
    """
    try:
        new_cfg = load_config()
    except Exception:
        logger.exception("重新加载配置失败，继续沿用当前配置")
        return CONFIG

    CONFIG.clear()
    CONFIG.update(new_cfg)

    try:
        from . import llm as _llm
        _llm.reset_client()
    except Exception:
        logger.exception("重置大模型客户端失败")
    return CONFIG


# ── 日志系统（滚动文件 + 密钥脱敏，幂等）────────────────────
try:
    from . import logsetup
    logsetup.setup()
except Exception:  # 日志初始化失败绝不阻断启动
    logger.exception("初始化日志系统失败，仅使用控制台输出")

# ── 数据库类型探测 ─────────────────────────────────────────
_DB_TYPE = None  # "mysql" | "sqlite"


def _detect_db_type():
    """自动探测可用数据库：优先 MySQL，回退 SQLite"""
    global _DB_TYPE
    if _DB_TYPE:
        return _DB_TYPE

    db_cfg = CONFIG.get("database", {})
    forced = db_cfg.get("db_type", "auto")

    if forced == "sqlite":
        _DB_TYPE = "sqlite"
    elif forced == "mysql":
        _DB_TYPE = "mysql"
    else:
        # auto: 尝试连接 MySQL
        try:
            conn = pymysql.connect(
                host=db_cfg.get("host", "localhost"),
                port=db_cfg.get("port", 3306),
                user=db_cfg.get("user", "root"),
                password=db_cfg.get("password", ""),
                database=db_cfg.get("database", "aiagent"),
                connect_timeout=3,
            )
            conn.close()
            _DB_TYPE = "mysql"
            print(f"[DB] 使用 MySQL ({db_cfg.get('host')}:{db_cfg.get('port')})")
        except Exception:
            _DB_TYPE = "sqlite"
            print("[DB] MySQL 不可用，回退到 SQLite（开发模式）")
    return _DB_TYPE


def _convert_sql(sql):
    """将 MySQL 风格 SQL (%s 占位符) 转换为 SQLite 风格 (? 占位符)"""
    return re.sub(r"%s", "?", sql)


_SQLITE_INITIALIZED = False


def sqlite_path():
    """
    SQLite 数据库文件路径。
    桌面端一律落用户数据目录（%APPDATA%\\Aegis\\aegis.db），安装目录保持只读；
    取不到用户目录时退回代码包内，保证老行为不崩。
    """
    try:
        return str(paths.db_path())
    except Exception:
        return os.path.join(_BASE_DIR, "aiagent.db")


def _init_sqlite():
    """初始化 SQLite 数据库表结构（仅首次运行时执行一次）"""
    global _SQLITE_INITIALIZED
    if _SQLITE_INITIALIZED:
        return
    _SQLITE_INITIALIZED = True
    try:
        paths.ensure_dirs()
    except Exception:
        pass
    db_path = sqlite_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS ai_user_message_rule (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_content  TEXT    NOT NULL,
        rule_status   INTEGER NOT NULL DEFAULT 1,
        sort_no       INTEGER NOT NULL DEFAULT 0,
        created_time  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        updated_time  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS ai_chat_session (
        session_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id           INTEGER NOT NULL DEFAULT 1,
        session_title     TEXT    NOT NULL DEFAULT '新对话',
        created_time      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        is_pinned         INTEGER NOT NULL DEFAULT 0,
        last_message_time TEXT,
        updated_time      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS ai_chat_message (
        message_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      INTEGER NOT NULL,
        user_id          INTEGER NOT NULL DEFAULT 1,
        message_role    TEXT    NOT NULL,
        message_type    TEXT    NOT NULL DEFAULT 'text',
        message_content TEXT,
        sequence_no     INTEGER NOT NULL DEFAULT 0,
        created_time    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (session_id) REFERENCES ai_chat_session(session_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS ai_chat_message_attachment (
        attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id    INTEGER NOT NULL,
        file_name     TEXT,
        file_ext      TEXT,
        file_type     TEXT,
        file_size     INTEGER NOT NULL DEFAULT 0,
        file_url      TEXT,
        sort_no       INTEGER NOT NULL DEFAULT 0,
        created_time  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (message_id) REFERENCES ai_chat_message(message_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS ai_user_memory (
        memory_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL DEFAULT 1,
        memory_key   VARCHAR(64) NOT NULL,
        memory_value TEXT NOT NULL,
        category     VARCHAR(32) NOT NULL DEFAULT 'general',
        importance   INTEGER NOT NULL DEFAULT 2,
        created_time TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        updated_time TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(user_id, memory_key)
    );
    """)

    # 插入初始数据（仅当规则表为空时）
    cur.execute("SELECT COUNT(*) as c FROM ai_user_message_rule")
    if cur.fetchone()[0] == 0:
        rules = [
            ("密码", 1, 1), ("身份证号", 1, 2), ("银行卡号", 1, 3),
            ("如何破解", 1, 4), ("如何攻击", 1, 5), ("如何黑入", 1, 6),
            ("制造炸弹", 1, 7), ("毒品", 1, 8), ("赌博", 1, 9), ("色情", 1, 10),
        ]
        cur.executemany(
            "INSERT INTO ai_user_message_rule (rule_content, rule_status, sort_no) VALUES (?,?,?)",
            rules,
        )

    # 插入示例会话（使用 lastrowid 而非硬编码 ID，避免 AUTOINCREMENT 偏移导致外键失败）
    cur.execute("SELECT COUNT(*) as c FROM ai_chat_session")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO ai_chat_session (session_title, is_pinned) VALUES (?, ?)",
            ("欢迎使用 Aegis", 1),
        )
        welcome_sid = cur.lastrowid
        cur.execute(
            "INSERT INTO ai_chat_message (session_id, message_role, message_type, message_content, sequence_no) "
            "VALUES (?, 'assistant', 'text', ?, 1)",
            (welcome_sid, "你好！我是 Aegis，有什么可以帮你的吗？"),
        )

    # ── 兼容迁移：ai_chat_message.metadata（旧库没有这一列）──
    # SQLite 不支持 ADD COLUMN IF NOT EXISTS，必须先查 PRAGMA（设计 §12.7）
    cur.execute("PRAGMA table_info(ai_chat_message)")
    columns = set()
    for row in cur.fetchall():
        try:
            columns.add(row[1])
        except (TypeError, IndexError):
            continue
    if "metadata" not in columns:
        cur.execute("ALTER TABLE ai_chat_message ADD COLUMN metadata TEXT")

    conn.commit()
    conn.close()


# ── 数据库连接 ────────────────────────────────────────────
def get_db():
    """获取数据库连接（MySQL 或 SQLite）"""
    db_type = _detect_db_type()
    db_cfg = CONFIG.get("database", {})

    if db_type == "mysql":
        return pymysql.connect(
            host=db_cfg["host"],
            port=db_cfg["port"],
            user=db_cfg["user"],
            password=db_cfg["password"],
            database=db_cfg["database"],
            charset=db_cfg.get("charset", "utf8mb4"),
            cursorclass=pymysql.cursors.DictCursor,
        )
    else:
        _init_sqlite()
        db_path = sqlite_path()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # 包装 cursor 使其 fetchall/fetchone 返回 dict
        return _DictCursorWrapper(conn)


class _DictCursorWrapper:
    """包装 sqlite3 连接，提供与 pymysql DictCursor 一致的接口。
    同时充当 connection 和 cursor，支持 with 语句。"""
    def __init__(self, conn):
        self._conn = conn
        self._cursor = conn.cursor()

    # ── connection 角色 ──
    def cursor(self):
        return self

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    # ── cursor 角色 ──
    def execute(self, sql, args=None):
        sql = _convert_sql(sql)
        if args is not None:
            self._cursor.execute(sql, args)
        else:
            self._cursor.execute(sql)
        return self._cursor

    def executemany(self, sql, args):
        sql = _convert_sql(sql)
        self._cursor.executemany(sql, args)
        return self._cursor

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [dict(r) for r in rows] if rows else []

    def fetchone(self):
        row = self._cursor.fetchone()
        return dict(row) if row else None

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    # ── 上下文管理器 ──
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


# ── 统一查询/执行函数 ─────────────────────────────────────
def query(sql, args=None):
    """执行查询，返回 dict 列表"""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def execute(sql, args=None):
    """执行写操作，返回 lastrowid"""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def execute_many(sql, args):
    """批量执行写操作"""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, args)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── 统一响应格式 ───────────────────────────────────────────
def api_ok(data=None, msg="success"):
    from flask import jsonify
    return jsonify({"code": 0, "msg": msg, "data": data})


def api_fail(msg="error", code=1, data=None):
    from flask import jsonify
    return jsonify({"code": code, "msg": msg, "data": data})
