"""casegen 生成半段：职业 → 模型产闭世界案例 → crosscheck 把关 → 物化 → qc。

编排与「谁来产文件」解耦（producer）：
  StaticProducer  从现成案例拷文件，不调模型 —— CI 用，验证整条接线；
  LLMProducer     真调模型：solve 侧（规则/生成器/solve/断言/物化）用一个模型，
                  solve2 用**另一个模型**、上下文**只有 规则.md**（见 solve2_messages）。

每轮：冒烟门 → crosscheck → 物化 → qc(N6/N8)；任一不过就把问题喂回 solve 侧模型返修，
规则改了就重产 solve2；最多 max_rounds 轮，仍不过 → CaseRejected（CLI 退出码 1，绝不先放行）。
不是案例的错的失败单独归类、不浪费返修：网关故障 → InfraError，判分器/地板合成器的问题 →
FrameworkError（CLI 退出码都是 3）。--resume 复用已产出的文件续跑，不重复花钱。
每次模型调用的 prompt / 原始响应落盘到 <out>/trace/；进度打到 stderr。
"""
import dataclasses
import inspect
import json
import os
import re
import shutil
import sys
import time
import traceback

from . import casegen, crosscheck, qc, scaffold, tasks

_HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE_CASE = os.path.join(_HERE, "crosscheck_cases", "good")
CODE_FILES = ("参数生成器.py", "solve.py", "断言.py", "物化.py")
# 文件块用显式标记而非 ``` 围栏：规则.md 里本身就有 ``` 代码块，套围栏会被截断
_FILE_RE = re.compile(r"<<<FILE\s+([^>\n]+?)\s*>>>\n(.*?)<<<END>>>", re.S)
_FENCE_RE = re.compile(r"^\s*```[^\n]*\n(.*?)\n?```\s*$", re.S)


class CaseRejected(Exception):
    """返修预算用完（或 producer 不能返修）仍不过 → 作废。args[0] 是报告 dict。"""


class InfraError(Exception):
    """网关/网络故障（429、超时、5xx…）：不是案例的错，不记作废、不消耗返修。args[0] 是报告 dict。"""


class FrameworkError(Exception):
    """判分器或地板合成器的问题（如地板在某些字段白拿分）：案例返修修不好，直接停下报出。"""


def _stderr_log(msg):
    print("[casegen %s] %s" % (time.strftime("%H:%M:%S"), msg), file=sys.stderr, flush=True)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _fblock(name, text):
    return "<<<FILE %s>>>\n%s%s<<<END>>>\n" % (name, text, "" if text.endswith("\n") else "\n")


def parse_files(text):
    """解析多文件响应；模型爱在块里再套一层 ``` 围栏，整块被包住时剥掉。"""
    out = {}
    for name, body in _FILE_RE.findall(text or ""):
        m = _FENCE_RE.match(body)
        out[name.strip()] = (m.group(1) if m else body).rstrip() + "\n"
    return out


def _write_files(case_dir, files, allow):
    done = []
    for name, body in files.items():
        if name in allow:
            with open(os.path.join(case_dir, name), "w", encoding="utf-8") as f:
                f.write(body)
            done.append(name)
    return done


# ---------------- 冒烟门 ----------------
def smoke(case_dir):
    """生成器出得了世界、solve 算得动、物化渲染得出来。返回 None 或错误文本（喂回返修）。"""
    try:
        gen = casegen._load(os.path.join(case_dir, "参数生成器.py"), "_cb_gen")
        s1 = casegen._load(os.path.join(case_dir, "solve.py"), "_cb_s1")
        ws = gen.worlds(20, 0)
        if not isinstance(ws, list) or not ws or not all(isinstance(w, dict) for w in ws):
            return "参数生成器.worlds(20, 0) 没返回非空的 dict 列表"
        for w in ws:
            if not isinstance(s1.solve(dict(w)), dict):
                return "solve.solve 返回的不是 dict"
        mp = os.path.join(case_dir, "物化.py")
        if os.path.isfile(mp):
            m = casegen._load(mp, "_cb_mat")
            miss = [k for k in ("领域", "任务名", "task_id", "prompt", "交付要求", "scoringBasis")
                    if k not in getattr(m, "TASK", {})]
            if miss:
                return "物化.TASK 缺字段：%s" % miss
            w = getattr(m, "INSTANCE", None) or ws[0]
            for fn, got in (("render_inputs", m.render_inputs(dict(w))),
                            ("deliver", m.deliver(s1.solve(dict(w))))):
                if not isinstance(got, dict) or not all(isinstance(v, str) for v in got.values()):
                    return "物化.%s 应返回 {文件名: 文本}" % fn
    except Exception:  # noqa: BLE001 冒烟就是要把任何异常变成返修线索
        return traceback.format_exc(limit=6)
    return None


# ---------------- 难度门（结构性下限） ----------------
@dataclasses.dataclass(frozen=True)
class Hardness:
    """结构性难度下限。不是难度本身（难度只认 N7 agent 档实测），而是把「一眼心算完」的软题
    在花 crosscheck/物化/N7 之前就挡回返修。首个自动案例（4 条标准 × 6 行、两份材料并成一个
    文件）便宜档 agent 3/3 满分，就是缺这道门。"""
    min_files: int = 3          # render_inputs 产出的独立材料份数（不含 核算规则.md）
    min_main_rows: int = 40     # 记录数最多那份材料（业务明细）的下限
    min_ref_rows: int = 15      # 记录数第二多那份材料（标准/参照表）的下限
    min_traps: int = 3          # 物化.TRAPS 至少几类陷阱
    trap_ratio: float = 0.10    # 随机世界（boundary=False）里每类陷阱至少出现在这么多比例的世界
    sample: int = 50            # 统计 trap_ratio 用的随机世界数


