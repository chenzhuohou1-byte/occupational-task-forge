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

N7_RUNS（2026-10-30 起）＝要凑够的**有效跑**数：某槽位补跑 N7_FAULT_RETRY 次仍是测量故障
就放弃、不算有效跑，另开槽位补；放弃数达 N7_MAX_ABANDONED（默认 max(3, N7_RUNS)）就停、
结果标 _partial、退出码 3。续跑（N7_RESUME=1）自动把旧文件里放弃的槽位补齐。
`--write-card` 只在凑够后回填，并按 qc.difficulty_tier 同时回填卡上 tier。
结果 json 里 `_faults` 是每次测量故障的结构化记录，`_noise` 是协议噪声步数合计（只计数、不改解析）。

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
TIERS = ("便宜档", "前沿档")


def tier_hint(tier, full_pass, n_runs):
    """按契约 §6.3 刻度给「本档这次测量指向哪个级别」的即时提示，返回 (提示文本, 指向级别)。

    2026-10-14 口径变更后**难度是标签、不是门槛**：便宜档满分就是合法的 L1，不再退回。
    此前这里是一张写死的门槛表（便宜档 `== 0` 才算"过"、前沿档 `<= 1/3` 才算"过"），
    于是 E1 便宜档 fpr=0.7 被打印成「不过 ✗」——而它其实是正常的 L1、qc 判可进库，纯误导。
    阈值一律引用 qc 的常量，不再在这里复制一份口径；最终 tier 仍由 qc.difficulty_tier
    按卡上 measuredDifficulty 重算，本提示只为跑完当场有个判断。
    """
    if tier == "便宜档":
        if full_pass >= qc.L1_FLOOR:
            return "→ L1（便宜模型就能做出来，前沿档不必跑）", "L1"
        if full_pass > 0:
            return "→ 便宜档 0 < fpr < 2/3：按契约只能给 L2 推定，要定死需补前沿档", "L2(推定)"
        return "→ 便宜档 0 分：级别由前沿档切 L2/L3/L4，去跑前沿档", None
    if full_pass <= qc.L4_CEIL:
        t = "L4"
    elif full_pass <= qc.L3_CEIL:
        t = "L3"
    else:
        t = "L2"
    short = "" if n_runs >= qc.FRONT_MIN_RUNS else (
        "；但有效 run 只有 %d < %d，只能算推定，补足才算实测" % (n_runs, qc.FRONT_MIN_RUNS))
    return "→ %s（ALE 刻度：≤10%% L4 / ≤30%% L3 / >30%% L2）%s" % (t, short), t


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


def _tier_hint_selftest():
    """分级提示对齐契约 §6.3 刻度：便宜档只切 L1，前沿档切 L2/L3/L4，跑数不足标推定。"""
    assert tier_hint("便宜档", 0.7, 10)[1] == "L1", "便宜档 ≥2/3 → L1（E1 就是这档，旧门槛表会误报不过）"
    assert tier_hint("便宜档", 2.0 / 3, 10)[1] == "L1", "正好 2/3 应算 L1"
    assert tier_hint("便宜档", 0.3, 10)[1] == "L2(推定)", "0<fpr<2/3 → L2 推定"
    assert tier_hint("便宜档", 0.0, 10)[1] is None, "便宜档 0 分 → 级别交给前沿档"
    assert tier_hint("前沿档", 0.10, 10)[1] == "L4", "≤10% → L4（H3 就是这档）"
    assert tier_hint("前沿档", 0.20, 10)[1] == "L3", "10–30% → L3"
    assert tier_hint("前沿档", 0.50, 10)[1] == "L2", ">30% → L2"
    assert "推定" in tier_hint("前沿档", 0.10, 3)[0], "前沿档跑数 <10 必须提示只能推定"
    print("[selftest] 分级提示：便宜档切 L1 / 前沿档切 L2L3L4 / 跑数不足标推定", file=sys.stderr)


RESUME_KEYS = ("model", "toolset", "budget", "scorerVersion")


