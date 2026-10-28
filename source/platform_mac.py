# -*- coding: utf-8 -*-
"""
macOS 백엔드 (macOS 12 Monterey / 13 Ventura 이상)

Windows 의 pywin32 기능을 macOS 의 Quartz / AppKit / Accessibility / Carbon 으로 대체한다.
  - 화면 캡처      : CGWindowListCreateImage  (Retina 는 실제 픽셀 해상도로 캡처)
  - 창 찾기/활성화 : CGWindowListCopyWindowInfo + NSRunningApplication + AXRaise
  - 스크롤         : CGEventPostToPid(스크롤 이벤트) → 마우스 포인터를 옮기지 않음
  - 전역 단축키    : Carbon RegisterEventHotKey  (별도 권한 불필요)
  - 클립보드       : NSPasteboard (PNG + TIFF)

필요 권한(시스템 설정 > 개인정보 보호 및 보안)
  - 화면 기록     : 다른 앱의 화면 캡처
  - 손쉬운 사용   : 스크롤/키 이벤트 전송, 창 앞으로 가져오기
"""

import os
import time
import ctypes
import subprocess
from collections import namedtuple

import numpy as np
import cv2

import Quartz
from AppKit import (NSApplication, NSRunningApplication, NSWorkspace,
                    NSEvent, NSPasteboard, NSPasteboardItem, NSImage)
from Foundation import NSData

try:
    import ApplicationServices as AS
except Exception:            # 손쉬운 사용 API 없이도 기본 기능은 동작
    AS = None

OWN_PID = os.getpid()

# 창 핸들 대용: (pid, 창 번호, (x, y, w, h) 화면 좌표[포인트])
WinRef = namedtuple("WinRef", "pid wid rect")


# ------------------------------------------------------------------
# 디스플레이 (좌표계: 주 모니터 좌상단 원점, 단위 포인트 — Tk 좌표와 동일)
# ------------------------------------------------------------------
def _rect_tuple(r):
    return (int(round(r.origin.x)), int(round(r.origin.y)),
            int(round(r.size.width)), int(round(r.size.height)))


def get_monitors():
    mons = []
    try:
        err, ids, cnt = Quartz.CGGetActiveDisplayList(16, None, None)
        if err == 0:
            for d in ids[:cnt]:
                mons.append(_rect_tuple(Quartz.CGDisplayBounds(d)))
    except Exception:
        pass
    if not mons:
        mons = [primary_screen()]
    return mons


def primary_screen():
    return _rect_tuple(Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()))


def virtual_screen():
    mons = get_monitors()
    x0 = min(m[0] for m in mons)
    y0 = min(m[1] for m in mons)
    x1 = max(m[0] + m[2] for m in mons)
    y1 = max(m[1] + m[3] for m in mons)
    return (x0, y0, x1 - x0, y1 - y0)


# ------------------------------------------------------------------
# 화면 캡처
# ------------------------------------------------------------------
def _cgimage_to_bgr(img):
    """CGImage → BGR ndarray. 비트맵 컨텍스트에 다시 그려 포맷(8bit BGRA)을 고정한다
    (와이드 컬러/HDR 디스플레이의 16bit 이미지도 안전하게 처리)."""
    W = int(Quartz.CGImageGetWidth(img))
    H = int(Quartz.CGImageGetHeight(img))
    if W <= 0 or H <= 0:
        raise RuntimeError("empty image")
    try:
        buf = bytearray(W * H * 4)
        cs = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
        ctx = Quartz.CGBitmapContextCreate(
            buf, W, H, 8, W * 4, cs,
            Quartz.kCGImageAlphaPremultipliedFirst
            | Quartz.kCGBitmapByteOrder32Little)
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, W, H), img)
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(H, W, 4)
    except Exception:
        # 폴백: 원본 데이터 직접 해석(일반적인 32bit BGRA 가정)
        bpr = int(Quartz.CGImageGetBytesPerRow(img))
        data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
        raw = np.frombuffer(bytes(data), dtype=np.uint8)
        arr = raw[:H * bpr].reshape(H, bpr)[:, :W * 4].reshape(H, W, 4)
    return np.ascontiguousarray(arr[:, :, :3])


