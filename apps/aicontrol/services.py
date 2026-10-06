"""
aicontrol 模块 - 服务层
负责合规规则的全量管理业务逻辑
"""
from apps import query, execute, execute_many


def get_all_rules():
    """查询全部启用规则，按 sort_no 排序"""
    return query(
        "SELECT id, rule_content, rule_status, sort_no, "
        "created_time, updated_time "
        "FROM ai_user_message_rule ORDER BY sort_no ASC, id ASC"
    )


def save_rules(rules):
    """
    批量保存全量规则（先清空再写入，保证前后端数据一致）
    :param rules: list[dict]  每项含 rule_content, rule_status, sort_no
    """
    conn = None
    try:
        from apps import get_db
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_user_message_rule")
            if rules:
                values = [
                    (r.get("rule_content", ""),
                     int(r.get("rule_status", 1)),
                     int(r.get("sort_no", i + 1)))
                    for i, r in enumerate(rules)
                ]
                cur.executemany(
                    "INSERT INTO ai_user_message_rule "
                    "(rule_content, rule_status, sort_no) VALUES (%s, %s, %s)",
                    values
                )
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def check_compliance(user_message):
    """
    合规校验：检查用户消息是否包含任何规则关键词
    :param user_message: 用户输入文本
    :return: (是否合规, 命中的规则内容)
    """
    rules = query(
        "SELECT rule_content FROM ai_user_message_rule WHERE rule_status = 1"
    )
    for rule in rules:
        keyword = rule["rule_content"].strip()
        if keyword and keyword in user_message:
            return False, keyword
    return True, None