def _load_resume(path, task_id, entry):
    """读 N7_OUT 里的半截结果，返回 (runs, errors, 已完成槽位数, 已花费, 结构化故障)。

    旧文件（2026-10-30 前）没有 `_faults`，返回空表；放弃的槽位数照样能从
    已完成槽位数 − 有效跑数 算出来，续跑会自动把它们补成有效跑。

    口径（model/toolset/budget/scorerVersion）任一项不同就拒绝续跑：拼起来的数字不可比。
    """
    with open(path, encoding="utf-8") as f:
        rep = json.load(f)
    if rep.get("task_id") != task_id:
        sys.exit("N7_RESUME：%s 是任务 %r 的结果，不是 %r" % (path, rep.get("task_id"), task_id))
    old = rep.get("entry") or {}
    diff = [k for k in RESUME_KEYS if old.get(k) != entry.get(k)]
    if diff:
        sys.exit("N7_RESUME：%s 的测量口径与本次不同（%s），不能拼接；换个 N7_OUT 重跑"
                 % (path, "、".join(diff)))
    runs = list(rep.get("_perRun") or [])
    return (runs, list(rep.get("_errors") or []),
            int(rep.get("_slotsDone", len(runs))), float(rep.get("_costRMB") or 0),
            list(rep.get("_faults") or []))


def _resume_selftest():
    """续跑只接同口径的半截结果；口径不同必须拒绝。"""
    import tempfile
    e = {"model": "m", "toolset": "agent-file-python", "budget": {"maxSteps": 20}, "scorerVersion": "1.0"}
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"task_id": "t", "entry": e, "_slotsDone": 3, "_costRMB": 1.5,
                       "_perRun": [{"score": 1.0, "passed": True}] * 2, "_errors": ["x"]}, f)
        runs, errs, done, c, faults = _load_resume(p, "t", dict(e))
        assert (len(runs), len(errs), done, c, faults) == (2, 1, 3, 1.5, []), "续跑应原样接回槽位/逐跑/花费"
        try:
            _load_resume(p, "t", {**e, "budget": {"maxSteps": 40}})
        except SystemExit:
            pass
        else:
            raise AssertionError("预算不同也被续跑了")
    finally:
        os.remove(p)
    print("[selftest] 续跑：同口径接回、口径不同拒绝", file=sys.stderr)


def _scorer_version(task):
    """本条测量该记哪个 scorerVersion，返回 (取用值, 卡上声明值)。

    优先级：`N7_SCORER_VERSION` 显式覆盖 > 任务卡 `evaluation.scorerVersion` > `"1.0"`。

    为什么必须取卡上的值：qc 的难度闸按「条目 sv == 卡上 sv」判断判分器有没有改版，
    不等就把这条测量当旧数字作废、级别降为「推定」。此前这里硬编码默认 `"1.0"`，于是凡
    `evaluation.scorerVersion` 不是字面 `"1.0"` 的题（如 H3 的 `h3-1.0`），一次真金白银的
    测量回填进卡当场被降级，而且只在「级别没变实测」这个弱信号上体现，极易漏掉。
    """
    card_sv = str(((task.card.get("evaluation") or {}).get("scorerVersion") or "")).strip()
    return (os.environ.get("N7_SCORER_VERSION") or card_sv or "1.0"), card_sv