def grab_region(x, y, w, h):
    """화면 영역(포인트)을 실제 픽셀 해상도로 캡처 → BGR.
    결과 크기는 항상 (h*배율, w*배율) 로 맞춘다(Retina 배율 2, 일반 1)."""
    rect = Quartz.CGRectMake(x, y, w, h)
    img = Quartz.CGWindowListCreateImage(
        rect, Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
    if img is None:
        raise RuntimeError("screen capture failed (Screen Recording permission?)")
    arr = _cgimage_to_bgr(img)
    sc = max(1, int(round(arr.shape[1] / float(max(1, w)))))
    want = (int(h) * sc, int(w) * sc)
    if arr.shape[:2] != want:
        arr = cv2.resize(arr, (want[1], want[0]), interpolation=cv2.INTER_AREA)
    return arr


def grab_window(ref):
    """창 하나만 캡처(그림자 제외, 가려져 있어도 캡처). 실패 시 None."""
    if not ref:
        return None
    try:
        img = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
            int(ref.wid), Quartz.kCGWindowImageBoundsIgnoreFraming)
        if img is None:
            return None
        return _cgimage_to_bgr(img)
    except Exception:
        return None


# ------------------------------------------------------------------
# 창 찾기 / 활성화
# ------------------------------------------------------------------
def _window_list(opts=None, wid=0):
    if opts is None:
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
    try:
        return Quartz.CGWindowListCopyWindowInfo(opts, wid) or []
    except Exception:
        return []


def _info_rect(info):
    b = info.get("kCGWindowBounds") or {}
    return (int(round(b.get("X", 0))), int(round(b.get("Y", 0))),
            int(round(b.get("Width", 0))), int(round(b.get("Height", 0))))


def window_at_point(x, y, exclude_pid=OWN_PID):
    """좌표 아래의 최상위 일반 창(레이어 0). 자기 자신(이 앱)의 창은 제외."""
    for info in _window_list():       # 앞(위) → 뒤 순서
        try:
            if int(info.get("kCGWindowLayer", 0)) != 0:
                continue
            if float(info.get("kCGWindowAlpha", 1.0)) <= 0.01:
                continue
            pid = int(info.get("kCGWindowOwnerPID", 0))
            if exclude_pid and pid == exclude_pid:
                continue
            rx, ry, rw, rh = _info_rect(info)
            if rw < 20 or rh < 20:
                continue
            if rx <= x < rx + rw and ry <= y < ry + rh:
                return WinRef(pid, int(info.get("kCGWindowNumber", 0)),
                              (rx, ry, rw, rh))
        except Exception:
            continue
    return None


def get_window_rect(ref):
    """창의 현재 사각형(포인트). 창이 사라졌으면 마지막으로 알던 값."""
    for info in _window_list(Quartz.kCGWindowListOptionIncludingWindow,
                             int(ref.wid)):
        if int(info.get("kCGWindowNumber", -1)) == int(ref.wid):
            return _info_rect(info)
    return ref.rect


def _ax_raise(ref):
    """손쉬운 사용(AX) API 로 해당 창을 앱 안에서 맨 앞으로."""
    if AS is None:
        return
    try:
        app = AS.AXUIElementCreateApplication(ref.pid)
        err, wins = AS.AXUIElementCopyAttributeValue(app, "AXWindows", None)
        if err != 0 or not wins:
            return
        tx, ty, tw, th = get_window_rect(ref)
        best, best_d = None, None
        for w in wins:
            try:
                _, pv = AS.AXUIElementCopyAttributeValue(w, "AXPosition", None)
                _, sv = AS.AXUIElementCopyAttributeValue(w, "AXSize", None)
                _, p = AS.AXValueGetValue(pv, AS.kAXValueCGPointType, None)
                _, s = AS.AXValueGetValue(sv, AS.kAXValueCGSizeType, None)
                d = (abs(p.x - tx) + abs(p.y - ty)
                     + abs(s.width - tw) + abs(s.height - th))
                if best_d is None or d < best_d:
                    best, best_d = w, d
            except Exception:
                continue
        if best is not None:
            AS.AXUIElementPerformAction(best, "AXRaise")
            try:
                AS.AXUIElementSetAttributeValue(best, "AXMain", True)
            except Exception:
                pass
    except Exception:
        pass


