"""
=============================================================================
극세밀 전략 엔진 — sync_patterns 테이블의 분석 결과를 실전에 반영
=============================================================================
기존 SyncTradeEngine을 상속하여, DB에 저장된 패턴 통계로
종목별 최적 진입 타이밍/배율을 동적으로 적용
=============================================================================
"""

import mysql.connector
import json
import os
from typing import Dict, List, Tuple
from dataclasses import dataclass
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# 종목명 매핑
STOCK_NAMES = {
    "005930": "삼성전자", "005935": "삼성전자우", "067310": "하나마이크론",
    "131970": "두산테스나", "356860": "티엘비", "240810": "원익IPS",
    "095340": "ISC", "330860": "네패스아크",
}


@dataclass
class StockPattern:
    """DB에서 로드한 종목별 패턴 통계"""
    code: str
    name: str
    avg_lag_minutes: float = 0       # 골든크로스까지 평균 시차
    median_lag_minutes: float = 0
    avg_beta: float = 1.0            # 평균 변동 배율
    correlation: float = 0           # 삼성전자와의 상관계수
    same_direction_pct: float = 0    # 동방향 확률(%)
    win_rate: float = 0              # 골든크로스 진입 시 승률
    avg_pnl: float = 0              # 평균 수익률
    response_rate: float = 0         # 급등 시 반응률
    avg_response_lag: float = 0      # 급등 시 평균 반응 시간


class PrecisionEngine:
    """
    극세밀 전략 엔진

    DB의 sync_patterns 분석 결과를 활용하여:
    1) 종목별 최적 진입 대기 시간 (avg_lag 활용)
    2) 동적 포지션 사이징 (beta × correlation 기반)
    3) 시간대별 매매 온/오프
    4) 실시간 승률 기반 종목 선별
    """

    def __init__(self, mysql_config: dict):
        self.patterns: Dict[str, StockPattern] = {}
        self.mysql_config = mysql_config
        self._load_patterns()

    def _load_patterns(self):
        """DB에서 분석된 패턴 로드"""
        conn = mysql.connector.connect(**self.mysql_config)
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT target_code, pattern_type,
                   avg_lag_minutes, median_lag_minutes, avg_beta,
                   sample_count, win_rate, avg_pnl, detail_json
            FROM sync_patterns
            WHERE signal_code = '005930'
            ORDER BY target_code, pattern_type
        """)

        for row in cursor.fetchall():
            code = row["target_code"]
            ptype = row["pattern_type"]

            if code not in self.patterns:
                self.patterns[code] = StockPattern(
                    code=code,
                    name=STOCK_NAMES.get(code, code)
                )

            p = self.patterns[code]
            detail = json.loads(row["detail_json"]) if row["detail_json"] else {}

            if ptype == "aligned_to_golden":
                p.avg_lag_minutes = row["avg_lag_minutes"] or 0
                p.median_lag_minutes = row["median_lag_minutes"] or 0
                p.win_rate = row["win_rate"] or 0
                p.avg_pnl = row["avg_pnl"] or 0

            elif ptype == "daily_beta":
                p.avg_beta = row["avg_beta"] or 1.0
                p.correlation = detail.get("correlation", 0)
                p.same_direction_pct = detail.get("same_direction_pct", 0)

            elif ptype == "intraday_response":
                p.response_rate = detail.get("response_rate", 0)
                p.avg_response_lag = detail.get("overall_avg_lag", 0)

        conn.close()

        # 로드 결과 출력
        for code, p in self.patterns.items():
            print(f"  {p.name}: 시차={p.avg_lag_minutes}분 β={p.avg_beta}x "
                  f"상관={p.correlation} 승률={p.win_rate}% 반응률={p.response_rate}%")

    def rank_targets(self) -> List[str]:
        """
        현재 패턴 통계 기반으로 종목 우선순위 산출

        점수 = (승률 × 0.3) + (동방향확률 × 0.2) + (배율 × 10 × 0.2)
             + (반응률 × 0.15) + (상관계수 × 100 × 0.15)
        """
        scores = {}
        for code, p in self.patterns.items():
            score = (
                (p.win_rate or 0) * 0.3 +
                (p.same_direction_pct or 0) * 0.2 +
                (p.avg_beta or 1) * 10 * 0.2 +
                (p.response_rate or 0) * 0.15 +
                (p.correlation or 0) * 100 * 0.15
            )
            scores[code] = round(score, 1)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        print("\n종목 우선순위:")
        for i, (code, score) in enumerate(ranked, 1):
            name = self.patterns[code].name
            print(f"  {i}. {name} ({code}): 점수 {score}")

        return [code for code, _ in ranked]

    def get_optimal_entry_delay(self, code: str) -> float:
        """
        해당 종목의 최적 진입 대기 시간(분)

        삼성전자 추세 확정 후, 이 시간만큼 기다렸다가
        골든크로스가 발생하면 진입
        """
        p = self.patterns.get(code)
        if not p:
            return 0
        # 중앙값 사용 (평균보다 이상치에 강건)
        return p.median_lag_minutes

    def get_position_weight(self, code: str) -> float:
        """
        종목별 포지션 가중치 (0.0 ~ 1.0)

        상관계수 높고 + 동방향 확률 높고 + 승률 높은 종목에 더 많이 배분
        """
        p = self.patterns.get(code)
        if not p:
            return 0.5
        weight = (
            min(1.0, (p.correlation or 0) * 1.2) * 0.4 +
            min(1.0, (p.same_direction_pct or 0) / 100) * 0.3 +
            min(1.0, (p.win_rate or 0) / 100) * 0.3
        )
        return round(max(0.1, min(1.0, weight)), 2)

    def should_trade_at_hour(self, code: str, hour: int, minute: int = 0) -> bool:
        """
        시간대별 매매 가능 여부
        09:15 이전 노이즈 구간 / 15:00 이후 마감 구간 회피
        """
        if hour < 9:
            return False
        if hour == 9 and minute < 15:
            return False
        if hour >= 15:
            return False
        return True

    def get_stop_loss_pct(self, code: str) -> float:
        """
        종목별 손절 기준(%)

        변동성(beta)이 큰 종목은 손절폭도 넓게
        """
        p = self.patterns.get(code)
        if not p:
            return -2.0
        # beta가 2배면 손절도 2배 넓게
        base_stop = -1.5
        return round(base_stop * max(1.0, p.avg_beta), 1)


if __name__ == "__main__":
    mysql_config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "database": os.getenv("MYSQL_DATABASE", "stock_minutes"),
        "charset": os.getenv("MYSQL_CHARSET", "utf8mb4")
    }

    engine = PrecisionEngine(mysql_config)
    ranked = engine.rank_targets()

    print("\n─── 종목별 분석 ───")
    for code in ranked:
        p = engine.patterns.get(code)
        if p:
            delay = engine.get_optimal_entry_delay(code)
            weight = engine.get_position_weight(code)
            stop_loss = engine.get_stop_loss_pct(code)

            print(f"\n{p.name} ({code}):")
            print(f"  - 진입 대기 시간: {delay:.1f}분")
            print(f"  - 포지션 가중치: {weight}")
            print(f"  - 손절 기준: {stop_loss}%")
            print(f"  - 변동 배율(β): {p.avg_beta}x")
            print(f"  - 승률: {p.win_rate}%")
