-- Test schema for integration tests with real MySQL (via docker-compose mysql-dev)
CREATE DATABASE IF NOT EXISTS mydb;
USE mydb;

CREATE TABLE IF NOT EXISTS loyalty_members (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    reward_tier VARCHAR(50) DEFAULT 'bronze',
    points INT NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_user_id (user_id),
    INDEX idx_reward_tier (reward_tier)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS users (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(100) NOT NULL,
    email VARCHAR(255) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_username (username),
    UNIQUE INDEX idx_email (email)
) ENGINE=InnoDB;

-- Sample data
INSERT IGNORE INTO users (id, username, email) VALUES
    (1, 'retailer1', 'r1@example.com'),
    (2, 'retailer2', 'r2@example.com'),
    (3, 'retailer3', 'r3@example.com');

INSERT IGNORE INTO loyalty_members (id, user_id, reward_tier, points) VALUES
    (1, 1, 'gold', 500),
    (2, 2, 'bronze', 100),
    (3, 3, 'silver', 250);
