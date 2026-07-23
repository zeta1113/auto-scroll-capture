# -*- coding: utf-8 -*-
"""
빌드 스크립트
  1) source/version.py 의 patch 버전을 자동 증가 (컴파일마다 버전 관리)
  2) PyInstaller 로 단일 실행 파일(ScrollCapture.exe) 빌드
  3) Inno Setup(iscc) 이 있으면 정식 설치 파일(Setup.exe),
     없으면 포터블 exe 를 release/ 폴더에 생성
  4) release/RELEASES.md 및 README.md 의 릴리즈 정보 갱신

사용:  python build.py            (patch 증가)
       python build.py --minor    (minor 증가, patch=0)
       python build.py --major    (major 증가, minor=patch=0)
"""

import os
import re
import sys
import shutil
import subprocess
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "source")
RELEASE = os.path.join(ROOT, "release")
BUILD = os.path.join(ROOT, "build")
TOOLS = os.path.join(ROOT, "tools")
VERSION_FILE = os.path.join(SRC, "version.py")
ENTRY = os.path.join(SRC, "scroll_capture.py")
ICON = os.path.join(SRC, "icon.ico")
CERT_CER = os.path.join(ROOT, "cert", "ScrollCapture.cer")
APP_NAME = "ScrollCapture"

INNO_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
]


def log(msg):
    print(f"[build] {msg}")


def ps(script, *args):
    """PowerShell 스크립트 실행 헬퍼."""
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", script] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


# ---------- 아이콘 ----------
def ensure_icon():
    if os.path.isfile(ICON):
        return
    maker = os.path.join(SRC, "make_icon.py")
    if os.path.isfile(maker):
        log("아이콘 생성...")
        subprocess.check_call([sys.executable, maker])


def ensure_capture_icons():
    """제공된 SVG를 렌더해 캡처 버튼용 assets/cap_*.png 생성(가능할 때)."""
    renderer = os.path.join(SRC, "render_icons.py")
    if not os.path.isfile(renderer):
        return
    r = subprocess.run([sys.executable, renderer],
                       capture_output=True, text=True)
    if r.returncode == 0:
        log("캡처 아이콘(assets) 렌더 완료")
    else:
        log("아이콘 렌더 건너뜀(기존 assets 사용): "
            + (r.stderr or "").strip().splitlines()[-1:][0] if r.stderr else "")


# ---------- 코드 서명 ----------
def ensure_cert():
    maker = os.path.join(TOOLS, "make_cert.ps1")
    if not os.path.isfile(maker):
        return False
    r = ps(maker, "-CerOut", CERT_CER)
    for line in (r.stdout or "").splitlines():
        log(f"cert: {line}")
    return os.path.isfile(CERT_CER)


def sign_file(path):
    signer = os.path.join(TOOLS, "sign.ps1")
    if not os.path.isfile(signer):
        return
    r = ps(signer, "-File", path)
    for line in (r.stdout or "").splitlines():
        log(f"sign: {line}")


# ---------- 1) 버전 관리 ----------
def bump_version(level="patch"):
    with open(VERSION_FILE, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text)
    if not m:
        raise RuntimeError("version.py 에서 __version__ 을 찾을 수 없습니다.")
    major, minor, patch = map(int, m.groups())
    if level == "major":
        major, minor, patch = major + 1, 0, 0
    elif level == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    new_ver = f"{major}.{minor}.{patch}"
    text = re.sub(r'(__version__\s*=\s*")\d+\.\d+\.\d+(")',
                  rf'\g<1>{new_ver}\g<2>', text)
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    log(f"버전 → {new_ver}")
    return new_ver


# ---------- 2) PyInstaller 빌드 ----------
def build_exe():
    dist = os.path.join(BUILD, "dist")
    work = os.path.join(BUILD, "work")
    spec = os.path.join(BUILD, "spec")
    for d in (dist, work, spec):
        os.makedirs(d, exist_ok=True)
    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--windowed", "--clean", "--noconfirm",
        "--name", APP_NAME,
        "--distpath", dist, "--workpath", work, "--specpath", spec,
        "--add-data", f"{os.path.join(SRC, 'version.py')}{sep}.",
        "--paths", SRC,
    ]
    if os.path.isfile(ICON):
        cmd += ["--add-data", f"{ICON}{sep}."]
    assets_dir = os.path.join(SRC, "assets")
    if os.path.isdir(assets_dir):
        cmd += ["--add-data", f"{assets_dir}{sep}assets"]
    cmd += [
        "--hidden-import", "win32gui",
        "--hidden-import", "win32api",
        "--hidden-import", "win32con",
        "--hidden-import", "win32process",
        "--hidden-import", "win32clipboard",
        "--hidden-import", "updater",
    ]
    if os.path.isfile(ICON):
        cmd += ["--icon", ICON]
    cmd.append(ENTRY)
    log("PyInstaller 실행...")
    subprocess.check_call(cmd)
    exe = os.path.join(dist, f"{APP_NAME}.exe")
    if not os.path.isfile(exe):
        raise RuntimeError("빌드된 exe 를 찾을 수 없습니다.")
    log(f"exe 생성: {exe}")
    return exe


