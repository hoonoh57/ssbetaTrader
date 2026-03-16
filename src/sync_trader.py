"""
=============================================================================
삼성전자 동기화 매매 시스템 — sync_trader.py
=============================================================================
기능:
  1) 6개월 분봉 DB 기반 백테스트 (일별 / 전체 기간)
  2) 특정 일자 장중(intraday) 시뮬레이션
  3) 실시간(LIVE) 매매 — Server32 WebSocket + REST 주문
  4) lightweight-charts v5 동기화 멀티 차트 표시
     (매매 신호 마커 + 수익률 실시간 표시)

의존:
  - minute_downloader.py 의 DB 스키마/종목 설정
  - pattern_analyzer.py 의 분석 결과 (sync_patterns)
  - precision_engine.py 의 PrecisionEngine
=============================================================================
"""

import os
import sys
import json
import time
import logging
import threading
import mysql.connector
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta, date
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv

# ─── lightweight-charts import ───
try:
    from lightweight_charts import Chart
except ImportError:
    Chart = None
    print("[WARN] lightweight-charts 미설치. 차트 없이 콘솔 모드로 실행합니다.")
    print("       설치: pip install lightweight-charts")

# ─── 프로젝트 내부 모듈 ───
from precision_engine import PrecisionEngine, STOCK_NAMES

load_dotenv()

# ─── 로깅 ───
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/sync_trader.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("SyncTrader")

# ═════════════════════════════════════════════════════════════════════════════
# 설정
# ═════════════════════════════════════════════════════════════════════════════
BASE_URL = os.getenv("SERVER_API_BASE_URL", "http://localhost:8082")
MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "localhost"),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "stock_minutes"),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
}

SIGNAL_CODE = "005930"  # 삼성전자 = 신호 종목
TARGET_CODES = ["005935", "067310", "131970", "356860", "240810", "095340", "330860"]
ALL_CODES = [SIGNAL_CODE] + TARGET_CODES

TICK = int(os.getenv("TICK_UNITS", "5").split(",")[0])  # 기본 5분봉
INVESTMENT = 30_000_000  # 총 투자금(원)
MA_PERIODS = [5, 10, 20]

# 장 시간 (한국)
MARKET_OPEN_H, MARKET_OPEN_M = 9, 0
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 30
ENTRY_WAIT_H, ENTRY_WAIT_M = 9, 15  # 노이즈 구간 이후


# ═════════════════════════════════════════════════════════════════════════════
# 데이터 모델
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class Position:
    code: str
    name: str
    entry_price: float
    entry_time: datetime
    qty: int
    weight: float  # PrecisionEngine 가중치
    stop_loss_pct: float

    @property
    def invested(self) -> float:
        return self.entry_price * self.qty


@dataclass
class Trade:
    code: str
    name: str
    side: str  # "BUY" / "SELL"
    price: float
    qty: int
    dt: datetime
    reason: str  # "GOLDEN_CROSS" / "DEAD_CROSS" / "STOP_LOSS" / "MARKET_CLOSE"
    pnl_pct: float = 0.0
    pnl_krw: float = 0.0


@dataclass
class DayResult:
    date: date
    trades: List[Trade] = field(default_factory=list)
    pnl_pct: float = 0.0
    pnl_krw: float = 0.0
    win_count: int = 0
    loss_count: int = 0


