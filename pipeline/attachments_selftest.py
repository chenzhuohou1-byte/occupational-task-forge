#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""attachments.py 自测：一份玩具规格 → 三层嵌套的六种格式附件，逐个读回来验。

跑法（在 occupational-task-forge/ 下）：python3 -m pipeline.attachments_selftest

三段：
  一、正路：六种格式都落盘、非空、且能被对应读取器读回，常量替换生效、数值仍是数值。
  二、降级与安全：不支持的类型 / 逃出输出目录 / 空内容 / 缺依赖 —— 只进 errors[]，
      不抛异常，同批里正常的文件照样写出来（这条是 N4 能被批量调用的前提）。
  三、接线：scaffold_task 带「附件规格」造真实附件、不带时行为不变（仍是占位符）。
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile

from . import attachments as att
from . import scaffold

# ---------------- 玩具规格：数据与排版分离，规格里没有一个表达式 ----------------
TOY_SPEC = {
    "常量": {
        "工程名称": "临河科创园二期2#厂房",
        "承包人": "宏远建设集团有限公司",
        "项目部": "{承包人}临河项目部",          # 常量引用常量
        "监理": "中衡工程管理咨询有限公司",
        "报送日期": "2026-09-30",
    },
    "文件": {
        # 三层目录嵌套 + 多 sheet + 跨列标题行 + 金额列
        "01_台账/工程量/分部分项/清单节选.xlsx": {
            "类型": "xlsx",
            "sheets": [
                {
                    "名称": "分部分项",
                    "标题": ["已标价工程量清单 · 分部分项（节选）",
                             "工程名称：{工程名称}　　编制：{项目部}"],
                    "表头": ["序号", "项目编码", "项目名称", "单位", "工程量", "综合单价(元)"],
                    "数据": [
                        [1, "010502001001", "矩形柱", "m3", 620.0, 692.0],
                        [2, "010505003001", "平板", "m3", 1860.0, 638.0],
                    ],
                    "列宽": [6, 16, 18, 8, 12, 14],
                    "金额列": [5, 6],
                },
                {
                    "名称": "计日工",
                    "表头": ["工种", "单位", "暂定数量", "综合单价(元)"],
                    "数据": [["普工", "工日", 120, 220.0]],
                    "列宽": [12, 8, 12, 14],
                    "金额列": [4],
                },
            ],
        },
        # 三层目录嵌套的 PDF：块列表 + 信息表
        "02_变更与签证/现场签证单/2026年6月/JZ-2026-018.pdf": {
            "类型": "pdf",
            "标题": "现场签证单 JZ-2026-018",
            "块": [
                {"块": "标题", "文本": "现场签证单"},
                {"块": "正文", "文本": "签证编号：JZ-2026-018　　工程名称：{工程名称}"},
                {"块": "表格", "样式": "信息表", "列宽": [70, 360],
                 "数据": [["事由", "屋面原种植土清运，原清单无此项"],
                          ["工种", "普工"],
                          ["数量", "32 工日"]]},
                {"块": "正文", "文本": "监理单位：{监理}（盖章）"},
            ],
        },
        "03_制度/管理办法/结算审核管理办法.docx": {
            "类型": "docx",
            "标题": "结算审核管理办法",
            "块": [
                {"块": "标题", "文本": "{承包人}　工程结算审核管理办法"},
                {"块": "小标题", "文本": "第一章　计价口径"},
                {"块": "正文", "文本": "变更部分单价缺项时，按同类项目单价套用，不另计取。"},
                {"块": "表格", "表头": ["环节", "责任岗位", "时限"],
                 "数据": [["初审", "造价员", "5 个工作日"], ["复核", "造价负责人", "3 个工作日"]]},
                {"块": "列表", "条目": ["报送日期：{报送日期}", "编制：{项目部}"]},
            ],
        },
        "04_明细/导出/费用明细.csv": {
            "类型": "csv",
            "表头": ["序号", "事项", "金额(元)"],
            "数据": [[1, "土方外运", 128600.0], [2, "钢筋价差", 46200.0]],
        },
        "05_说明/口径/计价口径说明.md": {
            "类型": "md",
            "行": ["# 计价口径说明", "", "- 工程名称：{工程名称}",
                   "- 编制单位：{项目部}", "- 材料取价：投标期信息价 × 0.965",
                   "- 未定义的占位符原样保留：{这个常量没定义}"],
        },
        "06_杂项/随附/交接说明.txt": {
            "类型": "txt",
            "文本": "本袋资料由{项目部}于{报送日期}移交，共 6 份。",
        },
        # 有意留给人工的附件（扫描件之类），应进 skipped[] 而不是 errors[]
        "07_原件/扫描件/发票原件.pdf": {"跳过": True},
    },
}

