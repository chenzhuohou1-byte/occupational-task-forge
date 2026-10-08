#!/usr/bin/env python3
"""集合型判分器模板 —— 对照 ALE 的 agriculture_env/crop_rotation_d02

产物：一份清单/集合（该有哪些条目 + 每个条目的若干字段）
契约：--output <产出目录>  --reference <隐藏答案目录>

核心口径（这是本模板唯一重要的设计）：
    「该有哪些条目」是硬闸门，不给部分分；
    「每个条目的值算得对不对」才给部分分。

对应到造价题：BG-05 是刻意埋的漏报项。
agent 没把 BG-05 识别出来 -> 整题 0 分，而不是扣 1/11 —— 因为"发现漏报"正是考点。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ------------------------------- CONFIG -------------------------------
SUBMISSION = "eligible.json"        # agent 应交的文件
ANSWER = "eligible_answer.json"     # 标准答案文件

ID_FIELD = "id"                     # 集合主键列名

# 逐行比对的字段及其权重（过闸门后才算分）
FIELD_WEIGHTS = {
    "amount_approved": 3.0,
    "amount_declared": 2.0,
    "category": 1.0,
    "responsible_party": 1.0,
}

# 集合层面的分项（全有或全无）
SET_COUNT_WEIGHT = 0.5              # 条目数量对
SET_ID_WEIGHT = 2.0                 # 主键集合完全一致

# 通过阈值
PASS_THRESHOLD = 0.95
# ----------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path, help="agent 产出目录")
    p.add_argument("--reference", required=True, type=Path, help="隐藏答案目录")
    p.add_argument("--variant", default=None)
    return p.parse_args()


def report(score=0.0, passed=False, errors=None, details=None,
           gates=None, missing_paths=None, extra_paths=None, **extra) -> dict:
    out = {
        "score": float(score),
        "passed": bool(passed),
        "errors": list(errors or []),
        "details": list(details or []),
        "missing_paths": sorted(missing_paths or []),
        "extra_paths": sorted(extra_paths or []),
        "meta": {"scorer": Path(__file__).name, "mode": "set"},
    }
    if gates is not None:
        out["gates"] = gates
    out.update(extra)
    return out


def norm(v) -> str:
    """归一化，用于精确比对：去空白、数字去掉多余的 0、字符串小写。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        # 103200.0 -> "103200"
        s = f"{v:.10f}".rstrip("0").rstrip(".")
        return s or "0"
    if isinstance(v, str):
        s = v.strip().lower().replace(",", "")
        try:
            return f"{float(s):.10f}".rstrip("0").rstrip(".") or "0"
        except ValueError:
            return s
    return str(v)


def main() -> int:
    args = parse_args()
    sub_file = args.output / SUBMISSION
    ref_file = args.reference / ANSWER

    # ---------------- 硬闸门：任何一条不过 -> 整题 0，不算部分分 ----------------
    gates = []

    def gate(name: str, ok: bool, msg: str = "") -> bool:
        gates.append({"name": name, "passed": bool(ok), "detail": msg})
        return ok

    if not gate("submission_exists", sub_file.is_file(), str(sub_file)):
        print(json.dumps(report(0.0, False, [f"missing required output: {SUBMISSION}"],
                                gates=gates), ensure_ascii=False))
        return 0
    if not gate("reference_exists", ref_file.is_file(), str(ref_file)):
        print(json.dumps(report(0.0, False, [f"missing reference: {ANSWER}"],
                                gates=gates), ensure_ascii=False))
        return 0

    try:
        sub = json.loads(sub_file.read_text(encoding="utf-8"))
        ref = json.loads(ref_file.read_text(encoding="utf-8"))
    except Exception as e:
        gate("json_parseable", False, str(e))
        print(json.dumps(report(0.0, False, [f"invalid JSON: {e}"], gates=gates),
                         ensure_ascii=False))
        return 0
    gate("json_parseable", True)

    sub_rows = sub.get("items") if isinstance(sub, dict) else sub
    ref_rows = ref.get("items") if isinstance(ref, dict) else ref

    if not gate("submission_is_list", isinstance(sub_rows, list),
                f"got {type(sub_rows).__name__}"):
        print(json.dumps(report(0.0, False, ["submission is not a list of items"],
                                gates=gates), ensure_ascii=False))
        return 0

    sub_ids = [norm(r.get(ID_FIELD)) for r in sub_rows]
    ref_ids = [norm(r.get(ID_FIELD)) for r in ref_rows]

    if not gate("id_non_null", all(sub_ids), "some items have empty id"):
        print(json.dumps(report(0.0, False, ["empty id present"], gates=gates),
                         ensure_ascii=False))
        return 0
    if not gate("id_unique", len(set(sub_ids)) == len(sub_ids), "duplicate ids"):
        print(json.dumps(report(0.0, False, ["duplicate id present"], gates=gates),
                         ensure_ascii=False))
        return 0
    if not gate("required_fields_present",
                all(all(f in r for f in FIELD_WEIGHTS) for r in sub_rows),
                "some items missing a scored field"):
        print(json.dumps(report(0.0, False, ["missing scored field"], gates=gates),
                         ensure_ascii=False))
        return 0

    # ★ 本模板的核心闸门：集合必须完全一致（筛多了或筛漏了 -> 0）
    sub_set, ref_set = set(sub_ids), set(ref_ids)
    missing_ids = sorted(ref_set - sub_set)
    extra_ids = sorted(sub_set - ref_set)
    if not gate("id_set_matches", sub_set == ref_set,
                f"missing={missing_ids} extra={extra_ids}"):
        print(json.dumps(report(0.0, False,
                                ["id set does not match reference"],
                                gates=gates, missing_paths=missing_ids,
                                extra_paths=extra_ids), ensure_ascii=False))
        return 0

    # ---------------- 过闸门后才算分：逐条目、逐字段给部分分 ----------------
    sub_by_id = {norm(r.get(ID_FIELD)): r for r in sub_rows}
    ref_by_id = {norm(r.get(ID_FIELD)): r for r in ref_rows}

    details = []
    core_total = 0.0
    core_max = 0.0
    for f, w in FIELD_WEIGHTS.items():
        hit = 0
        for i in sorted(ref_set):
            exp, obs = ref_by_id[i].get(f), sub_by_id[i].get(f)
            ok = norm(exp) == norm(obs)
            hit += int(ok)
            details.append({"path": f"{i}.{f}", "expected": str(exp),
                            "observed": str(obs), "correct": ok})
        core_total += w * (hit / len(ref_set))
        core_max += w

    flagged_total = 0.0
    flagged_max = SET_COUNT_WEIGHT + SET_ID_WEIGHT
    if len(sub_rows) == len(ref_rows):
        flagged_total += SET_COUNT_WEIGHT
    flagged_total += SET_ID_WEIGHT          # 上面闸门已保证集合一致

    score = (core_total + flagged_total) / (core_max + flagged_max)
    passed = score >= PASS_THRESHOLD

    print(json.dumps(
        report(score, passed, [], details, gates,
               core_score=round(core_total, 4), core_max=core_max,
               set_score=round(flagged_total, 4), set_max=flagged_max),
        ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
