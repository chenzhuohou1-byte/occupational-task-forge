"""外部专家题桥接自测（离线、不调真软件）：造一个玩具专家包，build → 四档夹具 → qc N6/N8 → 导入外部轮次。

玩具判分器照专家包的接口写：`grade.py <candidate> --ref <reference> --out <json>`，百分制、逐项 rows。
它和真专家包一样对「结构对、内容空」的作答也给分（M01 交付齐全 10 分），用来验桥接层硬闸门能把它压到 0。
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pipeline import expert_bridge, qc, tasks  # noqa: E402

FAILURES = []


def check(cond, label):
    print("  %s %s" % ("✓" if cond else "✗", label))
    if not cond:
        FAILURES.append(label)


GRADER = r'''
import argparse, json, os
p = argparse.ArgumentParser(); p.add_argument("candidate"); p.add_argument("--ref"); p.add_argument("--out")
a = p.parse_args()
def rd(d, f):
    try: return json.load(open(os.path.join(d, f)))
    except Exception: return None
ref = rd(a.ref, "answer.json"); got = rd(a.candidate, "answer.json")
files_ok = sorted(os.listdir(a.candidate)) == ["answer.json", "log.csv"]
rows = [{"id": "M01", "weight": 10, "passed": files_ok}]
for k, w in (("V1", 40), ("V2", 30), ("V3", 20)):
    rows.append({"id": k, "weight": w, "passed": bool(got) and got.get(k) == ref[k]})
for r in rows: r["score"] = r["weight"] if r["passed"] else 0
json.dump({"status": "SCORED", "score": sum(r["score"] for r in rows), "max_score": 100,
           "failed": [r["id"] for r in rows if not r["passed"]], "rows": rows}, open(a.out, "w"))
'''


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _mk_pkg(root):
    ans = {"V1": "Tumor", "V2": 1234567.5, "V3": "release"}
    _w(os.path.join(root, "agent", "任务描述.md"), "把结果放到 `deliverables/`，格式见契约。")
    _w(os.path.join(root, "agent", "任务信息.md"), "| 文件 | 说明 |\n|---|---|\n| input/规范.md | 规范 |\n")
    _w(os.path.join(root, "agent", "input", "规范.md"), "# 规范\n类别取 Tumor / Stroma。\n")
    _w(os.path.join(root, "agent", "software", "环境说明.md"), "Python 3。\n")
    _w(os.path.join(root, "evaluator", "Rubrics", "grade.py"), GRADER)
    for d, a in (("reference", ans), ("neg_fixture", {**ans, "V3": "hold"})):
        _w(os.path.join(root, "evaluator", d, "answer.json"), json.dumps(a))
        _w(os.path.join(root, "evaluator", d, "log.csv"), "case_id,action\nA,relabel\n")
    _w(os.path.join(root, "evaluator", "model_evaluation", "grade.json"),
       json.dumps({"score": 70, "failed": ["V2"]}))
    return ans


def _conf(gates=("M01", "V1")):
    return {
        "card": {"task_id": "expert-toy-001", "领域": "_demo", "任务名": "玩具专家题", "用途": "测试集",
                 "材料来源": {"class": "synthetic", "生成方式": "handmade", "note": "自测玩具包"},
                 "scoringBasis": [{"point": "类别", "source": "material", "ref": "规范.md"}]},
        "bridge": {"grader": "Rubrics/grade.py", "golden": "reference", "negFixture": "neg_fixture",
                   "scorerVersion": "toy-1", "args": ["{candidate}", "--ref", "{reference}/reference", "--out", "{out}"],
                   "core": ["V1"], "hardGates": list(gates), "passScore": 80},
    }


def main():
    print("[外部专家题桥接]")
    tmp = tempfile.mkdtemp(prefix="expert_bridge_test_")
    try:
        pkg = os.path.join(tmp, "pkg")
        ans = _mk_pkg(pkg)
        out = os.path.join(tmp, "tasks", "toy")
        path, (got, problems) = expert_bridge.build(pkg, _conf(), out)
        check(got == {"output_test_pos": 1.0, "output_test_neg": 0.0, "output_test_random": 0.0,
                      "output_test_partial": 0.8} and not problems,
              "四档夹具：参考 1.0、删文件 0、合成假答案 0、专家负样例 0.8：%s %s" % (got, problems))
        card = json.load(open(path, encoding="utf-8"))
        body = card["prompt"].split("\n---\n")[0]
        check("output/" in body and "deliverables/" not in body,
              "专家题面里的 deliverables/ 换成 output/（附注里另说明契约中的 deliverables/ 即 output/）")
        check(os.path.isfile(os.path.join(out, "assets", "input", "任务信息.md"))
              and not os.path.exists(os.path.join(out, "assets", "input", "evaluator")),
              "input 只有 agent/ 侧材料，评测侧不进 input")
        t = tasks.load_task(out)
        ok6, d6 = qc.n6_verify(t)
        ok8, d8 = qc.n8_floor_audit(t)
        check(ok6, "qc N6 全过：%s" % [c["检查"] for c in d6.get("checks", []) if not c["通过"]])
        check(ok8 and d8["floor_score"] == 0.0, "qc N8 两档地板都为 0：%s" % d8.get("各档地板"))

        # 硬闸门：结构对、内容空的作答，专家判分器给 10 分，桥接层压到 0
        empty = os.path.join(tmp, "empty")
        _w(os.path.join(empty, "answer.json"), "{}")
        _w(os.path.join(empty, "log.csv"), "case_id,action\n")
        out2 = os.path.join(tmp, "tasks", "toy_nogate")
        expert_bridge.build(pkg, _conf(gates=()), out2)
        from pipeline import harness
        for d, want, label in ((out, 0.0, "硬闸门 M01/V1：结构对、内容空 → V1 不过 → 0"),
                               (out2, 0.1, "不设硬闸门 → 专家判分器原样给 0.1（这正是要硬闸门的原因）")):
            wd = tempfile.mkdtemp(dir=tmp)
            harness.setup_env(tasks.load_task(d), wd)
            shutil.rmtree(os.path.join(wd, "output"))
            shutil.copytree(empty, os.path.join(wd, "output"))
            r = harness.evaluate(tasks.load_task(d), wd)
            check(abs(r.score - want) < 1e-9 and not r.passed, "%s：%s" % (label, r.score))
        wd = tempfile.mkdtemp(dir=tmp)
        harness.setup_env(t, wd)
        harness.neg_solver(t, wd)
        r = harness.evaluate(t, wd)
        check(r.score == 0.0 and any("硬闸门" in e for e in r.errors), "缺一个交付文件 → 硬闸门 M01 → 0：%s" % r.errors[:2])

        # 硬闸门不许选专家实跑没过的项（会改变已有测量）
        try:
            expert_bridge.build(pkg, _conf(gates=("V2",)), os.path.join(tmp, "tasks", "bad"))
            check(False, "硬闸门选了专家实跑没过的 V2 → build 应拒绝")
        except SystemExit as e:
            check("V2" in str(e), "硬闸门选了专家实跑没过的 V2 → build 拒绝")

        # 导入外部轮次：重判、会话复查作废、外部记分对账
        runs = os.path.join(tmp, "runs")
        for i, a in enumerate([ans, ans, {**ans, "V2": 0}], 1):
            _w(os.path.join(runs, "run%d" % i, "deliverables", "answer.json"), json.dumps(a))
            _w(os.path.join(runs, "run%d" % i, "deliverables", "log.csv"), "case_id,action\n")
            _w(os.path.join(runs, "run%d_meta.json" % i), json.dumps({"score": 100 if i < 3 else 99,
                                                                      "started": "2026-11-0%dT10:00" % i}))
        _w(os.path.join(runs, "run2_session.json"), json.dumps({"messages": [{"elements": [{"children": [
            {"type": "TOOL", "toolName": "run_command", "params": {"cmd": "cat ../evaluator/reference/answer.json"}}]}]}]}))
        entry, rows = expert_bridge.import_runs(out, runs, "toy", "Claude Sonnet 5.5", "high")
        byrun = {x["run"]: x for x in rows}
        check("作废" in byrun["run2"], "会话里读过 evaluator/ 的轮次作废：%s" % byrun["run2"])
        check(byrun["run1"].get("passed") is True and byrun["run3"].get("passed") is False,
              "其余轮次按卡上判分器重判：run1 满分、run3 V2 错")
        check("对账" in byrun["run3"], "重判分与外部记分不一致时记对账：%s" % byrun["run3"].get("对账"))
        check(entry["toolset"] == "external:toy" and entry["nRuns"] == 2 and entry["fullPassRate"] == 0.5
              and entry["measuredAt"] == "2026-11-03" and entry["scorerVersion"] == "toy-1",
              "测量条目：external:toy、2 轮、满分率 0.5、日期取最晚开跑日：%s" % entry)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if FAILURES:
        print("\n✗ %d 项不符预期：\n  - %s" % (len(FAILURES), "\n  - ".join(FAILURES)))
        return 1
    print("\n✓ 外部专家题桥接：夹具、硬闸门、qc、导入重判 全部符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