PROJ = TOY_SPEC["常量"]["工程名称"]
DEPT = "宏远建设集团有限公司临河项目部"          # 常量引用常量后的期望值


def _ok(msg):
    print("  ✓ %s" % msg)


def _nonempty(path, why):
    assert os.path.isfile(path), "文件没生成：%s（%s）" % (path, why)
    assert os.path.getsize(path) > 0, "文件是空的：%s" % path


def check_positive(tmp):
    """第一段：造出来、非空、能被对应读取器读回、常量替换生效、数值仍是数值。"""
    print("\n[一] 正路：六种格式 × 三层嵌套 → %s" % tmp)
    res = att.materialize(TOY_SPEC, tmp)
    assert res["errors"] == [], "不该有错：%s" % res["errors"]
    assert len(res["skipped"]) == 1, "跳过项应为 1：%s" % res["skipped"]
    assert len(res["written"]) == 6, "应写出 6 份：%s" % res["written"]
    _ok("written=6 / skipped=1 / errors=0")

    for rel in res["written"]:
        _nonempty(os.path.join(tmp, rel), "written 里报了它")
    assert not os.path.exists(os.path.join(tmp, "07_原件/扫描件/发票原件.pdf")), \
        "标了跳过的附件不该被写出来"
    _ok("六份文件全部存在且非空，跳过项确实没写")

    tools = att.tools_dir()
    assert tools, "找不到 kw_* 工具目录"
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import kw_docx
    import kw_xlsx

    # xlsx：能 load，两个 sheet 都在，标题行替换了常量，数值单元格仍是数值
    xlsx_path = os.path.join(tmp, "01_台账/工程量/分部分项/清单节选.xlsx")
    xt = kw_xlsx.sheet_text(xlsx_path)
    assert "# sheet: 分部分项" in xt and "# sheet: 计日工" in xt, xt[:200]
    assert PROJ in xt and DEPT in xt, "xlsx 常量没替换：%s" % xt[:200]
    assert "010502001001" in xt
    # 数值必须还是数值：2 行标题 + 1 行表头，数据首行是第 4 行，E 列＝工程量
    import openpyxl                     # kw_xlsx 已经 import 成功，这里必然可用
    cell = openpyxl.load_workbook(xlsx_path).worksheets[0]["E4"].value
    assert isinstance(cell, (int, float)) and abs(cell - 620.0) < 1e-9, \
        "数值单元格被 str 化了，xlsx 退化成文本表：%r" % cell
    _ok("xlsx 能 load：2 sheet / 常量已替换 / E4=%r 仍是数值" % cell)

    # pdf：先验魔数（纯标准库能验），再用 pypdf 抽文本
    pdf_path = os.path.join(tmp, "02_变更与签证/现场签证单/2026年6月/JZ-2026-018.pdf")
    with open(pdf_path, "rb") as f:
        assert f.read(5) == b"%PDF-", "不是合法 PDF"
    try:
        import pypdf
        pt = "".join(p.extract_text() or "" for p in pypdf.PdfReader(pdf_path).pages)
        assert "现场签证单" in pt, "pdf 抽不出标题：%r" % pt[:200]
        assert PROJ in pt.replace("\n", ""), "pdf 常量没替换：%r" % pt[:200]
        assert "屋面原种植土清运" in pt.replace("\n", ""), "pdf 表格内容丢了"
        _ok("pdf 能抽文本：标题 / 常量 / 表格单元格都在（%d 字）" % len(pt))
    except ImportError:
        print("  ! 未装 pypdf，跳过 pdf 文本抽取，只验了魔数")

    # docx：zipfile 解 document.xml 读回（不依赖 python-docx）
    dt = kw_docx.docx_text(os.path.join(tmp, "03_制度/管理办法/结算审核管理办法.docx"))
    assert "工程结算审核管理办法" in dt, dt[:200]
    assert "造价负责人" in dt, "docx 表格丢了"
    assert TOY_SPEC["常量"]["报送日期"] in dt, "docx 列表块/常量丢了"
    _ok("docx 能抽文本：标题 / 表格 / 列表块都在（%d 字）" % len(dt))

    # csv：带 BOM（Excel 双击不乱码）+ 表头与行数对得上
    cp = os.path.join(tmp, "04_明细/导出/费用明细.csv")
    with open(cp, "rb") as f:
        assert f.read(3) == b"\xef\xbb\xbf", "csv 缺 BOM"
    rows = list(csv.DictReader(open(cp, encoding="utf-8-sig")))
    assert [*rows[0]] == ["序号", "事项", "金额(元)"], rows[0]
    assert len(rows) == 2 and rows[1]["事项"] == "钢筋价差", rows
    _ok("csv 能读回：BOM / 表头 / 2 行")

    # md / txt：常量替换生效、未定义占位符原样保留
    md = open(os.path.join(tmp, "05_说明/口径/计价口径说明.md"), encoding="utf-8").read()
    assert md.startswith("# 计价口径说明") and md.endswith("\n")
    assert PROJ in md and DEPT in md
    assert "{这个常量没定义}" in md, "未知占位符该原样保留，不该被吞掉或报错"
    tx = open(os.path.join(tmp, "06_杂项/随附/交接说明.txt"), encoding="utf-8").read()
    assert tx == "本袋资料由%s于2026-09-30移交，共 6 份。\n" % DEPT, repr(tx)
    _ok("md/txt：常量替换（含常量引用常量）生效，未定义占位符原样保留")

    # 目录真嵌了三层
    deep = os.path.join(tmp, "02_变更与签证", "现场签证单", "2026年6月")
    assert os.path.isdir(deep), "三层嵌套目录没建出来"
    _ok("三层嵌套目录成立：02_变更与签证/现场签证单/2026年6月/")
    return res


