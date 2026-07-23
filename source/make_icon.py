# -*- coding: utf-8 -*-
"""
앱 아이콘 생성기 — 동그라미 위에 '캡처 사각형'(고해상도).
파란 원 + 흰색 캡처 프레임(모서리 브래킷) + 중앙 점.
8x 슈퍼샘플링으로 각 크기를 개별 렌더링해 선명한 멀티해상도 .ico 생성.
실행: python make_icon.py  ->  icon.ico
"""

import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")

CIRCLE = (37, 125, 246)       # 파랑
CIRCLE_HI = (90, 165, 255)    # 원 상단 하이라이트(살짝 밝게)
FRAME = (255, 255, 255)       # 흰색
SS = 8                        # 슈퍼샘플링 배수


def render(size):
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 동그라미(단색) + 상단 하이라이트
    pad = max(1, int(s * 0.055))
    d.ellipse([pad, pad, s - pad, s - pad], fill=CIRCLE + (255,))
    hl_pad = int(s * 0.14)
    hl = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(hl).ellipse(
        [hl_pad, int(s * 0.10), s - hl_pad, int(s * 0.62)],
        fill=CIRCLE_HI + (90,))
    img.alpha_composite(hl)
    d = ImageDraw.Draw(img)

    # 캡처 사각형(모서리 브래킷)
    m = int(s * 0.30)
    x0, y0, x1, y1 = m, m, s - m, s - m
    t = max(2, int(round(s * 0.055)))
    seg = int((x1 - x0) * 0.32)

    def bracket(cx, cy, dx, dy):
        d.line([(cx, cy), (cx + dx, cy)], fill=FRAME + (255,), width=t)
        d.line([(cx, cy), (cx, cy + dy)], fill=FRAME + (255,), width=t)
        # 모서리를 둥글게(작은 원으로 이음새 매끈)
        r = t // 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=FRAME + (255,))

    bracket(x0, y0, seg, seg)
    bracket(x1, y0, -seg, seg)
    bracket(x0, y1, seg, -seg)
    bracket(x1, y1, -seg, -seg)

    # 중앙 점
    r = int(s * 0.058)
    cx, cy = s // 2, s // 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=FRAME + (255,))

    return img.resize((size, size), Image.LANCZOS)


def main():
    sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
    frames = [render(sz) for sz in sizes]
    frames[-1].save(OUT, format="ICO",
                    sizes=[(sz, sz) for sz in sizes],
                    append_images=frames[:-1])
    print("생성:", OUT)


if __name__ == "__main__":
    main()
