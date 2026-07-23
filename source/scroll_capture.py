# -*- coding: utf-8 -*-
"""
스크롤 자동 캡처 (Scroll Auto Capture) - Windows 11

브라우저뿐 아니라 Claude 데스크탑 같은 네이티브 앱의 대화 내용처럼
'스크롤되는 영역'을 지정해 전체를 자동 캡처하고 한 장의 PNG로 저장합니다.

특징
  - 드래그로 '대화 영역'만 선택 → 상단 헤더/하단 입력창 같은 고정 영역 제외
  - 매 스크롤마다 대상 창을 활성화하고, 휠이 안 먹히면 자동으로
    WM_MOUSEWHEEL 메시지 → PageDown 키로 전환하는 적응형 스크롤
  - 겹치는 구간을 자동으로 찾아 중복 없이 이어붙임(OpenCV 템플릿 매칭)
  - 캡처한 파일을 목록(썸네일)으로 확인 / 열기
"""

import os
import io
import sys
import json
import time
import threading
import webbrowser
import ctypes
from datetime import datetime

import numpy as np
import cv2
import mss
import win32api
import win32con
import win32gui
import win32process
import win32clipboard

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk

try:
    from version import __version__ as APP_VERSION, APP_TITLE
except Exception:
    APP_VERSION, APP_TITLE = "dev", "스크롤 자동 캡처"

try:
    from version import GITHUB_REPO
except Exception:
    GITHUB_REPO = ""

try:
    import updater
except Exception:
    updater = None


def app_base_dir():
    """실행 파일(.exe) 또는 스크립트가 있는 폴더 — 캡처 저장 기준 경로."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """번들된 리소스(아이콘 등) 경로 — PyInstaller onefile 대응."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


# ------------------------------------------------------------------
# 설정 저장/불러오기
# ------------------------------------------------------------------
def config_path():
    base = os.environ.get("APPDATA") or app_base_dir()
    d = os.path.join(base, "ScrollCapture")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = app_base_dir()
    return os.path.join(d, "settings.json")


DEFAULT_CFG = {
    "lang": "ko",
    "scroll_top": True,
    "multi_monitor": True,
    "hide_app": True,        # 캡처 중 프로그램 숨기기
    "on_top": False,         # 항상 위에 위치
    "sticky": False,         # 웹페이지 상단 고정 메뉴 처리
    "hide_mouse": True,      # 캡처 시 마우스 숨기기
    "auto_copy": True,       # 캡처 후 클립보드에 자동 복사
    "speed_idx": 1,          # 1x
    "save_dir": "",
    "hotkeys": {},           # {mode: {"mods":int, "vk":int, "text":str}} (비면 기본값)
}

# 전역 단축키 기본값: mode -> (modifiers, virtual-key, 표시문자열)
#   MOD_ALT=0x0001, MOD_CONTROL=0x0002, MOD_SHIFT=0x0004, MOD_WIN=0x0008
CTRL_ALT = 0x0002 | 0x0001
DEFAULT_HOTKEYS = {
    "scroll_display": (CTRL_ALT, 0x31, "Ctrl + Alt + 1"),
    "scroll_window":  (CTRL_ALT, 0x32, "Ctrl + Alt + 2"),
    "scroll_region":  (CTRL_ALT, 0x33, "Ctrl + Alt + 3"),
    "shot_all":       (CTRL_ALT, 0x34, "Ctrl + Alt + 4"),
    "shot_display":   (CTRL_ALT, 0x35, "Ctrl + Alt + 5"),
    "shot_window":    (CTRL_ALT, 0x36, "Ctrl + Alt + 6"),
    "shot_region":    (CTRL_ALT, 0x37, "Ctrl + Alt + 7"),
}
HOTKEY_MODES = ["scroll_display", "scroll_window", "scroll_region",
                "shot_all", "shot_display", "shot_window", "shot_region"]


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(config_path(), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# 스크롤 속도 슬라이더 값
SPEED_VALUES = [0.5, 1.0, 1.2, 1.5, 2.0, 3.0]
SPEED_DEFAULT_IDX = 1  # 1x


# ------------------------------------------------------------------
# 모니터 정보
# ------------------------------------------------------------------
def primary_screen():
    return (0, 0, win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1))


def virtual_screen():
    return (win32api.GetSystemMetrics(76), win32api.GetSystemMetrics(77),
            win32api.GetSystemMetrics(78), win32api.GetSystemMetrics(79))


def get_monitors():
    """각 모니터의 (x, y, w, h) 목록."""
    mons = []
    try:
        for hMon, _hdc, _rect in win32api.EnumDisplayMonitors(None, None):
            info = win32api.GetMonitorInfo(hMon)
            l, t, r, b = info["Monitor"]
            mons.append((l, t, r - l, b - t))
    except Exception:
        pass
    if not mons:
        mons = [primary_screen()]
    return mons


def get_window_rect(hwnd):
    """창의 '보이는' 사각형 (x, y, w, h). DWM 확장 프레임 우선(불가 시 GetWindowRect)."""
    try:
        from ctypes import wintypes
        rect = wintypes.RECT()
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        res = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect), ctypes.sizeof(rect))
        if res == 0 and rect.right > rect.left and rect.bottom > rect.top:
            return (rect.left, rect.top,
                    rect.right - rect.left, rect.bottom - rect.top)
    except Exception:
        pass
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    return (l, t, r - l, b - t)


# ------------------------------------------------------------------
# 캡처 버튼 아이콘 (심플한 벡터 아이콘을 PIL 로 그려 생성)
# ------------------------------------------------------------------
def make_capture_icons(size=70, margin=10):
    """7개 캡처 버튼용 아이콘을 {mode: PhotoImage} 로 생성.
    캡처 도구풍(파란 화면 채움 + 진한 테두리, 플랫). 글리프는 사방 margin(px)만
    남기고 버튼에 꽉 차게 그린다. 스크롤 계열은 우하단에 파란 원 + 흰 아래화살표 배지."""
    from PIL import ImageDraw
    ss = 4
    S = size * ss
    STROKE = (44, 62, 80, 255)     # 진한 테두리(#2c3e50)
    SCREEN = (74, 158, 255, 255)   # 파란 화면(#4a9eff)
    WHITE = (255, 255, 255, 255)
    lw = max(3, S // 28)
    m = int(margin * ss)
    X0, Y0, X1, Y1 = m, m, S - m, S - m
    GW = X1 - X0

    def monitor(d):
        sb = Y1 - GW * 0.14                  # 스탠드 공간
        d.rounded_rectangle([X0, Y0, X1, sb], radius=GW * 0.07,
                            fill=SCREEN, outline=STROKE, width=lw)
        cx = (X0 + X1) / 2
        d.line([cx, sb, cx, Y1], fill=STROKE, width=lw)
        d.line([cx - GW * 0.16, Y1, cx + GW * 0.16, Y1], fill=STROKE, width=lw)

    def window(d):
        r = GW * 0.06
        ty = Y0 + (Y1 - Y0) * 0.26
        # 파란 타이틀바(상단만 라운드)
        d.rounded_rectangle([X0, Y0, X1, ty], radius=r, fill=SCREEN)
        d.rectangle([X0, ty - r, X1, ty], fill=SCREEN)
        d.line([X0, ty, X1, ty], fill=STROKE, width=lw)
        d.rounded_rectangle([X0, Y0, X1, Y1], radius=r, outline=STROKE, width=lw)
        rr = lw * 0.8
        cy = (Y0 + ty) / 2
        for i in range(3):
            cx = X0 + GW * 0.15 + i * (rr * 3.2)
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=WHITE)

    def region(d):
        dash, gap = GW * 0.10, GW * 0.05
        def hline(y):
            x = X0
            while x < X1:
                d.line([x, y, min(x + dash, X1), y], fill=STROKE, width=lw)
                x += dash + gap
        def vline(x):
            y = Y0
            while y < Y1:
                d.line([x, y, x, min(y + dash, Y1)], fill=STROKE, width=lw)
                y += dash + gap
        hline(Y0); hline(Y1); vline(X0); vline(X1)
        cx, cy = (X0 + X1) / 2, (Y0 + Y1) / 2
        cl = GW * 0.15
        d.line([cx - cl, cy, cx + cl, cy], fill=SCREEN, width=lw + ss)
        d.line([cx, cy - cl, cx, cy + cl], fill=SCREEN, width=lw + ss)

    def two_monitors(d):
        gap = GW * 0.10
        w = (GW - gap) / 2
        y0 = Y0 + GW * 0.10
        y1 = Y1 - GW * 0.22
        for i in range(2):
            xa = X0 + i * (w + gap)
            d.rounded_rectangle([xa, y0, xa + w, y1], radius=w * 0.10,
                                fill=SCREEN, outline=STROKE, width=lw)
        cx = (X0 + X1) / 2
        d.line([cx, y1, cx, Y1], fill=STROKE, width=lw)
        d.line([cx - GW * 0.16, Y1, cx + GW * 0.16, Y1], fill=STROKE, width=lw)

    def badge(d):
        rad = GW * 0.23
        cx, cy = X1 - rad, Y1 - rad
        d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad],
                  fill=SCREEN, outline=WHITE, width=max(2, int(lw * 0.7)))
        aw, ah = rad * 0.42, rad * 0.5
        w2 = max(3, int(lw * 0.95))
        d.line([cx, cy - ah, cx, cy + ah], fill=WHITE, width=w2)
        d.line([cx - aw, cy + ah - aw, cx, cy + ah], fill=WHITE, width=w2)
        d.line([cx + aw, cy + ah - aw, cx, cy + ah], fill=WHITE, width=w2)

    def build(kind, scroll):
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        {"monitor": monitor, "window": window,
         "region": region, "two": two_monitors}[kind](d)
        if scroll:
            badge(d)
        return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))

    return {
        "scroll_display": build("monitor", True),
        "scroll_window": build("window", True),
        "scroll_region": build("region", True),
        "shot_all": build("two", False),
        "shot_display": build("monitor", False),
        "shot_window": build("window", False),
        "shot_region": build("region", False),
    }


# ------------------------------------------------------------------
# 다국어 (i18n)
# ------------------------------------------------------------------
LANGS = [("en", "English"), ("ko", "한국어"), ("ja", "日本語"), ("zh", "中文")]

