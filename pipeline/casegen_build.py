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

from . import casegen, crosscheck, qc, scaffold, subdomains, tasks

_HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE_CASE = os.path.join(_HERE, "crosscheck_cases", "good")
# 代码分两次产出，避免一次 4 文件 + 大 INSTANCE 撞 max_tokens 截断（见 write_code）：
LOGIC_FILES = ("参数生成器.py", "solve.py", "断言.py")   # 第①步：逻辑三件
MAT_FILES = ("物化.py",)                                 # 第②步：物化（含大 INSTANCE）
CODE_FILES = LOGIC_FILES + MAT_FILES                     # 完整案例代码（校验/返修按这个全集）
REFS_DIR = "参考资料"          # 人工抓来的真实口径材料（--refs 拷进来），含 来源.json
REFS_MANIFEST = "来源.json"
REFS_SECTION = "## 引用来源"   # 规则.md 里声明借用了哪些真实值、各自出自哪份材料
# 文件块用显式标记而非 ``` 围栏：规则.md 里本身就有 ``` 代码块，套围栏会被截断
_FILE_RE = re.compile(r"<<<FILE\s+([^>\n]+?)\s*>>>\n(.*?)<<<END>>>", re.S)
_FENCE_RE = re.compile(r"^\s*```[^\n]*\n(.*?)\n?```\s*$", re.S)
# 「- <值> ← <文件名>」一行一条，值与文件名都允许带空格
_CITE_RE = re.compile(r"^\s*[-*]\s*(.+?)\s*(?:←|<-|<=)\s*(\S.*?)\s*$", re.M)


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
    miss = [f for f in CODE_FILES if not os.path.isfile(os.path.join(case_dir, f))]
    if miss:   # 文件没齐（常因上一步输出被截断 finish=length）→ 明确报缺哪个，别抛原始 traceback
        return ("缺少案例文件：%s —— 请**完整**输出这些文件（上一步可能被截断，没写全）"
                % "、".join(miss))
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
    max_attachment_chars: int = 7800  # 单个附件字符数上限（对齐 N7 读取上限 8000，留 200 余量）


DEFAULT_HARDNESS = Hardness()
RELAXED = Hardness(min_files=0, min_main_rows=0, min_ref_rows=0, min_traps=0, trap_ratio=0.0,
                   max_attachment_chars=0)


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


def _safe_trap(fn, world):
    import copy
    try:
        return bool(fn(copy.deepcopy(world)))
    except Exception:  # noqa: BLE001 TRAPS 里某个函数抛错不该中断挑世界
        return False


def choose_instance(case_dir, spec=DEFAULT_HARDNESS, seed=0, scan=400):
    """挑一个「材料够多 + 规模达标 + 覆盖每类 TRAP」的世界作 INSTANCE（框架侧确定性地选，
    不再让模型手写几十行字面量——那是撑爆 token 的主因）。物化.INSTANCE 已显式给出则直接用它。

    扫 scan 个生成器世界，返回第一个全部达标的；都不达标就返回「覆盖陷阱最多、规模最大」的那个，
    让难度门能报出可读的差距（而不是永远卡在生成器补不出合格世界上）。
    """
    import copy
    m = casegen._load(os.path.join(case_dir, "物化.py"), "_cb_pick_m")
    inst = getattr(m, "INSTANCE", None)
    if inst is not None:
        return inst
    gen = casegen._load(os.path.join(case_dir, "参数生成器.py"), "_cb_pick_g")
    traps = getattr(m, "TRAPS", None) or {}
    worlds = gen.worlds(max(scan, spec.sample), seed)
    best, best_key = None, None
    for w in worlds:
        try:
            files = m.render_inputs(copy.deepcopy(w))
        except Exception:  # noqa: BLE001 个别世界渲染抛错就跳过，别中断挑选
            continue
        szs = sorted((_records(nm, t) for nm, t in files.items()), reverse=True)
        main = szs[0] if szs else 0
        ref = szs[1] if len(szs) > 1 else 0
        max_char = max((len(t) for t in files.values()), default=0)
        covered = sum(1 for fn in traps.values() if _safe_trap(fn, w))
        if (len(files) >= spec.min_files and main >= spec.min_main_rows
                and ref >= spec.min_ref_rows and covered == len(traps)
                and (not spec.max_attachment_chars or max_char <= spec.max_attachment_chars)):
            return w
        key = (covered, main, ref)
        if best_key is None or key > best_key:
            best, best_key = w, key
    return best if best is not None else (worlds[0] if worlds else {})


