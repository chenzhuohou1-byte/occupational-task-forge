#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文 docx 生成。零领域耦合、**纯标准库、跨平台**。

原版靠 macOS 自带 `textutil` 把 HTML 转 docx —— 在 Linux（CI、协作者机器）上
直接没有这个命令，附件就只能降级跳过。docx 本身是一个 zip 装着 XML，用
zipfile + 手写 XML 就能产出真 docx，不依赖 python-docx 也不依赖 textutil。

支持的 HTML 子集（与原版一致，attachments.py 只用这些）：
  <h1>/<h2>/<h3>  标题     <p>  段落     <br>  换行
  <table><tr><th>/<td>     表格（带边框，表头浅底）
其余标签按「取其文字」处理，不报错。产出的 docx 能被 `kw_docx.docx_text()`
以及 `pipeline.qc._docx_text()` 读回纯文本（泄漏扫描依赖这一点）。
"""
import os
import re
import zipfile
from html.parser import HTMLParser
from xml.sax.saxutils import escape

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles %s>
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Songti SC" w:eastAsia="Songti SC" w:hAnsi="Songti SC"/>
<w:sz w:val="21"/></w:rPr></w:rPrDefault></w:docDefaults>
</w:styles>""" % _W

_SZ = {"h1": 32, "h2": 21, "h3": 24, "p": 21, "td": 19}
_ALIGN = {"h1": "center", "h2": "center"}


def _runs(text):
    out = []
    for i, seg in enumerate(text.split("\n")):
        if i:
            out.append("<w:r><w:br/></w:r>")
        if seg:
            out.append('<w:r><w:t xml:space="preserve">%s</w:t></w:r>' % escape(seg))
    return "".join(out) or "<w:r/>"


def _para(text, kind="p", bold=False):
    pr = ['<w:spacing w:before="40" w:after="40"/>']
    if kind in _ALIGN:
        pr.append('<w:jc w:val="%s"/>' % _ALIGN[kind])
    rpr = '<w:rPr><w:sz w:val="%d"/>%s</w:rPr>' % (
        _SZ.get(kind, 21), "<w:b/>" if (bold or kind in ("h1", "h3")) else "")
    body = _runs(text).replace("<w:r>", "<w:r>" + rpr)
    return "<w:p><w:pPr>%s%s</w:pPr>%s</w:p>" % (
        "".join(pr), '<w:rPr><w:sz w:val="%d"/></w:rPr>' % _SZ.get(kind, 21), body)


def _cell(text, header=False):
    shade = '<w:shd w:val="clear" w:fill="E8EEF7"/>' if header else ""
    return ("<w:tc><w:tcPr>%s</w:tcPr>%s</w:tc>"
            % (shade, _para(text, "td", bold=header)))


def _table(rows):
    borders = "".join('<w:%s w:val="single" w:sz="6" w:color="808080"/>' % b
                      for b in ("top", "left", "bottom", "right", "insideH", "insideV"))
    out = ['<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>'
           '<w:tblBorders>%s</w:tblBorders></w:tblPr>' % borders]
    for cells, header in rows:
        out.append("<w:tr>" + "".join(_cell(c, header) for c in cells) + "</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


class _Reader(HTMLParser):
    """把支持的 HTML 子集读成块序列：('p'|'h1'|'h2'|'h3', 文本) 或 ('table', rows)。"""

    BLOCKS = ("p", "h1", "h2", "h3", "div", "li")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.buf, self.kind = [], [], "p"
        self.table, self.row, self.in_cell, self.cell_header = None, None, False, False

    def _flush(self):
        text = "".join(self.buf).strip()
        self.buf = []
        if text:
            self.blocks.append((self.kind, text))
        self.kind = "p"

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._flush()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.in_cell, self.cell_header, self.buf = True, tag == "th", []
        elif tag == "br":
            self.buf.append("\n")
        elif tag in self.BLOCKS:
            self._flush()
            self.kind = tag if tag in ("h1", "h2", "h3") else "p"

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            self.row.append("".join(self.buf).strip())
            self.buf, self.in_cell = [], False
        elif tag == "tr" and self.row is not None:
            self.table.append((self.row, self.cell_header and len(self.table) == 0))
            self.row = None
        elif tag == "table" and self.table is not None:
            # 表头行：第一行里出现过 <th> 就算
            self.blocks.append(("table", self.table))
            self.table = None
        elif tag in self.BLOCKS:
            self._flush()

    def handle_data(self, data):
        self.buf.append(re.sub(r"[ \t]*\n[ \t]*", " ", data))

    def close(self):
        super().close()
        self._flush()
        return self.blocks


def html_to_docx(body_html, out_path, title=""):
    """body_html 为 <h1>/<p>/<table> 等片段，不含 <html><head>。"""
    r = _Reader()
    r.feed(body_html)
    blocks = r.close()
    body = []
    for kind, payload in blocks:
        body.append(_table(payload) if kind == "table" else _para(payload, kind))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<w:document %s><w:body>%s'
           '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
           '</w:sectPr></w:body></w:document>' % (_W, "".join(body) or "<w:p/>"))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/document.xml", doc)
    print("  OK %s" % out_path)
    return out_path


def docx_text(path):
    """读回 docx 的纯文本，供泄漏扫描用。单元格间制表符分隔、段落换行分隔。"""
    x = zipfile.ZipFile(path).read("word/document.xml").decode("utf8")
    x = re.sub(r"</w:p>", "\n", x)
    x = re.sub(r"</w:tc>", "\t", x)
    x = x.replace("<w:br/>", "\n")
    return re.sub(r"<[^>]+>", "", x)


def _selftest():
    import tempfile
    p = os.path.join(tempfile.mkdtemp(prefix="kw_docx_"), "t.docx")
    html_to_docx("<h1>结算审核意见书</h1><p>第一段<br>第二行</p>"
                 "<table><tr><th>项目</th><th>金额</th></tr>"
                 "<tr><td>停工损失</td><td>1,014,625.98</td></tr></table>", p, "标题")
    t = docx_text(p)
    for must in ("结算审核意见书", "第一段", "第二行", "项目", "停工损失", "1,014,625.98"):
        assert must in t, "读回的文本里没有 %r：\n%s" % (must, t)
    assert zipfile.ZipFile(p).read("word/document.xml").startswith(b"<?xml")
    print("kw_docx 自测通过（纯标准库、无 textutil）：%d 字符读回" % len(t))
    return True


if __name__ == "__main__":
    _selftest()