DEFAULT_HARDNESS = Hardness()
RELAXED = Hardness(min_files=0, min_main_rows=0, min_ref_rows=0, min_traps=0, trap_ratio=0.0)


def _records(name, text):
    """一份附件的记录数：CSV 数据行数；JSON 顶层数组长度，或顶层对象里最长的数组/对象。"""
    if name.endswith(".csv"):
        return max(0, len([ln for ln in text.splitlines() if ln.strip()]) - 1)
    if name.endswith(".json"):
        try:
            obj = json.loads(text)
        except ValueError:
            return 0
        if isinstance(obj, list):
            return len(obj)
        if isinstance(obj, dict):
            return max([len(v) for v in obj.values() if isinstance(v, (list, dict))] or [len(obj)])
    return 0


def hardness_check(case_dir, spec=DEFAULT_HARDNESS, seed=0):
    """返回不达标项列表（[] 为达标）。只看 INSTANCE 的渲染结果与 TRAPS，不调模型。"""
    import copy
    if not any(dataclasses.astuple(spec)[:5]):
        return []                     # RELAXED：不设门（自测里给小范例案例用）
    m = casegen._load(os.path.join(case_dir, "物化.py"), "_cb_hard")
    gen = casegen._load(os.path.join(case_dir, "参数生成器.py"), "_cb_hard_gen")
    world = getattr(m, "INSTANCE", None) or gen.worlds(1, seed)[0]
    files = m.render_inputs(copy.deepcopy(world))
    probs = []
    if len(files) < spec.min_files:
        probs.append("render_inputs 只产出 %d 份材料 %s，至少要 %d 份**独立**附件（每份材料单独一个文件，"
                     "不得把标准表和明细并进一个文件）" % (len(files), sorted(files), spec.min_files))
    sizes = sorted(((_records(n, t), n) for n, t in files.items()), reverse=True)
    main = sizes[0] if sizes else (0, "（无）")
    ref = sizes[1] if len(sizes) > 1 else (0, "（无）")
    if main[0] < spec.min_main_rows:
        probs.append("INSTANCE 规模不足：最大那份材料 %s 只有 %d 条记录，至少 %d 条"
                     % (main[1], main[0], spec.min_main_rows))
    if ref[0] < spec.min_ref_rows:
        probs.append("INSTANCE 规模不足：第二大那份材料 %s 只有 %d 条记录，至少 %d 条"
                     % (ref[1], ref[0], spec.min_ref_rows))
    traps = getattr(m, "TRAPS", None) or {}
    if not isinstance(traps, dict) or len(traps) < spec.min_traps:
        probs.append("物化.TRAPS 只有 %d 类陷阱，至少 %d 类（{陷阱名: world → 出现次数}，与规则一一对应）"
                     % (len(traps) if isinstance(traps, dict) else 0, spec.min_traps))
        return probs
    if spec.min_traps == 0 and spec.trap_ratio <= 0:
        return probs
    rand = gen.worlds(spec.sample, seed, boundary=False) if spec.trap_ratio > 0 else []
    for name, fn in traps.items():
        if not fn(copy.deepcopy(world)):
            probs.append("INSTANCE 里没出现陷阱「%s」（交给被测 agent 的实例必须覆盖每类陷阱）" % name)
        if rand:
            hit = sum(1 for w in rand if fn(copy.deepcopy(w))) / len(rand)
            if hit < spec.trap_ratio:
                probs.append("随机世界里陷阱「%s」只出现在 %.0f%% 的世界，至少 %.0f%%（参数生成器随机段也要按"
                             "概率注入每类陷阱，否则 crosscheck 只在边界世界碰得到它）"
                             % (name, hit * 100, spec.trap_ratio * 100))
    return probs


# ---------------- 一轮校验 + 编排 ----------------
def _check_round(case_dir, out_dir, rnd, n, seed, hardness=DEFAULT_HARDNESS, occupation=None):
    """返回 (stage, problem, info)；problem 为 None 表示本轮全过。"""
    err = smoke(case_dir)
    if err:
        return "smoke", "冒烟失败：\n" + err, None
    try:
        probs = hardness_check(case_dir, hardness, seed)
    except Exception:  # noqa: BLE001 TRAPS/渲染抛错也是返修线索
        probs = ["难度门检查出错：\n" + traceback.format_exc(limit=6)]
    if probs:
        return "hardness", "结构性难度不达标（题太软，便宜模型会一眼做完）：\n- " + "\n- ".join(probs), None
    try:
        rep = crosscheck.crosscheck(case_dir, n=n, seed=seed)
    except crosscheck.CaseError as e:
        return "crosscheck", "crosscheck 拒收：%s" % e, None
    if not rep["一致"]:
        return "crosscheck", "两份独立实现对不上，或一致性断言被破坏：\n" + json.dumps(
            {k: rep[k] for k in ("不一致数", "断言破坏数", "第一个反例", "第一个断言破坏")},
            ensure_ascii=False, indent=1, default=str)[:4000], None
    try:
        mat = casegen.materialize(case_dir, os.path.join(out_dir, "tasks_r%d" % rnd),
                                  occupation=occupation)
    except casegen.FixtureMismatch as e:
        # 负例/地板不符 → 判分器或地板合成器的锅（框架侧），返修案例没用；正例/部分分不符 → 案例侧
        return ("framework" if e.framework_side else "materialize"), "%s\n逐字段线索：\n%s" % (
            e, json.dumps(e.info, ensure_ascii=False, indent=1, default=str)[:3500]), None
    except Exception:  # noqa: BLE001 物化失败（渲染出错等）也是返修线索
        return "materialize", "物化失败：\n" + traceback.format_exc(limit=6), None
    t = tasks.load_task(mat["dir"])
    n6_ok, n6 = qc.n6_verify(t)
    n8_ok, n8 = qc.n8_floor_audit(t)
    if not (n6_ok and n8_ok):
        # N6 不过（泄答案/依据追不到附件…）是案例侧；只有 N8 地板不过 → 框架侧
        stage = "qc" if not n6_ok else "framework"
        return stage, "qc 不过：\n" + json.dumps(
            {"N6不过项": [c for c in n6["checks"] if not c["通过"]], "N8": n8},
            ensure_ascii=False, indent=1, default=str)[:4000], None
    return "pass", None, {"task_dir": mat["dir"], "fixtureScores": mat["fixtureScores"],
                          "crosscheck世界数": rep["世界数"], "N7": n7_commands(mat["dir"])}


