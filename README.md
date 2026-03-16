# 삼성전자 동기화 매매 시스템 (ssbetaTrader)

6개월 분봉 데이터 기반의 극세밀 패턴 분석 및 자동 매매 시스템

## 🎯 주요 특징

### 1. 극세밀 분봉 DB 인프라
- **6개월 × 8종목 × 분봉 단위** 데이터 저장 (5분봉 기준 약 78,000행)
- MySQL 월별 파티션으로 조회 성능 최적화
- 다운로드 진행 상태 추적 → 중단 시 이어받기 가능

### 2. 정량화된 패턴 분석
- **시차 패턴**: "삼성전자 정배열 확정 후 하나마이크론 골든크로스까지 평균 8.3분"
- **변동 배율**: 각 종목의 삼성전자 대비 변동률 (베타)
- **반응 속도**: 시간대별 급등 반응 시간 분석
- **승률/수익률**: 매매 타이밍별 역사적 통계

### 3. 극세밀 거래 전략
- DB의 분석 결과를 기반으로 **종목별 최적 진입 타이밍** 자동 계산
- **동적 포지션 사이징**: 상관계수, 배율, 승률로 가중치 결정
- **시간대별 매매 가능/불가** 자동 판단
- **손절 기준 동적 조정**: 변동성에 따라 손절폭 자동 설정

## 📊 데이터 규모

| 항목 | 규모 |
|------|------|
| 기간 | 6개월 (약 125거래일) |
| 종목 | 8개 (삼성전자 + 고베타 7종목) |
| 봉 단위 | 5분봉 기준 |
| 일간 봉 수 | 약 78봉 (09:00~15:30) |
| 월간 데이터 | 약 9,750행 |
| 총 데이터 | 약 78,000행 |

## 🗂️ 폴더 구조

```
ssbetaTrader/
├── sql/
│   └── schema.sql              # MySQL 스키마 정의
├── src/
│   ├── minute_downloader.py    # 분봉 데이터 다운로더
│   ├── pattern_analyzer.py     # 패턴 분석기
│   ├── precision_engine.py     # 극세밀 전략 엔진
│   └── sync_trader.py          # 매매 트레이더 (별도 구현)
├── scripts/
│   └── sync_trading_system.ps1 # 운영 스크립트 (Windows PowerShell)
├── logs/                        # 실행 로그 저장
├── data/                        # 백테스트 결과 등 저장
├── requirements.txt            # Python 의존성
└── README.md                   # 이 파일
```

## 🚀 빠른 시작

### 1. 사전 준비

