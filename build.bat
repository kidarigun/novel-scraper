@echo off
chcp 65001 >nul
echo ============================================
echo  단일 실행 파일(.exe) 빌드
echo ============================================
echo.

python -m pip install pyinstaller >nul 2>&1

python -m PyInstaller --noconfirm --clean --onefile --windowed --name "소설스크래퍼" ^
  --collect-all scrapling ^
  --collect-all patchright ^
  --collect-all browserforge ^
  --collect-all apify_fingerprint_datapoints ^
  --collect-all curl_cffi ^
  --collect-all babel ^
  --collect-all tldextract ^
  gui.py

echo.
if exist "dist\소설스크래퍼.exe" (
  echo [완료] dist\소설스크래퍼.exe 생성됨
) else (
  echo [실패] 빌드 오류를 확인하세요.
)
echo.
pause