def n7_commands(task_dir):
    """接收后下一步：N7 agent 档实测命令（直接可粘贴，路径全绝对、无行内注释）。

    maxSteps 按契约 §6.3：附件份数 + 交付文件数 + 50% 余量，最低 20。先跑便宜档；
    便宜档 fullPassRate 必须 = 0，否则直接退役重产、不必再花钱跑前沿档。
    """
    import math
    import shlex
    from .config import PipelineConfig
    card = json.load(open(os.path.join(task_dir, "task_card.json"), encoding="utf-8"))
    indir = os.path.join(task_dir, "assets", "input")
    n_in = len([f for f in os.listdir(indir) if os.path.isfile(os.path.join(indir, f))])
    n_out = len(card.get("交付要求") or {})
    steps = max(20, int(math.ceil((n_in + n_out) * 1.5)))
    sv = str((card.get("evaluation") or {}).get("scorerVersion") or "1.0")
    repo = os.path.dirname(_HERE)
    n7dir = os.path.join(os.path.abspath(PipelineConfig().runs_dir), "n7")

    def cmd(model, tier, tag):
        return ("cd %s && ALE_LLM_MODEL=%s N7_MODE=agent N7_RUNS=3 N7_MAX_STEPS=%d N7_TIER=%s "
                "N7_SCORER_VERSION=%s N7_TASK=%s N7_OUT=%s python3 n7_run.py") % (
            shlex.quote(repo), model, steps, tier, sv, shlex.quote(os.path.abspath(task_dir)),
            shlex.quote(os.path.join(n7dir, "%s-agent-%s.json" % (card["task_id"], tag))))
    return {"task_id": card["task_id"], "maxSteps": steps, "附件数": n_in, "交付数": n_out,
            "便宜档": cmd("glm-5.3-flash", "便宜档", "glm"),
            "前沿档": cmd("gpt-5.5", "前沿档", "gpt55"),
            "说明": "先跑便宜档：fullPassRate>0 → 太软，退役重产（§6.4），不跑前沿档；"
                  "=0 再跑前沿档，要 ≤1/3；0<fpr≤1/3 补到 6 跑。"}


def build_case(occupation, out_dir, producer, max_rounds=3, n=200, seed=0, resume=False, log=None,
               hardness=DEFAULT_HARDNESS):
    log = log or (lambda m: None)
    case_dir = os.path.join(out_dir, "case")
    os.makedirs(case_dir, exist_ok=True)
    report = {"occupation": occupation, "out": out_dir, "case_dir": case_dir,
              "resumed": bool(resume), "难度门": dataclasses.asdict(hardness), "rounds": []}

    def have(*names):
        return resume and all(os.path.isfile(os.path.join(case_dir, x)) for x in names)

    t0 = time.time()
    try:
        if have("规则.md"):
            log("复用已有 规则.md（不调模型）")
        else:
            log("① 写规则（solve 侧模型）…")
            producer.draft_rules(case_dir, occupation)
        rules = _read(os.path.join(case_dir, "规则.md"))
        if have(*CODE_FILES):
            log("复用已有 参数生成器/solve/断言/物化")
        else:
            log("② 写代码（solve 侧模型）…")
            producer.write_code(case_dir, rules)
        if have("solve2.py"):
            log("复用已有 solve2.py")
        else:
            log("③ 独立实现 solve2（另一模型，只给规则原文）…")
            producer.write_solve2(case_dir, rules)          # 只交规则原文——独立性的编排层保证
        for rnd in range(1, max_rounds + 1):
            log("第 %d/%d 轮校验：冒烟 → 难度门 → crosscheck(%d 世界) → 物化 → qc …" % (rnd, max_rounds, n))
            stage, problem, info = _check_round(case_dir, out_dir, rnd, n, seed, hardness, occupation)
            report["rounds"].append({"round": rnd, "stage": stage,
                                     "problem": problem[:1500] if problem else None})
            if problem is None:
                report.update(status="accepted", 耗时秒=round(time.time() - t0), **info)
                log("✓ 第 %d 轮全过，案例接收（%.1f min）" % (rnd, (time.time() - t0) / 60))
                return report
            log("✗ 第 %d 轮停在 %s" % (rnd, stage))
            if stage == "framework":
                report.update(status="framework_error", 耗时秒=round(time.time() - t0),
                              note="判分器/地板合成器侧的问题，返修案例修不好，已停止返修")
                raise FrameworkError(report)
            if rnd == max_rounds:
                break
            log("返修（solve 侧模型）…")
            changed = producer.repair(case_dir, problem)
            if changed is None:
                report["note"] = "producer 不支持返修，直接作废"
                break
            report["rounds"][-1]["changed"] = sorted(changed)
            if "规则.md" in changed or not changed:     # 规则变了（或没改出东西）→ 重产 solve2
                log("规则有改动 → 重产 solve2 …")
                producer.write_solve2(case_dir, _read(os.path.join(case_dir, "规则.md")))
    except InfraError as e:
        report.update(status="infra_error", 错误=str(e), 耗时秒=round(time.time() - t0))
        raise InfraError(report)
    report.update(status="rejected", 耗时秒=round(time.time() - t0))
    raise CaseRejected(report)