def _scorer_version_selftest():
    """scorerVersion 取值优先级：卡上值 > 兜底 1.0；环境变量可显式覆盖。"""
    class _T:
        def __init__(self, sv):
            self.card = {"evaluation": {"scorerVersion": sv}} if sv is not None else {}

    saved = os.environ.pop("N7_SCORER_VERSION", None)
    try:
        assert _scorer_version(_T("h3-1.0")) == ("h3-1.0", "h3-1.0"), "应取卡上的 h3-1.0"
        assert _scorer_version(_T("  2.0  ")) == ("2.0", "2.0"), "卡上值应 strip"
        assert _scorer_version(_T(None)) == ("1.0", ""), "卡未声明 → 兜底 1.0 且回报空"
        assert _scorer_version(_T("")) == ("1.0", ""), "卡上为空 → 兜底 1.0"
        os.environ["N7_SCORER_VERSION"] = "forced-9"
        assert _scorer_version(_T("h3-1.0"))[0] == "forced-9", "环境变量应显式覆盖卡上值"
    finally:
        os.environ.pop("N7_SCORER_VERSION", None)
        if saved is not None:
            os.environ["N7_SCORER_VERSION"] = saved
    print("[selftest] scorerVersion：取卡上值 / 兜底 1.0 / 环境变量可覆盖", file=sys.stderr)


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
    """把一条测量 entry 追加进 task_card.measuredDifficulty（§6.2：每次测量追加一条），
    并按卡上全部测量重算 `tier` 写回（契约 §3：tier 由 measuredDifficulty 机器派生）。

    只追加、不改旧条目；完全相同的 entry 已在卡上就不再追加（同一结果文件重复回填），
    但 tier 与重算结果不一致时照样刷新。nRuns=0（全是测量故障）不写：那不是一次测量。
    tier 用的就是 qc 的 `difficulty_tier`——qc 每次照旧按 measuredDifficulty 重算，卡上的值
    只作展示与统计，这里不另写一份定级逻辑。定不出级时写 null。
    返回 (卡片是否被改写, 说明)。
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
    # 回填时再把 scorerVersion 对一遍：`--write-card --from <旧结果文件>` 会原样replay
    # 文件里存的 entry，而旧文件可能是修复前跑的（硬编码 "1.0"）。不同就**响亮地报**，
    # 不静默改——真有可能是判分器确实改版了，那该重测而不是替作者改数字。
    card_sv = str(((card.get("evaluation") or {}).get("scorerVersion") or "")).strip()
    ent_sv = str(entry.get("scorerVersion") or "").strip()
    if card_sv and ent_sv and ent_sv != card_sv:
        print("⚠️  本条测量 scorerVersion=%r 与卡上 %r 不一致：qc 会把它当「判分器已改版、"
              "旧数字作废」而降为推定。要么重测，要么确认判分器未变后用 "
              "N7_SCORER_VERSION=%s 重跑回填。" % (ent_sv, card_sv, card_sv), file=sys.stderr)
    appended = entry not in md
    if appended:
        md.append(entry)
    task.card = card
    ok, d = qc.difficulty_tier(task)
    tier = d.get("tier") if ok else None
    if not appended and card.get("tier") == tier:
        return False, "完全相同的条目已在卡上、tier 已是 %s，跳过" % tier
    card["tier"] = tier
    with open(path, "w", encoding="utf-8") as f:   # 保持原文件是否带结尾换行，减少无关 diff
        f.write(json.dumps(card, ensure_ascii=False, indent=2) + ("\n" if raw.endswith("\n") else ""))
    return True, "%s；tier=%s%s" % (
        "已追加到 %s（第 %d 条）" % (path, len(md)) if appended else "条目已在卡上，只刷新 tier",
        tier if ok else "定不出（写 null）", "（%s）" % d.get("级别来源") if ok else "")


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
        c = tasks.load_task(d).card
        md = c["measuredDifficulty"]
        assert len(md) == n0 + 1 and md[-1] == e, "卡上应恰好多一条且内容一致"
        ok, dd = qc.difficulty_tier(tasks.load_task(d))
        assert "tier" in c and c["tier"] == (dd.get("tier") if ok else None), \
            "卡上 tier 应等于 qc.difficulty_tier 的结果：%r vs %r" % (c.get("tier"), dd.get("tier"))

        # tier 跟着测量走：补一条前沿档 10 跑 2/10 → qc 定 L3，卡上同步成 L3
        sv = str((c.get("evaluation") or {}).get("scorerVersion") or "1.0")
        front = {"model": "Claude Sonnet 5.5", "toolset": "agent-file-python",
                 "budget": {"maxSteps": 30}, "scorerVersion": sv, "nRuns": 10,
                 "fullPassRate": 0.2, "meanScore": 0.6, "measuredAt": "2026-10-30"}
        assert write_card(t, front)[0]
        c = tasks.load_task(d).card
        assert c["tier"] == "L3" == qc.difficulty_tier(tasks.load_task(d))[1]["tier"], c.get("tier")
        # 卡上 tier 被手改过：重复回填同一条也要刷回派生值
        c["tier"] = "L1"
        with open(os.path.join(d, "task_card.json"), "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=2)
        t = tasks.load_task(d)
        assert write_card(t, dict(front))[0], "tier 与派生值不符时应刷新"
        c = tasks.load_task(d).card
        assert c["tier"] == "L3" and len(c["measuredDifficulty"]) == n0 + 2, c.get("tier")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert open(os.path.join(task.dir, "task_card.json"), encoding="utf-8").read() == orig, "原卡被改了"
    print("[selftest] write_card：追加/去重/拒写 nRuns=0、tier 按 qc.difficulty_tier 回填与刷新，原卡未动",
          file=sys.stderr)


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


def collect_runs(one, n_runs, st, fault_retry, max_abandoned, flush=None, before_call=None,
                 log=None):
    """凑够 `n_runs` 个**有效跑**为止（2026-10-30 起的 N7_RUNS 语义）。

    每个槽位先跑一次，是测量故障（finish_reason=length / 504 / 断流…）就补跑，最多
    `fault_retry` 次；补跑用尽仍是故障 → 该槽位**放弃、不算有效跑**，再开一个新槽位补上。
    原来放弃的槽位也占一个 N7_RUNS 名额，于是「N7_RUNS=10」常常只测出 9 跑，而前沿档要
    满 10 跑才算实测，只能手工把 N7_RUNS 调成 11 再续跑。

    `st` 是可续跑的状态（原地修改）：runs / errors / faults / slots_done。放弃的槽位数
    = slots_done − len(runs)，达到 `max_abandoned` 就停（多半是网关或题本身出了系统性问题，
    再跑只是烧钱），这时有效跑不足、结果标 _partial，调用方不许回填卡片。
    `one(tag)` 跑一次并返回逐跑结果；抛异常即测量故障（异常带 .info 就一并记下）。
    返回是否凑够。
    """
    log = log or (lambda m: print(m, file=sys.stderr))
    while len(st["runs"]) < n_runs:
        abandoned = st["slots_done"] - len(st["runs"])
        if abandoned >= max_abandoned:
            log("  ✗ 已放弃 %d 个槽位（上限 N7_MAX_ABANDONED=%d），停止：只凑到 %d/%d 个有效跑"
                % (abandoned, max_abandoned, len(st["runs"]), n_runs))
            return False
        slot = st["slots_done"] + 1
        label = "槽位%d（有效 %d/%d）" % (slot, len(st["runs"]), n_runs)
        got = False
        for attempt in range(fault_retry + 1):
            tag = "run%d" % slot if attempt == 0 else "run%d_retry%d" % (slot, attempt)
            if before_call:
                before_call()
            try:
                r = one(tag)
            except Exception as e:   # noqa: BLE001 网关 504 / 断流 / finish_reason=length 等 = 测量故障
                st["errors"].append("%s: %s: %s" % (tag, type(e).__name__, e))
                st["faults"].append({"slot": tag, "error": "%s: %s" % (type(e).__name__, e),
                                     **(getattr(e, "info", None) or {})})
                log("  %s%s  ✗ 测量故障：%s%s" % (label, "" if attempt == 0 else "(补跑%d)" % attempt, e,
                                                  "，补跑…" if attempt < fault_retry else "，补跑预算用尽"))
                continue
            r = dict(r, slot=tag)
            st["runs"].append(r)
            log("  %s%s  score=%.4f passed=%s 写出文件=%s%s"
                % (label, "" if attempt == 0 else "(补跑%d)" % attempt, r["score"], r["passed"],
                   r.get("写出文件数"),
                   "  步数=%s 协议噪声步=%s 工具标记步=%s" % (r.get("步数"), r.get("协议噪声步"),
                                                              r.get("工具标记步"))
                   if "步数" in r else ""))
            got = True
            break
        if not got:
            log("  %s  ✗ 补跑 %d 次仍全是测量故障，放弃（不算有效跑，另开槽位补）" % (label, fault_retry))
        st["slots_done"] = slot
        if flush:
            flush()
    return True


def noise_summary(runs, faults):
    """协议噪声计数（只计数、不改解析）：有效跑与故障跑分开合计，逐跑数字在 _perRun / _faults 里。"""
    def tot(rows):
        return {k: sum(int(x.get(k) or 0) for x in rows) for k in ("步数", "协议噪声步", "工具标记步")}
    return {"有效跑": tot(runs), "故障跑": tot(faults)}


def complete_report(rep):
    """结果文件是不是一次凑够了的测量（`--write-card` 只回填这种）。"""
    n = (rep.get("entry") or {}).get("nRuns") or 0
    return not rep.get("_partial") and n >= int(rep.get("_runsRequested") or 0) and n > 0


def _collect_selftest():
    """凑够 N 个有效跑：放弃的槽位另开补上、续跑接着补、放弃到上限就停且不算凑够。"""
    class Fault(RuntimeError):
        def __init__(self, m):
            super().__init__(m)
            self.info = {"步数": 7, "协议噪声步": 2, "工具标记步": 3, "故障步": 7}

    def scripted(outcomes):
        it = iter(outcomes)

        def one(tag):
            if next(it) == "F":
                raise Fault("第 7 步无有效产出")
            return {"score": 1.0, "passed": True, "写出文件数": 1, "步数": 5,
                    "协议噪声步": 1, "工具标记步": 1}
        return one

    quiet = lambda m: None  # noqa: E731
    # 槽位 2 连挂 2 次（fault_retry=1 → 放弃），另开槽位 4 补上：3 个有效跑、4 个槽位
    st = {"runs": [], "errors": [], "faults": [], "slots_done": 0}
    assert collect_runs(scripted("OFFOO"), 3, st, 1, 3, log=quiet)
    assert [r["slot"] for r in st["runs"]] == ["run1", "run3", "run4"], st["runs"]
    assert st["slots_done"] == 4 and len(st["errors"]) == 2, st
    assert st["faults"][0]["slot"] == "run2" and st["faults"][0]["故障步"] == 7, st["faults"]
    ns = noise_summary(st["runs"], st["faults"])
    assert ns == {"有效跑": {"步数": 15, "协议噪声步": 3, "工具标记步": 3},
                  "故障跑": {"步数": 14, "协议噪声步": 4, "工具标记步": 6}}, ns
    # 续跑：旧文件已有 1 个有效跑、2 个槽位（1 个放弃）→ 再补 2 个有效跑
    st = {"runs": [{"score": 1.0, "passed": True}], "errors": ["x"], "faults": [], "slots_done": 2}
    assert collect_runs(scripted("OO"), 3, st, 0, 3, log=quiet)
    assert len(st["runs"]) == 3 and st["slots_done"] == 4 and st["runs"][-1]["slot"] == "run4"
    # 已凑够的旧文件续跑：一跑都不发
    assert collect_runs(scripted(""), 3, st, 0, 3, log=quiet) and st["slots_done"] == 4
    # 全是故障：放弃到上限就停，不算凑够
    st = {"runs": [], "errors": [], "faults": [], "slots_done": 0}
    assert not collect_runs(scripted("F" * 99), 10, st, 2, 2, log=quiet)
    assert st["slots_done"] == 2 and len(st["errors"]) == 6 and not st["runs"]
    # 回填闸：半截 / 有效跑不足 都不回填
    assert complete_report({"entry": {"nRuns": 10}, "_partial": False, "_runsRequested": 10})
    assert not complete_report({"entry": {"nRuns": 9}, "_partial": False, "_runsRequested": 10}), \
        "旧语义下放弃一个槽位的结果（9/10）不许回填"
    assert not complete_report({"entry": {"nRuns": 10}, "_partial": True, "_runsRequested": 10})
    assert not complete_report({"entry": {"nRuns": 0}, "_partial": False, "_runsRequested": 0})
    print("[selftest] 凑够有效跑：放弃槽位另开补、续跑接着补、放弃到上限即停、半截不回填", file=sys.stderr)


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
        entry = dict(rep["entry"])
        # 旧结果文件可能是 scorerVersion 修复前跑的（硬编码 "1.0"）。允许用
        # N7_SCORER_VERSION 显式重盖一次——必须人为确认"判分器真没改过"才该这么做，
        # 所以只认显式环境变量，绝不自动按卡上值悄悄改写别人的测量数字。
        forced = os.environ.get("N7_SCORER_VERSION")
        if forced and str(entry.get("scorerVersion") or "") != forced:
            print("· 按 N7_SCORER_VERSION 把本条 scorerVersion 由 %r 重盖为 %r"
                  % (entry.get("scorerVersion"), forced), file=sys.stderr)
            entry["scorerVersion"] = forced
        if not complete_report(rep):
            sys.exit("拒绝回填：%s 不是凑够了的测量（有效跑 %s / 请求 %s，_partial=%s）。"
                     "用同一个 N7_OUT 加 N7_RESUME=1 续跑补齐后再回填"
                     % (sys.argv[i + 1], entry.get("nRuns"), rep.get("_runsRequested"), rep.get("_partial")))
        print(write_card(resolve_task(cfg, rep["task_id"]), entry)[1], file=sys.stderr)
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
        _scorer_version_selftest()
        _tier_hint_selftest()
        _resume_selftest()
        _collect_selftest()
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

    out = os.environ.get("N7_OUT")
    runs, errors, faults = [], [], []
    gap = float(os.environ.get("ALE_LLM_INTER_RUN_SLEEP", "0"))
    fault_retry = int(os.environ.get("N7_FAULT_RETRY", "2"))   # 每个槽位遇测量故障最多补跑几次
    # N7_RUNS 是要凑够的**有效跑**数；放弃的槽位另开补，最多放弃这么多个就停（防系统性故障烧钱）
    max_abandoned = int(os.environ.get("N7_MAX_ABANDONED", str(max(3, n_runs))))
    trace_base = _trace_base(cfg, task, mode)

    scorer_version, card_sv = _scorer_version(task)
    if not card_sv:
        print("⚠️  任务卡未声明 evaluation.scorerVersion，本条测量记为 %r；qc 的难度闸要求"
              "两者相等，建议先把卡补上再测" % scorer_version, file=sys.stderr)

    # 判分契约 §6.2 形状：model/toolset/budget/scorerVersion/nRuns 必须钉住
    def _entry():
        n_ok = len(runs)
        scores = [r["score"] for r in runs]
        return {
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
                        "wallClockMin": None,
                        # 考生 run_python 的环境（解释器+可调软件）。None = 产线默认解释器、无外部软件
                        "runtime": agentsolve.runtime()[3]},
            "scorerVersion": scorer_version,
            "nRuns": n_ok,
            "fullPassRate": round(sum(1 for r in runs if r["passed"]) / n_ok, 4) if n_ok else None,
            "meanScore": round(sum(scores) / n_ok, 4) if n_ok else None,
            "measuredAt": datetime.date.today().isoformat(),
        }

    # 断点续跑（N7_RESUME=1）：N7_OUT 里已有同一口径的逐跑结果就接着跑，不重花钱。
    # 每跑完一个槽位都落盘一次（_partial=true），进程被杀也只丢正在跑的那一次。
    slots_done, prev_cost = 0, 0.0
    if os.environ.get("N7_RESUME") == "1" and out and os.path.isfile(out):
        runs, errors, slots_done, prev_cost, faults = _load_resume(out, task.task_id, _entry())
        print("· 续跑：%s 里已有 %d 个槽位（有效 %d 跑、放弃 %d 个），从第 %d 个槽位接着补到 %d 个有效跑"
              % (out, slots_done, len(runs), slots_done - len(runs), slots_done + 1, n_runs),
              file=sys.stderr)
    st = {"runs": runs, "errors": errors, "faults": faults, "slots_done": slots_done}

    def _report(partial):
        rep = {
            "task_id": task.task_id,
            "entry": _entry(),
            "_partial": partial,
            "_slotsDone": st["slots_done"],
            "_runsRequested": n_runs,
            "_abandonedSlots": st["slots_done"] - len(runs),
            "_maxAbandoned": max_abandoned,
            "_faultRetryBudget": fault_retry,
            "_perRun": runs,
            "_errors": errors,
            # 每次测量故障的结构化记录（槽位、走到第几步、噪声计数、留证文件名）
            "_faults": faults,
            # 协议噪声只计数、不改解析（改了新旧测量不可比）
            "_noise": noise_summary(runs, faults),
            "_traceDir": trace_base or "(未留轨迹，N7_NO_TRACE=1)",
            "_costRMB": round(prev_cost + cost.total(), 6),
            "_unpricedModels": cost.unpriced_models(),
        }
        return rep

    def _flush(partial, rep=None):
        if not out:
            return
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(rep or _report(partial), ensure_ascii=False, indent=2))
        os.replace(tmp, out)

    def _one(tag):
        td = os.path.join(trace_base, tag) if trace_base else None
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

    # 补跑不改 maxSteps/maxTokens，口径不变；每个槽位跑完都落盘（_partial=true 直到凑够）
    done = collect_runs(_one, n_runs, st, fault_retry, max_abandoned,
                        flush=lambda: _flush(partial=len(runs) < n_runs), before_call=_sleep_between)

    report = _report(partial=not done)
    entry = report["entry"]
    n_ok, full_pass, mean_score = entry["nRuns"], entry["fullPassRate"], entry["meanScore"]

    # 门槛判定（§6.3）—— 只在给了 N7_TIER 时判
    if tier in TIERS and full_pass is not None:
        hint, points_to = tier_hint(tier, full_pass, n_ok)
        report["_tierHint"] = {"tier": tier, "fullPassRate": full_pass,
                               "nRuns": n_ok, "指向": points_to}
        print("\n分级提示[%s] fullPassRate=%.4f（%d 跑）%s"
              "\n（难度是标签不是门槛；最终 tier 由 qc 按卡上 measuredDifficulty 重算）"
              % (tier, full_pass, n_ok, hint), file=sys.stderr)
    elif tier and tier not in TIERS:
        print("⚠️  N7_TIER=%r 不认识，只认 便宜档/前沿档" % tier, file=sys.stderr)

    if out:
        _flush(False, report)
        print("\n已写入 %s" % out, file=sys.stderr)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    print("\nnRuns=%d(请求%d) fullPassRate=%s meanScore=%s 成本RMB=%s%s%s"
          % (n_ok, n_runs, full_pass, mean_score, report["_costRMB"],
             "  ⚠️有%d次测量故障" % len(errors) if errors else "",
             "（放弃 %d 个槽位，已另开补齐）" % report["_abandonedSlots"]
             if report["_abandonedSlots"] and done else ""), file=sys.stderr)
    if not done:
        print("⚠️  只凑到 %d/%d 个有效跑（放弃槽位达上限 %d），结果标 _partial；"
              "查清故障后用同一个 N7_OUT 加 N7_RESUME=1 续跑补齐" % (n_ok, n_runs, max_abandoned),
              file=sys.stderr)
    if write:
        if complete_report(report):
            print("回填卡片：%s" % write_card(task, entry)[1], file=sys.stderr)
        else:
            print("回填卡片：跳过——有效跑 %d < 请求 %d，不写半截测量" % (n_ok, n_runs), file=sys.stderr)
    if not done:
        sys.exit(3)


if __name__ == "__main__":
    main()
