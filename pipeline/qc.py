"""质量闸门节点：N6 三道校验 / N7 盲解 / N8 地板审计。

三者都以「一条已造好的任务」为输入，是数据能否进库的关卡（非模型评测）。
N6/N8 是确定性纯代码校验；N7 需要 LLM 当考生。
"""
import json
import os
import re
import shutil
import tempfile

from . import harness


def n6_verify(task, pass_threshold=1.0):
    """N6 进库校验（确定性，判分契约 §8）：
      1) 四档夹具实测分 == 任务卡声明的 fixtureScores（老任务只有正负两档时退化为两档）
      2) 判分可复现：同一 output 判两次分数一致
      3) 判分器独立：import 图里没有造附件/生成器模块
      4) 数据已填：assets/ 无 TODO 占位
      5) 判分依据可溯源：scoringBasis 非空
      6) 判分依据真能追到附件：material 类的 ref 指到的文件在 input 里存在
      7) 题面不泄标准答案：reference 里的数值不许出现在 prompt / 交付要求里
    """
    checks = []

    fx_ok, fx_detail, fx_fields = _check_fixtures(task)
    checks.append({"检查": "夹具实测==声明", "通过": fx_ok, "明细": fx_detail})


    wd = tempfile.mkdtemp(prefix="ale_n6_")
    harness.setup_env(task, wd)
    harness.golden_solver(task, wd)
    e1 = harness.evaluate(task, wd)
    e2 = harness.evaluate(task, wd)
    repro = e1.score == e2.score and not e1.fault
    checks.append({"检查": "判分可复现", "通过": repro, "score": [e1.score, e2.score],
                   **({"判分器故障": e1.fault[:300]} if e1.fault else {})})

    ind_ok, ind_why = _check_independence(task)
    checks.append({"检查": "判分器独立(import 图干净)", "通过": ind_ok, "说明": ind_why})

    ready = not _has_placeholder(os.path.join(task.dir, "assets"))
    checks.append({"检查": "数据已填(无 TODO 占位)", "通过": ready})

    basis = task.card.get("scoringBasis")
    checks.append({"检查": "判分依据可溯源(scoringBasis 非空)", "通过": bool(basis),
                   "条数": len(basis) if isinstance(basis, list) else 0})

    tr_ok, tr_detail = _check_basis_traceable(task)
    checks.append({"检查": "判分依据能追到附件(material.ref 可定位)", "通过": tr_ok,
                   "明细": tr_detail})

    lk_ok, lk_detail = _check_no_answer_leak(task)
    checks.append({"检查": "题面不泄标准答案", "通过": lk_ok, "明细": lk_detail})

    ok = all(c["通过"] for c in checks)
    return ok, {"task_id": task.task_id, "checks": checks,
                "_fixtureDetails": fx_fields}




def n8_floor_audit(task, floor=0.0):
    """N8 地板审计（判分契约 §8）：地板＝**同结构随机假答案**，不是空解。

    空解得 0 只是最低门槛，挡不住「文件名/表头/行键/字段全对、数值全错」这种白拿分——
    而那恰恰是结构化表格类任务最常见的漏分口。本节跑**两条地板**、取较高分：
      ① 作者提供的 `output_test_random` 夹具（作者自证义务，判分契约 §4）
      ② **程序化合成**的同结构假答案（`pipeline/floor.py` 从 golden 机械改写）
    之所以要②：作者造的地板证不了作者自己判分器的清白。「行键列被计进分母、
    照抄题面清单白拿约一成」这个缺陷在两条数据上各出现一次、两次都靠人工发现。
    """
    probes = []

    fx = os.path.join(task.dir, "assets", "reference", "fixtures", "output_test_random")
    if os.path.isdir(fx):
        probes.append(("作者random夹具",
                       harness.run_task(task, solver=harness.fixture_solver(
                           "output_test_random")).score))
    else:
        probes.append(("空解(缺random夹具,证据不完整)",
                       harness.run_task(task, solver=harness.empty_solver).score))

    try:
        probes.append(("程序化合成假答案",
                       harness.run_task(task, solver=harness.synth_floor_solver).score))
    except Exception as e:  # noqa: BLE001 合成地板跑不起来要显式记下，不能静默跳过
        probes.append(("程序化合成假答案(未能运行:%s)" % type(e).__name__, None))

    scored = [(k, v) for k, v in probes if isinstance(v, (int, float))]
    worst_kind, worst = max(scored, key=lambda kv: kv[1])
    ok = worst <= floor and len(scored) == len(probes)
    detail = {"task_id": task.task_id, "floor_score": worst, "地板类型": worst_kind,
              "各档地板": {k: v for k, v in probes}, "通过": ok}
    if worst > floor:
        detail["说明"] = "%s 得分 %.4f 过高：判分器白送分，需修（结构对、值全错也该是 0）" % (
            worst_kind, worst)
    elif len(scored) != len(probes):
        detail["说明"] = "地板证据不完整：有探针没跑起来"
    else:
        detail["说明"] = "两档地板都为 0，判分器不给「格式对内容错」送分"
    return ok, detail