# ---------------- producers ----------------
class StaticProducer:
    """CI 用：从现成案例目录拷文件，不调模型、不能返修。记下 solve2 拿到的输入，供独立性自测核对。"""

    def __init__(self, src_dir):
        self.src = src_dir
        self.solve2_inputs = []

    def _copy(self, case_dir, names):
        for nm in names:
            p = os.path.join(self.src, nm)
            if os.path.isfile(p):
                shutil.copy(p, os.path.join(case_dir, nm))

    def draft_rules(self, case_dir, occupation):
        self._copy(case_dir, ["规则.md"])

    def write_code(self, case_dir, rules_text):
        self._copy(case_dir, CODE_FILES)

    def write_solve2(self, case_dir, rules_text):
        self.solve2_inputs.append(rules_text)
        self._copy(case_dir, ["solve2.py"])

    def repair(self, case_dir, problem):
        return None


SOLVE_SYSTEM = ("你是严谨的评测题作者兼 Python 工程师，为 ALE 式职业评测造闭世界任务。"
                "只按 <<<FILE 文件名>>> … <<<END>>> 格式输出文件，不要输出任何其它文字。")


def _ex(name):
    return _read(os.path.join(EXAMPLE_CASE, name))


def rules_messages(occupation):
    h = DEFAULT_HARDNESS
    return [{"role": "system", "content": SOLVE_SYSTEM}, {"role": "user", "content": (
        "【步骤:规则】为职业「%s」设计一个 numeric_tabular 范式的闭世界核算任务，写出 规则.md。硬要求：\n"
        "1. 至少 %d 份**独立材料**（如 标准/限值表 + 业务明细 + 登记/委托/台账），每份材料将来单独成一个附件文件，"
        "不得合并成一个文件；计算必须跨材料按关联键 join；\n"
        "2. 规模：业务明细不少于 %d 行、标准/参照表不少于 %d 条（数据由参数生成器产出，规则里只写结构）；\n"
        "3. 「## 材料」一节给出世界参数的精确 JSON 结构与字段名（出题方与独立复核员都只能据此取数），"
        "并写明每份材料对应哪个附件文件；\n"
        "4. 全部费率/阈值/封顶/判定口径写进规则，答案由规则唯一确定，不引用外部法规或常识；\n"
        "5. 「## 特殊情形处理」一节至少 %d 类特殊情形，并写清处理语义，从这些杠杆里选（越多越好）："
        "跨材料关联键写法不一致需规范化（前导零/大小写/全半角/首尾空格，规则写明规范化方法）、单位混用需换算、"
        "中间扣除或校正、需要自己发现的条目（重复登记/作废/未备案/明细里有而登记里没有）、边界相等与封顶保底；"
        "这一节只写口径，不要把它叫「陷阱」、不要提示哪几行会触发；\n"
        "6. 至少一份「必须自己发现」的条目清单作为输出（如 异常/缺项清单），**漏报或多报即整题 0 分**："
        "在「## 输出」里把它定义成对象数组，第一个字段是**字符串**行键（如 "
        "{\"样品编号\": \"S-007\", \"原因\": \"…\"}）；\n"
        "7. 「## 输出」一节明确返回 JSON 的结构与字段名；对象数组第一个字段是行键；"
        "集合语义的数组注明「顺序不计分」；金额保留 2 位小数；\n"
        "8. 纯虚构、可公开。\n\n下面是另一个职业的范例，只示结构、不要照抄内容；"
        "**范例规模很小、只有两份材料，你的案例必须满足上面的规模与特殊情形要求**：\n%s\n"
        "只输出：\n<<<FILE 规则.md>>>\n（规则原文）\n<<<END>>>") % (
            occupation, h.min_files, h.min_main_rows, h.min_ref_rows, h.min_traps,
            _fblock("规则.md", _ex("规则.md")))}]


