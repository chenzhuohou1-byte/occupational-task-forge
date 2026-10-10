"""casegen 生成半段：职业 → 模型产闭世界案例 → crosscheck 把关 → 物化 → qc。

编排与「谁来产文件」解耦（producer）：
  StaticProducer  从现成案例拷文件，不调模型 —— CI 用，验证整条接线；
  LLMProducer     真调模型：solve 侧（规则/生成器/solve/断言/物化）用一个模型，
                  solve2 用**另一个模型**、上下文**只有 规则.md**（见 solve2_messages）。

每轮：冒烟门 → crosscheck → 物化 → qc(N6/N8)；任一不过就把问题喂回 solve 侧模型返修，
规则改了就重产 solve2；最多 max_rounds 轮，仍不过 → CaseRejected（CLI 退出码 1，绝不先放行）。
每次模型调用的 prompt / 原始响应落盘到 <out>/trace/。
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


# ---------------- 一轮校验 + 编排 ----------------
def _check_round(case_dir, out_dir, rnd, n, seed):
    """返回 (stage, problem, info)；problem 为 None 表示本轮全过。"""
    err = smoke(case_dir)
    if err:
        return "smoke", "冒烟失败：\n" + err, None
    try:
        rep = crosscheck.crosscheck(case_dir, n=n, seed=seed)
    except crosscheck.CaseError as e:
        return "crosscheck", "crosscheck 拒收：%s" % e, None
    if not rep["一致"]:
        return "crosscheck", "两份独立实现对不上，或一致性断言被破坏：\n" + json.dumps(
            {k: rep[k] for k in ("不一致数", "断言破坏数", "第一个反例", "第一个断言破坏")},
            ensure_ascii=False, indent=1, default=str)[:4000], None
    try:
        mat = casegen.materialize(case_dir, os.path.join(out_dir, "tasks_r%d" % rnd))
    except Exception:  # noqa: BLE001 物化失败（夹具不符/渲染出错）也是返修线索
        return "materialize", "物化失败：\n" + traceback.format_exc(limit=6), None
    t = tasks.load_task(mat["dir"])
    n6_ok, n6 = qc.n6_verify(t)
    n8_ok, n8 = qc.n8_floor_audit(t)
    if not (n6_ok and n8_ok):
        return "qc", "qc 不过：\n" + json.dumps(
            {"N6不过项": [c for c in n6["checks"] if not c["通过"]], "N8": n8},
            ensure_ascii=False, indent=1, default=str)[:4000], None
    return "pass", None, {"task_dir": mat["dir"], "fixtureScores": mat["fixtureScores"],
                          "crosscheck世界数": rep["世界数"]}


def build_case(occupation, out_dir, producer, max_rounds=3, n=200, seed=0):
    case_dir = os.path.join(out_dir, "case")
    os.makedirs(case_dir, exist_ok=True)
    report = {"occupation": occupation, "out": out_dir, "case_dir": case_dir, "rounds": []}
    producer.draft_rules(case_dir, occupation)
    rules = _read(os.path.join(case_dir, "规则.md"))
    producer.write_code(case_dir, rules)
    producer.write_solve2(case_dir, rules)          # 只交规则原文——独立性的编排层保证
    for rnd in range(1, max_rounds + 1):
        stage, problem, info = _check_round(case_dir, out_dir, rnd, n, seed)
        report["rounds"].append({"round": rnd, "stage": stage,
                                 "problem": problem[:1500] if problem else None})
        if problem is None:
            report.update(status="accepted", **info)
            return report
        if rnd == max_rounds:
            break
        changed = producer.repair(case_dir, problem)
        if changed is None:
            report["note"] = "producer 不支持返修，直接作废"
            break
        report["rounds"][-1]["changed"] = sorted(changed)
        if "规则.md" in changed or not changed:     # 规则变了（或没改出东西）→ 重产 solve2
            producer.write_solve2(case_dir, _read(os.path.join(case_dir, "规则.md")))
    report["status"] = "rejected"
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
    return [{"role": "system", "content": SOLVE_SYSTEM}, {"role": "user", "content": (
        "【步骤:规则】为职业「%s」设计一个 numeric_tabular 范式的闭世界核算任务，写出 规则.md。硬要求：\n"
        "1. 至少两份材料（如标准表/费率表 + 业务明细），计算必须跨材料 join；\n"
        "2. 「## 材料」一节给出世界参数的精确 JSON 结构与字段名（出题方与独立复核员都只能据此取数）；\n"
        "3. 全部费率/阈值/封顶/判定口径写进规则，答案由规则唯一确定，不引用外部法规或常识；\n"
        "4. 至少两类陷阱（如未备案/缺失/边界相等/封顶保底），**处理语义必须写进规则**；\n"
        "5. 「## 输出」一节明确返回 JSON 的结构与字段名；对象数组第一个字段是行键；"
        "集合语义的数组注明「顺序不计分」；金额保留 2 位小数；\n"
        "6. 纯虚构、可公开。\n\n下面是另一个职业的范例，只示结构、不要照抄内容：\n%s\n"
        "只输出：\n<<<FILE 规则.md>>>\n（规则原文）\n<<<END>>>") % (occupation, _fblock("规则.md", _ex("规则.md")))}]


def code_messages(rules_text):
    ex = "".join(_fblock(n, _ex(n)) for n in CODE_FILES)
    return [{"role": "system", "content": SOLVE_SYSTEM}, {"role": "user", "content": (
        "【步骤:代码】按下面这份 规则.md 写 4 个文件，全部只用 Python 标准库：\n"
        "- 参数生成器.py：def worlds(n, seed=0, boundary=True) -> list[dict]。boundary=True 时先放边界世界"
        "（跨档分界 / 封顶触发 / 保底 / 零与空 / 负数 / 重复行键 / 单行 / 最大规模 中适用者），"
        "再用 random.Random(seed) 补满到 n 个；世界结构严格按规则「材料」。\n"
        "- solve.py：def solve(world) -> dict，严格按规则「输出」。\n"
        "- 断言.py：def assertions(world, answer) -> list[str]，写跨材料恒等式（如汇总=明细之和、"
        "清单⊆行键集合），全部成立返回 []。\n"
        "- 物化.py：INSTANCE（一个覆盖主要陷阱的世界）、TASK（领域/任务名/task_id/子域/prompt/交付要求/"
        "scoringBasis）、render_inputs(world)->{文件名: 文本}、deliver(answer)->{文件名: 文本}。"
        "规则.md 会被拷成 input/核算规则.md，scoringBasis 的 ref 只能指向它或 render_inputs 产出的文件；"
        "prompt 与交付要求里不得出现任何具体数值；交付文件用 csv（首列行键）或 json；"
        "task_id 用小写英文/拼音加连字符、以 -syn-001 结尾。\n\n规则：\n%s\n"
        "下面是另一个职业的完整范例（只学结构）：\n%s\n"
        "只按 <<<FILE 文件名>>> … <<<END>>> 格式输出这 4 个文件。") % (_fblock("规则.md", rules_text), ex)}]


def repair_messages(files, problem):
    cur = "".join(_fblock(n, t) for n, t in files.items())
    return [{"role": "system", "content": SOLVE_SYSTEM}, {"role": "user", "content": (
        "【步骤:返修】这个案例没通过校验：\n%s\n\n当前文件：\n%s\n"
        "请判断原因并修正：规则有歧义或缺漏 → 改 规则.md（独立复核员只看规则，必须写到无歧义）；"
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

    def __init__(self, solve_llm, solve2_llm, trace_dir=None, cost=None):
        self.solve_llm, self.solve2_llm = solve_llm, solve2_llm
        self.trace_dir, self.cost = trace_dir, cost
        self._n = 0

    def _call(self, llm, step, messages):
        res = llm.chat(messages)
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
def run_build(occupation, out=None, solve_model="gpt-5.5", solve2_model="glm-5.3-flash",
              rounds=3, n=200, seed=0):
    from .config import PipelineConfig
    from .cost import CostTracker
    from .llm import get_client
    cfg = PipelineConfig()
    if cfg.llm.provider == "mock":
        print("✗ ALE_LLM_PROVIDER=mock：生成半段要真模型（.env 设 openai + 网关 + key）", file=sys.stderr)
        return 2
    out = out or os.path.join(cfg.runs_dir, "casegen", "%s-%s" % (
        scaffold._safe_seg(occupation, 24), time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(out, exist_ok=True)
    cost = CostTracker()
    prod = LLMProducer(get_client(dataclasses.replace(cfg.llm, model=solve_model)),
                       get_client(dataclasses.replace(cfg.llm, model=solve2_model)),
                       trace_dir=os.path.join(out, "trace"), cost=cost)
    try:
        rep, code = build_case(occupation, out, prod, max_rounds=rounds, n=n, seed=seed), 0
    except CaseRejected as e:
        rep, code = e.args[0], 1
    except RuntimeError as e:
        rep, code = {"status": "rejected", "错误": str(e)}, 1
    rep.update({"模型": {"solve": solve_model, "solve2": solve2_model},
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
        rep = build_case("（自测）", os.path.join(tmp, "static"), sp, n=60)
        check(rep["status"] == "accepted" and len(rep["rounds"]) == 1,
              "静态 producer：好案例 1 轮接收，物化 + qc(N6/N8) 全过")
        check(sp.solve2_inputs == [_read(os.path.join(good, "规则.md"))],
              "独立性（编排层）：交给 solve2 的只有 规则.md 原文")
        try:
            build_case("（自测）", os.path.join(tmp, "bug"), StaticProducer(bug), n=60)
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
                         LLMProducer(solve_mock, solve2_mock, trace_dir=trace), n=60)
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
                         LLMProducer(s_llm, s2_llm, trace_dir=trace), n=60)
        r0 = rep["rounds"][0]
        check(rep["status"] == "accepted" and len(rep["rounds"]) == 2 and r0["stage"] == "crosscheck"
              and r0.get("changed") == ["规则.md"],
              "返修闭环：注错 solve2 被拒 → 改规则 → 重产 solve2 → 第 2 轮接收")
        pr = [f for f in sorted(os.listdir(trace)) if f.endswith("_repair_prompt.json")]
        check(bool(pr) and "BUG: >= 应为 >" not in _read(os.path.join(trace, pr[0])),
              "返修 prompt 不含 solve2.py（不让 solve 侧锚定独立实现）")
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
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest or not a.occupation:
        return 0 if _selftest() else 1
    return run_build(a.occupation, a.out)


if __name__ == "__main__":
    sys.exit(main())