def difficulty_gate(task):
    """§6.3 入库门槛（难度）—— 原来 qc 的「进库」只查 N6/N8，难度靠人手动确认。

    4 条数据时手动还行，几个人同时铺量就一定会把「进库=True」当成「这条合格」，
    于是便宜模型能满分的水题混进来。这里按契约 §6.3 程序化判：
      便宜档 glm-5.3-flash  fullPassRate 必须 = 0
      前沿档 GPT-5.5        fullPassRate ≤ 1/3
    三条硬规矩（契约 §6.3）：
      ① 门槛只认 agent 档（toolset=agent-file-tools），单发盲解只作预筛、不算数；
      ② 边界补跑：fpr=0 → 3 跑即可；0<fpr≤1/3（擦线）必须补到 6 跑，否则靠运气过；
      ③ 判分器版本必须对得上——改了判分器就等于换了考试，旧难度数字作废（§6.2 把
         scorerVersion 钉进测量条目正是为此）。
    未测 → 不通过（显式写「难度未测」，不是默默算过）。
    """
    tiers = (("便宜档", "glm-5.3-flash", lambda r: r == 0, "必须 = 0"),
             ("前沿档", "gpt-5.5", lambda r: r <= 1.0 / 3 + 1e-9, "≤ 1/3"))
    md = task.card.get("measuredDifficulty")
    if not isinstance(md, list) or not md:
        return False, {"task_id": task.task_id, "通过": False,
                       "说明": "难度未测（measuredDifficulty 为空）——未测 ≠ 达标"}
    cur_ver = str(((task.card.get("evaluation") or {}).get("scorerVersion") or "")).strip()
    if not cur_ver:
        return False, {"task_id": task.task_id, "通过": False,
                       "说明": "evaluation.scorerVersion 未声明：无法判断难度数字是不是"
                               "当前判分器测的（契约 §6.2）"}
    rows, ok_all = [], True
    for tier, model, rule, desc in tiers:
        # §6.3：门槛只认 agent 档。单发盲解（single-shot-blind）只作便宜预筛、不算入库依据。
        hit = [e for e in md if str(e.get("model", "")).lower() == model
               and str(e.get("scorerVersion", "")).strip() == cur_ver
               and str(e.get("toolset", "")) == "agent-file-tools"]
        if not hit:
            same_model = [e for e in md if str(e.get("model", "")).lower() == model]
            cur_any = [e for e in same_model
                       if str(e.get("scorerVersion", "")).strip() == cur_ver]
            if cur_any:   # 有当前版本的测量，但都是单发预筛、没有 agent 档
                why = ("只有单发预筛（toolset=%s），缺 agent 档测量——契约 §6.3 门槛只认 "
                       "agent-file-tools" % [e.get("toolset") for e in cur_any])
            elif same_model:  # 有过 agent 档测量，但判分器已改版
                why = ("判分器已改版(当前 %s)，旧测量 scorerVersion=%s 不再算数，需重测"
                       % (cur_ver, [e.get("scorerVersion") for e in same_model]))
            else:
                why = "未测"
            rows.append({"档": tier, "基准模型": model, "通过": False, "说明": why})
            ok_all = False
            continue
        e = sorted(hit, key=lambda x: str(x.get("measuredAt", "")))[-1]  # 取最近一次
        fpr, n = e.get("fullPassRate"), e.get("nRuns") or 0
        miss = [k for k in ("model", "toolset", "budget", "scorerVersion", "nRuns")
                if not e.get(k)]
        # §6.3 边界补跑：fpr=0 → 3 跑即可；0<fpr≤1/3（擦线）必须补到 6 跑。
        need_n = 3 if (isinstance(fpr, (int, float)) and fpr == 0) else 6
        ok = isinstance(fpr, (int, float)) and rule(fpr) and n >= need_n and not miss
        note = ({"说明": "nRuns=%d < %d（契约 §6.3：fpr=0 需 3 跑，0<fpr≤1/3 需补到 6 跑）"
                         % (n, need_n)} if n < need_n else {})
        rows.append({"档": tier, "基准模型": model, "fullPassRate": fpr, "nRuns": n,
                     "meanScore": e.get("meanScore"), "toolset": e.get("toolset"),
                     "scorerVersion": e.get("scorerVersion"), "门槛": desc, "通过": ok,
                     **({"缺字段": miss} if miss else {}),
                     **note})
        ok_all = ok_all and ok
    return ok_all, {"task_id": task.task_id, "通过": ok_all, "判分器版本": cur_ver,
                    "各档": rows}