def code_messages(rules_text):
    h = DEFAULT_HARDNESS
    ex = "".join(_fblock(n, _ex(n)) for n in CODE_FILES)
    return [{"role": "system", "content": SOLVE_SYSTEM}, {"role": "user", "content": (
        "【步骤:代码】按下面这份 规则.md 写 4 个文件，全部只用 Python 标准库：\n"
        "- 参数生成器.py：def worlds(n, seed=0, boundary=True) -> list[dict]。boundary=True 时先放边界世界"
        "（跨档分界 / 封顶触发 / 保底 / 零与空 / 负数 / 重复行键 / 单行 / 最大规模 中适用者），"
        "再用 random.Random(seed) 补满到 n 个；世界结构严格按规则「材料」。"
        "随机段也要**按概率注入每类特殊情形**（每类至少出现在 %d%% 以上的随机世界里），"
        "不能只靠边界世界碰到。\n"
        "- solve.py：def solve(world) -> dict，严格按规则「输出」。\n"
        "- 断言.py：def assertions(world, answer) -> list[str]，写跨材料恒等式（如汇总=明细之和、"
        "清单⊆行键集合），全部成立返回 []。\n"
        "- 物化.py：INSTANCE、TRAPS、TASK、render_inputs(world)->{文件名: 文本}、deliver(answer)->{文件名: 文本}。\n"
        "  · INSTANCE：交给被测 agent 的那个世界。业务明细 ≥ %d 行、标准/参照表 ≥ %d 条，覆盖每类特殊情形"
        "（每类至少 2 处）；**用代码确定性构造**（物化.py 内自带 random.Random(固定种子) 生成 + 显式注入"
        "特殊情形），不要手写几十行字面量，也不要 import 同目录其它文件；\n"
        "  · TRAPS：{特殊情形名: 函数(world) -> 该世界里此情形出现次数}，与规则「特殊情形处理」一一对应，"
        "至少 %d 类；校验会用它核对 INSTANCE 与随机世界的覆盖；\n"
        "  · render_inputs：每份材料单独一个文件（≥ %d 个），csv 或 json；\n"
        "  · TASK：领域/任务名/task_id/子域/prompt/交付要求/scoringBasis，另加三个**内部草稿**字段"
        "一句话概括 / 操作路径（分阶段编号的解题走查）/ 答复要点梗概（答案梗概 + 要点 + 易犯错法），"
        "这三个只进数据集视图、不给被测 agent。\n"
        "规则.md 会被拷成 input/核算规则.md，scoringBasis 的 ref 只能指向它或 render_inputs 产出的文件；"
        "prompt 与交付要求里不得出现任何具体数值，prompt 只派活、不点破特殊情形、不给解题步骤；"
        "交付文件用 csv（首列行键）或 json；task_id 用小写英文/拼音加连字符、以 -syn-001 结尾。\n\n规则：\n%s\n"
        "下面是另一个职业的完整范例（只学结构；范例规模与特殊情形都不达标，别学它的规模）：\n%s\n"
        "只按 <<<FILE 文件名>>> … <<<END>>> 格式输出这 4 个文件。") % (
            int(h.trap_ratio * 100), h.min_main_rows, h.min_ref_rows, h.min_traps, h.min_files,
            _fblock("规则.md", rules_text), ex)}]


def repair_messages(files, problem):
    cur = "".join(_fblock(n, t) for n, t in files.items())
    return [{"role": "system", "content": SOLVE_SYSTEM}, {"role": "user", "content": (
        "【步骤:返修】这个案例没通过校验：\n%s\n\n当前文件：\n%s\n"
        "请判断原因并修正：规则有歧义或缺漏 → 改 规则.md（独立复核员只看规则，必须写到无歧义）；"
        "结构性难度不达标 → 按提示扩材料份数/规模/特殊情形，规则要跟着改的一并改规则；"
        "生成器/solve/断言/物化 写错 → 改对应文件。只输出需要修改的文件，不要输出 solve2.py。")
        % (problem, cur)}]


def solve2_messages(rules_text):
    """独立实现的 prompt —— 上下文**只有 规则.md 全文**：没有 solve.py、没有范例实现、没有任何答案。

    交叉复算的地基就在这一个函数：唯一参数是 rules_text，内容里除固定接口约定外只有规则原文。
    """
    return [
        {"role": "system", "content": "你是独立复核员：只依据给你的规则文字实现计算，看不到、"
                                      "也不得假设任何其它实现或标准答案。"
                                      "只按 <<<FILE 文件名>>> … <<<END>>> 格式输出文件。"},
        {"role": "user", "content": (
            "【步骤:独立实现】下面是一份闭世界规则。仅依据它，独立用 Python 实现 def solve(world) -> dict：\n"
            "- world 的结构与字段名以规则「材料」一节为准；\n"
            "- 返回结构与字段名严格按规则「输出」一节；\n"
            "- 只用 Python 标准库，不读写文件，不 import 非标准库模块；只输出一个文件 solve2.py。\n\n"
            + _fblock("规则.md", rules_text)
            + "\n输出格式：\n<<<FILE solve2.py>>>\n（代码）\n<<<END>>>")},
    ]


