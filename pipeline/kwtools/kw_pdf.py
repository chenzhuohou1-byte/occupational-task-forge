#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文 PDF 底座（reportlab）。零领域耦合。

本机没装 CJK 字体文件，也不必装：reportlab 自带 UnicodeCIDFont("STSong-Light")，
Ω／Δ／℃／±／µ 都能正常渲染，且能被 pypdf 正确提取（泄漏扫描依赖这一点）。
装 reportlab 走阿里镜像：
    python3 -m pip install --user -i https://mirrors.aliyun.com/pypi/simple/ reportlab
"""
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
CN = "STSong-Light"

H1 = ParagraphStyle("H1", fontName=CN, fontSize=18, leading=26, alignment=TA_CENTER, spaceAfter=4)
H2 = ParagraphStyle("H2", fontName=CN, fontSize=11.5, leading=18, alignment=TA_CENTER, spaceAfter=10)
H3 = ParagraphStyle("H3", fontName=CN, fontSize=11.5, leading=18, alignment=TA_LEFT,
                    spaceBefore=8, spaceAfter=4)
BODY = ParagraphStyle("BODY", fontName=CN, fontSize=9.5, leading=15, alignment=TA_LEFT)
SMALL = ParagraphStyle("SMALL", fontName=CN, fontSize=8.5, leading=13, alignment=TA_LEFT)

GRID = TableStyle([
    ("FONTNAME", (0, 0), (-1, -1), CN),
    ("FONTSIZE", (0, 0), (-1, -1), 8.5),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#808080")),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("ALIGN", (0, 0), (-1, 0), "CENTER"),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
])

INFO = TableStyle([
    ("FONTNAME", (0, 0), (-1, -1), CN),
    ("FONTSIZE", (0, 0), (-1, -1), 9),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#808080")),
    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f2")),
    ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f2f2f2")),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
])


def build(path, flow, title=""):
    """把 flowable 列表落成 A4 PDF。"""
    SimpleDocTemplate(path, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                      leftMargin=20 * mm, rightMargin=20 * mm, title=title).build(flow)
    print(f"  OK {path}")
