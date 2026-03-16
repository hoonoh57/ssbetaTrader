"""
=============================================================================
분봉 데이터 다운로더
- Server32 API에서 분봉 연속조회 → MySQL stock_minutes DB 저장
- 키움 API 제한: 1초 5회, 1분 100회 → 안전하게 0.5초 간격
- 1회 조회 최대 900봉 → stopTime 이동하며 반복
=============================================================================
"""

import requests
import mysql.connector
import time
import logging
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('logs/minute_download.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("MinuteDownloader")

# ─── 설정 (.env 파일에서 로드) ───
BASE_URL = os.getenv("SERVER_API_BASE_URL", "http://localhost:8082")
MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "localhost"),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "stock_minutes"),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4")
}

# 대상 종목
TARGET_STOCKS = {
    "005930": "삼성전자",
    "005935": "삼성전자우",
    "067310": "하나마이크론",
    "131970": "두산테스나",
    "356860": "티엘비",
    "240810": "원익IPS",
    "095340": "ISC",
    "330860": "네패스아크",
}

# 다운로드 설정
TICK_UNITS = [int(t) for t in os.getenv("TICK_UNITS", "5").split(",")]
MONTHS_BACK = int(os.getenv("MONTHS_BACK", "6"))
API_DELAY = float(os.getenv("API_DELAY", "0.55"))
MAX_ROWS_PER_REQUEST = int(os.getenv("MAX_ROWS_PER_REQUEST", "900"))