def hardness_check(case_dir, spec=DEFAULT_HARDNESS, seed=0, world=None):
    """返回不达标项列表（[] 为达标）。只看选定世界的渲染结果与 TRAPS，不调模型。
    world 由调用方（choose_instance）挑好传入，物化与难度门用同一个世界。"""
    import copy
    if not any(dataclasses.astuple(spec)[:5]):
        return []                     # RELAXED：不设门（自测里给小范例案例用）
    m = casegen._load(os.path.join(case_dir, "物化.py"), "_cb_hard")
    gen = casegen._load(os.path.join(case_dir, "参数生成器.py"), "_cb_hard_gen")
    if world is None:
        world = getattr(m, "INSTANCE", None) or gen.worlds(1, seed)[0]
    files = m.render_inputs(copy.deepcopy(world))
    probs = []
    if spec.max_attachment_chars:
        for nm, t in files.items():
            if len(t) > spec.max_attachment_chars:
                probs.append("附件 %s 有 %d 字符，超过上限 %d（N7 单文件读取上限 8000，超了会被截断、测不出难度）："
                             "JSON 用 json.dumps(obj, ensure_ascii=False, separators=(',', ':')) 不带缩进，"
                             "或减小数据规模 / 拆成多份材料" % (nm, len(t), spec.max_attachment_chars))
    if len(files) < spec.min_files:
        probs.append("render_inputs 只产出 %d 份材料 %s，至少要 %d 份**独立**附件（每份材料单独一个文件，"
                     "不得把标准表和明细并进一个文件）" % (len(files), sorted(files), spec.min_files))
    sizes = sorted(((_records(n, t), n) for n, t in files.items()), reverse=True)
    main = sizes[0] if sizes else (0, "（无）")
    ref = sizes[1] if len(sizes) > 1 else (0, "（无）")
    if main[0] < spec.min_main_rows:
        probs.append("选不出达标世界：最大那份材料 %s 只有 %d 条记录，至少 %d 条"
                     "（扩大参数生成器的「最大规模」世界，或让随机段产更大的世界）"
                     % (main[1], main[0], spec.min_main_rows))
    if ref[0] < spec.min_ref_rows:
        probs.append("选不出达标世界：第二大那份材料 %s 只有 %d 条记录，至少 %d 条"
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
        if not _safe_trap(fn, world):
            probs.append("选定实例里没出现陷阱「%s」（生成器扫遍也挑不出同时覆盖所有陷阱的世界，"
                         "放宽各陷阱的共现约束或提高注入概率）" % name)
        if rand:
            hit = sum(1 for w in rand if fn(copy.deepcopy(w))) / len(rand)
            if hit < spec.trap_ratio:
                probs.append("随机世界里陷阱「%s」只出现在 %.0f%% 的世界，至少 %.0f%%（参数生成器随机段也要按"
                             "概率注入每类陷阱，否则 crosscheck 只在边界世界碰得到它）"
                             % (name, hit * 100, spec.trap_ratio * 100))
    return probs


# ---------------- 一轮校验 + 编排 ----------------
def load_refs(case_dir):
    """读 case/参考资料/：返回 ({文件名: 文本}, 来源清单)。目录不存在就返回空——纯合成路径不受影响。"""
    d = os.path.join(case_dir, REFS_DIR)
    if not os.path.isdir(d):
        return {}, []
    texts, manifest = {}, []
    for fn in sorted(os.listdir(d)):
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        try:
            txt = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        if fn == REFS_MANIFEST:
            try:
                manifest = json.loads(txt)
            except ValueError:
                manifest = []
            continue
        texts[fn] = txt
    return texts, manifest


def refs_trace_check(case_dir):
    """逐字回溯闸门：规则.md 声明借用的每个真实值，必须在对应参考资料原文里逐字命中。

    为什么靠显式声明而不是扫规则里的数字——规则.md 本来就满是行数、字段序号、小数位数，
    正则扫数字会把所有题都误挡。让模型自己在「## 引用来源」里列「值 ← 文件名」，再逐条
    核验，才是机械可判的。被查对象从「agent 编造证据」换成「出题模型编造口径」，机制同
    参考_评分体系规范 的证据逐字回溯。

    没有参考资料时返回 None（不设此门）。
    """
    texts, _ = load_refs(case_dir)
    if not texts:
        return None
    rules_p = os.path.join(case_dir, "规则.md")
    if not os.path.isfile(rules_p):
        return "缺 规则.md"
    rules = _read(rules_p)
    if REFS_SECTION not in rules:
        return ("给了参考资料，但 规则.md 里没有「%s」一节。请新增该节，逐条列出你从参考资料"
                "借用的真实值，每行一条，格式：`- <值> ← <参考资料文件名>`。"
                "可用的参考资料：%s" % (REFS_SECTION, "、".join(sorted(texts))))
    body = rules.split(REFS_SECTION, 1)[1]
    body = re.split(r"\n#{1,3} ", body, 1)[0]
    cites = _CITE_RE.findall(body)
    if not cites:
        return ("「%s」一节里解析不到任何条目。每行一条，格式：`- <值> ← <参考资料文件名>`。"
                "可用的参考资料：%s" % (REFS_SECTION, "、".join(sorted(texts))))
    bad = []
    for val, src in cites:
        v = val.strip().strip("`“”\"'")
        if not v:
            continue
        if src not in texts:
            bad.append("「%s」声明出自 %r，但参考资料里没有这个文件（可用：%s）"
                       % (v, src, "、".join(sorted(texts))))
        elif v not in texts[src]:
            bad.append("「%s」在 %s 原文里找不到（逐字比对）——真实口径不许自己编，"
                       "请改成原文里确有的值，或把它从本节移走（合成值不该列在这里）" % (v, src))
    if bad:
        return "引用来源逐字回溯不通过：\n- " + "\n- ".join(bad[:12])
    return None


def _check_round(case_dir, out_dir, rnd, n, seed, hardness=DEFAULT_HARDNESS, subdomain=None):
    """返回 (stage, problem, info)；problem 为 None 表示本轮全过。"""
    err = smoke(case_dir)
    if err:
        return "smoke", "冒烟失败：\n" + err, None
    err = refs_trace_check(case_dir)      # 给了参考资料才设此门
    if err:
        return "refs", err, None
    gate_on = any(dataclasses.astuple(hardness)[:5])
    world = None
    try:
        if gate_on:
            world = choose_instance(case_dir, hardness, seed)   # 框架挑 INSTANCE，物化与难度门共用
        probs = hardness_check(case_dir, hardness, seed, world=world)
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
                                  subdomain=subdomain, instance=world)
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
                          "crosscheck世界数": rep["世界数"], "后续测题计划": n7_commands(mat["dir"])}


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


def build_case(subdomain, out_dir, producer, max_rounds=3, n=200, seed=0, resume=False, log=None,
               hardness=DEFAULT_HARDNESS):
    log = log or (lambda m: None)
    case_dir = os.path.join(out_dir, "case")
    os.makedirs(case_dir, exist_ok=True)
    report = {"subdomain": subdomain, "out": out_dir, "case_dir": case_dir,
              "resumed": bool(resume), "难度门": dataclasses.asdict(hardness), "rounds": []}

    def have(*names):
        return resume and all(os.path.isfile(os.path.join(case_dir, x)) for x in names)

    t0 = time.time()
    try:
        if have("规则.md"):
            log("复用已有 规则.md（不调模型）")
        else:
            log("① 写规则（solve 侧模型）…")
            producer.draft_rules(case_dir, subdomain)
        rules = _read(os.path.join(case_dir, "规则.md"))
        if have(*CODE_FILES):
            log("复用已有 参数生成器/solve/断言/物化")
        else:
            log("② 写代码（逻辑三件 + 物化，分两次调用防截断）…")
            producer.write_code(case_dir, rules, only_missing=resume)
        if have("solve2.py"):
            log("复用已有 solve2.py")
        else:
            log("③ 独立实现 solve2（另一模型，只给规则原文）…")
            producer.write_solve2(case_dir, rules)          # 只交规则原文——独立性的编排层保证
        for rnd in range(1, max_rounds + 1):
            log("第 %d/%d 轮校验：冒烟 → 难度门 → crosscheck(%d 世界) → 物化 → qc …" % (rnd, max_rounds, n))
            stage, problem, info = _check_round(case_dir, out_dir, rnd, n, seed, hardness, subdomain)
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

    def draft_rules(self, case_dir, subdomain):
        self._copy(case_dir, ["规则.md"])

    def write_code(self, case_dir, rules_text, only_missing=False):
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


