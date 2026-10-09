#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N4 造附件：把一份声明式附件规格落成真实的多格式办公文件。

为什么要这一层：附件必须是**真** Excel/Word/PDF——agent 得像人一样翻文件、跨表
对账，而不是读一段已经喂好的文本；但「填哪些数字」属于案例，「怎么排版」属于工具，
两件事混在一个脚本里写就没法复用。02_造价变更索赔 那条数据已经把这条缝切开过：
`case_data.py` 只有数据、`gen_all.py` 只排版落盘且声明「不含任何业务判断」。
本模块是 `gen_all.py` 的通用化——排版规则留在代码里，数据全部外移成一份 JSON 规格。
规格里不许出现表达式或代码字符串：谁都能审、能 diff、能被非编码的人改。

三方库只经 kw_* 底座间接使用：kw_xlsx→openpyxl、kw_pdf→reportlab、
kw_docx→macOS 自带 textutil。任一缺失只让那一份附件进 errors[]，不抛异常、不影响
同批其余文件——产线对外声明「纯标准库」，这里必须真能降级，不能靠"反正机器上装了"。
"""
import csv
import os
import re
import sys
from xml.sax.saxutils import escape as _esc

# kw_* 写文件底座。**仓内自带一份**（`pipeline/kwtools/`），这样 clone 下来就能造
# xlsx/pdf/docx/csv —— 原来只在 `~/Desktop/KW数据产线/KW数据构造_通用工具` 这个仓外
# 目录里找，协作者 clone 完造不出 xlsx/pdf/docx（CI 也当场报了这个错）。
# 仍保留 KW_TOOLS_DIR 与兄弟目录两条外部路径，且**优先级高于仓内**，便于临时换版本。
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDORED_TOOLS = os.path.join(_HERE, "kwtools")
_DEFAULT_TOOLS = os.path.expanduser("~/Desktop/KW数据产线/KW数据构造_通用工具")
_SIBLING_TOOLS = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "KW数据构造_通用工具")

SUPPORTED = ("xlsx", "pdf", "docx", "csv", "md", "txt")


class MissingDep(Exception):
    """底座依赖缺失。单列一类，好让 errors[] 里写出人看得懂的降级原因。"""


def tools_dir():
    """返回第一个存在的 kw_* 工具目录；一个都没有返回空串。

    每次调用都重读环境变量，不在 import 期定死——跑测和换机器时要能临时改指向。
    顺序：KW_TOOLS_DIR（显式覆盖）→ **仓内自带**（默认，各机器行为一致）→ 兄弟目录 → 旧默认路径。
    """
    for p in (os.environ.get("KW_TOOLS_DIR", ""), _VENDORED_TOOLS,
              _SIBLING_TOOLS, _DEFAULT_TOOLS):
        if p and os.path.isdir(p):
            return p
    return ""



def _tool(name):
    """按需导入 kw_* 底座。导入失败统一抛 MissingDep，交给 materialize 记账。"""
    d = tools_dir()
    if not d:
        raise MissingDep("找不到 kw_* 通用工具目录（可用环境变量 KW_TOOLS_DIR 指定）")
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        return __import__(name)
    except ImportError as e:
        raise MissingDep("%s 不可用（%s）" % (name, e))


# ---------------- 共享常量替换 ----------------
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def _subst_str(s, consts):
    """把字符串里的 {键} 换成常量值。

    只替换 consts 里确实有的键，未知占位符**原样保留**：正文里出现裸花括号
    （公式、模板占位、代码片段）是常事，用 str.format 做替换会被一个花括号
    打死整份规格——这个坑在 harbor 的题面里踩过一次，不再踩。
    """
    if "{" not in s:
        return s
    return _PLACEHOLDER.sub(
        lambda m: str(consts[m.group(1)]) if m.group(1) in consts else m.group(0), s)


def _subst(obj, consts):
    """递归替换容器里所有字符串；数字/布尔/None 原样透传。

    透传是必须的：xlsx 的数值单元格靠原生 int/float 才能套千分位与被公式引用，
    一旦被 str 化，整张表就退化成文本表。
    """
    if isinstance(obj, str):
        return _subst_str(obj, consts)
    if isinstance(obj, list):
        return [_subst(x, consts) for x in obj]
    if isinstance(obj, dict):
        return {k: _subst(v, consts) for k, v in obj.items()}
    return obj


def resolve_constants(consts):
    """常量之间也允许互相引用（如 项目部 = "{承包人}第二项目部"），最多展开三轮。"""
    out = {str(k): v for k, v in (consts or {}).items()}
    for _ in range(3):
        nxt = {k: (_subst_str(v, out) if isinstance(v, str) else v)
               for k, v in out.items()}
        if nxt == out:
            break
        out = nxt
    return out


# ---------------- 各格式写盘器 ----------------
def _rows(item):
    """「表头 + 数据」拼成二维行；两者都可缺省。标量行自动裹成单元格。"""
    rows = []
    if item.get("表头"):
        rows.append(list(item["表头"]))
    for r in item.get("数据") or []:
        rows.append(list(r) if isinstance(r, (list, tuple)) else [r])
    return rows


def _write_xlsx(path, item):
    """xlsx：多 sheet，每表可有跨列标题行 + 表头 + 行数据 + 列宽 + 金额列。"""
    kw_xlsx = _tool("kw_xlsx")
    sheets = []
    # 单表文件允许把字段直接写在文件层，不必套一层 sheets
    for sh in item.get("sheets") or [item]:
        titles = [[t] for t in (sh.get("标题") or [])]
        head = list(sh.get("表头") or [])
        rows = titles + ([head] if head else []) + \
            [list(r) if isinstance(r, (list, tuple)) else [r] for r in (sh.get("数据") or [])]
        sheets.append({
            "name": sh.get("名称") or "Sheet1",
            "rows": rows,
            "widths": sh.get("列宽"),
            "head_rows": len(titles) + (1 if head else 0),
            "money_cols": sh.get("金额列") or (),
        })
    kw_xlsx.write(path, sheets)


# PDF 块类型别名：规格写中文，也认 case_data.py 里用过的英文短名
_PDF_KIND = {
    "标题": "h1", "副标题": "h2", "小标题": "h3", "正文": "p", "小字": "small",
    "表格": "table", "分页": "pagebreak", "空行": "spacer",
    "h1": "h1", "h2": "h2", "h3": "h3", "p": "p", "small": "small",
    "table": "table", "pagebreak": "pagebreak", "spacer": "spacer",
}


def _write_pdf(path, item):
    """pdf：块列表顺序排版。块＝标题/副标题/小标题/正文/小字/表格/分页/空行。"""
    kw_pdf = _tool("kw_pdf")
    styles = {"h1": kw_pdf.H1, "h2": kw_pdf.H2, "h3": kw_pdf.H3,
              "p": kw_pdf.BODY, "small": kw_pdf.SMALL}
    flow = []
    for blk in item.get("块") or []:
        raw_kind = str(blk.get("块") or "正文")
        kind = _PDF_KIND.get(raw_kind)
        if kind is None:
            raise ValueError("PDF 不认识的块类型：%s" % raw_kind)
        if kind == "table":
            # 单元格一律裹 Paragraph：reportlab 的裸字符串不换行，长文本直接溢出页面
            data = [[kw_pdf.Paragraph(_esc(str(c)), kw_pdf.SMALL) for c in r]
                    for r in _rows(blk)]
            if not data:
                raise ValueError("表格块没有任何行")
            t = kw_pdf.Table(data, colWidths=blk.get("列宽"))
            t.setStyle(kw_pdf.INFO if blk.get("样式") == "信息表" else kw_pdf.GRID)
            flow.append(t)
            flow.append(kw_pdf.Spacer(1, 6))
        elif kind == "pagebreak":
            flow.append(kw_pdf.PageBreak())
        elif kind == "spacer":
            flow.append(kw_pdf.Spacer(1, float(blk.get("高", 6))))
        else:
            flow.append(kw_pdf.Paragraph(_esc(str(blk.get("文本", ""))), styles[kind]))
            if kind == "p":
                flow.append(kw_pdf.Spacer(1, 3))
    if not flow:
        raise ValueError("pdf 规格没有任何块")
    kw_pdf.build(path, flow, item.get("标题") or "")


_DOCX_TAG = {"标题": "h1", "副标题": "h2", "小标题": "h3", "正文": "p", "小字": "p"}


def _write_docx(path, item):
    """docx：块列表 → HTML → textutil 转真 docx。也接受 html 字段直接透传。"""
    kw_docx = _tool("kw_docx")
    html = item.get("html")
    if html is None:
        parts = []
        for blk in item.get("块") or []:
            kind = str(blk.get("块") or "正文")
            if kind in ("表格", "table"):
                rows = _rows(blk)
                has_head = bool(blk.get("表头"))
                trs = []
                for i, r in enumerate(rows):
                    tag = "th" if (i == 0 and has_head) else "td"
                    trs.append("<tr>" + "".join(
                        "<%s>%s</%s>" % (tag, _esc(str(c)), tag) for c in r) + "</tr>")
                parts.append("<table>" + "".join(trs) + "</table>")
            elif kind in ("列表", "list"):
                parts.append("<ul>" + "".join(
                    "<li>%s</li>" % _esc(str(x)) for x in (blk.get("条目") or [])) + "</ul>")
            else:
                tag = _DOCX_TAG.get(kind)
                if tag is None:
                    raise ValueError("docx 不认识的块类型：%s" % kind)
                parts.append("<%s>%s</%s>" % (tag, _esc(str(blk.get("文本", ""))), tag))
        html = "".join(parts)
    if not str(html).strip():
        raise ValueError("docx 规格没有任何正文")
    try:
        kw_docx.html_to_docx(html, path, item.get("标题") or "")
    except FileNotFoundError as e:
        raise MissingDep("docx 依赖 macOS 自带 textutil（%s）" % e)


def _write_csv(path, item):
    """csv：表头 + 行数据。默认带 BOM，Excel 双击打开中文才不乱码（同 kw_csv 口径）。"""
    rows = _rows(item)
    if not rows:
        raise ValueError("csv 规格没有任何行")
    enc = "utf-8" if item.get("带BOM") is False else "utf-8-sig"
    with open(path, "w", newline="", encoding=enc) as f:
        csv.writer(f).writerows(rows)
    print("  OK %s" % path)


def _write_text(path, item):
    """md / txt：给 文本（整段）或 行（逐行）二者之一。"""
    text = item.get("文本")
    if text is None:
        text = "\n".join(str(x) for x in (item.get("行") or []))
    if not str(text).strip():
        raise ValueError("文本规格为空")
    if not text.endswith("\n"):
        text += "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print("  OK %s" % path)


WRITERS = {"xlsx": _write_xlsx, "pdf": _write_pdf, "docx": _write_docx,
           "csv": _write_csv, "md": _write_text, "txt": _write_text}


def _ext(rel):
    return rel.rsplit(".", 1)[-1].lower() if "." in os.path.basename(rel) else ""


def _safe_join(out_dir, rel):
    """把相对路径拼进 out_dir，拒绝绝对路径与用 .. 逃出目录的写法。"""
    if os.path.isabs(rel) or rel.startswith("~"):
        raise ValueError("附件路径必须是相对路径")
    path = os.path.normpath(os.path.join(out_dir, rel))
    if not path.startswith(out_dir + os.sep):
        raise ValueError("附件路径逃出了输出目录")
    return path


def materialize(spec, out_dir):
    """按附件规格生成真实文件到 out_dir。

    规格形状（纯数据，键全中文；完整示例见 attachments_selftest.py 的 TOY_SPEC）：

        {"常量": {"工程名称": "…", "承包人": "…"},          # 可选，字符串里用 {工程名称} 引用
         "文件": {"02_变更/签证单/JZ-018.pdf": {"类型": "pdf", …},
                  "01_清单/清单.xlsx": {"类型": "xlsx", "sheets": [ … ]}}}

    行为契约：
      - 返回 {"written": [...], "skipped": [...], "errors": [...]}，元素都是相对路径
        （errors 项形如 "相对路径：原因"），顺序与规格里 文件 段的书写顺序一致。
      - 一份文件出错（缺依赖 / 规格写错 / 类型不支持 / 路径不合法）只记进 errors[]，
        既不抛异常也不中断同批其余文件——这是 N4 能被批量调用的前提。
      - 目标目录按相对路径自动逐级创建，支持任意层嵌套。
      - 文件项写 {"跳过": true} 表示这份附件有意留给人工（扫描件之类），记进 skipped[]。
      - 类型可省略，按扩展名推断。
      - 写完复查大小为 0 的算失败：宁可报错，也不留一份"存在但空"的假附件。
    """
    if not isinstance(spec, dict):
        return {"written": [], "skipped": [], "errors": ["附件规格不是对象"]}
    files = spec.get("文件")
    if not isinstance(files, dict):
        return {"written": [], "skipped": [],
                "errors": ["附件规格缺「文件」段（相对路径 → 文件定义）"]}

    consts = resolve_constants(spec.get("常量"))
    out_dir = os.path.abspath(out_dir)
    written, skipped, errors = [], [], []

    for rel, raw in files.items():
        try:
            if not isinstance(raw, dict):
                raise ValueError("文件定义必须是对象")
            if raw.get("跳过"):
                skipped.append("%s（规格标为人工填）" % rel)
                continue
            item = _subst(raw, consts)
            kind = str(item.get("类型") or _ext(rel)).lower()
            writer = WRITERS.get(kind)
            if writer is None:
                raise ValueError("不支持的类型 %s（支持 %s）" % (kind or "(空)", "/".join(SUPPORTED)))
            path = _safe_join(out_dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            writer(path, item)
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                raise ValueError("写出的文件不存在或为空")
            written.append(rel)
        except MissingDep as e:
            errors.append("%s：依赖缺失，已跳过 —— %s" % (rel, e))
        except Exception as e:  # noqa: BLE001 单份附件出错不许中断整批
            errors.append("%s：%s: %s" % (rel, type(e).__name__, e))

    return {"written": written, "skipped": skipped, "errors": errors}


if __name__ == "__main__":
    # 单独跑 N4：python3 -m pipeline.attachments <规格.json> <输出目录>
    import json
    if len(sys.argv) < 3:
        sys.exit("用法：python3 -m pipeline.attachments <规格.json> <输出目录>")
    with open(sys.argv[1], encoding="utf-8") as _f:
        _spec = json.load(_f)
    print(json.dumps(materialize(_spec, sys.argv[2]), ensure_ascii=False, indent=2))