def _save_trace(trace_dir, work_dir=None, **parts):
    """落盘一次测量的全部轨迹。判分契约 §6 的数字要可复核，就得留下原始材料。

    原来 N7 只留 score/passed，盲解工作目录是临时目录、路径都没记 —— 一旦某条
    分数看着不对（比如「便宜档怎么会 0 分」），没有任何东西可查：既不知道喂给
    模型的是什么，也不知道它到底交了什么、判分器逐字段判成什么。
    `parts` 里的字符串按文件名写出；work_dir 给了就把它的 output/ 整树拷过来。
    """
    if not trace_dir:
        return None
    os.makedirs(trace_dir, exist_ok=True)
    for name, content in parts.items():
        if content is None:
            continue
        is_text = isinstance(content, str)
        p = os.path.join(trace_dir, name + (".txt" if is_text else ".json"))
        with open(p, "w", encoding="utf-8") as f:
            f.write(content if is_text
                    else json.dumps(content, ensure_ascii=False, indent=2))

    if work_dir:
        src = os.path.join(work_dir, "output")
        if os.path.isdir(src):
            dst = os.path.join(trace_dir, "output")
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
    return trace_dir


def n7_blind_solve(task, llm, cost=None, trace_dir=None):
    """N7 盲解：把 prompt+input 交给 LLM 当考生单发试做，产出 output 再判分。
    只给题面与输入、不给 reference。用于估基线通过率、定难度层。

    ★ 本节的诚实性是整条难度门槛的地基：**任何「不是 agent 不会做」的 0 分都必须
    抛成测量故障**，否则「便宜档 fullPassRate=0 过门槛」会被噪声刷过去、水题进库。
    三类故障：① 附件被截断（题目实际不可解）② 模型没按 JSON 协议交卷（我们自己
    定的单发协议，不是任务要求）③ 判分器自身崩了。

    给了 trace_dir 就把 prompt / 原始响应 / 产出文件 / 判分器完整输出（含逐字段
    明细）落盘，供事后复核；故障时也落盘，否则最该查的那几次反而什么都没留下。
    """
    wd = tempfile.mkdtemp(prefix="ale_n7_")
    harness.setup_env(task, wd)
    inputs, truncated = _read_inputs(os.path.join(wd, "input"))
    if truncated and os.environ.get("ALE_N7_ALLOW_TRUNCATION", "") != "1":
        raise RuntimeError(
            "附件被截断，盲解看不到完整材料＝题目不可解，这样测出来的难度是假的：%s。"
            "调大 ALE_N7_INPUT_LIMIT，或确认可接受后设 ALE_N7_ALLOW_TRUNCATION=1。"
            % json.dumps(truncated, ensure_ascii=False))
    prompt = _blind_prompt(task, inputs)
    msgs = [
        {"role": "system", "content": "你是完成真实工作任务的 agent，只依据给定输入作答。"
         "最终必须只输出一个 JSON 对象：键=相对 output/ 的文件名，值=该文件完整文本内容。"},
        {"role": "user", "content": prompt},
    ]
    res = llm.chat(msgs)
    if cost:
        cost.add("N7", res)
    meta = {"model": getattr(res, "model", ""), "finish_reason": res.finish_reason,
            "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens,
            "reasoning_tokens": res.reasoning_tokens, "work_dir": wd}
    _save_trace(trace_dir, prompt=prompt, response_raw=res.text, meta=meta)
    # 诚实性闸门①：content 为空、或被 max_tokens 截断（finish_reason=length）＝测量故障，
    # 不是「agent 不会做」。推理型模型会把预算烧在 reasoning_content 上、content 返空。
    if not res.text.strip() or res.finish_reason == "length":
        raise RuntimeError(
            "盲解无有效产出（疑似推理占满预算或超时截断）：finish_reason=%s, "
            "completion_tokens=%d, reasoning_tokens=%d, content长度=%d。"
            "换直答型模型或调大 ALE_LLM_MAX_TOKENS（注意过大可能触发网关生成超时）。"
            % (res.finish_reason, res.completion_tokens, res.reasoning_tokens, len(res.text)))
    n_files = _materialize(res.text, os.path.join(wd, "output"))
    # 诚实性闸门②：一个文件都没落盘＝模型没按单发 JSON 协议交卷（散文、围栏外夹字、
    # 半截 JSON）。这是**我们自己这套协议的格式噪声**，不是专业能力，必须记故障。
    # 实测便宜档曾有 2/3 跑挂在这里，若静默记 0 分，水题会假装过门槛。
    if n_files == 0:
        raise RuntimeError(
            "盲解产出解析不出任何文件（模型没按单发 JSON 协议交卷，content 长度 %d）：%s"
            % (len(res.text), res.text.strip()[:300].replace("\n", " ")))
    r = harness.evaluate(task, wd)
    _save_trace(trace_dir, work_dir=wd,
                score=dict(r.raw or {}, _score=r.score, _passed=r.passed,
                           _errors=r.errors, _fault=r.fault))
    # 诚实性闸门③：判分器自己崩了也不是 agent 得 0 分（判分契约 §2）。
    if r.fault:
        raise RuntimeError("判分器故障，本跑不计入难度：" + r.fault[:300])
    return True, {"task_id": task.task_id, "score": r.score,
                  "passed": r.passed, "写出文件数": n_files,
                  "trace": trace_dir}




