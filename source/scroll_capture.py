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
import collections
import webbrowser
import ctypes
from datetime import datetime

import numpy as np
import cv2

IS_MAC = sys.platform == "darwin"
if IS_MAC:
    import contextlib
    import platform_mac as pm
else:
    import mss
    import win32api
    import win32con
    import win32gui
    import win32process
    import win32clipboard
    pm = None

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import tkinter.font as tkfont
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
def default_save_dir():
    if IS_MAC:   # .app 번들 내부는 쓰기 불가 → 사진 폴더
        return os.path.join(os.path.expanduser("~"), "Pictures", "ScrollCapture")
    return os.path.join(app_base_dir(), "capture")


def open_path(path):
    """파일/폴더를 기본 앱으로 열기."""
    if IS_MAC:
        pm.open_path(path)
    else:
        os.startfile(path)


def config_path():
    if IS_MAC:
        base = os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support")
    else:
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
    "split_enabled": False,  # 높이 분할 자르기
    "split_height": 4000,    # 분할 높이(px)
    "speed_idx": 1,          # 1x
    "save_dir": "",
    "hotkeys": {},           # {mode: {"mods":int, "vk":int, "text":str}} (비면 기본값)
    "options_expanded": False,  # 옵션 영역 더보기 펼침 상태
    "panel_pos": None,       # 캡처 중 떠 있는 패널 위치 [x, y] (끌어서 옮긴 위치 기억)
}

# 전역 단축키 기본값: mode -> (modifiers, virtual-key, 표시문자열)
#   MOD_ALT=0x0001, MOD_CONTROL=0x0002, MOD_SHIFT=0x0004, MOD_WIN=0x0008
ALT_SHIFT = 0x0001 | 0x0004
DEFAULT_HOTKEYS = {
    "shot_region":    (ALT_SHIFT, 0x31, "Alt + Shift + 1"),
    "shot_window":    (ALT_SHIFT, 0x32, "Alt + Shift + 2"),
    "shot_display":   (ALT_SHIFT, 0x33, "Alt + Shift + 3"),
    "shot_all":       (ALT_SHIFT, 0x34, "Alt + Shift + 4"),
    "scroll_region":  (ALT_SHIFT, 0x35, "Alt + Shift + 5"),
    "scroll_window":  (ALT_SHIFT, 0x36, "Alt + Shift + 6"),
    "scroll_display": (ALT_SHIFT, 0x37, "Alt + Shift + 7"),
}
if IS_MAC:
    # macOS: Option + Shift + 1~7  (Carbon 수정자 / macOS 가상 키코드)
    _OPT_SHIFT = pm.MOD_OPTION | pm.MOD_SHIFT
    DEFAULT_HOTKEYS = {
        "shot_region":    (_OPT_SHIFT, 18, "Option + Shift + 1"),
        "shot_window":    (_OPT_SHIFT, 19, "Option + Shift + 2"),
        "shot_display":   (_OPT_SHIFT, 20, "Option + Shift + 3"),
        "shot_all":       (_OPT_SHIFT, 21, "Option + Shift + 4"),
        "scroll_region":  (_OPT_SHIFT, 23, "Option + Shift + 5"),
        "scroll_window":  (_OPT_SHIFT, 22, "Option + Shift + 6"),
        "scroll_display": (_OPT_SHIFT, 26, "Option + Shift + 7"),
    }
HOTKEY_MODES = ["shot_region", "shot_window", "shot_display", "shot_all",
                "scroll_region", "scroll_window", "scroll_display"]

# 캡처 중 전역 '취소' 키(Esc): (수정자 없음, Windows VK_ESCAPE / macOS 키코드 53)
CANCEL_MODE = "__cancel"
ESC_HOTKEY = (0, 53) if IS_MAC else (0, 0x1B)
# 자체 점검(CI)용: 실제 키 입력(F19)이 단축키로 전달되는지 확인
SELFTEST_MODE = "__selftest"
SELFTEST_HOTKEY = (0, 80) if IS_MAC else (0, 0x82)

# 오버레이/툴팁 등 고정 폰트
UI_FONT = "Apple SD Gothic Neo" if IS_MAC else "맑은 고딕"


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
    if IS_MAC:
        return pm.primary_screen()
    return (0, 0, win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1))


def virtual_screen():
    if IS_MAC:
        return pm.virtual_screen()
    return (win32api.GetSystemMetrics(76), win32api.GetSystemMetrics(77),
            win32api.GetSystemMetrics(78), win32api.GetSystemMetrics(79))


def get_monitors():
    """각 모니터의 (x, y, w, h) 목록."""
    if IS_MAC:
        return pm.get_monitors()
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
    if IS_MAC:
        return pm.get_window_rect(hwnd)
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
CAPTURE_ICON_MODES = ["shot_region", "shot_window", "shot_display", "shot_all",
                      "scroll_region", "scroll_window", "scroll_display"]


def make_capture_icons(target_h=56):
    """제공된 SVG를 렌더한 assets/cap_{mode}.png 를 '변형 없이' 로드.
    원본 가로세로 비율(240:190)을 유지한 채 높이 target_h 로만 맞춰 축소한다."""
    icons = {}
    for m in CAPTURE_ICON_MODES:
        try:
            p = resource_path(os.path.join("assets", f"cap_{m}.png"))
            im = Image.open(p).convert("RGBA")
            w, h = im.size
            nw = max(1, round(target_h * w / h))     # 비율 유지
            im = im.resize((nw, target_h), Image.LANCZOS)
            icons[m] = ImageTk.PhotoImage(im)
        except Exception:
            icons[m] = ImageTk.PhotoImage(
                Image.new("RGBA", (target_h, target_h), (0, 0, 0, 0)))
    return icons


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
        "opt_split": "높이 분할 자르기",
        "lbl_split_height": "높이(px)",
        "lf_options": "옵션",
        "btn_more": "더보기",
        "btn_less": "숨기기",
        "opt_on_top": "항상 위에 위치",
        "speed_hint": "빠를수록 정확도가 떨어질 수 있어요",
        "lbl_speed": "스크롤 속도",
        "btn_reset": "초기화",
        "btn_reset_all": "기본값 복원",
        "confirm_reset": "모든 옵션(체크박스·스크롤 속도·단축키·언어)을 기본값으로 되돌릴까요?",
        "reset_done": "기본값으로 초기화했습니다.",
        "lbl_language": "언어",
        "lf_save_folder": "캡처 저장 폴더",
        "btn_change": "변경",
        "btn_open": "열기",
        "lf_captured": "캡처한 파일",
        "btn_refresh": "새로고침",
        "btn_open_item": "열기",
        "btn_copy": "복사",
        "btn_delete": "삭제",
        "confirm_delete_fmt": "정말 삭제하시겠습니까?\n{name}",
        "delete_fail": "파일 삭제에 실패했습니다.",
        "copied_mark": "✓",
        "status_ready": "준비됨 — [캡처 시작]을 누르세요",
        "status_preparing": "캡처 준비 중...",
        "status_moving_top": "상단으로 이동 중...",
        "status_detecting": "고정 영역 감지 중...",
        "status_capturing": "캡처 중...",
        "status_capturing_fmt": "캡처 중... (높이 {h}px)",
        "status_done_fmt": "완료: {name}",
        "status_done_split_fmt": "완료: {n}개 파일로 분할 저장됨",
        "status_cancelled": "취소되었습니다",
        "panel_title": "스크롤 캡처 중",
        "btn_stop": "■ 중지",
        "btn_cancel_cap": "취소",
        "panel_hint": "중지 = 여기까지 저장 · 취소(Esc) = 저장 안 함",
        "status_stopping": "중지하는 중... 여기까지 저장합니다",
        "status_cancelling": "취소하는 중...",
        "status_stopped_early": "캡처를 시작하기 전에 중지되어 저장할 내용이 없습니다",
        "status_stopped_fmt": "중지 — 여기까지 저장했습니다: {name}",
        "status_stopped_split_fmt": "중지 — 여기까지 {n}개 파일로 나눠 저장했습니다",
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
        "err_no_scroll_mac": ("스크롤이 되지 않아 한 화면만 캡처되었습니다.\n"
                              "· 대상 영역(스크롤되는 부분)을 정확히 지정했는지\n"
                              "· 시스템 설정 > 개인정보 보호 및 보안 > '손쉬운 사용'에\n"
                              "  이 앱이 허용되어 있는지 확인하세요."),
        "mac_perm_msg": ("캡처하려면 macOS 권한이 필요합니다.\n\n"
                         "시스템 설정 > 개인정보 보호 및 보안에서\n"
                         "  · 화면 기록\n  · 손쉬운 사용\n"
                         "항목에 'ScrollCapture'를 허용한 뒤 앱을 다시 실행해 주세요."),
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
        "opt_split": "Split by height",
        "lbl_split_height": "Height(px)",
        "lf_options": "Options",
        "btn_more": "More",
        "btn_less": "Less",
        "opt_on_top": "Always on top",
        "speed_hint": "Faster may reduce accuracy",
        "lbl_speed": "Scroll speed",
        "btn_reset": "Reset",
        "btn_reset_all": "Restore Defaults",
        "confirm_reset": "Reset all options (checkboxes, speed, hotkeys, language) to defaults?",
        "reset_done": "Options reset to defaults.",
        "lbl_language": "Language",
        "lf_save_folder": "Save folder",
        "btn_change": "Change",
        "btn_open": "Open",
        "lf_captured": "Captured files",
        "btn_refresh": "Refresh",
        "btn_open_item": "Open",
        "btn_copy": "Copy",
        "btn_delete": "Delete",
        "confirm_delete_fmt": "Are you sure you want to delete?\n{name}",
        "delete_fail": "Failed to delete the file.",
        "copied_mark": "✓",
        "status_ready": "Ready — press [Capture]",
        "status_preparing": "Preparing...",
        "status_moving_top": "Moving to top...",
        "status_detecting": "Detecting fixed areas...",
        "status_capturing": "Capturing...",
        "status_capturing_fmt": "Capturing... (height {h}px)",
        "status_done_fmt": "Done: {name}",
        "status_done_split_fmt": "Done: split into {n} files",
        "status_cancelled": "Cancelled",
        "panel_title": "Scroll capturing",
        "btn_stop": "■ Stop",
        "btn_cancel_cap": "Cancel",
        "panel_hint": "Stop = save up to here · Cancel (Esc) = discard",
        "status_stopping": "Stopping... saving up to here",
        "status_cancelling": "Cancelling...",
        "status_stopped_early": "Stopped before capturing began — nothing to save",
        "status_stopped_fmt": "Stopped — saved up to here: {name}",
        "status_stopped_split_fmt": "Stopped — saved up to here as {n} files",
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
        "err_no_scroll_mac": ("Only one screen was captured because scrolling did not work.\n"
                              "· Check that you selected the actual scrolling area\n"
                              "· Allow this app in System Settings > Privacy & Security\n"
                              "  > Accessibility."),
        "mac_perm_msg": ("macOS permissions are required to capture.\n\n"
                         "In System Settings > Privacy & Security, allow 'ScrollCapture' in\n"
                         "  · Screen Recording\n  · Accessibility\n"
                         "then restart the app."),
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
        "opt_split": "高さで分割",
        "lbl_split_height": "高さ(px)",
        "lf_options": "オプション",
        "btn_more": "もっと見る",
        "btn_less": "閉じる",
        "opt_on_top": "常に手前に表示",
        "speed_hint": "速いほど精度が下がる場合があります",
        "lbl_speed": "スクロール速度",
        "btn_reset": "リセット",
        "btn_reset_all": "既定に戻す",
        "confirm_reset": "すべてのオプション(チェックボックス・スクロール速度・ショートカット・言語)を既定に戻しますか？",
        "reset_done": "既定に戻しました。",
        "lbl_language": "言語",
        "lf_save_folder": "保存フォルダ",
        "btn_change": "変更",
        "btn_open": "開く",
        "lf_captured": "キャプチャしたファイル",
        "btn_refresh": "更新",
        "btn_open_item": "開く",
        "btn_copy": "コピー",
        "btn_delete": "削除",
        "confirm_delete_fmt": "本当に削除しますか？\n{name}",
        "delete_fail": "ファイルの削除に失敗しました。",
        "copied_mark": "✓",
        "status_ready": "準備完了 — [キャプチャ開始] を押してください",
        "status_preparing": "準備中...",
        "status_moving_top": "上へ移動中...",
        "status_detecting": "固定領域を検出中...",
        "status_capturing": "キャプチャ中...",
        "status_capturing_fmt": "キャプチャ中... (高さ {h}px)",
        "status_done_fmt": "完了: {name}",
        "status_done_split_fmt": "完了: {n}個のファイルに分割保存されました",
        "status_cancelled": "キャンセルされました",
        "panel_title": "スクロールキャプチャ中",
        "btn_stop": "■ 停止",
        "btn_cancel_cap": "キャンセル",
        "panel_hint": "停止 = ここまで保存 · キャンセル(Esc) = 保存しない",
        "status_stopping": "停止中... ここまで保存します",
        "status_cancelling": "キャンセル中...",
        "status_stopped_early": "キャプチャ開始前に停止したため、保存する内容がありません",
        "status_stopped_fmt": "停止 — ここまで保存しました: {name}",
        "status_stopped_split_fmt": "停止 — ここまで {n} 個のファイルに分けて保存しました",
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
        "err_no_scroll_mac": ("スクロールできず1画面のみキャプチャされました。\n"
                              "· スクロールする範囲を正しく指定したか\n"
                              "· システム設定 > プライバシーとセキュリティ > アクセシビリティで\n"
                              "  このアプリが許可されているか確認してください。"),
        "mac_perm_msg": ("キャプチャには macOS の権限が必要です。\n\n"
                         "システム設定 > プライバシーとセキュリティで\n"
                         "  · 画面収録\n  · アクセシビリティ\n"
                         "に 'ScrollCapture' を許可し、アプリを再起動してください。"),
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
        "opt_split": "按高度分割裁切",
        "lbl_split_height": "高度(px)",
        "lf_options": "选项",
        "btn_more": "更多",
        "btn_less": "收起",
        "opt_on_top": "始终置顶",
        "speed_hint": "速度越快，精度可能越低",
        "lbl_speed": "滚动速度",
        "btn_reset": "重置",
        "btn_reset_all": "恢复默认",
        "confirm_reset": "将所有选项(复选框·滚动速度·快捷键·语言)恢复为默认值吗？",
        "reset_done": "已恢复为默认设置。",
        "lbl_language": "语言",
        "lf_save_folder": "保存文件夹",
        "btn_change": "更改",
        "btn_open": "打开",
        "lf_captured": "已截图的文件",
        "btn_refresh": "刷新",
        "btn_open_item": "打开",
        "btn_copy": "复制",
        "btn_delete": "删除",
        "confirm_delete_fmt": "确定要删除吗？\n{name}",
        "delete_fail": "删除文件失败。",
        "copied_mark": "✓",
        "status_ready": "就绪 — 请点击 [开始截图]",
        "status_preparing": "准备中...",
        "status_moving_top": "正在移到顶部...",
        "status_detecting": "正在检测固定区域...",
        "status_capturing": "正在截图...",
        "status_capturing_fmt": "正在截图... (高度 {h}px)",
        "status_done_fmt": "完成: {name}",
        "status_done_split_fmt": "完成: 已分割保存为 {n} 个文件",
        "status_cancelled": "已取消",
        "panel_title": "正在滚动截图",
        "btn_stop": "■ 停止",
        "btn_cancel_cap": "取消",
        "panel_hint": "停止 = 保存到此处 · 取消(Esc) = 不保存",
        "status_stopping": "正在停止... 保存到此处",
        "status_cancelling": "正在取消...",
        "status_stopped_early": "在开始截图前已停止，没有可保存的内容",
        "status_stopped_fmt": "已停止 — 已保存到此处: {name}",
        "status_stopped_split_fmt": "已停止 — 已分成 {n} 个文件保存到此处",
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
        "err_no_scroll_mac": ("由于无法滚动，只截取了一屏。\n"
                              "· 请确认已正确选择可滚动的区域\n"
                              "· 请在 系统设置 > 隐私与安全性 > 辅助功能 中\n"
                              "  允许本应用。"),
        "mac_perm_msg": ("截图需要 macOS 权限。\n\n"
                         "请在 系统设置 > 隐私与安全性 中为 'ScrollCapture' 允许\n"
                         "  · 屏幕录制\n  · 辅助功能\n"
                         "然后重新启动应用。"),
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
if not IS_MAC:
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
METHOD_STATS = {}   # 실제로 화면을 움직인 스크롤 방식 횟수(진단용)


