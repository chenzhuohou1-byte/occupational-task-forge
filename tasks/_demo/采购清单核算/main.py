#!/usr/bin/env python3
"""采购清单核算 —— _demo 玩具任务（纯合成、无任何真实/私有内容）。

目的：让 clone 下来的公开仓有一条能跑通的样例，演示八节点 I/O 契约里一条数据的物理形态
与 main.py 的 5 个子命令（load / start / golden / neg / evaluate）。
⚠️ 这是演示任务、不是有效数据：题太简单，便宜档模型必然满分（判分契约 §6.3 会直接退回），
故 measuredDifficulty 恒为 []。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUMMARY = "核算结果.json"


def _answer():
    with open(os.path.join(HERE, "assets", "reference", "answer.json"), encoding="utf-8") as f:
        return json.load(f)


def _write_summary(out_dir, detail, total):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, SUMMARY), "w", encoding="utf-8") as f:
        json.dump({"明细": detail, "总计": total}, f, ensure_ascii=False, indent=2)


def cmd_load():
    with open(os.path.join(HERE, "task_card.json"), encoding="utf-8") as f:
        sys.stdout.write(f.read())


def cmd_start(work):
    dst_in = os.path.join(work, "input")
    if os.path.isdir(dst_in):
        shutil.rmtree(dst_in)
    shutil.copytree(os.path.join(HERE, "assets", "input"), dst_in)
    os.makedirs(os.path.join(work, "output"), exist_ok=True)
    os.makedirs(os.path.join(work, "software"), exist_ok=True)
    # “答案已隐藏”自检（判分契约 §八纪律 2）
    assert not os.path.exists(os.path.join(work, "reference")), "答案泄漏：沙箱不应有 reference/"
    assert not os.path.exists(os.path.join(dst_in, "answer.json")), "答案泄漏：input 下不应有 answer.json"
    print("已铺设 input/ output/ software/；标准答案未进沙箱，自检通过")


def cmd_golden(out):
    ans = _answer()
    detail = {it["商品"]: round(float(it["金额"]), 2) for it in ans["items"]}
    _write_summary(out, detail, round(float(ans["总计"]), 2))


def cmd_neg(out):
    """刻意触发硬闸门：漏报第一个商品 → 商品集合不一致 → 整题 0。"""
    ans = _answer()
    detail = {it["商品"]: round(float(it["金额"]), 2) for it in ans["items"]}
    detail.pop(ans["items"][0]["商品"], None)
    _write_summary(out, detail, round(float(ans["总计"]), 2))


def cmd_evaluate(output, reference, variant):
    scorer = os.path.join(HERE, "scripts", "score_outputs.py")
    cmd = [sys.executable, scorer, "--output", output, "--reference", reference]
    if variant:
        cmd += ["--variant", variant]
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


def main():
    ap = argparse.ArgumentParser(description="采购清单核算 _demo 任务")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("load")
    p = sub.add_parser("start"); p.add_argument("--work", required=True)
    p = sub.add_parser("golden"); p.add_argument("--out", required=True)
    p = sub.add_parser("neg"); p.add_argument("--out", required=True)
    p = sub.add_parser("evaluate")
    p.add_argument("--output", required=True)
    p.add_argument("--reference", required=True)
    p.add_argument("--variant", default=None)
    a = ap.parse_args()
    if a.cmd == "load":
        cmd_load()
    elif a.cmd == "start":
        cmd_start(a.work)
    elif a.cmd == "golden":
        cmd_golden(a.out)
    elif a.cmd == "neg":
        cmd_neg(a.out)
    elif a.cmd == "evaluate":
        return cmd_evaluate(a.output, a.reference, a.variant)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
