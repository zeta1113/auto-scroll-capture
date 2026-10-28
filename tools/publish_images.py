# -*- coding: utf-8 -*-
"""
CI 결과 이미지를 저장소의 별도 브랜치(예: ci-art-win)에 올린다 — 눈으로 비교하기 위함.

  python tools/publish_images.py <브랜치> <파일 패턴...>

PNG 는 폭 900px 이하 JPEG 로 줄이고, 긴 이미지는 2400px 높이로 나눠 저장한다.
txt 파일은 그대로 올린다. 브랜치는 매번 새로(force) 덮어쓴다.
"""

import os
import sys
import glob
import shutil
import tempfile
import subprocess

import cv2
import numpy as np


def main():
    branch = sys.argv[1]
    files = []
    for pat in sys.argv[2:]:
        files += glob.glob(pat)
    d = tempfile.mkdtemp(prefix="ci_art_")
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        if f.lower().endswith(".txt"):
            shutil.copy(f, os.path.join(d, os.path.basename(f)))
            continue
        im = cv2.imdecode(np.fromfile(f, np.uint8), cv2.IMREAD_COLOR)
        if im is None:
            continue
        h, w = im.shape[:2]
        k = min(1.0, 900.0 / w)
        if k < 1.0:
            im = cv2.resize(im, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
        for i, y in enumerate(range(0, im.shape[0], 2400)):
            cv2.imwrite(os.path.join(d, f"{base}_{i:02d}.jpg"), im[y:y + 2400],
                        [cv2.IMWRITE_JPEG_QUALITY, 80])
    token = os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run = os.environ.get("GITHUB_RUN_ID", "")
    with open(os.path.join(d, "RUN.txt"), "w") as fh:
        fh.write(f"run {run}\n")

    def git(*a):
        subprocess.check_call(["git", *a], cwd=d)

    git("init", "-q")
    git("checkout", "-q", "-b", branch)
    git("config", "user.name", "ci")
    git("config", "user.email", "ci@users.noreply.github.com")
    git("add", "-A")
    git("commit", "-q", "-m", f"ci artifacts run {run}")
    git("push", "-q", "-f",
        f"https://x-access-token:{token}@github.com/{repo}.git", branch)
    print("published", len(os.listdir(d)), "files to", branch)


if __name__ == "__main__":
    main()