# ═════════════════════════════════════════════════════════════════════════════
# DB 클라이언트
# ═════════════════════════════════════════════════════════════════════════════
class DBClient:
    """MySQL 분봉 데이터 조회"""

    def __init__(self):
        self.conn = mysql.connector.connect(**MYSQL_CONFIG)

    def close(self):
        if self.conn.is_connected():
            self.conn.close()

    def load_day_minutes(self, code: str, day: date, tick: int = TICK) -> pd.DataFrame:
        """특정 일자의 분봉 로드"""
        sql = """
            SELECT dt, open, high, low, close, volume, change_pct
            FROM minute_candles
            WHERE code=%s AND tick=%s
              AND DATE(dt) = %s
            ORDER BY dt
        """
        df = pd.read_sql(sql, self.conn, params=[code, tick, day])
        if not df.empty:
            df["dt"] = pd.to_datetime(df["dt"])
        return df

    def load_range_minutes(self, code: str, start: date, end: date,
                           tick: int = TICK) -> pd.DataFrame:
        """기간 분봉 로드"""
        sql = """
            SELECT dt, open, high, low, close, volume, change_pct
            FROM minute_candles
            WHERE code=%s AND tick=%s
              AND DATE(dt) BETWEEN %s AND %s
            ORDER BY dt
        """
        df = pd.read_sql(sql, self.conn, params=[code, tick, start, end])
        if not df.empty:
            df["dt"] = pd.to_datetime(df["dt"])
            df["date"] = df["dt"].dt.date
        return df

    def get_trading_days(self, start: date, end: date) -> List[date]:
        """DB에 데이터가 있는 거래일 목록"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT DISTINCT DATE(dt) as d
            FROM minute_candles
            WHERE code=%s AND tick=%s
              AND DATE(dt) BETWEEN %s AND %s
            ORDER BY d
        """, (SIGNAL_CODE, TICK, start, end))
        days = [row[0] for row in cursor.fetchall()]
        cursor.close()
        return days

    def save_backtest_result(self, result: dict):
        """백테스트 결과 DB 저장"""
        cursor = self.conn.cursor()
        sql = """
            INSERT INTO backtest_results
                (strategy_name, run_date, period_start, period_end, tick,
                 total_trades, win_count, loss_count, win_rate,
                 total_pnl_pct, max_drawdown, avg_hold_minutes,
                 sharpe_ratio, params_json, trades_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(sql, (
            result["strategy_name"], datetime.now(),
            result["period_start"], result["period_end"], TICK,
            result["total_trades"], result["win_count"], result["loss_count"],
            result["win_rate"], result["total_pnl_pct"], result["max_drawdown"],
            result["avg_hold_minutes"], result.get("sharpe_ratio"),
            json.dumps(result.get("params", {}), ensure_ascii=False),
            json.dumps(result.get("trades", []), ensure_ascii=False, default=str),
        ))
        self.conn.commit()
        cursor.close()


# ═════════════════════════════════════════════════════════════════════════════
# Server API 클라이언트 (실시간 매매용)
# ═════════════════════════════════════════════════════════════════════════════
class ServerClient:
    """Server32 REST API 호출"""

    def __init__(self):
        self.session = requests.Session()

    def get_current_price(self, code: str) -> Optional[dict]:
        """현재가 조회"""
        try:
            r = self.session.get(
                f"{BASE_URL}/api/market/current",
                params={"code": code}, timeout=5,
            )
            res = r.json()
            if res.get("Success"):
                return res["Data"]
        except Exception as e:
            log.error(f"현재가 조회 실패 {code}: {e}")
        return None

    def get_minute_candles(self, code: str, tick: int = TICK,
                           stop_time: str = None) -> List[dict]:
        """분봉 조회"""
        params = {"code": code, "tick": tick}
        if stop_time:
            params["stopTime"] = stop_time
        try:
            r = self.session.get(
                f"{BASE_URL}/api/market/candles/minute",
                params=params, timeout=10,
            )
            res = r.json()
            if res.get("Success") and res.get("Data"):
                return res["Data"]
        except Exception as e:
            log.error(f"분봉 조회 실패 {code}: {e}")
        return []

    def send_order(self, code: str, side: str, qty: int, price: int = 0) -> bool:
        """
        매매 주문 전송
        side: "BUY" / "SELL"
        price=0 이면 시장가
        """
        order_type = "시장가" if price == 0 else "지정가"
        try:
            r = self.session.post(
                f"{BASE_URL}/api/order",
                json={
                    "code": code,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "orderType": order_type,
                },
                timeout=10,
            )
            res = r.json()
            if res.get("Success"):
                log.info(f"주문 성공: {side} {STOCK_NAMES.get(code, code)} "
                         f"{qty}주 @ {price if price else '시장가'}")
                return True
            else:
                log.error(f"주문 실패: {res.get('Message')}")
        except Exception as e:
            log.error(f"주문 오류: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
# 핵심: 동기화 매매 엔진
# ═════════════════════════════════════════════════════════════════════════════
class SyncTradeEngine:
    """
    삼성전자 이동평균 정배열 + 양봉 → 추세 확정
    → 타 종목 골든크로스 시 매수
    → 삼성전자 데드크로스 시 전량 매도

    PrecisionEngine의 패턴 통계를 활용하여:
    - 종목별 진입 대기 시간 최적화
    - 포지션 가중치 배분
    - 시간대별 매매 필터
    - 종목별 손절폭 설정
    """

    def __init__(self, precision: Optional[PrecisionEngine] = None):
        self.precision = precision
        self.positions: Dict[str, Position] = {}  # code → Position
        self.trades: List[Trade] = []
        self.samsung_aligned = False         # 현재 정배열 상태
        self.samsung_trend_confirmed = False  # 정배열+양봉 확정
        self.samsung_trend_start_time: Optional[datetime] = None
        self.investment = INVESTMENT

    def reset_day(self):
        """일 시작 시 상태 초기화"""
        self.samsung_aligned = False
        self.samsung_trend_confirmed = False
        self.samsung_trend_start_time = None
        # 포지션은 유지 (전일 미청산분)

    # ─── MA 계산 유틸 ───
    @staticmethod
    def calc_ma(series: pd.Series, period: int) -> pd.Series:
        return series.rolling(window=period, min_periods=period).mean()

    @staticmethod
    def add_all_ma(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for p in MA_PERIODS:
            df[f"ma{p}"] = df["close"].rolling(window=p, min_periods=p).mean()
        return df

    # ─── 삼성전자 신호 판단 ───
    def check_samsung_signal(self, row: pd.Series, prev_row: Optional[pd.Series] = None) -> str:
        """
        삼성전자 캔들 1개 입력 → 신호 반환

        Returns:
            "TREND_START"  : 정배열 + 양봉 (최초 확정)
            "ALIGNED"      : 정배열 유지중
            "DEAD_CROSS"   : MA5 < MA10 (추세 이탈)
            "NONE"         : 해당 없음
        """
        ma5 = row.get("ma5")
        ma10 = row.get("ma10")
        ma20 = row.get("ma20")

        if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20):
            return "NONE"

        is_aligned = (ma5 > ma10) and (ma10 > ma20)
        is_bullish = row["close"] > row["open"]

        # 데드크로스 감지
        if self.samsung_trend_confirmed and not is_aligned:
            if ma5 <= ma10:
                self.samsung_aligned = False
                self.samsung_trend_confirmed = False
                self.samsung_trend_start_time = None
                return "DEAD_CROSS"

        # 정배열 + 양봉 → 추세 확정
        if is_aligned and is_bullish:
            if not self.samsung_trend_confirmed:
                self.samsung_aligned = True
                self.samsung_trend_confirmed = True
                self.samsung_trend_start_time = row["dt"]
                return "TREND_START"
            else:
                return "ALIGNED"

        if is_aligned:
            self.samsung_aligned = True
            if self.samsung_trend_confirmed:
                return "ALIGNED"

        return "NONE"

    # ─── 타 종목 골든크로스 감지 ───
    @staticmethod
    def check_golden_cross(row: pd.Series, prev_row: Optional[pd.Series]) -> bool:
        """MA5가 MA10을 상향 돌파"""
        if prev_row is None:
            return False
        ma5 = row.get("ma5")
        ma10 = row.get("ma10")
        prev_ma5 = prev_row.get("ma5")
        prev_ma10 = prev_row.get("ma10")

        if any(pd.isna(v) for v in [ma5, ma10, prev_ma5, prev_ma10]):
            return False

        return (prev_ma5 <= prev_ma10) and (ma5 > ma10)

    # ─── 매수 실행 ───
    def execute_buy(self, code: str, price: float, dt: datetime,
                    reason: str = "GOLDEN_CROSS") -> Optional[Trade]:
        """매수 실행 (백테스트/실매매 공용)"""
        if code in self.positions:
            return None  # 이미 보유중

        name = STOCK_NAMES.get(code, code)

        # PrecisionEngine 가중치
        weight = 1.0
        stop_pct = -3.0
        if self.precision:
            weight = self.precision.get_position_weight(code)
            stop_pct = self.precision.get_stop_loss_pct(code)
            # 시간대 필터
            if not self.precision.should_trade_at_hour(code, dt.hour, dt.minute):
                log.info(f"  [{name}] 시간대 필터로 매수 스킵 ({dt.strftime('%H:%M')})")
                return None

        # 배분 금액 = 투자금 × (가중치 / 총 가중치합)
        total_weight = sum(
            self.precision.get_position_weight(c) if self.precision else 1.0
            for c in TARGET_CODES if c not in self.positions
        )
        if total_weight <= 0:
            total_weight = 1.0

        alloc = self.investment * (weight / total_weight) * 0.8  # 80% 투입
        qty = int(alloc / price) if price > 0 else 0
        if qty <= 0:
            return None

        pos = Position(
            code=code, name=name,
            entry_price=price, entry_time=dt,
            qty=qty, weight=weight,
            stop_loss_pct=stop_pct,
        )
        self.positions[code] = pos

        trade = Trade(
            code=code, name=name, side="BUY",
            price=price, qty=qty, dt=dt, reason=reason,
        )
        self.trades.append(trade)

        log.info(f"  ▲ BUY  {name} | {qty}주 × {price:,.0f}원 "
                 f"= {qty * price:,.0f}원 | 가중치 {weight} | 손절 {stop_pct}%")
        return trade

    # ─── 매도 실행 ───
    def execute_sell(self, code: str, price: float, dt: datetime,
                     reason: str = "DEAD_CROSS") -> Optional[Trade]:
        """매도 실행"""
        if code not in self.positions:
            return None

        pos = self.positions.pop(code)
        pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
        pnl_krw = (price - pos.entry_price) * pos.qty

        trade = Trade(
            code=code, name=pos.name, side="SELL",
            price=price, qty=pos.qty, dt=dt, reason=reason,
            pnl_pct=round(pnl_pct, 2), pnl_krw=round(pnl_krw, 0),
        )
        self.trades.append(trade)

        marker = "▼" if pnl_pct < 0 else "▽"
        color = "손실" if pnl_pct < 0 else "수익"
        log.info(f"  {marker} SELL {pos.name} | {pos.qty}주 × {price:,.0f}원 "
                 f"| {color} {pnl_pct:+.2f}% ({pnl_krw:+,.0f}원) | 사유: {reason}")
        return trade

    # ─── 손절 체크 ───
    def check_stop_loss(self, code: str, current_price: float, dt: datetime) -> Optional[Trade]:
        """개별 종목 손절"""
        if code not in self.positions:
            return None
        pos = self.positions[code]
        pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100
        if pnl_pct <= pos.stop_loss_pct:
            return self.execute_sell(code, current_price, dt, reason="STOP_LOSS")
        return None

    # ─── 전량 청산 ───
    def close_all_positions(self, prices: Dict[str, float], dt: datetime,
                            reason: str = "DEAD_CROSS") -> List[Trade]:
        """보유 전 종목 매도"""
        trades = []
        for code in list(self.positions.keys()):
            price = prices.get(code, self.positions[code].entry_price)
            t = self.execute_sell(code, price, dt, reason)
            if t:
                trades.append(t)
        return trades


# ═════════════════════════════════════════════════════════════════════════════
# 백테스터
# ═════════════════════════════════════════════════════════════════════════════
class Backtester:
    """6개월 분봉 DB 기반 백테스트"""

    def __init__(self):
        self.db = DBClient()
        try:
            self.precision = PrecisionEngine(MYSQL_CONFIG)
        except Exception as e:
            log.warning(f"PrecisionEngine 로드 실패 (패턴 미분석?): {e}")
            self.precision = None
        self.engine = SyncTradeEngine(precision=self.precision)

    def close(self):
        self.db.close()

    # ─── 단일 일자 시뮬레이션 ───
    def simulate_day(self, day: date) -> DayResult:
        """하루 장중 시뮬레이션"""
        self.engine.reset_day()
        result = DayResult(date=day)

        # 모든 종목 분봉 로드
        data: Dict[str, pd.DataFrame] = {}
        for code in ALL_CODES:
            df = self.db.load_day_minutes(code, day)
            if df.empty:
                continue
            df = SyncTradeEngine.add_all_ma(df)
            data[code] = df

        if SIGNAL_CODE not in data or data[SIGNAL_CODE].empty:
            return result

        df_sig = data[SIGNAL_CODE]

        # ─── 분봉 순회 ───
        for i in range(1, len(df_sig)):
            row = df_sig.iloc[i]
            prev = df_sig.iloc[i - 1]
            dt = row["dt"]

            # 장 시작 노이즈 구간 스킵
            if dt.hour == ENTRY_WAIT_H and dt.minute < ENTRY_WAIT_M:
                continue

            # 1) 삼성전자 신호 체크
            signal = self.engine.check_samsung_signal(row, prev)

            if signal == "TREND_START":
                log.info(f"\n{'─'*50}")
                log.info(f"[{day}] 삼성전자 추세 확정 @ {dt.strftime('%H:%M')} "
                         f"| 종가 {row['close']:,.0f}")
                log.info(f"  MA5={row['ma5']:,.0f} > MA10={row['ma10']:,.0f} "
                         f"> MA20={row['ma20']:,.0f} + 양봉")

            elif signal == "DEAD_CROSS":
                log.info(f"\n[{day}] 삼성전자 데드크로스 @ {dt.strftime('%H:%M')}")
                # 전량 청산
                close_prices = {}
                for code in TARGET_CODES:
                    if code in data and not data[code].empty:
                        tgt_at_time = data[code][data[code]["dt"] <= dt]
                        if not tgt_at_time.empty:
                            close_prices[code] = tgt_at_time.iloc[-1]["close"]
                sells = self.engine.close_all_positions(close_prices, dt, "DEAD_CROSS")
                result.trades.extend(sells)
                continue

            # 2) 삼성전자 추세 확정 상태에서 → 타 종목 골든크로스 체크
            if self.engine.samsung_trend_confirmed:
                for code in TARGET_CODES:
                    if code not in data or data[code].empty:
                        continue
                    if code in self.engine.positions:
                        continue

                    df_tgt = data[code]
                    # 현재 시각 이하의 봉 필터
                    tgt_at = df_tgt[df_tgt["dt"] <= dt]
                    if len(tgt_at) < 2:
                        continue

                    tgt_row = tgt_at.iloc[-1]
                    tgt_prev = tgt_at.iloc[-2]

                    # 진입 대기 시간 체크 (PrecisionEngine)
                    if self.precision and self.engine.samsung_trend_start_time:
                        delay = self.precision.get_optimal_entry_delay(code)
                        elapsed = (dt - self.engine.samsung_trend_start_time).total_seconds() / 60
                        if elapsed < delay * 0.5:
                            # 최적 시차의 절반 이전이면 아직 이르다
                            continue

                    if SyncTradeEngine.check_golden_cross(tgt_row, tgt_prev):
                        buy = self.engine.execute_buy(
                            code, tgt_row["close"], dt, "GOLDEN_CROSS"
                        )
                        if buy:
                            result.trades.append(buy)

            # 3) 보유 종목 손절 체크
            for code in list(self.engine.positions.keys()):
                if code in data and not data[code].empty:
                    tgt_at = data[code][data[code]["dt"] <= dt]
                    if not tgt_at.empty:
                        sl = self.engine.check_stop_loss(
                            code, tgt_at.iloc[-1]["close"], dt
                        )
                        if sl:
                            result.trades.append(sl)

        # ─── 장 마감 미청산 포지션 강제 정리 ───
        if self.engine.positions:
            close_prices = {}
            for code in list(self.engine.positions.keys()):
                if code in data and not data[code].empty:
                    close_prices[code] = data[code].iloc[-1]["close"]
            closes = self.engine.close_all_positions(close_prices, dt, "MARKET_CLOSE")
            result.trades.extend(closes)

        # 일별 집계
        sell_trades = [t for t in result.trades if t.side == "SELL"]
        result.win_count = len([t for t in sell_trades if t.pnl_pct > 0])
        result.loss_count = len([t for t in sell_trades if t.pnl_pct <= 0])
        result.pnl_krw = sum(t.pnl_krw for t in sell_trades)
        result.pnl_pct = round(result.pnl_krw / self.engine.investment * 100, 2) if sell_trades else 0.0

        return result

    # ─── 전체 기간 백테스트 ───
    def run_full_backtest(self, start: date = None, end: date = None) -> dict:
        """6개월 전체 백테스트"""
        if end is None:
            end = date.today()
        if start is None:
            start = end - timedelta(days=180)

        log.info("=" * 70)
        log.info(f"전체 기간 백테스트")
        log.info(f"기간: {start} ~ {end}")
        log.info(f"투자금: {INVESTMENT:,.0f}원 / 분봉: {TICK}분")
        log.info("=" * 70)

        trading_days = self.db.get_trading_days(start, end)
        log.info(f"거래일: {len(trading_days)}일")

        all_results: List[DayResult] = []
        cumulative_pnl = 0.0
        peak_pnl = 0.0
        max_drawdown = 0.0

        equity_curve = []  # (date, cum_pnl_pct)

        for day in trading_days:
            result = self.simulate_day(day)
            all_results.append(result)
            cumulative_pnl += result.pnl_krw

            cum_pct = cumulative_pnl / INVESTMENT * 100
            equity_curve.append((day, cum_pct))

            if cum_pct > peak_pnl:
                peak_pnl = cum_pct
            dd = peak_pnl - cum_pct
            if dd > max_drawdown:
                max_drawdown = dd

            if result.trades:
                sells = [t for t in result.trades if t.side == "SELL"]
                if sells:
                    log.info(f"[{day}] 수익 {result.pnl_pct:+.2f}% "
                             f"({result.pnl_krw:+,.0f}원) | "
                             f"누적 {cum_pct:+.2f}% | "
                             f"승 {result.win_count} 패 {result.loss_count}")

        # ─── 종합 통계 ───
        all_sells = []
        for r in all_results:
            all_sells.extend([t for t in r.trades if t.side == "SELL"])

        total_trades = len(all_sells)
        win_count = len([t for t in all_sells if t.pnl_pct > 0])
        loss_count = total_trades - win_count
        win_rate = round(win_count / total_trades * 100, 1) if total_trades > 0 else 0
        total_pnl_pct = round(cumulative_pnl / INVESTMENT * 100, 2)
        total_pnl_krw = cumulative_pnl

        # 평균 보유 시간
        hold_minutes = []
        buy_map = {}
        for r in all_results:
            for t in r.trades:
                if t.side == "BUY":
                    buy_map[t.code] = t.dt
                elif t.side == "SELL" and t.code in buy_map:
                    hold_min = (t.dt - buy_map.pop(t.code)).total_seconds() / 60
                    hold_minutes.append(hold_min)
        avg_hold = round(np.mean(hold_minutes)) if hold_minutes else 0

        # 샤프 비율 (일별 수익률 기준)
        daily_rets = [r.pnl_pct for r in all_results if r.pnl_pct != 0]
        sharpe = None
        if len(daily_rets) > 1:
            mean_ret = np.mean(daily_rets)
            std_ret = np.std(daily_rets, ddof=1)
            if std_ret > 0:
                sharpe = round(mean_ret / std_ret * np.sqrt(252), 3)

        summary = {
            "strategy_name": "samsung_sync_v1",
            "period_start": start,
            "period_end": end,
            "total_trades": total_trades,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "total_pnl_pct": total_pnl_pct,
            "total_pnl_krw": total_pnl_krw,
            "max_drawdown": round(max_drawdown, 2),
            "avg_hold_minutes": avg_hold,
            "sharpe_ratio": sharpe,
            "equity_curve": equity_curve,
            "params": {
                "investment": INVESTMENT,
                "tick": TICK,
                "ma_periods": MA_PERIODS,
                "signal_code": SIGNAL_CODE,
                "target_codes": TARGET_CODES,
            },
            "trades": [
                {
                    "code": t.code, "name": t.name, "side": t.side,
                    "price": t.price, "qty": t.qty,
                    "dt": t.dt.isoformat(), "reason": t.reason,
                    "pnl_pct": t.pnl_pct, "pnl_krw": t.pnl_krw,
                }
                for t in all_sells
            ],
        }

        # ─── 결과 출력 ───
        log.info("\n" + "=" * 70)
        log.info("백테스트 종합 결과")
        log.info("=" * 70)
        log.info(f"기간          : {start} ~ {end} ({len(trading_days)}거래일)")
        log.info(f"총 매매        : {total_trades}회")
        log.info(f"승률          : {win_rate}% ({win_count}승 {loss_count}패)")
        log.info(f"총 수익률      : {total_pnl_pct:+.2f}%")
        log.info(f"총 수익금      : {total_pnl_krw:+,.0f}원")
        log.info(f"최대 낙폭(MDD) : {max_drawdown:.2f}%")
        log.info(f"평균 보유      : {avg_hold}분")
        log.info(f"샤프 비율      : {sharpe}")
        log.info("=" * 70)

        # 종목별 수익
        log.info("\n종목별 수익:")
        stock_pnl: Dict[str, List[float]] = {}
        for t in all_sells:
            stock_pnl.setdefault(t.code, []).append(t.pnl_pct)
        for code, pnls in sorted(stock_pnl.items(), key=lambda x: sum(x[1]), reverse=True):
            name = STOCK_NAMES.get(code, code)
            avg = np.mean(pnls)
            total = sum(pnls)
            wins = len([p for p in pnls if p > 0])
            log.info(f"  {name:>8}: 총 {total:+.2f}% | 평균 {avg:+.2f}% | "
                     f"{len(pnls)}회 (승 {wins})")

        # DB 저장
        try:
            self.db.save_backtest_result(summary)
            log.info("\n결과 DB 저장 완료")
        except Exception as e:
            log.error(f"DB 저장 실패: {e}")

        return summary

    # ─── 단일 일자 장중 백테스트 ───
    def run_intraday(self, target_day: str) -> dict:
        """특정 일자 장중 분봉 시뮬레이션"""
        day = datetime.strptime(target_day, "%Y%m%d").date()
        log.info(f"\n장중 백테스트: {day}")
        log.info("=" * 50)

        result = self.simulate_day(day)

        log.info(f"\n[{day}] 결과:")
        log.info(f"  매매: {len(result.trades)}건")
        log.info(f"  승 {result.win_count} / 패 {result.loss_count}")
        log.info(f"  수익: {result.pnl_pct:+.2f}% ({result.pnl_krw:+,.0f}원)")

        for t in result.trades:
            side_mark = "▲" if t.side == "BUY" else "▼"
            log.info(f"  {side_mark} {t.dt.strftime('%H:%M')} {t.name} "
                     f"{t.side} {t.qty}주 × {t.price:,.0f}원 "
                     f"| {t.reason} {t.pnl_pct:+.2f}%")

        summary = {
            "strategy_name": "samsung_sync_v1_intraday",
            "period_start": day,
            "period_end": day,
            "total_trades": len([t for t in result.trades if t.side == "SELL"]),
            "win_count": result.win_count,
            "loss_count": result.loss_count,
            "win_rate": round(
                result.win_count / max(1, result.win_count + result.loss_count) * 100, 1
            ),
            "total_pnl_pct": result.pnl_pct,
            "max_drawdown": 0,
            "avg_hold_minutes": 0,
            "trades": [
                {
                    "code": t.code, "name": t.name, "side": t.side,
                    "price": t.price, "qty": t.qty,
                    "dt": t.dt.isoformat(), "reason": t.reason,
                    "pnl_pct": t.pnl_pct, "pnl_krw": t.pnl_krw,
                }
                for t in result.trades
            ],
        }

        try:
            self.db.save_backtest_result(summary)
        except Exception as e:
            log.error(f"DB 저장 실패: {e}")

        return summary


# ═════════════════════════════════════════════════════════════════════════════
# 동기화 멀티 차트 UI (lightweight-charts v5)
# ═════════════════════════════════════════════════════════════════════════════
class SyncChartUI:
    """
    8종목 동기화 차트 — 메인(삼성전자) + 서브 7개
    매매 신호 마커 + MA선 + 수익률 표시
    """

    # 서브차트 배색
    COLORS = [
        "#2196F3",  # 삼성전자우 (파랑)
        "#FF5722",  # 하나마이크론 (빨강)
        "#4CAF50",  # 두산테스나 (녹색)
        "#FF9800",  # 티엘비 (주황)
        "#9C27B0",  # 원익IPS (보라)
        "#00BCD4",  # ISC (청록)
        "#E91E63",  # 네패스아크 (분홍)
    ]

    MA_COLORS = {5: "#FFD700", 10: "#FF6347", 20: "#1E90FF"}  # 금 / 토마토 / 파랑

    def __init__(self):
        if Chart is None:
            raise RuntimeError(
                "lightweight-charts 미설치. pip install lightweight-charts"
            )
        self.main_chart: Optional[Chart] = None
        self.sub_charts: Dict[str, Chart] = {}
        self.ma_lines: Dict[str, Dict[int, object]] = {}  # code → {period: line}
        self.marker_data: Dict[str, list] = {}  # code → markers

    def create(self, title: str = "삼성전자 동기화 매매"):
        """멀티 차트 생성"""
        self.main_chart = Chart(
            title=title,
            width=1800,
            height=900,
            inner_width=1.0,
            inner_height=0.35,  # 메인차트 35%
            toolbox=True,
        )
        self.main_chart.legend(visible=True, font_size=12)
        self.main_chart.topbar.textbox("signal", "삼성전자 신호: 대기중")
        self.main_chart.topbar.textbox("pnl", "수익률: 0.00%")

        # 메인차트 MA 라인
        self.ma_lines[SIGNAL_CODE] = {}
        for p in MA_PERIODS:
            line = self.main_chart.create_line(
                name=f"MA{p}",
                color=self.MA_COLORS[p],
                width=2 if p == 5 else 1,
                price_line=False,
            )
            self.ma_lines[SIGNAL_CODE][p] = line

        # 서브차트 7개 생성
        sub_height = 0.65 / len(TARGET_CODES)  # 나머지 65%를 균등 분배
        for idx, code in enumerate(TARGET_CODES):
            name = STOCK_NAMES.get(code, code)
            sub = self.main_chart.create_subchart(
                width=1.0,
                height=sub_height,
                sync=True,  # x축 시간 동기화
                position="right" if idx % 2 == 1 else "left",
            )
            sub.legend(visible=True, font_size=10)
            sub.topbar.textbox("name", name)
            sub.topbar.textbox("status", "")
            self.sub_charts[code] = sub

            # 서브차트 MA 라인
            self.ma_lines[code] = {}
            for p in MA_PERIODS:
                line = sub.create_line(
                    name=f"MA{p}",
                    color=self.MA_COLORS[p],
                    width=2 if p == 5 else 1,
                    price_line=False,
                )
                self.ma_lines[code][p] = line

    def set_main_data(self, df: pd.DataFrame):
        """삼성전자 캔들 + MA 세팅"""
        candle_df = df[["dt", "open", "high", "low", "close", "volume"]].copy()
        candle_df.columns = ["time", "open", "high", "low", "close", "volume"]
        self.main_chart.set(candle_df)

        for p in MA_PERIODS:
            col = f"ma{p}"
            if col in df.columns:
                ma_df = df[["dt", col]].dropna().copy()
                ma_df.columns = ["time", col]
                self.ma_lines[SIGNAL_CODE][p].set(ma_df)

    def set_sub_data(self, code: str, df: pd.DataFrame):
        """서브차트 캔들 + MA 세팅"""
        if code not in self.sub_charts:
            return
        candle_df = df[["dt", "open", "high", "low", "close", "volume"]].copy()
        candle_df.columns = ["time", "open", "high", "low", "close", "volume"]
        self.sub_charts[code].set(candle_df)

        for p in MA_PERIODS:
            col = f"ma{p}"
            if col in df.columns and code in self.ma_lines:
                ma_df = df[["dt", col]].dropna().copy()
                ma_df.columns = ["time", col]
                self.ma_lines[code][p].set(ma_df)

    def add_marker(self, code: str, dt: datetime, side: str,
                   text: str = "", color: str = None):
        """매매 신호 마커 추가"""
        chart = self.main_chart if code == SIGNAL_CODE else self.sub_charts.get(code)
        if chart is None:
            return

        if color is None:
            color = "#FF0000" if side == "BUY" else "#0000FF"

        shape = "arrow_up" if side in ("BUY", "TREND_START") else "arrow_down"
        position = "below" if side in ("BUY", "TREND_START") else "above"

        chart.marker(
            time=dt,
            shape=shape,
            color=color,
            text=text,
            position=position,
        )

    def update_signal_text(self, text: str):
        if self.main_chart:
            self.main_chart.topbar["signal"].set(text)

    def update_pnl_text(self, pnl_pct: float):
        if self.main_chart:
            color = "green" if pnl_pct >= 0 else "red"
            self.main_chart.topbar["pnl"].set(f"수익률: {pnl_pct:+.2f}%")

    def update_sub_status(self, code: str, text: str):
        if code in self.sub_charts:
            self.sub_charts[code].topbar["status"].set(text)

    def show(self, block: bool = True):
        if self.main_chart:
            self.main_chart.show(block=block)

    def exit(self):
        if self.main_chart:
            self.main_chart.exit()


# ═════════════════════════════════════════════════════════════════════════════
# 차트 백테스트 뷰어 — 차트 위에 백테스트 결과 표시
# ═════════════════════════════════════════════════════════════════════════════
class ChartBacktestViewer:
    """백테스트 결과를 동기화 차트에 시각적으로 표시"""

    def __init__(self):
        self.db = DBClient()
        self.chart_ui = SyncChartUI()

    def close(self):
        self.db.close()

    def view_day(self, day: str, trades: List[dict] = None):
        """특정 일자의 차트 + 매매 신호 표시"""
        target_day = datetime.strptime(day, "%Y%m%d").date()

        self.chart_ui.create(title=f"삼성전자 동기화 매매 — {target_day}")

        # 데이터 로드 + MA 계산
        for code in ALL_CODES:
            df = self.db.load_day_minutes(code, target_day)
            if df.empty:
                continue
            df = SyncTradeEngine.add_all_ma(df)

            if code == SIGNAL_CODE:
                self.chart_ui.set_main_data(df)
            else:
                self.chart_ui.set_sub_data(code, df)

        # 매매 신호 마커
        if trades:
            cum_pnl = 0.0
            for t in trades:
                dt = datetime.fromisoformat(t["dt"]) if isinstance(t["dt"], str) else t["dt"]
                code = t["code"]
                side = t["side"]
                reason = t.get("reason", "")
                pnl = t.get("pnl_pct", 0)

                if side == "BUY":
                    text = f"BUY\n{t.get('name', '')}"
                    self.chart_ui.add_marker(code, dt, "BUY", text, "#FF4444")
                elif side == "SELL":
                    cum_pnl += pnl
                    color = "#00AA00" if pnl > 0 else "#FF0000"
                    text = f"{reason}\n{pnl:+.2f}%"
                    self.chart_ui.add_marker(code, dt, "SELL", text, color)

            self.chart_ui.update_pnl_text(cum_pnl)

        # 삼성전자 추세 구간 표시
        df_sig = self.db.load_day_minutes(SIGNAL_CODE, target_day)
        if not df_sig.empty:
            df_sig = SyncTradeEngine.add_all_ma(df_sig)
            engine_temp = SyncTradeEngine()
            for i in range(1, len(df_sig)):
                row = df_sig.iloc[i]
                prev = df_sig.iloc[i - 1]
                sig = engine_temp.check_samsung_signal(row, prev)
                if sig == "TREND_START":
                    self.chart_ui.add_marker(
                        SIGNAL_CODE, row["dt"], "TREND_START",
                        "추세확정", "#FFD700"
                    )
                    self.chart_ui.update_signal_text(
                        f"추세 확정 @ {row['dt'].strftime('%H:%M')}"
                    )
                elif sig == "DEAD_CROSS":
                    self.chart_ui.add_marker(
                        SIGNAL_CODE, row["dt"], "DEAD_CROSS",
                        "데드크로스", "#FF0000"
                    )
                    self.chart_ui.update_signal_text(
                        f"데드크로스 @ {row['dt'].strftime('%H:%M')}"
                    )

        self.chart_ui.show(block=True)

    def view_period(self, start_day: str, end_day: str, trades: List[dict] = None):
        """기간 차트 뷰 (일봉 축약)"""
        start = datetime.strptime(start_day, "%Y%m%d").date()
        end = datetime.strptime(end_day, "%Y%m%d").date()

        self.chart_ui.create(title=f"기간 백테스트 — {start} ~ {end}")

        for code in ALL_CODES:
            df = self.db.load_range_minutes(code, start, end)
            if df.empty:
                continue
            # 기간 차트: 일봉으로 리샘플링
            daily = df.groupby("date").agg({
                "dt": "first",
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }).reset_index(drop=True)

            daily = SyncTradeEngine.add_all_ma(daily)

            if code == SIGNAL_CODE:
                self.chart_ui.set_main_data(daily)
            else:
                self.chart_ui.set_sub_data(code, daily)

        # 매매 마커
        if trades:
            cum_pnl = 0.0
            for t in trades:
                dt = datetime.fromisoformat(t["dt"]) if isinstance(t["dt"], str) else t["dt"]
                if t["side"] == "SELL":
                    cum_pnl += t.get("pnl_pct", 0)
                    color = "#00AA00" if t.get("pnl_pct", 0) > 0 else "#FF0000"
                    self.chart_ui.add_marker(
                        t["code"], dt, t["side"],
                        f"{t.get('pnl_pct', 0):+.1f}%", color,
                    )
                else:
                    self.chart_ui.add_marker(
                        t["code"], dt, t["side"],
                        t.get("name", ""), "#FF4444",
                    )
            self.chart_ui.update_pnl_text(cum_pnl)

        self.chart_ui.show(block=True)


# ═════════════════════════════════════════════════════════════════════════════
# 실시간 매매 핸들러
# ═════════════════════════════════════════════════════════════════════════════
class RealtimeHandler:
    """
    실시간 매매 — Server32 WebSocket 구독 + 분봉 갱신
    매 분봉 완성 시 SyncTradeEngine으로 신호 판단 + 주문 발행
    """

    def __init__(self):
        self.db = DBClient()
        try:
            self.precision = PrecisionEngine(MYSQL_CONFIG)
        except Exception:
            self.precision = None
        self.engine = SyncTradeEngine(precision=self.precision)
        self.server = ServerClient()
        self.chart_ui: Optional[SyncChartUI] = None

        # 분봉 버퍼 (코드별)
        self.candle_buffers: Dict[str, pd.DataFrame] = {}
        self.running = False

    def _init_buffers(self):
        """장 시작 시 당일 + 전일 분봉 로드 (MA 계산용)"""
        today = date.today()
        yesterday = today - timedelta(days=3)  # 주말 고려
        for code in ALL_CODES:
            df = self.db.load_range_minutes(code, yesterday, today)
            if not df.empty:
                df = SyncTradeEngine.add_all_ma(df)
            self.candle_buffers[code] = df

    def _fetch_latest_candles(self, code: str) -> pd.DataFrame:
        """서버에서 최신 분봉 가져와서 버퍼 갱신"""
        rows = self.server.get_minute_candles(code, TICK)
        if not rows:
            return self.candle_buffers.get(code, pd.DataFrame())

        new_data = []
        for row in rows:
            dt_str = row.get("체결시간", "")
            if len(dt_str) < 14:
                continue
            dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
            new_data.append({
                "dt": dt,
                "open": abs(int(float(str(row.get("시가", 0)).replace(",", "")))),
                "high": abs(int(float(str(row.get("고가", 0)).replace(",", "")))),
                "low": abs(int(float(str(row.get("저가", 0)).replace(",", "")))),
                "close": abs(int(float(str(row.get("현재가", 0)).replace(",", "")))),
                "volume": abs(int(float(str(row.get("거래량", 0)).replace(",", "")))),
            })

        if not new_data:
            return self.candle_buffers.get(code, pd.DataFrame())

        new_df = pd.DataFrame(new_data)

        # 기존 버퍼와 병합 (중복 제거)
        existing = self.candle_buffers.get(code, pd.DataFrame())
        if not existing.empty:
            combined = pd.concat([existing[["dt", "open", "high", "low", "close", "volume"]],
                                  new_df]).drop_duplicates(subset=["dt"]).sort_values("dt")
        else:
            combined = new_df.sort_values("dt")

        combined = combined.reset_index(drop=True)
        combined = SyncTradeEngine.add_all_ma(combined)
        self.candle_buffers[code] = combined
        return combined

    def _process_tick(self):
        """분봉 완성 시 호출 — 전 종목 신호 체크"""
        # 1) 삼성전자 최신 분봉
        df_sig = self._fetch_latest_candles(SIGNAL_CODE)
        if df_sig.empty or len(df_sig) < 2:
            return

        row = df_sig.iloc[-1]
        prev = df_sig.iloc[-2]
        dt = row["dt"]

        # 장 시작 노이즈
        if dt.hour == 9 and dt.minute < 15:
            return

        # 2) 삼성전자 신호
        signal = self.engine.check_samsung_signal(row, prev)

        if signal == "TREND_START":
            log.info(f"[LIVE] 삼성전자 추세 확정 @ {dt.strftime('%H:%M')} "
                     f"| {row['close']:,.0f}원")
            if self.chart_ui:
                self.chart_ui.update_signal_text(f"추세 확정 @ {dt.strftime('%H:%M')}")
                self.chart_ui.add_marker(SIGNAL_CODE, dt, "TREND_START", "추세확정", "#FFD700")

        elif signal == "DEAD_CROSS":
            log.info(f"[LIVE] 삼성전자 데드크로스 @ {dt.strftime('%H:%M')}")
            if self.chart_ui:
                self.chart_ui.update_signal_text(f"데드크로스 @ {dt.strftime('%H:%M')}")
                self.chart_ui.add_marker(SIGNAL_CODE, dt, "DEAD_CROSS", "데드크로스", "#FF0000")

            # 전량 청산
            for code in list(self.engine.positions.keys()):
                df_tgt = self._fetch_latest_candles(code)
                if not df_tgt.empty:
                    price = df_tgt.iloc[-1]["close"]
                    trade = self.engine.execute_sell(code, price, dt, "DEAD_CROSS")
                    if trade:
                        self.server.send_order(code, "SELL", trade.qty, 0)  # 시장가
                        if self.chart_ui:
                            color = "#00AA00" if trade.pnl_pct > 0 else "#FF0000"
                            self.chart_ui.add_marker(
                                code, dt, "SELL", f"{trade.pnl_pct:+.1f}%", color
                            )
            return

        # 3) 추세 확정 상태에서 타 종목 골든크로스 체크
        if self.engine.samsung_trend_confirmed:
            for code in TARGET_CODES:
                if code in self.engine.positions:
                    continue

                df_tgt = self._fetch_latest_candles(code)
                if df_tgt.empty or len(df_tgt) < 2:
                    continue

                tgt_row = df_tgt.iloc[-1]
                tgt_prev = df_tgt.iloc[-2]

                # 진입 대기 시간
                if self.precision and self.engine.samsung_trend_start_time:
                    delay = self.precision.get_optimal_entry_delay(code)
                    elapsed = (dt - self.engine.samsung_trend_start_time).total_seconds() / 60
                    if elapsed < delay * 0.5:
                        continue

                if SyncTradeEngine.check_golden_cross(tgt_row, tgt_prev):
                    trade = self.engine.execute_buy(code, tgt_row["close"], dt, "GOLDEN_CROSS")
                    if trade:
                        self.server.send_order(code, "BUY", trade.qty, 0)  # 시장가
                        if self.chart_ui:
                            self.chart_ui.add_marker(
                                code, dt, "BUY", f"BUY {trade.name}", "#FF4444"
                            )
                            self.chart_ui.update_sub_status(
                                code, f"매수 {trade.qty}주 @ {trade.price:,.0f}"
                            )

        # 4) 보유 종목 손절 체크
        for code in list(self.engine.positions.keys()):
            df_tgt = self._fetch_latest_candles(code)
            if not df_tgt.empty:
                sl_trade = self.engine.check_stop_loss(code, df_tgt.iloc[-1]["close"], dt)
                if sl_trade:
                    self.server.send_order(code, "SELL", sl_trade.qty, 0)
                    if self.chart_ui:
                        self.chart_ui.add_marker(
                            code, dt, "SELL", f"손절 {sl_trade.pnl_pct:+.1f}%", "#FF0000"
                        )

        # 5) 차트 갱신
        self._update_chart()

    def _update_chart(self):
        """차트 데이터 실시간 업데이트"""
        if not self.chart_ui:
            return

        for code in ALL_CODES:
            df = self.candle_buffers.get(code)
            if df is None or df.empty:
                continue
            # 최신 1봉만 업데이트 (성능)
            last = df.iloc[-1]
            tick_data = {
                "time": last["dt"],
                "open": last["open"],
                "high": last["high"],
                "low": last["low"],
                "close": last["close"],
                "volume": last.get("volume", 0),
            }

            if code == SIGNAL_CODE:
                self.main_chart_update(tick_data)
            else:
                self.sub_chart_update(code, tick_data)

        # 수익률 갱신
        total_pnl = sum(
            t.pnl_pct for t in self.engine.trades if t.side == "SELL"
        )
        self.chart_ui.update_pnl_text(total_pnl)

    def main_chart_update(self, tick: dict):
        if self.chart_ui and self.chart_ui.main_chart:
            self.chart_ui.main_chart.update(tick)

    def sub_chart_update(self, code: str, tick: dict):
        if self.chart_ui and code in self.chart_ui.sub_charts:
            self.chart_ui.sub_charts[code].update(tick)

    def run(self, with_chart: bool = True):
        """실시간 매매 시작"""
        log.info("=" * 70)
        log.info("실시간 동기화 매매 시작")
        log.info(f"신호 종목: 삼성전자 ({SIGNAL_CODE})")
        log.info(f"대상 종목: {[STOCK_NAMES.get(c, c) for c in TARGET_CODES]}")
        log.info(f"분봉 단위: {TICK}분 / 투자금: {INVESTMENT:,.0f}원")
        log.info("=" * 70)

        self._init_buffers()

        # 차트 생성
        if with_chart and Chart is not None:
            self.chart_ui = SyncChartUI()
            self.chart_ui.create("삼성전자 동기화 매매 — 실시간")

            # 초기 데이터 세팅
            for code in ALL_CODES:
                df = self.candle_buffers.get(code)
                if df is not None and not df.empty:
                    if code == SIGNAL_CODE:
                        self.chart_ui.set_main_data(df)
                    else:
                        self.chart_ui.set_sub_data(code, df)

        # 매매 루프 (별도 스레드)
        self.running = True

        def trade_loop():
            while self.running:
                now = datetime.now()
                # 장 시간 체크
                market_open = now.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M, second=0)
                market_close = now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0)

                if market_open <= now <= market_close:
                    try:
                        self._process_tick()
                    except Exception as e:
                        log.error(f"매매 루프 오류: {e}")
                elif now > market_close and self.engine.positions:
                    # 장 마감 후 미청산 정리
                    log.info("[LIVE] 장 마감 — 미청산 포지션 정리")
                    for code in list(self.engine.positions.keys()):
                        df = self.candle_buffers.get(code)
                        if df is not None and not df.empty:
                            price = df.iloc[-1]["close"]
                            trade = self.engine.execute_sell(code, price, now, "MARKET_CLOSE")
                            if trade:
                                self.server.send_order(code, "SELL", trade.qty, 0)

                # 분봉 주기에 맞춰 대기
                sleep_sec = TICK * 60 - (now.second + now.microsecond / 1e6)
                if sleep_sec < 5:
                    sleep_sec += TICK * 60
                time.sleep(min(sleep_sec, TICK * 60))

        thread = threading.Thread(target=trade_loop, daemon=True)
        thread.start()

        if self.chart_ui:
            self.chart_ui.show(block=True)
        else:
            # 차트 없이 콘솔 모드
            log.info("차트 없이 콘솔 모드 실행. Ctrl+C로 종료.")
            try:
                while self.running:
                    time.sleep(1)
            except KeyboardInterrupt:
                log.info("종료 요청...")

        self.running = False
        self.db.close()


# ═════════════════════════════════════════════════════════════════════════════
# 메인 엔트리 포인트
# ═════════════════════════════════════════════════════════════════════════════
def print_usage():
    print("""
