@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 빌드에 필요한 패키지 확인/설치
python -c "import PyInstaller, numpy, cv2, mss, win32api, PIL" 2>nul
if errorlevel 1 (
    echo [설치] 빌드에 필요한 패키지를 설치합니다...
    python -m pip install -r source\requirements.txt pyinstaller
)

REM 빌드 실행 (인자 그대로 전달: --minor / --major 가능)
python build.py %*

echo.
pause