class LLMProducer:
    """真调模型。solve 侧与 solve2 用**两个不同的 client**（跨家模型，降共模错误）。"""

    def __init__(self, solve_llm, solve2_llm, trace_dir=None, cost=None, min_gap=0.0, log=None):
        self.solve_llm, self.solve2_llm = solve_llm, solve2_llm
        self.trace_dir, self.cost = trace_dir, cost
        self.min_gap = float(min_gap)   # 同一个 client 两次调用的最小间隔（秒），防 gpt-5.5 这类严限流模型 429
        self.log = log or (lambda m: None)
        # 续跑时接着已有轨迹编号，不覆盖上一次的 01_rules… 等文件
        self._n = len([f for f in os.listdir(trace_dir) if f.endswith("_meta.json")]) \
            if trace_dir and os.path.isdir(trace_dir) else 0
        self._last = {}

    def _call(self, llm, step, messages):
        wait = self.min_gap - (time.time() - self._last.get(id(llm), -1e18))
        if wait > 0:
            self.log("  （同一模型调用间隔，等 %.0fs 防 429）" % wait)
            time.sleep(wait)
        t0 = time.time()
        try:
            res = llm.chat(messages)
        except Exception as e:  # noqa: BLE001 调用层的失败（429/超时/5xx）一律算基础设施故障
            raise InfraError("%s 调用失败（%s）：%s" % (step, getattr(llm, "model", "")
                                                    or getattr(getattr(llm, "cfg", None), "model", ""), e))
        finally:
            self._last[id(llm)] = time.time()
        self.log("  %s 完成：%.1f min，completion_tokens=%s，finish=%s" % (
            step, (time.time() - t0) / 60, res.completion_tokens, res.finish_reason))
        if self.cost:
            self.cost.add("casegen:" + step, res)
        self._n += 1
        if self.trace_dir:   # 轨迹：每次调用的 prompt / 原始响应 / 元信息都落盘，供评审与复核
            os.makedirs(self.trace_dir, exist_ok=True)
            base = os.path.join(self.trace_dir, "%02d_%s" % (self._n, step))
            with open(base + "_prompt.json", "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)
            with open(base + "_response.txt", "w", encoding="utf-8") as f:
                f.write(res.text or "")
            with open(base + "_meta.json", "w", encoding="utf-8") as f:
                json.dump({"model": getattr(res, "model", ""), "finish_reason": res.finish_reason,
                           "completion_tokens": res.completion_tokens}, f, ensure_ascii=False)
        return res.text or ""

    def draft_rules(self, case_dir, occupation):
        files = parse_files(self._call(self.solve_llm, "rules", rules_messages(occupation)))
        if "规则.md" not in files:
            raise RuntimeError("模型没按格式产出 规则.md（见 trace）")
        _write_files(case_dir, files, {"规则.md"})

    def write_code(self, case_dir, rules_text):
        files = parse_files(self._call(self.solve_llm, "code", code_messages(rules_text)))
        _write_files(case_dir, files, set(CODE_FILES))

    def write_solve2(self, case_dir, rules_text):
        files = parse_files(self._call(self.solve2_llm, "solve2", solve2_messages(rules_text)))
        _write_files(case_dir, files, {"solve2.py"})

    def repair(self, case_dir, problem):
        cur = {n: _read(os.path.join(case_dir, n)) for n in ("规则.md",) + CODE_FILES
               if os.path.isfile(os.path.join(case_dir, n))}      # 不给 solve2.py，免得锚定
        files = parse_files(self._call(self.solve_llm, "repair", repair_messages(cur, problem)))
        return set(_write_files(case_dir, files, {"规则.md"} | set(CODE_FILES)))


# ---------------- CLI ----------------
def run_build(occupation=None, out=None, solve_model="gpt-5.5", solve2_model="glm-5.3-flash",
              rounds=3, n=200, seed=0, resume=None, call_gap=30.0):
    """退出码：0 接收 / 1 作废（案例质量） / 2 配置问题 / 3 不是案例的错（网关故障或框架侧问题）。"""
    from .config import PipelineConfig
    from .cost import CostTracker
    from .llm import get_client
    cfg = PipelineConfig()
    if cfg.llm.provider == "mock":
        print("✗ ALE_LLM_PROVIDER=mock：生成半段要真模型（.env 设 openai + 网关 + key）", file=sys.stderr)
        return 2
    if resume:
        out = resume
        if not os.path.isdir(os.path.join(out, "case")):
            print("✗ --resume 目录下没有 case/：%s" % out, file=sys.stderr)
            return 2
        old = os.path.join(out, "report.json")
        if os.path.isfile(old):          # 旧报告留档，不覆盖
            if not occupation:
                occupation = json.load(open(old, encoding="utf-8")).get("occupation")
            os.rename(old, os.path.join(out, "report.%s.json" % time.strftime(
                "%Y%m%d-%H%M%S", time.localtime(os.path.getmtime(old)))))
    if not occupation:
        print("✗ 需要 --occupation（或 --resume 一个带 report.json 的目录）", file=sys.stderr)
        return 2
    os.environ.setdefault("ALE_LLM_RETRIES", "6")   # 429 退避 8/16/32/60/60s，扛约 3 分钟限流
    out = out or os.path.join(cfg.runs_dir, "casegen", "%s-%s" % (
        scaffold._safe_seg(occupation, 24), time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(out, exist_ok=True)
    _stderr_log("产出目录：%s%s" % (out, "（续跑）" if resume else ""))
    cost = CostTracker()
    prod = LLMProducer(get_client(dataclasses.replace(cfg.llm, model=solve_model)),
                       get_client(dataclasses.replace(cfg.llm, model=solve2_model)),
                       trace_dir=os.path.join(out, "trace"), cost=cost,
                       min_gap=call_gap, log=_stderr_log)
    try:
        rep, code = build_case(occupation, out, prod, max_rounds=rounds, n=n, seed=seed,
                               resume=bool(resume), log=_stderr_log), 0
    except CaseRejected as e:
        rep, code = e.args[0], 1
    except (InfraError, FrameworkError) as e:
        rep, code = e.args[0], 3
    except RuntimeError as e:
        rep, code = {"status": "rejected", "错误": str(e)}, 1
    rep.update({"模型": {"solve": solve_model, "solve2": solve2_model}, "退出码": code,
                "成本RMB": cost.total(), "未计价模型": cost.unpriced_models()})
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    return code


def _selftest():
    """离线自测（不触网、不花钱）：静态接线 / 注错作废 / solve2 隔离 / LLM 路径(mock 网关)。"""
    import tempfile
    from .llm import MockLLMClient
    res = []

    def check(cond, msg):
        print(("✓ " if cond else "✗ ") + msg)
        res.append(bool(cond))

    good, bug = EXAMPLE_CASE, os.path.join(_HERE, "crosscheck_cases", "bug_boundary")
    tmp = tempfile.mkdtemp(prefix="casegen_build_selftest_")
    try:
        sp = StaticProducer(good)
        rep = build_case("（自测）", os.path.join(tmp, "static"), sp, n=60, hardness=RELAXED)
        check(rep["status"] == "accepted" and len(rep["rounds"]) == 1,
              "静态 producer：好案例 1 轮接收，物化 + qc(N6/N8) 全过")
        check(sp.solve2_inputs == [_read(os.path.join(good, "规则.md"))],
              "独立性（编排层）：交给 solve2 的只有 规则.md 原文")
        try:
            build_case("（自测）", os.path.join(tmp, "bug"), StaticProducer(bug), n=60, hardness=RELAXED)
            check(False, "注错案例应被作废")
        except CaseRejected as e:
            check(e.args[0]["rounds"][-1]["stage"] == "crosscheck",
                  "注错案例：crosscheck 拒收 → 作废（不先放行）")
        check(list(inspect.signature(solve2_messages).parameters) == ["rules_text"],
              "独立性（prompt 层）：solve2_messages 只接收 rules_text")

        def blk(names):
            return "".join(_fblock(nm, _read(os.path.join(good, nm))) for nm in names)
        solve_mock = MockLLMClient("mock-solve", scripted={
            "【步骤:规则】": blk(["规则.md"]), "【步骤:代码】": blk(CODE_FILES)})
        solve2_mock = MockLLMClient("mock-solve2", scripted={   # 只有这个 client 认识独立实现步骤
            "【步骤:独立实现】": "```\n" + blk(["solve2.py"]) + "```"})
        trace = os.path.join(tmp, "llm", "trace")
        rep = build_case("（自测）", os.path.join(tmp, "llm"),
                         LLMProducer(solve_mock, solve2_mock, trace_dir=trace), n=60, hardness=RELAXED)
        check(rep["status"] == "accepted", "LLM producer（mock 网关）：多文件响应解析 + 换 client → 接收")
        p2 = [f for f in sorted(os.listdir(trace)) if f.endswith("_solve2_prompt.json")]
        txt = _read(os.path.join(trace, p2[0])) if p2 else ""
        check(bool(p2) and "城市标准" in txt and "rows, over, abn, total" not in txt,
              "轨迹核对：solve2 的 prompt 含规则原文、不含 solve.py")

        # 返修闭环：第一版 solve2 注错 → crosscheck 拒 → 返修(改规则) → 重产 solve2 → 第 2 轮接收
        class SeqClient:
            def __init__(self, model, steps):
                self.model, self.steps = model, {k: list(v) for k, v in steps.items()}

            def chat(self, messages, **kw):
                from .llm import LLMResult
                last = messages[-1]["content"]
                for k, seq in self.steps.items():
                    if k in last:
                        return LLMResult(text=seq.pop(0) if len(seq) > 1 else seq[0], model=self.model)
                return LLMResult(text="", model=self.model)

        bad2 = _fblock("solve2.py", _read(os.path.join(bug, "solve2.py")))
        s_llm = SeqClient("seq-solve", {"【步骤:规则】": [blk(["规则.md"])],
                                        "【步骤:代码】": [blk(CODE_FILES)],
                                        "【步骤:返修】": [blk(["规则.md"])]})
        s2_llm = SeqClient("seq-solve2", {"【步骤:独立实现】": [bad2, blk(["solve2.py"])]})
        trace = os.path.join(tmp, "repair", "trace")
        rep = build_case("（自测）", os.path.join(tmp, "repair"),
                         LLMProducer(s_llm, s2_llm, trace_dir=trace), n=60, hardness=RELAXED)
        r0 = rep["rounds"][0]
        check(rep["status"] == "accepted" and len(rep["rounds"]) == 2 and r0["stage"] == "crosscheck"
              and r0.get("changed") == ["规则.md"],
              "返修闭环：注错 solve2 被拒 → 改规则 → 重产 solve2 → 第 2 轮接收")
        pr = [f for f in sorted(os.listdir(trace)) if f.endswith("_repair_prompt.json")]
        check(bool(pr) and "BUG: >= 应为 >" not in _read(os.path.join(trace, pr[0])),
              "返修 prompt 不含 solve2.py（不让 solve 侧锚定独立实现）")

        # 网关故障 → infra_error，不消耗返修轮次
        class FailClient:
            model = "fail"

            def chat(self, messages, **kw):
                raise RuntimeError("网关请求失败（重试 6 次仍失败）：HTTP Error 429: Too Many Requests")
        try:
            build_case("（自测）", os.path.join(tmp, "infra"), LLMProducer(FailClient(), FailClient()))
            check(False, "网关故障应抛 InfraError")
        except InfraError as e:
            r = e.args[0]
            check(r["status"] == "infra_error" and r["rounds"] == [] and "429" in r["错误"],
                  "网关故障（429）→ infra_error、不算作废、不消耗返修")

        # 地板白拿分 → framework_error，不返修（临时把地板合成器换成空操作来模拟）
        orig = casegen.floor.synthesize
        casegen.floor.synthesize = lambda d, seed=0: []
        try:
            build_case("（自测）", os.path.join(tmp, "fw"), StaticProducer(good), n=60, hardness=RELAXED)
            check(False, "地板白拿分应抛 FrameworkError")
        except FrameworkError as e:
            r = e.args[0]
            check(r["status"] == "framework_error" and len(r["rounds"]) == 1
                  and "白拿分的字段" in (r["rounds"][0]["problem"] or ""),
                  "地板白拿分 → framework_error、带字段线索、不烧返修")
        finally:
            casegen.floor.synthesize = orig

        # 续跑：文件都在就零次 producer 调用
        class Boom:
            def __getattr__(self, name):
                raise AssertionError("续跑不该调用 producer.%s" % name)
        rep = build_case("（自测）", os.path.join(tmp, "static"), Boom(), n=60, resume=True,
                         hardness=RELAXED)
        check(rep["status"] == "accepted" and rep["resumed"], "--resume：已有文件全复用，零次模型调用即接收")

        # 同一 client 调用间隔
        m = MockLLMClient("gap")
        p = LLMProducer(m, m, min_gap=0.3)
        t0 = time.time()
        p._call(m, "a", [{"role": "user", "content": "x"}])
        p._call(m, "b", [{"role": "user", "content": "x"}])
        check(time.time() - t0 >= 0.3, "同一模型两次调用之间按 min_gap 等待（防 429）")

        # 难度门：默认门拒收小范例（2 份材料 / 4 行 / 「单价等于封顶」随机世界里从不出现），停在 hardness
        try:
            build_case("（自测）", os.path.join(tmp, "soft"), StaticProducer(good), n=60)
            check(False, "默认难度门应拒收小范例")
        except CaseRejected as e:
            r0 = e.args[0]["rounds"][0]
            pb = r0["problem"] or ""
            check(r0["stage"] == "hardness" and "份材料" in pb and "规模不足" in pb
                  and "单价等于封顶" in pb and not os.path.isdir(os.path.join(tmp, "soft", "tasks_r1")),
                  "难度门：材料份数/规模/陷阱覆盖不达标 → 停在 hardness、不进 crosscheck/物化，带返修线索")

        # 难度门达标路径：在范例上扩成 3 份材料、40 行明细、18 条标准，去掉随机里碰不到的那类陷阱
        big = os.path.join(tmp, "big_case")
        shutil.copytree(good, big, ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(big, "物化.py"), "a", encoding="utf-8") as f:
            f.write(
                "\nINSTANCE = {'城市标准': dict({'北京': 503, '上海': 517, '成都': 349},\n"
                "              **{'城市%02d' % i: 300 + i for i in range(15)}),\n"
                "            '明细': [{'行号': i + 1, '城市': ['北京', '上海', '成都', '广州'][i % 4],\n"
                "                     '住宿单价': [600, 517, 300, 400][i % 4], '天数': 1 + i % 3}\n"
                "                    for i in range(40)]}\n"
                "_render0 = render_inputs\n\n\n"
                "def render_inputs(world):\n"
                "    out = _render0(world)\n"
                "    out['出差审批单.json'] = json.dumps([r['行号'] for r in world['明细']])\n"
                "    return out\n\n\n"
                "import json\n"
                "TRAPS = {k: v for k, v in TRAPS.items() if k != '单价等于封顶'}\n")
        hp = hardness_check(big, Hardness(min_traps=2))
        check(hp == [] and _records("a.csv", "h\n1\n2\n") == 2
              and _records("b.json", '{"x": [1, 2, 3], "y": 1}') == 3,
              "难度门达标路径：3 份材料 / 40 行 / 18 条 / 陷阱覆盖够 → 放行%s" % ("" if hp == [] else "：%s" % hp))

        # 接收报告带 N7 agent 档命令（maxSteps 按公式、路径绝对且已引号）+ 数据集 sidecar 草稿
        rep = build_case("（自测）", os.path.join(tmp, "n7"), StaticProducer(good), n=60, hardness=RELAXED)
        n7 = rep.get("N7") or {}
        want = max(20, -(-(n7.get("附件数", 0) + n7.get("交付数", 0)) * 3 // 2))
        check(n7.get("maxSteps") == want == 20 and "N7_MODE=agent" in n7.get("便宜档", "")
              and "ALE_LLM_MODEL=glm-5.3-flash" in n7["便宜档"] and "N7_TIER=前沿档" in n7.get("前沿档", "")
              and ("N7_TASK='%s'" % os.path.abspath(rep["task_dir"])) in n7["便宜档"],
              "接收报告带 N7 agent 档命令：maxSteps=附件+交付+50%(最低20)、任务路径绝对且加引号")
        side = json.load(open(os.path.join(rep["task_dir"], "dataset.json"), encoding="utf-8"))
        check(side.get("职位") == "（自测）" and side.get("自动化类型") == "闭世界" and side.get("提交人") == ""
              and set(side.get("_draft", {})) == set(casegen.DRAFT_FIELDS),
              "物化产数据集 sidecar 草稿 dataset.json（职位/闭世界/构建日期，_draft 标待人核字段）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = all(res)
    print("\n%s" % ("casegen 生成半段自测通过" if ok else "casegen 生成半段自测失败"))
    return ok


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="pipeline.casegen_build")
    ap.add_argument("--occupation")
    ap.add_argument("--out")
    ap.add_argument("--resume", help="续跑某个产出目录（复用已有 规则/代码/solve2）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest or not (a.occupation or a.resume):
        return 0 if _selftest() else 1
    return run_build(a.occupation, a.out, resume=a.resume)


if __name__ == "__main__":
    sys.exit(main())
