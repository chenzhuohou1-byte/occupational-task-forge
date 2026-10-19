#!/usr/bin/env python3
"""N7 实测难度 runner —— 判分契约 §6.3 入库门槛的测量端。

把一条任务当「单发盲解」交给指定模型跑 N 次，统计 fullPassRate / meanScore，
产出判分契约 §6.2 形状的 measuredDifficulty 条目。

重建自丢失的 /tmp/n7_run.py —— 这次落在仓库里（交接坑：/tmp 会被清）。
核心逻辑复用 pipeline.qc.n7_blind_solve（它已按契约返回 passed(bool)），
本脚本只负责「跑 N 次 + 统计 + 按契约 §6.2 成形 + 对门槛 §6.3」。

用法（真跑，花钱）：
    ALE_LLM_PROVIDER=openai ALE_LLM_MODEL=glm-5.3-flash N7_RUNS=10 \
    N7_TASK=zaojia-biangeng-suopei-jiesuan-001 N7_TIER=便宜档 \
    N7_OUT=runs/n7/glm.json python3 n7_run.py

自测（离线、不花钱、只验证控制流）：
    python3 n7_run.py --selftest

门槛（§6.3）：便宜档 fullPassRate 必须 = 0；前沿档 ≤ 1/3。
默认只测量、只写 N7_OUT；加 `--write-card`（或 N7_WRITE_CARD=1）才把 entry 追加进
task_card.measuredDifficulty —— 原来全靠手抄，抄错/漏抄 qc 都发现不了。
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
    """ident 可以是 task_id、或相对/绝对任务目录路径。

    宁可报错也不猜：空 ident、匹配不到、匹配到多条，一律退出并列出候选。
    原来空字符串会命中 `t.dir.endswith("")`（任何字符串都以空串结尾），静默测了排第一的任务——
    实测一次 `N7_TASK="$T"` 而 T 未赋值，难度体检被测到了一条无关任务上；同一 task_id
    出现在多个目录时也只取第一个。路径后缀匹配现在要求落在目录边界上。
    """
    ident = (ident or "").strip()
    if not ident:
        sys.exit("N7_TASK 为空：没指定要测哪条任务（多半是 shell 变量没赋值，"
                 "如 N7_TASK=\"$T\" 而 T 未定义）")
    if os.path.isdir(ident):
        if os.path.isfile(os.path.join(ident, "task_card.json")):
            return tasks.load_task(ident)
        sys.exit("N7_TASK 是目录但不是任务目录（缺 task_card.json）：%s" % ident)
    found = tasks.discover_tasks(cfg.tasks_dir)
    hits = [t for t in found if t.task_id == ident]
    if not hits:
        tail = os.sep + ident.strip(os.sep)
        hits = [t for t in found if t.dir.rstrip(os.sep).endswith(tail)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        sys.exit("N7_TASK=%r 匹配到 %d 条任务，不知道该测哪条——请改用任务目录的绝对路径：\n%s"
                 % (ident, len(hits), "\n".join("  - %s  %s" % (t.task_id, t.dir) for t in hits)))
    ids = "\n".join("  - %s" % t.task_id for t in found)
    sys.exit("找不到任务 %r。可选：\n%s" % (ident, ids))


def _resolve_selftest(cfg):
    """resolve_task 的离线回归：空 / 不存在 / 多重匹配一律退出，唯一匹配才返回。"""
    import shutil
    import tempfile

    def exits(ident, c=cfg):
        try:
            resolve_task(c, ident)
            return False
        except SystemExit:
            return True

    found = tasks.discover_tasks(cfg.tasks_dir)
    assert found, "selftest 需要 ALE_TASKS_DIR 下至少有一条任务"
    t0 = found[0]
    assert exits(""), "空 N7_TASK 应报错退出"
    assert exits("   "), "空白 N7_TASK 应报错退出"
    assert exits("no-such-task-xyz"), "不存在的任务应报错退出"
    assert resolve_task(cfg, t0.task_id).dir == t0.dir, "唯一 task_id 应命中"
    assert resolve_task(cfg, t0.dir).dir == t0.dir, "任务目录路径应命中"
    tmp = tempfile.mkdtemp(prefix="n7_resolve_")
    try:
        for i in (1, 2):
            shutil.copytree(t0.dir, os.path.join(tmp, "copy%d" % i))

        class _Cfg:
            tasks_dir = tmp
        assert exits(t0.task_id, _Cfg), "同一 task_id 出现在两个目录应报错退出（不许静默取第一个）"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("[selftest] resolve_task：空/不存在/多重匹配均报错退出，唯一匹配命中", file=sys.stderr)


def write_card(task, entry):
    """把一条测量 entry 追加进 task_card.measuredDifficulty（§6.2：每次测量追加一条）。

    只追加、不改旧条目；完全相同的 entry 已在卡上就跳过（同一结果文件重复回填）。
    nRuns=0（全是测量故障）不写：那不是一次测量。tier 不写进卡——qc 每次按
    measuredDifficulty 重算（§6.3），这里只打印派生出的级别供核对。
    返回 (是否写入, 说明)。
    """
    if not entry.get("nRuns"):
        return False, "nRuns=0（全是测量故障），不回填"
    path = os.path.join(task.dir, "task_card.json")
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    card = json.loads(raw)
    md = card.get("measuredDifficulty")
    if md is None:
        md = card["measuredDifficulty"] = []
    if not isinstance(md, list):
        return False, "卡上 measuredDifficulty 不是数组，拒绝改写：%s" % path
    if entry in md:
        return False, "完全相同的条目已在卡上，跳过"
    md.append(entry)
    with open(path, "w", encoding="utf-8") as f:   # 保持原文件是否带结尾换行，减少无关 diff
        f.write(json.dumps(card, ensure_ascii=False, indent=2) + ("\n" if raw.endswith("\n") else ""))
    task.card = card
    ok, d = qc.difficulty_tier(task)
    return True, "已追加到 %s（第 %d 条）；派生级别：%s%s" % (
        path, len(md), d.get("tier") if ok else "定不出", "（%s）" % d.get("级别来源") if ok else "")


def _write_card_selftest(task):
    """write_card 的离线回归：在临时副本上追加、去重、拒写 nRuns=0，原卡不动。"""
    import shutil
    import tempfile
    orig = open(os.path.join(task.dir, "task_card.json"), encoding="utf-8").read()
    tmp = tempfile.mkdtemp(prefix="n7_card_")
    try:
        d = os.path.join(tmp, "t")
        shutil.copytree(task.dir, d)
        t = tasks.load_task(d)
        n0 = len(t.card.get("measuredDifficulty") or [])
        e = {"model": "selftest", "nRuns": 2, "fullPassRate": 0.5, "meanScore": 0.5}
        assert write_card(t, e)[0], "首次回填应写入"
        assert not write_card(t, dict(e))[0], "相同条目应跳过"
        assert not write_card(t, {**e, "nRuns": 0})[0], "nRuns=0 不应写入"
        md = tasks.load_task(d).card["measuredDifficulty"]
        assert len(md) == n0 + 1 and md[-1] == e, "卡上应恰好多一条且内容一致"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert open(os.path.join(task.dir, "task_card.json"), encoding="utf-8").read() == orig, "原卡被改了"
    print("[selftest] write_card：追加/去重/拒写 nRuns=0 正确，原卡未动", file=sys.stderr)


def _trace_base(cfg, task, mode):
    """每次测量的轨迹目录。默认放在 N7_OUT 旁边的 `<同名>_trace/`，没给 N7_OUT 就
    放 runs/n7-trace/<task>/<model>-<mode>/。设 N7_NO_TRACE=1 关掉（省磁盘）。

    留轨迹的理由：难度数字是入库门槛的依据，而原来只留 score —— 分数看着不对时
    既查不到喂了什么、也查不到它交了什么。轨迹含模型表现，**一律不进公开仓**。
    """
    if os.environ.get("N7_NO_TRACE") == "1":
        return ""
    explicit = os.environ.get("N7_TRACE_DIR")
    if explicit:
        return os.path.abspath(explicit)
    out = os.environ.get("N7_OUT")
    if out:
        return os.path.splitext(os.path.abspath(out))[0] + "_trace"
    return os.path.join(cfg.runs_dir, "n7-trace", task.task_id,
                        "%s-%s" % (os.environ.get("ALE_LLM_MODEL", "model"), mode))


def main():
    selftest = "--selftest" in sys.argv
    write = "--write-card" in sys.argv or os.environ.get("N7_WRITE_CARD") == "1"
    cfg = PipelineConfig()
    if "--from" in sys.argv:
        # 补填已有结果：python3 n7_run.py --write-card --from runs/n7/x.json（不触网、不花钱）
        i = sys.argv.index("--from")
        if not write or i + 1 >= len(sys.argv):
            sys.exit("用法：n7_run.py --write-card --from <N7_OUT 结果文件>")
        with open(sys.argv[i + 1], encoding="utf-8") as f:
            rep = json.load(f)
        print(write_card(resolve_task(cfg, rep["task_id"]), rep["entry"])[1], file=sys.stderr)
        return
    if selftest:
        cfg.llm.provider = "mock"   # 离线，不触网
        print("[selftest] provider=mock —— 只验证控制流，分数无意义", file=sys.stderr)

    if cfg.llm.provider == "mock" and not selftest:
        print("⚠️  provider=mock：这不是真实测量。真跑请设 ALE_LLM_PROVIDER=openai + ALE_LLM_MODEL",
              file=sys.stderr)

    # 默认 10 跑（原 3）。3 跑的满分率只能落在 0 / ⅓ / ⅔ / 1 四个值上，而 L1-L4 的门槛是
    # ≥2/3、(0,2/3)、=0、≤1/3——L2 的「0<满分率<2/3」只能由 ⅓ 这一个取值命中。实算误判率：
    # 真实满分率 0.5 的题在 n=3 下有 50% 概率被误标 L4，n=10 降到 17%。定级全靠这个数。
    n_runs = int(os.environ.get("N7_RUNS", "2" if selftest else "10"))
    if selftest:
        _resolve_selftest(cfg)
    task = resolve_task(cfg, os.environ.get("N7_TASK", DEFAULT_TASK))
    if selftest:
        _write_card_selftest(task)
    if write and cfg.llm.provider == "mock":
        sys.exit("--write-card 拒绝在 provider=mock 下回填：mock 分数不是测量")
    tier = os.environ.get("N7_TIER", "").strip()
    mode = os.environ.get("N7_MODE", "blind").strip()    # blind=单发盲解 / agent=多步带文件工具
    max_steps = int(os.environ.get("N7_MAX_STEPS", "20"))
    # agent 档工具集：python（默认，可跑代码，对齐 ALE 沙箱的 run_command）/ file（旧考法，仅对照）
    tools = os.environ.get("N7_TOOLS", "python").strip()
    if mode not in ("blind", "agent"):
        sys.exit("N7_MODE 只认 blind / agent，给的是 %r" % mode)
    if tools not in agentsolve.TOOLSETS:
        sys.exit("N7_TOOLS 只认 %s，给的是 %r" % (list(agentsolve.TOOLSETS), tools))
    client = llm.get_client(cfg.llm)
    cost = CostTracker()

    runs, errors = [], []
    gap = float(os.environ.get("ALE_LLM_INTER_RUN_SLEEP", "0"))
    fault_retry = int(os.environ.get("N7_FAULT_RETRY", "2"))   # 每个槽位遇测量故障最多补跑几次
    trace_base = _trace_base(cfg, task, mode)

    def _one(td):
        if mode == "agent":
            _, r = agentsolve.agent_solve(task, client, cost, max_steps, trace_dir=td, tools=tools)
        else:
            _, r = qc.n7_blind_solve(task, client, cost, trace_dir=td)
        return r

    slept = [False]   # 控制跑间 gap：除第一次外，每次真实调用前留白

    def _sleep_between():
        if slept[0] and gap:
            time.sleep(gap)   # 跑间留白，躲限流严的模型（如 gpt-5.5）的突发 429
        slept[0] = True

    for i in range(n_runs):
        # 每个槽位：先正常跑；是测量故障（如 finish_reason=length / 504 / 断流）就补跑，
        # 直到拿到一次有效结果或用尽 fault_retry 预算。补跑不改 maxSteps/maxTokens，口径不变。
        got = False
        for attempt in range(fault_retry + 1):
            tag = "run%d" % (i + 1) if attempt == 0 else "run%d_retry%d" % (i + 1, attempt)
            td = os.path.join(trace_base, tag) if trace_base else None
            _sleep_between()
            try:
                r = _one(td)
            except Exception as e:   # 网关 504 / 断流 / finish_reason=length 等 = 测量故障
                errors.append("%s: %s: %s" % (tag, type(e).__name__, e))
                more = attempt < fault_retry
                print("  run %d/%d%s  ✗ 测量故障：%s%s"
                      % (i + 1, n_runs, "" if attempt == 0 else "(补跑%d)" % attempt, e,
                         "，补跑…" if more else "，补跑预算用尽"), file=sys.stderr)
                continue
            runs.append(r)
            print("  run %d/%d%s  score=%.4f passed=%s 写出文件=%d%s"
                  % (i + 1, n_runs, "" if attempt == 0 else "(补跑%d)" % attempt,
                     r["score"], r["passed"], r["写出文件数"],
                     "  步数=%s 噪声步=%s" % (r.get("步数"), r.get("协议噪声步"))
                     if mode == "agent" else ""), file=sys.stderr)
            got = True
            break
        if not got:
            print("  run %d/%d  ✗ 该槽位补跑 %d 次仍全是测量故障，放弃"
                  % (i + 1, n_runs, fault_retry), file=sys.stderr)



    n_ok = len(runs)
    scores = [r["score"] for r in runs]
    full_pass = round(sum(1 for r in runs if r["passed"]) / n_ok, 4) if n_ok else None
    mean_score = round(sum(scores) / n_ok, 4) if n_ok else None

    # 判分契约 §6.2 形状：model/toolset/budget/scorerVersion/nRuns 必须钉住
    entry = {
        "model": cfg.llm.model,
        # 换工具数字就不可比（§6.2）：单发盲解 vs 多步带文件工具是两种考法
        "toolset": "single-shot-blind" if mode == "blind" else agentsolve.TOOLSETS[tools],
        "budget": {"maxSteps": 1 if mode == "blind" else max_steps,
                   "maxTokens": int(os.environ.get("ALE_LLM_MAX_TOKENS", "16000")),
                   "reasoningEffort": os.environ.get("ALE_LLM_REASONING_EFFORT", "") or None,
                   # 附件单文件读取上限也是「换了就不可比」的一项（§6.4）：材料被截断过
                   # 和材料完整，对考生是两道题。不记下来，两条测量就没法对照。
                   "inputLimit": agentsolve.PAGE_CHARS if (mode == "agent" and tools in ("python", "engine"))
                   else int(os.environ.get("ALE_N7_INPUT_LIMIT", "8000")),
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
        "_faultRetryBudget": fault_retry,
        "_perRun": runs,
        "_errors": errors,
        "_traceDir": trace_base or "(未留轨迹，N7_NO_TRACE=1)",
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
    if write:
        print("回填卡片：%s" % write_card(task, entry)[1], file=sys.stderr)


if __name__ == "__main__":
    main()
