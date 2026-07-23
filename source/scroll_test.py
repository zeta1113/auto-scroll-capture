# -*- coding: utf-8 -*-
"""
스크롤 진단 도구
- 실행 후 3초 안에, 캡처하려는 대화 영역 '안'으로 마우스를 올려두세요.
- 세 가지 스크롤 방식(휠 / WM_MOUSEWHEEL 메시지 / PageDown 키)을
  각각 시도하고, 화면 내용이 실제로 움직였는지 알려줍니다.
- 어떤 방식이 'MOVED'로 나오는지 확인해 알려주시면 됩니다.
"""

import time
import ctypes
import numpy as np
import cv2
import mss
import win32api
import win32con
import win32gui
import win32process

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

WHEEL_DELTA = 120
GA_ROOT = 2


def ensure_foreground(hwnd):
    try:
        fg = win32gui.GetForegroundWindow()
        if fg == hwnd:
            return
        cur = win32api.GetCurrentThreadId()
        fg_t = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        tg_t = win32process.GetWindowThreadProcessId(hwnd)[0]
        att = []
        for t in {fg_t, tg_t}:
            if t and t != cur:
                try:
                    win32process.AttachThreadInput(cur, t, True); att.append(t)
                except Exception:
                    pass
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        for t in att:
            try:
                win32process.AttachThreadInput(cur, t, False)
            except Exception:
                pass
    except Exception:
        pass


def grab(sct, region):
    x, y, w, h = region
    shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)


def diff(a, b):
    return float(np.mean(cv2.absdiff(a, b)))


def main():
    print("3초 안에 캡처할 대화 영역 안으로 마우스를 올려두세요...")
    for i in (3, 2, 1):
        print(i, "...")
        time.sleep(1)

    cx, cy = win32gui.GetCursorPos()
    child = win32gui.WindowFromPoint((cx, cy))
    hwnd = ctypes.windll.user32.GetAncestor(child, GA_ROOT)
    title = win32gui.GetWindowText(hwnd)
    print(f"\n커서 위치: ({cx},{cy})")
    print(f"대상 최상위 창: '{title}'  (hwnd={hwnd}, child={child})")

    # 커서 주변 400x300 영역을 관찰 대상으로
    w, h = 400, 300
    region = (cx - w // 2, cy - h // 2, w, h)

    ensure_foreground(hwnd)
    time.sleep(0.4)

    with mss.mss() as sct:
        def test(name, fn):
            before = grab(sct, region)
            fn()
            time.sleep(0.5)
            after = grab(sct, region)
            d = diff(before, after)
            print(f"  [{name:6}] 변화량={d:7.2f}  ->  {'MOVED ✅' if d > 2 else '변화없음'}")

        print("\n=== 아래로 스크롤 테스트 ===")

        def m_wheel():
            ensure_foreground(hwnd)
            win32api.SetCursorPos((cx, cy)); time.sleep(0.03)
            for _ in range(3):
                win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -WHEEL_DELTA, 0)
                time.sleep(0.02)

        def m_send():
            ch = win32gui.WindowFromPoint((cx, cy))
            delta = -WHEEL_DELTA * 3
            wparam = (delta & 0xFFFF) << 16
            lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
            win32gui.SendMessage(ch, win32con.WM_MOUSEWHEEL, wparam, lparam)

        def m_key():
            ensure_foreground(hwnd)
            win32api.SetCursorPos((cx, cy))
            win32api.keybd_event(win32con.VK_NEXT, 0, 0, 0)
            time.sleep(0.02)
            win32api.keybd_event(win32con.VK_NEXT, 0, win32con.KEYEVENTF_KEYUP, 0)

        test("wheel", m_wheel)
        test("send", m_send)
        test("key", m_key)

    print("\n하나라도 MOVED가 나왔다면, 그 방식으로 캡처가 가능합니다.")
    print("결과를 알려주세요.")


if __name__ == "__main__":
    main()