def _refs_block(refs, manifest, per_file=6000):
    """参考资料注入段。真实口径必须逐字采用，业务流水仍然合成——这样 200 世界与模板家族都保住。"""
    if not refs:
        return ""
    src = {}
    for m in manifest if isinstance(manifest, list) else []:
        if isinstance(m, dict) and m.get("file"):
            src[m["file"]] = "；".join(
                str(m[k]) for k in ("说明", "public_source_url", "抓取日期") if m.get(k))
    parts = [
        "\n## 参考资料（真实公开源，已冻结快照）\n",
        "下面每份材料都是从真实公开源抓来的原文。**口径层必须逐字采用、一个数都不许自己编**：\n"
        "标准/分类定义、编码、限值、费率、系数、分档阈值、字段名与量表计分规则，凡能在参考资料里"
        "找到的就照抄原文；**业务流水仍然由参数生成器合成**（这样才能产出多个世界）。\n"
        "并且必须在 规则.md 里新增一节「%s」，逐条声明你借用了哪些真实值、各自出自哪份材料，"
        "每行一条，格式：\n`- <值> ← <参考资料文件名>`\n"
        "机器会逐字核验这一节：命不中原文即退回。合成出来的值**不要**列进这一节。\n" % REFS_SECTION,
    ]
    for fn, txt in refs.items():
        head = "### %s" % fn + ("（%s）" % src[fn] if fn in src else "")
        body = txt if len(txt) <= per_file else txt[:per_file] + "\n…（已截断，只摘前 %d 字符）" % per_file
        parts.append("%s\n```\n%s\n```\n" % (head, body))
    return "\n".join(parts)


def rules_messages(subdomain, refs=None, manifest=None):
    h = DEFAULT_HARDNESS
    return [{"role": "system", "content": SOLVE_SYSTEM}, {"role": "user", "content": (
        "【步骤:规则】为下面这个 ALE 子域设计一个 numeric_tabular 范式的闭世界核算任务，写出 规则.md。\n\n"
        "%s\n%s\n硬要求：\n"
        "1. 至少 %d 份**独立材料**（如 标准/限值表 + 业务明细 + 登记/委托/台账），每份材料将来单独成一个附件文件，"
        "不得合并成一个文件；计算必须跨材料按关联键 join；\n"
        "2. 规模：业务明细不少于 %d 行、标准/参照表不少于 %d 条（数据由参数生成器产出，规则里只写结构）；\n"
        "3. 「## 材料」一节给出世界参数的精确 JSON 结构与字段名（出题方与独立复核员都只能据此取数），"
        "并写明每份材料对应哪个附件文件；\n"
        "4. 全部费率/阈值/封顶/判定口径写进规则，答案由规则唯一确定，不引用规则外的法规或常识"
        "（给了参考资料时，口径值照抄参考资料原文，并在「%s」里声明出处）；\n"
        "5. 「## 特殊情形处理」一节至少 %d 类特殊情形，并写清处理语义，从这些杠杆里选（越多越好）："
        "跨材料关联键写法不一致需规范化（前导零/大小写/全半角/首尾空格，规则写明规范化方法）、单位混用需换算、"
        "中间扣除或校正、需要自己发现的条目（重复登记/作废/未备案/明细里有而登记里没有）、边界相等与封顶保底；"
        "这一节只写口径，不要把它叫「陷阱」、不要提示哪几行会触发；\n"
        "6. 至少一份「必须自己发现」的条目清单作为输出（如 异常/缺项清单），**漏报或多报即整题 0 分**："
        "在「## 输出」里把它定义成对象数组，第一个字段是**字符串**行键（如 "
        "{\"样品编号\": \"S-007\", \"原因\": \"…\"}）；\n"
        "7. 「## 输出」一节明确返回 JSON 的结构与字段名；对象数组第一个字段是行键；"
        "集合语义的数组注明「顺序不计分」；金额保留 2 位小数；\n"
        "8. 纯虚构、可公开（参考资料里的真实口径除外，它们按上面的要求照抄并声明出处）。\n\n"
        "下面是另一个子域的范例，只示结构、不要照抄内容；"
        "**范例规模很小、只有两份材料，你的案例必须满足上面的规模与特殊情形要求**：\n%s\n"
        "只输出：\n<<<FILE 规则.md>>>\n（规则原文）\n<<<END>>>") % (
            subdomains.anchor(subdomain), _refs_block(refs or {}, manifest),
            h.min_files, h.min_main_rows, h.min_ref_rows, REFS_SECTION,
            h.min_traps, _fblock("规则.md", _ex("规则.md")))}]


def code_messages(rules_text):
    h = DEFAULT_HARDNESS
    ex = "".join(_fblock(n, _ex(n)) for n in LOGIC_FILES)
    return [{"role": "system", "content": SOLVE_SYSTEM}, {"role": "user", "content": (
        "【步骤:代码】按下面这份 规则.md 写 3 个文件（物化.py 下一步单独写，这步不要出），全部只用 Python 标准库：\n"
        "- 参数生成器.py：def worlds(n, seed=0, boundary=True) -> list[dict]。boundary=True 时先放边界世界"
        "（跨档分界 / 封顶触发 / 保底 / 零与空 / 负数 / 重复行键 / 单行 / 最大规模 中适用者），"
        "再用 random.Random(seed) 补满到 n 个；世界结构严格按规则「材料」。"
        "随机段也要**按概率注入每类特殊情形**（每类至少出现在 %d%% 以上的随机世界里），"
        "不能只靠边界世界碰到。\n"
        "- solve.py：def solve(world) -> dict，严格按规则「输出」。\n"
        "- 断言.py：def assertions(world, answer) -> list[str]，写跨材料恒等式（如汇总=明细之和、"
        "清单⊆行键集合），全部成立返回 []。\n\n规则：\n%s\n"
        "下面是另一个职业的这三个文件的范例（只学结构）：\n%s\n"
        "只按 <<<FILE 文件名>>> … <<<END>>> 格式输出这 3 个文件。") % (
            int(h.trap_ratio * 100), _fblock("规则.md", rules_text), ex)}]


