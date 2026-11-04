# -*- coding: utf-8 -*-
"""
스크롤 캡처 엔진 자동 점검 (Windows / macOS 공용, CI 에서도 사용)

  python tools/engine_test.py [결과PNG경로] [옵션]

  --sticky        : 테스트 창에 중간부터 상단에 붙는 고정(sticky) 바 표시(GitHub 처럼)
  --right-edge    : 4줄마다 오른쪽 끝까지 닿는 파란 띠(카카오톡 말풍선처럼) — 결과 오른쪽 끝에 남는지 검사
  --chrome        : Tk 창 대신 실제 Chrome 으로 테스트 페이지(고정 바 포함)를 열어 캡처
  --url URL       : 실제 웹페이지를 Chrome 으로 열어 '창 전체'(윈도우 스크롤처럼) 캡처
                    (줄 표식 검사 없이 결과 이미지·통계만 — 눈으로 비교용)
  --src DIR       : 다른 버전의 source 폴더로 실행(예: 이전 버전과 비교)
  --stop-after N  : N초 뒤 [중지] → '여기까지' 저장되는지
  --cancel-after N: N초 뒤 [취소] → 저장 없이 CaptureCancelled 로 끝나는지

줄마다 '빨간 시작칸 + 흑백 8칸(줄 번호 2진수)' 표식을 넣고, 결과 이미지에서
표식을 읽어 1~N 번 줄이 빠짐·중복 없이 순서대로 있는지 검사한다(흑백이라 색 변환에 강함).
"""

import os
import sys
import time
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "source")
N_LINES = 240
BAR_AFTER = 20           # 이 줄 다음에 고정 바
CELL = 20                # 표식 한 칸 폭(px, 화면 좌표)


def _bits(i):
    return [(i >> (7 - k)) & 1 for k in range(8)]


# ------------------------------------------------------------------
# 테스트 대상 1: Tk 창
# ------------------------------------------------------------------
def run_target(sticky, right_edge=False):
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    import tkinter as tk
    import tkinter.font as tkfont
    root = tk.Tk()
    root.title("engine-test-target")
    root.geometry("560x460+120+140")
    root.attributes("-topmost", True)
    font = tkfont.Font(family="Courier", size=16)
    cw_char = font.measure(" ")
    n_sp = max(1, round(CELL / cw_char))        # 한 칸 = 공백 n_sp 개
    cell_px = n_sp * cw_char
    txt = tk.Text(root, font=font, wrap="none", bd=0, highlightthickness=0,
                  padx=0, pady=0, spacing1=3, spacing3=3)
    sb = None
    if right_edge:      # 카카오톡처럼 영역 안 오른쪽 끝에 스크롤바
        sb = tk.Scrollbar(root, orient="vertical", command=txt.yview)
        sb.pack(side="right", fill="y")
    txt.pack(fill="both", expand=True)
    txt.tag_configure("r", background="#ff0000")
    txt.tag_configure("k", background="#000000")
    txt.tag_configure("w", background="#ffffff")
    txt.tag_configure("b", background="#1e64ff")
    sp = " " * n_sp
    for i in range(1, N_LINES + 1):
        txt.insert("end", sp, "r")
        for b in _bits(i):
            txt.insert("end", sp, "k" if b else "w")
        if right_edge and i % 4 == 0:      # 짧은 글자 + 창 오른쪽 끝을 넘어가는 띠
            txt.insert("end", f"  {i:04d} abc")
            txt.insert("end", " " * 200, "b")
        else:
            txt.insert("end", f"  {i:04d} " + ("abcdefghij" * (i % 5 + 1)))
        txt.insert("end", "\n")
    txt.config(state="disabled")

    bar = tk.Label(root, text="STICKY BAR", bg="#ff8c00", fg="black",
                   font=("Arial", 14, "bold"), anchor="w")

    def on_scroll(first, last):
        if sb is not None:
            sb.set(first, last)
        if sticky and float(first) > BAR_AFTER / float(N_LINES):
            bar.place(x=0, y=0, relwidth=1, height=34)
        else:
            bar.place_forget()

    txt.configure(yscrollcommand=on_scroll)

    def report():
        root.update_idletasks()
        sbw = sb.winfo_width() if sb is not None else 0
        print("READY", txt.winfo_rootx(), txt.winfo_rooty(),
              txt.winfo_width() + sbw, txt.winfo_height(), cell_px, sbw, flush=True)

    root.after(800, report)
    root.after(180000, root.destroy)
    root.mainloop()