def _count_method(m):
    METHOD_STATS[m] = METHOD_STATS.get(m, 0) + 1


# ==================================================================
# 창 활성화 / 스크롤 저수준 함수
# ==================================================================
def ensure_foreground(hwnd):
    """대상 창을 확실하게 앞으로 가져온다(AttachThreadInput 우회 포함)."""
    if not hwnd:
        return
    if IS_MAC:
        pm.ensure_foreground(hwnd)
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
    """최후 수단: 실제 마우스 휠 입력. 포인터를 잠깐 옮겼다가 '즉시' 원위치
    (스크롤 중에도 사용자가 마우스를 자유롭게 쓸 수 있도록)."""
    ensure_foreground(hwnd)
    home = win32api.GetCursorPos()
    try:
        win32api.SetCursorPos((cx, cy))
        time.sleep(0.03)
        step = WHEEL_DELTA if notches > 0 else -WHEEL_DELTA
        for _ in range(abs(notches)):
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, step, 0)
            time.sleep(0.02)
        time.sleep(0.04)
    finally:
        win32api.SetCursorPos(home)


def _own_window(h):
    try:
        return bool(h) and win32process.GetWindowThreadProcessId(h)[1] == os.getpid()
    except Exception:
        return False


def _sendmsg_wheel(cx, cy, notches, hwnd=None):
    """커서 위치의 창에 WM_MOUSEWHEEL 메시지를 직접 전달."""
    try:
        child = win32gui.WindowFromPoint((cx, cy))
        if _own_window(child) and hwnd:     # 떠 있는 패널 등 이 앱의 창이면 대상 창으로
            child = hwnd
        delta = (WHEEL_DELTA if notches > 0 else -WHEEL_DELTA) * abs(notches)
        wparam = (delta & 0xFFFF) << 16
        lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
        win32gui.SendMessage(child, win32con.WM_MOUSEWHEEL, wparam, lparam)
    except Exception:
        pass


def _key(hwnd, cx, cy, down, notches=1):
    """방향키(↓/↑) 입력 — 커서를 옮기지 않음.
    (PageDown 은 한 번에 화면 대부분을 넘겨 이어 붙이기가 깨지므로 작은 단위로)"""
    ensure_foreground(hwnd)
    key = win32con.VK_DOWN if down else win32con.VK_UP
    for _ in range(min(12, max(1, abs(int(notches)) * 2))):
        # 키 입력은 '앞에 있는 창'으로 가므로, 사용자가 다른 창을 눌렀으면 보내지 않음
        try:
            fg = ctypes.windll.user32.GetAncestor(win32gui.GetForegroundWindow(), GA_ROOT)
            if hwnd and fg != hwnd:
                return
        except Exception:
            pass
        win32api.keybd_event(key, 0, 0, 0)
        time.sleep(0.015)
        win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.015)


def _foreign_window_at(x, y):
    """(x, y) 아래의 '다른 프로세스' 최상위 창 (Z 순서 위부터). 없으면 None."""
    found = []

    def cb(h, _):
        if found:
            return True
        try:
            if (win32gui.IsWindowVisible(h) and not win32gui.IsIconic(h)
                    and not _own_window(h)):
                l, t, r, b = win32gui.GetWindowRect(h)
                if l <= x < r and t <= y < b and (r - l) > 40 and (b - t) > 40:
                    found.append(h)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return found[0] if found else None


def apply_scroll(hwnd, cx, cy, method, down, notches, park=None):
    if IS_MAC:   # 포인터를 옮기지 않는 이벤트 전달(send) 우선
        pm.apply_scroll(hwnd, cx, cy, method, down, notches)
        return
    if method == "wheel":
        _wheel(hwnd, cx, cy, -notches if down else notches)
    elif method == "send":
        _sendmsg_wheel(cx, cy, -notches if down else notches, hwnd)
    else:
        _key(hwnd, cx, cy, down, notches)
    # 캡처 직전 커서를 콘텐츠 밖으로 이동 → 마우스오버(hover) 배경색 변화 방지
    if park is not None:
        try:
            win32api.SetCursorPos(park)
        except Exception:
            pass


# ==================================================================
# 캡처 유틸
# ==================================================================
def screen_grabber():
    """캡처 세션(Windows: mss). macOS 는 Quartz 를 직접 사용하므로 빈 컨텍스트."""
    if IS_MAC:
        return contextlib.nullcontext(None)
    return mss.mss()


def grab(sct, region):
    x, y, w, h = region
    if IS_MAC:   # Retina 는 실제 픽셀(2배) 해상도로 반환
        return pm.grab_region(x, y, w, h)
    shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    img = np.array(shot)  # BGRA
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def freeze_screen():
    """지금 이 순간의 전체 화면(모든 모니터)을 한 장으로 고정.
    오버레이가 뜨면 마우스 오버 메뉴가 닫히고 영상은 계속 재생되므로,
    캡처를 누른 '그 순간'을 먼저 찍어 두고 이후 선택·저장에 사용한다."""
    vx, vy, vw, vh = virtual_screen()
    with screen_grabber() as sct:
        img = grab(sct, (vx, vy, vw, vh))
    sc = max(1, int(round(img.shape[1] / float(vw))))
    return {"org": (vx, vy), "sc": sc, "img": img}


