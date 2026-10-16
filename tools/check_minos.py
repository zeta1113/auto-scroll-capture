# -*- coding: utf-8 -*-
"""
.app 번들 안의 모든 Mach-O 바이너리의 '최소 macOS 버전(minos)'을 검사한다.
지원 목표(기본 12.0 = Monterey)보다 높은 바이너리가 있으면 실패(exit 1).

  python tools/check_minos.py dist/ScrollCapture.app 12.0
"""

import os
import re
import sys
import subprocess


def minos_of(path):
    try:
        out = subprocess.run(["otool", "-l", path], capture_output=True,
                             text=True).stdout
    except Exception:
        return []
    vers = re.findall(r"minos (\d+(?:\.\d+)*)", out)                 # LC_BUILD_VERSION
    vers += re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version "
                       r"(\d+(?:\.\d+)*)", out)                         # 구형
    return vers


def vt(v):
    return tuple(int(x) for x in v.split("."))


def is_macho(path):
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except Exception:
        return False
    return magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                     b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


def main():
    root = sys.argv[1]
    target = vt(sys.argv[2] if len(sys.argv) > 2 else "12.0")
    worst, bad, n = (0,), [], 0
    for d, _, files in os.walk(root):
        for fn in files:
            p = os.path.join(d, fn)
            if os.path.islink(p) or not is_macho(p):
                continue
            n += 1
            for v in minos_of(p):
                worst = max(worst, vt(v))
                if vt(v) > target:
                    bad.append((v, os.path.relpath(p, root)))
    print(f"checked {n} Mach-O files, highest minos = "
          f"{'.'.join(map(str, worst))}")
    for v, p in sorted(set(bad)):
        print(f"  TOO NEW ({v}): {p}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
