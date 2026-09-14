# -*- coding: utf-8 -*-
"""
제공된 아이콘 이미지(capture-icons.png)를 '변형 없이' 그대로 사용해
7개 캡처 버튼용 PNG(assets/cap_{mode}.png)를 생성한다.

- 원본 이미지에서 카드 경계를 자동 감지해 7개로 분할(크롭)
- 원본 픽셀/가로세로 비율을 그대로 유지
- 의존성: numpy, Pillow (앱과 동일 — 추가 빌드 의존성 없음)
"""

import os
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_IMG = os.path.join(HERE, "capture-icons.png")
OUT_DIR = os.path.join(HERE, "assets")

# 원본 이미지의 카드 순서 → 앱 모드
MODES = ["scroll_display", "scroll_window", "scroll_region",
         "shot_all", "shot_display", "shot_window", "shot_region"]


def _runs(flags, gap=30):
    idx = np.where(flags)[0]
    groups, s, p = [], idx[0], idx[0]
    for i in idx[1:]:
        if i > p + gap:
            groups.append((int(s), int(p)))
            s = i
        p = i
    groups.append((int(s), int(p)))
    return groups


def render(pad=10):
    im = Image.open(SRC_IMG).convert("RGB")
    a = np.array(im)
    H, W = a.shape[:2]
    mask = (a < 250).any(axis=2)             # 카드(연회색)+글리프 영역
    col_runs = _runs(mask.any(axis=0))       # 가로로 카드 7개 분리
    if len(col_runs) != len(MODES):
        raise RuntimeError(f"카드 {len(MODES)}개를 찾지 못함: {len(col_runs)}")
    ys = np.where(mask.any(axis=1))[0]
    y0, y1 = int(ys.min()), int(ys.max())

    os.makedirs(OUT_DIR, exist_ok=True)
    made = []
    for (x0, x1), mode in zip(col_runs, MODES):
        box = (max(0, x0 - pad), max(0, y0 - pad),
               min(W, x1 + pad), min(H, y1 + pad))
        c = np.array(im.crop(box).convert("RGBA"))
        # 카드 밖 흰 여백/둥근 모서리 바깥을 투명 처리 → 버튼에서 카드 라운딩이 살아남
        whitish = (c[:, :, 0] >= 250) & (c[:, :, 1] >= 250) & (c[:, :, 2] >= 250)
        c[whitish, 3] = 0
        out = os.path.join(OUT_DIR, f"cap_{mode}.png")
        Image.fromarray(c, "RGBA").save(out)
        made.append(out)
    return made


if __name__ == "__main__":
    for p in render():
        print("wrote", p)