# ------------------------------------------------------------------
# 테스트 대상 2: Chrome 페이지
# ------------------------------------------------------------------
def _html():
    rows = []
    for i in range(1, N_LINES + 1):
        cells = '<i class="r"></i>' + "".join(
            '<i class="k"></i>' if b else '<i class="w"></i>' for b in _bits(i))
        rows.append(f'<div class="l">{cells}<span>{i:04d} '
                    + "abcdefghij" * (i % 5 + 1) + "</span></div>")
        if i == BAR_AFTER:
            rows.append('<div id="bar">STICKY BAR (like GitHub README tabs)</div>')
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<title>ScrollCapture Engine Test</title><style>"
            "body{margin:0;font:16px monospace;background:#fff}"
            ".l{display:flex;height:28px;align-items:center;white-space:nowrap}"
            f"i{{display:block;width:{CELL}px;height:28px;flex:none}}"
            ".r{background:#ff0000}.k{background:#000}.w{background:#fff}"
            "span{margin-left:12px}"
            "#bar{position:sticky;top:0;height:36px;line-height:36px;"
            "background:#ff8c00;font-weight:bold;z-index:2}"
            "#pad{height:160px;background:#f4f4f4}"
            "</style></head><body><div id='pad'>top area</div>"
            + "".join(rows) + "<div style='height:40px'></div></body></html>")