class MinuteDownloader:
    """분봉 데이터 연속조회 + DB 저장"""

    def __init__(self):
        self.conn = mysql.connector.connect(**MYSQL_CONFIG)
        self.conn.autocommit = False
        self.cursor = self.conn.cursor()
        self.session = requests.Session()

    def close(self):
        self.conn.close()

    # ─── 서버 API 조회 ───
    def fetch_minute_candles(self, code: str, tick: int,
                              stop_time: str) -> List[Dict]:
        """
        서버에서 분봉 조회
        stop_time: yyyyMMddHHmmss — 이 시각 이전 데이터를 가져옴
        반환: 최대 900봉 (최신→과거 순)
        """
        url = f"{BASE_URL}/api/market/candles/minute"
        params = {"code": code, "tick": tick, "stopTime": stop_time}
        try:
            r = self.session.get(url, params=params, timeout=30)
            res = r.json()
            if res.get("Success") and res.get("Data"):
                return res["Data"]
            else:
                log.warning(f"  조회 실패: {res.get('Message', 'unknown')}")
                return []
        except Exception as e:
            log.error(f"  API 오류: {e}")
            return []

    # ─── DB 저장 ───
    def save_candles(self, code: str, tick: int, rows: List[Dict]) -> int:
        """분봉 데이터를 DB에 UPSERT"""
        if not rows:
            return 0

        sql = """
            INSERT INTO minute_candles
                (code, tick, dt, open, high, low, close, volume, trade_value, change_pct)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                open=VALUES(open), high=VALUES(high), low=VALUES(low),
                close=VALUES(close), volume=VALUES(volume),
                trade_value=VALUES(trade_value), change_pct=VALUES(change_pct)
        """

        saved = 0
        batch = []
        for row in rows:
            try:
                # 서버 응답 컬럼 매핑
                dt_str = row.get("체결시간", "")
                if not dt_str or len(dt_str) < 14:
                    continue

                dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
                o = abs(int(float(str(row.get("시가", "0")).replace(",", ""))))
                h = abs(int(float(str(row.get("고가", "0")).replace(",", ""))))
                l = abs(int(float(str(row.get("저가", "0")).replace(",", ""))))
                c = abs(int(float(str(row.get("현재가", "0")).replace(",", ""))))
                v = abs(int(float(str(row.get("거래량", "0")).replace(",", ""))))

                if c == 0:
                    continue

                tv = int(c * v / 1_000_000) if v > 0 else 0  # 거래대금(백만원)
                chg = None  # 등락률은 후처리

                batch.append((code, tick, dt, o, h, l, c, v, tv, chg))
                saved += 1

                # 1000건씩 배치 커밋
                if len(batch) >= 1000:
                    self.cursor.executemany(sql, batch)
                    self.conn.commit()
                    batch = []

            except Exception as e:
                log.warning(f"  행 파싱 오류: {e} / row={row}")
                continue

        if batch:
            self.cursor.executemany(sql, batch)
            self.conn.commit()

        return saved

    # ─── 진행 상태 관리 ───
    def get_progress(self, code: str, tick: int) -> Optional[Dict]:
        self.cursor.execute(
            "SELECT last_datetime, total_rows, status FROM download_progress "
            "WHERE code=%s AND tick=%s", (code, tick)
        )
        row = self.cursor.fetchone()
        if row:
            return {"last_datetime": row[0], "total_rows": row[1], "status": row[2]}
        return None

    def update_progress(self, code: str, tick: int, last_dt: datetime,
                         total: int, status: str = "running"):
        sql = """
            INSERT INTO download_progress (code, tick, last_datetime, total_rows, status)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                last_datetime=VALUES(last_datetime),
                total_rows=VALUES(total_rows),
                status=VALUES(status),
                finished_at=IF(VALUES(status)='done', NOW(), NULL)
        """
        self.cursor.execute(sql, (code, tick, last_dt, total, status))
        self.conn.commit()

    # ─── 메인 다운로드 로직 ───
    def download_stock(self, code: str, tick: int, target_start: datetime):
        """
        특정 종목의 분봉을 target_start까지 연속조회하여 저장

        서버 API는 stopTime 이전의 최신 900봉을 반환하므로,
        첫 요청: stopTime = 현재시각 → 최신 900봉
        다음 요청: stopTime = 이전 결과의 가장 오래된 시각 → 이전 900봉
        ... target_start에 도달할 때까지 반복
        """
        name = TARGET_STOCKS.get(code, code)
        log.info(f"{'='*60}")
        log.info(f"다운로드 시작: {name} ({code}) / {tick}분봉")
        log.info(f"목표 기간: {target_start.strftime('%Y-%m-%d')} ~ 현재")

        # 이어받기 확인
        progress = self.get_progress(code, tick)
        if progress and progress["status"] == "done":
            log.info(f"  이미 완료됨 (총 {progress['total_rows']}행). 스킵.")
            return

        # 시작점 결정
        now = datetime.now()
        stop_time = now.strftime("%Y%m%d%H%M%S")
        total_saved = progress["total_rows"] if progress else 0
        request_count = 0

        while True:
            # API 호출
            rows = self.fetch_minute_candles(code, tick, stop_time)
            request_count += 1

            if not rows:
                log.info(f"  데이터 없음. 다운로드 종료.")
                break

            # DB 저장
            saved = self.save_candles(code, tick, rows)
            total_saved += saved

            # 가장 오래된 시각 추출 (다음 요청의 stopTime)
            oldest_dt_str = None
            for row in rows:
                dt_str = row.get("체결시간", "")
                if dt_str and (oldest_dt_str is None or dt_str < oldest_dt_str):
                    oldest_dt_str = dt_str

            if not oldest_dt_str:
                break

            oldest_dt = datetime.strptime(oldest_dt_str, "%Y%m%d%H%M%S")

            # 목표 시작일 도달 확인
            if oldest_dt <= target_start:
                log.info(f"  목표 시작일 도달: {oldest_dt}")
                break

            # 진행 상태 업데이트
            self.update_progress(code, tick, oldest_dt, total_saved)

            log.info(
                f"  요청 #{request_count}: {saved}봉 저장 "
                f"(누적 {total_saved}) / 마지막: {oldest_dt_str}"
            )

            # 900봉 미만 = 더 이상 데이터 없음
            if len(rows) < MAX_ROWS_PER_REQUEST:
                log.info(f"  데이터 끝 도달 ({len(rows)}봉 < {MAX_ROWS_PER_REQUEST})")
                break

            # 다음 요청 준비
            stop_time = oldest_dt_str

            # API 쿨다운
            time.sleep(API_DELAY)

        # 완료 처리
        self.update_progress(code, tick, target_start, total_saved, "done")
        log.info(f"  완료: 총 {total_saved}봉 저장 / {request_count}회 요청")

    # ─── 등락률 후처리 ───
    def compute_change_pct(self, code: str, tick: int):
        """저장된 데이터의 전봉대비 등락률 계산"""
        sql = """
            UPDATE minute_candles mc
            INNER JOIN (
                SELECT id, code, tick, dt, close,
                       LAG(close) OVER (PARTITION BY code, tick ORDER BY dt) AS prev_close
                FROM minute_candles
                WHERE code = %s AND tick = %s
            ) sub ON mc.id = sub.id
            SET mc.change_pct = ROUND((sub.close - sub.prev_close) / sub.prev_close * 100, 2)
            WHERE sub.prev_close IS NOT NULL AND sub.prev_close > 0
        """
        self.cursor.execute(sql, (code, tick))
        self.conn.commit()
        log.info(f"  {TARGET_STOCKS.get(code, code)} 등락률 계산 완료")

    # ─── 전체 실행 ───
    def run(self):
        """모든 종목 + 모든 분봉단위 다운로드"""
        target_start = datetime.now() - timedelta(days=MONTHS_BACK * 30)

        log.info("=" * 60)
        log.info(f"분봉 데이터 다운로드 시작")
        log.info(f"대상: {len(TARGET_STOCKS)}종목 / 분봉단위: {TICK_UNITS}")
        log.info(f"기간: {target_start.strftime('%Y-%m-%d')} ~ 현재")
        log.info(f"예상 데이터: ~{len(TARGET_STOCKS) * 78 * 125 * len(TICK_UNITS):,}행 (5분봉 기준)")
        log.info("=" * 60)

        start_time = time.time()

        for tick in TICK_UNITS:
            for code, name in TARGET_STOCKS.items():
                try:
                    self.download_stock(code, tick, target_start)
                    # 등락률 계산
                    self.compute_change_pct(code, tick)
                except Exception as e:
                    log.error(f"다운로드 실패: {name} ({code}): {e}")
                    self.update_progress(code, tick, target_start, 0, "error")
                    continue

        elapsed = time.time() - start_time
        log.info(f"\n전체 다운로드 완료: {elapsed/60:.1f}분 소요")

        # 통계 출력
        self.print_stats()

    def print_stats(self):
        """저장 현황 출력"""
        self.cursor.execute("""
            SELECT code, tick,
                   COUNT(*) AS cnt,
                   MIN(dt) AS min_dt,
                   MAX(dt) AS max_dt
            FROM minute_candles
            GROUP BY code, tick
            ORDER BY code, tick
        """)
        log.info("\n" + "=" * 70)
        log.info(f"{'종목':>8} {'분봉':>4} {'건수':>8} {'시작일':>12} {'종료일':>12}")
        log.info("-" * 70)
        for row in self.cursor.fetchall():
            code, tick, cnt, min_dt, max_dt = row
            name = TARGET_STOCKS.get(code, code)
            log.info(f"{name:>8} {tick:>4} {cnt:>8,} {str(min_dt)[:10]:>12} {str(max_dt)[:10]:>12}")
        log.info("=" * 70)


# ─── 일간 증분 다운로드 (스케줄러용) ───
class IncrementalDownloader(MinuteDownloader):
    """매일 장 마감 후 당일 분봉만 추가 다운로드"""

    def run_today(self):
        today = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        log.info(f"증분 다운로드: {today.strftime('%Y-%m-%d')} 당일분")

        for tick in TICK_UNITS:
            for code, name in TARGET_STOCKS.items():
                try:
                    self.download_stock(code, tick, today)
                    self.compute_change_pct(code, tick)
                except Exception as e:
                    log.error(f"증분 다운로드 실패: {name}: {e}")


if __name__ == "__main__":
    import sys
    downloader = MinuteDownloader()
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "incremental":
            IncrementalDownloader().run_today()
        else:
            downloader.run()
    finally:
        downloader.close()