def ensure_foreground(ref):
    """대상 앱을 활성화하고 그 창을 맨 앞으로."""
    if not ref:
        return
    try:
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None or int(front.processIdentifier()) != int(ref.pid):
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(
                ref.pid)
            if app is not None:
                app.activateWithOptions_(1 << 1)  # IgnoringOtherApps
                time.sleep(0.15)
    except Exception:
        pass
    _ax_raise(ref)


def set_capture_excluded(excluded):
    """이 앱의 모든 창을 화면 캡처에서 제외/복원 (NSWindowSharingNone ↔ ReadOnly).
    캡처 중 떠 있는 패널·메인 창이 결과 이미지에 찍히지 않게 한다."""
    try:
        for w in NSApplication.sharedApplication().windows():
            w.setSharingType_(0 if excluded else 1)
    except Exception:
        pass


def _main_height():
    return float(Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.height)


OVERLAY_LEVEL = 1000      # NSScreenSaverWindowLevel — Dock(20)·메뉴 막대(24)보다 위


def place_overlay(win, rect, level=OVERLAY_LEVEL):
    """오버레이 NSWindow 를 Dock·메뉴 막대 위 레벨로 올리고 화면 좌표(rect)에 정확히 고정."""
    from Foundation import NSMakeRect
    x, y, w, h = rect
    win.setLevel_(level)
    win.setCollectionBehavior_((1 << 0) | (1 << 8))   # 모든 Space + 전체화면 보조
    win.setFrame_display_(NSMakeRect(x, _main_height() - (y + h), w, h), True)
    win.orderFrontRegardless()
    try:
        win.makeKeyWindow()
    except Exception:
        pass
    return int(win.level())


def overlay_window(rect, exclude=()):
    """Tk 로 만든 테두리 없는 오버레이 창의 NSWindow 를 찾아 place_overlay 적용.
    (Tk 는 창 번호를 주지 않으므로 크기·x 위치로 찾는다. 메뉴 막대 때문에 높이가
    줄어 있을 수 있어 높이는 여유 있게 비교.) 못 찾으면 None."""
    x, y, w, h = rect
    best = None
    for win in NSApplication.sharedApplication().windows():
        try:
            if win in exclude or not win.isVisible():
                continue
            f = win.frame()
            if (abs(f.size.width - w) <= 2 and abs(f.origin.x - x) <= 2
                    and abs(f.size.height - h) <= 60):
                best = win
        except Exception:
            continue
    if best is not None:
        place_overlay(best, rect)
    return best


_esc_flag = [None]
_esc_monitor = [None]


def esc_watch_start():
    """선택 화면용 Esc 감지 시작: 앱 내부 키 입력 모니터(플래그만 기록, Tk 호출 없음)."""
    _esc_flag[0] = None
    esc_watch_stop()
    try:
        def handler(ev):
            try:
                if int(ev.keyCode()) == 53:
                    _esc_flag[0] = "monitor"
            except Exception:
                pass
            return ev
        _esc_monitor[0] = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            1 << 10, handler)                     # NSEventMaskKeyDown
    except Exception:
        _esc_monitor[0] = None


def esc_watch_stop():
    m = _esc_monitor[0]
    _esc_monitor[0] = None
    if m is not None:
        try:
            NSEvent.removeMonitor_(m)
        except Exception:
            pass


def esc_pressed():
    """Esc 가 눌렸으면 감지 경로 이름, 아니면 None (폴링용)."""
    if _esc_flag[0]:
        return _esc_flag[0]
    try:
        if Quartz.CGEventSourceKeyState(
                Quartz.kCGEventSourceStateHIDSystemState, 53):
            return "keystate"
    except Exception:
        pass
    return None


def mouse_pos():
    """현재 마우스 위치(화면 좌표, 주 모니터 좌상단 원점). 권한 불필요."""
    x, y = _cursor_pos()
    return int(round(x)), int(round(y))


