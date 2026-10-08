#!/usr/bin/env python3
"""数值型判分器模板 —— 对照 ALE 的 business_finance/financial_stmt_reconstruction_aapl_fy2024

产物：一个多字段数值 JSON/表格
契约：--output <产出目录>  --reference <隐藏答案目录>
输出：stdout 一个 JSON，含 score / passed / errors / details / missing_paths / extra_paths

骨架（四步）：
  1. 文件不在      -> score 0.0，不抛异常
  2. 递归压平 JSON -> {"assets.current.cash": Decimal("29943")}
  3. 逐字段比对    -> accuracy = correct / total          <- 部分分在这里
  4. passed 另算   -> 关键总额全对 AND accuracy >= 阈值    <- 松紧分离

改这里就够了：CONFIG 段。
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ------------------------------- CONFIG -------------------------------
SUBMISSION = "report.json"          # 产出目录下 agent 应交的文件名
ANSWER = "answer.json"              # 答案目录下的标准答案文件名

# 关键项：这些字段必须全对，passed 才可能为 True（对应 ALE 的 totals_correct）
KEY_FIELDS = ["total_assets", "total_liabilities", "total_equity"]

# 通过阈值：score 达到多少才可能 passed
PASS_THRESHOLD = 0.95

# 容差。四档之一：exact / absolute / relative / banded
# 见《交付物2_评分体系规范》§4.1 的选择流程
TOLERANCE = {"mode": "exact", "value": None, "rationale": "会计恒等式，必须精确"}
# 例：{"mode": "relative", "value": 0.001, "rationale": "金额类，行业惯例 0.1%"}
# ----------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path, help="agent 产出目录")
    p.add_argument("--reference", required=True, type=Path, help="隐藏答案目录")
    p.add_argument("--variant", default=None, help="变体任务标识（可选）")
    return p.parse_args()


def to_decimal(v):
    """把可能是 str/int/float 的值转成 Decimal；转不了返回 None。

    处理千分位逗号、货币符号、会计负数写法 (123) -> -123。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("$", "").replace(" ", "")
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        try:
            return Decimal(s)
        except InvalidOperation:
            return None
    return None


def flatten(obj, prefix: str = "") -> dict:
    """把嵌套 dict/list 压平成 {点分路径: 值}。

    {"assets": {"current": {"cash": 29943}}}
      -> {"assets.current.cash": 29943}
    """
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def numeric_leaves(obj) -> dict:
    """只保留能转成 Decimal 的叶子。"""
    out = {}
    for path, v in flatten(obj).items():
        d = to_decimal(v)
        if d is not None:
            out[path] = d
    return out


def within(observed: Decimal, expected: Decimal) -> bool:
    """按 TOLERANCE 判定是否算对。"""
    mode = TOLERANCE["mode"]
    if mode == "exact":
        return observed == expected
    if mode == "absolute":
        return abs(observed - expected) <= Decimal(str(TOLERANCE["value"]))
    if mode == "relative":
        if expected == 0:
            return observed == 0
        return abs(observed - expected) / abs(expected) <= Decimal(str(TOLERANCE["value"]))
    raise ValueError(f"unsupported tolerance mode: {mode}")


def report(score=0.0, passed=False, errors=None, details=None,
           missing_paths=None, extra_paths=None, **extra) -> dict:
    """统一的输出形状。所有 return 都走这里，保证 schema 一致。"""
    out = {
        "score": float(score),
        "passed": bool(passed),
        "errors": list(errors or []),
        "details": list(details or []),
        "missing_paths": sorted(missing_paths or []),
        "extra_paths": sorted(extra_paths or []),
        "meta": {"scorer": Path(__file__).name, "mode": "numeric",
                 "tolerance": TOLERANCE["mode"]},
    }
    out.update(extra)
    return out


def main() -> int:
    args = parse_args()

    # --- 第 1 步：文件不在 -> 0 分，不抛异常 ---------------------------------
    submission_file = args.output / SUBMISSION
    answer_file = args.reference / ANSWER
    errors = []
    if not submission_file.is_file():
        errors.append(f"missing required output: {SUBMISSION}")
    if not answer_file.is_file():
        errors.append(f"missing reference: {ANSWER}")
    if errors:
        # 注意：答案缺失属于基础设施问题，但按契约仍然返回良定义的 0 分
        print(json.dumps(report(0.0, False, errors), ensure_ascii=False))
        return 0

    try:
        submission = json.loads(submission_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(json.dumps(report(0.0, False, [f"{SUBMISSION} is not valid JSON: {e}"]),
                         ensure_ascii=False))
        return 0

    reference = json.loads(answer_file.read_text(encoding="utf-8"))

    # --- 第 2 步：压平 -------------------------------------------------------
    expected = numeric_leaves(reference)
    observed = numeric_leaves(submission)

    # --- 第 3 步：逐字段比对 -------------------------------------------------
    details = []
    correct = 0
    for path, exp in sorted(expected.items()):
        obs = observed.get(path)
        ok = obs is not None and within(obs, exp)
        correct += int(ok)
        details.append({
            "path": path,
            "expected": str(exp),
            "observed": None if obs is None else str(obs),
            "correct": ok,
        })

    total = len(expected)
    accuracy = correct / total if total else 0.0
    missing_paths = [p for p in expected if p not in observed]
    extra_paths = [p for p in observed if p not in expected]

    # --- 第 4 步：passed 另算（关键项 + 阈值）--------------------------------
    key_ok = all(d["correct"] for d in details
                 if d["path"].split(".")[-1] in KEY_FIELDS)
    passed = (not errors) and key_ok and accuracy >= PASS_THRESHOLD

    if not key_ok:
        errors.append("one or more key fields (KEY_FIELDS) is wrong")

    print(json.dumps(
        report(accuracy, passed, errors, details, missing_paths, extra_paths,
               correct_fields=correct, total_fields=total),
        ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
