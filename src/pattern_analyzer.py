"""
=============================================================================
동기화 패턴 분석기
- 6개월 분봉 DB에서 삼성전자 ↔ 고베타 종목 간 시차/배율 패턴 추출
- 분석 결과를 sync_patterns 테이블에 저장
=============================================================================
"""

import pandas as pd
import numpy as np
import mysql.connector
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("PatternAnalyzer")

MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "localhost"),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "stock_minutes"),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4")
}

SIGNAL_CODE = "005930"
TARGET_CODES = ["005935", "067310", "131970", "356860", "240810", "095340", "330860"]

STOCK_NAMES = {
    "005930": "삼성전자", "005935": "삼성전자우", "067310": "하나마이크론",
    "131970": "두산테스나", "356860": "티엘비", "240810": "원익IPS",
    "095340": "ISC", "330860": "네패스아크",
}


class PatternAnalyzer:

    def __init__(self, tick: int = 5):
        self.tick = tick
        self.conn = mysql.connector.connect(**MYSQL_CONFIG)

    def close(self):
        self.conn.close()

    # ─── 데이터 로드 ───
    def load_minutes(self, code: str, start_date: str = None,
                      end_date: str = None) -> pd.DataFrame:
        """분봉 데이터 로드"""
        where = "code=%s AND tick=%s"
        params = [code, self.tick]
        if start_date:
            where += " AND dt >= %s"
            params.append(start_date)
        if end_date:
            where += " AND dt <= %s"
            params.append(end_date)

        df = pd.read_sql(
            f"SELECT dt, open, high, low, close, volume, change_pct "
            f"FROM minute_candles WHERE {where} ORDER BY dt",
            self.conn, params=params
        )
        df["dt"] = pd.to_datetime(df["dt"])
        df["date"] = df["dt"].dt.date
        return df

    # ─── MA 계산 ───
    def add_ma(self, df: pd.DataFrame, periods: List[int] = [5, 10, 20]) -> pd.DataFrame:
        """각 거래일 내에서 MA 계산 (장 간 연결 방지)"""
        result = []
        for date, group in df.groupby("date"):
            g = group.copy()
            for p in periods:
                g[f"ma{p}"] = g["close"].rolling(window=p, min_periods=p).mean()
            result.append(g)
        return pd.concat(result).reset_index(drop=True)

    # ─── 정배열 구간 탐지 ───
    def detect_aligned_periods(self, df: pd.DataFrame) -> pd.DataFrame:
        """MA5 > MA10 > MA20 정배열 구간 탐지"""
        df = df.copy()
        df["aligned"] = (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"])
        df["aligned_start"] = df["aligned"] & ~df["aligned"].shift(1, fill_value=False)
        df["bullish_candle"] = df["close"] > df["open"]
        # 정배열 + 양봉 = 추세 확정
        df["trend_confirmed"] = df["aligned"] & df["bullish_candle"]
        df["trend_start"] = df["trend_confirmed"] & ~df["trend_confirmed"].shift(1, fill_value=False)
        return df

    # ─── 골든크로스 탐지 ───
    def detect_golden_cross(self, df: pd.DataFrame) -> pd.DataFrame:
        """MA5가 MA10을 상향돌파하는 시점 탐지"""
        df = df.copy()
        df["gc"] = (df["ma5"] > df["ma10"]) & (df["ma5"].shift(1) <= df["ma10"].shift(1))
        return df

    # ─── 핵심 분석: 시차 패턴 ───
    def analyze_lag_pattern(self, start_date: str = None,
                            end_date: str = None) -> Dict:
        """
        삼성전자 정배열+양봉 확정 시점 → 각 종목 골든크로스 발생 시점
        의 시차(분)를 6개월간 통계로 산출
        """
        log.info("=" * 70)
        log.info("시차 패턴 분석 시작")
        log.info("=" * 70)

        # 삼성전자 데이터
        df_sig = self.load_minutes(SIGNAL_CODE, start_date, end_date)
        df_sig = self.add_ma(df_sig)
        df_sig = self.detect_aligned_periods(df_sig)

        # 추세 확정 시점 추출
        trend_starts = df_sig[df_sig["trend_start"]].copy()
        log.info(f"삼성전자 추세 확정 시점: {len(trend_starts)}회 감지")

        results = {}

        for target_code in TARGET_CODES:
            name = STOCK_NAMES.get(target_code, target_code)
            log.info(f"\n─── {name} ({target_code}) 분석 ───")

            df_tgt = self.load_minutes(target_code, start_date, end_date)
            if df_tgt.empty:
                log.warning(f"  데이터 없음. 스킵.")
                continue
            df_tgt = self.add_ma(df_tgt)
            df_tgt = self.detect_golden_cross(df_tgt)

            lag_minutes = []
            beta_ratios = []
            outcomes = []  # (pnl_pct, hold_minutes)

            for _, ts_row in trend_starts.iterrows():
                ts_time = ts_row["dt"]
                ts_date = ts_row["date"]
                ts_price = ts_row["close"]

                # 같은 날 해당 종목의 골든크로스 찾기
                tgt_day = df_tgt[(df_tgt["date"] == ts_date) & (df_tgt["dt"] > ts_time)]
                gc_rows = tgt_day[tgt_day["gc"]]

                if gc_rows.empty:
                    continue

                # 첫 번째 골든크로스
                first_gc = gc_rows.iloc[0]
                lag = (first_gc["dt"] - ts_time).total_seconds() / 60
                lag_minutes.append(lag)

                # 변동 배율 계산
                sig_after = df_sig[
                    (df_sig["date"] == ts_date) & (df_sig["dt"] >= ts_time)
                ]
                tgt_after = df_tgt[
                    (df_tgt["date"] == ts_date) & (df_tgt["dt"] >= first_gc["dt"])
                ]

                if len(sig_after) >= 2 and len(tgt_after) >= 2:
                    # 골든크로스 이후 장 마감까지의 수익률
                    sig_ret = (sig_after.iloc[-1]["close"] - ts_price) / ts_price * 100
                    tgt_entry = first_gc["close"]
                    tgt_ret = (tgt_after.iloc[-1]["close"] - tgt_entry) / tgt_entry * 100
                    hold_min = (tgt_after.iloc[-1]["dt"] - first_gc["dt"]).total_seconds() / 60

                    if abs(sig_ret) > 0.01:
                        beta_ratios.append(tgt_ret / sig_ret)
                    outcomes.append((tgt_ret, hold_min))

            # 통계 집계
            if lag_minutes:
                stats = {
                    "avg_lag": round(np.mean(lag_minutes), 1),
                    "median_lag": round(np.median(lag_minutes), 1),
                    "std_lag": round(np.std(lag_minutes), 1),
                    "min_lag": round(min(lag_minutes), 1),
                    "max_lag": round(max(lag_minutes), 1),
                    "avg_beta": round(np.mean(beta_ratios), 2) if beta_ratios else None,
                    "sample_count": len(lag_minutes),
                    "win_rate": round(
                        len([o for o in outcomes if o[0] > 0]) / len(outcomes) * 100, 1
                    ) if outcomes else None,
                    "avg_pnl": round(np.mean([o[0] for o in outcomes]), 2) if outcomes else None,
                    "avg_hold_min": round(np.mean([o[1] for o in outcomes]), 0) if outcomes else None,
                }

                log.info(f"  시차: 평균 {stats['avg_lag']}분 / 중앙값 {stats['median_lag']}분 "
                         f"/ 표준편차 {stats['std_lag']}분")
                log.info(f"  배율: 평균 {stats['avg_beta']}x")
                log.info(f"  승률: {stats['win_rate']}% / 평균수익 {stats['avg_pnl']}%")
                log.info(f"  샘플: {stats['sample_count']}회")

                results[target_code] = stats

                # DB 저장
                self._save_pattern(SIGNAL_CODE, target_code, "aligned_to_golden", stats)
            else:
                log.info(f"  매칭 패턴 없음")

        return results

    # ─── 변동 배율 일간 분석 ───
    def analyze_daily_beta(self, start_date: str = None,
                            end_date: str = None) -> Dict:
        """
        삼성전자 일간 등락률 대비 각 종목의 변동 배율을 날짜별로 산출
        """
        log.info("\n" + "=" * 70)
        log.info("일간 변동 배율 분석")
        log.info("=" * 70)

        df_sig = self.load_minutes(SIGNAL_CODE, start_date, end_date)
        sig_daily = self._calc_daily_return(df_sig)

        results = {}
        for target_code in TARGET_CODES:
            name = STOCK_NAMES.get(target_code, target_code)
            df_tgt = self.load_minutes(target_code, start_date, end_date)
            if df_tgt.empty:
                continue
            tgt_daily = self._calc_daily_return(df_tgt)

            # 병합
            merged = pd.merge(sig_daily, tgt_daily, on="date",
                              suffixes=("_sig", "_tgt"))
            if merged.empty:
                continue

            # 삼성전자 상승일만 필터
            up_days = merged[merged["daily_ret_sig"] > 0.5]  # +0.5% 이상 상승일

            if len(up_days) > 0:
                betas = up_days["daily_ret_tgt"] / up_days["daily_ret_sig"]
                stats = {
                    "avg_beta": round(betas.mean(), 2),
                    "median_beta": round(betas.median(), 2),
                    "std_beta": round(betas.std(), 2),
                    "correlation": round(
                        merged["daily_ret_sig"].corr(merged["daily_ret_tgt"]), 3
                    ),
                    "up_day_count": len(up_days),
                    "same_direction_pct": round(
                        len(up_days[up_days["daily_ret_tgt"] > 0]) / len(up_days) * 100, 1
                    ),
                }
                log.info(f"  {name}: 배율 {stats['avg_beta']}x / 상관 {stats['correlation']} "
                         f"/ 동방향 {stats['same_direction_pct']}%")
                results[target_code] = stats
                self._save_pattern(SIGNAL_CODE, target_code, "daily_beta", stats)

        return results

    # ─── 장중 시간대별 반응 속도 분석 ───
    def analyze_intraday_response(self, start_date: str = None,
                                    end_date: str = None) -> Dict:
        """
        삼성전자가 특정 시간대에 급등할 때,
        각 종목이 몇 분 후에 반응하는지 시간대별 분석
        """
        log.info("\n" + "=" * 70)
        log.info("장중 시간대별 반응 속도 분석")
        log.info("=" * 70)

        df_sig = self.load_minutes(SIGNAL_CODE, start_date, end_date)

        # 삼성전자 5분봉 등락률 > 1% 인 "급등 봉" 추출
        df_sig["ret"] = df_sig["close"].pct_change() * 100
        surges = df_sig[df_sig["ret"] > 1.0].copy()
        surges["hour"] = surges["dt"].dt.hour
        surges["minute"] = surges["dt"].dt.minute

        log.info(f"삼성전자 급등 봉(>1%): {len(surges)}회 감지")

        results = {}
        for target_code in TARGET_CODES:
            name = STOCK_NAMES.get(target_code, target_code)
            df_tgt = self.load_minutes(target_code, start_date, end_date)
            if df_tgt.empty:
                continue
            df_tgt["ret"] = df_tgt["close"].pct_change() * 100

            response_lags = []

            for _, surge in surges.iterrows():
                surge_time = surge["dt"]
                surge_date = surge["date"]

                # 같은 날 급등 후 30분 이내 대상 종목의 반응
                window_start = surge_time
                window_end = surge_time + timedelta(minutes=30)

                tgt_window = df_tgt[
                    (df_tgt["date"] == surge_date) &
                    (df_tgt["dt"] > window_start) &
                    (df_tgt["dt"] <= window_end) &
                    (df_tgt["ret"] > 0.5)  # 0.5% 이상 반응
                ]

                if not tgt_window.empty:
                    first_response = tgt_window.iloc[0]
                    lag = (first_response["dt"] - surge_time).total_seconds() / 60
                    response_lags.append({
                        "lag_min": lag,
                        "hour": surge["hour"],
                        "surge_ret": surge["ret"],
                        "response_ret": first_response["ret"],
                    })

            if response_lags:
                lag_df = pd.DataFrame(response_lags)

                # 시간대별 통계
                hourly_stats = {}
                for hour in sorted(lag_df["hour"].unique()):
                    h_data = lag_df[lag_df["hour"] == hour]
                    hourly_stats[int(hour)] = {
                        "avg_lag": round(h_data["lag_min"].mean(), 1),
                        "count": len(h_data),
                        "avg_response": round(h_data["response_ret"].mean(), 2),
                    }

                stats = {
                    "overall_avg_lag": round(lag_df["lag_min"].mean(), 1),
                    "overall_median_lag": round(lag_df["lag_min"].median(), 1),
                    "response_rate": round(len(response_lags) / len(surges) * 100, 1),
                    "sample_count": len(response_lags),
                    "hourly": hourly_stats,
                }

                log.info(f"  {name}: 평균반응 {stats['overall_avg_lag']}분 / "
                         f"반응률 {stats['response_rate']}% / "
                         f"샘플 {stats['sample_count']}")
                for h, hs in hourly_stats.items():
                    log.info(f"    {h}시: 평균 {hs['avg_lag']}분 ({hs['count']}회)")

                results[target_code] = stats
                self._save_pattern(SIGNAL_CODE, target_code, "intraday_response", stats)

        return results

    # ─── 유틸 ───
    def _calc_daily_return(self, df: pd.DataFrame) -> pd.DataFrame:
        """분봉 데이터에서 일간 수익률 계산"""
        daily = df.groupby("date").agg(
            open_price=("open", "first"),
            close_price=("close", "last")
        ).reset_index()
        daily["daily_ret"] = (daily["close_price"] - daily["open_price"]) / daily["open_price"] * 100
        return daily[["date", "daily_ret"]]

    def _save_pattern(self, signal_code: str, target_code: str,
                       pattern_type: str, stats: dict):
        """분석 결과를 DB에 저장"""
        cursor = self.conn.cursor()
        sql = """
            INSERT INTO sync_patterns
                (signal_code, target_code, pattern_type,
                 avg_lag_minutes, median_lag_minutes, std_lag_minutes,
                 avg_beta, sample_count, win_rate, avg_pnl, detail_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                avg_lag_minutes=VALUES(avg_lag_minutes),
                median_lag_minutes=VALUES(median_lag_minutes),
                std_lag_minutes=VALUES(std_lag_minutes),
                avg_beta=VALUES(avg_beta),
                sample_count=VALUES(sample_count),
                win_rate=VALUES(win_rate),
                avg_pnl=VALUES(avg_pnl),
                detail_json=VALUES(detail_json),
                computed_at=NOW()
        """
        cursor.execute(sql, (
            signal_code, target_code, pattern_type,
            stats.get("avg_lag"), stats.get("median_lag"),
            stats.get("std_lag"), stats.get("avg_beta"),
            stats.get("sample_count"), stats.get("win_rate"),
            stats.get("avg_pnl"), json.dumps(stats, ensure_ascii=False, default=str)
        ))
        self.conn.commit()
        cursor.close()


# ─── 전체 분석 실행 ───
def run_full_analysis():
    analyzer = PatternAnalyzer(tick=5)
    try:
        # 1) 시차 패턴
        lag_results = analyzer.analyze_lag_pattern()

        # 2) 일간 배율
        beta_results = analyzer.analyze_daily_beta()

        # 3) 장중 반응 속도
        response_results = analyzer.analyze_intraday_response()

        # 종합 리포트
        log.info("\n" + "=" * 70)
        log.info("종합 분석 리포트")
        log.info("=" * 70)

        for code in TARGET_CODES:
            name = STOCK_NAMES.get(code, code)
            lag = lag_results.get(code, {})
            beta = beta_results.get(code, {})
            resp = response_results.get(code, {})

            log.info(f"\n{name} ({code}):")
            if lag:
                log.info(f"  골든크로스 시차: 평균 {lag.get('avg_lag', '?')}분 "
                         f"(승률 {lag.get('win_rate', '?')}%)")
            if beta:
                log.info(f"  변동 배율: {beta.get('avg_beta', '?')}x "
                         f"(상관계수 {beta.get('correlation', '?')})")
            if resp:
                log.info(f"  급등 반응: 평균 {resp.get('overall_avg_lag', '?')}분 "
                         f"(반응률 {resp.get('response_rate', '?')}%)")

    finally:
        analyzer.close()


if __name__ == "__main__":
    run_full_analysis()