def materialize_messages(rules_text, solve_text):
    """第②步只产 物化.py。单独一次调用；INSTANCE 不再让模型写（框架自己挑一个达标世界），
    所以这个文件很小——只填 4 个符号，不许写 __main__/命令行/读写磁盘。"""
    h = DEFAULT_HARDNESS
    return [{"role": "system", "content": SOLVE_SYSTEM}, {"role": "user", "content": (
        "【步骤:物化】按下面的 规则.md 与 solve.py，写**一个**文件 物化.py，只用 Python 标准库。\n"
        "它只是被 import 的「物化元数据」模块，**不是脚本**：禁止写 if __name__ == '__main__'、"
        "禁止命令行/argv、禁止读写磁盘或建目录、禁止 import 同目录其它文件。只提供下面 4 个顶层符号：\n"
        "- TRAPS：dict，{特殊情形名: 函数(world) -> 该世界里此情形出现次数}，与规则「特殊情形处理」"
        "一一对应，至少 %d 类。world 的结构即规则「材料」里那个 JSON。\n"
        "- render_inputs(world) -> {文件名: 文本}：把 world 的每份材料**各渲染成一个独立文件**"
        "（共 ≥ %d 个，csv 或 json），文件名用中文业务名。这是交给被测 agent 的附件；\n"
        "  每个附件的文本长度必须 ≤ %d 字符（评测单文件读取上限 8000，超了会被截断、无法测难度）："
        "JSON 一律 json.dumps(obj, ensure_ascii=False, separators=(',', ':'))，不带缩进、不要多余空格；"
        "若某份材料数据量偏大，优先用更紧凑的字段名或拆成多份材料，不要堆进一个超大文件。\n"
        "- deliver(answer) -> {文件名: 文本}：把 solve(world) 的返回渲染成交付文件，"
        "键名/结构与 solve.py 的输出严格对应；交付用 csv（首列行键）或 json。\n"
        "- TASK：dict，含 领域/任务名/task_id/子域/prompt/交付要求/scoringBasis，"
        "另加三个**内部草稿**字段 一句话概括 / 操作路径（分阶段编号的解题走查）/ "
        "答复要点梗概（答案梗概 + 要点 + 易犯错法）——这三个只进数据集视图、不给被测 agent。\n"
        "  · 规则.md 会被拷成 input/核算规则.md，scoringBasis 每条 {point, source:'material', ref}，"
        "ref 只能指向它或 render_inputs 产出的文件名；\n"
        "  · prompt 与交付要求里**不得出现任何具体数值**，prompt 只派活、不点破特殊情形、不给解题步骤；\n"
        "  · task_id 用小写英文/拼音加连字符、以 -syn-001 结尾。\n"
        "**不要**定义 INSTANCE（框架会从参数生成器里挑一个规模达标、覆盖所有 TRAPS 的世界）；"
        "也**不要**写 worlds/solve（它们在别的文件里）。\n\n"
        "规则：\n%s\nsolve.py：\n%s\n下面是另一个职业的 物化.py 范例（只学这 4 个符号的写法）：\n%s\n"
        "只输出：\n<<<FILE 物化.py>>>\n（代码）\n<<<END>>>") % (
            h.min_traps, h.min_files, h.max_attachment_chars,
            _fblock("规则.md", rules_text), _fblock("solve.py", solve_text),
            _fblock("物化.py", _ex("物化.py")))}]


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
        # 调用前先打一行：旧代码只在返回后记日志，于是「卡在网关」和「跑得慢」在日志里
        # 长得一模一样（实测跑批有 25 分钟不出任何新行）。超时上限一并打出来，便于判断该等多久。
        self.log("  → %s 调用 %s（prompt %d 字符，超时上限 %ss）…" % (
            step, getattr(llm, "model", "") or getattr(getattr(llm, "cfg", None), "model", "?"),
            sum(len(m.get("content") or "") for m in messages),
            os.environ.get("ALE_LLM_TIMEOUT", "默认")))
        try:
            res = llm.chat(messages)
        except Exception as e:  # noqa: BLE001 调用层的失败（429/超时/5xx）一律算基础设施故障
            raise InfraError("%s 调用失败（%s）：%s" % (step, getattr(llm, "model", "")
                                                    or getattr(getattr(llm, "cfg", None), "model", ""), e))
        finally:
            self._last[id(llm)] = time.time()
        self.log("  ← %s 完成：%.1f min，completion_tokens=%s，finish=%s" % (
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

    _NUDGE = ("上一次回复里没有解析到任何文件块。请**只**输出文件内容，严格用下面的格式，"
              "不要加解释文字、不要套 ``` 围栏：\n<<<FILE 文件名>>>\n文件内容\n<<<END>>>\n"
              "本步需要的文件：%s")

    def _call_files(self, llm, step, messages, want, retries=1):
        """调模型 + 解析文件块；一个目标文件都没解析到就带纠正提示重试。

        模型偶发整段不吐 <<<FILE>>> 标记（实测 glm-5.3 有过）。旧代码会静默写入 0 个文件，
        这一轮白跑，而且要等到后面冒烟门报「缺少案例文件」才暴露。协议没守住不是案例的错，
        所以重试用尽后抛 InfraError —— 不记作废、不消耗返修预算。
        """
        for attempt in range(retries + 1):
            msgs = messages if attempt == 0 else (
                messages + [{"role": "user", "content": self._NUDGE % "、".join(sorted(want))}])
            files = parse_files(self._call(llm, step if attempt == 0 else step + "_retry", msgs))
            if set(files) & set(want):
                return files
            self.log("  ⚠ %s 第 %d 次没解析到文件块（期望 %s）"
                     % (step, attempt + 1, "、".join(sorted(want))))
        raise InfraError("%s 连续 %d 次没按 <<<FILE>>> 格式产出 %s（见 trace）"
                         % (step, retries + 1, "、".join(sorted(want))))

    def draft_rules(self, case_dir, subdomain):
        refs, manifest = load_refs(case_dir)
        if refs:
            self.log("  （带 %d 份参考资料：%s）" % (len(refs), "、".join(sorted(refs))))
        files = self._call_files(self.solve_llm, "rules",
                                 rules_messages(subdomain, refs, manifest), {"规则.md"})
        _write_files(case_dir, files, {"规则.md"})

    def write_code(self, case_dir, rules_text, only_missing=False):
        def have_all(group):
            return all(os.path.isfile(os.path.join(case_dir, f)) for f in group)
        if not (only_missing and have_all(LOGIC_FILES)):   # 第①步：逻辑三件
            files = self._call_files(self.solve_llm, "code", code_messages(rules_text),
                                     set(LOGIC_FILES))
            _write_files(case_dir, files, set(LOGIC_FILES))
        if not (only_missing and have_all(MAT_FILES)):      # 第②步：物化（单独一次，防截断）
            sp = os.path.join(case_dir, "solve.py")
            solve_text = _read(sp) if os.path.isfile(sp) else ""
            files = self._call_files(self.solve_llm, "materialize",
                                     materialize_messages(rules_text, solve_text), set(MAT_FILES))
            _write_files(case_dir, files, set(MAT_FILES))

    def write_solve2(self, case_dir, rules_text):
        files = self._call_files(self.solve2_llm, "solve2", solve2_messages(rules_text),
                                 {"solve2.py"})
        _write_files(case_dir, files, {"solve2.py"})

    def repair(self, case_dir, problem):
        cur = {n: _read(os.path.join(case_dir, n)) for n in ("规则.md",) + CODE_FILES
               if os.path.isfile(os.path.join(case_dir, n))}      # 不给 solve2.py，免得锚定
        # 参考资料也给（只读）：修「引用来源」那一节必须看得到原文，否则只能瞎猜
        refs, _ = load_refs(case_dir)
        for fn, txt in refs.items():
            cur["%s/%s（只读，不要输出这个文件）" % (REFS_DIR, fn)] = txt[:6000]
        allow = {"规则.md"} | set(CODE_FILES)
        files = self._call_files(self.solve_llm, "repair", repair_messages(cur, problem), allow)
        return set(_write_files(case_dir, files, allow))


# ---------------- CLI ----------------
def run_build(subdomain=None, out=None, solve_model="gpt-5.5", solve2_model="glm-5.3-flash",
              rounds=3, n=200, seed=0, resume=None, call_gap=30.0, refs=None):
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
            if not subdomain:
                subdomain = json.load(open(old, encoding="utf-8")).get("subdomain")
            os.rename(old, os.path.join(out, "report.%s.json" % time.strftime(
                "%Y%m%d-%H%M%S", time.localtime(os.path.getmtime(old)))))
    if not subdomain:
        print("✗ 需要 --subdomain（ALE 子域 id，如 6.1；或 --resume 一个带 report.json 的目录）", file=sys.stderr)
        return 2
    try:      # 选题合法性前置：子域写错或暂时做不了，别先造完一条再发现没法归类
        sd = subdomains.get(subdomain)
    except KeyError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 2
    if not sd["enabled"]:
        print("✗ 子域 %s %s 暂时做不了（%s：%s）" % (
            sd["id"], sd["name"] or "", sd["blocked_by"], sd["blocked_note"]), file=sys.stderr)
        return 2
    os.environ.setdefault("ALE_LLM_RETRIES", "6")   # 429 退避 8/16/32/60/60s，扛约 3 分钟限流
    os.environ.setdefault("ALE_LLM_MAX_TOKENS", "32000")   # 代码/物化步输出长，默认 16000 会截断（finish=length）
    out = out or os.path.join(cfg.runs_dir, "casegen", "%s-%s-%s" % (
        sd["id"], scaffold._safe_seg(sd["code"] or "unknown", 20),
        time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(out, exist_ok=True)
    if refs:      # 人工抓来的真实口径材料冻结进案例目录；抓取是一次性人工步骤，管线不联网取数
        src = os.path.abspath(os.path.expanduser(refs))
        if not os.path.isdir(src):
            print("✗ --refs 不是目录：%s" % src, file=sys.stderr)
            return 2
        dst = os.path.join(out, "case", REFS_DIR)
        os.makedirs(dst, exist_ok=True)
        for fn in sorted(os.listdir(src)):
            if os.path.isfile(os.path.join(src, fn)):
                shutil.copy(os.path.join(src, fn), os.path.join(dst, fn))
        got, man = load_refs(os.path.join(out, "case"))
        if not got:
            print("✗ --refs 目录里没有可读文件：%s" % src, file=sys.stderr)
            return 2
        _stderr_log("参考资料：%d 份（%s）%s" % (
            len(got), "、".join(sorted(got)),
            "" if man else "；注意 %s 缺失或解析失败，出处信息不会进 prompt" % REFS_MANIFEST))
    _stderr_log("产出目录：%s%s" % (out, "（续跑）" if resume else ""))
    cost = CostTracker()
    prod = LLMProducer(get_client(dataclasses.replace(cfg.llm, model=solve_model)),
                       get_client(dataclasses.replace(cfg.llm, model=solve2_model)),
                       trace_dir=os.path.join(out, "trace"), cost=cost,
                       min_gap=call_gap, log=_stderr_log)
    # 网关故障（503/504/429 耗尽、协议没守住）是瞬时的，不是案例的错：原地带退避自动续跑，
    # 复用已产出的 规则/代码/solve2，不重复花钱。10-13 三次尝试死两次在网关、白耗 59 分钟，
    # 全是因为要人工再敲一次 --resume。
    infra_retry = int(os.environ.get("ALE_CASEGEN_INFRA_RETRY", "2"))
    attempt, rep, code = 0, None, 0
    while True:
        try:
            rep, code = build_case(subdomain, out, prod, max_rounds=rounds, n=n, seed=seed,
                                   resume=bool(resume) or attempt > 0, log=_stderr_log), 0
            break
        except CaseRejected as e:
            rep, code = e.args[0], 1
            break
        except InfraError as e:
            rep = e.args[0] if isinstance(e.args[0], dict) else {"status": "infra_error",
                                                                 "错误": str(e)}
            if attempt >= infra_retry:
                code = 3
                break
            attempt += 1
            back = 30 * attempt
            _stderr_log("网关故障，%ds 后自动续跑（第 %d/%d 次）：%s"
                        % (back, attempt, infra_retry, rep.get("错误")))
            time.sleep(back)
        except FrameworkError as e:
            rep, code = e.args[0], 3
            break
        except RuntimeError as e:
            rep, code = {"status": "rejected", "错误": str(e)}, 1
            break
    rep.update({"模型": {"solve": solve_model, "solve2": solve2_model}, "退出码": code,
                "网关重试次数": attempt,
                "成本RMB": cost.total(), "未计价模型": cost.unpriced_models()})
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    return code


def _batch_report(root, rows, t0):
    acc = [r for r in rows if r["status"] == "accepted"]
    # 网关故障不算案例质量问题，所以产出率给两个：含网关（交付排期用）与不含（案例质量用）
    non_infra = [r for r in rows if r["status"] != "infra_error"]
    dist = {}
    for r in rows:
        k = r["status"] or "无报告"
        dist[k] = dist.get(k, 0) + 1
    secs = sorted(r["耗时秒"] for r in acc if isinstance(r["耗时秒"], (int, float)))
    costs = [r["成本RMB"] for r in rows if isinstance(r["成本RMB"], (int, float))]
    # 返修预算花在哪：按 stage 统计所有非 pass 轮次。只看「停在哪」答不了这个问题。
    stage = {}
    for r in rows:
        for s in (r.get("各轮stage") or []):
            if s and s != "pass":
                stage[s] = stage.get(s, 0) + 1
    n_rep = sum(stage.values()) or 1
    rep = {
        "尝试数": len(rows), "接收数": len(acc),
        "产出率": round(len(acc) / len(rows), 4) if rows else None,
        "产出率_排除网关故障": round(len(acc) / len(non_infra), 4) if non_infra else None,
        "状态分布": dist,
        "返修轮次总数": sum(stage.values()),
        "返修stage分布": dict(sorted(stage.items(), key=lambda kv: -kv[1])),
        "返修stage占比": {k: round(v / n_rep, 4)
                       for k, v in sorted(stage.items(), key=lambda kv: -kv[1])},
        "网关重试总次数": sum(r.get("网关重试次数") or 0 for r in rows),
        "接收耗时秒_中位": secs[len(secs) // 2] if secs else None,
        "接收耗时秒_最长": secs[-1] if secs else None,
        "总耗时秒": round(time.time() - t0),
        "总成本RMB": round(sum(costs), 4) if costs else None,
        "单条可用成本RMB": round(sum(costs) / len(acc), 4) if costs and acc else None,
        "明细": rows,
    }
    with open(os.path.join(root, "batch_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, default=str)
    return rep


def run_batch(sids, repeat=1, out_root=None, **kw):
    """连跑 N 次 build-case，实测「每次尝试产出一条可用数据」的比率。

    500 条的交付排期全靠这个比率推，必须实测、不能估（分工文档雅婷第 1 项，带★）。
    每次尝试一个独立产出目录；每跑完一次就重写 batch_report.json，中途挂掉也有数。
    """
    from .config import PipelineConfig
    cfg = PipelineConfig()
    root = out_root or os.path.join(cfg.runs_dir, "casegen",
                                    "batch-%s" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(root, exist_ok=True)
    plan = [sids[i % len(sids)] for i in range(len(sids) * repeat)]
    rows, t0 = [], time.time()
    _stderr_log("跑批：%d 次尝试，产出根目录 %s" % (len(plan), root))
    for i, sid in enumerate(plan, 1):
        code_seg = scaffold._safe_seg((subdomains.get(sid)["code"] or "unknown"), 20) \
            if sid in subdomains.load()["subdomains"] else scaffold._safe_seg(str(sid), 20)
        out = os.path.join(root, "%02d-%s-%s" % (i, sid, code_seg))
        _stderr_log("═══ 尝试 %d/%d：子域 %s ═══" % (i, len(plan), sid))
        code = run_build(sid, out, **kw)
        rep = {}
        rp = os.path.join(out, "report.json")
        if os.path.isfile(rp):
            with open(rp, encoding="utf-8") as f:
                rep = json.load(f)
        rounds = rep.get("rounds") or []
        rows.append({"序号": i, "子域": sid, "退出码": code, "status": rep.get("status"),
                     "耗时秒": rep.get("耗时秒"), "轮数": len(rounds),
                     "停在": (rounds or [{}])[-1].get("stage"),
                     "各轮stage": [r.get("stage") for r in rounds],
                     "网关重试次数": rep.get("网关重试次数"), "成本RMB": rep.get("成本RMB"),
                     "task_dir": rep.get("task_dir"), "out": out})
        _batch_report(root, rows, t0)
    rep = _batch_report(root, rows, t0)
    print(json.dumps({k: v for k, v in rep.items() if k != "明细"},
                     ensure_ascii=False, indent=2, default=str))
    _stderr_log("跑批报告：%s" % os.path.join(root, "batch_report.json"))
    return 0 if rep["接收数"] else 1


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
        rep = build_case("6.1", os.path.join(tmp, "static"), sp, n=60, hardness=RELAXED)
        check(rep["status"] == "accepted" and len(rep["rounds"]) == 1,
              "静态 producer：好案例 1 轮接收，物化 + qc(N6/N8) 全过")
        check(sp.solve2_inputs == [_read(os.path.join(good, "规则.md"))],
              "独立性（编排层）：交给 solve2 的只有 规则.md 原文")
        try:
            # bug_boundary 是 crosscheck 专用夹具、本就没有 物化.py；补上 good 的 物化.py 做成完整案例，
            # 这样走到 crosscheck 才被拒（而不是先被缺文件守卫拦下）
            bugfull = os.path.join(tmp, "bugfull")
            shutil.copytree(bug, bugfull, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy(os.path.join(good, "物化.py"), os.path.join(bugfull, "物化.py"))
            build_case("6.1", os.path.join(tmp, "bug"), StaticProducer(bugfull), n=60, hardness=RELAXED)
            check(False, "注错案例应被作废")
        except CaseRejected as e:
            check(e.args[0]["rounds"][-1]["stage"] == "crosscheck",
                  "注错案例：crosscheck 拒收 → 作废（不先放行）")
        check(list(inspect.signature(solve2_messages).parameters) == ["rules_text"],
              "独立性（prompt 层）：solve2_messages 只接收 rules_text")

        def blk(names):
            return "".join(_fblock(nm, _read(os.path.join(good, nm))) for nm in names)
        solve_mock = MockLLMClient("mock-solve", scripted={
            "【步骤:规则】": blk(["规则.md"]), "【步骤:代码】": blk(LOGIC_FILES),
            "【步骤:物化】": blk(["物化.py"])})
        solve2_mock = MockLLMClient("mock-solve2", scripted={   # 只有这个 client 认识独立实现步骤
            "【步骤:独立实现】": "```\n" + blk(["solve2.py"]) + "```"})
        trace = os.path.join(tmp, "llm", "trace")
        rep = build_case("6.1", os.path.join(tmp, "llm"),
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
                                        "【步骤:代码】": [blk(LOGIC_FILES)],
                                        "【步骤:物化】": [blk(["物化.py"])],
                                        "【步骤:返修】": [blk(["规则.md"])]})
        s2_llm = SeqClient("seq-solve2", {"【步骤:独立实现】": [bad2, blk(["solve2.py"])]})
        trace = os.path.join(tmp, "repair", "trace")
        rep = build_case("6.1", os.path.join(tmp, "repair"),
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
            build_case("6.1", os.path.join(tmp, "infra"), LLMProducer(FailClient(), FailClient()))
            check(False, "网关故障应抛 InfraError")
        except InfraError as e:
            r = e.args[0]
            check(r["status"] == "infra_error" and r["rounds"] == [] and "429" in r["错误"],
                  "网关故障（429）→ infra_error、不算作废、不消耗返修")

        # 协议守卫：模型整段不吐 <<<FILE>>> 标记（实测 glm-5.3 有过）→ 带纠正提示重试
        class ProtoClient:
            model = "proto"

            def __init__(self, texts):
                self.texts, self.seen = list(texts), []

            def chat(self, messages, **kw):
                from .llm import LLMResult
                self.seen.append(messages[-1]["content"])
                t = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
                return LLMResult(text=t, model=self.model)

        def _draft(texts, tag):
            cd = os.path.join(tmp, tag, "case")
            os.makedirs(cd)
            pc = ProtoClient(texts)
            LLMProducer(pc, MockLLMClient("s2"),
                        trace_dir=os.path.join(tmp, tag, "trace")).draft_rules(cd, "6.1")
            return pc, cd

        pc, cd = _draft(["我先说一下思路……（没有文件块）", blk(["规则.md"])], "proto_ok")
        check(os.path.isfile(os.path.join(cd, "规则.md")) and len(pc.seen) == 2
              and "<<<FILE 文件名>>>" in pc.seen[1],
              "协议守卫：没解析到文件块 → 带纠正提示重试一次 → 规则.md 落盘")
        try:
            pc, _ = _draft(["两次都没有文件块"], "proto_bad")
            check(False, "连续不守协议应抛 InfraError")
        except InfraError as e:
            check("规则.md" in str(e) and len(pc.seen) == 2,
                  "协议守卫：重试用尽 → InfraError（不记作废、不消耗返修）")

        # 参考资料输入口 + 引用来源逐字回溯闸门
        def _refs_case(rules_body, ref_text="限值标准\nEL1 内部欺诈\nalpha 系数 0.15\n", tag="r"):
            cd = os.path.join(tmp, "refs_" + tag, "case")
            os.makedirs(os.path.join(cd, REFS_DIR))
            with open(os.path.join(cd, "规则.md"), "w", encoding="utf-8") as f:
                f.write(rules_body)
            with open(os.path.join(cd, REFS_DIR, "标准表.txt"), "w", encoding="utf-8") as f:
                f.write(ref_text)
            with open(os.path.join(cd, REFS_DIR, REFS_MANIFEST), "w", encoding="utf-8") as f:
                json.dump([{"file": "标准表.txt", "public_source_url": "https://example.gov/x",
                            "抓取日期": "2026-10-14", "说明": "自测用"}], f, ensure_ascii=False)
            return cd

        check(refs_trace_check(os.path.join(tmp, "static", "case")) is None,
              "没有参考资料 → 不设逐字回溯门（纯合成路径不受影响）")
        cd = _refs_case("# 规则\n费率 0.15\n", tag="nosec")
        msg = refs_trace_check(cd)
        check(msg and REFS_SECTION in msg and "标准表.txt" in msg,
              "给了参考资料但规则.md 没有「引用来源」一节 → 退回并列出可用材料")
        cd = _refs_case("# 规则\n%s\n- 0.15 ← 标准表.txt\n- EL1 内部欺诈 ← 标准表.txt\n"
                        % REFS_SECTION, tag="ok")
        check(refs_trace_check(cd) is None, "声明的值能在参考资料原文里逐字命中 → 放行")
        cd = _refs_case("# 规则\n%s\n- 0.18 ← 标准表.txt\n" % REFS_SECTION, tag="fake")
        msg = refs_trace_check(cd)
        check(msg and "0.18" in msg and "找不到" in msg,
              "声明的值在原文里找不到 → 退回（挡住出题模型编造真实口径）")
        cd = _refs_case("# 规则\n%s\n- 0.15 ← 不存在的表.txt\n" % REFS_SECTION, tag="badsrc")
        msg = refs_trace_check(cd)
        check(msg and "不存在的表.txt" in msg, "声明的出处文件不存在 → 退回")
        refs, man = load_refs(cd)
        blk_txt = _refs_block(refs, man)
        check("标准表.txt" in blk_txt and "https://example.gov/x" in blk_txt
              and REFS_SECTION in blk_txt and "业务流水仍然由参数生成器合成" in blk_txt,
              "参考资料注入段带原文 + 出处 + 「口径照抄、流水合成」约束")
        m = rules_messages("6.1", refs, man)[1]["content"]
        check("## 参考资料" in m and "逐字采用" in m,
              "规则步 prompt 注入参考资料（无参考资料时该段为空串）")
        check("## 参考资料" not in rules_messages("6.1")[1]["content"],
              "不给参考资料时 prompt 不含参考资料段")

        # 跑批统计：产出率两种口径（含/不含网关故障）
        rows = [{"status": "accepted", "耗时秒": 300, "成本RMB": 1.0, "网关重试次数": 0},
                {"status": "rejected", "耗时秒": 900, "成本RMB": 2.0, "网关重试次数": 0},
                {"status": "infra_error", "耗时秒": 60, "成本RMB": 0.5, "网关重试次数": 2},
                {"status": "accepted", "耗时秒": 600, "成本RMB": 1.5, "网关重试次数": 1}]
        br = _batch_report(tmp, rows, time.time())
        check(br["尝试数"] == 4 and br["接收数"] == 2 and br["产出率"] == 0.5
              and br["产出率_排除网关故障"] == round(2 / 3, 4) and br["网关重试总次数"] == 3
              and br["单条可用成本RMB"] == 2.5 and br["接收耗时秒_中位"] == 600,
              "跑批统计：产出率 0.5 / 排除网关 0.6667 / 单条可用成本 2.5 元")

        # 地板白拿分 → framework_error，不返修（临时把地板合成器换成空操作来模拟）
        orig = casegen.floor.synthesize
        casegen.floor.synthesize = lambda d, seed=0: []
        try:
            build_case("6.1", os.path.join(tmp, "fw"), StaticProducer(good), n=60, hardness=RELAXED)
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
        rep = build_case("6.1", os.path.join(tmp, "static"), Boom(), n=60, resume=True,
                         hardness=RELAXED)
        check(rep["status"] == "accepted" and rep["resumed"], "--resume：已有文件全复用，零次模型调用即接收")

        # 缺文件守卫：物化.py 没写出来（如上一步被截断）→ smoke 明确报缺文件，不抛 traceback、不误报难度
        check(smoke(good) is None, "冒烟门：齐全的案例目录不报缺文件")
        import tempfile as _tf
        partial = _tf.mkdtemp(prefix="casegen_partial_")
        try:
            for nm in LOGIC_FILES:    # 只有逻辑三件，缺 物化.py
                shutil.copy(os.path.join(good, nm), os.path.join(partial, nm))
            msg = smoke(partial)
            check(msg is not None and "缺少案例文件" in msg and "物化.py" in msg and "Traceback" not in msg,
                  "缺文件守卫：缺 物化.py → 明确报「缺少案例文件：物化.py」，不抛 traceback")
        finally:
            shutil.rmtree(partial, ignore_errors=True)

        # 分两次产代码 + 续跑只补缺失：物化.py 缺失时，write_code 只调「物化」这一次、不重跑逻辑三件
        split = os.path.join(tmp, "split")
        os.makedirs(os.path.join(split, "case"))
        for nm in ("规则.md",) + LOGIC_FILES + ("solve2.py",):   # 逻辑三件已在，只缺 物化.py
            shutil.copy(os.path.join(good, nm), os.path.join(split, "case", nm))
        mat_only = SeqClient("seq-mat", {"【步骤:物化】": [blk(["物化.py"])],
                                         "【步骤:代码】": ["不该被调用"]})
        rep = build_case("6.1", split, LLMProducer(mat_only, MockLLMClient("s2")),
                         n=60, resume=True, hardness=RELAXED)
        check(rep["status"] == "accepted"
              and os.path.isfile(os.path.join(split, "case", "物化.py")),
              "续跑分步补齐：缺 物化.py → 只调「物化」一步补回（逻辑三件不重跑）→ 接收")

        # 同一 client 调用间隔
        m = MockLLMClient("gap")
        p = LLMProducer(m, m, min_gap=0.3)
        t0 = time.time()
        p._call(m, "a", [{"role": "user", "content": "x"}])
        p._call(m, "b", [{"role": "user", "content": "x"}])
        check(time.time() - t0 >= 0.3, "同一模型两次调用之间按 min_gap 等待（防 429）")

        # 难度门：默认门拒收小范例（2 份材料 / 4 行 / 「单价等于封顶」随机世界里从不出现），停在 hardness
        try:
            build_case("6.1", os.path.join(tmp, "soft"), StaticProducer(good), n=60)
            check(False, "默认难度门应拒收小范例")
        except CaseRejected as e:
            r0 = e.args[0]["rounds"][0]
            pb = r0["problem"] or ""
            check(r0["stage"] == "hardness" and "份材料" in pb and "选不出达标世界" in pb
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

        # 附件字符数门：给一个极小上限，任何附件都超 → 难度门应报「超过上限」
        tiny = hardness_check(big, Hardness(min_traps=2, max_attachment_chars=50))
        check(any("超过上限" in p for p in tiny),
              "附件字符数门：附件超过上限 → 难度门报「超过上限」%s" % ("；" + ";".join(tiny) if tiny else ""))

        # 框架挑 INSTANCE：物化不写 INSTANCE 时，choose_instance 从生成器里挑一个覆盖所有 TRAP 的世界
        noinst = os.path.join(tmp, "noinst_case")
        shutil.copytree(good, noinst, ignore=shutil.ignore_patterns("__pycache__"))
        # 末尾覆盖：INSTANCE 置空（后定义生效，省得去拆多行字面量），并去掉随机世界碰不到的「单价等于封顶」
        with open(os.path.join(noinst, "物化.py"), "a", encoding="utf-8") as f:
            f.write("\nINSTANCE = None\nTRAPS = {k: v for k, v in TRAPS.items() if k != '单价等于封顶'}\n")
        picked = choose_instance(noinst, Hardness(min_files=0, min_main_rows=0, min_ref_rows=0,
                                                  min_traps=2, trap_ratio=0, sample=50))
        mni = casegen._load(os.path.join(noinst, "物化.py"), "_cb_ni")
        check(isinstance(picked, dict) and getattr(mni, "INSTANCE", None) is None
              and len(mni.TRAPS) == 2 and all(_safe_trap(fn, picked) for fn in mni.TRAPS.values()),
              "框架挑 INSTANCE：物化无 INSTANCE 时从生成器扫出一个覆盖所有 TRAP 的世界")

        # 接收报告带后续测题计划（当前生题不执行 N7；maxSteps 按公式、路径绝对且已引号）+ 数据集 sidecar 草稿
        rep = build_case("6.1", os.path.join(tmp, "n7"), StaticProducer(good), n=60, hardness=RELAXED)
        n7 = rep.get("后续测题计划") or {}
        want = max(20, -(-(n7.get("附件数", 0) + n7.get("交付数", 0)) * 3 // 2))
        check(n7.get("maxSteps") == want == 20 and "N7_MODE=agent" in n7.get("便宜档", "")
              and "ALE_LLM_MODEL=glm-5.3-flash" in n7["便宜档"] and "N7_TIER=前沿档" in n7.get("前沿档", "")
              and ("N7_TASK='%s'" % os.path.abspath(rep["task_dir"])) in n7["便宜档"],
              "接收报告带 N7 agent 档命令：maxSteps=附件+交付+50%(最低20)、任务路径绝对且加引号")
        side = json.load(open(os.path.join(rep["task_dir"], "dataset.json"), encoding="utf-8"))
        check(side.get("ALE子域") == "6.1 Accounting & Finance" and side.get("职位") == ""
              and side.get("自动化类型") == "闭世界" and side.get("提交人") == ""
              and set(side.get("_draft", {})) == set(casegen.DRAFT_FIELDS),
              "物化产数据集 sidecar 草稿 dataset.json（ALE子域/闭世界/构建日期，_draft 标待人核字段）")
        card = json.load(open(os.path.join(rep["task_dir"], "task_card.json"), encoding="utf-8"))
        check(card["taxonomy"] == {"domain_id": "6", "domain_code": "business_finance",
                                   "subdomain_id": "6.1", "subdomain_code": "accounting",
                                   "subdomain_name": "Accounting & Finance"}
              and card["领域"] == "business_finance" and card["子域"].startswith("6.1 "),
              "taxonomy 由框架按 ALE 码表自动写卡，不采信模型自己写的 TASK[领域]")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = all(res)
    print("\n%s" % ("casegen 生成半段自测通过" if ok else "casegen 生成半段自测失败"))
    return ok


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="pipeline.casegen_build")
    ap.add_argument("--subdomain", help="ALE 子域 id，如 6.1")
    ap.add_argument("--out")
    ap.add_argument("--resume", help="续跑某个产出目录（复用已有 规则/代码/solve2）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest or not (a.subdomain or a.resume):
        return 0 if _selftest() else 1
    return run_build(a.subdomain, a.out, resume=a.resume)


if __name__ == "__main__":
    sys.exit(main())