**필수 요구사항:**
- Python 3.8+
- MySQL 8.0+
- 키움 Server32 API 서버 (http://localhost:8082)

**설치:**
```bash
pip install -r requirements.txt
```

### 2. DB 셋업

```bash
# MySQL에 직접 실행 (Windows 터미널에서)
mysql -u root -p < sql/schema.sql
```

또는 MySQL Workbench에서 `sql/schema.sql` 파일을 열어 실행

### 3. 초기 셋업 (6개월 데이터 다운로드)

```powershell
# Windows PowerShell에서
.\scripts\sync_trading_system.ps1 setup
```

**소요 시간:**
- 다운로드: 30~60분 (API 레이트 제한)
- 분석: 5~10분
- 총: 약 1~2시간

### 4. 매일 증분 업데이트

```powershell
# 매일 장 마감 후 실행
.\scripts\sync_trading_system.ps1 daily
```

**권장:** Windows 작업 스케줄러에 등록 (16:30 자동 실행)

## 📈 분석 기능

### 시차 패턴 분석 (`pattern_analyzer.py`)

**분석 대상:**
- 삼성전자 추세 확정 (MA5 > MA10 > MA20 + 양봉) 시점
- 각 종목의 골든크로스 (MA5 > MA10 상향돌파)까지의 시차(분)

**결과 예시:**
```
하나마이크론 (067310):
  시차: 평균 8.3분 / 중앙값 6.0분 / 표준편차 4.2분
  배율: 평균 2.1x
  승률: 67.5% / 평균수익: 0.85%
  샘플: 42회
```

### 변동 배율 분석

**분석 대상:**
- 삼성전자 상승일(>0.5%) 대비 각 종목의 변동률

**결과 예시:**
```
원익IPS (240810):
  배율: 2.3x (95% 신뢰도)
  상관계수: 0.82
  동방향 확률: 94.2%
```

### 시간대별 반응 속도

**분석 대상:**
- 삼성전자 급등 봉(1% 이상) 후 30분 이내 각 종목의 반응

**결과 예시:**
```
두산테스나 (131970):
  전체 반응률: 72.3%
  09시: 평균 반응 3.2분 (15회)
  10시: 평균 반응 5.1분 (12회)
  ...
```

## 🛠️ 설정 변경

### 다운로드 대상 종목 변경

`src/minute_downloader.py`:
```python
TARGET_STOCKS = {
    "005930": "삼성전자",      # 신호 종목 (필수)
    "005935": "삼성전자우",
    "067310": "하나마이크론",
    # 추가/변경...
}
```

### 분봉 단위 변경

`src/minute_downloader.py`:
```python
TICK_UNITS = [5]                        # 5분봉
TICK_UNITS = [1, 5, 10, 15, 30, 60]   # 여러 단위 동시 다운로드
```

### MySQL 연결 설정

모든 Python 파일의 `MYSQL_CONFIG`:
```python
MYSQL_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "your_password",  # 변경
    "database": "stock_minutes",
    "charset": "utf8mb4"
}
```

### API 레이트 제한

`src/minute_downloader.py`:
```python
API_DELAY = 0.55    # 요청 간격 (초)
                    # 키움 1초 5회 제한 → 약 0.2초 최소
                    # 안정성을 위해 0.5~1.0초 권장
```

## 📊 DB 스키마

### minute_candles (분봉 데이터)
```sql
code        CHAR(6)              # 종목코드
tick        TINYINT              # 분봉 단위 (1,5,10,...)
dt          DATETIME             # 캔들 시작 시각
open, high, low, close  INT     # OHLC
volume      BIGINT               # 거래량
trade_value BIGINT               # 거래대금(백만원)
change_pct  DECIMAL(6,2)        # 전봉대비 등락률(%)
```

**월별 파티션:**
- p202509: 2025-09
- p202510: 2025-10
- ...
- p202603: 2026-03
- p_future: 2026-04 이후

### sync_patterns (분석 결과)
```sql
signal_code        CHAR(6)      # 신호 종목 (삼성전자)
target_code        CHAR(6)      # 대상 종목
pattern_type       VARCHAR(50)  # 'aligned_to_golden', 'daily_beta', ...
avg_lag_minutes    DECIMAL      # 평균 시차
median_lag_minutes DECIMAL      # 중앙값
std_lag_minutes    DECIMAL      # 표준편차
avg_beta           DECIMAL      # 평균 배율
sample_count       INT          # 표본 수
win_rate           DECIMAL      # 승률(%)
avg_pnl            DECIMAL      # 평균 수익률(%)
detail_json        JSON         # 상세 통계
```

### download_progress (다운로드 진행 상태)
```sql
code               CHAR(6)      # 종목코드
tick               TINYINT      # 분봉 단위
last_datetime      DATETIME     # 마지막 수신 캔들
total_rows         INT          # 누적 저장 행수
status             ENUM         # 'running', 'done', 'error'
```

## 🧪 백테스트

```powershell
# 특정 날짜 장중 백테스트
.\scripts\sync_trading_system.ps1 backtest 20260317

# 오늘 백테스트
.\scripts\sync_trading_system.ps1 backtest
```

**결과 항목:**
- 총 거래 수
- 승률 / 평균 수익률
- 최대 낙폭 (MDD)
- 샤프 지수

## 📝 로그 확인

```bash
# 실시간 로그 확인
type logs/minute_download.log

# 또는 PowerShell
Get-Content -Path logs/minute_download.log -Tail 50 -Wait
```

## ⚠️ 주의사항

1. **실제 거래 전 반드시 백테스트** 실행
2. **API 레이트 제한** 준수 (키움 1초 5회)
3. **장 시간 (09:00~15:30)** 데이터만 수집
4. **오래된 데이터** 정기적으로 DROP PARTITION으로 정리
5. **손절 기준** 반드시 설정 후 거래 시작

## 🔧 문제 해결

### 1. MySQL 연결 오류
```
mysql.connector.errors.ProgrammingError: 1045 (28000): Access denied
```
→ `MYSQL_CONFIG` 의 user/password 확인

### 2. API 조회 실패
```
조회 실패: unknown
```
→ Server32 API 서버(http://localhost:8082) 실행 확인

### 3. 데이터 없음
```
데이터 없음. 다운로드 종료.
```
→ 종목코드 확인, 또는 발생 시간(09:00~15:30 외) 확인

## 📚 참고 자료

- [MySQL 분봉 최적화](https://dev.mysql.com/doc/)
- [판다스 시계열](https://pandas.pydata.org/docs/)
- [키움 API 문서](http://www3.kiwoom.com/h/common/system/apidown/)

## 📄 라이선스

Private / 개인 사용 전용

## 👨‍💻 연락처

Issues 또는 GitHub 토론 참조
