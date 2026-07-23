@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 관리자 권한 확인 → 없으면 승격 재실행
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 관리자 권한이 필요합니다. UAC 창에서 [예]를 눌러주세요...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

if not exist "cert\ScrollCapture.cer" (
    echo [오류] cert\ScrollCapture.cer 가 없습니다. 먼저 build.bat 로 빌드하세요.
    pause
    exit /b 1
)

echo 인증서를 신뢰할 수 있는 루트/게시자 저장소에 등록합니다...
certutil -addstore -f Root "cert\ScrollCapture.cer"
certutil -addstore -f TrustedPublisher "cert\ScrollCapture.cer"

echo.
echo [완료] 이제 서명된 ScrollCapture 실행 파일이 '신뢰할 수 있는 게시자'로 인식됩니다.
echo        (SmartScreen/보안 경고가 줄어듭니다.)
pause
