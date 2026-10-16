#!/bin/bash
# macOS 빌드 스크립트 — ScrollCapture.app 과 DMG 생성 (macOS 12 Monterey 이상)
#   사용: ./build_mac.sh            (현재 CPU 아키텍처: arm64 또는 x86_64)
#   필요: python3 (python.org 3.12 권장), pip install -r source/requirements-mac.txt pyinstaller
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
PY="${PYTHON:-python3}"
ARCH="$(uname -m)"
VER="$("$PY" -c "import re;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', open('source/version.py').read()).group(1))")"
APP="ScrollCapture"
OUT="$ROOT/build/mac"
DIST="$OUT/dist"
echo "[build] v$VER ($ARCH)"

rm -rf "$OUT"
mkdir -p "$OUT" "$ROOT/release"

"$PY" -m PyInstaller --windowed --noconfirm --clean \
  --name "$APP" \
  --osx-bundle-identifier "com.zeta1113.scrollcapture" \
  --icon "$ROOT/source/icon.ico" \
  --distpath "$DIST" --workpath "$OUT/work" --specpath "$OUT/spec" \
  --paths "$ROOT/source" \
  --add-data "$ROOT/source/version.py:." \
  --add-data "$ROOT/source/assets:assets" \
  --hidden-import updater --hidden-import platform_mac \
  --hidden-import Quartz --hidden-import AppKit --hidden-import Foundation \
  --hidden-import ApplicationServices \
  "$ROOT/source/scroll_capture.py"

BUNDLE="$DIST/$APP.app"
PLIST="$BUNDLE/Contents/Info.plist"
pb() { /usr/libexec/PlistBuddy -c "Set :$1 $2" "$PLIST" 2>/dev/null \
       || /usr/libexec/PlistBuddy -c "Add :$1 $3 $2" "$PLIST"; }
pb CFBundleShortVersionString "$VER" string
pb CFBundleVersion "$VER" string
pb CFBundleDisplayName "$APP" string
pb LSMinimumSystemVersion "12.0" string
pb NSHighResolutionCapable true bool
pb NSScreenCaptureUsageDescription "Captures the screen area you select." string
pb NSAppleEventsUsageDescription "Scrolls the selected window while capturing." string

# 임시(ad-hoc) 서명 — Apple Silicon 실행 필수. 번들 전체 재서명.
codesign --force --deep --sign - "$BUNDLE"
codesign --verify --deep --strict "$BUNDLE" && echo "[build] codesign OK"

# DMG (앱 + 응용 프로그램 폴더 바로가기)
STAGE="$OUT/dmg"
mkdir -p "$STAGE"
cp -R "$BUNDLE" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="$ROOT/release/ScrollCapture_mac_${ARCH}_v${VER}.dmg"
rm -f "$DMG"
# hdiutil 은 CI 에서 간헐적으로 "Resource busy" 로 실패 → 재시도
for i in 1 2 3 4 5; do
  if hdiutil create -volname "ScrollCapture $VER" -srcfolder "$STAGE" \
       -ov -format UDZO "$DMG"; then
    break
  fi
  echo "[build] hdiutil 실패($i) — 재시도"; sleep $((i * 5))
  [ "$i" -eq 5 ] && exit 1
done
echo "[build] 완료: $DMG"
