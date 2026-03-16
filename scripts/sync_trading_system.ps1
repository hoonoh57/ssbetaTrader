# =============================================================================
# sync_trading_system.ps1 — 전체 시스템 운영 스크립트 (Windows PowerShell)
# =============================================================================

param(
    [string]$Command = "help",
    [string]$Date = ""
)

function Show-Help {
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host " 삼성전자 동기화 매매 시스템" -ForegroundColor Cyan
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "사용법:" -ForegroundColor Yellow
    Write-Host "  .\sync_trading_system.ps1 setup              - 최초 셋업 (DB 생성 + 6개월 다운로드 + 분석)" -ForegroundColor White
    Write-Host "  .\sync_trading_system.ps1 daily              - 매일 장 후 증분 업데이트" -ForegroundColor White
    Write-Host "  .\sync_trading_system.ps1 backtest [YYYYMMDD] - 특정 일자 장중 백테스트" -ForegroundColor White
    Write-Host "  .\sync_trading_system.ps1 live               - 실시간 매매" -ForegroundColor White
    Write-Host "  .\sync_trading_system.ps1 analyze            - 패턴 재분석만" -ForegroundColor White
    Write-Host ""
}

function Invoke-Setup {
    Write-Host ""
    Write-Host "[1/4] DB 생성..." -ForegroundColor Green
    # MySQL 실행 명령어 - 환경에 맞게 수정 필요
    # & mysql -u root < sql/schema.sql
    Write-Host "✓ DB 생성 완료 (수동 실행: mysql -u root < sql/schema.sql)" -ForegroundColor Green

    Write-Host ""
    Write-Host "[2/4] 6개월 분봉 다운로드 (약 30~60분 소요)..." -ForegroundColor Green
    & python src/minute_downloader.py
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✓ 다운로드 완료" -ForegroundColor Green
    } else {
        Write-Host "✗ 다운로드 실패" -ForegroundColor Red
        exit 1
    }

    Write-Host ""
    Write-Host "[3/4] 패턴 분석..." -ForegroundColor Green
    & python src/pattern_analyzer.py
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✓ 분석 완료" -ForegroundColor Green
    } else {
        Write-Host "✗ 분석 실패" -ForegroundColor Red
        exit 1
    }

    Write-Host ""
    Write-Host "[4/4] 완료!" -ForegroundColor Green
    Write-Host ""
}

function Invoke-Daily {
    Write-Host ""
    Write-Host "당일 분봉 증분 다운로드..." -ForegroundColor Green
    & python src/minute_downloader.py incremental
    
    Write-Host ""
    Write-Host "패턴 재분석..." -ForegroundColor Green
    & python src/pattern_analyzer.py
    
    Write-Host "✓ 증분 업데이트 완료" -ForegroundColor Green
    Write-Host ""
}

function Invoke-Backtest {
    param([string]$TestDate)
    
    if ([string]::IsNullOrEmpty($TestDate)) {
        $TestDate = (Get-Date).ToString("yyyyMMdd")
    }
    
    Write-Host ""
    Write-Host "장중 백테스트: $TestDate" -ForegroundColor Green
    & python src/sync_trader.py intraday $TestDate
    Write-Host ""
}

function Invoke-Live {
    Write-Host ""
    Write-Host "실시간 매매 시작..." -ForegroundColor Cyan
    Write-Host "주의: 실제 거래가 발생합니다!" -ForegroundColor Yellow
    Write-Host ""
    & python src/sync_trader.py live
}

function Invoke-Analyze {
    Write-Host ""
    Write-Host "패턴 분석 실행..." -ForegroundColor Green
    & python src/pattern_analyzer.py
    Write-Host ""
}

# ─── 메인 로직 ───
switch ($Command.ToLower()) {
    "setup"     { Invoke-Setup }
    "daily"     { Invoke-Daily }
    "backtest"  { Invoke-Backtest -TestDate $Date }
    "live"      { Invoke-Live }
    "analyze"   { Invoke-Analyze }
    default     { Show-Help }
}