def post_mouse(kind, x, y):
    """자체 점검용 마우스 이벤트 전송: kind = move / down / drag / up."""
    t = {"move": Quartz.kCGEventMouseMoved,
         "down": Quartz.kCGEventLeftMouseDown,
         "drag": Quartz.kCGEventLeftMouseDragged,
         "up": Quartz.kCGEventLeftMouseUp}[kind]
    ev = Quartz.CGEventCreateMouseEvent(None, t, Quartz.CGPointMake(x, y),
                                        Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def activate_self():
    """이 앱을 앞으로(단축키로 백그라운드에서 호출됐을 때 오버레이가 클릭을 받도록)."""
    try:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


# ------------------------------------------------------------------
# 스크롤 / 키 입력
# ------------------------------------------------------------------
SCROLL_PX_PER_NOTCH = 80          # Windows 휠 1노치(약 100px)에 해당하는 픽셀 스크롤
_F_WIN_UNDER = getattr(Quartz, "kCGMouseEventWindowUnderMousePointer", 91)
_F_WIN_UNDER_CAN = getattr(
    Quartz, "kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent", 92)
KC_PAGE_DOWN, KC_PAGE_UP = 121, 116


SCROLL_SIGN = [1]   # 스크롤 방향 보정(마우스 방향 반전 유틸 등으로 뒤집혀 있으면 -1)


def _scroll_event(cx, cy, notches, ref=None):
    """notches > 0 = 위로, < 0 = 아래로 (Windows 휠과 같은 부호)."""
    ev = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitPixel, 1,
        int(notches * SCROLL_PX_PER_NOTCH * SCROLL_SIGN[0]))
    Quartz.CGEventSetLocation(ev, Quartz.CGPointMake(cx, cy))
    if ref is not None:
        try:
            Quartz.CGEventSetIntegerValueField(ev, _F_WIN_UNDER, int(ref.wid))
            Quartz.CGEventSetIntegerValueField(ev, _F_WIN_UNDER_CAN, int(ref.wid))
        except Exception:
            pass
    return ev


def scroll_send(ref, cx, cy, notches):
    """대상 앱 프로세스에 스크롤 이벤트를 직접 전달 — 마우스 포인터를 옮기지 않는다."""
    if ref is None:
        return scroll_wheel(cx, cy, notches)
    n = abs(int(notches)) or 1
    step = 1 if notches > 0 else -1
    for _ in range(n):
        Quartz.CGEventPostToPid(ref.pid, _scroll_event(cx, cy, step, ref))
        time.sleep(0.012)


def _cursor_pos():
    ev = Quartz.CGEventCreate(None)
    p = Quartz.CGEventGetLocation(ev)
    return (p.x, p.y)


def scroll_hid(cx, cy, notches, ref=None):
    """포인터를 옮기지 않고, 위치만 지정한 휠 이벤트를 시스템에 전달.
    (포인터가 다른 앱 창 위에 있으면 그 창이 스크롤될 수 있어 보내지 않는다)"""
    try:
        px, py = mouse_pos()
        under = window_at_point(px, py, exclude_pid=0)
        if under is not None and under.pid != OWN_PID and (
                ref is None or under.pid != ref.pid):
            return
    except Exception:
        pass
    # 위치를 지정한 휠 이벤트도 시스템(HID) 단계에서는 포인터를 그 위치로 옮긴다
    # → 보낸 뒤 즉시 원위치
    home = _cursor_pos()
    n = abs(int(notches)) or 1
    step = 1 if notches > 0 else -1
    try:
        for _ in range(n):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, _scroll_event(cx, cy, step))
            time.sleep(0.012)
        time.sleep(0.04)
    finally:
        cur = _cursor_pos()
        if abs(cur[0] - home[0]) > 1 or abs(cur[1] - home[1]) > 1:
            Quartz.CGWarpMouseCursorPosition(Quartz.CGPointMake(*home))
            Quartz.CGAssociateMouseAndMouseCursorPosition(True)