# ---------- 3) 설치 파일 / 포터블 ----------
def find_iscc():
    for p in INNO_CANDIDATES:
        if os.path.isfile(p):
            return p
    found = shutil.which("iscc")
    return found


def make_installer(exe, version):
    os.makedirs(RELEASE, exist_ok=True)
    iscc = find_iscc()
    if iscc:
        iss = os.path.join(ROOT, "installer.iss")
        cmd = [
            iscc,
            f"/DMyAppVersion={version}",
            f"/DSrcExe={exe}",
            f"/DOutDir={RELEASE}",
            iss,
        ]
        log("Inno Setup 으로 설치 파일 생성...")
        subprocess.check_call(cmd)
        out = os.path.join(RELEASE, f"ScrollCapture_Setup_v{version}.exe")
        return out, "installer"
    # Inno Setup 없음 → 포터블 exe
    out = os.path.join(RELEASE, f"ScrollCapture_Portable_v{version}.exe")
    shutil.copy2(exe, out)
    log("Inno Setup 미설치 → 포터블 exe 생성")
    return out, "portable"


# ---------- 4) 릴리즈 정보 기록 ----------
def record_release(out_path, version, kind):
    os.makedirs(RELEASE, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    kind_ko = "설치 파일(Setup)" if kind == "installer" else "포터블(Portable) exe"
    line = (f"| v{version} | {ts} | {kind_ko} | "
            f"`{os.path.basename(out_path)}` | {size_mb:.1f} MB |\n")
    log_file = os.path.join(RELEASE, "RELEASES.md")
    if not os.path.isfile(log_file):
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("# 릴리즈 기록 (Releases)\n\n")
            f.write("| 버전 | 빌드 시각 | 형식 | 파일 | 크기 |\n")
            f.write("|------|-----------|------|------|------|\n")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line)
    log(f"릴리즈 기록 갱신: {log_file}")
    return size_mb, kind_ko


def read_version():
    with open(VERSION_FILE, encoding="utf-8") as f:
        m = re.search(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"', f.read())
    return m.group(1) if m else "0.0.0"


def main():
    if "--no-bump" in sys.argv:
        version = read_version()
        log(f"버전 유지 (no-bump) → {version}")
    else:
        level = "patch"
        if "--major" in sys.argv:
            level = "major"
        elif "--minor" in sys.argv:
            level = "minor"
        version = bump_version(level)

    ensure_icon()
    ensure_capture_icons()
    have_cert = ensure_cert()

    exe = build_exe()
    if have_cert:
        sign_file(exe)          # 설치본에 포함될 exe를 먼저 서명

    out, kind = make_installer(exe, version)
    if have_cert:
        sign_file(out)          # 설치 파일/포터블 자체도 서명

    size_mb, kind_ko = record_release(out, version, kind)

    print()
    log("=" * 48)
    log(f"완료!  버전 v{version} ({kind_ko})")
    log(f"결과물: {out}  ({size_mb:.1f} MB)")
    signed = "서명됨(자체 인증서)" if have_cert else "서명 안 됨"
    log(f"코드 서명: {signed}")
    log("=" * 48)
    if have_cert:
        print()
        log("※ 서명을 '신뢰할 수 있는 게시자'로 인식시키려면 최초 1회")
        log("   trust_cert.bat 을 실행하세요(관리자 승인 1회).")
    if kind == "portable":
        print()
        log("※ 정식 설치 파일(Setup.exe)을 만들려면 Inno Setup(무료)을 설치 후")
        log("  다시 python build.py 를 실행하세요:  https://jrsoftware.org/isdl.php")


if __name__ == "__main__":
    main()
