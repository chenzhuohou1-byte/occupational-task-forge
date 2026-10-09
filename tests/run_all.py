#!/usr/bin/env python3
"""全仓自测总入口：`python3 tests/run_all.py`（CI 跑的就是这一条）。

为什么要有：框架是公开仓、main 已设强制 PR + 1 评审，但评审全靠人眼读 ——
1008 那三个「前半链路从没跑过」的潜伏 bug 就是这么积累起来的。这里把所有
不花钱、不触网的自测串成一条命令，任何改动都得先过它。

覆盖：
  1) N4 造附件自测（声明式规格 → 真实 xlsx/pdf/docx/csv，含降级与接线）
  2) 程序化地板合成自测（键名/表头/行键保留、值全变）
  3) agent 档动作循环自测（含「协议噪声 → 测量故障」这条诚实性路径）
  4) N7 runner 控制流自测（mock provider，不触网）
  5) 对自带 `tasks/_demo` 跑 qc：N6/N8 必须全过；难度门槛必须**不过**
     （demo 故意是水题，难度未测 → 不该进库。这一条同时在回归「难度闸真的在拦」）
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def _run(label, args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    r = subprocess.run([PY] + args, cwd=ROOT, capture_output=True, text=True, env=e)
    if r.returncode != 0:
        print("✗ %s\n--- stdout ---\n%s\n--- stderr ---\n%s"
              % (label, r.stdout[-3000:], r.stderr[-3000:]))
        return False, r
    print("✓ %s" % label)
    return True, r


def check_demo_qc():
    """对 _demo 跑 qc，逐项核对闸门行为（而不是只看一个总布尔）。"""
    env = {"ALE_TASKS_DIR": os.path.join(ROOT, "tasks"),
           "ALE_RUNS_DIR": os.path.join(ROOT, ".ci-runs")}
    ok, r = _run("qc 跑通 tasks/_demo", ["-m", "pipeline.cli", "qc", "--all"], env)
    if not ok:
        return False
    data = json.loads(r.stdout)
    rows = [x for x in data["结果"] if x.get("task_id") == "demo-procurement-tally-001"]
    if not rows:
        print("✗ qc 没发现 _demo 任务（tasks/_demo 丢了？）")
        return False
    row = rows[0]
    bad = []
    if not row.get("N6通过"):
        bad.append("N6 应全过，实际失败项：%s"
                   % [c["检查"] for c in row["N6"]["checks"] if not c["通过"]])
    if not row.get("N8通过"):
        bad.append("N8 地板应为 0，实际：%s" % row["N8"].get("各档地板"))
    if row.get("难度门槛通过"):
        bad.append("难度门槛本该不过（demo 是水题、measuredDifficulty 为空）却判过了"
                   "——难度闸失效")
    if row.get("进库"):
        bad.append("demo 不该可进库")
    if bad:
        print("✗ _demo qc 行为不符预期：\n  - " + "\n  - ".join(bad))
        return False
    print("✓ _demo qc 闸门行为正确（N6/N8 过、难度未测 → 不进库）")
    return True


def main():
    results = [
        _run("N4 造附件自测", ["-m", "pipeline.attachments_selftest"])[0],
        _run("程序化地板自测", ["-m", "pipeline.floor"])[0],
        _run("agent 档动作循环自测", ["-m", "pipeline.agentsolve"])[0],
        _run("N7 runner 控制流自测", ["n7_run.py", "--selftest"],
             {"ALE_TASKS_DIR": os.path.join(ROOT, "tasks"),
              "ALE_RUNS_DIR": os.path.join(ROOT, ".ci-runs"),
              "N7_TASK": "demo-procurement-tally-001"})[0],
        check_demo_qc(),
    ]
    n_ok = sum(1 for x in results if x)
    print("\n%d/%d 通过" % (n_ok, len(results)))
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
