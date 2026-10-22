"""被沙箱的生题 worker（判分契约 §9.5 生产隔离 · 数值 casegen 侧）。

作为 `python -m pipeline.casegen_worker <case_dir>` 被 pipeline.sandbox.run_worker 在 sandbox-exec
子进程里拉起：模型写的 参数生成器/solve/solve2/物化 全部在**这个被隔离的进程**里执行，world 从头到尾
不出进程，只把判定结果与**渲染好的文本**经 stdout 的一行 JSON 交回宿主。宿主侧只做可信活
（scaffold / 写文件 / 夹具 / 回填 / qc），永不 import 模型代码。

两种模式：
  gate   —— 冒烟 → 难度门(含挑世界) → crosscheck → 渲染；_check_round 用，一次跑完整条门。
  render —— 只渲染；standalone materialize 用（案例已过 crosscheck）。
输出 {"stage": smoke|hardness|crosscheck|ok, ...}；ok 带 payload（task/inputs/expected/rules_md/meta）。
"""
import argparse
import dataclasses
import json
import os
import sys
import traceback

from . import casegen, crosscheck
from . import casegen_build as cb


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    return 0


def _render(case_dir, seed, world):
    """materialize 的模型代码前半：world → task/inputs/expected + 计数。world 不出进程，只回文本。"""
    物化 = casegen._load(os.path.join(case_dir, "物化.py"), "_cw_mat")
    solve_mod = casegen._load(os.path.join(case_dir, "solve.py"), "_cw_solve")
    if world is None:                                  # 与 materialize 选世界口径一致
        world = getattr(物化, "INSTANCE", None)
        if world is None:
            gen = casegen._load(os.path.join(case_dir, "参数生成器.py"), "_cw_gen")
            world = gen.worlds(1, seed)[0]
    task = 物化.TASK
    inputs = 物化.render_inputs(world)
    answer = solve_mod.solve(dict(world))
    expected = 物化.deliver(answer)
    traps = getattr(物化, "TRAPS", None) or {}
    rules = open(os.path.join(case_dir, "规则.md"), encoding="utf-8").read()
    meta = {"明细数": len(world.get("明细", []) if isinstance(world, dict) else []),
            "输出字段数": len(answer.get("明细结果", []) if isinstance(answer, dict) else []),
            "陷阱类数": len(traps)}
    return {"task": task, "inputs": inputs, "expected": expected,
            "rules_md": rules, "meta": meta}


def _gate(case_dir, seed, n, spec):
    """冒烟 → 难度门(挑世界) → crosscheck → 渲染；完全照搬 _check_round 的模型代码段落与顺序。"""
    err = cb.smoke(case_dir)
    if err:
        return {"stage": "smoke", "error": err}
    world = None
    try:
        gate_on = any(getattr(spec, f.name) for f in dataclasses.fields(spec) if f.name != "sample")
        if gate_on:
            world = cb.choose_instance(case_dir, spec, seed)   # 物化与难度门共用同一世界
        probs = cb.hardness_check(case_dir, spec, seed, world=world)
    except Exception:  # noqa: BLE001 TRAPS/渲染抛错也是返修线索
        probs = ["难度门检查出错：\n" + traceback.format_exc(limit=6)]
    if probs:
        return {"stage": "hardness", "probs": probs}
    try:
        rep = crosscheck.crosscheck(case_dir, n=n, seed=seed)
    except crosscheck.CaseError as e:
        return {"stage": "crosscheck", "error": "crosscheck 拒收：%s" % e}
    if not rep["一致"]:
        return {"stage": "crosscheck", "report": {k: rep[k] for k in
                ("不一致数", "断言破坏数", "第一个反例", "第一个断言破坏")}}
    payload = _render(case_dir, seed, world)
    payload["crosscheck世界数"] = rep["世界数"]
    return dict(payload, stage="ok")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pipeline.casegen_worker")
    ap.add_argument("case_dir")
    ap.add_argument("--mode", choices=("gate", "render"), default="gate")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--hardness-json", default="")   # dataclasses.asdict(Hardness)；缺省用 DEFAULT
    a = ap.parse_args(argv)
    try:
        if a.mode == "render":
            out = dict(_render(a.case_dir, a.seed, None), stage="ok")
        else:
            spec = cb.Hardness(**json.loads(a.hardness_json)) if a.hardness_json else cb.DEFAULT_HARDNESS
            out = _gate(a.case_dir, a.seed, a.n, spec)
    except Exception:  # noqa: BLE001 worker 永不裸抛：异常收敛成 stage=worker_error 交回宿主
        out = {"stage": "worker_error", "error": traceback.format_exc(limit=8)}
    return _emit(out)


if __name__ == "__main__":
    sys.exit(main())

