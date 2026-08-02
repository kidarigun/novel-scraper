@echo off
chcp 65001 >nul
echo ============================================
echo  스텔스 브라우저(chromium) 설치 (최초 1회)
echo ============================================
echo.
python -m patchright install chromium
if %errorlevel% neq 0 (
  echo.
  echo [오류] Python이 설치되어 있지 않거나 실패했습니다.
  echo Python 미설치 시: https://www.python.org 에서 설치 후 다시 실행하세요.
)
echo.
pause
