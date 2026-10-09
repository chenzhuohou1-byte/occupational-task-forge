#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KA 存档表的读写与同步。零领域耦合。

两个用途：
  1. upsert_row()  按「任务类型」列做键，新增或覆盖一行（不会重复插入）
  2. sync_cell()   把某一行的某一列同步为一个文件的当前内容（prompt 定稿常用）

prompt 存在三处副本（设计稿 / prompt_final.txt / CSV 单元格），改一处必须同步，
本模块保证 CSV 那一处永远等于 txt 文件，且同步后回读校验逐字一致。
"""
import csv
import os

CSV_DEFAULT = os.path.expanduser("~/Desktop/KW数据产线/KA类数据构造存档.csv")
KEY_COL = 5          # 「任务类型」列下标，作为行的唯一键
PROMPT_COL = 3       # 「任务 Prompt」列下标
ENC = "utf-8-sig"    # Excel 打得开中文，必须带 BOM


def _read(path):
    with open(path, newline="", encoding=ENC) as f:
        return list(csv.reader(f))


def _write(path, rows):
    with open(path, "w", newline="", encoding=ENC) as f:
        csv.writer(f).writerows(rows)


def han(s):
    """汉字数。规范的篇幅要求按汉字算，不是字符数。"""
    return sum(1 for c in s if "一" <= c <= "鿿")


def upsert_row(values, path=CSV_DEFAULT):
    """values 为前 N 列的值，按表头宽度补空。已存在同「任务类型」的行则覆盖。"""
    rows = _read(path)
    header = rows[0]
    row = list(values) + [""] * (len(header) - len(values))
    body = [r for r in rows[1:] if any(c.strip() for c in r)]
    key = row[KEY_COL]

    for i, r in enumerate(body):
        if r[KEY_COL] == key:
            body[i] = row
            action = "覆盖"
            break
    else:
        body.append(row)
        action = "新增"

    _write(path, [header] + body)
    print(f"{action}一行：{key}（现有 {len(body)} 行，{len(header)} 列）")
    return row


def sync_cell(key, src_file, col=PROMPT_COL, path=CSV_DEFAULT):
    """把 key 所在行的第 col 列同步为 src_file 的内容，并回读校验。"""
    text = open(src_file, encoding="utf-8").read().strip()
    rows = _read(path)
    hit = [r for r in rows[1:] if r and r[KEY_COL].startswith(key)]
    assert len(hit) == 1, f"按「{key}」匹配到 {len(hit)} 行，应为 1 行"
    old = hit[0][col]
    hit[0][col] = text
    _write(path, rows)

    back = [r for r in _read(path)[1:] if r and r[KEY_COL].startswith(key)][0]
    ok = back[col] == text
    print(f"同步第 {col} 列：{han(old)} 汉字 -> {han(text)} 汉字 | 回读逐字一致：{ok}")
    assert ok, "回读不一致，CSV 转义有问题"
    return text
