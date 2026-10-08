"""编排器：把 N1~N8 批量化。并发 + 重试 + 失败跳过 + 落盘，借鉴老生题管线的批处理骨架。

- qc_batch：对已造好的任务批量跑 N6 三道校验 + N8 地板审计（数据进库关卡）
- blind_batch：批量跑 N7 盲解，估基线通过率
- build_batch：职位 → N1 选材 → 逐工作面 N3~N5 起草（造题上游）
运行产物写到 runs/<run_id>/ 下。
"""
import json
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import generate, qc, scaffold
from .cost import CostTracker


def _run_id():
    return time.strftime("%Y%m%d-%H%M%S")


def _map(fn, items, concurrency, max_retries):
    """并发执行，单项失败重试；耗尽则记 error 不中断整批。"""
    results = [None] * len(items)

    def _one(i, item):
        last = ""
        for _ in range(max_retries + 1):
            try:
                return i, fn(item)
            except Exception:  # noqa: BLE001
                last = traceback.format_exc(limit=3)
        return i, {"error": last}

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(_one, i, it) for i, it in enumerate(items)]
        for f in as_completed(futs):
            i, res = f.result()
            results[i] = res
    return results


def _dump(cfg, run_id, name, payload):
    path = os.path.join(cfg.runs_dir, run_id, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def qc_batch(tasks, cfg, run_id=None):
    run_id = run_id or _run_id()

    def _qc(task):
        n6_ok, n6 = qc.n6_verify(task, cfg.pass_threshold)
        n8_ok, n8 = qc.n8_floor_audit(task, cfg.floor)
        return {"task_id": task.task_id, "N6通过": n6_ok, "N8通过": n8_ok,
                "进库": n6_ok and n8_ok, "N6": n6, "N8": n8}

    rows = _map(_qc, tasks, cfg.concurrency, cfg.max_retries)
    passed = sum(1 for r in rows if r.get("进库"))
    summary = {"run_id": run_id, "任务数": len(tasks), "可进库": passed, "结果": rows}
    _dump(cfg, run_id, "qc.json", summary)
    return summary


def blind_batch(tasks, llm, cfg, run_id=None):
    run_id = run_id or _run_id()
    cost = CostTracker()

    def _blind(task):
        _, r = qc.n7_blind_solve(task, llm, cost)
        return r

    rows = _map(_blind, tasks, cfg.concurrency, cfg.max_retries)
    solved = sum(1 for r in rows if isinstance(r.get("score"), (int, float))
                 and r["score"] >= cfg.pass_threshold)
    summary = {"run_id": run_id, "任务数": len(tasks), "盲解通过": solved,
               "基线通过率": round(solved / len(tasks), 4) if tasks else 0.0,
               "成本RMB": cost.total(), "未计价模型": cost.unpriced_models(), "结果": rows}
    _dump(cfg, run_id, "blind.json", summary)
    cost.dump(os.path.join(cfg.runs_dir, run_id, "cost_blind.json"))
    return summary


def build_batch(occupation, llm, cfg, n_faces=10, run_id=None, do_scaffold=True):
    run_id = run_id or _run_id()
    cost = CostTracker()
    _, sel = generate.n1_select(llm, occupation, n_faces, cost)
    faces = sel["faces"]

    def _draft(face):
        _, d = generate.n3_5_draft(llm, occupation, face, cost=cost)
        return d

    drafts = _map(_draft, faces, cfg.concurrency, cfg.max_retries)

    # N3/N4 半自动：草案齐备则落成可跑任务骨架
    scaffolded = []
    if do_scaffold:
        for i, d in enumerate(drafts):
            spec = scaffold.draft_to_spec(occupation, d.get("face"), d.get("draft"), i)
            if spec:
                scaffolded.append(scaffold.scaffold_task(spec, cfg.tasks_dir))

    summary = {"run_id": run_id, "occupation": occupation, "工作面数": len(faces),
               "选材": sel, "草案": drafts, "已建骨架": scaffolded,
               "成本RMB": cost.total(), "未计价模型": cost.unpriced_models()}
    _dump(cfg, run_id, "build.json", summary)
    cost.dump(os.path.join(cfg.runs_dir, run_id, "cost_build.json"))
    return summary