TR = {
    "ko": {
        "btn_capture_region": "🎯  캡처 시작 (영역 선택)",
        "btn_capture_full": "🖥  캡처 시작 (전체 영역)",
        "tip_scroll_display_t": "스크롤 캡처 (디스플레이)",
        "tip_scroll_display_h": "선택한 모니터를 자동 스크롤하며 전체를 캡처",
        "tip_scroll_window_t": "스크롤 캡처 (윈도우)",
        "tip_scroll_window_h": "선택한 창을 자동 스크롤하며 전체를 캡처",
        "tip_scroll_region_t": "스크롤 캡처 (영역 지정)",
        "tip_scroll_region_h": "드래그한 영역을 자동 스크롤하며 전체를 캡처",
        "tip_shot_all_t": "전체 디스플레이 캡처",
        "tip_shot_all_h": "모든 모니터의 현재 화면을 한 장으로 캡처",
        "tip_shot_display_t": "디스플레이 캡처",
        "tip_shot_display_h": "선택한 모니터의 현재 화면을 캡처",
        "tip_shot_window_t": "윈도우 캡처",
        "tip_shot_window_h": "선택한 창의 현재 화면을 캡처",
        "tip_shot_region_t": "영역 지정 캡처",
        "tip_shot_region_h": "드래그한 영역의 현재 화면을 캡처",
        "guide_window": "캡처할 창 위에 마우스를 올리고 클릭해 주세요.  (ESC = 취소)",
        "btn_hotkeys": "단축키 매핑",
        "hk_title": "단축키 매핑",
        "hk_hint": "입력창을 클릭한 뒤 원하는 키 조합을 누르세요.  (Esc = 해제)",
        "hk_col_name": "캡처",
        "hk_col_key": "단축키",
        "hk_save": "저장",
        "hk_close": "닫기",
        "hk_none": "(없음)",
        "hk_dup_fmt": "단축키가 중복됩니다: {keys}",
        "hk_saved": "단축키를 저장했습니다.",
        "opt_scroll_top": "최상위 위치로 자동 스크롤 후 캡처 시작",
        "opt_multi_monitor": "멀티 모니터 지원",
        "opt_hide_app": "캡처 프로그램 숨기기",
        "opt_sticky": "웹페이지 Sticky(고정) 메뉴 처리",
        "opt_hide_mouse": "캡처 시 마우스 숨기기",
        "opt_auto_copy": "캡처 후 클립보드에 자동 복사",
        "copied_status": "클립보드 복사됨",
        "lf_options": "옵션",
        "opt_on_top": "항상 위에 위치",
        "speed_hint": "빠를수록 정확도가 떨어질 수 있어요",
        "lbl_speed": "스크롤 속도",
        "btn_reset": "초기화",
        "lbl_language": "언어",
        "lf_save_folder": "캡처 저장 폴더",
        "btn_change": "변경",
        "btn_open": "열기",
        "lf_captured": "캡처한 파일",
        "btn_refresh": "새로고침",
        "btn_open_item": "열기",
        "btn_copy": "복사",
        "copied_mark": "✓",
        "status_ready": "준비됨 — [캡처 시작]을 누르세요",
        "status_preparing": "캡처 준비 중...",
        "status_moving_top": "상단으로 이동 중...",
        "status_detecting": "고정 영역 감지 중...",
        "status_capturing": "캡처 중...",
        "status_capturing_fmt": "캡처 중... (높이 {h}px)",
        "status_done_fmt": "완료: {name}",
        "status_cancelled": "취소되었습니다",
        "status_error": "오류 / 안내",
        "guide_region": "캡처할 '스크롤 영역'을 드래그하세요   (ESC = 취소)",
        "guide_monitor": "캡처할 모니터로 마우스를 이동 후 클릭해 주세요.  (ESC = 취소)",
        "please_wait": "이미지 저장 중입니다. 잠시만 기다려 주세요...",
        "folder_missing": "(폴더 없음)",
        "no_files": "아직 캡처한 파일이 없습니다.",
        "info_title": "안내",
        "done_saved_fmt": "저장되었습니다:\n{path}",
        "err_small_region": "선택한 영역이 너무 작습니다.",
        "err_no_scroll": ("스크롤이 되지 않아 한 화면만 캡처되었습니다.\n"
                          "· 대상 대화 영역(스크롤되는 부분)을 정확히 지정했는지\n"
                          "· 이 프로그램을 관리자 권한으로 실행했는지 확인하세요."),
        "copy_done": "이미지를 클립보드에 복사했습니다.",
        "copy_fail": "클립보드 복사에 실패했습니다.",
        "ok_mark": "OK",
        "btn_check_update": "업데이트 확인",
        "update_latest": "최신 버전입니다.",
        "update_avail_fmt": "새 버전 {ver} 이(가) 있습니다.",
        "update_fail": "업데이트 확인에 실패했습니다.",
        "btn_download": "다운로드",
    },
    "en": {
        "btn_capture_region": "🎯  Capture (Select Area)",
        "btn_capture_full": "🖥  Capture (Full Area)",
        "tip_scroll_display_t": "Scroll Capture (Display)",
        "tip_scroll_display_h": "Auto-scroll the selected monitor and capture it all",
        "tip_scroll_window_t": "Scroll Capture (Window)",
        "tip_scroll_window_h": "Auto-scroll the selected window and capture it all",
        "tip_scroll_region_t": "Scroll Capture (Area)",
        "tip_scroll_region_h": "Auto-scroll the dragged area and capture it all",
        "tip_shot_all_t": "Capture All Displays",
        "tip_shot_all_h": "Capture the current view of all monitors in one shot",
        "tip_shot_display_t": "Capture Display",
        "tip_shot_display_h": "Capture the current view of the selected monitor",
        "tip_shot_window_t": "Capture Window",
        "tip_shot_window_h": "Capture the current view of the selected window",
        "tip_shot_region_t": "Capture Area",
        "tip_shot_region_h": "Capture the current view of the dragged area",
        "guide_window": "Hover over the window to capture, then click.  (ESC = cancel)",
        "btn_hotkeys": "Hotkeys",
        "hk_title": "Hotkey Mapping",
        "hk_hint": "Click a field and press the key combo.  (Esc = clear)",
        "hk_col_name": "Capture",
        "hk_col_key": "Hotkey",
        "hk_save": "Save",
        "hk_close": "Close",
        "hk_none": "(none)",
        "hk_dup_fmt": "Duplicate hotkey: {keys}",
        "hk_saved": "Hotkeys saved.",
        "opt_scroll_top": "Auto-scroll to top before capturing",
        "opt_multi_monitor": "Multi-monitor support",
        "opt_hide_app": "Hide capture window",
        "opt_sticky": "Handle web sticky header",
        "opt_hide_mouse": "Hide cursor while capturing",
        "opt_auto_copy": "Copy to clipboard after capture",
        "copied_status": "Copied to clipboard",
        "lf_options": "Options",
        "opt_on_top": "Always on top",
        "speed_hint": "Faster may reduce accuracy",
        "lbl_speed": "Scroll speed",
        "btn_reset": "Reset",
        "lbl_language": "Language",
        "lf_save_folder": "Save folder",
        "btn_change": "Change",
        "btn_open": "Open",
        "lf_captured": "Captured files",
        "btn_refresh": "Refresh",
        "btn_open_item": "Open",
        "btn_copy": "Copy",
        "copied_mark": "✓",
        "status_ready": "Ready — press [Capture]",
        "status_preparing": "Preparing...",
        "status_moving_top": "Moving to top...",
        "status_detecting": "Detecting fixed areas...",
        "status_capturing": "Capturing...",
        "status_capturing_fmt": "Capturing... (height {h}px)",
        "status_done_fmt": "Done: {name}",
        "status_cancelled": "Cancelled",
        "status_error": "Error / Notice",
        "guide_region": "Drag the scrolling area to capture   (ESC = cancel)",
        "guide_monitor": "Move the mouse to the monitor to capture, then click.  (ESC = cancel)",
        "please_wait": "Saving image. Please wait...",
        "folder_missing": "(folder not found)",
        "no_files": "No captured files yet.",
        "info_title": "Notice",
        "done_saved_fmt": "Saved:\n{path}",
        "err_small_region": "The selected area is too small.",
        "err_no_scroll": ("Only one screen was captured because scrolling did not work.\n"
                          "· Check that you selected the actual scrolling area\n"
                          "· Try running this program as administrator."),
        "copy_done": "Image copied to clipboard.",
        "copy_fail": "Failed to copy to clipboard.",
        "ok_mark": "OK",
        "btn_check_update": "Check for updates",
        "update_latest": "You have the latest version.",
        "update_avail_fmt": "New version {ver} is available.",
        "update_fail": "Failed to check for updates.",
        "btn_download": "Download",
    },
    "ja": {
        "btn_capture_region": "🎯  キャプチャ開始 (範囲選択)",
        "btn_capture_full": "🖥  キャプチャ開始 (全体)",
        "tip_scroll_display_t": "スクロールキャプチャ (ディスプレイ)",
        "tip_scroll_display_h": "選択したモニターを自動スクロールして全体をキャプチャ",
        "tip_scroll_window_t": "スクロールキャプチャ (ウィンドウ)",
        "tip_scroll_window_h": "選択したウィンドウを自動スクロールして全体をキャプチャ",
        "tip_scroll_region_t": "スクロールキャプチャ (範囲指定)",
        "tip_scroll_region_h": "ドラッグした範囲を自動スクロールして全体をキャプチャ",
        "tip_shot_all_t": "全ディスプレイキャプチャ",
        "tip_shot_all_h": "すべてのモニターの現在の画面を1枚でキャプチャ",
        "tip_shot_display_t": "ディスプレイキャプチャ",
        "tip_shot_display_h": "選択したモニターの現在の画面をキャプチャ",
        "tip_shot_window_t": "ウィンドウキャプチャ",
        "tip_shot_window_h": "選択したウィンドウの現在の画面をキャプチャ",
        "tip_shot_region_t": "範囲キャプチャ",
        "tip_shot_region_h": "ドラッグした範囲の現在の画面をキャプチャ",
        "guide_window": "キャプチャするウィンドウにマウスを合わせてクリックしてください。 (ESC = キャンセル)",
        "btn_hotkeys": "ショートカット",
        "hk_title": "ショートカット設定",
        "hk_hint": "入力欄をクリックしてキーの組み合わせを押してください。 (Esc = 解除)",
        "hk_col_name": "キャプチャ",
        "hk_col_key": "ショートカット",
        "hk_save": "保存",
        "hk_close": "閉じる",
        "hk_none": "(なし)",
        "hk_dup_fmt": "ショートカットが重複しています: {keys}",
        "hk_saved": "ショートカットを保存しました。",
        "opt_scroll_top": "キャプチャ前に一番上へ自動スクロール",
        "opt_multi_monitor": "マルチモニター対応",
        "opt_hide_app": "キャプチャ画面を隠す",
        "opt_sticky": "Web上部固定メニューの処理",
        "opt_hide_mouse": "キャプチャ中はカーソルを隠す",
        "opt_auto_copy": "キャプチャ後クリップボードへ自動コピー",
        "copied_status": "クリップボードにコピー済み",
        "lf_options": "オプション",
        "opt_on_top": "常に手前に表示",
        "speed_hint": "速いほど精度が下がる場合があります",
        "lbl_speed": "スクロール速度",
        "btn_reset": "リセット",
        "lbl_language": "言語",
        "lf_save_folder": "保存フォルダ",
        "btn_change": "変更",
        "btn_open": "開く",
        "lf_captured": "キャプチャしたファイル",
        "btn_refresh": "更新",
        "btn_open_item": "開く",
        "btn_copy": "コピー",
        "copied_mark": "✓",
        "status_ready": "準備完了 — [キャプチャ開始] を押してください",
        "status_preparing": "準備中...",
        "status_moving_top": "上へ移動中...",
        "status_detecting": "固定領域を検出中...",
        "status_capturing": "キャプチャ中...",
        "status_capturing_fmt": "キャプチャ中... (高さ {h}px)",
        "status_done_fmt": "完了: {name}",
        "status_cancelled": "キャンセルされました",
        "status_error": "エラー / お知らせ",
        "guide_region": "キャプチャする'スクロール範囲'をドラッグしてください   (ESC = キャンセル)",
        "guide_monitor": "キャプチャするモニターへマウスを移動してクリックしてください。  (ESC = キャンセル)",
        "please_wait": "画像を保存中です。しばらくお待ちください...",
        "folder_missing": "(フォルダなし)",
        "no_files": "まだキャプチャしたファイルがありません。",
        "info_title": "お知らせ",
        "done_saved_fmt": "保存しました:\n{path}",
        "err_small_region": "選択した範囲が小さすぎます。",
        "err_no_scroll": ("スクロールできず1画面のみキャプチャされました。\n"
                          "· スクロールする範囲を正しく指定したか\n"
                          "· 管理者権限で実行しているか確認してください。"),
        "copy_done": "画像をクリップボードにコピーしました。",
        "copy_fail": "クリップボードへのコピーに失敗しました。",
        "ok_mark": "OK",
        "btn_check_update": "更新を確認",
        "update_latest": "最新バージョンです。",
        "update_avail_fmt": "新しいバージョン {ver} があります。",
        "update_fail": "更新の確認に失敗しました。",
        "btn_download": "ダウンロード",
    },
    "zh": {
        "btn_capture_region": "🎯  开始截图 (选择区域)",
        "btn_capture_full": "🖥  开始截图 (整个区域)",
        "tip_scroll_display_t": "滚动截图 (显示器)",
        "tip_scroll_display_h": "自动滚动所选显示器并截取全部内容",
        "tip_scroll_window_t": "滚动截图 (窗口)",
        "tip_scroll_window_h": "自动滚动所选窗口并截取全部内容",
        "tip_scroll_region_t": "滚动截图 (区域)",
        "tip_scroll_region_h": "自动滚动拖选区域并截取全部内容",
        "tip_shot_all_t": "全部显示器截图",
        "tip_shot_all_h": "将所有显示器的当前画面截为一张",
        "tip_shot_display_t": "显示器截图",
        "tip_shot_display_h": "截取所选显示器的当前画面",
        "tip_shot_window_t": "窗口截图",
        "tip_shot_window_h": "截取所选窗口的当前画面",
        "tip_shot_region_t": "区域截图",
        "tip_shot_region_h": "截取拖选区域的当前画面",
        "guide_window": "请将鼠标移到要截取的窗口上并点击。 (ESC = 取消)",
        "btn_hotkeys": "快捷键",
        "hk_title": "快捷键映射",
        "hk_hint": "点击输入框后按下想要的组合键。 (Esc = 清除)",
        "hk_col_name": "截图",
        "hk_col_key": "快捷键",
        "hk_save": "保存",
        "hk_close": "关闭",
        "hk_none": "(无)",
        "hk_dup_fmt": "快捷键重复: {keys}",
        "hk_saved": "已保存快捷键。",
        "opt_scroll_top": "截图前自动滚动到顶部",
        "opt_multi_monitor": "多显示器支持",
        "opt_hide_app": "隐藏截图程序",
        "opt_sticky": "处理网页顶部固定菜单",
        "opt_hide_mouse": "截图时隐藏鼠标",
        "opt_auto_copy": "截图后自动复制到剪贴板",
        "copied_status": "已复制到剪贴板",
        "lf_options": "选项",
        "opt_on_top": "始终置顶",
        "speed_hint": "速度越快，精度可能越低",
        "lbl_speed": "滚动速度",
        "btn_reset": "重置",
        "lbl_language": "语言",
        "lf_save_folder": "保存文件夹",
        "btn_change": "更改",
        "btn_open": "打开",
        "lf_captured": "已截图的文件",
        "btn_refresh": "刷新",
        "btn_open_item": "打开",
        "btn_copy": "复制",
        "copied_mark": "✓",
        "status_ready": "就绪 — 请点击 [开始截图]",
        "status_preparing": "准备中...",
        "status_moving_top": "正在移到顶部...",
        "status_detecting": "正在检测固定区域...",
        "status_capturing": "正在截图...",
        "status_capturing_fmt": "正在截图... (高度 {h}px)",
        "status_done_fmt": "完成: {name}",
        "status_cancelled": "已取消",
        "status_error": "错误 / 提示",
        "guide_region": "拖动要截取的'滚动区域'   (ESC = 取消)",
        "guide_monitor": "请将鼠标移动到要截取的显示器并点击。  (ESC = 取消)",
        "please_wait": "正在保存图片，请稍候...",
        "folder_missing": "(文件夹不存在)",
        "no_files": "还没有截图文件。",
        "info_title": "提示",
        "done_saved_fmt": "已保存:\n{path}",
        "err_small_region": "所选区域太小。",
        "err_no_scroll": ("由于无法滚动，只截取了一屏。\n"
                          "· 请确认已正确选择可滚动的区域\n"
                          "· 请尝试以管理员身份运行本程序。"),
        "copy_done": "图片已复制到剪贴板。",
        "copy_fail": "复制到剪贴板失败。",
        "ok_mark": "OK",
        "btn_check_update": "检查更新",
        "update_latest": "已是最新版本。",
        "update_avail_fmt": "有新版本 {ver}。",
        "update_fail": "检查更新失败。",
        "btn_download": "下载",
    },
}


