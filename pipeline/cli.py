"""命令行入口：python -m pipeline.cli <子命令>

  list                          列出所有已发现任务
  run   <任务目录> [--solver]   跑单任务（solver=golden|empty），打印判分
  qc    [--all | <任务目录>]     N6 三道校验 + N8 地板审计
  blind [--all | <任务目录>]     N7 盲解基线（用 LLM 当考生）
  build --subdomain "6.1"       N1 选材 → N3~N5 起草
"""
import argparse
import json
import sys

from . import harness, qc, scaffold, subdomains as sd
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
    pd.add_argument("--subdomain", required=True, help="ALE 子域 id，如 6.1")
    pd.add_argument("--faces", type=int, default=10)

    pf = sub.add_parser("scaffold")
    pf.add_argument("--spec", required=True, help="任务 spec 的 JSON 文件路径")

    po = sub.add_parser("subdomains", help="列出 ALE 领域/子域码表（57 槽位）")
    po.add_argument("--domain", help="列出某一级领域下的子域（domain_id 或 domain_code）；不填列各领域")
    po.add_argument("--enabled-only", action="store_true", help="只列可派活的子域")

    py = sub.add_parser("sync-schema")
    py.add_argument("task", help="任务目录：从标准答案反推字段说明写回 task_card")

    pc = sub.add_parser("crosscheck")
    pc.add_argument("--case", required=True, help="案例目录（规则.md/参数生成器.py/solve.py/solve2.py[/断言.py/容差.json]）")
    pc.add_argument("--n", type=int, default=200)
    pc.add_argument("--seed", type=int, default=0)
    pc.add_argument("--tol", type=float, default=1e-6, help="全局 absolute 容差，可被 容差.json 覆盖")

    pg = sub.add_parser("build-case", help="生成半段：模型产闭世界案例 → crosscheck → 物化 → qc")
    pg.add_argument("--subdomain", help="ALE 子域 id（如 6.1）；--resume 时可省（从旧 report.json 读）")
    pg.add_argument("--out", help="产出目录（默认 ALE_RUNS_DIR/casegen/<职业>-<时间>，不入公开仓）")
    pg.add_argument("--resume", help="续跑某个产出目录：已有的 规则/代码/solve2 直接复用，不重复花钱")
    pg.add_argument("--solve-model", default="gpt-5.5")
    pg.add_argument("--solve2-model", default="glm-5.3-flash")
    pg.add_argument("--rounds", type=int, default=3, help="返修预算：最多几轮，仍不过即作废")
    pg.add_argument("--n", type=int, default=200, help="crosscheck 世界数")
    pg.add_argument("--call-gap", type=float, default=30.0, help="同一模型两次调用的最小间隔秒数（防 429）")
    pg.add_argument("--refs", help="参考资料目录（人工抓好的真实口径材料 + 来源.json），"
                                   "给了就要求规则.md 声明「## 引用来源」并逐字核验")

    pn = sub.add_parser("build-case-batch",
                        help="连跑 N 次 build-case，实测产出率（每次尝试产出一条可用数据的比率）")
    pn.add_argument("--subdomains", required=True, help="ALE 子域 id 列表，逗号分隔，如 6.1,4.4,9.4")
    pn.add_argument("--repeat", type=int, default=1, help="每个职业各跑几次")
    pn.add_argument("--out-root", help="跑批根目录（默认 ALE_RUNS_DIR/casegen/batch-<时间>）")
    pn.add_argument("--solve-model", default="gpt-5.5")
    pn.add_argument("--solve2-model", default="glm-5.3-flash")
    pn.add_argument("--rounds", type=int, default=3)
    pn.add_argument("--n", type=int, default=200)
    pn.add_argument("--call-gap", type=float, default=30.0)

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
        _pp(build_batch(args.subdomain, llm, cfg, args.faces))
        return 0

    if args.cmd == "scaffold":
        with open(args.spec, encoding="utf-8") as f:
            spec = json.load(f)
        _pp(scaffold.scaffold_task(spec, cfg.tasks_dir))
        return 0

    if args.cmd == "subdomains":
        if args.domain:
            for sid in sd.subdomains(args.domain, enabled_only=args.enabled_only):
                s = sd.get(sid)
                flag = "" if s["enabled"] else "  ✗ %s" % s["blocked_by"]
                print("%-6s %-24s %-48s ALE %2d 条%s" % (
                    sid, s["code"] or "-", s["name"] or "（ALE 未公开）", s["ale_n"], flag))
        else:
            for did, code, n_sub, ale_n in sd.domains():
                on = len(sd.subdomains(did, enabled_only=True))
                print("%-3s %-22s 子域 %2d（可派活 %2d）  ALE %2d 条" % (did, code, n_sub, on, ale_n))
        return 0

    if args.cmd == "sync-schema":
        _pp(scaffold.sync_schema(args.task))
        return 0

    if args.cmd == "crosscheck":
        return _crosscheck.run_cli(args.case, args.n, args.seed, args.tol)

    if args.cmd == "build-case":
        from . import casegen_build
        return casegen_build.run_build(args.subdomain, args.out, args.solve_model,
                                       args.solve2_model, args.rounds, args.n,
                                       resume=args.resume, call_gap=args.call_gap,
                                       refs=args.refs)

    if args.cmd == "build-case-batch":
        from . import casegen_build
        sids = [s.strip() for s in args.subdomains.split(",") if s.strip()]
        if not sids:
            print("✗ --subdomains 至少给一个子域 id", file=sys.stderr)
            return 2
        bad = [s for s in sids if s not in sd.load()["subdomains"]]
        if bad:
            print("✗ 不认识的子域：%s。可派活的：%s"
                  % ("、".join(bad), "、".join(sd.subdomains(enabled_only=True))), file=sys.stderr)
            return 2
        return casegen_build.run_batch(
            sids, repeat=args.repeat, out_root=args.out_root,
            solve_model=args.solve_model, solve2_model=args.solve2_model,
            rounds=args.rounds, n=args.n, call_gap=args.call_gap)

    return 0


if __name__ == "__main__":
    sys.exit(main())