# ---------------- helpers ----------------
FIXTURE_ORDER = ("output_test_pos", "output_test_neg",
                 "output_test_random", "output_test_partial", "output_test_fabricated")
# 造附件/生成器模块：判分器 import 了这些就不是独立复算，同一个 bug 会自我验证通过
FORBIDDEN_IMPORTS = ("case_data", "gen_all", "attachments", "make_fixtures",
                     "recompute_reference", "scaffold", "generate")


def _check_fixtures(task):
    """跑任务卡声明的每一档夹具，比对实测分与声明分（判分契约 §4）。

    没声明 fixtureScores、或声明成描述字符串（ALE 的散文债）→ 直接不通过。
    顺带把判分器的**逐字段明细**收出来（第三个返回值）：夹具分对不上时，
    不看逐字段根本不知道是哪一项判错了。
    """
    ev = task.card.get("evaluation")
    if not isinstance(ev, dict):
        return False, "任务卡 evaluation 不是结构化对象", {}
    fs = ev.get("fixtureScores")
    if not isinstance(fs, dict) or not fs:
        return False, "缺 fixtureScores", {}
    fixdir = os.path.join(task.dir, "assets", "reference", "fixtures")
    out, ok_all, details = [], True, {}
    for name in FIXTURE_ORDER:
        if name not in fs:
            continue
        spec = fs[name]
        if not isinstance(spec, dict) or "expected" not in spec:
            out.append("%s: 声明不是 {expected, observed} 对象（散文债）" % name)
            ok_all = False
            continue
        exp = float(spec["expected"])
        d = os.path.join(fixdir, name)
        if os.path.isdir(d):
            res = harness.run_task(task, solver=harness.fixture_solver(name))
        elif name == "output_test_pos":
            res = harness.run_task(task, solver=harness.golden_solver)
        elif name == "output_test_neg":
            res = harness.run_task(task, solver=harness.neg_solver)
        else:
            out.append("%s: 夹具目录不存在" % name)
            ok_all = False
            continue
        got = res.score
        details[name] = {"score": got, "passed": res.passed, "errors": res.errors,
                         "details": res.details}
        hit = abs(got - exp) < 1e-9
        ok_all = ok_all and hit
        out.append("%s: 实测 %s / 声明 %s %s" % (name, got, exp, "" if hit else "← 不符"))
    if len(out) < 4:
        out.append("只有 %d 档夹具，契约 §4 要求四档（定性型五档）" % len(out))
        ok_all = False
    return ok_all, out, details