def scroll_session(cx, cy, notches, ref=None):
    """세션 단계로 보내는 위치 지정 휠(포인터 갱신 없이 전달되는지 실험용)."""
    n = abs(int(notches)) or 1
    step = 1 if notches > 0 else -1
    for _ in range(n):
        Quartz.CGEventPost(Quartz.kCGSessionEventTap,
                           _scroll_event(cx, cy, step, ref))
        time.sleep(0.012)


def scroll_wheel(cx, cy, notches):
    """최후 수단: 포인터를 영역 위로 잠깐 옮겨 휠 전송 후 '즉시' 원위치.
    (스크롤 중에도 사용자가 마우스를 자유롭게 쓸 수 있도록 머무르지 않음)"""
    home = _cursor_pos()
    try:
        Quartz.CGWarpMouseCursorPosition(Quartz.CGPointMake(cx, cy))
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)
        # 포인터가 그 위에 있다는 걸 앱(Chrome 등)이 알도록 이동 신호
        try:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, Quartz.CGPointMake(cx, cy),
                Quartz.kCGMouseButtonLeft))
        except Exception:
            pass
        time.sleep(0.04)
        n = abs(int(notches)) or 1
        step = 1 if notches > 0 else -1
        for _ in range(n):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, _scroll_event(cx, cy, step))
            time.sleep(0.012)
        time.sleep(0.06)
    finally:
        Quartz.CGWarpMouseCursorPosition(Quartz.CGPointMake(*home))
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)


def restore_pointer():
    """(호환용) 휠 방식은 이제 매번 즉시 원위치하므로 할 일 없음."""
    return


KC_DOWN, KC_UP = 125, 126


def key_page(ref, down, notches=1):
    """방향키(↓/↑)를 notches×2 번 대상 앱에 전달.
    (PageDown 은 한 번에 화면 대부분을 넘겨 이어 붙이기가 깨지므로 작은 단위로)"""
    kc = KC_DOWN if down else KC_UP
    for _ in range(max(1, abs(int(notches)) * 2)):
        for is_down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, kc, is_down)
            if ref is not None:
                Quartz.CGEventPostToPid(ref.pid, ev)
            else:
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.015)


def post_key(kc):
    """시스템 전체로 키 입력(누름/뗌) 전송 — 자체 점검에서 전역 단축키 확인용."""
    for is_down in (True, False):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                           Quartz.CGEventCreateKeyboardEvent(None, kc, is_down))
        time.sleep(0.03)


def apply_scroll(ref, cx, cy, method, down, notches):
    try:
        if method == "send":
            scroll_send(ref, cx, cy, -notches if down else notches)
        elif method == "hid":
            scroll_hid(cx, cy, -notches if down else notches, ref)
        elif method == "sess":
            scroll_session(cx, cy, -notches if down else notches, ref)
        elif method == "wheel":
            scroll_wheel(cx, cy, -notches if down else notches)
        else:
            # 키 입력은 앞에 있는 창으로 가므로, 사용자가 다른 앱(예: 중지 패널)을
            # 눌러 대상이 뒤로 갔으면 다시 앞으로
            try:
                front = NSWorkspace.sharedWorkspace().frontmostApplication()
                if ref is not None and (front is None
                                        or int(front.processIdentifier()) != int(ref.pid)):
                    ensure_foreground(ref)
            except Exception:
                pass
            key_page(ref, down, notches)
    except Exception:
        pass


# ------------------------------------------------------------------
# 마우스 상태 (권한 불필요)
# ------------------------------------------------------------------
def left_button_down():
    try:
        return bool(NSEvent.pressedMouseButtons() & 1)
    except Exception:
        return False


def right_button_down():
    try:
        return bool(NSEvent.pressedMouseButtons() & 2)
    except Exception:
        return False


# ------------------------------------------------------------------
# 클립보드
# ------------------------------------------------------------------
def copy_png_bytes(png_bytes, with_tiff=True):
    try:
        data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
        item = NSPasteboardItem.alloc().init()
        item.setData_forType_(data, "public.png")
        if with_tiff:
            img = NSImage.alloc().initWithData_(data)
            tiff = img.TIFFRepresentation() if img is not None else None
            if tiff is not None:
                item.setData_forType_(tiff, "public.tiff")
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        return bool(pb.writeObjects_([item]))
    except Exception:
        return False


