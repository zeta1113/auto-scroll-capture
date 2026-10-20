# -*- coding: utf-8 -*-
"""
스크롤 캡처 엔진 자동 점검 (Windows / macOS 공용, CI 에서도 사용)

  python tools/engine_test.py [결과PNG경로] [--stop-after 초 | --cancel-after 초]

별도 프로세스로 긴 텍스트가 든 테스트 창을 띄운 뒤, 실제 캡처 엔진
(capture_scrolling_region)으로 그 영역을 스크롤 캡처한다.
결과 이미지 높이가 화면 영역의 3배 이상이면 성공(exit 0).
--stop-after   : N초 뒤 [중지] → '여기까지' 저장되는지(끝까지보다 짧고 1화면 이상)
--cancel-after : N초 뒤 [취소] → 저장 없이 CaptureCancelled 로 끝나는지
"""

import os
import sys
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "source")


def run_target():
    """테스트 대상 창: 번호가 매겨진 긴 텍스트."""
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    import tkinter as tk
    root = tk.Tk()
    root.title("engine-test-target")
    root.geometry("520x420+120+140")
    root.attributes("-topmost", True)
    txt = tk.Text(root, font=("Courier", 16), wrap="none", bd=0,
                  highlightthickness=0)
    txt.pack(fill="both", expand=True)
    for i in range(1, 241):
        txt.insert("end", f"{i:04d}  " + ("abcdefghij" * (i % 5 + 1))
                   + f"  line {i}\n")
    txt.config(state="disabled")

    def report():
        root.update_idletasks()
        print("READY", txt.winfo_rootx(), txt.winfo_rooty(),
              txt.winfo_width(), txt.winfo_height(), flush=True)

    root.after(800, report)
    root.after(120000, root.destroy)
    root.mainloop()


def _arg(name):
    if name in sys.argv:
        return float(sys.argv[sys.argv.index(name) + 1])
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")
            and not a.replace(".", "").isdigit()]
    out = args[0] if args else os.path.join(HERE, "engine_test.png")
    stop_after, cancel_after = _arg("--stop-after"), _arg("--cancel-after")
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--target"],
                         stdout=subprocess.PIPE, text=True)
    try:
        line = ""
        t0 = time.time()
        while time.time() - t0 < 30:
            line = p.stdout.readline().strip()
            if line.startswith("READY"):
                break
        if not line.startswith("READY"):
            print("FAIL: target window did not start")
            return 1
        _, x, y, w, h = line.split()
        x, y, w, h = int(x), int(y), int(w), int(h)
        region = (x + 4, y + 4, w - 8, h - 8)
        print("target region:", region, flush=True)
        time.sleep(1.0)

        sys.path.insert(0, SRC)
        import threading
        import scroll_capture as sc
        ctl = sc.CaptureControl()
        if stop_after:
            threading.Timer(stop_after, ctl.stop.set).start()
        if cancel_after:
            threading.Timer(cancel_after, ctl.cancel.set).start()
        try:
            img = sc.capture_scrolling_region(
                region, lambda m: print("  status:", m, flush=True),
                {"auto_top": True, "speed": 1.0, "control": ctl})
        except sc.CaptureCancelled:
            print("cancelled (nothing saved)")
            print("PASS" if cancel_after else "FAIL: unexpected cancel")
            return 0 if cancel_after else 1
        if cancel_after:
            print("FAIL: cancel was ignored")
            return 1
        import cv2
        ok_w, buf = cv2.imencode(".png", img)
        if ok_w:
            buf.tofile(out)
        scale = img.shape[1] / float(region[2])
        ratio = img.shape[0] / (region[3] * scale)
        print(f"result: {img.shape[1]}x{img.shape[0]}  scale={scale:.2f}  "
              f"height ratio={ratio:.1f}  -> {out}", flush=True)
        if stop_after:
            ok = ctl.stopped and 1.0 <= ratio < 9
            print(f"stopped={ctl.stopped}")
            print("PASS" if ok else "FAIL: stop did not cut the capture")
            return 0 if ok else 1
        if ratio >= 3:
            print("PASS")
            return 0
        print("FAIL: captured image is too short")
        return 1
    finally:
        p.kill()


if __name__ == "__main__":
    if "--target" in sys.argv:
        run_target()
    else:
        sys.exit(main())