def tr(lang, key, **kw):
    d = TR.get(lang, TR["ko"])
    s = d.get(key, TR["ko"].get(key, key))
    return s.format(**kw) if kw else s

# ------------------------------------------------------------------
# DPI 인식 (고해상도 모니터 좌표/크기 정확도)
# ------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

WHEEL_DELTA = 120
SCROLL_NOTCHES = 2       # 캡처(하강) 시 한 스텝 노치 — 작을수록 겹침이 안정적
SCROLL_NOTCHES_TOP = 5   # 상단으로 빠르게 이동할 때 노치
RENDER_WAIT = 0.35       # 스크롤 후 렌더링 대기(초)
GA_ROOT = 2
METHODS = ["wheel", "send", "key"]  # 스크롤 방식 우선순위


# ==================================================================
# 창 활성화 / 스크롤 저수준 함수
# ==================================================================
def ensure_foreground(hwnd):
    """대상 창을 확실하게 앞으로 가져온다(AttachThreadInput 우회 포함)."""
    if not hwnd:
        return
    try:
        fg = win32gui.GetForegroundWindow()
        if fg == hwnd:
            return
        cur = win32api.GetCurrentThreadId()
        fg_t = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        tg_t = win32process.GetWindowThreadProcessId(hwnd)[0]
        attached = []
        for t in {fg_t, tg_t}:
            if t and t != cur:
                try:
                    win32process.AttachThreadInput(cur, t, True)
                    attached.append(t)
                except Exception:
                    pass
        try:
            # 최소화된 경우에만 복원(최대화 창을 축소시키지 않도록 SW_RESTORE 무분별 호출 금지)
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        for t in attached:
            try:
                win32process.AttachThreadInput(cur, t, False)
            except Exception:
                pass
    except Exception:
        pass


def _wheel(hwnd, cx, cy, notches):
    """실제 마우스 휠 입력(노치 단위로 나눠 전송)."""
    ensure_foreground(hwnd)
    win32api.SetCursorPos((cx, cy))
    time.sleep(0.03)
    step = WHEEL_DELTA if notches > 0 else -WHEEL_DELTA
    for _ in range(abs(notches)):
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, step, 0)
        time.sleep(0.02)


def _sendmsg_wheel(cx, cy, notches):
    """커서 위치의 창에 WM_MOUSEWHEEL 메시지를 직접 전달."""
    try:
        child = win32gui.WindowFromPoint((cx, cy))
        delta = (WHEEL_DELTA if notches > 0 else -WHEEL_DELTA) * abs(notches)
        wparam = (delta & 0xFFFF) << 16
        lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
        win32gui.SendMessage(child, win32con.WM_MOUSEWHEEL, wparam, lparam)
    except Exception:
        pass


def _key(hwnd, cx, cy, down):
    """PageDown / PageUp 키 입력. (커서를 옮기지 않음 → 마우스 깜빡임 방지)"""
    ensure_foreground(hwnd)
    key = win32con.VK_NEXT if down else win32con.VK_PRIOR
    win32api.keybd_event(key, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)


def apply_scroll(hwnd, cx, cy, method, down, notches, park=None):
    if method == "wheel":
        _wheel(hwnd, cx, cy, -notches if down else notches)
    elif method == "send":
        _sendmsg_wheel(cx, cy, -notches if down else notches)
    else:
        _key(hwnd, cx, cy, down)
    # 캡처 직전 커서를 콘텐츠 밖으로 이동 → 마우스오버(hover) 배경색 변화 방지
    if park is not None:
        try:
            win32api.SetCursorPos(park)
        except Exception:
            pass


