"""命令行入口：python -m pipeline.cli <子命令>

  list                          列出所有已发现任务
  run   <任务目录> [--solver]   跑单任务（solver=golden|empty），打印判分
  qc    [--all | <任务目录>]     N6 三道校验 + N8 地板审计
  blind [--all | <任务目录>]     N7 盲解基线（用 LLM 当考生）
  build --occupation "职业"      N1 选材 → N3~N5 起草
"""
import argparse
import json
import sys

from . import harness, qc, scaffold, occupations as occ
from . import crosscheck as _crosscheck
from .config import PipelineConfig
from .llm import get_client
from .orchestrator import blind_batch, build_batch, qc_batch
from .tasks import discover_tasks, load_task


def _pp(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _tasks(args, cfg):
    if getattr(args, "all", False) or not getattr(args, "task", None):
        return discover_tasks(cfg.tasks_dir)
    return [load_task(args.task)]


def main(argv=None):
    cfg = PipelineConfig()
    ap = argparse.ArgumentParser(prog="pipeline.cli", description="ALE 式数据集产线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    pr = sub.add_parser("run")
    pr.add_argument("task")
    pr.add_argument("--solver", choices=["golden", "empty", "neg"], default="golden")

    pq = sub.add_parser("qc")
    pq.add_argument("task", nargs="?")
    pq.add_argument("--all", action="store_true")

    pb = sub.add_parser("blind")
    pb.add_argument("task", nargs="?")
    pb.add_argument("--all", action="store_true")

    pd = sub.add_parser("build")
    pd.add_argument("--occupation", required=True)
    pd.add_argument("--faces", type=int, default=10)

    pf = sub.add_parser("scaffold")
    pf.add_argument("--spec", required=True, help="任务 spec 的 JSON 文件路径")

    po = sub.add_parser("occupations")
    po.add_argument("--domain", help="列出某领域下的职业；不填则列出各领域及职业数")

    py = sub.add_parser("sync-schema")
    py.add_argument("task", help="任务目录：从标准答案反推字段说明写回 task_card")

    pc = sub.add_parser("crosscheck")
    pc.add_argument("--case", required=True, help="案例目录（规则.md/参数生成器.py/solve.py/solve2.py[/断言.py/容差.json]）")
    pc.add_argument("--n", type=int, default=200)
    pc.add_argument("--seed", type=int, default=0)
    pc.add_argument("--tol", type=float, default=1e-6, help="全局 absolute 容差，可被 容差.json 覆盖")

    pg = sub.add_parser("build-case", help="生成半段：模型产闭世界案例 → crosscheck → 物化 → qc")
    pg.add_argument("--occupation", required=True)
    pg.add_argument("--out", help="产出目录（默认 ALE_RUNS_DIR/casegen/<职业>-<时间>，不入公开仓）")
    pg.add_argument("--solve-model", default="gpt-5.5")
    pg.add_argument("--solve2-model", default="glm-5.3-flash")
    pg.add_argument("--rounds", type=int, default=3, help="返修预算：最多几轮，仍不过即作废")
    pg.add_argument("--n", type=int, default=200, help="crosscheck 世界数")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        for t in discover_tasks(cfg.tasks_dir):
            print("%s\t%s" % (t.task_id, t.dir))
        return 0

    if args.cmd == "run":
        task = load_task(args.task)
        solver = {"golden": harness.golden_solver, "empty": harness.empty_solver,
                  "neg": harness.neg_solver}[args.solver]
        r = harness.run_task(task, solver=solver)
        _pp({"task_id": r.task_id, "score": r.score, "passed": r.passed,
             "errors": r.errors, "details": r.details})
        return 0 if r.passed else 1

    if args.cmd == "qc":
        _pp(qc_batch(_tasks(args, cfg), cfg))
        return 0

    if args.cmd == "blind":
        llm = get_client(cfg.llm)
        _pp(blind_batch(_tasks(args, cfg), llm, cfg))
        return 0

    if args.cmd == "build":
        llm = get_client(cfg.llm)
        _pp(build_batch(args.occupation, llm, cfg, args.faces))
        return 0

    if args.cmd == "scaffold":
        with open(args.spec, encoding="utf-8") as f:
            spec = json.load(f)
        _pp(scaffold.scaffold_task(spec, cfg.tasks_dir))
        return 0

    if args.cmd == "occupations":
        if args.domain:
            for o in occ.occupations(args.domain):
                print(o)
        else:
            for name, n in occ.domains():
                print("%s\t%d" % (name, n))
        return 0

    if args.cmd == "sync-schema":
        _pp(scaffold.sync_schema(args.task))
        return 0

    if args.cmd == "crosscheck":
        return _crosscheck.run_cli(args.case, args.n, args.seed, args.tol)

    if args.cmd == "build-case":
        from . import casegen_build
        return casegen_build.run_build(args.occupation, args.out, args.solve_model,
                                       args.solve2_model, args.rounds, args.n)

    return 0


if __name__ == "__main__":
    sys.exit(main())