def open_path(path):
    subprocess.Popen(["open", path])


# ------------------------------------------------------------------
# 권한
# ------------------------------------------------------------------
def permissions_status():
    """(화면 기록 허용, 손쉬운 사용 허용)"""
    screen = ax = True
    try:
        screen = bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        pass
    try:
        if AS is not None:
            ax = bool(AS.AXIsProcessTrusted())
    except Exception:
        pass
    return screen, ax


def request_permissions():
    """없는 권한에 대해 시스템 권한 요청 창을 띄운다."""
    screen, ax = permissions_status()
    if not screen:
        try:
            Quartz.CGRequestScreenCaptureAccess()
        except Exception:
            pass
    if not ax and AS is not None:
        try:
            AS.AXIsProcessTrustedWithOptions(
                {AS.kAXTrustedCheckOptionPrompt: True})
        except Exception:
            pass
    return screen, ax


def open_privacy_settings(kind="ScreenCapture"):
    """시스템 설정의 해당 개인정보 보호 패널 열기 (ScreenCapture / Accessibility)."""
    url = ("x-apple.systempreferences:com.apple.preference.security"
           f"?Privacy_{kind}")
    subprocess.Popen(["open", url])


# ------------------------------------------------------------------
# 단축키 (Carbon RegisterEventHotKey — 권한 없이 전역 단축키 등록, 키 입력은 소비됨)
# ------------------------------------------------------------------
MOD_CMD = 0x0100
MOD_SHIFT = 0x0200
MOD_OPTION = 0x0800
MOD_CONTROL = 0x1000

# macOS 가상 키코드(ANSI 배열) → 표시 이름
KEYCODE_NAMES = {
    0: "A", 11: "B", 8: "C", 2: "D", 14: "E", 3: "F", 5: "G", 4: "H", 34: "I",
    38: "J", 40: "K", 37: "L", 46: "M", 45: "N", 31: "O", 35: "P", 12: "Q",
    15: "R", 1: "S", 17: "T", 32: "U", 9: "V", 13: "W", 7: "X", 16: "Y", 6: "Z",
    29: "0", 18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6", 26: "7",
    28: "8", 25: "9",
    27: "-", 24: "=", 33: "[", 30: "]", 42: "\\", 41: ";", 39: "'", 43: ",",
    47: ".", 44: "/", 50: "`",
    36: "Enter", 48: "Tab", 49: "Space", 51: "Backspace", 117: "Del",
    115: "Home", 119: "End", 116: "PageUp", 121: "PageDown",
    123: "Left", 124: "Right", 125: "Down", 126: "Up",
    122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6", 98: "F7",
    100: "F8", 101: "F9", 109: "F10", 103: "F11", 111: "F12",
    105: "F13", 107: "F14", 113: "F15",
    82: "Num0", 83: "Num1", 84: "Num2", 85: "Num3", 86: "Num4", 87: "Num5",
    88: "Num6", 89: "Num7", 91: "Num8", 92: "Num9",
}
MODIFIER_KEYCODES = {54, 55, 56, 57, 58, 59, 60, 61, 62, 63}
_NAME_TO_KEYCODE = {v.lower(): k for k, v in KEYCODE_NAMES.items()}


def keysym_to_keycode(keysym):
    """Tk keysym → macOS 키코드(폴백용)."""
    if not keysym:
        return None
    k = keysym.lower()
    alias = {"return": "enter", "next": "pagedown", "prior": "pageup",
             "backspace": "backspace", "delete": "del", "space": "space"}
    k = alias.get(k, k)
    return _NAME_TO_KEYCODE.get(k)


def mods_text(mods):
    parts = []
    if mods & MOD_CONTROL:
        parts.append("Ctrl")
    if mods & MOD_OPTION:
        parts.append("Option")
    if mods & MOD_SHIFT:
        parts.append("Shift")
    if mods & MOD_CMD:
        parts.append("Cmd")
    return parts