# ==================================================================
# 캡처 유틸
# ==================================================================
def grab(sct, region):
    x, y, w, h = region
    shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    img = np.array(shot)  # BGRA
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def find_delta(prev, curr, max_frac=0.6, side_margin=24):
    """
    아래로 몇 px 스크롤됐는지(delta)와 매칭 신뢰도(score)를 반환.
    - 키 큰 템플릿(문맥이 풍부)으로 반복되는 UI 요소(구분선/버튼)에 오매칭되지 않도록 함
    - 탐색 범위를 아래쪽으로 제한(delta ∈ [0, H*max_frac])해 먼 곳의 동일 패턴 매칭 방지
    """
    H, W = prev.shape[:2]
    if curr.shape[:2] != (H, W):
        curr = curr[:H, :W]
    pg = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
    cg = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY)

    mx = min(side_margin, max(1, W // 4))
    ty = max(4, H // 20)
    th = max(80, H // 3)           # 키 큰 템플릿
    if ty + th >= H - 2:
        th = H - 2 - ty
    templ = cg[ty:ty + th, mx:W - mx]

    maxd = max(4, int(H * max_frac))
    y1 = min(H, ty + maxd + th)
    field = pg[ty:y1, mx:W - mx]   # ty에서 시작 → field 내 y좌표가 곧 delta
    if field.shape[0] < templ.shape[0] + 1 or templ.shape[1] > field.shape[1]:
        return 0, 0.0

    res = cv2.matchTemplate(field, templ, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    return maxloc[1], float(maxv)


def images_equal(a, b, thresh=1.2):
    if a.shape != b.shape:
        return False
    return float(np.mean(cv2.absdiff(a, b))) < thresh


# ==================================================================
# 캡처 엔진
# ==================================================================
def scroll_to_top(sct, region, hwnd, cx, cy, notches, wait, park=None):
    """대상을 맨 위로 스크롤. 마지막으로 성공한 방식 인덱스를 반환."""
    prev = grab(sct, region)
    pref = 0
    stagnant = 0
    for _ in range(400):
        moved = False
        for m in METHODS[pref:] + METHODS[:pref]:
            apply_scroll(hwnd, cx, cy, m, down=False, notches=notches, park=park)
            time.sleep(wait)
            cur = grab(sct, region)
            if not images_equal(prev, cur):
                moved = True
                pref = METHODS.index(m)
                prev = cur
                break
        if not moved:
            stagnant += 1
            if stagnant >= 2:
                break
        else:
            stagnant = 0
    return pref


def _content_span(changed, win=32, frac=0.4):
    """
    changed(불리언 배열)에서 '가장 긴 연속 고밀도 구간'(=스크롤 콘텐츠)의 (상단, 하단) 인덱스.
    - 밀도를 win 창으로 평활화 → 콘텐츠 사이 빈 여백에 강함
    - 고정영역의 짧은 애니메이션/배지는 '짧은 구간'이라 가장 긴 구간(콘텐츠)에 밀려 무시됨
    구간이 없으면 None.
    """
    n = len(changed)
    if n == 0:
        return None
    c = np.asarray(changed, dtype=np.float64)
    win = max(4, min(win, n))
    dens = np.convolve(c, np.ones(win) / win, mode="same")
    dense = dens >= frac
    best = (0, -1)
    i = 0
    while i < n:
        if dense[i]:
            j = i
            while j < n and dense[j]:
                j += 1
            if (j - 1 - i) > (best[1] - best[0]):
                best = (i, j - 1)
            i = j
        else:
            i += 1
    return best if best[1] >= best[0] else None


def measure_fixed_regions(sct, region, hwnd, cx, cy, notches, wait, park=None):
    """
    스크롤해도 변하지 않는 고정 영역을 감지:
      - 상단(헤더)/하단(고정 바): 가로 밴드 높이 (top, bot)
      - 좌/우 고정 메뉴 + 스크롤바: 세로 밴드 폭 (left, right)  ← 변경된 '행 비율'로 판단해
        썸(thumb)만 움직이는 스크롤바도 포착
    감지 후 원래 위치로 복원한다. 좌/우 밴드의 배경색도 함께 반환.
    """
    frames = [grab(sct, region)]
    downs = 0
    for _ in range(3):
        got = None
        for m in METHODS:
            apply_scroll(hwnd, cx, cy, m, down=True, notches=notches, park=park)
            time.sleep(wait)
            f = grab(sct, region)
            if not images_equal(frames[-1], f):
                got = f
                downs += 1
                break
        frames.append(got if got is not None else grab(sct, region))

    # 원래 위치로 복원
    for _ in range(downs):
        before = grab(sct, region)
        for m in METHODS:
            apply_scroll(hwnd, cx, cy, m, down=False, notches=notches, park=park)
            time.sleep(wait * 0.7)
            if not images_equal(before, grab(sct, region)):
                break

    H, W = frames[0].shape[:2]
    row_thr = 2.5
    row_changed = np.zeros(H, bool)   # 모든 프레임쌍의 '바뀐 행' 합집합(union)
    # 스크롤된 프레임끼리(맨 위 프레임 제외)의 '바뀐 행' — sticky(스크롤 시 나타나는 고정메뉴) 감지용
    row_changed_scrolled = np.zeros(H, bool)
    scrolled = frames[1:]
    for a, b in zip(scrolled, scrolled[1:]):
        if images_equal(a, b):
            continue
        rd = np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16)), axis=(1, 2))
        row_changed_scrolled |= (rd > row_thr)
    lefts, rights = [], []
    for a, b in zip(frames, frames[1:]):
        if images_equal(a, b):
            continue
        ai = a.astype(np.int16)
        bi = b.astype(np.int16)
        # 가로 밴드(행) — 합집합에 누적(과소 감지로 인한 상단 누출/반복 방지)
        rd = np.mean(np.abs(ai - bi), axis=(1, 2))
        row_changed |= (rd > row_thr)
        # 세로 밴드(열)
        #  - 고정 메뉴: 열 평균차가 거의 0 (완전 정지)
        #  - 스크롤바: 썸만 움직여 평균차는 크지만 '바뀐 행 비율'은 낮음 → 우측 끝 좁게 확장
        cmean = np.mean(np.abs(ai - bi), axis=(0, 2))    # (W,) 열 평균차
        cfrac = (np.abs(ai - bi).max(axis=2) > 22).mean(axis=0)  # (W,) 바뀐 행 비율
        col_thr = 2.5
        l = 0
        while l < W and cmean[l] < col_thr:
            l += 1
        rstat = 0
        k = W - 1
        while k >= 0 and cmean[k] < col_thr:
            rstat += 1
            k -= 1
        # 우측 스크롤바(썸 이동) 좁게 확장(최대 24px)
        sb = 0
        k2 = W - 1 - rstat
        while k2 >= 0 and sb < 24 and cfrac[k2] < 0.45:
            sb += 1
            k2 -= 1
        lefts.append(l)
        rights.append(rstat + sb)

    def pick(vals, cap, minsz):
        v = min(vals) if vals else 0
        return 0 if v < minsz else min(v, cap)

    # 상/하단: '가장 긴 고밀도 구간'(=스크롤 콘텐츠)의 위/아래를 고정영역 경계로 확정
    span = _content_span(row_changed, win=32, frac=0.4)
    if span is not None:
        top = span[0]
        bot = H - 1 - span[1]
    else:
        top = bot = 0
    # 경계를 콘텐츠 쪽으로 살짝(6px) 밀어 고정영역 누출(반복)을 확실히 차단
    top = 0 if top < 16 else min(top + 6, H // 2)
    bot = 0 if bot < 16 else min(bot + 6, H // 2)
    left = pick(lefts, int(W * 0.45), 6)
    right = pick(rights, int(W * 0.45), 6)

    # sticky(스크롤 시 나타나는 상단 고정메뉴): 스크롤된 프레임끼리의 고정 상단 - 일반 고정 상단
    sspan = _content_span(row_changed_scrolled, win=32, frac=0.4)
    scrolled_top = 0
    if sspan is not None and sspan[0] >= 16:
        scrolled_top = min(sspan[0] + 6, H // 2)
    sticky = max(0, scrolled_top - top)
    if sticky < 16 or sticky > H // 2:
        sticky = 0

    f0 = frames[0]
    left_bg = (np.median(f0[:, :left].reshape(-1, 3), axis=0).astype(np.uint8)
               if left else None)
    right_bg = (np.median(f0[:, W - right:].reshape(-1, 3), axis=0).astype(np.uint8)
                if right else None)

    return {"top": top, "bot": bot, "left": left, "right": right,
            "left_bg": left_bg, "right_bg": right_bg, "sticky": sticky}


def _scroll_down_n(sct, region, hwnd, cx, cy, notches, wait, park, n):
    """n번 아래로 스크롤(작동하는 방식 자동 선택)."""
    for _ in range(n):
        before = grab(sct, region)
        for m in METHODS:
            apply_scroll(hwnd, cx, cy, m, down=True, notches=notches, park=park)
            time.sleep(wait)
            if not images_equal(before, grab(sct, region)):
                break


def detect_fixed_deep(sct, region, hwnd, cx, cy, notches, wait, park):
    """
    '이전 캡처와 새 캡처에서 같은 위치에 같은 내용 = 고정 영역'을 실제로 구현.
    한참 깊이 스크롤한 뒤(=sticky 활성화) 그 위치의 연속 프레임을 비교해,
    스크롤해도 안 바뀌는 고정 밴드(상단 top / 좌 left / 우 right)를 감지한다.
    (초기 감지는 얕아 GitHub처럼 한참 내려가야 나타나는 sticky를 놓치므로 별도 감지)
    반환: (top, left, right) 픽셀.
    """
    # 감지는 '작은 스텝(휠)'으로 깊이를 제어(PageDown 큰 스텝이 바닥을 지나치는 것 방지)
    saved_methods = list(METHODS)
    METHODS[:] = ["wheel", "send", "key"]
    try:
        # sticky 활성화용으로 적당히 내려간 뒤, '실제로 움직인' 프레임만 수집
        _scroll_down_n(sct, region, hwnd, cx, cy, notches, wait, park, 5)
        frames = [grab(sct, region)]
        for _ in range(6):
            _scroll_down_n(sct, region, hwnd, cx, cy, notches, wait, park, 2)
            f = grab(sct, region)
            if images_equal(frames[-1], f):
                break  # 바닥 도달 → 더 수집 불가
            frames.append(f)
    finally:
        METHODS[:] = saved_methods
    if len(frames) < 2:
        return 0, 0, 0
    H, W = frames[0].shape[:2]
    rchg = np.zeros(H, bool)
    lefts, rights = [], []
    for a, b in zip(frames, frames[1:]):
        if images_equal(a, b):
            continue
        ai = a.astype(np.int16)
        bi = b.astype(np.int16)
        # 상단 고정 밴드용 '바뀐 행'
        rchg |= (np.mean(np.abs(ai - bi), axis=(1, 2)) > 2.5)
        # 좌/우 고정 밴드용 열 분석(고정메뉴=평균차0, 스크롤바=썸만 이동)
        cmean = np.mean(np.abs(ai - bi), axis=(0, 2))
        cfrac = (np.abs(ai - bi).max(axis=2) > 22).mean(axis=0)
        l = 0
        while l < W and cmean[l] < 2.5:
            l += 1
        rs = 0
        k = W - 1
        while k >= 0 and cmean[k] < 2.5:
            rs += 1
            k -= 1
        sb = 0
        k2 = W - 1 - rs
        while k2 >= 0 and sb < 24 and cfrac[k2] < 0.45:
            sb += 1
            k2 -= 1
        lefts.append(l)
        rights.append(rs + sb)
    rspan = _content_span(rchg, win=32, frac=0.4)
    top = min(rspan[0] + 14, H // 2) if (rspan and rspan[0] >= 16) else 0
    left = min(min(lefts), int(W * 0.45)) if (lefts and min(lefts) >= 6) else 0
    right = min(min(rights), int(W * 0.45)) if (rights and min(rights) >= 6) else 0
    return top, left, right


def _trim_left_band(frame, L, thr=250):
    """좌측 고정 밴드가 흰 여백까지 물면 실제 내용(비흰색)이 있는 마지막 열까지로 축소."""
    if L <= 0:
        return 0
    col_has = (frame[:, :L] < thr).any(axis=2).any(axis=0)  # (L,) 열별 비흰색 유무
    idx = np.where(col_has)[0]
    if idx.size == 0:
        return 0
    return int(min(L, idx.max() + 4))


def _trim_right_band(frame, R, W, thr=250):
    """우측 고정 밴드가 흰 여백까지 물면 실제 내용이 시작되는 열부터로 축소."""
    if R <= 0:
        return 0
    col_has = (frame[:, W - R:] < thr).any(axis=2).any(axis=0)  # (R,)
    idx = np.where(col_has)[0]
    if idx.size == 0:
        return 0
    return int(min(R, R - idx.min() + 4))


def _sticky_rows(a, b, cl, cr, h):
    """
    두 프레임(a=이전, b=현재)에서 '같은 위치에 같은 내용'인 상단 고정 밴드(sticky) 높이.
    (스크롤됐는데도 안 바뀐 최상단 = sticky 메뉴)
    """
    diff = np.mean(np.abs(a[:h, cl:cr].astype(np.int16)
                          - b[:h, cl:cr].astype(np.int16)), axis=(1, 2))
    span = _content_span(diff > 2.5, win=24, frac=0.4)
    if span is None or span[0] < 16:
        return 0
    return min(span[0] + 10, h // 2)


def capture_scrolling_region(region, status_cb, opts=None):
    """
    opts: {'auto_top': bool, 'speed': float, 'strings': dict}
    """
    opts = opts or {}
    S = opts.get("strings", {})
    auto_top = opts.get("auto_top", True)
    handle_sticky = opts.get("handle_sticky", False)
    hide_mouse = opts.get("hide_mouse", True)
    speed = float(opts.get("speed", 1.0))
    notches = max(1, round(2 * speed))          # 하강 스텝
    top_notches = max(2, round(5 * speed))      # 상단 이동 스텝
    wait = min(0.8, max(0.12, 0.35 / speed))    # 렌더 대기

    def msg(key, default, **kw):
        return (S.get(key, default)).format(**kw) if kw else S.get(key, default)

    x, y, w, h = region
    if w < 10 or h < 10:
        raise RuntimeError(S.get("err_small_region", "선택한 영역이 너무 작습니다."))
    cx, cy = x + w // 2, y + h // 2

    # 마우스 숨기기: 캡처 직전 커서를 콘텐츠 밖(영역 위쪽/좌상단)으로 이동시킬 위치
    park = None
    if hide_mouse:
        park = (x + 2, y - 5) if y >= 5 else (x + 2, y + 2)

    # 마우스 숨기기 시: 커서를 안 움직이는 방식(send/key)을 우선 → 커서 깜빡임 방지.
    # (매 캡처마다 재설정하므로 별도 복원 불필요)
    METHODS[:] = ["send", "key", "wheel"] if hide_mouse else ["wheel", "send", "key"]

    child = win32gui.WindowFromPoint((cx, cy))
    hwnd = ctypes.windll.user32.GetAncestor(child, GA_ROOT)
    ensure_foreground(hwnd)
    time.sleep(0.4)

    pref = 0
    header_img = None
    footer_img = None

    with mss.mss() as sct:
        # ---------- 1) (옵션) 맨 위로 이동 ----------
        if auto_top:
            status_cb(S.get("status_moving_top", "상단으로 이동 중..."))
            pref = scroll_to_top(sct, region, hwnd, cx, cy, top_notches, wait, park=park)

        # ---------- 1-b) 고정 상/하/좌/우 영역 감지 (위치 복원) ----------
        status_cb(S.get("status_detecting", "고정 영역 감지 중..."))
        reg = measure_fixed_regions(sct, region, hwnd, cx, cy, notches, wait, park=park)
        top_band, bot_band = reg["top"], reg["bot"]
        left_band, right_band = reg["left"], reg["right"]
        if auto_top:
            pref = scroll_to_top(sct, region, hwnd, cx, cy, top_notches, wait, park=park)

        # 고정 상단(헤더)은 맨 위 1회, 고정 하단(트레이바 등)은 메모리에 보관 후 맨 아래에 덮음
        H_full = h
        full0 = grab(sct, region)
        if top_band > 0:
            header_img = full0[0:top_band].copy()
        if bot_band > 0:
            footer_img = full0[H_full - bot_band:H_full].copy()
        if top_band > 0 or bot_band > 0:
            y2 = y + top_band
            h = h - top_band - bot_band
            region = (x, y2, w, h)
            cx, cy = x + w // 2, y2 + h // 2

        # ---------- 1-c) (옵션) sticky 등 '깊이 스크롤해야 나타나는' 고정 영역 감지 ----------
        # 실제 깊은 위치의 연속 프레임을 비교해 위/좌/우 고정 밴드를 잡아 초기 감지를 보강.
        sticky_base = 0
        if handle_sticky:
            status_cb(S.get("status_detecting", "고정 영역 감지 중..."))
            dtop, dleft, dright = detect_fixed_deep(
                sct, region, hwnd, cx, cy, notches, wait, park)
            sticky_base = min(dtop, h // 2)
            left_band = max(left_band, dleft)
            right_band = max(right_band, dright)
            scroll_to_top(sct, region, hwnd, cx, cy, top_notches, wait, park=park)

        # ---------- 2) 아래로 스크롤하며 캡처 (composite 덮어쓰기) ----------
        SCORE_THR = 0.5
        status_cb(S.get("status_capturing", "캡처 중..."))
        canvas = grab(sct, region)
        # 좌/우 밴드가 흰 여백까지 물어 콘텐츠를 자르지 않도록 실제 내용 경계까지 축소
        if left_band > 0:
            left_band = _trim_left_band(canvas, left_band)
        if right_band > 0:
            right_band = _trim_right_band(canvas, right_band, w)
        cl, cr = left_band, w - right_band
        if cr - cl < max(40, w // 4):   # 콘텐츠 폭이 너무 좁으면 좌/우 밴드 무시
            left_band = right_band = 0
            cl, cr = 0, w
        # 배경색: 좌측 메뉴는 메뉴색, 우측 스크롤바 자리는 콘텐츠색으로 채움
        left_bg = (np.median(canvas[:, :left_band].reshape(-1, 3), axis=0).astype(np.uint8)
                   if left_band else None)
        content_bg = np.median(canvas[:, cl:cr].reshape(-1, 3), axis=0).astype(np.uint8)
        # 첫 컷의 좌/우(사이드바 기준) — 마지막 컷 비교용
        first_left = canvas[:h, :left_band].copy() if left_band else None
        first_right = canvas[:h, w - right_band:].copy() if right_band else None
        prev = canvas.copy()
        frame0 = canvas.copy()          # scroll0 첫 프레임 — 전환 sticky 정리용
        last_delta = 0
        offset = 0
        first_off = None                # 첫 place의 offset(전환 sticky 위치)
        sticky_seen = 0                 # 매 스텝 감지된 최대 sticky 높이
        left_mask = np.zeros(canvas.shape[0], bool)   # 좌측에 '실제 내용'을 쓴 행
        cap_fmt = S.get("status_capturing_fmt", "캡처 중... (높이 {h}px)")

        # 진행률(%) — 스크롤바 썸 위치로 계산. 없으면 단계 기반으로 완만히 상승.
        progress_cb = opts.get("progress_cb")
        sb_x0 = (w - min(right_band, 24)) if right_band else None
        track_col = None
        if sb_x0 is not None:
            strip0 = canvas[0:h, sb_x0:w].astype(np.int16)
            track_col = np.median(strip0.reshape(-1, 3), axis=0)
        last_pct = 0.0
        last_reported = -1

        def calc_pct(frame):
            if sb_x0 is None or track_col is None:
                return None
            try:
                strip = frame[:, sb_x0:w].astype(np.int16)
                dif = np.abs(strip - track_col).sum(axis=(1, 2))  # (h,)
                mx = float(dif.max())
                if mx < 60:
                    return None
                rows = np.where(dif > mx * 0.5)[0]
                if rows.size < 3:
                    return None
                top = int(rows.min())
                th = int(rows.max()) - top + 1
                return min(1.0, max(0.0, top / max(1, h - th)))
            except Exception:
                return None

        def report(frame):
            nonlocal last_pct, last_reported
            if not progress_cb:
                return
            p = calc_pct(frame)
            if p is None:
                last_pct = min(0.95, last_pct + 0.02)   # 폴백: 완만히 상승
            else:
                last_pct = max(last_pct, p)              # 단조 증가
            ip = int(round(last_pct * 100))
            if ip != last_reported:
                last_reported = ip
                progress_cb(ip)

        def order():
            return METHODS[pref:] + METHODS[:pref]

        def C(fr):
            # 콘텐츠 열만(정렬·변화감지용) + sticky 상단 제외
            return fr[sticky_base:h, cl:cr]

        def place(frame, d, st=None):
            # 콘텐츠 열(cl:cr)과 상단 sticky(st)를 제외하고 덮어씀.
            # 좌측 밴드는 '첫 컷(사이드바)과 다른 행 = 실제 내용'만 복원(전체폭 표/footer 살림).
            nonlocal canvas, offset, left_mask, first_off
            if st is None:
                st = sticky_base
            offset += d
            if first_off is None:
                first_off = offset
            need = offset + h
            if canvas.shape[0] < need:
                add = need - canvas.shape[0]
                canvas = np.vstack([canvas,
                                    np.zeros((add, canvas.shape[1], 3), canvas.dtype)])
                left_mask = np.concatenate([left_mask, np.zeros(add, bool)])
            canvas[offset + st:offset + h, cl:cr] = frame[st:h, cl:cr]
            if left_band and first_left is not None:
                seg = frame[st:h, :left_band]
                dl = np.mean(np.abs(seg.astype(np.int16)
                                    - first_left[st:h].astype(np.int16)), axis=(1, 2))
                di = np.where(dl > 12)[0]
                if di.size:
                    rr = offset + st + di
                    canvas[rr, :left_band] = seg[di]
                    left_mask[rr] = True
            report(frame)

        for _ in range(8000):
            before = prev

            # (a) 작은 스텝으로 정확히 진행 (콘텐츠 열만으로 정렬)
            #     매 스텝 '이전/현재 프레임의 상단 동일영역'을 sticky로 감지해 제외(제안 방식).
            best = None
            for m in order():
                apply_scroll(hwnd, cx, cy, m, down=True, notches=notches, park=park)
                time.sleep(wait)
                cur = grab(sct, region)
                st = sticky_base
                if handle_sticky:
                    st = max(sticky_base, _sticky_rows(before, cur, cl, cr, h))
                delta, score = find_delta(
                    before[st:h, cl:cr], cur[st:h, cl:cr], max_frac=0.6)
                if 2 < delta <= h - st and score >= SCORE_THR:
                    best = (cur, delta, st)
                    pref = METHODS.index(m)
                    break
            if best is not None:
                cur, delta, st = best
                place(cur, delta, st)
                prev = cur
                last_delta = delta
                sticky_seen = max(sticky_seen, st)
                if canvas.shape[0] > 100000:
                    break
                status_cb(cap_fmt.format(h=canvas.shape[0]))
                continue

            # (b) 화소가 조금 바뀌었으나 정합 실패 → 직전 delta로 진행
            if not images_equal(C(before), C(cur)):
                d = last_delta if last_delta > 2 else h // 4
                place(cur, d)
                prev = cur
                status_cb(cap_fmt.format(h=canvas.shape[0]))
                continue

            # (c) 변화 없음 → 점점 큰 스크롤로 단색구간/바닥 판별
            crossed = None
            for pn in (8, 20, 45):
                for m in order():
                    apply_scroll(hwnd, cx, cy, m, down=True, notches=pn, park=park)
                    time.sleep(wait * 1.4)
                    cur = grab(sct, region)
                    if not images_equal(C(before), C(cur)):
                        crossed = cur
                        pref = METHODS.index(m)
                        break
                if crossed is not None:
                    break
            if crossed is None:
                break  # 진짜 바닥

            cur = crossed
            delta, score = find_delta(C(before), C(cur), max_frac=0.95)
            d = delta if (2 < delta <= h and score >= SCORE_THR) else h
            place(cur, d)
            prev = cur
            status_cb(cap_fmt.format(h=canvas.shape[0]))
            continue

    # 화면 캡처(그랩) 단계 종료 → 이후는 합성/저장(화면 캡처 없음). '진행중' 오버레이 표시 신호.
    processing_cb = opts.get("processing_cb")
    if processing_cb:
        try:
            processing_cb()
        except Exception:
            pass

    if canvas.shape[0] <= h + 4:
        raise RuntimeError(S.get(
            "err_no_scroll",
            "스크롤이 되지 않아 한 화면만 캡처되었습니다.\n"
            "· 대상 대화 영역(스크롤되는 부분)을 정확히 지정했는지\n"
            "· 이 프로그램을 관리자 권한으로 실행했는지 확인하세요."))

    # 전환 sticky 정리: 딥 감지 실패로 첫 스크롤 컷에 한 번 찍힌 sticky를,
    # 첫 프레임(scroll0)의 실제 내용으로 덮어 제거(=GitHub에서 1회 나오던 것 제거).
    if handle_sticky and sticky_seen > 0 and first_off is not None:
        e = first_off + sticky_seen
        if e <= h and e <= canvas.shape[0]:
            canvas[first_off:e, cl:cr] = frame0[first_off:e, cl:cr]

    # 좌측: place가 '실제 내용'을 쓴 행(left_mask)은 그대로 두고, 나머지(=사이드바)만 배경색.
    # 우측(스크롤바 자리)은 배경색으로 채우고 마지막 컷 footer만 실제 내용 복원.
    if canvas.shape[0] > h:
        if left_band > 0 and left_bg is not None:
            reg = canvas[h:, :left_band]
            reg[~left_mask[h:]] = left_bg
        if right_band > 0:
            canvas[h:, w - right_band:] = content_bg
            if first_right is not None:
                cs = prev[:h, w - right_band:]
                d = np.mean(np.abs(cs.astype(np.int16) - first_right.astype(np.int16)),
                            axis=(1, 2))
                r = h - 1
                while r >= 0 and d[r] > 8:
                    r -= 1
                if r + 1 < h:
                    idx = np.arange(r + 1, h)
                    canvas[offset + idx, w - right_band:] = cs[idx]

    # 고정 상단(헤더)은 맨 위, 고정 하단(트레이바 등)은 맨 아래에 1회 덮어 붙임
    if header_img is not None and header_img.shape[1] == canvas.shape[1]:
        canvas = np.vstack([header_img, canvas])
    if footer_img is not None and footer_img.shape[1] == canvas.shape[1]:
        canvas = np.vstack([canvas, footer_img])
    return canvas


def save_png(img, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    H, W = img.shape[:2]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"capture_{ts}_{W}x{H}.png")
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("이미지 인코딩 실패")
    buf.tofile(path)  # 한글 경로 대응
    return path


# ==================================================================
# 영역 선택 오버레이
# ==================================================================
class RegionSelector:
    """드래그로 영역 선택. multi=True면 전체 가상 화면(멀티 모니터) 위에서 선택.
    실제 좌표는 win32 커서 좌표로 취득해 DPI 스케일 불일치를 방지한다."""

    def __init__(self, master, multi=False, guide=""):
        self.result = None
        self._start_scr = None
        self._start_cnv = None
        vx, vy, vw, vh = virtual_screen() if multi else primary_screen()
        self._org = (vx, vy)

        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.geometry(f"{vw}x{vh}+{vx}+{vy}")
        self.top.attributes("-alpha", 0.28)
        self.top.attributes("-topmost", True)
        self.top.configure(bg="black", cursor="cross")
        self.cv = tk.Canvas(self.top, highlightthickness=0, bg="black")
        self.cv.pack(fill="both", expand=True)
        self.cv.create_text(
            vw // 2, 40, text=guide,
            fill="white", font=("맑은 고딕", 16, "bold"),
        )
        self.rect_id = None
        self.cv.bind("<ButtonPress-1>", self._down)
        self.cv.bind("<B1-Motion>", self._move)
        self.cv.bind("<ButtonRelease-1>", self._up)
        self.top.bind("<Escape>", lambda e: self._cancel())
        self.top.focus_force()
        self.top.grab_set()
        master.wait_window(self.top)

    def _down(self, e):
        self._start_scr = win32gui.GetCursorPos()
        self._start_cnv = (e.x, e.y)
        if self.rect_id:
            self.cv.delete(self.rect_id)
        self.rect_id = self.cv.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#ff3b30", width=2
        )

    def _move(self, e):
        if self.rect_id:
            self.cv.coords(self.rect_id, self._start_cnv[0], self._start_cnv[1], e.x, e.y)

    def _up(self, e):
        end = win32gui.GetCursorPos()
        x1, y1 = self._start_scr
        x2, y2 = end
        x, y = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        self.result = (x, y, w, h) if (w >= 10 and h >= 10) else None
        self.top.destroy()

    def _cancel(self):
        self.result = None
        self.top.destroy()


class MonitorPicker:
    """마우스가 올라간 모니터를 반투명으로 덮고 안내문구를 표시.
    다른 모니터로 이동하면 그 모니터로 오버레이가 따라간다. 클릭 시 선택."""

    def __init__(self, master, guide=""):
        self.result = None
        self.monitors = get_monitors()
        self.cur = None
        self._job = None

        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.attributes("-alpha", 0.32)
        self.top.attributes("-topmost", True)
        self.top.configure(bg="#1e6fff", cursor="hand2")
        self.lbl = tk.Label(self.top, text=guide, bg="#1e6fff", fg="white",
                            font=("맑은 고딕", 16, "bold"))
        self.lbl.place(relx=0.5, rely=0.5, anchor="center")
        self.top.bind("<Button-1>", lambda e: self._pick())
        self.top.bind("<Escape>", lambda e: self._close(None))
        self._poll()
        self.top.focus_force()
        master.wait_window(self.top)

    def _mon_at(self, x, y):
        for r in self.monitors:
            mx, my, mw, mh = r
            if mx <= x < mx + mw and my <= y < my + mh:
                return r
        return None

    def _poll(self):
        try:
            x, y = win32gui.GetCursorPos()
        except Exception:
            x, y = 0, 0
        r = self._mon_at(x, y)
        if r and r != self.cur:
            self.cur = r
            self.top.geometry(f"{r[2]}x{r[3]}+{r[0]}+{r[1]}")
        self._job = self.top.after(60, self._poll)

    def _pick(self):
        self._close(self.cur)

    def _close(self, result):
        self.result = result
        if self._job:
            try:
                self.top.after_cancel(self._job)
            except Exception:
                pass
        self.top.destroy()


class WindowPicker:
    """마우스 아래의 창을 빨간 테두리로 강조하고, 클릭하면 그 창을 선택.
    (전체를 덮지 않아 아래 창을 WindowFromPoint 로 감지할 수 있음)."""

    def __init__(self, master, guide=""):
        self.result = None      # (x, y, w, h)
        self.hwnd = None
        self.cur_hwnd = None
        self._job = None
        self._own_hwnds = set()

        # 안내 배너(상단 중앙)
        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.93)
        self.top.configure(bg="#1e6fff")
        tk.Label(self.top, text=guide, bg="#1e6fff", fg="white",
                 font=("맑은 고딕", 14, "bold"), padx=22, pady=12).pack()
        self.top.update_idletasks()
        vx, vy, vw, vh = virtual_screen()
        bw = self.top.winfo_width()
        self.top.geometry(f"+{vx + (vw - bw) // 2}+{vy + 40}")

        # 강조 테두리(얇은 4개 스트립)
        self.edges = []
        for _ in range(4):
            e = tk.Toplevel(master)
            e.overrideredirect(True)
            e.attributes("-topmost", True)
            e.configure(bg="#ff3b30")
            e.withdraw()
            self.edges.append(e)

        self.top.bind("<Escape>", lambda e: self._close(None))
        self.top.focus_force()
        self._prev_btn = bool(win32api.GetAsyncKeyState(0x01) & 0x8000)
        self.top.after(60, self._register_own)
        self._job = self.top.after(120, self._poll)
        master.wait_window(self.top)

    def _register_own(self):
        for w in [self.top] + self.edges:
            try:
                hid = w.winfo_id()
                self._own_hwnds.add(hid)
                self._own_hwnds.add(
                    ctypes.windll.user32.GetAncestor(hid, GA_ROOT))
            except Exception:
                pass

    def _window_under(self, x, y):
        try:
            root = ctypes.windll.user32.GetAncestor(
                win32gui.WindowFromPoint((x, y)), GA_ROOT)
        except Exception:
            return None
        if not root or root in self._own_hwnds:
            return None
        return root

    def _draw_outline(self, hwnd):
        try:
            l, t, w, h = get_window_rect(hwnd)
        except Exception:
            return
        th = 4
        geoms = [
            (w, th, l, t),                 # top
            (w, th, l, t + h - th),        # bottom
            (th, h, l, t),                 # left
            (th, h, l + w - th, t),        # right
        ]
        for e, (gw, gh, gx, gy) in zip(self.edges, geoms):
            try:
                e.geometry(f"{max(1, int(gw))}x{max(1, int(gh))}+{int(gx)}+{int(gy)}")
                e.deiconify()
                e.lift()
            except Exception:
                pass

    def _poll(self):
        try:
            x, y = win32gui.GetCursorPos()
        except Exception:
            x, y = 0, 0
        hwnd = self._window_under(x, y)
        if hwnd and hwnd != self.cur_hwnd:
            self.cur_hwnd = hwnd
            self._draw_outline(hwnd)

        btn = bool(win32api.GetAsyncKeyState(0x01) & 0x8000)
        if btn and not self._prev_btn and self.cur_hwnd:
            self._close(self.cur_hwnd)
            return
        self._prev_btn = btn
        self._job = self.top.after(60, self._poll)

    def _close(self, hwnd):
        if self._job:
            try:
                self.top.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if hwnd:
            self.hwnd = hwnd
            try:
                self.result = get_window_rect(hwnd)
            except Exception:
                self.result = None
        for w in self.edges + [self.top]:
            try:
                w.destroy()
            except Exception:
                pass


class Tooltip:
    """위젯 아래 중앙에 표시되는 툴팁. 첫 줄은 굵은 제목(+단축키), 다음 줄은 사용 안내.
    title/hint/key 는 문자열 또는 표시 시점에 호출되는 callable 모두 허용(단축키 최신 반영)."""

    def __init__(self, widget, title, hint, key=None):
        self.widget = widget
        self.title = title
        self.hint = hint
        self.key = key
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    @staticmethod
    def _val(v):
        try:
            return v() if callable(v) else v
        except Exception:
            return ""

    def _enter(self, _=None):
        self._cancel()
        self._after = self.widget.after(300, self._show)

    def _cancel(self):
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self):
        if self.tip or not self.widget.winfo_exists():
            return
        cx = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        frm = tk.Frame(tw, bg="#2b2b2b", highlightbackground="#555",
                       highlightthickness=1)
        frm.pack()
        # 제목 줄: 제목(굵게) + 단축키(옆에, 옅은 파랑)
        head = tk.Frame(frm, bg="#2b2b2b")
        head.pack(anchor="w", padx=10, pady=(6, 0))
        tk.Label(head, text=self._val(self.title), bg="#2b2b2b", fg="white",
                 font=("맑은 고딕", 9, "bold")).pack(side="left")
        key_txt = self._val(self.key)
        if key_txt:
            tk.Label(head, text=key_txt, bg="#2b2b2b", fg="#8fb7ff",
                     font=("맑은 고딕", 9, "bold")).pack(side="left", padx=(8, 0))
        tk.Label(frm, text=self._val(self.hint), bg="#2b2b2b", fg="#cfcfcf",
                 font=("맑은 고딕", 8), justify="left", wraplength=220).pack(
                     anchor="w", padx=10, pady=(1, 7))
        tw.update_idletasks()
        tw.geometry(f"+{cx - tw.winfo_width() // 2}+{y}")

    def _leave(self, _=None):
        self._cancel()
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000


class HotkeyManager:
    """전역 단축키(RegisterHotKey)를 백그라운드 스레드에서 등록/수신.
    hwnd=None 으로 등록하면 WM_HOTKEY 가 그 스레드 큐로 전달된다."""

    def __init__(self, dispatch_cb):
        self.dispatch_cb = dispatch_cb      # (mode) -> None (메인 스레드로 넘겨야 함)
        self.bindings = {}                  # mode -> (mods, vk)
        self._thread = None
        self._tid = None
        self._stop = threading.Event()

    def set_bindings(self, bindings):
        self.bindings = {m: (v[0], v[1]) for m, v in bindings.items() if v}
        self.restart()

    def restart(self):
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        t = self._thread
        if t and t.is_alive():
            self._stop.set()
            if self._tid:
                try:
                    win32api.PostThreadMessage(self._tid, win32con.WM_NULL, 0, 0)
                except Exception:
                    pass
            t.join(timeout=1.0)
        self._thread = None
        self._tid = None

    def _run(self):
        user32 = ctypes.windll.user32
        self._tid = win32api.GetCurrentThreadId()
        ids = {}
        i = 1
        for mode, (mods, vk) in self.bindings.items():
            try:
                if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                    ids[i] = mode
            except Exception:
                pass
            i += 1
        try:
            from ctypes import wintypes
            msg = wintypes.MSG()
            while not self._stop.is_set():
                r = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                if r == 0 or r == -1:
                    break
                if msg.message == WM_HOTKEY:
                    mode = ids.get(int(msg.wParam))
                    if mode:
                        try:
                            self.dispatch_cb(mode)
                        except Exception:
                            pass
        finally:
            for hid in list(ids):
                try:
                    user32.UnregisterHotKey(None, hid)
                except Exception:
                    pass


# keysym -> 표시 이름
_KEY_SPECIAL = {
    "space": "Space", "Return": "Enter", "Escape": "Esc", "Tab": "Tab",
    "Prior": "PageUp", "Next": "PageDown", "Home": "Home", "End": "End",
    "Delete": "Del", "Insert": "Ins", "BackSpace": "Backspace",
    "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
}
_MOD_KEYSYMS = {"Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L",
                "Shift_R", "Win_L", "Win_R", "Super_L", "Super_R",
                "Meta_L", "Meta_R"}


def _key_display_name(keysym):
    if keysym in _KEY_SPECIAL:
        return _KEY_SPECIAL[keysym]
    if len(keysym) == 1:
        return keysym.upper()
    if keysym.startswith("F") and keysym[1:].isdigit():
        return keysym
    if keysym.startswith("KP_"):
        return keysym[3:]
    return keysym.capitalize() if keysym else None


class HotkeyDialog:
    """캡처 7종의 단축키를 편집하는 모달 창. 저장 시 on_save(dict) 호출."""

    def __init__(self, master, t, hotkeys, on_save):
        self.t = t
        self.on_save = on_save
        # temp: mode -> (mods, vk, text) 또는 None
        self.temp = {m: (hotkeys.get(m) if hotkeys.get(m) else None)
                     for m in HOTKEY_MODES}
        self.entries = {}

        self.top = tk.Toplevel(master)
        self.top.title(t("hk_title"))
        self.top.transient(master)
        self.top.resizable(False, False)
        self.top.configure(bg=MC["bg"])
        try:
            ico = resource_path("icon.ico")
            if os.path.isfile(ico):
                self.top.iconbitmap(ico)
        except Exception:
            pass

        frm = ttk.Frame(self.top, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=t("hk_hint"), style="Muted.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        for i, m in enumerate(HOTKEY_MODES, start=1):
            ttk.Label(frm, text=t(f"tip_{m}_t")).grid(
                row=i, column=0, sticky="w", padx=(0, 10), pady=3)
            e = ttk.Entry(frm, width=22, justify="center")
            e.grid(row=i, column=1, sticky="ew", pady=3)
            e.bind("<KeyPress>", lambda ev, mm=m: self._on_key(ev, mm))
            e.bind("<FocusIn>", lambda ev, ee=e: ee.selection_range(0, "end"))
            self.entries[m] = e
            self._refresh_entry(m)

        frm.columnconfigure(1, weight=1)

        btns = ttk.Frame(frm)
        btns.grid(row=len(HOTKEY_MODES) + 1, column=0, columnspan=2,
                  sticky="e", pady=(12, 0))
        ttk.Button(btns, text=t("hk_close"), style="Secondary.TButton",
                   command=self.top.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text=t("hk_save"), style="Primary.TButton",
                   command=self._save).pack(side="right")

        self.top.bind("<Escape>", lambda e: None)  # Esc는 입력창에서 '해제'로 사용
        self.top.update_idletasks()
        # 부모 중앙에 배치
        try:
            px = master.winfo_rootx() + (master.winfo_width()
                                         - self.top.winfo_width()) // 2
            py = master.winfo_rooty() + 60
            self.top.geometry(f"+{max(0, px)}+{max(0, py)}")
        except Exception:
            pass
        self.top.grab_set()
        self.top.focus_force()

    def _refresh_entry(self, mode):
        e = self.entries[mode]
        val = self.temp.get(mode)
        text = val[2] if val else self.t("hk_none")
        e.config(state="normal")
        e.delete(0, "end")
        e.insert(0, text)
        e.config(state="readonly")

    def _on_key(self, event, mode):
        ks = event.keysym
        if ks in _MOD_KEYSYMS:
            return "break"
        if ks == "Escape":                 # 해제
            self.temp[mode] = None
            self._refresh_entry(mode)
            return "break"
        mods = 0
        parts = []
        if win32api.GetKeyState(win32con.VK_CONTROL) < 0:
            mods |= 0x0002
            parts.append("Ctrl")
        if win32api.GetKeyState(win32con.VK_MENU) < 0:
            mods |= 0x0001
            parts.append("Alt")
        if win32api.GetKeyState(win32con.VK_SHIFT) < 0:
            mods |= 0x0004
            parts.append("Shift")
        if (win32api.GetKeyState(win32con.VK_LWIN) < 0
                or win32api.GetKeyState(win32con.VK_RWIN) < 0):
            mods |= 0x0008
            parts.append("Win")
        name = _key_display_name(ks)
        if not name:
            return "break"
        parts.append(name)
        self.temp[mode] = (mods, int(event.keycode), " + ".join(parts))
        self._refresh_entry(mode)
        return "break"

    def _save(self):
        # 중복 검사
        seen = {}
        for m in HOTKEY_MODES:
            v = self.temp.get(m)
            if not v:
                continue
            key = (v[0], v[1])
            if key in seen:
                messagebox.showwarning(
                    self.t("hk_title"),
                    self.t("hk_dup_fmt", keys=v[2]))
                return
            seen[key] = m
        out = {m: ({"mods": v[0], "vk": v[1], "text": v[2]} if v else None)
               for m, v in self.temp.items()}
        self.on_save(out)
        messagebox.showinfo(self.t("hk_title"), self.t("hk_saved"))
        self.top.destroy()


class BusyOverlay:
    """캡처 후 저장/합성 중 '잠시만 기다려 주세요' + 큰 진행 아이콘을 화면 가운데 표시."""

    def __init__(self, master, text):
        self._job = None
        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        vx, vy, vw, vh = virtual_screen()
        self.top.geometry(f"{vw}x{vh}+{vx}+{vy}")
        self.top.attributes("-alpha", 0.6)
        self.top.attributes("-topmost", True)
        self.top.configure(bg="black", cursor="watch")
        box = tk.Frame(self.top, bg="black")
        box.place(relx=0.5, rely=0.5, anchor="center")
        self.canvas = tk.Canvas(box, width=120, height=120, bg="black",
                                highlightthickness=0)
        self.canvas.pack(pady=(0, 24))
        self.canvas.create_oval(15, 15, 105, 105, outline="#333", width=10)
        self._arc = self.canvas.create_arc(15, 15, 105, 105, start=90,
                                            extent=90, style="arc",
                                            outline="#4aa3ff", width=10)
        tk.Label(box, text=text, bg="black", fg="white",
                 font=("맑은 고딕", 20, "bold")).pack()
        self._start = 90
        self._spin()

    def _spin(self):
        try:
            self._start = (self._start - 18) % 360
            self.canvas.itemconfig(self._arc, start=self._start)
            self._job = self.top.after(50, self._spin)
        except Exception:
            pass

    def close(self):
        if self._job:
            try:
                self.top.after_cancel(self._job)
            except Exception:
                pass
        try:
            self.top.destroy()
        except Exception:
            pass


# ==================================================================
# Clean Material UI 스킨 (색상 / 폰트)
# ==================================================================
MC = {
    "primary": "#03A9F4",        # Ocean Blue
    "primary_hover": "#0288D1",  # Deep Ocean
    "bg": "#FFFFFF",             # Surface White
    "surface": "#F5F5F5",        # Light Gray
    "text": "#333333",           # Dark Slate
    "muted": "#757575",          # Muted Gray
    "border": "#E0E0E0",         # Line Gray
    "list_hover": "#E1F5FE",     # 매우 연한 블루
    "thumb": "#BDBDBD",
    "thumb_hover": "#9E9E9E",
    "white": "#FFFFFF",
    "disabled": "#BBBBBB",
}


def pick_font_family():
    """Pretendard / Noto Sans KR 우선, 없으면 맑은 고딕."""
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families())
        for f in ("Pretendard", "Noto Sans KR", "맑은 고딕", "Malgun Gothic"):
            if f in fams:
                return f
    except Exception:
        pass
    return "맑은 고딕"


# ==================================================================
# 메인 GUI
# ==================================================================
class App:
    def __init__(self):
        self.busy = False
        self._thumbs = []
        self.cfg = load_cfg()

        self.root = tk.Tk()
        self.root.geometry("560x760")
        self.root.minsize(520, 680)
        self.root.configure(bg=MC["bg"])
        try:
            ico = resource_path("icon.ico")
            if os.path.isfile(ico):
                self.root.iconbitmap(ico)
        except Exception:
            pass

        # ----- Clean Material UI 폰트/스타일 -----
        fam = pick_font_family()
        self.f_title = (fam, -16, "bold")   # Panel Title 16px Bold
        self.f_text = (fam, -14)            # 본문/리스트 14px
        self.f_bold = (fam, -14, "bold")
        self.f_cap = (fam, -12)             # 보조 12px
        self._setup_style()

        # 상태 변수 (언어 전환 시에도 유지)
        default_dir = self.cfg.get("save_dir") or os.path.join(app_base_dir(), "capture")
        self.save_dir = tk.StringVar(value=default_dir)
        self.opt_scroll_top = tk.BooleanVar(value=bool(self.cfg.get("scroll_top", True)))
        self.opt_multi = tk.BooleanVar(value=bool(self.cfg.get("multi_monitor", True)))
        self.opt_hide = tk.BooleanVar(value=bool(self.cfg.get("hide_app", True)))
        self.opt_on_top = tk.BooleanVar(value=bool(self.cfg.get("on_top", False)))
        self.opt_sticky = tk.BooleanVar(value=bool(self.cfg.get("sticky", False)))
        self.opt_hide_mouse = tk.BooleanVar(value=bool(self.cfg.get("hide_mouse", True)))
        self.opt_auto_copy = tk.BooleanVar(value=bool(self.cfg.get("auto_copy", True)))
        self.speed_idx = tk.IntVar(value=int(self.cfg.get("speed_idx", SPEED_DEFAULT_IDX)))
        self.status = tk.StringVar()
        self._update_info = None   # 사용 가능한 업데이트 정보
        self._busy_overlay = None  # 저장 중 '진행중' 오버레이

        self.hotkeys = self._load_hotkeys()
        self.hotkeys_mgr = HotkeyManager(self._hotkey_fire)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.attributes("-topmost", self.opt_on_top.get())
        self.refresh_list()
        self._apply_hotkeys()
        self._auto_check_update()

    # ---------- 번역 헬퍼 ----------
    def t(self, key, **kw):
        return tr(self.cfg.get("lang", "ko"), key, **kw)

    # ---------- Clean Material UI 스타일 ----------
    def _setup_style(self):
        C = MC
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=C["bg"], foreground=C["text"],
                     font=self.f_text, focuscolor=C["bg"])
        st.configure("TFrame", background=C["bg"])
        st.configure("TLabel", background=C["bg"], foreground=C["text"])
        st.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"],
                     font=self.f_cap)
        st.configure("Title.TLabel", background=C["bg"], foreground=C["text"],
                     font=self.f_title)
        st.configure("Primary.TLabel", background=C["bg"],
                     foreground=C["primary"], font=self.f_bold)
        # 패널(LabelFrame): 얇은 테두리 + 굵은 제목
        st.configure("TLabelframe", background=C["bg"], bordercolor=C["border"],
                     relief="solid", borderwidth=1)
        st.configure("TLabelframe.Label", background=C["bg"],
                     foreground=C["text"], font=self.f_bold)
        # 체크박스
        st.configure("TCheckbutton", background=C["bg"], foreground=C["text"],
                     font=self.f_text)
        st.map("TCheckbutton",
               background=[("active", C["bg"])],
               foreground=[("disabled", C["muted"])])
        # Entry / Combobox
        st.configure("TEntry", fieldbackground=C["white"],
                     bordercolor=C["border"], foreground=C["text"],
                     insertcolor=C["text"], padding=4)
        st.configure("TCombobox", fieldbackground=C["white"],
                     background=C["white"], bordercolor=C["border"],
                     arrowcolor=C["muted"], foreground=C["text"], padding=3)
        st.map("TCombobox", fieldbackground=[("readonly", C["white"])],
               foreground=[("readonly", C["text"])])
        # Primary 버튼(파란 배경/흰 글자)
        st.configure("Primary.TButton", background=C["primary"],
                     foreground=C["white"], font=self.f_bold, borderwidth=0,
                     relief="flat", padding=(12, 5), focuscolor=C["primary"])
        st.map("Primary.TButton",
               background=[("active", C["primary_hover"]),
                           ("pressed", C["primary_hover"]),
                           ("disabled", C["disabled"])],
               foreground=[("disabled", "#F0F0F0")])
        # Secondary 버튼(흰 배경/회색 테두리)
        st.configure("Secondary.TButton", background=C["white"],
                     foreground=C["text"], bordercolor=C["border"],
                     borderwidth=1, relief="solid", font=self.f_cap,
                     padding=(8, 3), focuscolor=C["white"])
        st.map("Secondary.TButton",
               background=[("active", C["surface"]), ("pressed", C["surface"])],
               bordercolor=[("active", C["primary"])])
        # 캡처 아이콘 버튼(플랫, 흰 배경, hover 시 연블루)
        st.configure("Icon.TButton", background=C["white"], borderwidth=1,
                     bordercolor=C["border"], relief="solid", padding=0,
                     focuscolor=C["white"])
        st.map("Icon.TButton",
               background=[("active", C["list_hover"]),
                           ("pressed", C["list_hover"]),
                           ("disabled", C["surface"])],
               bordercolor=[("active", C["primary"])])
        # 얇은 스크롤바
        st.configure("Thin.Vertical.TScrollbar", troughcolor=C["surface"],
                     background=C["thumb"], bordercolor=C["surface"],
                     arrowcolor=C["surface"], arrowsize=1, width=8,
                     relief="flat")
        st.map("Thin.Vertical.TScrollbar",
               background=[("active", C["thumb_hover"]),
                           ("pressed", C["thumb_hover"])])
        # 진행바
        st.configure("Material.Horizontal.TProgressbar",
                     troughcolor=C["surface"], background=C["primary"],
                     bordercolor=C["surface"], lightcolor=C["primary"],
                     darkcolor=C["primary"])
        self.style = st
        self._make_check_images()

    def _make_check_images(self):
        """머티리얼 체크박스(18px, 라운드 2px, 파란 채움 + 흰 체크)."""
        try:
            from PIL import ImageDraw
            C = MC
            ss = 4
            s = 18 * ss
            r = 2 * ss

            def mk(checked):
                img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                if checked:
                    d.rounded_rectangle([1, 1, s - 2, s - 2], radius=r,
                                        fill=C["primary"])
                    lw = max(2, int(s * 0.11))
                    d.line([s * 0.27, s * 0.52, s * 0.43, s * 0.68],
                           fill="white", width=lw)
                    d.line([s * 0.43, s * 0.68, s * 0.75, s * 0.30],
                           fill="white", width=lw)
                else:
                    d.rounded_rectangle([2, 2, s - 3, s - 3], radius=r,
                                        outline=C["muted"], width=2 * ss)
                return ImageTk.PhotoImage(img.resize((18, 18), Image.LANCZOS))

            self.img_check = mk(True)
            self.img_uncheck = mk(False)
            st = self.style
            st.element_create("mat.checkind", "image", self.img_uncheck,
                              ("selected", self.img_check),
                              ("!selected", "disabled", self.img_uncheck),
                              sticky="", width=24)
            st.layout("TCheckbutton", [
                ("Checkbutton.padding", {"sticky": "nswe", "children": [
                    ("mat.checkind", {"side": "left", "sticky": ""}),
                    ("Checkbutton.focus", {"side": "left", "sticky": "",
                                           "children": [
                        ("Checkbutton.label", {"sticky": "nswe"})]})]})])
            st.configure("TCheckbutton", background=C["bg"],
                         foreground=C["text"], font=self.f_text,
                         padding=(0, 1))
        except Exception:
            pass

    def _save_cfg(self):
        self.cfg.update({
            "scroll_top": self.opt_scroll_top.get(),
            "multi_monitor": self.opt_multi.get(),
            "hide_app": self.opt_hide.get(),
            "on_top": self.opt_on_top.get(),
            "sticky": self.opt_sticky.get(),
            "hide_mouse": self.opt_hide_mouse.get(),
            "auto_copy": self.opt_auto_copy.get(),
            "speed_idx": self.speed_idx.get(),
            "save_dir": self.save_dir.get(),
        })
        save_cfg(self.cfg)

    def _toggle_topmost(self):
        self.root.attributes("-topmost", self.opt_on_top.get())
        self._save_cfg()

    # ---------- 전역 단축키 ----------
    def _load_hotkeys(self):
        """cfg 저장값 + 기본값 병합 → {mode: (mods, vk, text) 또는 None}."""
        saved = self.cfg.get("hotkeys") or {}
        hk = {}
        for m in HOTKEY_MODES:
            if m in saved:
                v = saved[m]
                hk[m] = ((int(v["mods"]), int(v["vk"]), v.get("text", ""))
                         if v else None)
            else:
                hk[m] = DEFAULT_HOTKEYS[m]
        return hk

    def _hotkey_text(self, mode):
        """툴팁 제목 옆에 표시할 현재 단축키 문자열(없으면 '')."""
        v = self.hotkeys.get(mode)
        return v[2] if v else ""

    def _apply_hotkeys(self):
        binds = {m: (v[0], v[1]) for m, v in self.hotkeys.items() if v}
        try:
            self.hotkeys_mgr.set_bindings(binds)
        except Exception:
            pass

    def _hotkey_fire(self, mode):
        """단축키 스레드에서 호출 → 메인 스레드로 넘겨 캡처 시작."""
        try:
            self.root.after(0, lambda m=mode: self.start_capture(m))
        except Exception:
            pass

    def _open_hotkeys(self):
        HotkeyDialog(self.root, self.t, self.hotkeys, self._on_hotkeys_saved)

    def _on_hotkeys_saved(self, out):
        self.cfg["hotkeys"] = out
        save_cfg(self.cfg)
        self.hotkeys = self._load_hotkeys()
        self._apply_hotkeys()

    def _on_close(self):
        try:
            self.hotkeys_mgr.stop()
        except Exception:
            pass
        self.root.destroy()

    # ---------- UI 구성 ----------
    def _build_ui(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.title(f"{APP_TITLE} v{APP_VERSION}")

        # 업데이트 알림 배너(사용 가능할 때만 표시)
        self.update_bar = tk.Frame(self.root, bg=MC["list_hover"])
        self.update_lbl = tk.Label(self.update_bar, bg=MC["list_hover"],
                                   fg=MC["primary_hover"], font=self.f_cap)
        self.update_lbl.pack(side="left", padx=12, pady=5)
        tk.Button(self.update_bar, text=self.t("btn_download"), relief="flat",
                  bg=MC["primary"], fg="white", cursor="hand2", bd=0,
                  activebackground=MC["primary_hover"], activeforeground="white",
                  command=self._open_update).pack(side="right", padx=10, pady=5)

        # 캡처 버튼 7개 (아이콘 + 툴팁). 한 줄로 가로 정렬.
        #  스크롤 3종(디스플레이/윈도우/영역) + 즉시 캡처 4종(전체/디스플레이/윈도우/영역)
        top = ttk.Frame(self.root, padding=(14, 12, 14, 6))
        top.pack(fill="x")
        self.btn_bar = top
        self._icon_imgs = make_capture_icons(70, margin=10)
        modes = ["scroll_display", "scroll_window", "scroll_region",
                 "shot_all", "shot_display", "shot_window", "shot_region"]
        self.cap_buttons = []
        for i, m in enumerate(modes):
            b = ttk.Button(top, image=self._icon_imgs[m], padding=0,
                           style="Icon.TButton",
                           command=lambda mm=m: self.start_capture(mm))
            b.grid(row=0, column=i, sticky="nsew", padx=3)
            top.columnconfigure(i, weight=1, uniform="cap")
            Tooltip(b, self.t(f"tip_{m}_t"), self.t(f"tip_{m}_h"),
                    key=(lambda mm=m: self._hotkey_text(mm)))
            self.cap_buttons.append(b)

        # 진행 바(%) — 캡처 프로그램을 숨기지 않을 때 표시. 진행률에 따라 채워짐.
        self.prog_frame = ttk.Frame(self.root, padding=(14, 0, 14, 0))
        self.prog_var = tk.IntVar(value=0)
        self.progress = ttk.Progressbar(self.prog_frame, mode="determinate",
                                        maximum=100, variable=self.prog_var,
                                        style="Material.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True)
        self.pct_lbl = tk.Label(self.prog_frame, text="00%", width=5,
                                bg=MC["bg"], fg=MC["primary"], font=self.f_bold)
        # prog_frame 은 필요할 때만 pack (여기서는 숨김)

        # ===== 옵션 영역 =====
        optf = ttk.LabelFrame(self.root, text=self.t("lf_options"),
                              padding=(12, 6, 12, 8))
        optf.pack(fill="x", padx=14, pady=(4, 5))

        # 텍스트 옵션 2칸
        cols = ttk.Frame(optf)
        cols.pack(fill="x")
        col1 = ttk.Frame(cols)
        col1.pack(side="left", fill="both", expand=True, anchor="nw")
        col2 = ttk.Frame(cols)
        col2.pack(side="left", fill="both", expand=True, anchor="nw")

        # 1칸: 최상위 자동 스크롤 / 멀티 모니터 / 캡처 프로그램 숨기기
        ttk.Checkbutton(col1, text=self.t("opt_scroll_top"),
                        variable=self.opt_scroll_top,
                        command=self._save_cfg).pack(anchor="w")
        ttk.Checkbutton(col1, text=self.t("opt_multi_monitor"),
                        variable=self.opt_multi,
                        command=self._save_cfg).pack(anchor="w")
        ttk.Checkbutton(col1, text=self.t("opt_hide_app"),
                        variable=self.opt_hide,
                        command=self._save_cfg).pack(anchor="w")
        ttk.Checkbutton(col1, text=self.t("opt_sticky"),
                        variable=self.opt_sticky,
                        command=self._save_cfg).pack(anchor="w")
        ttk.Checkbutton(col1, text=self.t("opt_hide_mouse"),
                        variable=self.opt_hide_mouse,
                        command=self._save_cfg).pack(anchor="w")
        ttk.Checkbutton(col1, text=self.t("opt_auto_copy"),
                        variable=self.opt_auto_copy,
                        command=self._save_cfg).pack(anchor="w")

        # 2칸: 항상 위에 위치(맨 위) / 언어 / 단축키 매핑 / 업데이트 확인
        ttk.Checkbutton(col2, text=self.t("opt_on_top"),
                        variable=self.opt_on_top,
                        command=self._toggle_topmost).pack(anchor="w", pady=(0, 4))
        langf = ttk.Frame(col2)
        langf.pack(anchor="w", fill="x")
        ttk.Label(langf, text=self.t("lbl_language")).pack(side="left")
        self.lang_cb = ttk.Combobox(langf, width=10, state="readonly",
                                    values=[n for _, n in LANGS])
        cur_name = dict(LANGS).get(self.cfg.get("lang", "ko"), "한국어")
        self.lang_cb.set(cur_name)
        self.lang_cb.pack(side="left", padx=(6, 0))
        self.lang_cb.bind("<<ComboboxSelected>>", self._on_lang)
        # 단축키 매핑 버튼 (언어 밑)
        ttk.Button(col2, text=self.t("btn_hotkeys"), style="Secondary.TButton",
                   command=self._open_hotkeys).pack(anchor="w", pady=(6, 0))
        if GITHUB_REPO and updater is not None:
            ttk.Button(col2, text=self.t("btn_check_update"),
                       style="Secondary.TButton",
                       command=self._check_update_manual).pack(anchor="w", pady=(4, 0))

        # 스크롤 속도 (옵션 영역 내)
        spd = ttk.Frame(optf)
        spd.pack(fill="x", pady=(6, 0))
        ttk.Label(spd, text=self.t("lbl_speed")).pack(side="left")
        self.speed_lbl = ttk.Label(spd, width=4, style="Primary.TLabel")
        self.speed_lbl.pack(side="left", padx=(6, 6))
        ttk.Label(spd, text=self.t("speed_hint"),
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(spd, text=self.t("btn_reset"), style="Secondary.TButton",
                   command=self._reset_speed).pack(side="right")
        self.speed_scale = tk.Scale(
            optf, from_=0, to=len(SPEED_VALUES) - 1, orient="horizontal",
            showvalue=0, variable=self.speed_idx, command=self._on_speed,
            bg=MC["bg"], fg=MC["text"], troughcolor=MC["surface"],
            activebackground=MC["primary"], highlightthickness=0,
            bd=0, sliderrelief="flat")
        self.speed_scale.pack(fill="x", pady=(2, 0))
        self._update_speed_label()

        # 저장 폴더
        dirf = ttk.LabelFrame(self.root, text=self.t("lf_save_folder"),
                              padding=(12, 6, 12, 8))
        dirf.pack(fill="x", padx=14, pady=(4, 5))
        ttk.Entry(dirf, textvariable=self.save_dir).pack(
            side="left", fill="x", expand=True)
        ttk.Button(dirf, text=self.t("btn_change"), style="Secondary.TButton",
                   command=self.change_dir).pack(side="left", padx=(8, 0))
        ttk.Button(dirf, text=self.t("btn_open"), style="Secondary.TButton",
                   command=self.open_dir).pack(side="left", padx=(6, 0))

        # 상태
        if not self.status.get():
            self.status.set(self.t("status_ready"))
        ttk.Label(self.root, textvariable=self.status, style="Primary.TLabel",
                  wraplength=470).pack(fill="x", padx=14, pady=(0, 4))

        # 파일 목록
        listf = ttk.LabelFrame(self.root, text=self.t("lf_captured"),
                               padding=(10, 6, 10, 8))
        listf.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        head = ttk.Frame(listf)
        head.pack(fill="x")
        ttk.Button(head, text=self.t("btn_refresh"), style="Secondary.TButton",
                   command=self.refresh_list).pack(side="right")

        canvas = tk.Canvas(listf, borderwidth=0, highlightthickness=0,
                           bg=MC["bg"], height=295)   # 약 3.5개 항목이 보이는 높이
        vsb = ttk.Scrollbar(listf, orient="vertical", command=canvas.yview,
                            style="Thin.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.list_inner = tk.Frame(canvas, bg=MC["bg"])
        self._win = canvas.create_window((0, 0), window=self.list_inner, anchor="nw")
        self.list_inner.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(self._win, width=e.width))
        canvas.bind_all(
            "<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        self._list_canvas = canvas

        # 이미 발견된 업데이트가 있으면 배너 복원(언어 전환 후 등)
        if self._update_info and self._update_info.get("newer"):
            self._show_update_banner(self._update_info)

    # ---------- 옵션 콜백 ----------
    def _speed(self):
        i = max(0, min(len(SPEED_VALUES) - 1, self.speed_idx.get()))
        return SPEED_VALUES[i]

    def _update_speed_label(self):
        v = self._speed()
        txt = ("%gx" % v)
        self.speed_lbl.config(text=txt)

    def _on_speed(self, _=None):
        self._update_speed_label()
        self._save_cfg()

    def _reset_speed(self):
        self.speed_idx.set(SPEED_DEFAULT_IDX)
        self._update_speed_label()
        self._save_cfg()

    def _on_lang(self, _=None):
        name = self.lang_cb.get()
        code = next((c for c, n in LANGS if n == name), "ko")
        if code == self.cfg.get("lang"):
            return
        self.cfg["lang"] = code
        save_cfg(self.cfg)
        self.status.set(self.t("status_ready"))
        self._build_ui()          # 즉시 반영(재시작 불필요)
        self.refresh_list()

    def set_status(self, text):
        self.root.after(0, lambda: self.status.set(text))

    def change_dir(self):
        d = filedialog.askdirectory(initialdir=self.save_dir.get())
        if d:
            self.save_dir.set(d)
            self._save_cfg()
            self.refresh_list()

    def open_dir(self):
        d = self.save_dir.get()
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    # ---------- 캡처 ----------
    def _set_busy(self, busy):
        self.busy = busy
        st = "disabled" if busy else "normal"
        for b in self.cap_buttons:
            try:
                b.config(state=st)
            except Exception:
                pass

    def _show_progress(self, show):
        """진행 바 표시/숨김 (프로그램을 숨기지 않는 모드에서 사용)."""
        if show:
            self._set_progress(0)
            self.prog_frame.pack(fill="x", after=self.btn_bar)
            self.progress.pack(side="left", fill="x", expand=True)
            self.pct_lbl.pack(side="right", padx=(8, 0))
        else:
            self.prog_frame.pack_forget()

    def _set_progress(self, pct):
        pct = max(0, min(100, int(pct)))
        try:
            self.prog_var.set(pct)
            self.pct_lbl.config(text=f"{pct:02d}%")
        except Exception:
            pass

    def _on_progress(self, pct):
        self.root.after(0, lambda: self._set_progress(pct))

    def start_capture(self, mode):
        if self.busy:
            return
        self._hidden = self.opt_hide.get()
        if self._hidden:
            self.root.withdraw()
            self.root.update()
        time.sleep(0.15)

        # ---- 대상 영역 선택 ----
        region = None
        hwnd = None
        if mode in ("scroll_region", "shot_region"):
            region = RegionSelector(self.root, multi=self.opt_multi.get(),
                                    guide=self.t("guide_region")).result
        elif mode in ("scroll_display", "shot_display"):
            if self.opt_multi.get():
                region = MonitorPicker(self.root,
                                       guide=self.t("guide_monitor")).result
            else:
                region = primary_screen()
        elif mode in ("scroll_window", "shot_window"):
            wp = WindowPicker(self.root, guide=self.t("guide_window"))
            region, hwnd = wp.result, wp.hwnd
        elif mode == "shot_all":
            region = virtual_screen()

        if not region:
            if self._hidden:
                self.root.deiconify()
            self.status.set(self.t("status_cancelled"))
            return

        self._set_busy(True)

        # ---- 즉시 캡처(스크롤 없음) ----
        if mode.startswith("shot_"):
            self.status.set(self.t("status_capturing"))
            threading.Thread(target=self._worker_shot, args=(region, hwnd),
                             daemon=True).start()
            return

        # ---- 스크롤 캡처 ----
        self.status.set(self.t("status_preparing"))
        if not self._hidden:
            self._show_progress(True)   # 노출 모드: 진행 바 표시
        opts = {
            "auto_top": self.opt_scroll_top.get(),
            "handle_sticky": self.opt_sticky.get(),
            "hide_mouse": self.opt_hide_mouse.get(),
            "speed": self._speed(),
            "progress_cb": self._on_progress,
            "processing_cb": self._on_processing,
            "strings": {k: self.t(k) for k in (
                "status_moving_top", "status_detecting", "status_capturing",
                "status_capturing_fmt", "err_small_region", "err_no_scroll")},
        }
        threading.Thread(target=self._worker, args=(region, opts),
                         daemon=True).start()

    def _worker(self, region, opts):
        try:
            time.sleep(0.2)
            img = capture_scrolling_region(region, self.set_status, opts)
            path = save_png(img, self.save_dir.get())
            self.root.after(0, lambda: self._done(path))
        except Exception as e:
            m = str(e)
            self.root.after(0, lambda: self._error(m))

    def _worker_shot(self, region, hwnd=None):
        """스크롤 없이 현재 화면을 한 번만 캡처(디스플레이/윈도우/영역/전체)."""
        try:
            time.sleep(0.2)
            if hwnd:                       # 윈도우 캡처: 대상 창을 앞으로
                ensure_foreground(hwnd)
                time.sleep(0.35)
            with mss.mss() as sct:
                img = grab(sct, region)
            path = save_png(img, self.save_dir.get())
            self.root.after(0, lambda: self._done(path))
        except Exception as e:
            m = str(e)
            self.root.after(0, lambda: self._error(m))

    # 캡처 종료(그랩 끝) → 합성/저장 중 '진행중' 오버레이 표시
    def _on_processing(self):
        self.root.after(0, self._show_busy)

    def _show_busy(self):
        if getattr(self, "_busy_overlay", None) is None:
            try:
                self._busy_overlay = BusyOverlay(self.root, self.t("please_wait"))
            except Exception:
                self._busy_overlay = None

    def _close_busy(self):
        ov = getattr(self, "_busy_overlay", None)
        if ov is not None:
            ov.close()
            self._busy_overlay = None

    def _done(self, path):
        self._close_busy()
        self._set_busy(False)
        if getattr(self, "_hidden", True):
            self.root.deiconify()
        else:
            # 노출 모드: 진행 바를 100%로 채우고 잠시 후 숨김
            self._set_progress(100)
            self.root.after(3000, lambda: self._show_progress(False))
        done = self.t("status_done_fmt", name=os.path.basename(path))
        # 캡처 완료 시 자동으로 클립보드에 복사(옵션)
        if self.opt_auto_copy.get() and copy_image_to_clipboard(path):
            done = f"{done}  ·  {self.t('copied_status')}"
        self.status.set(done)
        self.refresh_list()

    def _error(self, msg):
        self._close_busy()
        self._set_busy(False)
        if getattr(self, "_hidden", True):
            self.root.deiconify()
        else:
            self._show_progress(False)
        self.status.set(self.t("status_error"))
        messagebox.showwarning(self.t("info_title"), msg)

    # ---------- 파일 목록 ----------
    def refresh_list(self):
        for w in self.list_inner.winfo_children():
            w.destroy()
        self._thumbs.clear()
        d = self.save_dir.get()
        if not os.path.isdir(d):
            tk.Label(self.list_inner, text=self.t("folder_missing"),
                     bg=MC["bg"], fg=MC["muted"], font=self.f_text).pack(
                         anchor="w", padx=6, pady=6)
            return
        files = [os.path.join(d, f) for f in os.listdir(d)
                 if f.lower().endswith(".png")]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        if not files:
            tk.Label(self.list_inner, text=self.t("no_files"),
                     bg=MC["bg"], fg=MC["muted"], font=self.f_text).pack(
                         anchor="w", padx=6, pady=6)
            return
        for path in files[:60]:
            self._add_row(path)

    def _add_row(self, path):
        C = MC
        row = tk.Frame(self.list_inner, bg=C["white"])
        row.pack(fill="x")
        inner = tk.Frame(row, bg=C["white"])
        inner.pack(fill="x", padx=4, pady=4)
        try:
            im = Image.open(path)
            im.thumbnail((120, 80))
            photo = ImageTk.PhotoImage(im)
            self._thumbs.append(photo)
            thumb = tk.Label(inner, image=photo, bg=C["white"])
        except Exception:
            thumb = tk.Label(inner, text="[img]", width=12, bg=C["white"],
                             fg=C["muted"])
        thumb.pack(side="left")
        try:
            with Image.open(path) as im2:
                w, h = im2.size
        except Exception:
            w = h = 0
        mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime(
            "%Y-%m-%d %H:%M:%S")
        info = tk.Frame(inner, bg=C["white"])
        info.pack(side="left", fill="x", expand=True, padx=10)
        l1 = tk.Label(info, text=os.path.basename(path), bg=C["white"],
                      fg=C["text"], font=self.f_bold, anchor="w")
        l1.pack(anchor="w")
        l2 = tk.Label(info, text=f"{w} x {h} px", bg=C["white"],
                      fg=C["muted"], font=self.f_cap, anchor="w")
        l2.pack(anchor="w")
        l3 = tk.Label(info, text=mtime, bg=C["white"], fg=C["muted"],
                      font=self.f_cap, anchor="w")
        l3.pack(anchor="w")

        # 열기 / 복사 버튼(세로 배치)
        btns = tk.Frame(inner, bg=C["white"])
        btns.pack(side="right")
        ttk.Button(btns, text=self.t("btn_open_item"), style="Secondary.TButton",
                   command=lambda p=path: os.startfile(p)).pack(anchor="e")
        copy_btn = ttk.Button(btns, text=self.t("btn_copy"),
                              style="Secondary.TButton")
        copy_btn.config(command=lambda p=path, b=copy_btn: self._copy_image(p, b))
        copy_btn.pack(anchor="e", pady=(4, 0))

        # 하단 구분선
        tk.Frame(self.list_inner, bg=C["border"], height=1).pack(fill="x")

        # Hover(연블루) — 항목의 tk 위젯들 배경 전환
        hover_widgets = [row, inner, thumb, info, l1, l2, l3, btns]

        def set_bg(color):
            for wd in hover_widgets:
                try:
                    wd.config(bg=color)
                except Exception:
                    pass

        for wd in (row, inner, thumb, info, l1, l2, l3):
            wd.bind("<Enter>", lambda e: set_bg(C["list_hover"]))
            wd.bind("<Leave>", lambda e: set_bg(C["white"]))
            wd.bind("<Double-Button-1>", lambda e, p=path: os.startfile(p))

    def _copy_image(self, path, btn):
        ok = copy_image_to_clipboard(path)
        if ok:
            btn.config(text=self.t("copied_mark"))
            self.root.after(3000, lambda: self._restore_copy_btn(btn))
        else:
            messagebox.showwarning(self.t("info_title"), self.t("copy_fail"))

    def _restore_copy_btn(self, btn):
        try:
            btn.config(text=self.t("btn_copy"))
        except Exception:
            pass

    # ---------- 업데이트 확인 ----------
    def _auto_check_update(self):
        """시작 시 백그라운드로 조용히 확인 + 주기적 재확인."""
        if not (GITHUB_REPO and updater is not None):
            return

        def work():
            info = updater.check_latest(GITHUB_REPO, APP_VERSION)
            if info and info.get("newer"):
                self.root.after(0, lambda: self._on_update_found(info))

        threading.Thread(target=work, daemon=True).start()
        # 6시간마다 재확인
        self.root.after(6 * 3600 * 1000, self._auto_check_update)

    def _on_update_found(self, info):
        self._update_info = info
        self._show_update_banner(info)

    def _show_update_banner(self, info):
        try:
            ver = info.get("tag") or ".".join(map(str, info.get("version", ())))
            self.update_lbl.config(text=self.t("update_avail_fmt", ver=ver))
            self.update_bar.pack(fill="x", before=self.btn_bar)
        except Exception:
            pass

    def _open_update(self):
        info = self._update_info or {}
        url = info.get("download_url") or info.get("html_url")
        if url:
            webbrowser.open(url)

    def _check_update_manual(self):
        if not (GITHUB_REPO and updater is not None):
            return

        def work():
            info = updater.check_latest(GITHUB_REPO, APP_VERSION)
            self.root.after(0, lambda: self._manual_result(info))

        threading.Thread(target=work, daemon=True).start()

    def _manual_result(self, info):
        if info is None:
            messagebox.showwarning(self.t("info_title"), self.t("update_fail"))
        elif info.get("newer"):
            self._on_update_found(info)
            ver = info.get("tag") or ".".join(map(str, info.get("version", ())))
            messagebox.showinfo(self.t("info_title"),
                                self.t("update_avail_fmt", ver=ver))
        else:
            messagebox.showinfo(self.t("info_title"), self.t("update_latest"))

    def run(self):
        self.root.mainloop()


def copy_image_to_clipboard(path):
    """PNG 파일을 클립보드에 이미지(CF_DIB)로 복사."""
    try:
        img = Image.open(path).convert("RGB")
        out = io.BytesIO()
        img.save(out, "BMP")
        data = out.getvalue()[14:]  # BMP 파일 헤더(14바이트) 제거 → DIB
        out.close()
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    App().run()
