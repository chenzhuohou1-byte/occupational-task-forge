#!/usr/bin/env python3
"""定性型判分器模板 —— 对照 ALE 的 computing_math/k8s_payment_api_root_cause_analysis

产物：一份"分析结论"，但**不是自由文本报告**，而是结构化 JSON
契约：--output <产出目录>  --reference <隐藏答案目录>

本模板回答的核心问题：**定性型答复怎么让机器判分？**
四步：
  1. 把定性结论结构化（每条结论一个对象：类型标签 / 严重度或金额档 / 责任方 / 依据条款 / 证据原文）
  2. 硬闸门（JSON 合法、必需 key 齐、结论列表非空）
  3. 加权 rubric：判「分类标签 + 档位」，不判「写得好不好」
  4. ★ 证据逐字回溯：每条 evidence 必须能在输入材料里原样找到，瞎编的直接不算分

   missing = [e for e in evidence if not any(e in t for t in input_texts)]

一个子串检查，就把"模型有没有瞎编证据"变成了机械可判。

对应场景：造价第 7 条「司法鉴定意见书的资料梳理与争议焦点归纳」、计量第 2 条「整改报告」。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ------------------------------- CONFIG -------------------------------
SUBMISSION = "analysis.json"        # agent 应交的分析结果
ANSWER = "findings.json"            # 标准答案（期望的争议焦点清单）
INPUTS_DIR = "inputs"               # ★ 输入材料目录（用于证据逐字回溯）

# 每条结论必须有的字段
REQUIRED_ITEM_FIELDS = ["type", "severity", "evidence"]

# 加权 rubric。权重写在代码里，模型不参与汇总。
WEIGHTS = {
    "primary_finding": 0.30,        # 主结论：类型标签命中 且 严重度/档位判对
    "secondary_finding": 0.15,      # 次结论
    "amount_band": 0.15,            # 金额影响落在正确档位
    "responsible_party": 0.10,      # 责任方判对
    "clause_reference": 0.10,       # 依据条款/整改措施命中
    "evidence_grounding": 0.15,     # ★ 证据逐字回溯
    "set_f1": 0.05,                 # 争议焦点集合的 F1
}

# 通过阈值。ALE 的 K8s 任务要求满分才算 passed，我们放宽到可配。
PASS_THRESHOLD = 0.95

# 金额档位容差：相对误差
AMOUNT_BAND_TOLERANCE = 0.01        # 1%
# ----------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path, help="agent 产出目录")
    p.add_argument("--reference", required=True, type=Path, help="隐藏答案目录")
    p.add_argument("--variant", default=None)
    return p.parse_args()


def report(score=0.0, passed=False, errors=None, details=None,
           gates=None, components=None, **extra) -> dict:
    out = {
        "score": float(score),
        "passed": bool(passed),
        "errors": list(errors or []),
        "details": list(details or []),
        "missing_paths": [],
        "extra_paths": [],
        "meta": {"scorer": Path(__file__).name, "mode": "qualitative"},
    }
    if gates is not None:
        out["gates"] = gates
    if components is not None:
        out["components"] = {k: round(v, 4) for k, v in components.items()}
    out.update(extra)
    return out


# --------------------------- 标签归一化 ---------------------------
# 判的是「分类标签」，所以要把自由写法归一到同一个标签上。
# 这里用关键词表，不用大模型。
LABEL_KEYWORDS = {
    "missing_item": ["漏报", "漏项", "未申报", "少报", "missing", "omitted"],
    "overclaimed": ["多报", "虚报", "高估", "overclaim", "inflated"],
    "mispricing": ["单价错误", "组价错误", "计价错误", "mispric", "wrong rate"],
    "quantity_error": ["工程量", "数量错误", "quantity"],
    "scope_dispute": ["范围争议", "界面划分", "scope"],
    "schedule_impact": ["工期", "延误", "delay", "schedule"],
}


def normalize_label(value) -> str:
    """把 agent 写的类型标签归一到一个已知标签；认不出就原样小写返回。"""
    s = str(value or "").strip().lower()
    if not s:
        return ""
    if s in LABEL_KEYWORDS:
        return s
    for label, kws in LABEL_KEYWORDS.items():
        if any(kw in s for kw in kws):
            return label
    return s


def parse_amount(v):
    """从任意写法里抠出金额数值。'−103,200元' -> -103200.0"""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).replace(",", "").replace("，", "")
    s = s.replace("−", "-").replace("–", "-")     # unicode 减号
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def amount_band_match(observed, expected) -> bool:
    """金额档位是否落在容差内。expected 为 0 时要求 observed 也接近 0。"""
    if observed is None or expected is None:
        return False
    if expected == 0:
        return abs(observed) <= 1e-9
    return abs(observed - expected) / abs(expected) <= AMOUNT_BAND_TOLERANCE


def evidence_grounding(findings, input_texts):
    """★ 反幻觉那一招。

    每条 evidence 必须是输入材料里的**逐字原文**（不是文件名引用）。
    有一条找不到 -> 这条结论整个不算 grounded。
    返回 (grounded_ratio, 明细列表)。
    """
    if not findings:
        return 0.0, []
    grounded = 0
    detail = []
    for f in findings:
        ev = f.get("evidence") or []
        if isinstance(ev, str):
            ev = [ev]
        bad = [e for e in ev if not any(str(e) in t for t in input_texts)]
        ok = bool(ev) and not bad
        grounded += int(ok)
        detail.append({
            "path": f"finding[{f.get('type')}].evidence",
            "expected": "every item verbatim in input materials",
            "observed": f"{len(ev) - len(bad)}/{len(ev)} verbatim",
            "correct": ok,
        })
    return grounded / len(findings), detail


def f1(pred: set, gold: set) -> float:
    if not gold:
        return 1.0 if not pred else 0.0
    if not pred:
        return 0.0
    tp = len(pred & gold)
    p = tp / len(pred)
    r = tp / len(gold)
    return 0.0 if p + r == 0 else 2 * p * r / (p + r)


def main() -> int:
    args = parse_args()
    sub_file = args.output / SUBMISSION
    ref_file = args.reference / ANSWER
    inputs_dir = args.reference / INPUTS_DIR

    # ---------------- 硬闸门 ----------------
    gates = []

    def gate(name, ok, msg=""):
        gates.append({"name": name, "passed": bool(ok), "detail": str(msg)})
        return ok

    if not gate("submission_exists", sub_file.is_file(), sub_file):
        print(json.dumps(report(0.0, False, [f"missing required output: {SUBMISSION}"],
                                gates=gates), ensure_ascii=False))
        return 0
    if not gate("reference_exists", ref_file.is_file(), ref_file):
        print(json.dumps(report(0.0, False, [f"missing reference: {ANSWER}"],
                                gates=gates), ensure_ascii=False))
        return 0

    try:
        sub = json.loads(sub_file.read_text(encoding="utf-8"))
    except Exception as e:
        gate("json_parseable", False, e)
        print(json.dumps(report(0.0, False, [f"{SUBMISSION} is not valid JSON: {e}"],
                                gates=gates), ensure_ascii=False))
        return 0
    gate("json_parseable", True)

    ref = json.loads(ref_file.read_text(encoding="utf-8"))
    expected_findings = ref.get("findings", ref if isinstance(ref, list) else [])

    if not gate("has_findings_key", isinstance(sub, dict) and "findings" in sub,
                list(sub.keys()) if isinstance(sub, dict) else type(sub).__name__):
        print(json.dumps(report(0.0, False, ["top-level 'findings' key missing"],
                                gates=gates), ensure_ascii=False))
        return 0

    findings = sub.get("findings") or []
    if not gate("findings_non_empty", len(findings) > 0, len(findings)):
        print(json.dumps(report(0.0, False, ["'findings' is empty"], gates=gates),
                         ensure_ascii=False))
        return 0

    ok_fields = all(all(k in f for k in REQUIRED_ITEM_FIELDS) for f in findings)
    if not gate("findings_have_required_fields", ok_fields,
                "each finding needs " + ",".join(REQUIRED_ITEM_FIELDS)):
        print(json.dumps(report(0.0, False,
                                ["a finding is missing a required field"], gates=gates),
                         ensure_ascii=False))
        return 0

    # ---------------- 输入材料（用于证据回溯）----------------
    # 注意：只用 inputs/ 里的材料。绝不能把答案文件本身算进材料，
    # 否则"瞎编的证据"只要和答案撞上就会被误判为 grounded。
    input_texts = []
    if inputs_dir.is_dir():
        for p in sorted(inputs_dir.rglob("*")):
            if p.is_file():
                try:
                    input_texts.append(p.read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    pass

    # ---------------- 加权 rubric ----------------
    comp = {k: 0.0 for k in WEIGHTS}

    pred_labels = {normalize_label(f.get("type")) for f in findings}
    gold_labels = {normalize_label(f.get("type")) for f in expected_findings}

    # 主/次结论：按答案里的顺序，第 1 条算 primary，第 2 条算 secondary
    ordered = sorted(expected_findings, key=lambda f: -float(f.get("priority", 0)))
    primary = ordered[0] if ordered else None
    secondary = ordered[1] if len(ordered) > 1 else None

    def finding_of(label):
        for f in findings:
            if normalize_label(f.get("type")) == label:
                return f
        return None

    if primary:
        pl = normalize_label(primary.get("type"))
        hit = finding_of(pl)
        sev_ok = hit is not None and str(hit.get("severity", "")).lower() == \
            str(primary.get("severity", "")).lower()
        comp["primary_finding"] = 1.0 if (hit is not None and sev_ok) else 0.0
    if secondary:
        sl = normalize_label(secondary.get("type"))
        comp["secondary_finding"] = 1.0 if finding_of(sl) is not None else 0.0

    # 金额档位
    gold_amounts = [parse_amount(f.get("amount_impact")) for f in expected_findings]
    gold_amounts = [a for a in gold_amounts if a is not None]
    pred_amounts = [parse_amount(f.get("amount_impact")) for f in findings]
    pred_amounts = [a for a in pred_amounts if a is not None]
    if gold_amounts:
        hits = sum(1 for g in gold_amounts
                   if any(amount_band_match(p, g) for p in pred_amounts))
        comp["amount_band"] = hits / len(gold_amounts)

    # 责任方
    gold_parties = {str(f.get("responsible_party", "")).strip().lower()
                    for f in expected_findings if f.get("responsible_party")}
    pred_parties = {str(f.get("responsible_party", "")).strip().lower()
                    for f in findings if f.get("responsible_party")}
    if gold_parties:
        comp["responsible_party"] = len(gold_parties & pred_parties) / len(gold_parties)

    # 依据条款 / 整改措施：正则命中率
    clauses = [str(f.get("clause", "")) for f in expected_findings if f.get("clause")]
    if clauses:
        blob = json.dumps(findings, ensure_ascii=False)
        comp["clause_reference"] = sum(
            1 for c in clauses if c and re.search(re.escape(c), blob, re.I)
        ) / len(clauses)

    # ★ 证据回溯
    comp["evidence_grounding"], gdetails = evidence_grounding(findings, input_texts)

    # 集合 F1
    comp["set_f1"] = f1(pred_labels, gold_labels)

    score = sum(comp[k] * WEIGHTS[k] for k in WEIGHTS)
    passed = score >= PASS_THRESHOLD

    details = gdetails + [{
        "path": k,
        "expected": WEIGHTS[k],
        "observed": round(comp[k] * WEIGHTS[k], 4),
        "correct": comp[k] >= 1.0,
    } for k in WEIGHTS]

    print(json.dumps(
        report(score, passed, [], details, gates, comp,
               f1=round(comp["set_f1"], 4)),
        ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
