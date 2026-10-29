-- ============================================================
-- AI 智能工单处理平台 —— 建表脚本（MySQL 8.0）
--
-- 【重要】这个脚本是"参考 / 手动初始化"用的。
--         正常启动服务时，app/core/database.py 的 init_database() 会自动：
--             1) CREATE DATABASE IF NOT EXISTS ai_ticket
--             2) Base.metadata.create_all()   ← 按 app/models/ 建表
--             3) dev 环境灌入演示数据（见 app/repositories/seed.py）
--         也就是说：**你不需要手动执行这个文件**，服务一启动表就有了。
--         除非你想手工建库、或者拿去给 DBA 审核，才用它。
--
-- 手动执行（PowerShell 里不要用 "<" 重定向）：
--     mysql -u root -p -e "source sql/schema.sql"
--
-- 注意：表和字段必须与 app/models/ 保持一致，改了模型记得同步改这里。
-- ============================================================

CREATE DATABASE IF NOT EXISTS ai_ticket
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE ai_ticket;

-- ------------------------------------------------------------
-- users
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            BIGINT       PRIMARY KEY AUTO_INCREMENT,
    username      VARCHAR(64)  NOT NULL,
    password_hash VARCHAR(255) NULL COMMENT 'PBKDF2 哈希，格式 pbkdf2_sha256$迭代$salt$hash',
    email         VARCHAR(128) NULL,
    phone         VARCHAR(32)  NULL,
    created_at    DATETIME     NOT NULL,
    updated_at    DATETIME     NOT NULL,

    UNIQUE KEY uk_users_username (username)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COMMENT = '用户表';

-- ------------------------------------------------------------
-- orders
--   amount 用 DECIMAL(10,2)：钱不能用浮点数存
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id         BIGINT        PRIMARY KEY AUTO_INCREMENT,
    order_no   VARCHAR(64)   NOT NULL,
    user_id    BIGINT        NOT NULL,
    amount     DECIMAL(10,2) NOT NULL,
    status     VARCHAR(32)   NOT NULL
               COMMENT 'PENDING_PAYMENT / PAID / SHIPPED / DELIVERED / CANCELLED / REFUNDED',
    created_at DATETIME      NOT NULL,
    updated_at DATETIME      NOT NULL,

    UNIQUE KEY uk_orders_order_no (order_no),
    KEY idx_orders_user_id (user_id),
    KEY idx_orders_status (status)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COMMENT = '订单表';

-- ------------------------------------------------------------
-- logistics
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS logistics (
    id               BIGINT       PRIMARY KEY AUTO_INCREMENT,
    order_id         BIGINT       NOT NULL,
    company          VARCHAR(64)  NULL,
    tracking_no      VARCHAR(128) NULL,
    status           VARCHAR(32)  NULL
                     COMMENT 'PENDING / IN_TRANSIT / DELIVERED / EXCEPTION',
    last_update_time DATETIME     NULL,
    created_at       DATETIME     NOT NULL,
    updated_at       DATETIME     NOT NULL,

    KEY idx_logistics_order_id (order_id)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COMMENT = '物流表';

-- ------------------------------------------------------------
-- tickets
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickets (
    id           BIGINT       PRIMARY KEY AUTO_INCREMENT,
    user_id      BIGINT       NOT NULL,
    title        VARCHAR(255) NULL,
    content      TEXT         NOT NULL,
    type         VARCHAR(32)  NULL,
    priority     VARCHAR(32)  NULL,
    status       VARCHAR(32)  NOT NULL DEFAULT 'OPEN'
                 COMMENT 'OPEN / PROCESSING / WAITING_HUMAN / RESOLVED / CLOSED',
    agent_result TEXT         NULL COMMENT 'Agent 的处理结论',
    resolution   TEXT         NULL COMMENT '结单说明',
    created_at   DATETIME     NOT NULL,
    updated_at   DATETIME     NOT NULL,

    KEY idx_tickets_user_id (user_id),
    KEY idx_tickets_status (status)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COMMENT = '工单表';

-- ------------------------------------------------------------
-- refund_records
--   approved_by: NULL=还没人工审批  0=系统自动通过  其它=审批人 user_id
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refund_records (
    id          BIGINT        PRIMARY KEY AUTO_INCREMENT,
    order_id    BIGINT        NOT NULL,
    ticket_id   BIGINT        NOT NULL,
    amount      DECIMAL(10,2) NOT NULL,
    status      VARCHAR(32)   NOT NULL
                COMMENT 'PENDING / APPROVED / REJECTED / COMPLETED',
    reason      TEXT          NULL,
    approved_by BIGINT        NULL,
    created_at  DATETIME      NOT NULL,
    updated_at  DATETIME      NOT NULL,

    KEY idx_refund_records_order_id (order_id),
    KEY idx_refund_records_ticket_id (ticket_id),
    KEY idx_refund_records_status (status)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COMMENT = '退款记录表';

-- ------------------------------------------------------------
-- 演示数据（正常由 app/repositories/seed.py 在 dev 环境自动写入，
--           这里只作为参考，方便你手工建库时也能有数据可测）
-- ------------------------------------------------------------
INSERT INTO users (id, username, email, phone, created_at, updated_at)
VALUES (1, 'demo_user', 'demo@example.com', '13800000000', NOW(), NOW()),
       (2, 'another_user', 'another@example.com', '13900000000', NOW(), NOW())
ON DUPLICATE KEY UPDATE username = VALUES(username);