BAD_SPEC = {
    "文件": {
        "正常/能写的.txt": {"类型": "txt", "文本": "这份必须照样写出来"},
        "../逃出去.txt": {"类型": "txt", "文本": "越狱"},
        "坏类型.pptx": {"类型": "pptx", "文本": "不支持"},
        "空的.md": {"类型": "md", "行": []},
        "块名写错.pdf": {"类型": "pdf", "块": [{"块": "大标题", "文本": "没有这种块"}]},
    },
}

DEP_SPEC = {
    "文件": {
        "a.xlsx": {"类型": "xlsx", "表头": ["列1"], "数据": [[1]]},
        "b.pdf": {"类型": "pdf", "块": [{"块": "标题", "文本": "x"}]},
        "c.docx": {"类型": "docx", "块": [{"块": "正文", "文本": "x"}]},
        "d.csv": {"类型": "csv", "表头": ["列1"], "数据": [[1]]},
    },
}


def check_degrade(tmp):
    """第二段：坏规格与缺依赖都只记账、不抛异常，同批正常文件照写。"""
    print("\n[二] 降级与安全")
    res = att.materialize(BAD_SPEC, tmp)
    assert res["written"] == ["正常/能写的.txt"], res["written"]
    assert len(res["errors"]) == 4, res["errors"]
    joined = "；".join(res["errors"])
    assert "逃出" in joined and "不支持的类型" in joined, joined
    _nonempty(os.path.join(tmp, "正常/能写的.txt"), "同批有坏项也该写出来")
    assert not os.path.exists(os.path.join(os.path.dirname(tmp), "逃出去.txt")), \
        "路径逃逸没挡住，写到输出目录外面去了"
    _ok("4 条坏规格进 errors[]、好文件照写、路径逃逸被挡")
    for e in res["errors"]:
        print("    · %s" % e)

    # 缺依赖演练：把底座导入换成必然失败，xlsx/pdf/docx 应全部降级，csv 不受影响
    orig = att._tool

    def _no_tool(name):
        raise att.MissingDep("演练：假装本机没装 %s 的依赖" % name)

    att._tool = _no_tool
    try:
        res2 = att.materialize(DEP_SPEC, os.path.join(tmp, "缺依赖"))
    finally:
        att._tool = orig
    assert res2["written"] == ["d.csv"], res2["written"]
    assert len(res2["errors"]) == 3 and all("依赖缺失" in e for e in res2["errors"]), res2["errors"]
    _ok("缺依赖时 xlsx/pdf/docx 降级进 errors[]，csv（纯标准库）照写")
    for e in res2["errors"]:
        print("    · %s" % e)
    return res


