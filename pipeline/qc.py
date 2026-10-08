"""质量闸门节点：N6 三道校验 / N7 盲解 / N8 地板审计。

三者都以「一条已造好的任务」为输入，是数据能否进库的关卡（非模型评测）。
N6/N8 是确定性纯代码校验；N7 需要 LLM 当考生。
"""
import json
import os
import re
import tempfile

from . import harness


def n6_verify(task, pass_threshold=1.0):
    """N6 进库校验（确定性，判分契约 §8）：
      1) 四档夹具实测分 == 任务卡声明的 fixtureScores（老任务只有正负两档时退化为两档）
      2) 判分可复现：同一 output 判两次分数一致
      3) 判分器独立：import 图里没有造附件/生成器模块
      4) 数据已填：assets/ 无 TODO 占位
      5) 判分依据可溯源：scoringBasis 非空
    """
    checks = []

    fx_ok, fx_detail = _check_fixtures(task)
    checks.append({"检查": "夹具实测==声明", "通过": fx_ok, "明细": fx_detail})

    wd = tempfile.mkdtemp(prefix="ale_n6_")
    harness.setup_env(task, wd)
    harness.golden_solver(task, wd)
    s1 = harness.evaluate(task, wd).score
    s2 = harness.evaluate(task, wd).score
    checks.append({"检查": "判分可复现", "通过": s1 == s2, "score": [s1, s2]})

    ind_ok, ind_why = _check_independence(task)
    checks.append({"检查": "判分器独立(import 图干净)", "通过": ind_ok, "说明": ind_why})

    ready = not _has_placeholder(os.path.join(task.dir, "assets"))
    checks.append({"检查": "数据已填(无 TODO 占位)", "通过": ready})

    basis = task.card.get("scoringBasis")
    checks.append({"检查": "判分依据可溯源(scoringBasis 非空)", "通过": bool(basis),
                   "条数": len(basis) if isinstance(basis, list) else 0})

    ok = all(c["通过"] for c in checks)
    return ok, {"task_id": task.task_id, "checks": checks}


def n8_floor_audit(task, floor=0.0):
    """N8 地板审计（判分契约 §8）：地板＝**同结构随机假答案**，不是空解。

    空解得 0 只是最低门槛，挡不住「文件名/表头/行键/字段全对、数值全错」这种白拿分——
    而那恰恰是结构化表格类任务最常见的漏分口。任务若提供了 output_test_random 夹具就用它；
    没有则退回空解，并在说明里标注这条地板证据不完整。
    """
    fx = os.path.join(task.dir, "assets", "reference", "fixtures", "output_test_random")
    if os.path.isdir(fx):
        r = harness.run_task(task, solver=harness.fixture_solver("output_test_random"))
        kind, note = "同结构随机假答案", "地板正常"
    else:
        r = harness.run_task(task, solver=harness.empty_solver)
        kind, note = "空解", "地板正常（但缺 output_test_random 夹具，这条地板证据不完整）"
    ok = r.score <= floor
    return ok, {"task_id": task.task_id, "floor_score": r.score, "地板类型": kind,
                "通过": ok,
                "说明": note if ok else "%s 得分过高：判分器白送分，需修" % kind}



