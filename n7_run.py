#!/usr/bin/env python3
"""N7 实测难度 runner —— 判分契约 §6.3 入库门槛的测量端。

把一条任务当「单发盲解」交给指定模型跑 N 次，统计 fullPassRate / meanScore，
产出判分契约 §6.2 形状的 measuredDifficulty 条目。

重建自丢失的 /tmp/n7_run.py —— 这次落在仓库里（交接坑：/tmp 会被清）。
核心逻辑复用 pipeline.qc.n7_blind_solve（它已按契约返回 passed(bool)），
本脚本只负责「跑 N 次 + 统计 + 按契约 §6.2 成形 + 对门槛 §6.3」。

用法（真跑，花钱）：
    ALE_LLM_PROVIDER=openai ALE_LLM_MODEL=glm-5.3-flash N7_RUNS=3 \
    N7_TASK=zaojia-biangeng-suopei-jiesuan-001 N7_TIER=便宜档 \
    N7_OUT=runs/n7/glm.json python3 n7_run.py

自测（离线、不花钱、只验证控制流）：
    python3 n7_run.py --selftest

门槛（§6.3）：便宜档 fullPassRate 必须 = 0；前沿档 ≤ 1/3。
本脚本只测量、只写 N7_OUT，**不改 task_card.measuredDifficulty**（填卡是单独一步）。
"""
import datetime
import json
import os
import sys
import time

from pipeline import agentsolve, qc, tasks, llm
from pipeline.config import PipelineConfig
from pipeline.cost import CostTracker

DEFAULT_TASK = "zaojia-biangeng-suopei-jiesuan-001"
GATE = {"便宜档": ("== 0", lambda r: r == 0.0),
        "前沿档": ("<= 1/3", lambda r: r <= 1.0 / 3 + 1e-9)}


def resolve_task(cfg, ident):
    """ident 可以是 task_id、或相对/绝对任务目录路径。"""
    if ident and os.path.isdir(ident) and os.path.isfile(os.path.join(ident, "task_card.json")):
        return tasks.load_task(ident)
    found = tasks.discover_tasks(cfg.tasks_dir)
    for t in found:
        if t.task_id == ident or t.dir.rstrip("/").endswith(ident):
            return t
    ids = "\n".join("  - %s" % t.task_id for t in found)
    sys.exit("找不到任务 %r。可选：\n%s" % (ident, ids))


def main():
    selftest = "--selftest" in sys.argv
    cfg = PipelineConfig()
    if selftest:
        cfg.llm.provider = "mock"   # 离线，不触网
        print("[selftest] provider=mock —— 只验证控制流，分数无意义", file=sys.stderr)

    if cfg.llm.provider == "mock" and not selftest:
        print("⚠️  provider=mock：这不是真实测量。真跑请设 ALE_LLM_PROVIDER=openai + ALE_LLM_MODEL",
              file=sys.stderr)

    n_runs = int(os.environ.get("N7_RUNS", "2" if selftest else "3"))
    task = resolve_task(cfg, os.environ.get("N7_TASK", DEFAULT_TASK))
    tier = os.environ.get("N7_TIER", "").strip()
    mode = os.environ.get("N7_MODE", "blind").strip()    # blind=单发盲解 / agent=多步带文件工具
    max_steps = int(os.environ.get("N7_MAX_STEPS", "20"))
    if mode not in ("blind", "agent"):
        sys.exit("N7_MODE 只认 blind / agent，给的是 %r" % mode)
    client = llm.get_client(cfg.llm)
    cost = CostTracker()

    runs, errors = [], []
    gap = float(os.environ.get("ALE_LLM_INTER_RUN_SLEEP", "0"))
    for i in range(n_runs):
        if i and gap:
            time.sleep(gap)   # 跑间留白，躲限流严的模型（如 gpt-5.5）的突发 429
        try:
            if mode == "agent":
                _, r = agentsolve.agent_solve(task, client, cost, max_steps)
            else:
                _, r = qc.n7_blind_solve(task, client, cost)
            runs.append(r)
            print("  run %d/%d  score=%.4f passed=%s 写出文件=%d%s"
                  % (i + 1, n_runs, r["score"], r["passed"], r["写出文件数"],
                     "  步数=%s 噪声步=%s" % (r.get("步数"), r.get("协议噪声步"))
                     if mode == "agent" else ""), file=sys.stderr)
        except Exception as e:   # 网关 504 / 断流等 = 测量故障，不记成「agent 不会做」
            errors.append("run %d: %s: %s" % (i + 1, type(e).__name__, e))
            print("  run %d/%d  ✗ 测量故障：%s" % (i + 1, n_runs, e), file=sys.stderr)


    n_ok = len(runs)
    scores = [r["score"] for r in runs]
    full_pass = round(sum(1 for r in runs if r["passed"]) / n_ok, 4) if n_ok else None
    mean_score = round(sum(scores) / n_ok, 4) if n_ok else None

    # 判分契约 §6.2 形状：model/toolset/budget/scorerVersion/nRuns 必须钉住
    entry = {
        "model": cfg.llm.model,
        # 换工具数字就不可比（§6.2）：单发盲解 vs 多步带文件工具是两种考法
        "toolset": "single-shot-blind" if mode == "blind" else "agent-file-tools",
        "budget": {"maxSteps": 1 if mode == "blind" else max_steps,
                   "maxTokens": int(os.environ.get("ALE_LLM_MAX_TOKENS", "16000")),
                   "reasoningEffort": os.environ.get("ALE_LLM_REASONING_EFFORT", "") or None,
                   "wallClockMin": None},
        "scorerVersion": os.environ.get("N7_SCORER_VERSION", "1.0"),
        "nRuns": n_ok,
        "fullPassRate": full_pass,
        "meanScore": mean_score,
        "measuredAt": datetime.date.today().isoformat(),
    }

    report = {
        "task_id": task.task_id,
        "entry": entry,
        "_runsRequested": n_runs,
        "_perRun": runs,
        "_errors": errors,
        "_costRMB": cost.total(),
        "_unpricedModels": cost.unpriced_models(),
    }

    # 门槛判定（§6.3）—— 只在给了 N7_TIER 时判
    if tier in GATE and full_pass is not None:
        desc, ok = GATE[tier]
        report["_gate"] = {"tier": tier, "rule": "fullPassRate %s" % desc,
                           "fullPassRate": full_pass, "pass": ok(full_pass)}
        print("\n门槛[%s] fullPassRate %s → %s（实测 %.4f）"
              % (tier, desc, "过" if ok(full_pass) else "不过 ✗", full_pass), file=sys.stderr)
    elif tier and tier not in GATE:
        print("⚠️  N7_TIER=%r 不认识，只认 便宜档/前沿档" % tier, file=sys.stderr)

    out = os.environ.get("N7_OUT")
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print("\n已写入 %s" % out, file=sys.stderr)
    else:
        print(text)

    print("\nnRuns=%d(请求%d) fullPassRate=%s meanScore=%s 成本RMB=%s%s"
          % (n_ok, n_runs, full_pass, mean_score, cost.total(),
             "  ⚠️有%d次测量故障" % len(errors) if errors else ""), file=sys.stderr)


if __name__ == "__main__":
    main()
