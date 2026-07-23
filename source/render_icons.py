# -*- coding: utf-8 -*-
"""
제공된 SVG 아이콘 세트(capture-icons.svg)를 '변형 없이' 그대로 렌더링해
7개 캡처 버튼용 PNG(assets/cap_{mode}.png)를 생성한다.

- 각 카드(240x190, 원본 가로세로 비율 유지)를 개별로 렌더
- 카드 배경(#F4F6F9 라운드) + 글리프만 남기고 하단 텍스트는 제외
- 빌드 시 1회 실행 → 런타임은 PNG만 로드(svglib/reportlab 런타임 의존성 없음)

필요 패키지(빌드 환경): svglib, reportlab, rlPyCairo, pycairo
"""

import io
import os
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
NS = "{%s}" % SVG_NS
HERE = os.path.dirname(os.path.abspath(__file__))
SRC_SVG = os.path.join(HERE, "capture-icons.svg")
OUT_DIR = os.path.join(HERE, "assets")

# 원본 SVG의 카드 순서 → 앱 모드
MODES = ["scroll_display", "scroll_window", "scroll_region",
         "shot_all", "shot_display", "shot_window", "shot_region"]

CARD_W, CARD_H, CARD_RX, CARD_BG = 240, 190, 22, "#F4F6F9"


def build_card_svgs():
    """원본 SVG에서 카드별 (mode, svg_bytes) 목록을 만든다(텍스트 제외)."""
    ET.register_namespace("", SVG_NS)
    root = ET.parse(SRC_SVG).getroot()
    cards = root.findall(NS + "g")           # 7개 카드 g
    out = []
    for card, mode in zip(cards, MODES):
        glyph = card.find(NS + "g")          # 카드 안 글리프 g (translate(120,95)...)
        svg = ET.Element(NS + "svg", {
            "xmlns": SVG_NS,
            "viewBox": f"0 0 {CARD_W} {CARD_H}",
            "width": str(CARD_W), "height": str(CARD_H),
        })
        ET.SubElement(svg, NS + "rect", {
            "x": "0", "y": "0", "width": str(CARD_W), "height": str(CARD_H),
            "rx": str(CARD_RX), "fill": CARD_BG,
        })
        svg.append(glyph)                    # 글리프 그대로 삽입(변형 없음)
        out.append((mode, ET.tostring(svg, encoding="utf-8")))
    return out


def render(dpi=250):
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPM
    os.makedirs(OUT_DIR, exist_ok=True)
    made = []
    for mode, svg_bytes in build_card_svgs():
        drawing = svg2rlg(io.BytesIO(svg_bytes))
        out_png = os.path.join(OUT_DIR, f"cap_{mode}.png")
        renderPM.drawToFile(drawing, out_png, fmt="PNG", dpi=dpi)
        made.append(out_png)
    return made


if __name__ == "__main__":
    for p in render():
        print("wrote", p)
