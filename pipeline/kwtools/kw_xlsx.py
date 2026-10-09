#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文 xlsx 底座（openpyxl）。零领域耦合。

为什么单独一份：01（计量校准）与 02（造价变更索赔）两条数据的生成脚本各自抄了
一遍表格样式代码——细边框、表头底色、自动换行、列宽、金额千分位，样式要改就得
改两处。本模块把那份样式收在一起，调用方只给「行数据 + 列宽 + 表头行数 + 金额列」，
不含任何业务判断。

sheet 名的长度与非法字符由本模块兜住：Excel 限 31 字符且不许 []:*?/\\，越界时
openpyxl 直接抛异常，而规格里的表名往往是中文长句。

装 openpyxl 走阿里镜像：
    python3 -m pip install --user -i https://mirrors.aliyun.com/pypi/simple/ openpyxl
"""
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HEAD_FILL = PatternFill("solid", fgColor="E8EEF7")
BOLD = Font(name="Songti SC", bold=True, size=10)
NORM = Font(name="Songti SC", size=10)
THIN = Side(style="thin", color="808080")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="center")
MONEY = "#,##0.00"

_BAD_TITLE = re.compile(r"[\[\]:*?/\\]")


def safe_title(name, used=()):
    """把任意字符串收成合法 sheet 名：换掉非法字符、截到 31 字、重名补序号。"""
    title = _BAD_TITLE.sub("_", str(name or "Sheet")).strip() or "Sheet"
    title = title[:31]
    base, i = title, 2
    while title in used:
        suffix = "_%d" % i
        title = base[:31 - len(suffix)] + suffix
        i += 1
    return title


def fill_sheet(ws, rows, widths=None, head_rows=1, money_cols=()):
    """逐行写入并套样式。

    head_rows 为顶部表头行数（含跨列的大标题与说明行），这些行加粗并填底色；
    money_cols 为 1 起的列号，该列的数值单元格套千分位格式（文本单元格不动，
    因为「合计」「—」这类占位不该被当成数)。
    """
    for r in rows:
        ws.append(list(r))
    for i, row in enumerate(ws.iter_rows(), 1):
        for c in row:
            c.border = BOX
            c.font = BOLD if i <= head_rows else NORM
            c.alignment = WRAP
            if i <= head_rows:
                c.fill = HEAD_FILL
            elif c.column in money_cols and isinstance(c.value, (int, float)):
                c.number_format = MONEY
    for j, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    return ws


def write(path, sheets):
    """把多个 sheet 落成一个 xlsx。

    sheets 为 [{name, rows, widths, head_rows, money_cols}]，键名用英文是有意的：
    本层是排版底座、与领域无关，中文规格的翻译工作留给调用方。
    """
    wb = Workbook()
    wb.remove(wb.active)
    used = []
    for s in sheets:
        title = safe_title(s.get("name"), used)
        used.append(title)
        fill_sheet(wb.create_sheet(title), s.get("rows") or [], s.get("widths"),
                   s.get("head_rows", 1), tuple(s.get("money_cols") or ()))
    if not used:
        # 一个 sheet 都没有的 workbook 存不出来，给个空表兜底
        fill_sheet(wb.create_sheet("Sheet1"), [], None, 0)
    wb.save(path)
    print(f"  OK {path}")
    return path


def sheet_text(path):
    """读回全部 sheet 的纯文本，供泄漏扫描与自测用。制表符分列，`# sheet:` 标表名。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append("# sheet: %s" % ws.title)
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if any(cells):
                lines.append("\t".join(cells))
    return "\n".join(lines)
