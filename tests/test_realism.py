#!/usr/bin/env python3
"""规模 / 软件两条提示的单元自测（契约 §8.2）。纯本地文件，不触网、不花钱。

为什么要有：这两条是给作者看的提示，错了不会让坏题进库，但会让人对着假警报改题，
或者让「卡上写了 QGIS、考生环境里根本没有」这种问题继续漏过去。边界（标准库写法、
带版本号的写法、别名、未登记镜像）在真实题库里不一定都碰得到，所以单独写。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import realism  # noqa: E402

FAILURES = []


def check(cond, label):
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        FAILURES.append(label)


class FakeTask:
    def __init__(self, card, d):
        self.card, self.dir, self.task_id = card, d, "fake-001"


def make_inputs(files):
    d = tempfile.mkdtemp(prefix="realism_")
    for rel, size in files.items():
        p = os.path.join(d, "assets", "input", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x" * size)
    return d


def main():
    print("规模提示")
    small = FakeTask({}, make_inputs({"a.csv": 2000, "b.md": 500}))
    h = realism.scale_hint(small)
    check(not h["达标"] and len(h["缺"]) == 3, "小附件 + 只有 csv/md + 无子目录 → 三项都缺")
    big = FakeTask({}, make_inputs({"raw/scan.pcap": 90_000, "规则.md": 20_000}))
    h = realism.scale_hint(big)
    check(h["达标"] and h["非通用格式"] == [".pcap"] and h["有子目录"], "≥100KB + .pcap + 子目录 → 达标")
    empty = FakeTask({}, tempfile.mkdtemp())
    check(not realism.scale_hint(empty)["达标"], "没有 assets/input → 不达标且不崩")

    print("软件提示")
    d = tempfile.mkdtemp()
    ok = FakeTask({"software": ["Python 3.12", "Python标准库", "numpy", "scipy 1.18",
                                "pinocchio 4.1.0（pip: pin==4.1.0）", "MuJoCo 3.16.0",
                                {"name": "pandas", "version": "3.0.6"}, "KiCad"]}, d)
    h = realism.software_hint(ok)
    check(h["达标"] and not h["未装"], "标准库写法 / 带版本号 / 别名 pinocchio→pin / 字典写法 / 宿主应用 KiCad 都算已装：%s" % h["未装"])
    bad = FakeTask({"software": ["Python 3", "QGIS", "tshark", "stim"]}, d)
    h = realism.software_hint(bad)
    check(not h["达标"] and h["未装"] == ["QGIS", "tshark", "stim"], "QGIS/tshark/stim 不在考生环境 → 报出来")
    check(realism.software_hint(FakeTask({}, d))["达标"], "没写 software → 不报")
    img = FakeTask({"software": [], "evaluation": {"engine": {"adapter_args": {"image": "bench-astro:v1"}}}}, d)
    h = realism.software_hint(img)
    check(not h["达标"] and h.get("镜像未登记"), "可执行题用了未登记镜像（只在作者本机）→ 报出来")
    reg = FakeTask({"software": ["CellProfiler 4.2.8"],
                    "evaluation": {"engine": {"adapter_args": {"image": "ale-examinee:cellprofiler-4.2.8"}}}}, d)
    check(realism.software_hint(reg)["达标"], "登记镜像自带的软件（CellProfiler）算已装")
    layered = FakeTask({"software": ["Biopython", "pyhmmer 0.12.3", "SimpleITK", "lifelines", "wntr"]}, d)
    h = realism.software_hint(layered)
    check(h["达标"], "叠加镜像（bio-v2、ext-v3）的包表也算进考生环境：%s" % h["未装"])

    print("\n%s" % ("全部通过" if not FAILURES else "%d 项失败" % len(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
