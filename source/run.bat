@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 필요한 패키지가 없으면 설치
python -c "import numpy, cv2, mss, win32api, PIL" 2>nul
if errorlevel 1 (
    echo [설치] 필요한 패키지를 설치합니다...
    python -m pip install -r requirements.txt
)

python scroll_capture.py