def _check_independence(task):
    """判分器独立性：解析 import 图，不许 import 造附件/生成器模块（判分契约 §8）。

    旧做法是 grep evaluate() 函数体里有没有 `input` 字样——那既挡不住 import，
    又会把「读 --output/--reference」的正常代码误判，近乎无意义。
    """
    ev = task.card.get("evaluation") or {}
    cand = [task.main_py]
    scorer = ev.get("scorer") if isinstance(ev, dict) else None
    if scorer:
        cand.append(os.path.join(task.dir, scorer))
    bad = []
    for p in cand:
        if not os.path.isfile(p):
            continue
        src = open(p, encoding="utf-8", errors="ignore").read()
        mods = set(re.findall(r"^\s*(?:import|from)\s+([\w.]+)", src, re.M))
        for m in mods:
            head = m.split(".")[0]
            if head in FORBIDDEN_IMPORTS:
                bad.append("%s import 了 %s" % (os.path.basename(p), m))
    if bad:
        return False, bad
    return True, "查了 %s 的 import 图，无造附件/生成器依赖" % "、".join(
        os.path.basename(p) for p in cand if os.path.isfile(p))


# ref 里的文件名 token：一直吃到扩展名为止，只在空白/逗号/加号/井号处断开。
# 注意不能把全角括号当分隔符——真实附件名里就有「施工合同专用条款（摘录）.docx」。
_REF_FILE_RE = re.compile(
    r"[^\s,，+#]+\.(?:xlsx|xls|csv|docx|doc|pdf|md|txt|json)", re.I)



def _input_basenames(task):
    d = os.path.join(task.dir, "assets", "input")
    out = set()
    for root, _, files in os.walk(d):
        for f in files:
            out.add(f.lower())
    return out


def _check_basis_traceable(task):
    """判分依据真能追到地方（判分契约 §7 核心纪律的机械化）。

    原来只查 scoringBasis 非空——那挡不住「ref 只写个文件名/写个查不到的出处」，
    0917 审出的高频病「核心依据只写文件名」会原样复发。这里按 source 分别查：
      material → ref 里至少有一个文件名能在 assets/input 下找到（或显式写「题面」）
      external → ref 必须是可点开的 http(s) 链接（证据链本身由 N2a 机械核验）
    """
    basis = task.card.get("scoringBasis")
    if not isinstance(basis, list) or not basis:
        return False, ["scoringBasis 为空"]
    names = _input_basenames(task)
    bad = []
    for i, e in enumerate(basis):
        if not isinstance(e, dict):
            bad.append("第%d条不是对象" % (i + 1))
            continue
        src, ref = str(e.get("source", "")), str(e.get("ref", "")).strip()
        if not ref:
            bad.append("第%d条 ref 为空" % (i + 1))
        elif src == "material":
            toks = [os.path.basename(t).lower() for t in _REF_FILE_RE.findall(ref)]
            # 判分依据也可以直接出自题面（契约 §7 的「题面材料里明写的约定规则」），
            # 这类 ref 写成「题面 …」或「task_card.prompt …」，不该被当成指不到附件。
            from_prompt = "题面" in ref or "prompt" in ref.lower()
            if not any(t in names for t in toks) and not from_prompt:
                bad.append("第%d条 ref 指不到任何 input 附件、也没说是题面：%s" % (i + 1, ref[:80]))

        elif src == "external":
            if "http" not in ref:
                bad.append("第%d条 source=external 但 ref 不是链接：%s" % (i + 1, ref[:80]))
        else:
            bad.append("第%d条 source 非法（只许 material / external）：%r" % (i + 1, src))
    if bad:
        return False, bad
    return True, ["%d 条依据全部可定位" % len(basis)]