WIRE_SPEC = {
    "task_id": "selftest-n4-001",
    "领域": "自测领域",
    "任务名": "带附件规格的任务",
    "prompt": "按 input/ 的台账与签证单复核结算金额。",
    "交付要求": {"结算复核.json": "复核结论"},
    "附件计划": ["人工补的扫描件说明.txt"],          # 规格没覆盖的，仍走占位符
    "附件规格": {
        "常量": {"工程名称": "临河科创园二期2#厂房"},
        "文件": {
            "01_台账/工程量/分部分项/清单节选.csv": {
                "类型": "csv", "表头": ["项目编码", "工程量"],
                "数据": [["010502001001", 620.0]]},
            "02_变更与签证/现场签证单/口径.md": {
                "类型": "md", "行": ["# 口径", "工程名称：{工程名称}"]},
        },
    },
}


def check_wiring(tmp):
    """第三段：scaffold 接线。带规格造真实附件、嵌套能铺进沙箱；不带规格行为不变。"""
    print("\n[三] 接线：scaffold_task")
    tasks_dir = os.path.join(tmp, "tasks")
    out = scaffold.scaffold_task(WIRE_SPEC, tasks_dir)
    task_dir = out["dir"]
    assert out["附件"]["written"] == ["01_台账/工程量/分部分项/清单节选.csv",
                                     "02_变更与签证/现场签证单/口径.md"], out["附件"]
    _nonempty(os.path.join(task_dir, "assets/input/01_台账/工程量/分部分项/清单节选.csv"), "附件规格里有")
    ph = os.path.join(task_dir, "assets/input/人工补的扫描件说明.txt")
    assert "TODO: 填" in open(ph, encoding="utf-8").read(), "规格没覆盖的附件仍应留占位符"
    card = json.load(open(os.path.join(task_dir, "task_card.json"), encoding="utf-8"))
    assert "01_台账/工程量/分部分项/清单节选.csv" in card["环境"]["input"], card["环境"]["input"]
    _ok("真实附件 2 份 + 未覆盖项仍是占位符 + 任务卡 input 说明含嵌套路径")

    # 真跑一遍任务自己的 start，确认嵌套附件能整树铺进沙箱 input/
    work = os.path.join(tmp, "work")
    r = subprocess.run([sys.executable, "main.py", "start", "--work", work],
                       cwd=task_dir, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout
    landed = os.path.join(work, "input/01_台账/工程量/分部分项/清单节选.csv")
    _nonempty(landed, "start 应整树拷贝")
    _ok("main.py start 把三层嵌套附件铺进沙箱：%s" % r.stdout.strip())

    # 不带附件规格：完全走老路，占位符 + todos（现有四条任务就是这条路，不能破）
    plain = dict(WIRE_SPEC)
    plain.pop("附件规格")
    plain["任务名"] = "不带附件规格的任务"
    out2 = scaffold.scaffold_task(plain, tasks_dir)
    assert "附件" not in out2, "没给规格就不该有附件结果"
    # todos = 待填文件（附件占位 + 标准答案占位）+ 固定清单（scoringBasis / 夹具）。
    # 原来写死 len==2，1008 给 scaffold 加了两条固定清单后这条断言就一直是红的，
    # 但当时没有 CI、没人跑到——所以这里改成按内容断言，不按条数。
    files = [t for t in out2["todos"] if t.endswith((".txt", ".json", ".csv", ".md"))]
    assert len(files) == 2, out2["todos"]
    assert any("scoringBasis" in t for t in out2["todos"]), out2["todos"]
    assert any("夹具" in t for t in out2["todos"]), out2["todos"]

    p = os.path.join(out2["dir"], "assets/input/人工补的扫描件说明.txt")
    assert "TODO: 填" in open(p, encoding="utf-8").read()
    _ok("不带规格时行为不变：占位符 + %d 条 todos" % len(out2["todos"]))


def main():
    tmp = tempfile.mkdtemp(prefix="n4_selftest_")
    try:
        r1 = check_positive(os.path.join(tmp, "附件"))
        check_degrade(os.path.join(tmp, "坏规格"))
        check_wiring(os.path.join(tmp, "接线"))
        n_files = sum(len(fs) for _, _, fs in os.walk(tmp))
        print("\n全部通过：正路 %d 份附件、三段断言无一失败；临时目录共 %d 个文件"
              % (len(r1["written"]), n_files))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