def _chrome_path():
    cands = ([r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
             if sys.platform == "win32" else
             ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"])
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def _find_chrome_window():
    """Chrome 테스트 창의 화면 사각형(포인트/픽셀)."""
    if sys.platform == "darwin":
        import Quartz
        best = None
        for info in Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID) or []:
            if "Chrome" not in str(info.get("kCGWindowOwnerName", "")):
                continue
            if int(info.get("kCGWindowLayer", 0)) != 0:
                continue
            b = info.get("kCGWindowBounds") or {}
            r = (int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"]))
            if r[2] * r[3] > 200000 and (best is None or r[2] * r[3] > best[2] * best[3]):
                best = r
        return best
    import win32gui
    found = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and "ScrollCapture Engine Test" in win32gui.GetWindowText(h):
            found.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    if not found:
        return None
    l, t, r, b = win32gui.GetWindowRect(found[0])
    return (l, t, r - l, b - t)


def _window_of_pid(pid):
    """그 프로세스가 띄운 가장 큰 창의 화면 사각형."""
    if sys.platform == "darwin":
        import Quartz
        best = None
        for info in Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID) or []:
            if int(info.get("kCGWindowOwnerPID", 0)) != pid:
                continue
            if int(info.get("kCGWindowLayer", 0)) != 0:
                continue
            b = info.get("kCGWindowBounds") or {}
            r = (int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"]))
            if r[2] * r[3] > 200000 and (best is None or r[2] * r[3] > best[2] * best[3]):
                best = r
        return best
    import win32gui
    import win32process
    found = []

    def cb(h, _):
        try:
            if (win32gui.IsWindowVisible(h)
                    and win32process.GetWindowThreadProcessId(h)[1] == pid):
                l, t, r, b = win32gui.GetWindowRect(h)
                if (r - l) * (b - t) > 200000:
                    found.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    if not found:
        return None
    sys.path.insert(0, SRC)
    import scroll_capture as sc
    return sc.get_window_rect(found[0])     # 앱의 '윈도우 캡처'와 같은 창 영역


def _win_height(want):
    """화면 높이에 맞춘 창 높이(작은 CI 화면에서 창이 화면 밖·작업 표시줄까지 가지 않게)."""
    try:
        if sys.platform == "win32":
            import ctypes
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            return int(max(400, min(want, sh - 60 - 70)))   # 위치 60 + 작업 표시줄
    except Exception:
        pass
    return want


def start_chrome_url(url):
    """실제 웹페이지를 새 Chrome 창으로 열고 '창 전체' 영역을 돌려준다."""
    exe = _chrome_path()
    if not exe:
        return None, None
    tmp = tempfile.mkdtemp(prefix="sc_chrome_")
    proc = subprocess.Popen([
        exe, f"--user-data-dir={os.path.join(tmp, 'profile')}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-session-crashed-bubble", "--password-store=basic",
        "--use-mock-keychain", "--window-position=0,0",
        "--start-maximized", "--new-window", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.5)
        if _window_of_pid(proc.pid):
            time.sleep(7.0)                 # 페이지 로딩 대기
            return proc, _window_of_pid(proc.pid)
    return proc, None


def start_chrome():
    exe = _chrome_path()
    if not exe:
        return None, None
    tmp = tempfile.mkdtemp(prefix="sc_chrome_")
    page = os.path.join(tmp, "test.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(_html())
    url = "file:///" + page.replace("\\", "/").lstrip("/")
    proc = subprocess.Popen([
        exe, f"--user-data-dir={os.path.join(tmp, 'profile')}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-session-crashed-bubble", "--password-store=basic",
        "--use-mock-keychain", "--window-position=80,60",
        f"--window-size=900,{_win_height(820)}", "--new-window", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(0.5)
        r = _find_chrome_window()
        if r:
            time.sleep(2.0)
            r = _find_chrome_window()
            x, y, w, h = r
            # 페이지 본문 영역(탭/주소창 아래, 오른쪽 스크롤바 앞)
            return proc, (x + 8, y + 130, w - 40, h - 150)
    return proc, None


# ------------------------------------------------------------------
# 결과 검사: 줄 표식 읽기
# ------------------------------------------------------------------
def decode_lines(img, cell):
    """img(BGR) 에서 행마다 표식(빨강+8칸)을 읽어 줄 번호 순서와 줄별 행 수를 반환."""
    import numpy as np
    H, W = img.shape[:2]
    b, g, r = img[:, :, 0].astype(int), img[:, :, 1].astype(int), img[:, :, 2].astype(int)
    red = (r > 170) & (g < 100) & (b < 100)
    orange = (r > 200) & (g > 100) & (g < 180) & (b < 80)
    bar_rows = int((orange.mean(axis=1) > 0.4).sum())
    seq, counts = [], {}
    for y in range(H):
        xs = np.flatnonzero(red[y])
        if xs.size < cell * 0.5:
            continue
        x0 = xs[0]
        val, ok = 0, True
        for k in range(8):
            x = int(x0 + cell * (k + 1) + cell / 2)
            if x >= W:
                ok = False
                break
            v = (r[y, x] + g[y, x] + b[y, x]) / 3.0
            if v < 90:
                bit = 1
            elif v > 165:
                bit = 0
            else:
                ok = False
                break
            val = val * 2 + bit
        if not ok or val == 0:
            continue
        counts[val] = counts.get(val, 0) + 1
        if not seq or seq[-1] != val:
            seq.append(val)
    return seq, counts, bar_rows


def check_lines(img, cell, first_expected=1):
    seq, counts, bar_rows = decode_lines(img, cell)
    exp = list(range(seq[0], N_LINES + 1)) if seq else []
    problems = []
    if not seq:
        problems.append("no line markers found")
    else:
        if seq != exp:
            # 빠진 줄 / 중복 / 순서 오류
            seen = set()
            dups = [v for v in seq if v in seen or seen.add(v)]
            missing = sorted(set(exp) - set(seq))
            problems.append(f"sequence mismatch: missing={missing[:12]} dups={dups[:12]}")
        if seq[0] > first_expected + 2:
            problems.append(f"starts at line {seq[0]}")
        med = sorted(counts.values())[len(counts) // 2]
        thin = [v for v, c in counts.items()
                if c < med * 0.7 and v not in (seq[0], seq[-1])]
        if thin:
            problems.append(f"partially lost lines: {sorted(thin)[:12]}")
    return problems, seq, bar_rows


# ------------------------------------------------------------------
def _arg(name):
    if name in sys.argv:
        return float(sys.argv[sys.argv.index(name) + 1])
    return None


def _sarg(name):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return None


def main():
    url = _sarg("--url")
    src = _sarg("--src")
    skip = {url, src, _sarg("--speed")}
    args = [a for a in sys.argv[1:] if not a.startswith("--")
            and not a.replace(".", "").isdigit() and a not in skip]
    out = args[0] if args else os.path.join(HERE, "engine_test.png")
    stop_after, cancel_after = _arg("--stop-after"), _arg("--cancel-after")
    sticky = "--sticky" in sys.argv
    use_chrome = "--chrome" in sys.argv
    right_edge = "--right-edge" in sys.argv
    if not os.environ.get("SC_LOG"):          # 캡처 기록(고정 영역 판단 등)을 결과와 함께 출력
        os.environ["SC_LOG"] = os.path.splitext(out)[0] + "_log.txt"
        try:
            os.remove(os.environ["SC_LOG"])
        except OSError:
            pass

    p = None
    try:
        if url:
            p, region = start_chrome_url(url)
            if region is None:
                print("SKIP: Chrome not available")
                return 0
            cell = CELL
        elif use_chrome:
            p, region = start_chrome()
            if region is None:
                print("SKIP: Chrome not available")
                return 0
            cell = CELL
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--target"]
            if sticky:
                cmd.append("--sticky")
            if right_edge:
                cmd.append("--right-edge")
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
            line = ""
            t0 = time.time()
            while time.time() - t0 < 30:
                line = p.stdout.readline().strip()
                if line.startswith("READY"):
                    break
            if not line.startswith("READY"):
                print("FAIL: target window did not start")
                return 1
            _, x, y, w, h, cell, sbw = line.split()
            sbw = int(sbw)
            x, y, w, h, cell = int(x), int(y), int(w), int(h), float(cell)
            region = (x + 4, y + 4, w - 8, h - 8)
        print("target region:", region, flush=True)
        time.sleep(1.0)

        if src:                                  # 다른 버전 소스로 실행
            for k in [k for k in sys.modules if k in ("scroll_capture", "updater",
                                                       "version", "platform_mac")]:
                del sys.modules[k]
            sys.path.insert(0, os.path.abspath(src))
        else:
            sys.path.insert(0, SRC)
        import threading
        import scroll_capture as sc
        ctl = sc.CaptureControl() if hasattr(sc, "CaptureControl") else None
        if stop_after and ctl:
            threading.Timer(stop_after, ctl.stop.set).start()
        if cancel_after and ctl:
            threading.Timer(cancel_after, ctl.cancel.set).start()
        # 포인터를 영역 밖(왼쪽 가장자리)에 두고, 캡처 중 포인터가 움직이는지 기록
        def get_pos():
            if sys.platform == "darwin":
                return sc.pm.mouse_pos()
            import win32api
            return win32api.GetCursorPos()

        def set_pos(x, y):
            if sys.platform == "darwin":
                sc.pm.post_mouse("move", x, y)
            else:
                import win32api
                win32api.SetCursorPos((x, y))

        px0, py0, pw0, ph0 = sc.primary_screen()
        set_pos(px0 + 5, py0 + ph0 // 2)
        time.sleep(0.3)
        home = get_pos()
        moved = {"max": 0, "n": 0}
        sampling = {"on": True}

        def sampler():
            while sampling["on"]:
                try:
                    x, y = get_pos()
                    moved["max"] = max(moved["max"], abs(x - home[0]) + abs(y - home[1]))
                    moved["n"] += 1
                except Exception:
                    pass
                time.sleep(0.01)

        threading.Thread(target=sampler, daemon=True).start()
        t0 = time.time()
        try:
            # 앱 옵션 재현: --opt-sticky(고정 메뉴 처리 켬), --speed 0.5 등
            opts = {"auto_top": True, "speed": float(_sarg("--speed") or 1.0),
                    "handle_sticky": "--opt-sticky" in sys.argv}
            if ctl:
                opts["control"] = ctl
            img = sc.capture_scrolling_region(region, lambda m: None, opts)
        except getattr(sc, "CaptureCancelled", KeyboardInterrupt):
            print("cancelled (nothing saved)")
            print("PASS" if cancel_after else "FAIL: unexpected cancel")
            return 0 if cancel_after else 1
        dt = time.time() - t0
        sampling["on"] = False
        print(f"pointer: moved max {moved['max']}px during capture "
              f"({moved['n']} samples)", flush=True)
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
              f"height ratio={ratio:.1f}  time={dt:.0f}s  "
              f"methods={dict(getattr(sc, 'METHOD_STATS', {}))}  -> {out}", flush=True)
        if url:                                  # 실제 페이지: 이미지 비교용(검사 없음)
            print("DONE")
            return 0
        if stop_after:
            ok = ctl.stopped and 1.0 <= ratio < 9
            print(f"stopped={ctl.stopped}")
            print("PASS" if ok else "FAIL: stop did not cut the capture")
            return 0 if ok else 1
        try:
            with open(os.environ["SC_LOG"], encoding="utf-8") as f:
                for ln in f:
                    if " bands " in ln or "right band" in ln:
                        print("log:", ln.split(" ", 1)[1].strip(), flush=True)
        except OSError:
            pass
        problems, seq, bar_rows = check_lines(img, cell * scale)
        import numpy as np
        if right_edge:
            # 파란 띠가 스크롤바 바로 왼쪽(내용 끝 8px)까지 남아 있는 행 비율(기대: 약 1/4)
            sbi = int(round(max(0, sbw - 4) * scale))       # 결과 안 스크롤바 폭
            x1 = img.shape[1] - sbi
            bb, gg, rr = (img[:, x1 - int(10 * scale):x1 - int(2 * scale), k].astype(int)
                          for k in range(3))
            blue = (bb > 200) & (rr < 90) & (gg > 60) & (gg < 150)
            rows_b = blue.mean(axis=1) > 0.5
            n0 = int(region[3] * scale)              # 첫 화면(그대로 찍힌 부분)과 비교
            f0 = float(rows_b[:n0].mean())
            frac = float(rows_b[n0:].mean()) if rows_b.size > n0 else 0.0
            print(f"right edge blue rows: {frac:.3f} (first screen {f0:.3f}, "
                  f"scrollbar {sbi}px)", flush=True)
            if f0 < 0.05 or frac < f0 * 0.7:
                problems.append(f"right edge content lost (blue rows {frac:.3f} vs {f0:.3f})")
            if sbi > 2:
                # 스크롤바 썸이 결과에 반복해 찍히지 않았는지(첫 화면 1회는 허용)
                e = img[:, x1 + 1:x1 + sbi - 1].astype(int)
                ref = np.median(e[-int(region[3] * scale) // 2:].reshape(-1, 3), axis=0)
                diff = np.abs(e - ref).max(axis=2).mean(axis=1) > 25
                d_rows = int(diff[int(region[3] * scale):].sum())
                print(f"scrollbar thumb rows below first screen: {d_rows}", flush=True)
                if d_rows > 60 * scale:
                    problems.append(f"scrollbar thumb repeated ({d_rows} rows)")
        if use_chrome:
            # 스크롤바 썸(회색)이 결과 오른쪽 끝에 반복해 찍히지 않았는지
            e = img[:, -14:].astype(int)
            gray = ((np.abs(e[:, :, 0] - e[:, :, 1]) < 10) & (np.abs(e[:, :, 1] - e[:, :, 2]) < 10)
                    & (e[:, :, 1] > 90) & (e[:, :, 1] < 215))
            g_rows = int((gray.mean(axis=1) > 0.3).sum())
            print(f"scrollbar-like rows at right edge: {g_rows}", flush=True)
            if g_rows > 400 * scale:
                problems.append(f"scrollbar repeated at right edge ({g_rows} rows)")
        print(f"lines: {len(seq)} found ({seq[:1]}..{seq[-1:]}), "
              f"sticky bar rows in result={bar_rows}", flush=True)
        if bar_rows > 40 * scale:
            problems.append(f"sticky bar repeated ({bar_rows} rows)")
        if problems:
            print("FAIL: " + " / ".join(problems))
            return 1
        print("PASS")
        return 0
    finally:
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass


if __name__ == "__main__":
    if "--target" in sys.argv:
        run_target("--sticky" in sys.argv, "--right-edge" in sys.argv)
    else:
        sys.exit(main())