def n7_blind_solve(task, llm, cost=None):
    """N7 盲解：把 prompt+input 交给 LLM 当考生单发试做，产出 output 再判分。
    只给题面与输入、不给 reference。用于估基线通过率、定难度层。"""
    wd = tempfile.mkdtemp(prefix="ale_n7_")
    harness.setup_env(task, wd)
    inputs = _read_inputs(os.path.join(wd, "input"))
    msgs = [
        {"role": "system", "content": "你是完成真实工作任务的 agent，只依据给定输入作答。"
         "最终必须只输出一个 JSON 对象：键=相对 output/ 的文件名，值=该文件完整文本内容。"},
        {"role": "user", "content": _blind_prompt(task, inputs)},
    ]
    res = llm.chat(msgs)
    if cost:
        cost.add("N7", res)
    # 诚实性闸门：content 为空、或被 max_tokens 截断（finish_reason=length）＝测量故障，
    # 不是「agent 不会做」。推理型模型会把预算烧在 reasoning_content 上、content 返空，
    # 若静默当 0 分，便宜档会假装「fullPassRate=0 过门槛」。必须抛出、由调用方记成故障。
    if not res.text.strip() or res.finish_reason == "length":
        raise RuntimeError(
            "盲解无有效产出（疑似推理占满预算或超时截断）：finish_reason=%s, "
            "completion_tokens=%d, reasoning_tokens=%d, content长度=%d。"
            "换直答型模型或调大 ALE_LLM_MAX_TOKENS（注意过大可能触发网关生成超时）。"
            % (res.finish_reason, res.completion_tokens, res.reasoning_tokens, len(res.text)))
    n_files = _materialize(res.text, os.path.join(wd, "output"))
    r = harness.evaluate(task, wd)
    return True, {"task_id": task.task_id, "score": r.score,
                  "passed": r.passed, "写出文件数": n_files}


# ---------------- helpers ----------------
FIXTURE_ORDER = ("output_test_pos", "output_test_neg",
                 "output_test_random", "output_test_partial", "output_test_fabricated")
# 造附件/生成器模块：判分器 import 了这些就不是独立复算，同一个 bug 会自我验证通过
FORBIDDEN_IMPORTS = ("case_data", "gen_all", "attachments", "make_fixtures",
                     "recompute_reference", "scaffold", "generate")


def _check_fixtures(task):
    """跑任务卡声明的每一档夹具，比对实测分与声明分（判分契约 §4）。

    没声明 fixtureScores、或声明成描述字符串（ALE 的散文债）→ 直接不通过。
    """
    ev = task.card.get("evaluation")
    if not isinstance(ev, dict):
        return False, "任务卡 evaluation 不是结构化对象"
    fs = ev.get("fixtureScores")
    if not isinstance(fs, dict) or not fs:
        return False, "缺 fixtureScores"
    fixdir = os.path.join(task.dir, "assets", "reference", "fixtures")
    out, ok_all = [], True
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
            got = harness.run_task(task, solver=harness.fixture_solver(name)).score
        elif name == "output_test_pos":
            got = harness.run_task(task, solver=harness.golden_solver).score
        elif name == "output_test_neg":
            got = harness.run_task(task, solver=harness.neg_solver).score
        else:
            out.append("%s: 夹具目录不存在" % name)
            ok_all = False
            continue
        hit = abs(got - exp) < 1e-9
        ok_all = ok_all and hit
        out.append("%s: 实测 %s / 声明 %s %s" % (name, got, exp, "" if hit else "← 不符"))
    if len(out) < 4:
        out.append("只有 %d 档夹具，契约 §4 要求四档（定性型五档）" % len(out))
        ok_all = False
    return ok_all, out


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


def _read_inputs(input_dir, limit=8000):
    """把 input/ 文件读成文本喂给盲解 agent：xlsx→制表符表格、docx/pdf→段落文本，其余按文本读。

    整树遍历：真实附件带目录分层，键用相对路径，agent 才知道文件在哪一层。
    """
    out = {}
    if not os.path.isdir(input_dir):
        return out
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
                out[name] = _xlsx_text(p)[:limit]
            elif ext == "docx":
                out[name] = _docx_text(p)[:limit]
            elif ext == "pdf":
                out[name] = _pdf_text(p)[:limit]
            else:
                out[name] = open(p, encoding="utf-8", errors="ignore").read()[:limit]
        except Exception as e:  # noqa: BLE001 读取失败不阻断盲解
            out[name] = "[无法读取 %s：%s]" % (name, type(e).__name__)
    return out


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


def _materialize(text, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    files = _extract_json_obj(text)
    if not isinstance(files, dict):
        return 0
    n = 0
    for name, content in files.items():
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        dst = os.path.join(output_dir, os.path.basename(name))
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
        n += 1
    return n