def _gold_numbers(task, lo=1000.0):
    """标准答案里的「有辨识度的数」：|v|≥lo。夹具目录排除（夹具本就是答案的变体）。"""
    ref = os.path.join(task.dir, "assets", "reference")
    out = set()
    for root, _, files in os.walk(ref):
        if "fixtures" in root.split(os.sep):
            continue
        for f in files:
            try:
                text = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for m in re.findall(r"-?\d[\d,]*\.?\d*", text):
                try:
                    v = float(m.replace(",", ""))
                except ValueError:
                    continue
                if abs(v) >= lo and not (1900 <= v <= 2100):
                    out.add(v)
    return out


def _check_no_answer_leak(task):
    """题面不许印标准答案（harbor 的旧伤：示例块抄了真数值，照抄即满分）。

    判定口径：出现在 reference 里、又出现在 prompt / 交付要求里、且**不在 input 附件里**
    的数 —— 在附件里的数是给定材料，题面复述它不是泄答案。
    白名单：`元数据.题面允许数值`（显式声明，带理由）。
    """
    gold = _gold_numbers(task)
    if not gold:
        return True, ["标准答案里没有 ≥1000 的数，本检查不适用"]
    seen_in_input = set()
    try:
        wd = tempfile.mkdtemp(prefix="ale_leak_")
        harness.setup_env(task, wd)
        inputs, _ = _read_inputs(os.path.join(wd, "input"), limit=10 ** 9)
        itext = "\n".join(inputs.values()).replace(",", "")
        for g in gold:
            if re.search(r"(?<![\d.])%s(?![\d])" % re.escape("%g" % g), itext):
                seen_in_input.add(g)
    except Exception:  # noqa: BLE001 读不出附件就不做排除，宁可多报不漏报
        seen_in_input = set()
    allow = set()
    for v in (task.card.get("元数据") or {}).get("题面允许数值") or []:
        try:
            allow.add(float(v))
        except (TypeError, ValueError):
            pass
    ptext = (json.dumps(task.card.get("prompt", ""), ensure_ascii=False)
             + json.dumps(task.card.get("交付要求", {}), ensure_ascii=False)).replace(",", "")
    hits = sorted(g for g in gold - seen_in_input - allow
                  if re.search(r"(?<![\d.])%s(?![\d])" % re.escape("%g" % g), ptext))
    if hits:
        return False, ["题面出现了标准答案里的数（照抄即得分）：%s" % hits[:12],
                       "若确属题面必须给出的已知量，写进 元数据.题面允许数值 并注明理由"]
    return True, ["gold 大数 %d 个，题面命中 0 个（附件内已给的数 %d 个不计）"
                  % (len(gold), len(seen_in_input))]


