@echo off
chcp 65001 >nul
echo ================================================
echo   Qoo10 최저가 매칭 시스템 - TheKple
echo ================================================
echo.

:: 의존성 체크 및 설치
pip show flask >nul 2>&1
if errorlevel 1 (
    echo [설치] 필요한 패키지를 설치합니다...
    pip install flask flask-cors playwright
    playwright install chromium
    echo.
)

echo [시작] 서버를 시작합니다...
echo [안내] 브라우저가 자동으로 열립니다.
echo [안내] 종료하려면 이 창에서 Ctrl+C를 누르세요.
echo.

python server.py
pause
