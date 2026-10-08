#!/usr/bin/env bash
# 判分器自测（出题人的自证义务）
#
#   正例   fixtures/output_test_pos      必须得 1.0，passed = true
#   负例   fixtures/output_test_neg      必须得 0.0，passed = false（结构不完整，闸门拦下）
#   诊断例 fixtures/output_test_wrong    编造证据 + 标签全错 -> 0.0，且 evidence_grounding = 0
#   诊断例 fixtures/output_test_partial  标签对、证据真，但档位判错 -> 部分分，passed = false
#
# 前两条是验收项；后两条用来证明「反幻觉」和「部分分」两个机制真的在工作。
#
# 用法：bash run_selftest.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCORER="$HERE/score_outputs.py"
REF="$HERE/fixtures/reference"

python3 - "$SCORER" "$REF" "$HERE" <<'PY'
import json, subprocess, sys
from pathlib import Path

scorer, ref, here = sys.argv[1], sys.argv[2], Path(sys.argv[3])

CASES = [
    ("正例 output_test_pos",     "output_test_pos",    1.0,  True,  None),
    ("负例 output_test_neg",     "output_test_neg",    0.0,  False, None),
    ("诊断例 output_test_wrong",  "output_test_wrong",  0.0,  False, 0.0),
    ("诊断例 output_test_partial", "output_test_partial", None, False, 1.0),
]

failures = []
print("=" * 74)
print("判分器自测报告")
print("=" * 74)

for label, outdir, want_score, want_passed, want_grounding in CASES:
    out = Path(here) / "fixtures" / outdir
    proc = subprocess.run(
        [sys.executable, scorer, "--output", str(out), "--reference", ref],
        capture_output=True, text=True,
    )
    print(f"\n[{label}]")
    if proc.returncode != 0:
        print(f"  !! 判分器退出码 {proc.returncode}（判分器自身故障）")
        print("  stderr:", proc.stderr.strip()[:300])
        failures.append(f"{label}: 退出码 {proc.returncode}")
        continue

    try:
        rep = json.loads(proc.stdout)
    except Exception as e:
        print(f"  !! stdout 不是合法 JSON: {e}")
        print("  stdout:", proc.stdout[:300])
        failures.append(f"{label}: stdout 非 JSON")
        continue

    score = rep["score"]
    passed = rep["passed"]
    grounding = (rep.get("components") or {}).get("evidence_grounding")
    print(f"  score   = {score}")
    print(f"  passed  = {passed}")
    if grounding is not None:
        print(f"  evidence_grounding = {grounding}")
    if rep.get("gates"):
        bad = [g["name"] for g in rep["gates"] if not g["passed"]]
        print(f"  闸门    = {len(rep['gates'])} 道，未通过：{bad if bad else '无'}")
    if rep.get("errors"):
        print(f"  errors  = {rep['errors']}")

    if want_score is not None and abs(score - want_score) > 1e-9:
        failures.append(f"{label}: score 期望 {want_score}，实际 {score}")
    if want_passed is not None and passed != want_passed:
        failures.append(f"{label}: passed 期望 {want_passed}，实际 {passed}")
    if want_grounding is not None and grounding is not None and abs(grounding - want_grounding) > 1e-9:
        failures.append(f"{label}: evidence_grounding 期望 {want_grounding}，实际 {grounding}")
    if want_grounding is not None and grounding is None:
        failures.append(f"{label}: 缺少 evidence_grounding 分项")

print("\n" + "=" * 74)
if failures:
    print("自测未通过：")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("自测全部通过：正例 1.0 / 负例 0.0 / 编造证据 0.0 / 档位判错得部分分 0.6")
print("=" * 74)
PY