def _has_placeholder(root):

    """扫描 assets/ 是否残留脚手架占位符——用于识别"骨架已建但数据未填"的任务。"""
    if not os.path.isdir(root):
        return False
    for r, _, fs in os.walk(root):
        for f in fs:
            try:
                text = open(os.path.join(r, f), encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if "__TODO__" in text or "TODO: 填" in text:
                return True
    return False


def _evaluate_body(main_py):
    src = open(main_py, encoding="utf-8").read()
    m = re.search(r"def evaluate\(.*?\):(.*?)(?:\ndef \w|\Z)", src, re.S)
    return m.group(1) if m else ""


def _xlsx_text(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append("# sheet: %s" % ws.title)
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if any(cells):
                lines.append("\t".join(cells))
    return "\n".join(lines)


def _docx_text(path):
    """docx 取文本。本机没装 python-docx 时退回 zipfile 解 document.xml（同 kw_docx）。"""
    try:
        import docx
        return "\n".join(p.text for p in docx.Document(path).paragraphs if p.text.strip())
    except ImportError:
        import re as _re
        import zipfile
        x = zipfile.ZipFile(path).read("word/document.xml").decode("utf8")
        x = _re.sub(r"</w:p>", "\n", x)
        x = _re.sub(r"</w:tc>", "\t", x)
        return "\n".join(ln for ln in _re.sub(r"<[^>]+>", "", x).splitlines() if ln.strip())


def _pdf_text(path):
    """pdf 取文本。N4 造的签证单/指令类附件是 PDF，不解出文字盲解就只能看到乱码。"""
    import pypdf
    return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(path).pages)


def _read_inputs(input_dir, limit=None):
    """把 input/ 文件读成文本喂给盲解 agent：xlsx→制表符表格、docx/pdf→段落文本，其余按文本读。

    整树遍历：真实附件带目录分层，键用相对路径，agent 才知道文件在哪一层。
    返回 (内容字典, 被截断的文件清单)——**截断必须被看见**：附件被悄悄砍掉一半，
    题目就变成不可解，而盲解分数会低得像「这题很难」，难度测量从此失真。
    """
    limit = int(os.environ.get("ALE_N7_INPUT_LIMIT", 8000)) if limit is None else limit
    out, truncated = {}, []
    if not os.path.isdir(input_dir):
        return out, truncated
    paths = []
    for root, dirs, files in os.walk(input_dir):
        dirs.sort()
        for fn in sorted(files):
            paths.append(os.path.relpath(os.path.join(root, fn), input_dir))
    for name in sorted(paths):
        p = os.path.join(input_dir, name)
        ext = name.lower().rsplit(".", 1)[-1] if "." in os.path.basename(name) else ""
        try:
            if ext == "xlsx":
                text = _xlsx_text(p)
            elif ext == "docx":
                text = _docx_text(p)
            elif ext == "pdf":
                text = _pdf_text(p)
            else:
                text = open(p, encoding="utf-8", errors="ignore").read()
        except Exception as e:  # noqa: BLE001 读取失败不阻断盲解
            out[name] = "[无法读取 %s：%s]" % (name, type(e).__name__)
            continue
        if len(text) > limit:
            truncated.append({"file": name, "原长": len(text), "上限": limit})
            text = text[:limit]
        out[name] = text
    return out, truncated



def _blind_prompt(task, inputs):
    parts = [task.card.get("prompt", ""), "", "## 交付要求",
             json.dumps(task.card.get("交付要求", {}), ensure_ascii=False, indent=2),
             "", "## input/ 文件内容"]
    for name, content in inputs.items():
        parts.append("### %s\n%s" % (name, content))
    return "\n".join(parts)


def _extract_json_obj(text):
    """从模型回复里尽量稳地抠出 {文件名: 内容} 对象。

    盲解 agent 常把 JSON 包进 ```json 围栏、或 JSON 后面还跟一段散文，
    原来一句贪婪 `\\{.*\\}` 对这些都会解析失败 → 写出 0 文件（假 0 分）。
    依次试：整段直接解析 → ```围栏内 → 首个 { 起平衡括号扫描 → 贪婪首尾花括号。
    只是更稳地解析模型自己的产出，不改任何内容、不泄答案。
    """
    cands = [text.strip()]
    for m in re.finditer(r"```(?:json)?\s*(.+?)```", text, re.S | re.I):
        cands.append(m.group(1).strip())
    start = text.find("{")
    if start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                esc = (ch == "\\" and not esc)
                if ch == '"' and not esc:
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    cands.append(text[start:i + 1])
                    break
    gm = re.search(r"\{.*\}", text, re.S)
    if gm:
        cands.append(gm.group(0))
    for c in cands:
        try:
            obj = json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _safe_rel(name):
    """把模型给的文件名收敛成 output/ 内的安全相对路径。

    保留子目录（交付物可能分层，原来一律取 basename 会把 `报告/明细.csv` 拍平成
    `明细.csv`、判分器找不到就记成做错了）；同时剥掉模型爱加的 `output/` 前缀，
    并挡掉 `..`、绝对路径这类越界写。
    """
    parts = [p for p in str(name).replace("\\", "/").strip().split("/")
             if p not in ("", ".", "..")]
    if parts and parts[0] == "output":
        parts = parts[1:]
    return os.path.join(*parts) if parts else ""


def _materialize(text, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    files = _extract_json_obj(text)
    if not isinstance(files, dict):
        return 0
    n = 0
    for name, content in files.items():
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        rel = _safe_rel(name)
        if not rel:
            continue
        dst = os.path.join(output_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
        n += 1
    return n

