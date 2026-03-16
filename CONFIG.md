# 환경 설정 가이드 (.env)

## 개요

`.env` 파일은 DB 비밀번호, API 설정 등 민감한 정보를 저장합니다.
- **자동으로 .gitignore에 등록됨**
- **절대 GitHub에 업로드되지 않음**
- **각 개발자/서버가 자신의 로컬 설정으로 관리**

## 설정 방법

### 1. .env 파일 수정

프로젝트 루트에서 `.env` 파일을 텍스트 에디터로 열고 수정합니다:

```bash
# Windows: 메모장 또는 VSCode에서 열기
code .env
```

### 2. 필수 설정 항목

```ini
# MySQL Database Configuration
MYSQL_HOST=localhost           # MySQL 서버 주소
MYSQL_USER=root                # DB 사용자명
MYSQL_PASSWORD=your_password   # DB 비밀번호 (중요!)
MYSQL_DATABASE=stock_minutes   # 데이터베이스명
MYSQL_CHARSET=utf8mb4          # 문자 인코딩

# Server API Configuration
SERVER_API_BASE_URL=http://localhost:8082  # 키움 Server32 API 주소

# Download Settings
MONTHS_BACK=6                  # 다운로드 기간(개월)
TICK_UNITS=5                   # 분봉 단위 (5분봉)
API_DELAY=0.55                 # API 요청 간격(초)
MAX_ROWS_PER_REQUEST=900       # 1회 조회 최대 봉 수
```

## 실제 사용 예시

### 예시 1: 기본 설정 (로컬 개발)

```ini
MYSQL_HOST=localhost
MYSQL_USER=root
MYSQL_PASSWORD=root
MYSQL_DATABASE=stock_minutes
MYSQL_CHARSET=utf8mb4

SERVER_API_BASE_URL=http://localhost:8082

MONTHS_BACK=6
TICK_UNITS=5
API_DELAY=0.55
MAX_ROWS_PER_REQUEST=900
```

### 예시 2: 원격 MySQL 서버

```ini
MYSQL_HOST=db.example.com
MYSQL_USER=trading_user
MYSQL_PASSWORD=ComplexPassword123!@#
MYSQL_DATABASE=stock_minutes
MYSQL_CHARSET=utf8mb4

SERVER_API_BASE_URL=http://192.168.1.100:8082

MONTHS_BACK=6
TICK_UNITS=5
API_DELAY=0.55
MAX_ROWS_PER_REQUEST=900
```

### 예시 3: 여러 분봉 단위 다운로드

```ini
# 1분봉, 5분봉, 10분봉 동시 다운로드
TICK_UNITS=1,5,10
```

## Python 코드에서 사용

모든 Python 파일에서 자동으로 로드됩니다:

```python
from dotenv import load_dotenv
import os

load_dotenv()  # .env 파일 로드

# 환경 변수 사용
db_password = os.getenv("MYSQL_PASSWORD")
db_host = os.getenv("MYSQL_HOST", "localhost")  # 기본값 지정 가능
```

## ⚠️ 보안 주의사항

1. **비밀번호 저장**
   ```ini
   # ❌ 절대 금지!
   MYSQL_PASSWORD=root

   # ✅ 복잡한 비밀번호 추천
   MYSQL_PASSWORD=Tr@d1ng2025!Secure#Pwd
   ```

2. **GitHub 확인**
   ```bash
   # .env가 실제로 추적되지 않는지 확인
   git status | grep .env  # 아무것도 표시되지 않음
   ```

3. **파일 권한 (Linux/Mac)**
   ```bash
   chmod 600 .env  # 소유자만 읽기/쓰기 가능
   ```

4. **실수로 푸시하지 않기**
   ```bash
   # .gitignore 확인
   cat .gitignore | grep .env
   # .env 가 표시되면 OK
   ```

## 여러 환경 관리

### 개발(dev), 스테이징(staging), 프로덕션(prod) 분리

```
.env              # 로컬 개발용 (실제 비밀번호)
.env.example      # 템플릿 (더미 값)
.env.prod         # 프로덕션 설정 (선택)
.env.staging      # 스테이징 설정 (선택)
```

**사용 방법:**
```bash
# 프로덕션 환경 사용
$env:ENV_FILE = ".env.prod"
python src/minute_downloader.py
```

## .env.example 생성 (팀 공유용)

비밀번호를 제외한 템플릿 파일:

```ini
# .env.example (GitHub에 커밋됨)
MYSQL_HOST=localhost
MYSQL_USER=root
MYSQL_PASSWORD=CHANGE_ME
MYSQL_DATABASE=stock_minutes
MYSQL_CHARSET=utf8mb4

SERVER_API_BASE_URL=http://localhost:8082

MONTHS_BACK=6
TICK_UNITS=5
API_DELAY=0.55
MAX_ROWS_PER_REQUEST=900
```

팀원이 프로젝트를 받으면:
```bash
cp .env.example .env
# 이후 자신의 DB 비밀번호로 수정
```

## 문제 해결

### Q: `.env` 파일을 수정했는데 적용이 안 되나요?

**A:** Python 프로세스 재시작 필요:
```bash
# 이전 프로세스 종료
Ctrl+C

# 다시 실행
python src/minute_downloader.py
```

### Q: 환경 변수가 인식 안 됨

**A:** 파일 위치 확인:
```bash
# .env 파일이 프로젝트 루트에 있는지 확인
ls -la .env  # Windows: dir .env

# 또는 절대 경로 지정
from dotenv import load_dotenv
load_dotenv("e:/2026/ssbetaTrader/.env")
```

### Q: 특정 환경에서만 다른 설정?

**A:** 환경 변수 오버라이드:
```bash
# PowerShell에서 임시 덮어쓰기
$env:MYSQL_PASSWORD = "temp_password"
python src/minute_downloader.py

# 또는 Python에서
import os
os.environ["MYSQL_PASSWORD"] = "temp_password"
```

## 체크리스트

프로젝트 시작 전 확인:

- [ ] `.env` 파일이 프로젝트 루트에 있는가?
- [ ] `.gitignore`에 `.env`가 등록되어 있는가?
- [ ] DB 비밀번호가 정확하게 입력되어 있는가?
- [ ] MySQL 서버가 실행 중인가?
- [ ] Server32 API가 실행 중인가? (http://localhost:8082)
- [ ] `pip install python-dotenv` 설치했는가?

## 참고 링크

- [python-dotenv 공식 문서](https://github.com/theskumar/python-dotenv)
- [MySQL 연결 설정](https://dev.mysql.com/doc/connector-python/en/)
- [GitHub .gitignore 가이드](https://git-scm.com/docs/gitignore)
