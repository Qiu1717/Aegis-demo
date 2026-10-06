-- ============================================================
-- Aegis (aegis) 数据库初始化脚本
-- 数据库: MySQL 8.0+
-- 字符集: utf8mb4
-- ============================================================

CREATE DATABASE IF NOT EXISTS aiagent
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE aiagent;

-- ============================================================
-- 1. 用户消息规则表 (ai_user_message_rule)
-- ============================================================
DROP TABLE IF EXISTS ai_user_message_rule;
CREATE TABLE ai_user_message_rule (
    id            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '规则记录ID',
    rule_content  VARCHAR(2000) NOT NULL COMMENT '违规提问关键词/语句规则',
    rule_status   TINYINT      NOT NULL DEFAULT 1     COMMENT '1-启用 0-禁用',
    sort_no       INT          NOT NULL DEFAULT 0      COMMENT '排序序号，越小优先级越高',
    created_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (id),
    INDEX idx_rule_status (rule_status),
    INDEX idx_sort_no (sort_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户消息规则表';

-- ============================================================
-- 2. AI会话表 (ai_chat_session)
-- ============================================================
DROP TABLE IF EXISTS ai_chat_session;
CREATE TABLE ai_chat_session (
    session_id        BIGINT       NOT NULL AUTO_INCREMENT COMMENT '会话ID',
    user_id           BIGINT       NOT NULL DEFAULT 1     COMMENT '用户ID，当前单用户默认1',
    session_title     VARCHAR(255) NOT NULL DEFAULT '新对话' COMMENT '会话标题',
    created_time      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '会话创建时间',
    is_pinned         TINYINT      NOT NULL DEFAULT 0      COMMENT '1-置顶 0-未置顶',
    last_message_time DATETIME     NULL                    COMMENT '最后消息时间',
    updated_time      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (session_id),
    INDEX idx_user_id (user_id),
    INDEX idx_is_pinned (is_pinned),
    INDEX idx_last_message_time (last_message_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI会话表';

-- ============================================================
-- 3. AI会话消息记录表 (ai_chat_message)
-- ============================================================
DROP TABLE IF EXISTS ai_chat_message;
CREATE TABLE ai_chat_message (
    message_id      BIGINT       NOT NULL AUTO_INCREMENT COMMENT '消息ID',
    session_id      BIGINT       NOT NULL                COMMENT '所属会话ID',
    user_id         BIGINT       NOT NULL DEFAULT 1      COMMENT '用户ID',
    message_role    VARCHAR(20)  NOT NULL                COMMENT 'system/user/assistant/tool',
    message_type    VARCHAR(20)  NOT NULL DEFAULT 'text'  COMMENT 'text/image/file，默认text',
    message_content LONGTEXT                              COMMENT '消息内容',
    sequence_no     INT          NOT NULL DEFAULT 0       COMMENT '会话内消息排序序号',
    created_time    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '消息发送时间',
    PRIMARY KEY (message_id),
    INDEX idx_session_id (session_id),
    INDEX idx_sequence_no (session_id, sequence_no),
    CONSTRAINT fk_message_session FOREIGN KEY (session_id)
        REFERENCES ai_chat_session (session_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI会话消息记录表';

-- ============================================================
-- 4. AI会话消息附件表 (ai_chat_message_attachment) - 预留
-- ============================================================
DROP TABLE IF EXISTS ai_chat_message_attachment;
CREATE TABLE ai_chat_message_attachment (
    attachment_id BIGINT       NOT NULL AUTO_INCREMENT COMMENT '附件ID',
    message_id    BIGINT       NOT NULL                COMMENT '关联消息ID',
    file_name     VARCHAR(255)                          COMMENT '文件名',
    file_ext      VARCHAR(50)                           COMMENT '文件后缀',
    file_type     VARCHAR(100)                          COMMENT '文件类型',
    file_size     BIGINT       NOT NULL DEFAULT 0       COMMENT '文件大小(字节)',
    file_url      VARCHAR(500)                          COMMENT '文件存储地址',
    sort_no       INT          NOT NULL DEFAULT 0       COMMENT '附件排序序号',
    created_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '附件上传时间',
    PRIMARY KEY (attachment_id),
    INDEX idx_message_id (message_id),
    CONSTRAINT fk_attachment_message FOREIGN KEY (message_id)
        REFERENCES ai_chat_message (message_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI会话消息附件表';

-- ============================================================
-- 初始化合规规则数据
-- ============================================================
INSERT INTO ai_user_message_rule (rule_content, rule_status, sort_no) VALUES
('密码', 1, 1),
('身份证号', 1, 2),
('银行卡号', 1, 3),
('如何破解', 1, 4),
('如何攻击', 1, 5),
('如何黑入', 1, 6),
('制造炸弹', 1, 7),
('毒品', 1, 8),
('赌博', 1, 9),
('色情', 1, 10);

-- ============================================================
-- 初始化示例会话数据（可选，便于演示）
-- ============================================================
INSERT INTO ai_chat_session (session_id, user_id, session_title, is_pinned) VALUES
(1, 1, '欢迎使用 Aegis', 1);
INSERT INTO ai_chat_message (session_id, user_id, message_role, message_type, message_content, sequence_no) VALUES
(1, 1, 'assistant', 'text', '你好！我是 Aegis，有什么可以帮助你的吗？你可以问我任何问题，我会尽力为你解答。', 1);