def crop_frozen(frozen, region):
    """정지 화면에서 화면 좌표 영역(region)을 잘라낸다(Retina 배율 반영)."""
    vx, vy = frozen["org"]
    sc = frozen["sc"]
    img = frozen["img"]
    x, y, w, h = region
    x0 = max(0, int(round((x - vx) * sc)))
    y0 = max(0, int(round((y - vy) * sc)))
    x1 = min(img.shape[1], int(round((x - vx + w) * sc)))
    y1 = min(img.shape[0], int(round((y - vy + h) * sc)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise RuntimeError("region outside frozen screen")
    return np.ascontiguousarray(img[y0:y1, x0:x1])


def set_main_capture_excluded(root, excluded):
    """앱 메인 창을 화면 캡처에서만 제외/복원(창을 숨기는 지연 없이 바로 찍기 위해)."""
    if IS_MAC:
        pm.set_capture_excluded(excluded)
        return
    try:
        u = ctypes.windll.user32
        hwnd = u.GetAncestor(root.winfo_id(), GA_ROOT)
        u.SetWindowDisplayAffinity(hwnd, 0x11 if excluded else 0)
    except Exception:
        pass


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
    maxd = max(4, int(H * max_frac))

    def match(ty, th):
        if ty + th >= H - 2:
            th = H - 2 - ty
        if th < 20:
            return 0, 0.0
        templ = cg[ty:ty + th, mx:W - mx]
        y1 = min(H, ty + maxd + th)
        field = pg[ty:y1, mx:W - mx]   # ty에서 시작 → field 내 y좌표가 곧 delta
        if field.shape[0] < templ.shape[0] + 1 or templ.shape[1] > field.shape[1]:
            return 0, 0.0
        res = cv2.matchTemplate(field, templ, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        return maxloc[1], float(maxv)

    # 위쪽 기준(기본): 키 큰 템플릿으로 반복 요소 오매칭 방지
    d1, s1 = match(max(4, H // 20), max(80, H // 3))
    # 중간 기준: 위쪽에 고정(sticky) 메뉴가 막 나타나 위쪽 기준이 흐려질 때 대비
    d2, s2 = match(int(H * 0.3), max(60, H // 4))
    if s2 > s1 + 0.15 and d2 > 2:
        return d2, s2
    return d1, s1


def mostly_same(a, b, frac=0.15, row_thr=2.5):
    """바뀐 행이 전체의 frac 미만이면 '같은 화면'(스크롤되지 않음)으로 본다.
    스크롤되면 내용이 위로 밀려 대부분의 행이 바뀌지만, 떠 있는 '맨 위로' 버튼이나
    마우스 오버 효과는 일부 행만 바꾼다 → 페이지 끝에서 같은 화면이 중복으로 붙는 것 방지."""
    if a.shape != b.shape:
        return False
    rd = np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16)), axis=(1, 2))
    return float((rd > row_thr).mean()) < frac


def images_equal(a, b, thresh=1.2):
    if a.shape != b.shape:
        return False
    return float(np.mean(cv2.absdiff(a, b))) < thresh


# ==================================================================
# 캡처 중지 / 취소
# ==================================================================
class CaptureCancelled(Exception):
    """[취소] 또는 Esc — 결과를 저장하지 않고 끝낸다."""


class CaptureControl:
    """캡처 스레드와 UI 사이의 중지/취소 신호.
    stop  : 지금까지 캡처한 부분까지 저장하고 끝냄 ([중지])
    cancel: 저장하지 않고 끝냄 ([취소] / Esc)"""

    def __init__(self):
        self.stop = threading.Event()
        self.cancel = threading.Event()
        self.stopped = False        # 실제로 '중지'로 끝났는지(완료 메시지용)


_CTL = None   # 현재 진행 중인 캡처의 제어 객체(캡처 스레드 1개만 동작)


def _check_abort():
    """준비 단계(맨 위 이동/고정 영역 감지) 중의 중지·취소는 모두 '취소'로 처리
    (아직 이어붙인 결과가 없으므로 저장할 것이 없음)."""
    c = _CTL
    if c is not None and (c.cancel.is_set() or c.stop.is_set()):
        raise CaptureCancelled()


# ==================================================================
# 캡처 엔진
# ==================================================================
# 포인터를 (잠깐이라도) 움직이는 스크롤 방식 — 다른 방식이 되면 쓰지 않는다
#  hid/sess: macOS 에서 위치 지정 휠도 포인터를 그 위치로 옮김(CI 실측)
POINTER_MOVING = {"hid", "sess", "wheel"}


def probe_methods(sct, region, hwnd, cx, cy, notches, wait):
    """캡처 시작 시 실제로 동작하는 스크롤 방식을 먼저 찾는다.
    - 포인터를 옮기지 않는 방식(send/hid/key)이 동작하면 이번 캡처에서는 휠(wheel:
      포인터를 잠깐 옮김)을 아예 쓰지 않는다 → 맨 위/맨 끝 확인 중에도 포인터가 안 움직임
    - (macOS) '아래로'를 보냈는데 위로 가면 스크롤 방향을 뒤집는다(방향 반전 유틸 대응)
    시험으로 움직인 만큼은 되돌려 놓는다."""
    free = [m for m in METHODS if m not in POINTER_MOVING]
    moving = [m for m in METHODS if m in POINTER_MOVING]
    tries = [(m, d) for m in free + moving for d in (True, False)]
    for m, down in tries:
        _check_abort()
        a = grab(sct, region)
        apply_scroll(hwnd, cx, cy, m, down=down, notches=notches)
        time.sleep(wait)
        b = grab(sct, region)
        if images_equal(a, b) or mostly_same(a, b):
            continue
        went_down = _moved_down(a, b)
        went_up = (not went_down) and _moved_down(b, a)
        if IS_MAC and m != "key" and ((down and went_up) or (not down and went_down)):
            pm.SCROLL_SIGN[0] *= -1             # 반대로 움직임 → 방향 보정
            back = down                          # 보정된 방향으로 같은 명령 = 되돌리기
        else:
            back = not down
        apply_scroll(hwnd, cx, cy, m, down=back, notches=notches)
        time.sleep(wait)
        METHODS.remove(m)
        METHODS.insert(0, m)
        if m not in POINTER_MOVING:              # 포인터를 옮기는 방식은 이번엔 사용 안 함
            METHODS[:] = [x for x in METHODS if x not in POINTER_MOVING]
        return m
    return None


def _moved_down(a, b):
    """a → b 사이에 페이지가 아래로 스크롤됐는지(내용이 위로 밀림)."""
    h = a.shape[0]
    d, score = find_delta(a, b, max_frac=0.6)
    return 2 < d <= h and score >= 0.5


def scroll_to_top(sct, region, hwnd, cx, cy, notches, wait, park=None):
    """대상을 맨 위로 스크롤. 마지막으로 성공한 방식 인덱스를 반환."""
    prev = grab(sct, region)
    pref = 0
    stagnant = 0
    for _ in range(400):
        _check_abort()
        moved = False
        for m in METHODS[pref:] + METHODS[:pref]:
            apply_scroll(hwnd, cx, cy, m, down=False, notches=notches, park=park)
            time.sleep(wait)
            cur = grab(sct, region)
            if not images_equal(prev, cur):
                moved = True
                pref = METHODS.index(m)
                prev = cur
                _count_method(m)
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


def measure_fixed_regions(sct, region, hwnd, cx, cy, notches, wait, park=None,
                          restore=True):
    """
    스크롤해도 변하지 않는 고정 영역을 감지:
      - 상단(헤더)/하단(고정 바): 가로 밴드 높이 (top, bot)
      - 좌/우 고정 메뉴 + 스크롤바: 세로 밴드 폭 (left, right)  ← 변경된 '행 비율'로 판단해
        썸(thumb)만 움직이는 스크롤바도 포착
    감지 후 원래 위치로 복원한다(restore=False 면 복원하지 않고, 실제로 움직인
    프레임들을 'moved' 로 돌려줘 본 캡처에 그대로 이어 쓴다 → 위아래 왕복 없음).
    좌/우 밴드의 배경색도 함께 반환.
    """
    frames = [grab(sct, region)]
    moved = [frames[0]]
    downs = 0
    for _ in range(3):
        _check_abort()
        got = None
        for m in METHODS:
            apply_scroll(hwnd, cx, cy, m, down=True, notches=notches, park=park)
            time.sleep(wait)
            f = grab(sct, region)
            if not images_equal(frames[-1], f):
                got = f
                downs += 1
                moved.append(f)
                _count_method(m)
                break
        frames.append(got if got is not None else grab(sct, region))

    # 원래 위치로 복원
    for _ in range(downs if restore else 0):
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
            "left_bg": left_bg, "right_bg": right_bg, "sticky": sticky,
            "moved": moved, "downs": downs}


def _scroll_down_n(sct, region, hwnd, cx, cy, notches, wait, park, n):
    """n번 아래로 스크롤(작동하는 방식 자동 선택)."""
    for _ in range(n):
        _check_abort()
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
    global _CTL
    opts = opts or {}
    _CTL = opts.get("control")
    try:
        return _capture_scrolling_region(region, status_cb, opts)
    finally:
        _CTL = None
        if IS_MAC:
            pm.restore_pointer()     # 휠 방식으로 옮겼던 포인터를 원위치로


def _capture_scrolling_region(region, status_cb, opts):
    ctl = _CTL
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

    # 캡처 중 마우스 포인터를 옮기지 않는다(사용자 요청): park(커서 이동) 사용 안 함.
    park = None

    # 커서를 움직이지 않는 방식(send/key: 대상 창에 직접 스크롤 메시지)을 우선하고,
    # 이 방식들이 안 먹히는 창에서만 마지막으로 wheel(커서 위치 필요)을 쓴다.
    # 포인터를 옮기지 않는 방식 우선: 직접 전달(send) → (macOS) 위치 지정 휠(hid) → 방향키(key)
    # → 최후에만 실제 휠(wheel: 포인터를 잠깐 옮겼다 즉시 원위치)
    METHODS[:] = (["send", "key", "hid", "wheel"] if IS_MAC
                  else ["send", "key", "wheel"])
    if os.environ.get("SCROLLCAPTURE_METHODS"):     # 진단용: 방식 강제 지정
        METHODS[:] = os.environ["SCROLLCAPTURE_METHODS"].split(",")
    METHOD_STATS.clear()

    if IS_MAC:
        hwnd = pm.window_at_point(cx, cy)
    else:
        child = win32gui.WindowFromPoint((cx, cy))
        hwnd = ctypes.windll.user32.GetAncestor(child, GA_ROOT)
        if _own_window(hwnd):
            hwnd = _foreign_window_at(cx, cy) or hwnd
    ensure_foreground(hwnd)
    time.sleep(0.4)

    pref = 0
    header_img = None
    footer_img = None

    with screen_grabber() as sct:
        # 화면 좌표(region, 포인트) ↔ 이미지 픽셀 배율. Windows=1, macOS Retina=2.
        # 이후 w/h 는 '이미지 픽셀' 크기, region/cx/cy 는 '화면 좌표'로 사용한다.
        probe = grab(sct, region)
        sc = max(1, int(round(probe.shape[1] / float(w))))
        xs, ys, ws, hs = region
        h, w = probe.shape[:2]
        del probe

        # ---------- 0) 동작하는 스크롤 방식 확인(+ macOS 방향 보정) ----------
        probe_methods(sct, region, hwnd, cx, cy, notches, wait)

        # ---------- 1) (옵션) 맨 위로 이동 ----------
        if auto_top:
            status_cb(S.get("status_moving_top", "상단으로 이동 중..."))
            pref = scroll_to_top(sct, region, hwnd, cx, cy, top_notches, wait, park=park)

        # ---------- 1-b) 고정 상/하/좌/우 영역 감지 ----------
        # 깊은 sticky 감지(옵션)가 없으면 감지하며 내려간 프레임을 본 캡처에 그대로 이어 쓴다
        # (다시 위로 올라가지 않음). 옵션이 켜져 있으면 예전처럼 위치를 복원한다.
        status_cb(S.get("status_detecting", "고정 영역 감지 중..."))
        replay_mode = not handle_sticky
        reg = measure_fixed_regions(sct, region, hwnd, cx, cy, notches, wait,
                                    park=park, restore=not replay_mode)
        top_band, bot_band = reg["top"], reg["bot"]
        left_band, right_band = reg["left"], reg["right"]
        replay = reg["moved"] if replay_mode else None
        if auto_top and not replay_mode:
            pref = scroll_to_top(sct, region, hwnd, cx, cy, top_notches, wait, park=park)

        # 고정 상단(헤더)은 맨 위 1회, 고정 하단(트레이바 등)은 메모리에 보관 후 맨 아래에 덮음
        if sc > 1:   # Retina: 화면 좌표가 정수가 되도록 밴드를 배율의 배수로 올림
            top_band = -(-top_band // sc) * sc if top_band > 0 else 0
            bot_band = -(-bot_band // sc) * sc if bot_band > 0 else 0
        H_full = h
        full0 = replay[0] if replay else grab(sct, region)
        if top_band > 0:
            header_img = full0[0:top_band].copy()
        if bot_band > 0:
            footer_img = full0[H_full - bot_band:H_full].copy()
        if top_band > 0 or bot_band > 0:
            y2 = ys + top_band // sc
            hs2 = hs - (top_band + bot_band) // sc
            h = h - top_band - bot_band
            region = (xs, y2, ws, hs2)
            cx, cy = xs + ws // 2, y2 + hs2 // 2

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

        def inner(f):   # 측정 프레임(전체 영역) → 고정 상/하단을 뺀 본 캡처 영역
            return np.ascontiguousarray(f[top_band:H_full - bot_band])

        canvas = inner(replay[0]) if replay else grab(sct, region)
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

        last_place = {"off": None, "st": 0, "bak": None}

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
            # 고정 메뉴가 '처음 나타난' 직전 컷은 메뉴를 그대로 찍었으므로(그땐 감지 불가),
            # 이번에 감지된 높이만큼 그 자리를 덮기 전 내용(백업)으로 되돌린다.
            # (좌측 고정 메뉴 열에 남은 조각까지 전체 폭으로 복원)
            lp = last_place
            if st > lp["st"] and lp["bak"] is not None:
                bk, bm = lp["bak"]
                a, b = lp["st"], min(st, bk.shape[0])
                if b > a:
                    canvas[lp["off"] + a:lp["off"] + b] = bk[a:b]
                    left_mask[lp["off"] + a:lp["off"] + b] = bm[a:b]
            n_bak = min(h // 2, h - d) if lp["off"] is not None else 0
            bak = ((canvas[offset:offset + n_bak].copy(),
                    left_mask[offset:offset + n_bak].copy()) if n_bak > 0 else None)
            canvas[offset + st:offset + h, cl:cr] = frame[st:h, cl:cr]
            last_place.update(off=offset, st=st, bak=bak)
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

        def halted():
            """본 캡처 중: 취소면 예외, 중지면 True(여기까지 저장)."""
            if ctl is None:
                return False
            if ctl.cancel.is_set():
                raise CaptureCancelled()
            if ctl.stop.is_set():
                ctl.stopped = True
                return True
            return False

        # 고정 영역 감지 때 내려가며 찍은 프레임을 이어 붙인다(다시 올라가지 않음).
        # 정합이 안 되면 그만큼 위로 되돌아가 예전 방식으로 처음부터.
        if replay:
            ok = True
            for f in replay[1:]:
                cur = inner(f)
                st = max(sticky_base, _sticky_rows(prev, cur, cl, cr, h))
                delta, score = find_delta(prev[st:h, cl:cr], cur[st:h, cl:cr],
                                          max_frac=0.6)
                if 2 < delta <= h - st and score >= SCORE_THR:
                    place(cur, delta, st)
                    prev = cur
                    last_delta = delta
                    sticky_seen = max(sticky_seen, st)
                else:
                    ok = False
                    break
            if not ok:
                # 감지 때 내려간 만큼 되돌아감(실제로 움직이는 방식으로)
                for _ in range(reg.get("downs", 0)):
                    b0 = grab(sct, region)
                    for m in METHODS:
                        apply_scroll(hwnd, cx, cy, m, down=False, notches=notches,
                                     park=park)
                        time.sleep(wait)
                        if not images_equal(b0, grab(sct, region)):
                            break
                if auto_top:
                    scroll_to_top(sct, region, hwnd, cx, cy, top_notches, wait,
                                  park=park)
                canvas = grab(sct, region)
                prev = canvas.copy()
                offset = 0
                first_off = None
                left_mask = np.zeros(canvas.shape[0], bool)
                last_delta = 0
                sticky_seen = 0
                last_place.update(off=None, st=0, bak=None)

        for _ in range(8000):
            if halted():
                break
            before = prev

            # (a) 작은 스텝으로 정확히 진행 (콘텐츠 열만으로 정렬)
            #     매 스텝 '이전/현재 프레임의 상단 동일영역'을 sticky로 감지해 제외(제안 방식).
            best = None
            for m in order():
                if halted():
                    break
                apply_scroll(hwnd, cx, cy, m, down=True, notches=notches, park=park)
                time.sleep(wait)
                cur = grab(sct, region)
                # 매 스텝 '이전/현재 프레임 상단의 같은 내용' = 고정(sticky) 메뉴 → 제외
                st = max(sticky_base, _sticky_rows(before, cur, cl, cr, h))
                delta, score = find_delta(
                    before[st:h, cl:cr], cur[st:h, cl:cr], max_frac=0.6)
                if 2 < delta <= h - st and score >= SCORE_THR:
                    best = (cur, delta, st)
                    pref = METHODS.index(m)
                    _count_method(m)
                    break
                if (not images_equal(C(before), C(cur))
                        and not mostly_same(C(before), C(cur))):
                    # 움직였는데 정합만 실패 → 다른 방식을 더 실행하면 이동이 겹치므로 중단
                    pref = METHODS.index(m)
                    break
            if ctl is not None and ctl.stopped:
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

            # (b) 화면이 실제로 스크롤됐으나 정합 실패 → 직전 delta로 진행
            #     (일부만 바뀐 경우 = 떠 있는 버튼/마우스 오버 → 스크롤 아님)
            if not images_equal(C(before), C(cur)) and not mostly_same(C(before), C(cur)):
                st = max(sticky_base, _sticky_rows(before, cur, cl, cr, h))
                delta, score = find_delta(before[st:h, cl:cr], cur[st:h, cl:cr],
                                          max_frac=0.95)
                if 2 < delta <= h - st and score >= 0.35:
                    d = delta
                else:
                    d = last_delta if last_delta > 2 else h // 4
                    st = max(st, last_place["st"])   # 직전 고정 메뉴 높이는 계속 제외
                    if d > h - st:
                        st = 0
                place(cur, d, st)
                prev = cur
                status_cb(cap_fmt.format(h=canvas.shape[0]))
                continue

            # (c) 변화 없음 → 점점 큰 스크롤로 단색구간/바닥 판별
            crossed = None
            for pn in (6, 15):
                if halted():
                    break
                for m in order()[:1]:      # 확인된 방식만(다른 방식·키 입력을 남발하지 않음)
                    apply_scroll(hwnd, cx, cy, m, down=True, notches=pn, park=park)
                    time.sleep(wait * 1.4)
                    cur = grab(sct, region)
                    if (not images_equal(C(before), C(cur))
                            and not mostly_same(C(before), C(cur))):
                        crossed = cur
                        pref = METHODS.index(m)
                        break
                if crossed is not None:
                    break
            if crossed is None:
                break  # 진짜 바닥(또는 중지)

            cur = crossed
            st = max(sticky_base, _sticky_rows(before, cur, cl, cr, h))
            delta, score = find_delta(before[st:h, cl:cr], cur[st:h, cl:cr],
                                      max_frac=0.95)
            if 2 < delta <= h - st and score >= SCORE_THR:
                d = delta
            else:
                d, st = h, 0
            place(cur, d, st)
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

    if canvas.shape[0] <= h + 4 and not (ctl is not None and ctl.stopped):
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


def save_png_split(img, out_dir, split_h):
    """설정된 높이(px)마다 이미지를 잘라 01, 02, 03... 순번으로 저장."""
    os.makedirs(out_dir, exist_ok=True)
    H, W = img.shape[:2]
    split_h = max(1, int(split_h))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    n = max(1, (H + split_h - 1) // split_h)
    digits = max(2, len(str(n)))
    paths = []
    for i in range(n):
        y0 = i * split_h
        y1 = min(H, y0 + split_h)
        piece = img[y0:y1]
        idx = str(i + 1).zfill(digits)
        path = os.path.join(out_dir, f"capture_{ts}_{W}x{H}_{idx}.png")
        ok, buf = cv2.imencode(".png", piece)
        if not ok:
            raise RuntimeError("이미지 인코딩 실패")
        buf.tofile(path)
        paths.append(path)
    return paths


# ==================================================================
# 영역 선택 오버레이
# ==================================================================
# 지금 열려 있는 선택 화면의 '취소' 함수. 선택 중 Esc 를 전역 단축키로 받아 호출한다
# (macOS 는 테두리 없는 Tk 창이 키 입력을 받지 못해 Esc 가 전달되지 않음).
_ACTIVE_OVERLAY = [None]
# 자체 점검(CI)용 마지막 선택 화면 정보
LAST_OVERLAY_INFO = {}


class RegionSelector:
    """드래그로 영역 선택. multi=True면 전체 가상 화면(멀티 모니터) 위에서 선택.
    Windows: 가상 화면 전체를 덮는 오버레이 1개, 실제 좌표는 win32 커서 좌표(DPI 안전).
    macOS  : 창이 디스플레이를 넘을 수 없으므로 모니터마다 오버레이를 하나씩 띄운다.

    화면을 한 장 찍어 어둡게 깔고, 선택 중인 영역만 원래 밝기로 보여 준다
    (선택 영역은 반투명 마스킹 없음). 포인터는 가운데 공백 없는 흰색 줄 십자."""

    DIM = 0.55          # 선택 영역 밖 어둡게(1.0 = 원래 밝기)
    CROSS = 22          # 십자선 한쪽 길이(px)

    def __init__(self, master, multi=False, guide="", frozen=None):
        self.result = None
        self._start_scr = None
        self._start_root = None
        self._sel = None
        self._pending = None
        self._job = None
        self._closed = False
        LAST_OVERLAY_INFO.clear()
        if IS_MAC:
            areas = get_monitors() if multi else [primary_screen()]
        else:
            areas = [virtual_screen() if multi else primary_screen()]

        self.tops = []
        self.views = []        # 모니터(오버레이)별 그리기 정보
        for (vx, vy, vw, vh) in areas:
            top = tk.Toplevel(master)
            top.overrideredirect(True)
            top.geometry(f"{vw}x{vh}+{vx}+{vy}")
            top.attributes("-topmost", True)
            # 포인터: Windows 는 직접 그린 흰색 줄 십자(시스템 포인터 숨김),
            # macOS 는 시스템 십자 포인터(하드웨어가 그려 지연 없음)
            cur = "crosshair" if IS_MAC else "none"
            top.configure(bg="black", cursor=cur)
            cv = tk.Canvas(top, highlightthickness=0, bg="black", cursor=cur)
            cv.pack(fill="both", expand=True)
            v = {"cv": cv, "ox": vx, "oy": vy, "w": vw, "h": vh}
            bright, dark = self._backdrop(top, vx, vy, vw, vh, frozen)
            if dark is not None:
                v["bright"], v["dark"] = bright, dark
                cv.create_image(0, 0, anchor="nw", image=dark)
                v["hole_img"] = tk.PhotoImage(master=top)
                v["hole"] = cv.create_image(0, 0, anchor="nw",
                                            image=v["hole_img"], state="hidden")
            else:        # 화면을 못 찍으면 예전 방식(창 전체 반투명)
                top.attributes("-alpha", 0.28)
                v["hole"] = None
            v["rect"] = cv.create_rectangle(0, 0, 0, 0, outline="#ff3b30",
                                            width=1, state="hidden")
            # 안내 문구(그림자 + 흰 글자)
            for dx, col in ((2, "black"), (0, "white")):
                cv.create_text(vw // 2 + dx, 40 + dx, text=guide, fill=col,
                               font=(UI_FONT, 16, "bold"))
            # 줄 십자 포인터: 검은 테두리(3px) 위에 흰 선(1px) — 흰 배경에서도 보이게
            v["cross"] = [] if IS_MAC else [
                cv.create_line(0, 0, 0, 0, fill="black", width=3),
                cv.create_line(0, 0, 0, 0, fill="black", width=3),
                cv.create_line(0, 0, 0, 0, fill="white", width=1),
                cv.create_line(0, 0, 0, 0, fill="white", width=1)]
            if not IS_MAC:
                # Windows: Tk 마우스 이벤트 사용
                cv.bind("<Motion>", self._hover)
                cv.bind("<ButtonPress-1>", self._down)
                cv.bind("<B1-Motion>", self._move)
                cv.bind("<ButtonRelease-1>", self._up)
            # 취소: Esc (전역 단축키로도 받음 — _ACTIVE_OVERLAY)
            top.bind("<Escape>", lambda e: self._cancel())
            self.tops.append(top)
            self.views.append(v)
        self.top = self.tops[0]
        try:
            px, py = (pm.mouse_pos() if IS_MAC else self.top.winfo_pointerxy())
            self._draw_cross(px, py)
        except Exception:
            pass
        self.top.focus_force()
        if IS_MAC:
            # macOS: 테두리 없는 Tk 창은 마우스 이동/키 이벤트를 제대로 못 받으므로
            #  - 창을 Dock·메뉴 막대 위 레벨로 올리고(화면 전체 덮기)
            #  - 마우스 위치·버튼 상태를 직접 읽어(폴링) 십자/드래그를 처리한다.
            for t in self.tops:
                t.update()
            used, levels = [], []
            for area in areas:
                w = pm.overlay_window(area, exclude=used)
                if w is not None:
                    used.append(w)
                    levels.append(int(w.level()))
            LAST_OVERLAY_INFO["levels"] = levels
            pm.esc_watch_start()
            self._prev_btn = pm.left_button_down()
            self._job = self.top.after(15, self._mac_poll)
        else:
            self.top.grab_set()
            if len(self.tops) > 1:   # 다른 모니터의 오버레이도 클릭을 받도록 전역 grab 해제
                self.top.grab_release()
        _ACTIVE_OVERLAY[0] = self._cancel
        try:
            master.wait_window(self.top)
        finally:
            if _ACTIVE_OVERLAY[0] == self._cancel:
                _ACTIVE_OVERLAY[0] = None

    # ---------- 배경(어두운 화면 / 밝은 원본) ----------
    def _backdrop(self, top, x, y, w, h, frozen=None):
        try:
            if frozen is not None:          # 캡처를 누른 순간의 화면
                img = crop_frozen(frozen, (x, y, w, h))
            else:
                with screen_grabber() as sct:
                    img = grab(sct, (x, y, w, h))
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            if rgb.shape[1] != w or rgb.shape[0] != h:     # Retina(2배) → 화면 좌표 크기
                rgb = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA)
            dark = (rgb.astype(np.float32) * self.DIM).astype(np.uint8)
            return (ImageTk.PhotoImage(Image.fromarray(rgb), master=top),
                    ImageTk.PhotoImage(Image.fromarray(dark), master=top))
        except Exception:
            return None, None

    # ---------- 그리기 ----------
    def _pointer(self, e):
        if IS_MAC:
            return (e.x_root, e.y_root)
        return win32gui.GetCursorPos()

    def _draw_cross(self, gx, gy):
        LAST_OVERLAY_INFO["cross"] = (int(gx), int(gy))
        L = self.CROSS
        for v in self.views:
            if not v["cross"]:              # macOS: 시스템 포인터 사용(그리지 않음)
                continue
            x, y = gx - v["ox"], gy - v["oy"]
            hb, vb, hw, vw_ = v["cross"]
            cv = v["cv"]
            cv.coords(hb, x - L, y, x + L + 1, y)
            cv.coords(vb, x, y - L, x, y + L + 1)
            cv.coords(hw, x - L, y, x + L + 1, y)
            cv.coords(vw_, x, y - L, x, y + L + 1)
            for i in v["cross"]:
                cv.tag_raise(i)

    def _draw_sel(self):
        self._pending = None
        if self._sel is None:
            return
        x1, y1, x2, y2 = self._sel
        gx1, gx2 = min(x1, x2), max(x1, x2)
        gy1, gy2 = min(y1, y2), max(y1, y2)
        for v in self.views:
            cv = v["cv"]
            lx1, ly1 = gx1 - v["ox"], gy1 - v["oy"]
            lx2, ly2 = gx2 - v["ox"], gy2 - v["oy"]
            cv.coords(v["rect"], lx1 - 1, ly1 - 1, lx2, ly2)   # 1px, 선택 영역 바깥
            cv.itemconfigure(v["rect"], state="normal")
            if v["hole"] is None:
                continue
            # 이 모니터 안쪽으로 자른 선택 영역만 밝은 원본에서 복사
            cx1, cy1 = max(0, lx1), max(0, ly1)
            cx2, cy2 = min(v["w"], lx2), min(v["h"], ly2)
            if cx2 - cx1 < 1 or cy2 - cy1 < 1:
                cv.itemconfigure(v["hole"], state="hidden")
                continue
            img = v["hole_img"]
            img.blank()
            img.configure(width=cx2 - cx1, height=cy2 - cy1)
            img.tk.call(str(img), "copy", str(v["bright"]),
                        "-from", cx1, cy1, cx2, cy2, "-to", 0, 0)
            cv.coords(v["hole"], cx1, cy1)
            cv.itemconfigure(v["hole"], state="normal")
            cv.tag_raise(v["rect"])
        for v in self.views:              # 십자 포인터는 항상 맨 위
            for i in v["cross"]:
                v["cv"].tag_raise(i)

    def _schedule_sel(self):
        if self._pending is None:      # 마우스 이동이 빠를 때 그리기 합치기
            self._pending = self.top.after_idle(self._draw_sel)

    # ---------- 마우스 ----------
    def _mac_poll(self):
        """macOS: 마우스 위치/버튼을 직접 읽어 십자·드래그 처리(15ms 간격)."""
        if self._closed:
            return
        try:
            x, y = pm.mouse_pos()
            esc = pm.esc_pressed()
            if esc:                             # Esc = 취소
                LAST_OVERLAY_INFO["esc_via"] = esc
                self._cancel()
                return
            if pm.right_button_down():          # 우클릭 = 취소
                self._cancel()
                return
            btn = pm.left_button_down()
            if btn and not self._prev_btn:
                self._down_at(x, y, (x, y))
            elif btn and self._start_root is not None:
                self._move_at(x, y)
            elif not btn and self._prev_btn and self._start_scr is not None:
                self._up_at((x, y))
                return
            else:
                self._draw_cross(x, y)
            self._prev_btn = btn
        except Exception:
            pass
        if not self._closed:
            self._job = self.top.after(15, self._mac_poll)

    def _hover(self, e):
        self._draw_cross(e.x_root, e.y_root)

    def _down(self, e):
        self._down_at(e.x_root, e.y_root, self._pointer(e))

    def _down_at(self, gx, gy, scr):
        self._start_scr = scr
        self._start_root = (gx, gy)
        self._sel = (gx, gy, gx, gy)
        self._draw_cross(gx, gy)

    def _move(self, e):
        self._move_at(e.x_root, e.y_root)

    def _move_at(self, gx, gy):
        self._draw_cross(gx, gy)
        if self._start_root is not None:
            self._sel = (self._start_root[0], self._start_root[1], gx, gy)
            self._schedule_sel()

    def _up(self, e):
        self._up_at(self._pointer(e))

    def _up_at(self, end):
        if self._start_scr is None:
            return
        x1, y1 = self._start_scr
        x2, y2 = end
        x, y = int(min(x1, x2)), int(min(y1, y2))
        w, h = int(abs(x2 - x1)), int(abs(y2 - y1))
        self.result = (x, y, w, h) if (w >= 10 and h >= 10) else None
        self._close()

    def _cancel(self):
        self.result = None
        self._close()

    def _close(self):
        if self._closed:
            return
        self._closed = True
        if IS_MAC:
            pm.esc_watch_stop()
        if self._job:
            try:
                self.top.after_cancel(self._job)
            except Exception:
                pass
        for t in self.tops[1:] + self.tops[:1]:   # 대기 중인 첫 창은 마지막에
            try:
                t.destroy()
            except Exception:
                pass


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
                            font=(UI_FONT, 16, "bold"))
        self.lbl.place(relx=0.5, rely=0.5, anchor="center")
        self.top.bind("<Escape>", lambda e: self._close(None))
        self._nswin = None
        self._closed = False
        if IS_MAC:
            # macOS: 클릭도 직접 읽음(폴링), 오버레이는 Dock·메뉴 막대 위 레벨
            self._prev_btn = pm.left_button_down()
            pm.esc_watch_start()
        else:
            self.top.bind("<Button-1>", lambda e: self._pick())
        self._poll()
        self.top.focus_force()
        _ACTIVE_OVERLAY[0] = lambda: self._close(None)
        try:
            master.wait_window(self.top)
        finally:
            _ACTIVE_OVERLAY[0] = None

    def _mon_at(self, x, y):
        for r in self.monitors:
            mx, my, mw, mh = r
            if mx <= x < mx + mw and my <= y < my + mh:
                return r
        return None

    def _poll(self):
        if self._closed:
            return
        try:
            x, y = (pm.mouse_pos() if IS_MAC else win32gui.GetCursorPos())
        except Exception:
            x, y = 0, 0
        r = self._mon_at(x, y)
        if r and r != self.cur:
            self.cur = r
            self.top.geometry(f"{r[2]}x{r[3]}+{r[0]}+{r[1]}")
            if IS_MAC:
                try:
                    self.top.update()
                    if self._nswin is None:
                        self._nswin = pm.overlay_window(r)
                    else:
                        pm.place_overlay(self._nswin, r)
                except Exception:
                    pass
        if IS_MAC:
            if pm.right_button_down() or pm.esc_pressed():
                self._close(None)
                return
            btn = pm.left_button_down()
            if not btn and self._prev_btn and self.cur:   # 클릭(뗄 때) = 선택
                self._close(self.cur)
                return
            self._prev_btn = btn
        self._job = self.top.after(30 if IS_MAC else 60, self._poll)

    def _pick(self):
        self._close(self.cur)

    def _close(self, result):
        if self._closed:
            return
        self._closed = True
        if IS_MAC:
            pm.esc_watch_stop()
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
                 font=(UI_FONT, 14, "bold"), padx=22, pady=12).pack()
        self.top.update_idletasks()
        vx, vy, vw, vh = primary_screen() if IS_MAC else virtual_screen()
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
        self._prev_btn = self._left_down()
        if IS_MAC:
            pm.esc_watch_start()
        self.top.after(60, self._register_own)
        self._job = self.top.after(120, self._poll)
        _ACTIVE_OVERLAY[0] = lambda: self._close(None)
        try:
            master.wait_window(self.top)
        finally:
            _ACTIVE_OVERLAY[0] = None

    @staticmethod
    def _left_down():
        if IS_MAC:
            return pm.left_button_down()
        return bool(win32api.GetAsyncKeyState(0x01) & 0x8000)

    def _register_own(self):
        if IS_MAC:          # macOS 는 자기 프로세스(pid) 창을 통째로 제외
            return
        for w in [self.top] + self.edges:
            try:
                hid = w.winfo_id()
                self._own_hwnds.add(hid)
                self._own_hwnds.add(
                    ctypes.windll.user32.GetAncestor(hid, GA_ROOT))
            except Exception:
                pass

    def _window_under(self, x, y):
        if IS_MAC:
            return pm.window_at_point(x, y)
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
            x, y = (self.top.winfo_pointerxy() if IS_MAC
                    else win32gui.GetCursorPos())
        except Exception:
            x, y = 0, 0
        hwnd = self._window_under(x, y)
        if hwnd and hwnd != self.cur_hwnd:
            self.cur_hwnd = hwnd
            self._draw_outline(hwnd)

        if IS_MAC and (pm.right_button_down() or pm.esc_pressed()):  # 우클릭/Esc = 취소
            self._close(None)
            return
        btn = self._left_down()
        if btn and not self._prev_btn and self.cur_hwnd:
            self._close(self.cur_hwnd)
            return
        self._prev_btn = btn
        self._job = self.top.after(60, self._poll)

    def _close(self, hwnd):
        if IS_MAC:
            pm.esc_watch_stop()
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
                 font=(UI_FONT, 9, "bold")).pack(side="left")
        key_txt = self._val(self.key)
        if key_txt:
            tk.Label(head, text=key_txt, bg="#2b2b2b", fg="#8fb7ff",
                     font=(UI_FONT, 9, "bold")).pack(side="left", padx=(8, 0))
        tk.Label(frm, text=self._val(self.hint), bg="#2b2b2b", fg="#cfcfcf",
                 font=(UI_FONT, 8), justify="left", wraplength=220).pack(
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


if IS_MAC:   # macOS: Carbon RegisterEventHotKey 기반(같은 인터페이스)
    HotkeyManager = pm.HotkeyManager


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
# Shift+숫자로 나오는 기호 keysym → 원래 숫자(표시용)
_SHIFT_DIGIT = {"exclam": "1", "at": "2", "numbersign": "3", "dollar": "4",
                "percent": "5", "asciicircum": "6", "ampersand": "7",
                "asterisk": "8", "parenleft": "9", "parenright": "0"}


def _key_display_name(keysym):
    kl = keysym.lower()
    if kl in _SHIFT_DIGIT:            # Shift+숫자 → 숫자로 표시
        return _SHIFT_DIGIT[kl]
    if keysym in _KEY_SPECIAL:
        return _KEY_SPECIAL[keysym]
    if len(keysym) == 1:
        return keysym.upper()
    if keysym.startswith("F") and keysym[1:].isdigit():
        return keysym
    if keysym.startswith("KP_"):
        return keysym[3:]
    return keysym.capitalize() if keysym else None


def _round_rect_points(x1, y1, x2, y2, r):
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


class RoundButton(tk.Canvas):
    """Canvas 로 그린 모서리 둥근 얇은 버튼(ttk 사각 버튼 대체).
    kind: 'secondary'(흰 배경+연한 테두리) 또는 'primary'(파란 배경)."""

    def __init__(self, master, text="", command=None, kind="secondary",
                 font=None, padx=12, pady=5, radius=8, min_width=0):
        self.kind = kind
        self.command = command
        self.radius = radius
        self.padx = padx
        self.pady = pady
        self.min_width = min_width
        self._text = text
        self._enabled = True
        self._hover = False
        self.font = font or (pick_font_family(), -12)
        super().__init__(master, highlightthickness=0, bd=0, bg=MC["bg"],
                         takefocus=0, cursor="hand2")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def _size(self):
        f = tkfont.Font(font=self.font)
        w = max(self.min_width, f.measure(self._text) + self.padx * 2)
        h = f.metrics("linespace") + self.pady * 2
        return int(w), int(h)

    def _draw(self, press=False):
        self.delete("all")
        w, h = self._size()
        self.configure(width=w, height=h)
        C = MC
        if not self._enabled:
            fill, fg, bd = C["surface"], C["muted"], C["border"]
        elif self.kind == "primary":
            fill = C["primary_hover"] if (self._hover or press) else C["primary"]
            fg, bd = "white", fill
        elif self.kind == "danger":
            fill = "#FDECEA" if (self._hover or press) else C["white"]
            fg = "#E53935"
            bd = "#E53935" if self._hover else C["border"]
        else:
            fill = C["surface"] if (self._hover or press) else C["white"]
            fg = C["text"]
            bd = C["primary"] if self._hover else C["border"]
        self.create_polygon(_round_rect_points(1, 1, w - 1, h - 1, self.radius),
                            smooth=True, splinesteps=24, fill=fill,
                            outline=bd, width=1)
        self.create_text(w // 2, h // 2 + 1, text=self._text, fill=fg,
                         font=self.font)

    def _on_enter(self, _=None):
        if self._enabled:
            self._hover = True
            self._draw()

    def _on_leave(self, _=None):
        self._hover = False
        self._draw()

    def _on_press(self, _=None):
        if self._enabled:
            self._draw(press=True)

    def _on_release(self, e=None):
        if not self._enabled:
            return
        self._draw()
        # 커서가 버튼 위에서 떼졌을 때만 실행
        if 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height():
            if self.command:
                self.command()

    def set_text(self, text):
        self._text = text
        self._draw()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw()


class SpeedSlider(tk.Canvas):
    """세그먼트형 커스텀 슬라이더. 핸들 가로폭 = 전체폭 / 단계수(눈금과 정렬).
    가운데 grip 선이 없고, 창 가로에 따라 폭이 자동 조정된다."""

    def __init__(self, master, values, idx_var, on_change=None, height=26):
        self.values = values
        self.n = max(1, len(values))
        self.idx_var = idx_var
        self.on_change = on_change
        super().__init__(master, height=height, highlightthickness=0, bd=0,
                         bg=MC["bg"], cursor="hand2")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<B1-Motion>", self._on_click)
        self._draw()

    def _on_click(self, e):
        sw = max(1.0, self.winfo_width() / self.n)
        i = max(0, min(self.n - 1, int(e.x // sw)))
        changed = (i != self.idx_var.get())
        self.idx_var.set(i)
        self._draw()
        if changed and self.on_change:
            self.on_change()

    def refresh(self):
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = int(self.cget("height"))
        if w <= 1:
            return
        C = MC
        # 트랙(얇은 라운드 바)
        ty0, ty1 = h * 0.40, h * 0.60
        self.create_polygon(
            _round_rect_points(1, ty0, w - 1, ty1, (ty1 - ty0) / 2),
            smooth=True, splinesteps=12, fill=C["surface"], outline=C["surface"])
        # 핸들(한 구간 폭 = 전체/단계수)
        sw = w / self.n
        i = max(0, min(self.n - 1, self.idx_var.get()))
        x0, x1 = i * sw + 2, (i + 1) * sw - 2
        self.create_polygon(
            _round_rect_points(x0, 2, x1, h - 2, 8),
            smooth=True, splinesteps=16, fill=C["primary"], outline=C["primary"])


class IconRoundButton(tk.Canvas):
    """캡처 아이콘 버튼: 카드색 배경을 꽉 채운 둥근 사각(1px 테두리) + 중앙 아이콘."""

    def __init__(self, master, image, command=None, radius=12, height=60):
        self.image = image
        self.command = command
        self.radius = radius
        self._enabled = True
        self._hover = False
        super().__init__(master, height=height, highlightthickness=0, bd=0,
                         bg=MC["bg"], cursor="hand2")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _draw(self, press=False):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1:
            return
        C = MC
        bd = C["primary"] if (self._hover and self._enabled) else "#E5E5E5"
        fill = C["card"] if self._enabled else C["surface"]
        self.create_polygon(_round_rect_points(1, 1, w - 1, h - 1, self.radius),
                            smooth=True, splinesteps=24, fill=fill,
                            outline=bd, width=1)
        self.create_image(w // 2, h // 2, image=self.image)

    def _on_enter(self, _=None):
        self._hover = True
        self._draw()

    def _on_leave(self, _=None):
        self._hover = False
        self._draw()

    def _on_press(self, _=None):
        if self._enabled:
            self._draw(press=True)

    def _on_release(self, e=None):
        if not self._enabled:
            return
        self._draw()
        if 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height():
            if self.command:
                self.command()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw()


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
        RoundButton(btns, text=t("hk_close"), kind="secondary",
                    command=self.top.destroy).pack(side="right", padx=(8, 0))
        RoundButton(btns, text=t("hk_save"), kind="primary",
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
        if IS_MAC:
            return self._on_key_mac(event, mode)
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

    def _on_key_mac(self, event, mode):
        """macOS: 지금 눌린 물리 키코드/수정자를 NSEvent 에서 직접 얻는다
        (Option 조합으로 바뀐 문자와 무관). 실패 시 Tk keysym/state 로 폴백."""
        info = pm.current_key_event()
        if info is not None:
            kc, mods = info
        else:
            kc = pm.keysym_to_keycode(_key_display_name(event.keysym) or "")
            st = int(event.state)
            mods = 0
            if st & 0x0001:
                mods |= pm.MOD_SHIFT
            if st & 0x0004:
                mods |= pm.MOD_CONTROL
            if st & 0x0010:
                mods |= pm.MOD_OPTION
            if st & 0x0008:
                mods |= pm.MOD_CMD
        if kc is None or kc in pm.MODIFIER_KEYCODES:
            return "break"
        name = pm.KEYCODE_NAMES.get(kc)
        if not name:
            return "break"
        parts = pm.mods_text(mods) + [name]
        self.temp[mode] = (mods, int(kc), " + ".join(parts))
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


class CapturePanel:
    """스크롤 캡처 중 떠 있는 작은 패널: [■ 중지](여기까지 저장) / [취소](저장 안 함).
    제목 부분을 끌어서 옮길 수 있고, 캡처 결과 이미지에는 찍히지 않는다."""

    def __init__(self, master, t, region, on_stop, on_cancel, pos=None,
                 on_moved=None):
        self.on_moved = on_moved
        self._drag = None
        C = MC
        self.top = top = tk.Toplevel(master)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=C["primary"])                     # 1px 파란 테두리
        body = tk.Frame(top, bg=C["white"])
        body.pack(padx=1, pady=1)
        head = tk.Frame(body, bg=C["white"], cursor="fleur")
        head.pack(fill="x", padx=10, pady=(8, 0))
        grip = tk.Label(head, text="⠿", bg=C["white"], fg=C["muted"],
                        font=(UI_FONT, 11), cursor="fleur")
        grip.pack(side="left")
        title = tk.Label(head, text=t("panel_title"), bg=C["white"], fg=C["text"],
                         font=(pick_font_family(), -13, "bold"), cursor="fleur")
        title.pack(side="left", padx=(4, 0))
        self.info = tk.Label(body, text=t("status_preparing"), bg=C["white"],
                             fg=C["primary"], font=(pick_font_family(), -12),
                             anchor="w", width=26)
        self.info.pack(fill="x", padx=10, pady=(4, 0))
        btns = tk.Frame(body, bg=C["white"])
        btns.pack(fill="x", padx=10, pady=(8, 0))
        self.b_stop = RoundButton(btns, text=t("btn_stop"), kind="primary",
                                  min_width=96, command=on_stop)
        self.b_stop.pack(side="left")
        self.b_cancel = RoundButton(btns, text=t("btn_cancel_cap"), kind="danger",
                                    min_width=96, command=on_cancel)
        self.b_cancel.pack(side="left", padx=(8, 0))
        hint = tk.Label(body, text=t("panel_hint"), bg=C["white"], fg=C["muted"],
                        font=(pick_font_family(), -11))
        hint.pack(fill="x", padx=10, pady=(6, 8))
        for w in (top, body, head, grip, title, self.info, hint):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._motion)
            w.bind("<ButtonRelease-1>", self._release)

        top.update_idletasks()
        pw, ph = top.winfo_reqwidth(), top.winfo_reqheight()
        x, y = self._place(region, pw, ph, pos)
        top.geometry(f"+{x}+{y}")
        top.update()
        self._exclude_from_capture()

    # ----- 위치: 저장된 위치(캡처 영역과 안 겹칠 때) → 영역과 안 겹치는 모서리 -----
    @staticmethod
    def _overlap(a, b, m=8):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return not (ax + aw + m <= bx or bx + bw + m <= ax
                    or ay + ah + m <= by or by + bh + m <= ay)

    def _place(self, region, pw, ph, pos):
        mons = get_monitors()
        if pos:
            px, py = int(pos[0]), int(pos[1])
            inside = any(mx <= px and my <= py and px + pw <= mx + mw
                         and py + ph <= my + mh for mx, my, mw, mh in mons)
            if inside and not self._overlap((px, py, pw, ph), region):
                return px, py
        rx, ry, rw, rh = region
        cx, cy = rx + rw // 2, ry + rh // 2
        home = [m for m in mons
                if m[0] <= cx < m[0] + m[2] and m[1] <= cy < m[1] + m[3]]
        order = home + [m for m in mons if m not in home]
        for mx, my, mw, mh in order:
            for x, y in ((mx + mw - pw - 24, my + mh - ph - 64),
                         (mx + 24, my + mh - ph - 64),
                         (mx + mw - pw - 24, my + 48),
                         (mx + 24, my + 48)):
                if not self._overlap((x, y, pw, ph), region):
                    return x, y
        mx, my, mw, mh = order[0]
        return mx + mw - pw - 24, my + mh - ph - 64

    def _exclude_from_capture(self):
        if IS_MAC:
            pm.set_capture_excluded(True)
            return
        try:
            u = ctypes.windll.user32
            hwnd = u.GetAncestor(self.top.winfo_id(), GA_ROOT)
            # 클릭해도 대상 창의 포커스를 빼앗지 않음
            ex = u.GetWindowLongW(hwnd, -20)                  # GWL_EXSTYLE
            u.SetWindowLongW(hwnd, -20, ex | 0x08000000)      # WS_EX_NOACTIVATE
            # 화면 캡처에서 제외 (Windows 10 2004+)
            u.SetWindowDisplayAffinity(hwnd, 0x11)            # WDA_EXCLUDEFROMCAPTURE
        except Exception:
            pass

    # ----- 끌어서 이동 -----
    def _press(self, e):
        self._drag = (e.x_root - self.top.winfo_x(), e.y_root - self.top.winfo_y())

    def _motion(self, e):
        if self._drag:
            self.top.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _release(self, e):
        if self._drag and self.on_moved:
            self.on_moved(self.top.winfo_x(), self.top.winfo_y())
        self._drag = None

    def set_info(self, text):
        try:
            self.info.config(text=text)
        except Exception:
            pass

    def lock(self, text):
        """중지/취소를 누른 뒤: 버튼 비활성 + 안내."""
        self.set_info(text)
        for b in (self.b_stop, self.b_cancel):
            try:
                b.set_enabled(False)
            except Exception:
                pass

    def close(self):
        if IS_MAC:
            pm.set_capture_excluded(False)
        try:
            self.top.destroy()
        except Exception:
            pass


class BusyOverlay:
    """캡처 후 저장/합성 중 '잠시만 기다려 주세요' + 큰 진행 아이콘을 화면 가운데 표시."""

    def __init__(self, master, text):
        self._job = None
        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        vx, vy, vw, vh = primary_screen() if IS_MAC else virtual_screen()
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
                 font=(UI_FONT, 20, "bold")).pack()
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
    "card": "#F4F6F9",           # 아이콘 카드 배경색
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
        for f in ("Pretendard", "Noto Sans KR", "맑은 고딕", "Malgun Gothic",
                  "Apple SD Gothic Neo"):
            if f in fams:
                return f
    except Exception:
        pass
    return UI_FONT


# ==================================================================
# 메인 GUI
# ==================================================================
class App:
    def __init__(self):
        self.busy = False
        self._thumbs = []
        self.cfg = load_cfg()

        self.root = tk.Tk()
        if IS_MAC:
            # Tk(macOS)는 72dpi 기준이라 pt 글꼴이 작게 보임 → Windows(96dpi)와 같은 크기로
            try:
                self.root.tk.call("tk", "scaling", 96.0 / 72.0)
            except Exception:
                pass
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
        default_dir = self.cfg.get("save_dir") or default_save_dir()
        self.save_dir = tk.StringVar(value=default_dir)
        self.opt_scroll_top = tk.BooleanVar(value=bool(self.cfg.get("scroll_top", True)))
        self.opt_multi = tk.BooleanVar(value=bool(self.cfg.get("multi_monitor", True)))
        self.opt_hide = tk.BooleanVar(value=bool(self.cfg.get("hide_app", True)))
        self.opt_on_top = tk.BooleanVar(value=bool(self.cfg.get("on_top", False)))
        self.opt_sticky = tk.BooleanVar(value=bool(self.cfg.get("sticky", False)))
        self.opt_hide_mouse = tk.BooleanVar(value=bool(self.cfg.get("hide_mouse", True)))
        self.opt_auto_copy = tk.BooleanVar(value=bool(self.cfg.get("auto_copy", True)))
        self.opt_split_enabled = tk.BooleanVar(value=bool(self.cfg.get("split_enabled", False)))
        self.split_height_var = tk.StringVar(
            value=str(int(self.cfg.get("split_height", 4000) or 4000)))
        self.opt_expanded = tk.BooleanVar(value=bool(self.cfg.get("options_expanded", False)))
        self.speed_idx = tk.IntVar(value=int(self.cfg.get("speed_idx", SPEED_DEFAULT_IDX)))
        self.status = tk.StringVar()
        self._update_info = None   # 사용 가능한 업데이트 정보
        self._busy_overlay = None  # 저장 중 '진행중' 오버레이

        self.hotkeys = self._load_hotkeys()
        self._hk_queue = collections.deque()
        self._starting = False
        self.hotkeys_mgr = HotkeyManager(self._hotkey_fire)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.attributes("-topmost", self.opt_on_top.get())
        self.refresh_list()
        self._apply_hotkeys()
        self._auto_check_update()
        if IS_MAC:
            self.root.after(50, self._pump_hotkeys)
        if IS_MAC and not os.environ.get("SCROLLCAPTURE_NO_PERM_PROMPT"):
            self.root.after(600, self._check_mac_permissions)

    # ---------- macOS 권한(화면 기록 / 손쉬운 사용) ----------
    def _check_mac_permissions(self):
        screen, ax = pm.permissions_status()
        if screen and ax:
            return
        pm.request_permissions()            # 시스템 권한 요청 창
        messagebox.showinfo(self.t("info_title"), self.t("mac_perm_msg"))
        pm.open_privacy_settings("ScreenCapture" if not screen
                                 else "Accessibility")

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
        st.configure("TLabelframe", background=C["bg"], bordercolor="#EDEDED",
                     relief="solid", borderwidth=1, lightcolor="#EDEDED",
                     darkcolor="#EDEDED")
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
        # 경로(변경/열기) 전용 컴팩트 버튼 — 가로 폭 축소
        st.configure("Path.TButton", background=C["white"],
                     foreground=C["text"], bordercolor=C["border"],
                     borderwidth=1, relief="solid", font=self.f_cap,
                     padding=(2, 3), focuscolor=C["white"])
        st.map("Path.TButton",
               background=[("active", C["surface"]), ("pressed", C["surface"])],
               bordercolor=[("active", C["primary"])])
        # 캡처 아이콘 버튼(얇은 테두리 + 라운드 카드 이미지)
        st.configure("Icon.TButton", background=C["bg"], borderwidth=1,
                     bordercolor="#E5E5E5", relief="solid", padding=3,
                     lightcolor="#E5E5E5", darkcolor="#E5E5E5",
                     focuscolor=C["bg"])
        st.map("Icon.TButton",
               background=[("active", C["bg"]),
                           ("pressed", C["bg"]),
                           ("disabled", C["bg"])],
               bordercolor=[("active", C["primary"]), ("pressed", C["primary"])],
               lightcolor=[("active", C["primary"]), ("pressed", C["primary"])],
               darkcolor=[("active", C["primary"]), ("pressed", C["primary"])])
        # 얇은 스크롤바
        st.configure("Thin.Vertical.TScrollbar", troughcolor=C["surface"],
                     background=C["thumb"], bordercolor=C["surface"],
                     arrowcolor=C["surface"], arrowsize=12, gripcount=0,
                     lightcolor=C["thumb"], darkcolor=C["thumb"], relief="flat")
        st.map("Thin.Vertical.TScrollbar",
               background=[("active", C["thumb_hover"]),
                           ("pressed", C["thumb_hover"])])
        # 진행바
        st.configure("Material.Horizontal.TProgressbar",
                     troughcolor=C["surface"], background=C["primary"],
                     bordercolor=C["surface"], lightcolor=C["primary"],
                     darkcolor=C["primary"])
        # 스크롤 속도 슬라이더(가운데 선 없는 넓은 핸들)
        st.configure("Speed.Horizontal.TScale", background=C["bg"],
                     troughcolor=C["surface"], bordercolor=C["surface"],
                     lightcolor=C["primary"], darkcolor=C["primary"],
                     sliderlength=30, sliderthickness=18)
        st.map("Speed.Horizontal.TScale",
               background=[("active", C["primary_hover"])])
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
            "split_enabled": self.opt_split_enabled.get(),
            "split_height": self._split_height(),
            "speed_idx": self.speed_idx.get(),
            "save_dir": self.save_dir.get(),
            "options_expanded": self.opt_expanded.get(),
        })
        save_cfg(self.cfg)

    def _split_height(self):
        try:
            v = int(str(self.split_height_var.get()).strip())
        except (TypeError, ValueError):
            v = 4000
        return max(50, v)

    def _validate_digits(self, proposed):
        return proposed == "" or proposed.isdigit()

    def _update_split_state(self):
        state = "normal" if self.opt_split_enabled.get() else "disabled"
        try:
            self.split_height_entry.config(state=state)
        except Exception:
            pass

    def _on_split_toggle(self):
        self._update_split_state()
        self._save_cfg()

    def _on_split_height_focusout(self, event=None):
        self.split_height_var.set(str(self._split_height()))
        self._save_cfg()

    def _toggle_topmost(self):
        self.root.attributes("-topmost", self.opt_on_top.get())
        self._save_cfg()

    # ---------- 옵션 영역 더보기/숨기기 ----------
    def _toggle_options(self):
        self.opt_expanded.set(not self.opt_expanded.get())
        self._apply_options_expanded()
        self._save_cfg()

    def _apply_options_expanded(self):
        try:
            self.opt_content.update_idletasks()
        except Exception:
            pass
        expanded = self.opt_expanded.get()
        if expanded:
            self.opt_clip.pack_propagate(True)
            self.opt_clip.configure(height=1)
        else:
            row_h = 24
            try:
                col1 = self.opt_content.winfo_children()[0].winfo_children()[0]
                row_h = max(16, col1.winfo_children()[0].winfo_reqheight())
            except Exception:
                pass
            self.opt_clip.pack_propagate(False)
            self.opt_clip.configure(height=int(row_h * 2) + 4)
        try:
            self.more_btn.set_text(
                self.t("btn_less") if expanded else self.t("btn_more"))
        except Exception:
            pass

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

    def _apply_hotkeys(self, capturing=False):
        binds = {m: (v[0], v[1]) for m, v in self.hotkeys.items() if v}
        if capturing:   # 캡처 중에만 Esc 를 전역 '취소' 키로 등록
            binds[CANCEL_MODE] = ESC_HOTKEY
        try:
            self.hotkeys_mgr.set_bindings(binds)
        except Exception:
            pass

    def _hotkey_fire(self, mode):
        """단축키 수신 → 메인 루프에서 처리.
        Windows: 단축키 스레드에서 호출 → root.after 로 메인 스레드에 넘김.
        macOS  : Carbon 콜백은 Tk 이벤트 처리 '도중'에 불리므로 여기서 Tk 를 호출하면
                 tkinter 내부 상태가 깨져 앱이 비정상 종료된다 → 큐에만 넣고 폴링으로 처리."""
        if IS_MAC:
            self._hk_queue.append(mode)
            return
        try:
            self.root.after(0, lambda m=mode: self._on_hotkey(m))
        except Exception:
            pass

    def _pump_hotkeys(self):
        """(macOS) 단축키 큐를 50ms 마다 메인 루프에서 처리."""
        self.root.after(50, self._pump_hotkeys)
        while self._hk_queue:
            self._on_hotkey(self._hk_queue.popleft())

    def _on_hotkey(self, mode):
        if mode == CANCEL_MODE:
            ov = _ACTIVE_OVERLAY[0]
            if ov is not None:          # 영역/모니터/창 선택 화면 취소
                LAST_OVERLAY_INFO["esc_via"] = "hotkey"
                ov()
            else:                       # 스크롤 캡처 취소
                self._panel_cancel()
        elif mode == SELFTEST_MODE:
            cb = getattr(self, "_selftest_hook", None)
            if cb:
                cb()
        else:
            self.start_capture(mode)

    def _open_hotkeys(self):
        # 편집 중에는 전역 단축키를 잠시 중지(입력하려는 조합이 캡처로 발동되는 것 방지)
        try:
            self.hotkeys_mgr.stop()
        except Exception:
            pass
        try:
            dlg = HotkeyDialog(self.root, self.t, self.hotkeys,
                               self._on_hotkeys_saved)
            self.root.wait_window(dlg.top)      # 창이 닫힐 때까지 대기
        finally:
            self._apply_hotkeys()               # 닫히면 전역 단축키 다시 등록

    def _on_hotkeys_saved(self, out):
        self.cfg["hotkeys"] = out
        save_cfg(self.cfg)
        self.hotkeys = self._load_hotkeys()
        # 재등록은 _open_hotkeys 의 wait_window 종료 후 일괄 처리

    def _on_close(self):
        try:
            self.hotkeys_mgr.stop()
        except Exception:
            pass
        self.root.destroy()

    # ---------- UI 구성 ----------
    def _titled_box(self, title, expand=False, pad=(12, 8)):
        """제목을 박스 '밖(위)'에 두고, 아래에 1px 테두리 박스를 만든 뒤
        내부 여백 프레임을 돌려준다(자식은 이 프레임에 배치)."""
        tk.Label(self.root, text=title, bg=MC["bg"], fg=MC["text"],
                 font=self.f_bold, anchor="w").pack(fill="x", padx=16, pady=(8, 2))
        box = tk.Frame(self.root, bg=MC["bg"], highlightthickness=1,
                       highlightbackground="#E5E5E5", highlightcolor="#E5E5E5")
        box.pack(fill="both" if expand else "x", expand=expand,
                 padx=14, pady=(0, 4))
        inner = tk.Frame(box, bg=MC["bg"])
        inner.pack(fill="both", expand=True, padx=pad[0], pady=pad[1])
        return inner

    def _build_ui(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.title(f"{APP_TITLE} v{APP_VERSION}")

        # 업데이트 알림 배너(사용 가능할 때만 표시)
        self.update_bar = tk.Frame(self.root, bg=MC["list_hover"])
        self.update_lbl = tk.Label(self.update_bar, bg=MC["list_hover"],
                                   fg=MC["primary_hover"], font=self.f_cap)
        self.update_lbl.pack(side="left", padx=12, pady=5)
        dlbtn = RoundButton(self.update_bar, text=self.t("btn_download"),
                            kind="primary", command=self._open_update)
        dlbtn.configure(bg=MC["list_hover"])
        dlbtn.pack(side="right", padx=10, pady=5)

        # 캡처 버튼 7개 (아이콘 + 툴팁). 한 줄로 가로 정렬.
        #  스크롤 3종(디스플레이/윈도우/영역) + 즉시 캡처 4종(전체/디스플레이/윈도우/영역)
        top = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        self.btn_bar = top
        self._icon_imgs = make_capture_icons(46)
        modes = CAPTURE_ICON_MODES
        self.cap_buttons = []
        for i, m in enumerate(modes):
            b = IconRoundButton(top, image=self._icon_imgs[m], height=58,
                                command=lambda mm=m: self.start_capture(mm))
            b.grid(row=0, column=i, sticky="nsew", padx=2)
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
        optf = self._titled_box(self.t("lf_options"), pad=(12, 8))

        # 접었을 때 높이(약 2.5줄)로 잘라 보여주는 클리핑 컨테이너
        self.opt_clip = ttk.Frame(optf)
        self.opt_clip.pack(fill="x")
        opt_content = ttk.Frame(self.opt_clip)
        opt_content.pack(fill="x", anchor="n")
        self.opt_content = opt_content

        # 텍스트 옵션 2칸
        cols = ttk.Frame(opt_content)
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

        # 높이 분할 자르기(체크 시 아래 높이 입력 필드 활성화)
        ttk.Checkbutton(col1, text=self.t("opt_split"),
                        variable=self.opt_split_enabled,
                        command=self._on_split_toggle).pack(anchor="w")
        splitf = ttk.Frame(col1)
        splitf.pack(anchor="w", fill="x", padx=(20, 0))
        ttk.Label(splitf, text=self.t("lbl_split_height")).pack(side="left")
        vcmd = (self.root.register(self._validate_digits), "%P")
        self.split_height_entry = ttk.Entry(
            splitf, width=6, justify="right", textvariable=self.split_height_var,
            validate="key", validatecommand=vcmd)
        self.split_height_entry.pack(side="left", padx=(6, 2))
        ttk.Label(splitf, text="px").pack(side="left")
        self.split_height_entry.bind("<FocusOut>", self._on_split_height_focusout)
        self._update_split_state()

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
        RoundButton(col2, text=self.t("btn_hotkeys"), kind="secondary",
                    command=self._open_hotkeys).pack(anchor="w", pady=(6, 0))
        # 기본값 복원 버튼 (업데이트 확인 위) — 모든 옵션을 기본값으로
        RoundButton(col2, text=self.t("btn_reset_all"), kind="secondary",
                    command=self._reset_all).pack(anchor="w", pady=(4, 0))
        if GITHUB_REPO and updater is not None:
            RoundButton(col2, text=self.t("btn_check_update"), kind="secondary",
                        command=self._check_update_manual).pack(anchor="w", pady=(4, 0))

        # 스크롤 속도 (옵션 영역 내)
        spd = ttk.Frame(opt_content)
        spd.pack(fill="x", pady=(6, 0))
        ttk.Label(spd, text=self.t("lbl_speed")).pack(side="left")
        self.speed_lbl = ttk.Label(spd, width=4, style="Primary.TLabel")
        self.speed_lbl.pack(side="left", padx=(6, 6))
        ttk.Label(spd, text=self.t("speed_hint"),
                  style="Muted.TLabel").pack(side="left")
        self.speed_scale = SpeedSlider(
            opt_content, SPEED_VALUES, self.speed_idx, on_change=self._on_speed,
            height=14)
        self.speed_scale.pack(fill="x", pady=(4, 0))
        # 배속 눈금 라벨(가독성) — 슬라이더 아래 균등 배치, 현재 값은 강조
        self.speed_ticks = ttk.Frame(opt_content)
        self.speed_ticks.pack(fill="x")
        self._tick_lbls = []
        for i, v in enumerate(SPEED_VALUES):
            lb = ttk.Label(self.speed_ticks, text=("%gx" % v),
                           style="Muted.TLabel", anchor="center")
            lb.pack(side="left", expand=True, fill="x")
            self._tick_lbls.append(lb)
        self._update_speed_label()

        # 더보기/숨기기 버튼(옵션 영역 하단 가운데) — 펼치면 옵션 영역이 늘어나고
        # 아래 파일 목록 영역이 줄어든다.
        morebar = ttk.Frame(optf)
        morebar.pack(fill="x", pady=(4, 0))
        self.more_btn = RoundButton(
            morebar, kind="secondary", command=self._toggle_options,
            text=self.t("btn_less") if self.opt_expanded.get() else self.t("btn_more"))
        self.more_btn.pack(anchor="center")
        self.root.after_idle(self._apply_options_expanded)

        # 저장 폴더
        dirf = self._titled_box(self.t("lf_save_folder"), pad=(12, 8))
        ttk.Entry(dirf, textvariable=self.save_dir).pack(
            side="left", fill="x", expand=True)
        RoundButton(dirf, text=self.t("btn_change"), kind="secondary",
                    min_width=52, command=self.change_dir).pack(
                        side="left", padx=(8, 0))
        RoundButton(dirf, text=self.t("btn_open"), kind="secondary",
                    min_width=52, command=self.open_dir).pack(
                        side="left", padx=(4, 0))

        # 상태
        if not self.status.get():
            self.status.set(self.t("status_ready"))
        ttk.Label(self.root, textvariable=self.status, style="Primary.TLabel",
                  wraplength=470).pack(fill="x", padx=14, pady=(0, 4))

        # 파일 목록
        listf = self._titled_box(self.t("lf_captured"), expand=True, pad=(10, 8))
        head = ttk.Frame(listf)
        head.pack(fill="x")
        # 새로고침: 아래 열기/복사 버튼과 오른쪽 끝이 맞도록 스크롤바 폭만큼 띄움
        RoundButton(head, text=self.t("btn_refresh"), kind="secondary",
                    min_width=52, command=self.refresh_list).pack(
                        side="right", padx=(0, 8))

        canvas = tk.Canvas(listf, borderwidth=0, highlightthickness=0,
                           bg=MC["bg"], height=345)   # 약 4개 항목이 보이는 높이
        vsb = tk.Scrollbar(listf, orient="vertical", command=canvas.yview,
                           width=12, bd=0, relief="flat",
                           troughcolor=MC["surface"], bg=MC["thumb"],
                           activebackground=MC["thumb_hover"],
                           highlightthickness=0, elementborderwidth=0)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.list_inner = tk.Frame(canvas, bg=MC["bg"])
        self._win = canvas.create_window((0, 0), window=self.list_inner, anchor="nw")
        self.list_inner.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(self._win, width=e.width))
        if IS_MAC:   # macOS 는 delta 가 ±1~수 단위(트랙패드 포함)
            canvas.bind_all(
                "<MouseWheel>", lambda e: canvas.yview_scroll(-e.delta, "units"))
        else:
            canvas.bind_all(
                "<MouseWheel>",
                lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
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
        self.speed_lbl.config(text=("%gx" % v))
        # 현재 배속 눈금만 강조(파란색), 나머지는 흐린 회색
        idx = self.speed_idx.get()
        for i, lb in enumerate(getattr(self, "_tick_lbls", [])):
            try:
                lb.config(style="Primary.TLabel" if i == idx else "Muted.TLabel")
            except Exception:
                pass

    def _on_speed(self, _=None):
        self._update_speed_label()
        self._save_cfg()

    def _on_speed_scale(self, v):
        """ttk.Scale(연속값)을 정수 인덱스로 스냅."""
        i = max(0, min(len(SPEED_VALUES) - 1, int(round(float(v)))))
        if i != self.speed_idx.get():
            self.speed_idx.set(i)
        if abs(float(v) - i) > 1e-6:
            self.speed_scale.set(i)          # 눈금에 딱 맞춤
        self._update_speed_label()
        self._save_cfg()

    def _reset_speed(self):
        self.speed_idx.set(SPEED_DEFAULT_IDX)
        self._update_speed_label()
        self._save_cfg()

    def _reset_all(self):
        """옵션의 모든 값(체크박스·스크롤 속도·단축키·언어)을 기본값으로 복원."""
        if not messagebox.askyesno(self.t("info_title"), self.t("confirm_reset")):
            return
        d = DEFAULT_CFG
        self.opt_scroll_top.set(d["scroll_top"])
        self.opt_multi.set(d["multi_monitor"])
        self.opt_hide.set(d["hide_app"])
        self.opt_sticky.set(d["sticky"])
        self.opt_hide_mouse.set(d["hide_mouse"])
        self.opt_auto_copy.set(d["auto_copy"])
        self.opt_split_enabled.set(d["split_enabled"])
        self.split_height_var.set(str(d["split_height"]))
        self.opt_on_top.set(d["on_top"])
        self.opt_expanded.set(d["options_expanded"])
        self.speed_idx.set(SPEED_DEFAULT_IDX)
        self.cfg["panel_pos"] = d["panel_pos"]   # 떠 있는 패널 위치도 초기화
        self.cfg["lang"] = d["lang"]
        self.cfg["hotkeys"] = {}                 # 단축키도 기본값으로
        self.hotkeys = self._load_hotkeys()
        self._save_cfg()
        self._apply_hotkeys()
        self.root.attributes("-topmost", self.opt_on_top.get())
        self._build_ui()                         # 언어 등 즉시 반영
        self.refresh_list()
        self.status.set(self.t("reset_done"))

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
        def apply():
            self.status.set(text)
            p = getattr(self, "_panel", None)
            if p is not None and not (self._ctl and (self._ctl.stop.is_set()
                                                     or self._ctl.cancel.is_set())):
                p.set_info(text)
        self.root.after(0, apply)

    def change_dir(self):
        d = filedialog.askdirectory(initialdir=self.save_dir.get())
        if d:
            self.save_dir.set(d)
            self._save_cfg()
            self.refresh_list()

    def open_dir(self):
        d = self.save_dir.get()
        os.makedirs(d, exist_ok=True)
        open_path(d)

    # ---------- 캡처 ----------
    def _set_busy(self, busy):
        self.busy = busy
        for b in self.cap_buttons:
            try:
                b.set_enabled(not busy)
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
        if self.busy or self._starting:
            return
        self._starting = True
        try:
            self._start_capture(mode)
        finally:
            self._starting = False

    def _pick_target(self, mode, frozen):
        """캡처 대상 선택(영역/모니터/창/전체) → (region, hwnd)."""
        region = None
        hwnd = None
        if mode in ("scroll_region", "shot_region"):
            region = RegionSelector(self.root, multi=self.opt_multi.get(),
                                    guide=self.t("guide_region"),
                                    frozen=frozen).result
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
        return region, hwnd

    def _start_capture(self, mode):
        self._hidden = self.opt_hide.get()
        # 캡처를 누른 '그 순간'의 화면을 가장 먼저 고정한다(마우스 오버 메뉴·영상 프레임 유지).
        # 앱 창은 숨기면 지연이 생기므로, 찍는 순간에만 캡처에서 제외한다.
        frozen = None
        excl = self._hidden and self.root.state() == "normal"
        try:
            if excl:
                set_main_capture_excluded(self.root, True)
                time.sleep(0.05)
            frozen = freeze_screen()
        except Exception:
            frozen = None
        finally:
            if excl:
                set_main_capture_excluded(self.root, False)
        if IS_MAC:   # 단축키로 백그라운드에서 호출돼도 오버레이가 클릭을 받도록
            pm.activate_self()
        if self._hidden:
            self.root.withdraw()
            self.root.update()
        time.sleep(0.15 if frozen is None or mode.startswith("scroll_") else 0.05)

        # ---- 대상 영역 선택 ----  (선택 중 Esc 는 전역 키로도 받아 취소)
        self._apply_hotkeys(capturing=True)
        try:
            region, hwnd = self._pick_target(mode, frozen)
        finally:
            self._apply_hotkeys()

        if not region:
            if self._hidden:
                self.root.deiconify()
            self.status.set(self.t("status_cancelled"))
            return

        self._set_busy(True)

        # ---- 즉시 캡처(스크롤 없음) ----
        if mode.startswith("shot_"):
            self.status.set(self.t("status_capturing"))
            threading.Thread(target=self._worker_shot,
                             args=(region, hwnd, frozen), daemon=True).start()
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
        if IS_MAC:
            opts["strings"]["err_no_scroll"] = self.t("err_no_scroll_mac")
        # 중지/취소: 떠 있는 패널 + 전역 Esc
        self._ctl = CaptureControl()
        opts["control"] = self._ctl
        try:
            self._panel = CapturePanel(
                self.root, self.t, region, self._panel_stop, self._panel_cancel,
                pos=self.cfg.get("panel_pos"), on_moved=self._panel_moved)
        except Exception:
            self._panel = None
        self._apply_hotkeys(capturing=True)
        threading.Thread(target=self._worker, args=(region, opts),
                         daemon=True).start()

    # ---------- 캡처 중지 / 취소 ----------
    def _panel_stop(self):
        ctl = getattr(self, "_ctl", None)
        if ctl is None or ctl.cancel.is_set():
            return
        ctl.stop.set()
        if self._panel:
            self._panel.lock(self.t("status_stopping"))
        self.status.set(self.t("status_stopping"))

    def _panel_cancel(self):
        ctl = getattr(self, "_ctl", None)
        if ctl is None:
            return
        ctl.cancel.set()
        if self._panel:
            self._panel.lock(self.t("status_cancelling"))
        self.status.set(self.t("status_cancelling"))

    def _panel_moved(self, x, y):
        self.cfg["panel_pos"] = [int(x), int(y)]
        save_cfg(self.cfg)

    def _end_capture_controls(self):
        """패널 닫기 + Esc 전역 키 해제 (여러 번 불려도 안전)."""
        p = getattr(self, "_panel", None)
        self._panel = None
        if p is not None:
            p.close()
        if getattr(self, "_ctl", None) is not None:
            self._ctl = None
            self._apply_hotkeys()

    def _cancelled(self, early_stop=False):
        self._end_capture_controls()
        self._close_busy()
        self._set_busy(False)
        if getattr(self, "_hidden", True):
            self.root.deiconify()
        else:
            self._show_progress(False)
        # 준비 단계(맨 위 이동/고정 영역 감지)에서 [중지] → 이어붙인 결과가 없어 저장 안 함
        self.status.set(self.t("status_stopped_early" if early_stop
                               else "status_cancelled"))

    def _worker(self, region, opts):
        try:
            time.sleep(0.2)
            img = capture_scrolling_region(region, self.set_status, opts)
            ctl = opts.get("control")
            stopped = bool(ctl is not None and ctl.stopped)
            if self.opt_split_enabled.get():
                paths = save_png_split(img, self.save_dir.get(), self._split_height())
                self.root.after(0, lambda: self._done_split(paths, img, stopped))
            else:
                path = save_png(img, self.save_dir.get())
                self.root.after(0, lambda: self._done(path, stopped, img))
        except CaptureCancelled:
            ctl = opts.get("control")
            early = bool(ctl is not None and ctl.stop.is_set()
                         and not ctl.cancel.is_set())
            self.root.after(0, lambda: self._cancelled(early))
        except Exception as e:
            m = str(e)
            self.root.after(0, lambda: self._error(m))

    def _worker_shot(self, region, hwnd=None, frozen=None):
        """스크롤 없이 한 번만 캡처(디스플레이/윈도우/영역/전체).
        캡처를 누른 순간의 정지 화면이 있으면 거기서 잘라낸다(그 순간 그대로)."""
        try:
            img = None
            if frozen is not None:
                try:
                    img = crop_frozen(frozen, region)
                except Exception:
                    img = None
            if img is None:
                time.sleep(0.2)
            if img is None and hwnd and IS_MAC:   # macOS: 창만 직접 캡처
                img = pm.grab_window(hwnd)
            if img is None:
                if hwnd:                   # 윈도우 캡처: 대상 창을 앞으로
                    ensure_foreground(hwnd)
                    time.sleep(0.35)
                with screen_grabber() as sct:
                    img = grab(sct, region)
            path = save_png(img, self.save_dir.get())
            self.root.after(0, lambda: self._done(path, False, img))
        except Exception as e:
            m = str(e)
            self.root.after(0, lambda: self._error(m))

    # 캡처 종료(그랩 끝) → 합성/저장 중 '진행중' 오버레이 표시
    def _on_processing(self):
        self.root.after(0, self._show_busy)

    def _show_busy(self):
        self._end_capture_controls()      # 합성/저장 단계부터는 중지·취소 불가
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

    def _finish_ui(self):
        self._end_capture_controls()
        self._close_busy()
        self._set_busy(False)
        if getattr(self, "_hidden", True):
            self.root.deiconify()
        else:
            # 노출 모드: 진행 바를 100%로 채우고 잠시 후 숨김
            self._set_progress(100)
            self.root.after(3000, lambda: self._show_progress(False))

    def _done(self, path, stopped=False, img=None):
        self._finish_ui()
        done = self.t("status_stopped_fmt" if stopped else "status_done_fmt",
                      name=os.path.basename(path))
        self.status.set(done)
        self._prepend_files([path], img)            # 목록: 새 파일만 맨 위에 추가
        if self.opt_auto_copy.get():                 # 클립보드 복사(옵션)
            self._copy_async(done, path=path, img=img)

    def _done_split(self, paths, img=None, stopped=False):
        """높이 분할 자르기로 여러 파일이 생성된 경우의 완료 처리.
        클립보드에는 분할 전 '원본 전체' 이미지를 복사한다."""
        self._finish_ui()
        done = self.t("status_stopped_split_fmt" if stopped
                      else "status_done_split_fmt", n=len(paths))
        self.status.set(done)
        self._prepend_files(list(reversed(paths)), None)
        if img is not None and self.opt_auto_copy.get():
            self._copy_async(done, img=img)

    def _copy_async(self, done_text, path=None, img=None):
        """클립보드 복사. 큰 이미지는 변환·전달에 시간이 걸리므로 Windows 는
        백그라운드 스레드에서 처리(창이 멈추지 않게). macOS 는 메인 스레드(빠름)."""
        def work():
            ok = (copy_ndarray_to_clipboard(img) if img is not None
                  else copy_image_to_clipboard(path))
            return ok

        def finish(ok):
            if ok and self.status.get() == done_text:
                self.status.set(f"{done_text}  ·  {self.t('copied_status')}")

        if IS_MAC:
            finish(work())
            return

        def bg():
            ok = work()
            try:
                self.root.after(0, lambda: finish(ok))
            except Exception:
                pass

        threading.Thread(target=bg, daemon=True).start()

    def _error(self, msg):
        self._end_capture_controls()
        self._close_busy()
        self._set_busy(False)
        if getattr(self, "_hidden", True):
            self.root.deiconify()
        else:
            self._show_progress(False)
        self.status.set(self.t("status_error"))
        messagebox.showwarning(self.t("info_title"), msg)

    # ---------- 파일 목록 ----------
    LIST_MAX = 60
    THUMB_SIZE = (120, 80)

    def _thumb_cached(self, path):
        """캐시된 썸네일(PIL) — 파일이 바뀌지 않았으면 다시 읽지 않는다."""
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return None
        v = self._thumb_cache.get(path)
        return v[1] if (v and v[0] == mt) else None

    def _thumb_make(self, path, img=None):
        """썸네일 생성(캐시 저장). img(BGR 배열)가 있으면 파일을 다시 읽지 않음."""
        try:
            mt = os.path.getmtime(path)
            if img is not None:
                h, w = img.shape[:2]
                k = max(1, int(max(w / self.THUMB_SIZE[0], h / self.THUMB_SIZE[1])))
                small = img[::k, ::k]
                im = Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
            else:
                im = Image.open(path)
                im.draft("RGB", self.THUMB_SIZE)
            im.thumbnail(self.THUMB_SIZE)
            im = im.convert("RGB")
            self._thumb_cache[path] = (mt, im)
            return im
        except Exception:
            return None

    def _load_thumbs_async(self, pending):
        """캐시에 없는 썸네일을 백그라운드에서 만들고, 다 되면 화면에 반영."""
        if not pending:
            return
        gen = self._list_gen

        def bg():
            made = [(p, lbl, self._thumb_make(p)) for p, lbl in pending]

            def apply():
                if gen != self._list_gen:        # 그사이 목록이 다시 그려짐
                    return
                for p, lbl, im in made:
                    if im is None:
                        continue
                    try:
                        photo = ImageTk.PhotoImage(im)
                        self._thumbs.append(photo)
                        lbl.config(image=photo, text="", width=0, height=0)
                    except Exception:
                        pass
            try:
                self.root.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=bg, daemon=True).start()

    def _prepend_files(self, paths, img=None):
        """새로 저장된 파일만 목록 맨 위에 추가(전체 다시 읽기 없음)."""
        rows = [w for w in self.list_inner.winfo_children()
                if getattr(w, "_sc_row", False)]
        if not rows:                 # 비어 있음/폴더 없음 안내 상태 → 새로 그림
            self.refresh_list()
            return
        if img is not None and len(paths) == 1:
            self._thumb_make(paths[0], img)
        pending = []
        for path in paths:
            lbl = self._add_row(path, top=True)
            if lbl is not None:
                pending.append((path, lbl))
        self._load_thumbs_async(pending)
        # 최대 개수 초과분(가장 오래된 것) 제거
        rows = [w for w in self.list_inner.winfo_children()
                if getattr(w, "_sc_row", False)]
        for r in rows[self.LIST_MAX:]:
            try:
                r._sc_sep.destroy()
                r.destroy()
            except Exception:
                pass

    def refresh_list(self):
        self._list_gen = getattr(self, "_list_gen", 0) + 1
        if not hasattr(self, "_thumb_cache"):
            self._thumb_cache = {}
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
        pending = []
        for path in files[:self.LIST_MAX]:
            lbl = self._add_row(path)
            if lbl is not None:
                pending.append((path, lbl))
        self._load_thumbs_async(pending)       # 썸네일은 백그라운드에서

    def _add_row(self, path, top=False):
        """목록 한 줄 추가. 썸네일이 캐시에 없으면 자리만 만들고 그 Label 을 돌려준다
        (나중에 백그라운드에서 채움). top=True 면 목록 맨 위에 넣는다."""
        C = MC
        if not hasattr(self, "_thumb_cache"):
            self._thumb_cache = {}
        kids = self.list_inner.winfo_children()
        first = kids[0] if (top and kids) else None
        row = tk.Frame(self.list_inner, bg=C["white"])
        if first is not None:
            row.pack(fill="x", before=first)
        else:
            row.pack(fill="x")
        row._sc_row = True
        inner = tk.Frame(row, bg=C["white"])
        inner.pack(fill="x", padx=4, pady=4)
        pending = None
        im = self._thumb_cached(path)
        if im is not None:
            photo = ImageTk.PhotoImage(im)
            self._thumbs.append(photo)
            thumb = tk.Label(inner, image=photo, bg=C["white"])
        else:
            thumb = tk.Label(inner, text="…", width=12, height=4, bg=C["white"],
                             fg=C["muted"])
            pending = thumb
        thumb.pack(side="left")
        # 열기/복사 버튼(오른쪽 먼저 배치해 폭을 확보 → 파일명이 침범 못 함)
        btns = tk.Frame(inner, bg=C["white"])
        btns.pack(side="right")
        obtn = RoundButton(btns, text=self.t("btn_open_item"), kind="secondary",
                           min_width=52, command=lambda p=path: open_path(p))
        obtn.pack(anchor="e")
        copy_btn = RoundButton(btns, text=self.t("btn_copy"), kind="secondary",
                               min_width=52)
        copy_btn.command = (lambda p=path, b=copy_btn: self._copy_image(p, b))
        copy_btn.pack(anchor="e", pady=(4, 0))
        del_btn = RoundButton(btns, text=self.t("btn_delete"), kind="danger",
                              min_width=52,
                              command=lambda p=path: self._delete_image(p))
        del_btn.pack(anchor="e", pady=(4, 0))
        # 파일 정보(남은 공간) — 파일명은 .png 앞에서 말줄임
        try:
            with Image.open(path) as im2:
                w, h = im2.size
        except Exception:
            w = h = 0
        mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime(
            "%Y-%m-%d %H:%M:%S")
        info = tk.Frame(inner, bg=C["white"])
        info.pack(side="left", fill="x", expand=True, padx=10)
        fullname = os.path.basename(path)
        l1 = tk.Label(info, text=fullname, bg=C["white"], fg=C["text"],
                      font=self.f_bold, anchor="w")
        l1.pack(anchor="w", fill="x")
        l1.bind("<Configure>",
                lambda e, lb=l1, nm=fullname: self._fit_name(lb, nm))
        l2 = tk.Label(info, text=f"{w} x {h} px", bg=C["white"],
                      fg=C["muted"], font=self.f_cap, anchor="w")
        l2.pack(anchor="w")
        l3 = tk.Label(info, text=mtime, bg=C["white"], fg=C["muted"],
                      font=self.f_cap, anchor="w")
        l3.pack(anchor="w")

        # 하단 구분선
        sep = tk.Frame(self.list_inner, bg=C["border"], height=1)
        sep.pack(fill="x", after=row)
        row._sc_sep = sep

        # Hover(연블루) — 항목의 tk 위젯 배경 전환(라운드 버튼 여백도 함께)
        hover_widgets = [row, inner, thumb, info, l1, l2, l3, btns,
                         obtn, copy_btn, del_btn]

        def set_bg(color):
            for wd in hover_widgets:
                try:
                    wd.config(bg=color)
                except Exception:
                    pass

        for wd in (row, inner, thumb, info, l1, l2, l3):
            wd.bind("<Enter>", lambda e: set_bg(C["list_hover"]))
            wd.bind("<Leave>", lambda e: set_bg(C["white"]))
            wd.bind("<Double-Button-1>", lambda e, p=path: open_path(p))
        return pending

    def _fit_name(self, label, fullname):
        """이름 표시 폭에 맞춰, 넘칠 때만 확장자(.png)는 남기고 … 로 줄임.
        창 가로가 넓어지면 원래 이름 전체가 다시 보인다."""
        try:
            avail = label.winfo_width()
        except Exception:
            return
        if avail <= 1:
            return
        f = tkfont.Font(font=label.cget("font"))
        if f.measure(fullname) <= avail:
            if label.cget("text") != fullname:
                label.config(text=fullname)
            return
        root, ext = os.path.splitext(fullname)
        ell = "…"
        best = ell + ext
        lo, hi = 0, len(root)
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = root[:mid] + ell + ext
            if f.measure(cand) <= avail:
                best = cand
                lo = mid + 1
            else:
                hi = mid - 1
        if label.cget("text") != best:
            label.config(text=best)

    def _copy_image(self, path, btn):
        ok = copy_image_to_clipboard(path)
        if ok:
            btn.set_text(self.t("copied_mark"))
            self.root.after(3000, lambda: self._restore_copy_btn(btn))
        else:
            messagebox.showwarning(self.t("info_title"), self.t("copy_fail"))

    def _delete_image(self, path):
        name = os.path.basename(path)
        if not messagebox.askyesno(self.t("info_title"),
                                   self.t("confirm_delete_fmt", name=name)):
            return
        try:
            os.remove(path)
        except Exception:
            messagebox.showwarning(self.t("info_title"), self.t("delete_fail"))
        getattr(self, "_thumb_cache", {}).pop(path, None)
        self.refresh_list()

    def _restore_copy_btn(self, btn):
        try:
            btn.set_text(self.t("btn_copy"))
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
            msg = self.t("update_fail")
            why = getattr(updater, "last_error", "")
            if why:
                msg = f"{msg}\n\n({why})"
            messagebox.showwarning(self.t("info_title"), msg)
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
    if IS_MAC:
        try:
            with open(path, "rb") as f:
                return pm.copy_png_bytes(f.read())
        except Exception:
            return False
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


def copy_ndarray_to_clipboard(img_bgr):
    """메모리의 이미지(BGR ndarray)를 클립보드에 이미지(CF_DIB)로 복사.
    분할 저장 시 파일로 나뉜 원본 '전체'를 그대로 복사하는 데 사용."""
    if IS_MAC:
        ok, buf = cv2.imencode(".png", img_bgr)
        big = img_bgr.shape[0] * img_bgr.shape[1] > 12_000_000
        return bool(ok) and pm.copy_png_bytes(buf.tobytes(), with_tiff=not big)
    try:
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        out = io.BytesIO()
        Image.fromarray(rgb).save(out, "BMP")
        data = out.getvalue()[14:]  # BMP 파일 헤더 제거 → DIB
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


class _SelftestLog(list):
    """자체 점검 결과: 한 줄 추가할 때마다 파일에 즉시 기록(멈추거나 죽어도 남도록)."""

    def __init__(self, path):
        super().__init__()
        self.path = path

    def append(self, line):
        super().append(line)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("\n".join(self) + "\n")
        except Exception:
            pass
        print(line, flush=True)


def _selftest(app, out_path):
    """빌드 검증용(CI): UI 생성 후 플랫폼 기능을 점검하고 결과를 기록한 뒤 종료.
    SCROLLCAPTURE_SELFTEST=<결과 파일 경로> 환경변수로 실행."""
    lines, failed = _SelftestLog(out_path), False

    def check(name, fn):
        nonlocal failed
        try:
            lines.append(f"OK   {name}: {fn()}")
        except Exception as e:
            failed = True
            lines.append(f"FAIL {name}: {type(e).__name__}: {e}")

    check("platform", lambda: sys.platform)
    check("monitors", get_monitors)
    check("primary", primary_screen)
    check("virtual", virtual_screen)

    def _grab():
        x, y, w, h = primary_screen()
        with screen_grabber() as sct:
            img = grab(sct, (x + 10, y + 40, 200, 120))
        return img.shape

    check("grab", _grab)

    def _freeze():
        x, y, w, h = primary_screen()
        return crop_frozen(freeze_screen(), (x + 10, y + 40, 200, 120)).shape

    check("freeze_crop", _freeze)
    check("icons", lambda: len(app._icon_imgs))
    check("list_rows", lambda: len(app.list_inner.winfo_children()))
    check("hotkeys", lambda: {m: v[2] for m, v in app.hotkeys.items() if v})

    def _panel():
        pnl = CapturePanel(app.root, app.t, (0, 0, 200, 200),
                           lambda: None, lambda: None)
        geo = (pnl.top.winfo_width(), pnl.top.winfo_height())
        pnl.close()
        return geo

    check("capture_panel", _panel)

    def _esc_hotkey():
        app._ctl = CaptureControl()
        app._apply_hotkeys(capturing=True)
        n = len(app.hotkeys_mgr.bindings)
        app._end_capture_controls()
        return f"{n} -> {len(app.hotkeys_mgr.bindings)}"

    check("esc_hotkey(bind)", _esc_hotkey)
    check("region_backdrop", lambda: RegionSelector._backdrop(
        RegionSelector.__new__(RegionSelector), app.root,
        *primary_screen())[1] is not None)
    if IS_MAC:
        check("hotkeys_registered", lambda: len(app.hotkeys_mgr._refs))
        check("permissions(screen,ax)", pm.permissions_status)
        check("window_at_center", lambda: pm.window_at_point(
            primary_screen()[2] // 2, primary_screen()[3] // 2))
        check("clipboard", lambda: copy_ndarray_to_clipboard(
            np.full((20, 30, 3), 200, np.uint8)))

    # 업데이트 확인용 HTTPS 접속(인증서 검증). 기본 컨텍스트 결과는 참고용(INFO)
    def _https(ctx):
        import urllib.request
        with urllib.request.urlopen("https://github.com", timeout=10,
                                    context=ctx) as r:
            return r.status

    if updater is not None:
        import ssl
        try:
            lines.append("INFO https(default ssl): "
                         f"{_https(ssl.create_default_context())}")
        except Exception as e:
            lines.append(f"INFO https(default ssl): {type(e).__name__}: "
                         f"{str(e)[:120]}")
        check("https(updater ssl)", lambda: _https(updater.ssl_context()))
        p = ssl.get_default_verify_paths()
        lines.append(f"INFO ssl default cafile: {p.openssl_cafile} "
                     f"exists={os.path.exists(p.openssl_cafile)}")
        check("update_check", lambda: (lambda i: i["tag"] if i else
                                       updater.last_error)(
            updater.check_latest(GITHUB_REPO, APP_VERSION)))

    # 사용 중인 PC 에서는 마우스/키 입력을 흉내 내는 점검을 건너뛸 수 있음
    if os.environ.get("SCROLLCAPTURE_SELFTEST_NOINPUT"):
        lines.append("INFO input tests skipped (SCROLLCAPTURE_SELFTEST_NOINPUT)")
        _selftest_finish(app, out_path, lines, failed)
        return

    # 실제 키(F19)를 보내 전역 단축키 → 메인 루프 처리까지 확인
    # (예전 macOS 크래시: 단축키 콜백에서 Tk 호출 → 다음 after 콜백에서 abort)
    got = []
    app._selftest_hook = lambda: got.append(1)
    try:
        binds = {m: (v[0], v[1]) for m, v in app.hotkeys.items() if v}
        binds[SELFTEST_MODE] = SELFTEST_HOTKEY
        app.hotkeys_mgr.set_bindings(binds)
    except Exception as e:
        lines.append(f"FAIL hotkey_bind: {e}")
        failed = True

    def send_key():
        try:
            if IS_MAC:
                pm.post_key(SELFTEST_HOTKEY[1])
            else:
                win32api.keybd_event(SELFTEST_HOTKEY[1], 0, 0, 0)
                time.sleep(0.03)
                win32api.keybd_event(SELFTEST_HOTKEY[1], 0,
                                     win32con.KEYEVENTF_KEYUP, 0)
        except Exception as e:
            lines.append(f"FAIL hotkey_send: {e}")

    def wait_key(n=0):
        if not got and n < 40:
            app.root.after(100, lambda: wait_key(n + 1))
            return
        # 단축키 처리 뒤에도 after 콜백이 정상 동작하는지(크래시 재현 지점)
        app.root.after(10, lambda: app.root.after(10, finish))

    def finish():
        check("hotkey_fired(F19)", lambda: bool(got) or (_ for _ in ()).throw(
            RuntimeError("hotkey not received")))
        app.root.after(200, region_drag_test)

    # ---- 영역 지정: 실제 마우스 이동·드래그 → 저장 파일 크기 확인 ----
    import tempfile
    import glob
    tmp = tempfile.mkdtemp(prefix="sc_selftest_")
    app.save_dir.set(tmp)
    app.opt_auto_copy.set(False)
    app.opt_multi.set(False)
    app.opt_hide.set(True)
    px, py, _pw, _ph = primary_screen()
    X1, Y1, X2, Y2 = px + 200, py + 200, px + 500, py + 400

    def mouse(kind, x, y):
        if IS_MAC:
            pm.post_mouse(kind, x, y)
            return
        win32api.SetCursorPos((x, y))
        if kind == "down":
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        elif kind == "up":
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def press_esc():
        if IS_MAC:
            pm.post_key(53)
        else:
            win32api.keybd_event(0x1B, 0, 0, 0)
            time.sleep(0.03)
            win32api.keybd_event(0x1B, 0, win32con.KEYEVENTF_KEYUP, 0)

    def force_close():          # 선택 화면이 반응하지 않을 때의 안전장치
        lines.append("FAIL overlay: did not respond (forced close)")
        if _ACTIVE_OVERLAY[0]:
            _ACTIVE_OVERLAY[0]()

    def region_drag_test():
        def drive():
            time.sleep(2.0)
            mouse("move", X1, Y1)
            time.sleep(0.3)
            mouse("down", X1, Y1)
            time.sleep(0.2)
            for i in range(1, 11):
                mouse("drag", X1 + (X2 - X1) * i // 10, Y1 + (Y2 - Y1) * i // 10)
                time.sleep(0.05)
            time.sleep(0.3)
            mouse("up", X2, Y2)

        threading.Thread(target=drive, daemon=True).start()
        guard = app.root.after(12000, force_close)
        lines.append("STEP region_drag: start")
        app.start_capture("shot_region")          # 선택이 끝날 때까지 대기
        app.root.after_cancel(guard)
        lines.append("STEP region_drag: selection returned")
        lines.append(f"INFO overlay: {dict(LAST_OVERLAY_INFO)}")
        wait_file()

    def wait_file(n=0):
        files = glob.glob(os.path.join(tmp, "*.png"))
        if not files and n < 50:
            app.root.after(200, lambda: wait_file(n + 1))
            return

        def size():
            if not files:
                raise RuntimeError("no file saved")
            with Image.open(files[0]) as im:
                w, h = im.size
            sc = max(1, round(w / float(X2 - X1)))
            if abs(w - (X2 - X1) * sc) > 2 * sc or abs(h - (Y2 - Y1) * sc) > 2 * sc:
                raise RuntimeError(f"unexpected size {w}x{h}")
            return f"{w}x{h} (scale {sc})"

        check("region_drag_capture", size)
        app.root.after(500, esc_test)

    # ---- 영역 지정 중 Esc → 취소(파일 없음) ----
    def esc_test():
        n_before = len(glob.glob(os.path.join(tmp, "*.png")))
        threading.Thread(target=lambda: (time.sleep(2.0), press_esc()),
                         daemon=True).start()
        guard = app.root.after(10000, force_close)
        t0 = time.time()
        lines.append("STEP esc: start")
        app.start_capture("shot_region")
        app.root.after_cancel(guard)
        dt = time.time() - t0
        n_after = len(glob.glob(os.path.join(tmp, "*.png")))
        lines.append(f"INFO esc: via={LAST_OVERLAY_INFO.get('esc_via')} "
                     f"hk_err={getattr(pm, 'LAST_HOTKEY_ERRORS', {}).get(CANCEL_MODE) if IS_MAC else '-'}")
        check("esc_cancel", lambda: f"{dt:.1f}s" if (n_after == n_before and dt < 9)
              else (_ for _ in ()).throw(RuntimeError(f"not cancelled ({dt:.1f}s)")))
        _selftest_finish(app, out_path, lines, failed)

    app.root.after(300, send_key)
    app.root.after(400, wait_key)


def _selftest_finish(app, out_path, lines, failed):
    failed = failed or any(ln.startswith("FAIL") for ln in lines)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    app.hotkeys_mgr.stop()
    app.root.destroy()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    _st = os.environ.get("SCROLLCAPTURE_SELFTEST")
    if _st:
        os.environ["SCROLLCAPTURE_NO_PERM_PROMPT"] = "1"
        import faulthandler           # 멈추거나 죽으면 호출 위치를 stderr 로 출력
        faulthandler.enable()
        faulthandler.dump_traceback_later(100, exit=True)
    app = App()
    if _st:
        app.root.after(2500, lambda: _selftest(app, _st))
    app.run()
