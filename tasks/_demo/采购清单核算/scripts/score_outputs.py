#!/usr/bin/env python3
"""采购清单核算 判分器（判分契约 v1.0 §1/§2）。

只读 --output / --reference，不碰 input/，不 import 造题/生成器代码（判分契约 §8 独立性）。
gate-and-score：商品集合/格式层走硬闸门（不一致整题 0），金额值层给部分分。
"""
import argparse
import json
import os
import sys

SUMMARY = "核算结果.json"
TOL = 0.01   # 金额按分位，absolute 容差（题面把单位钉死到两位小数）


def _load(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def emit(score, passed, errors, details, missing=(), extra=()):
    print(json.dumps({
        "score": round(float(score), 4),
        "passed": bool(passed),
        "errors": list(errors),
        "details": details,
        "missing_paths": list(missing),
        "extra_paths": list(extra),
        "meta": {"scorer": "score_outputs.py", "version": "1.0", "mode": "numeric_tabular"},
    }, ensure_ascii=False))
    return 0


def score(output_dir, reference_dir):
    ans_path = os.path.join(reference_dir, "answer.json")
    if not os.path.isfile(ans_path):
        sys.exit("判分器故障：标准答案不存在 %s" % ans_path)   # 退出码非 0（判分契约 §2.5）
    ans = _load(ans_path)
    gold = {it["商品"]: round(float(it["金额"]), 2) for it in ans["items"]}
    gold_total = round(float(ans["总计"]), 2)

    out_path = os.path.join(output_dir, SUMMARY)
    if not os.path.isfile(out_path):
        return emit(0.0, False, ["缺交付文件 %s" % SUMMARY], [], list(gold) + ["总计"])
    try:
        got = _load(out_path)
    except Exception as ex:
        return emit(0.0, False, ["%s 不是合法 JSON（%s）" % (SUMMARY, type(ex).__name__)], [])

    detail = got.get("明细")
    if not isinstance(detail, dict):
        return emit(0.0, False, ["缺必需键 明细（对象）"], [])
    # 硬闸门：商品集合一致（漏报/多报都整题 0——“对齐清单”本身是考点）
    missing = sorted(set(gold) - set(detail))
    extra = sorted(set(detail) - set(gold))
    if missing or extra:
        return emit(0.0, False,
                    ["商品集合不一致：漏报 %s / 多报 %s" % (missing, extra)],
                    [], missing, extra)
    if "总计" not in got:
        return emit(0.0, False, ["缺必需键 总计"], [])

    # 部分分：逐项金额 + 总计，等权
    details, correct, checks = [], 0, 0
    for k in sorted(gold):
        checks += 1
        try:
            ok = abs(float(detail[k]) - gold[k]) <= TOL
        except (TypeError, ValueError):
            ok = False
        correct += 1 if ok else 0
        details.append({"path": "明细.%s" % k, "expected": gold[k],
                        "observed": detail.get(k), "correct": ok})
    checks += 1
    try:
        tot_ok = abs(float(got["总计"]) - gold_total) <= TOL
    except (TypeError, ValueError):
        tot_ok = False
    correct += 1 if tot_ok else 0
    details.append({"path": "总计", "expected": gold_total,
                    "observed": got.get("总计"), "correct": tot_ok})

    sc = correct / checks
    passed = correct == checks            # passed 严于 score：逐项全对才算过
    return emit(sc, passed, [], details)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--variant", default=None)   # 本任务暂无变体
    a = ap.parse_args()
    return score(a.output, a.reference)


if __name__ == "__main__":
    sys.exit(main())
