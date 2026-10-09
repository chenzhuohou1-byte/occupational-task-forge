#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打交付 zip。零领域耦合，任何 knowledge work 数据条都能直接用。

为什么不用 macOS 的 zip 命令：`zip -r -X` 会把 UTF-8 文件名的字节写进包里，
但不置 UTF-8 标志位（bit 11）。Windows 侧和 Python zipfile 都会按 cp437 解码，
中文目录名全成乱码。实测 16 个条目 0 个置位。Python zipfile 对非 ASCII 名会
自动置位，实测 16/16。

用法：
    python3 kw_pack.py ~/Desktop/KW数据产线/01_计量校准_超差追溯/计量与检测_20260914
    python3 kw_pack.py <目录> <输出.zip>
"""
import os
import sys
import zipfile

SKIP = {".DS_Store", "Thumbs.db", "desktop.ini"}


def pack(src_dir, out_zip=None):
    src_dir = os.path.abspath(src_dir).rstrip("/")
    out_zip = out_zip or src_dir + ".zip"
    root = os.path.dirname(src_dir)
    base = os.path.basename(src_dir)

    if os.path.exists(out_zip):
        os.remove(out_zip)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(src_dir):
            dirnames.sort()
            filenames.sort()
            arc_dir = os.path.relpath(dirpath, root)
            z.write(dirpath, arc_dir + "/")
            for fn in filenames:
                if fn in SKIP:
                    continue
                full = os.path.join(dirpath, fn)
                z.write(full, os.path.join(arc_dir, fn))

    z = zipfile.ZipFile(out_zip)
    items = z.infolist()
    utf8 = sum(bool(i.flag_bits & 0x800) for i in items)
    files = [i for i in items if not i.filename.endswith("/")]
    bad = [i.filename for i in items
           if not (i.flag_bits & 0x800) and any(ord(c) > 127 for c in i.filename)]
    crc = z.testzip()

    print(f"{out_zip}")
    print(f"  条目 {len(items)}（文件 {len(files)}、目录 {len(items) - len(files)}）")
    print(f"  UTF-8 标志位 {utf8}/{len(items)}"
          + (f"  ⚠️ 未置位且含非 ASCII：{bad}" if bad else "  OK"))
    print(f"  CRC {crc or 'OK'}")
    print(f"  大小 {os.path.getsize(out_zip) / 1024:.0f} KB")
    assert not bad, "有非 ASCII 文件名未置 UTF-8 标志位"
    assert crc is None, f"CRC 校验失败：{crc}"
    return out_zip


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    pack(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
