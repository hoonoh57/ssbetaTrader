=============================================================================
-- 분봉 데이터 전용 DB
=============================================================================
CREATE DATABASE IF NOT EXISTS stock_minutes
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE stock_minutes;

-- ─────────────────────────────────────────────────────────────────────────
-- 분봉 캔들 테이블 (월별 파티션)
-- 5분봉 기준 6개월 8종목 ≈ 78,000행 / 1분봉까지 ≈ 390,000행
-- 파티션으로 조회 속도 최적화 + 오래된 데이터 DROP PARTITION으로 즉시 삭제
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE `minute_candles` (
  `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `code`        CHAR(6)         NOT NULL             COMMENT '종목코드',
  `tick`        TINYINT UNSIGNED NOT NULL DEFAULT 5   COMMENT '분봉단위 1,3,5,10,15,30,60',
  `dt`          DATETIME        NOT NULL             COMMENT '캔들 시작 시각',
  `open`        INT             NOT NULL,
  `high`        INT             NOT NULL,
  `low`         INT             NOT NULL,
  `close`       INT             NOT NULL,
  `volume`      BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `trade_value` BIGINT UNSIGNED DEFAULT 0            COMMENT '거래대금(백만원)',
  `change_pct`  DECIMAL(6,2)    DEFAULT NULL         COMMENT '전봉대비 등락률(%)',
  `created_at`  TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`, `dt`),
  UNIQUE KEY `uk_candle` (`code`, `tick`, `dt`),
  KEY `idx_code_dt`  (`code`, `dt`),
  KEY `idx_dt`       (`dt`),
  KEY `idx_code_tick`(`code`, `tick`, `dt`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
PARTITION BY RANGE (TO_DAYS(`dt`)) (
  PARTITION p202509 VALUES LESS THAN (TO_DAYS('2025-10-01')),
  PARTITION p202510 VALUES LESS THAN (TO_DAYS('2025-11-01')),
  PARTITION p202511 VALUES LESS THAN (TO_DAYS('2025-12-01')),
  PARTITION p202512 VALUES LESS THAN (TO_DAYS('2026-01-01')),
  PARTITION p202601 VALUES LESS THAN (TO_DAYS('2026-02-01')),
  PARTITION p202602 VALUES LESS THAN (TO_DAYS('2026-03-01')),
  PARTITION p202603 VALUES LESS THAN (TO_DAYS('2026-04-01')),
  PARTITION p202604 VALUES LESS THAN (TO_DAYS('2026-05-01')),
  PARTITION p202605 VALUES LESS THAN (TO_DAYS('2026-06-01')),
  PARTITION p202606 VALUES LESS THAN (TO_DAYS('2026-07-01')),
  PARTITION p202607 VALUES LESS THAN (TO_DAYS('2026-08-01')),
  PARTITION p202608 VALUES LESS THAN (TO_DAYS('2026-09-01')),
  PARTITION p202609 VALUES LESS THAN (TO_DAYS('2026-10-01')),
  PARTITION p202610 VALUES LESS THAN (TO_DAYS('2026-11-01')),
  PARTITION p202611 VALUES LESS THAN (TO_DAYS('2026-12-01')),
  PARTITION p202612 VALUES LESS THAN (TO_DAYS('2027-01-01')),
  PARTITION p_future VALUES LESS THAN MAXVALUE
);


-- ─────────────────────────────────────────────────────────────────────────
-- 다운로드 진행 상태 추적 테이블
-- 연속조회 중단점 기록 → 재시작 시 이어서 다운로드
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE `download_progress` (
  `id`            INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `code`          CHAR(6)      NOT NULL,
  `tick`          TINYINT UNSIGNED NOT NULL,
  `last_datetime` DATETIME     NOT NULL         COMMENT '마지막 수신 캔들 시각',
  `total_rows`    INT UNSIGNED DEFAULT 0        COMMENT '누적 저장 행수',
  `status`        ENUM('running','done','error') DEFAULT 'running',
  `started_at`    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  `finished_at`   TIMESTAMP    NULL,
  `error_msg`     TEXT         NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_progress` (`code`, `tick`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─────────────────────────────────────────────────────────────────────────
-- 백테스트 결과 저장 테이블
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE `backtest_results` (
  `id`              INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `strategy_name`   VARCHAR(100) NOT NULL,
  `run_date`        DATETIME     NOT NULL,
  `period_start`    DATE         NOT NULL,
  `period_end`      DATE         NOT NULL,
  `tick`            TINYINT UNSIGNED NOT NULL,
  `total_trades`    INT          DEFAULT 0,
  `win_count`       INT          DEFAULT 0,
  `loss_count`      INT          DEFAULT 0,
  `win_rate`        DECIMAL(5,2) DEFAULT 0,
  `total_pnl_pct`   DECIMAL(8,2) DEFAULT 0,
  `max_drawdown`    DECIMAL(8,2) DEFAULT 0,
  `avg_hold_minutes` INT         DEFAULT 0,
  `sharpe_ratio`    DECIMAL(6,3) DEFAULT NULL,
  `params_json`     JSON         NULL             COMMENT '전략 파라미터',
  `trades_json`     JSON         NULL             COMMENT '매매 상세 기록',
  PRIMARY KEY (`id`),
  KEY `idx_strategy` (`strategy_name`, `run_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─────────────────────────────────────────────────────────────────────────
-- 분석 패턴 저장 테이블
-- "삼성전자 정배열 확정 후 하나마이크론 골든크로스까지 평균 8.3분" 등
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE `sync_patterns` (
  `id`              INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `signal_code`     CHAR(6)      NOT NULL         COMMENT '신호 종목(삼성전자)',
  `target_code`     CHAR(6)      NOT NULL         COMMENT '대상 종목',
  `pattern_type`    VARCHAR(50)  NOT NULL         COMMENT 'aligned_to_golden, peak_lag 등',
  `avg_lag_minutes`  DECIMAL(6,1) DEFAULT NULL    COMMENT '평균 시차(분)',
  `median_lag_minutes` DECIMAL(6,1) DEFAULT NULL,
  `std_lag_minutes`  DECIMAL(6,1) DEFAULT NULL,
  `avg_beta`        DECIMAL(5,2) DEFAULT NULL     COMMENT '평균 변동 배율',
  `sample_count`    INT          DEFAULT 0,
  `win_rate`        DECIMAL(5,2) DEFAULT NULL,
  `avg_pnl`         DECIMAL(6,2) DEFAULT NULL,
  `computed_at`     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  `detail_json`     JSON         NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_pattern` (`signal_code`, `target_code`, `pattern_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