def current_key_event():
    """지금 처리 중인 키 입력(NSEvent)의 (키코드, Carbon 수정자). 없으면 None.
    Option 조합으로 바뀐 문자와 무관하게 '물리 키'를 정확히 얻는다."""
    try:
        ev = NSApplication.sharedApplication().currentEvent()
        if ev is None or int(ev.type()) != 10:     # NSEventTypeKeyDown
            return None
        flags = int(ev.modifierFlags())
        mods = 0
        if flags & (1 << 17):
            mods |= MOD_SHIFT
        if flags & (1 << 18):
            mods |= MOD_CONTROL
        if flags & (1 << 19):
            mods |= MOD_OPTION
        if flags & (1 << 20):
            mods |= MOD_CMD
        return int(ev.keyCode()), mods
    except Exception:
        return None


def _fourcc(s):
    return int.from_bytes(s.encode("ascii"), "big")


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_void_p)


LAST_HOTKEY_ERRORS = {}   # mode -> RegisterEventHotKey 결과(진단용)


class HotkeyManager:
    """Windows 판 HotkeyManager 와 같은 인터페이스. 메인 스레드에서 호출해야 한다."""

    SIG = _fourcc("SCap")

    def __init__(self, dispatch_cb):
        self.dispatch_cb = dispatch_cb
        self.bindings = {}
        self._refs = []
        self._ids = {}
        self._c = None
        self._handler = None
        self._handler_ref = ctypes.c_void_p()

    def _load(self):
        if self._c is not None:
            return True
        try:
            c = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/Carbon.framework/Carbon")
        except OSError:
            return False
        c.GetApplicationEventTarget.restype = ctypes.c_void_p
        c.GetApplicationEventTarget.argtypes = []
        c.InstallEventHandler.restype = ctypes.c_int32
        c.InstallEventHandler.argtypes = [
            ctypes.c_void_p, _HANDLER, ctypes.c_ulong,
            ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p)]
        c.RegisterEventHotKey.restype = ctypes.c_int32
        c.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        c.UnregisterEventHotKey.restype = ctypes.c_int32
        c.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        c.GetEventParameter.restype = ctypes.c_int32
        c.GetEventParameter.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
        self._c = c

        def _on_hotkey(_next, event, _user):
            try:
                hk = _EventHotKeyID()
                err = c.GetEventParameter(
                    event, _fourcc("----"), _fourcc("hkid"), None,
                    ctypes.sizeof(hk), None, ctypes.byref(hk))
                if err == 0 and hk.signature == self.SIG:
                    mode = self._ids.get(int(hk.id))
                    if mode:
                        self.dispatch_cb(mode)
            except Exception:
                pass
            return 0

        self._handler = _HANDLER(_on_hotkey)      # 참조 유지(GC 방지)
        spec = _EventTypeSpec(_fourcc("keyb"), 5)  # kEventHotKeyPressed
        err = c.InstallEventHandler(
            c.GetApplicationEventTarget(), self._handler, 1,
            ctypes.byref(spec), None, ctypes.byref(self._handler_ref))
        return err == 0

    def set_bindings(self, bindings):
        self.bindings = {m: (v[0], v[1]) for m, v in bindings.items() if v}
        self.restart()

    def restart(self):
        self.stop()
        if not self._load():
            return
        c = self._c
        target = c.GetApplicationEventTarget()
        i = 1
        for mode, (mods, kc) in self.bindings.items():
            ref = ctypes.c_void_p()
            try:
                err = c.RegisterEventHotKey(
                    int(kc), int(mods), _EventHotKeyID(self.SIG, i), target, 0,
                    ctypes.byref(ref))
                LAST_HOTKEY_ERRORS[mode] = int(err)     # 0 = 성공
                if err == 0:
                    self._ids[i] = mode
                    self._refs.append(ref)
            except Exception:
                pass
            i += 1

    def stop(self):
        if self._c is not None:
            for ref in self._refs:
                try:
                    self._c.UnregisterEventHotKey(ref)
                except Exception:
                    pass
        self._refs = []
        self._ids = {}
