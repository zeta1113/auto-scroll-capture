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


def _scroll_event(cx, cy, notches, ref=None):
    """notches > 0 = 위로, < 0 = 아래로 (Windows 휠과 같은 부호)."""
    ev = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitPixel, 1,
        int(notches * SCROLL_PX_PER_NOTCH))
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


def scroll_wheel(cx, cy, notches):
    """최후 수단: 포인터를 잠깐 영역 위로 옮겨 실제 휠 이벤트 전송 후 원위치."""
    orig = _cursor_pos()
    try:
        Quartz.CGWarpMouseCursorPosition(Quartz.CGPointMake(cx, cy))
        time.sleep(0.03)
        n = abs(int(notches)) or 1
        step = 1 if notches > 0 else -1
        for _ in range(n):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                               _scroll_event(cx, cy, step))
            time.sleep(0.012)
        time.sleep(0.05)
    finally:
        Quartz.CGWarpMouseCursorPosition(Quartz.CGPointMake(*orig))
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)


def key_page(ref, down):
    """PageDown / PageUp 키를 대상 앱에 전달."""
    kc = KC_PAGE_DOWN if down else KC_PAGE_UP
    for is_down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, kc, is_down)
        if ref is not None:
            Quartz.CGEventPostToPid(ref.pid, ev)
        else:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.02)


def apply_scroll(ref, cx, cy, method, down, notches):
    try:
        if method == "send":
            scroll_send(ref, cx, cy, -notches if down else notches)
        elif method == "wheel":
            scroll_wheel(cx, cy, -notches if down else notches)
        else:
            key_page(ref, down)
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