╔══════════════════════════════════════════════════════════════╗
║  삼성전자 동기화 매매 시스템 (sync_trader.py)               ║
╠══════════════════════════════════════════════════════════════╣
║  사용법:                                                    ║
║                                                              ║
║  python sync_trader.py backtest                              ║
║    → 6개월 전체 백테스트                                     ║
║                                                              ║
║  python sync_trader.py backtest 20260101 20260315            ║
║    → 지정 기간 백테스트                                      ║
║                                                              ║
║  python sync_trader.py intraday 20260314                     ║
║    → 특정 일자 장중 시뮬레이션                               ║
║                                                              ║
║  python sync_trader.py chart 20260314                        ║
║    → 특정 일자 차트 뷰어 (백테스트 + 시각화)                 ║
║                                                              ║
║  python sync_trader.py chart 20260101 20260315               ║
║    → 기간 차트 뷰어                                          ║
║                                                              ║
║  python sync_trader.py live                                  ║
║    → 실시간 매매 (Server32 연동)                             ║
║                                                              ║
║  python sync_trader.py live --no-chart                       ║
║    → 실시간 매매 (차트 없이 콘솔 모드)                       ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
    """)


def main():
    if len(sys.argv) < 2:
        print_usage()
        return

    command = sys.argv[1].lower()

    # ─── 백테스트 ───
    if command == "backtest":
        bt = Backtester()
        try:
            if len(sys.argv) >= 4:
                start = datetime.strptime(sys.argv[2], "%Y%m%d").date()
                end = datetime.strptime(sys.argv[3], "%Y%m%d").date()
                result = bt.run_full_backtest(start, end)
            else:
                result = bt.run_full_backtest()

            # 차트 표시 (lightweight-charts 설치 시)
            if Chart is not None and result.get("trades"):
                viewer = ChartBacktestViewer()
                start_str = result["period_start"].strftime("%Y%m%d") \
                    if isinstance(result["period_start"], date) else str(result["period_start"])
                end_str = result["period_end"].strftime("%Y%m%d") \
                    if isinstance(result["period_end"], date) else str(result["period_end"])
                viewer.view_period(start_str, end_str, result["trades"])
                viewer.close()
        finally:
            bt.close()

    # ─── 장중 시뮬레이션 ───
    elif command == "intraday":
        if len(sys.argv) < 3:
            print("사용법: python sync_trader.py intraday YYYYMMDD")
            return
        target_day = sys.argv[2]
        bt = Backtester()
        try:
            result = bt.run_intraday(target_day)

            if Chart is not None and result.get("trades"):
                viewer = ChartBacktestViewer()
                viewer.view_day(target_day, result["trades"])
                viewer.close()
        finally:
            bt.close()

    # ─── 차트 뷰어 ───
    elif command == "chart":
        if Chart is None:
            print("lightweight-charts 미설치. pip install lightweight-charts")
            return

        viewer = ChartBacktestViewer()
        try:
            if len(sys.argv) >= 4:
                viewer.view_period(sys.argv[2], sys.argv[3])
            elif len(sys.argv) >= 3:
                # 백테스트 실행 후 차트 표시
                bt = Backtester()
                result = bt.run_intraday(sys.argv[2])
                viewer.view_day(sys.argv[2], result.get("trades", []))
                bt.close()
            else:
                print("사용법: python sync_trader.py chart YYYYMMDD [YYYYMMDD]")
        finally:
            viewer.close()

    # ─── 실시간 매매 ───
    elif command == "live":
        with_chart = "--no-chart" not in sys.argv
        handler = RealtimeHandler()
        handler.run(with_chart=with_chart)

    else:
        print_usage()


if __name__ == "__main__":
    main()
