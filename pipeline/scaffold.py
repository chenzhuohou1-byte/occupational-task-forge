"""任务脚手架：把 N3~N5 草案落成一个**可跑的** ALE 式任务骨架。

生成物：tasks/<领域>/<任务名>/ 下的 task_card.json + main.py + assets/（input 与 reference/expected）。
main.py 用通用判分：把 output/ 与 assets/reference/expected/ 逐文件、逐字段比对
（JSON 递归 / CSV 逐格 / 其余规范化文本，数值带容差）——作者只需填真实附件与标准答案即可跑；
需要“独立复算”式判分的任务再改写 evaluate()。

附件（N4）两条路：spec 带「附件规格」就交给 attachments.materialize 造真实
Excel/Word/PDF；不带则沿用占位符文件、把待填清单挂在 todos 里等人写。
"""
import json
import os
import re

from . import attachments as att


def _safe_seg(s, maxlen=40):
    """把任意字符串清洗成能安全当目录名/文件名的一段：去路径分隔符与非法字符、收空白、截断。

    N1 的工作面常是一整句话（含「/」如 使用Excel/用友/金蝶…），直接当目录名会被
    拆成多层嵌套目录、污染 task_id。这里统一清洗。
    """
    s = re.sub(r"[\\/:*?\"<>|]+", " ", str(s))   # 路径分隔符与文件系统非法字符 → 空格
    s = re.sub(r"\s+", "", s).strip()             # 收掉空白
    return s[:maxlen] or "task"

# 通用任务 main.py 模板（对所有脚手架任务一致）。仅替换 __TASK_TITLE__。
MAIN_TEMPLATE = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""__TASK_TITLE__ —— 由 pipeline.scaffold 生成的 ALE 式任务骨架（gate-and-score）。

契约：start / golden(正例) / neg(负例) / evaluate(判分打契约 JSON)。
判分：output/ 与 assets/reference/expected/ 逐文件比对。
  硬闸门（任一不过 → 整题0）：缺交付文件 / CSV 首列行键集合不一致(漏报/多报) / JSON 缺顶层键。
  过闸后给部分分：JSON 逐叶子、CSV 逐格、其余规范化文本，数值带容差 TOL。
输出契约：score / passed(bool) / errors[] / details[{path,expected,observed,correct}]。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
EXPECTED = os.path.join(ASSETS, "reference", "expected")


def load():
    with open(os.path.join(HERE, "task_card.json"), encoding="utf-8") as f:
        return json.load(f)


def start(work_dir):
    for d in ("input", "output", "software"):
        os.makedirs(os.path.join(work_dir, d), exist_ok=True)
    src = os.path.join(ASSETS, "input")
    if os.path.isdir(src):
        # 整树拷贝：真实附件常带目录分层（如 02_变更与签证/现场签证单/JZ-018.pdf），
        # 只拷顶层文件会漏掉嵌套附件，且 shutil.copy 撞到目录会直接报错。
        shutil.copytree(src, os.path.join(work_dir, "input"), dirs_exist_ok=True)
    _assert_reference_hidden(work_dir)
    print("答案隐藏自检通过：工作目录内无 expected/ 标准答案")


def _assert_reference_hidden(work_dir):
    """答案隐藏自检：标准答案目录 expected/ 绝不能出现在 agent 可见的工作目录里。"""
    for root, _, _ in os.walk(work_dir):
        if os.path.basename(root) == "expected":
            raise RuntimeError("答案泄露：expected/ 不应出现在沙箱内 %s" % root)


def golden(out_dir):
    """正例：标准答案原样提交（应得 1.0）。"""
    os.makedirs(out_dir, exist_ok=True)
    for name in os.listdir(EXPECTED):
        shutil.copy(os.path.join(EXPECTED, name), os.path.join(out_dir, name))


def neg(out_dir):
    """负例：拷标准答案后刻意破坏第一个交付文件（触发硬闸门，应得 0.0）。"""
    os.makedirs(out_dir, exist_ok=True)
    names = sorted(os.listdir(EXPECTED))
    for name in names:
        shutil.copy(os.path.join(EXPECTED, name), os.path.join(out_dir, name))
    if not names:
        return
    first = os.path.join(out_dir, names[0])
    if names[0].endswith(".json"):
        obj = json.load(open(first, encoding="utf-8"))
        if isinstance(obj, dict) and obj:
            obj.pop(sorted(obj)[0])          # 删一个顶层键 → JSON 缺键硬闸门
            open(first, "w", encoding="utf-8").write(json.dumps(obj, ensure_ascii=False))
            return
    if names[0].endswith(".csv"):
        rows = open(first, encoding="utf-8-sig").read().splitlines()
        if len(rows) > 1:
            open(first, "w", encoding="utf-8").write("\n".join(rows[:-1]) + "\n")  # 删一行 → 行键集合硬闸门
            return
    os.remove(first)                         # 其余：删文件 → 缺交付文件硬闸门


def evaluate(output_dir, reference_dir):
    """判分逻辑已拆到独立脚本（判分契约 §1）：调用 scripts/score_outputs.py，取其 stdout 契约 JSON。"""
    scorer = os.path.join(HERE, "scripts", "score_outputs.py")
    r = subprocess.run([sys.executable, scorer, "--output", output_dir,
                        "--reference", reference_dir], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except (ValueError, TypeError):
        why = ("判分器故障（退出码 %d）：" % r.returncode) if r.returncode else "判分器未输出合法 JSON："
        return {"score": 0.0, "passed": False, "errors": [why + (r.stderr or r.stdout)[:400]], "details": []}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("load")
    ps = sub.add_parser("start")
    ps.add_argument("--work", required=True)
    pg = sub.add_parser("golden")
    pg.add_argument("--out", required=True)
    pn = sub.add_parser("neg")
    pn.add_argument("--out", required=True)
    pe = sub.add_parser("evaluate")
    pe.add_argument("--output", required=True)
    pe.add_argument("--reference", required=True)
    args = ap.parse_args()
    if args.cmd == "load":
        print(json.dumps(load(), ensure_ascii=False, indent=2))
    elif args.cmd == "start":
        start(args.work)
    elif args.cmd == "golden":
        golden(args.out)
    elif args.cmd == "neg":
        neg(args.out)
    elif args.cmd == "evaluate":
        result = evaluate(args.output, args.reference)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
'''


# 通用判分器模板（独立脚本，判分契约 §1）。写进每个脚手架任务的 scripts/score_outputs.py。
SCORER_TEMPLATE = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用判分器（pipeline.scaffold 生成）—— 判分契约 v1.0 §1/§2，独立可跑、不 import 其他模块。

比对 output/ 与 reference/expected/：JSON 逐叶子 / CSV 逐格 / 文本规范化，数值带容差 TOL。
硬闸门（任一不过 → 整题 0）：缺交付文件 / CSV 首列行键集合不一致 / JSON 缺顶层键。
只读 --output / --reference，不碰 input/，不 import 造题/生成器代码（§8 独立性）。
"""
import argparse
import csv
import json
import os
import sys

TOL = 0.01


def _flatten(obj, prefix=""):
    items = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            items.update(_flatten(v, prefix + "/" + str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            items.update(_flatten(v, prefix + "[" + str(i) + "]"))
    else:
        items[prefix] = obj
    return items


def _eq(a, b):
    try:
        return abs(float(a) - float(b)) <= TOL
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _row_key(items):
    """一组同构 dict 的行键：按出场顺序第一个取值两两不同的非空字符串键。"""
    ds = [x for x in items if isinstance(x, dict)]
    if not ds:
        return None
    keys = [k for k in ds[0] if all(k in d for d in ds[1:])]
    for k in keys:
        vals = [d.get(k) for d in ds]
        if all(isinstance(v, str) and v.strip() for v in vals) and len(set(vals)) == len(vals):
            return k
    return None


def _align(exp, got, path, errors):
    """把数组按**行键**对齐再比，而不是按下标比。

    题面从不规定数组顺序，按下标比等于把没约定过的东西计进分数——同一套完全正确
    的条目换个顺序就大面积扣分。判分契约 §5 的切法是：该有哪些条目＝硬闸门，
    条目的值才给部分分。行键本身只用于对齐、不计分（否则照抄题面给的名称白送分）。
    """
    if isinstance(exp, dict):
        oe, og = {}, {}
        for k, v in exp.items():
            g = got.get(k) if isinstance(got, dict) else None
            oe[k], og[k] = _align(v, g, "%s/%s" % (path, k), errors)
        return oe, og
    if isinstance(exp, list):
        rk = _row_key(exp)
        if rk:
            em = {str(d.get(rk)).strip(): d for d in exp if isinstance(d, dict)}
            gm = {str(d.get(rk)).strip(): d for d in (got or [])
                  if isinstance(d, dict) and d.get(rk) is not None}
            if set(em) != set(gm):
                errors.append("%s 条目集合不一致（按「%s」对齐；漏报=%s 多报=%s）" % (
                    path or "数组", rk, sorted(set(em) - set(gm)) or "无",
                    sorted(set(gm) - set(em)) or "无"))
                return {}, {}
            oe, og = {}, {}
            for k in em:
                oe[k], og[k] = _align(
                    {a: b for a, b in em[k].items() if a != rk},
                    {a: b for a, b in gm[k].items() if a != rk},
                    "%s[%s]" % (path, k), errors)
            return oe, og
        ee = sorted(json.dumps(x, ensure_ascii=False, sort_keys=True) for x in exp)
        gg = sorted(json.dumps(x, ensure_ascii=False, sort_keys=True)
                    for x in got) if isinstance(got, list) else []
        return ({str(i): v for i, v in enumerate(ee)},
                {str(i): v for i, v in enumerate(gg)})
    return exp, got


def _cmp_json(name, exp, got):
    gate = []
    ae, ag = _align(exp, got, "", gate)
    if gate:
        return 0, 0, [], ["%s %s" % (name, e) for e in gate]
    fe = _flatten(ae)
    gg = _flatten(ag)
    details, passed = [], 0
    for k, v in fe.items():
        ok = k in gg and _eq(v, gg[k])
        passed += 1 if ok else 0
        details.append({"path": name + "#" + k, "expected": v, "observed": gg.get(k), "correct": ok})
    return passed, len(fe), details, []



def _key_set_csv(rows):
    if not rows:
        return set()
    key = list(rows[0].keys())[0]
    return set((r.get(key) or "").strip() for r in rows)


def _cmp_csv(name, exp_rows, got_rows):
    if not exp_rows:
        return 0, 0, []
    key = list(exp_rows[0].keys())[0]
    gmap = {(r.get(key) or "").strip(): r for r in got_rows}
    details, passed, total = [], 0, 0
    for er in exp_rows:
        rk = (er.get(key) or "").strip()
        gr = gmap.get(rk, {})
        for col, v in er.items():
            if col == key:
                continue   # 行键只用于对齐行（集合一致已由硬闸门把关），不计分，否则格式对就白拿分
            total += 1
            ok = _eq(v, gr.get(col))
            passed += 1 if ok else 0
            details.append({"path": "%s#%s.%s" % (name, rk, col), "expected": v,
                            "observed": gr.get(col), "correct": ok})
    return passed, total, details


def _read_json(p):
    try:
        return json.load(open(p, encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


def _emit(score, passed, errors, details, missing=(), extra=()):
    print(json.dumps({
        "score": round(float(score), 4), "passed": bool(passed), "errors": list(errors),
        "details": details, "missing_paths": list(missing), "extra_paths": list(extra),
        "meta": {"scorer": "score_outputs.py", "version": "1.0", "mode": "generic_tabular"},
    }, ensure_ascii=False))
    return 0

def score(output_dir, reference_dir):
    expected = os.path.join(reference_dir, "expected")
    if not os.path.isdir(expected):
        sys.exit("判分器故障：标准答案目录不存在 " + expected)
    exp_files = sorted(f for f in os.listdir(expected) if os.path.isfile(os.path.join(expected, f)))
    if not exp_files:
        sys.exit("判分器故障：expected/ 为空")
    missing = [n for n in exp_files if not os.path.exists(os.path.join(output_dir, n))]
    if missing:
        return _emit(0.0, False, ["缺少交付文件：" + "、".join(missing)], [], missing)
    errors, parsed = [], {}
    for name in exp_files:
        ep = os.path.join(expected, name)
        gp = os.path.join(output_dir, name)
        if name.endswith(".json"):
            e, g = _read_json(ep), _read_json(gp)
            if g is None:
                errors.append(name + " 不是合法 JSON")
            elif isinstance(e, dict):
                miss = [k for k in e if not isinstance(g, dict) or k not in g]
                if miss:
                    errors.append("%s 缺顶层键：%s" % (name, "、".join(map(str, miss))))
            parsed[name] = ("json", e, g)
        elif name.endswith(".csv"):
            e = list(csv.DictReader(open(ep, encoding="utf-8-sig")))
            g = list(csv.DictReader(open(gp, encoding="utf-8-sig")))
            if _key_set_csv(e) != _key_set_csv(g):
                mk = sorted(_key_set_csv(e) - _key_set_csv(g))
                ek = sorted(_key_set_csv(g) - _key_set_csv(e))
                errors.append("%s 行集合不一致（漏报=%s 多报=%s）" % (name, mk or "无", ek or "无"))
            parsed[name] = ("csv", e, g)
        else:
            ne = "".join(open(ep, encoding="utf-8", errors="ignore").read().split())
            ng = "".join(open(gp, encoding="utf-8", errors="ignore").read().split())
            parsed[name] = ("text", ne, ng)
    if errors:
        return _emit(0.0, False, errors, [])
    details, passed, total = [], 0, 0
    for name in exp_files:
        kind, e, g = parsed[name]
        if kind == "json":
            p, t, d, gate = _cmp_json(name, e, g)
            if gate:   # 条目集合不一致＝硬闸门（该有哪些条目本身就是考点）
                return _emit(0.0, False, gate, [])
        elif kind == "csv":
            p, t, d = _cmp_csv(name, e, g)
        else:
            eq = e == g
            p, t, d = (1 if eq else 0), 1, [{"path": name, "expected": "<text>", "observed": "<text>", "correct": eq}]
        passed += p
        total += t
        details += d
    sc = round(passed / total, 4) if total else 0.0
    return _emit(sc, sc >= 1.0, [], details)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--variant", default=None)
    a = ap.parse_args()
    return score(a.output, a.reference)


if __name__ == "__main__":
    sys.exit(main())
'''


def draft_to_spec(occupation, face, draft, idx=0):
    """把 N3~N5 草案转成 scaffold_task 可用的 spec；缺 prompt/交付要求则返回 None。"""
    if not isinstance(draft, dict) or not draft.get("prompt") or not draft.get("交付要求"):
        return None
    spec = {
        "task_id": "auto-%s-%03d" % (_safe_seg(occupation, 24), idx),
        "领域": occupation,
        "任务名": _safe_seg(face),
        "子域": str(face)[:120],
        "prompt": draft["prompt"],
        "交付要求": draft["交付要求"],
        "附件计划": draft.get("附件计划", []),
        "conventions": draft.get("conventions", []),
    }
    # 草案已经给出附件规格就带上，让 scaffold 直接造真实附件；没给就还是占位符
    if draft.get("附件规格"):
        spec["附件规格"] = draft["附件规格"]
    return spec


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _placeholder(name):
    """按扩展名给出占位内容，作者需替换为真实数据。"""
    if name.endswith(".json"):
        return "{\n  \"__TODO__\": \"填入本文件的标准答案字段\"\n}\n"
    if name.endswith(".csv"):
        return "列1,列2\n# TODO: 填入标准答案行\n"
    return "TODO: 填入内容\n"


def scaffold_task(spec, tasks_dir):
    """从草案 spec 生成可跑任务骨架，返回 {dir, todos}（带附件规格时多一个 附件 字段）。

    spec 必填：领域、任务名、task_id、prompt、交付要求(dict: 输出文件名->说明)
    spec 选填：难度层、附件计划(list: input 文件名)、元数据(dict)、conventions(list)、
              附件规格(dict: 见 attachments.materialize，带它就造真实附件而非占位符)
    """
    domain = spec["领域"]
    name = spec["任务名"]
    task_dir = os.path.join(tasks_dir, _safe_seg(domain, 40), _safe_seg(name, 40))
    deliver = spec.get("交付要求", {})
    # 附件计划可能是文件名字符串列表，也可能是模型给的对象列表
    # （如 [{"name":"工资表.xlsx","desc":...}]）——统一归一成文件名字符串。
    def _att_name(x):
        if isinstance(x, str):
            return x
        if isinstance(x, dict):
            for k in ("name", "文件名", "filename", "file", "名称"):
                if x.get(k):
                    return str(x[k])
            return str(x.get("desc") or x.get("说明") or "附件")
        return str(x)
    att_plan = [_att_name(x) for x in spec.get("附件计划", [])]

    # N4 造附件：带「附件规格」就先把真实文件落进 assets/input/，落成的相对路径
    # 直接进任务卡的 input 说明；没带则完全走老路（占位符 + todos），行为不变。
    att_spec = spec.get("附件规格")
    att_result = None
    if att_spec:
        att_result = att.materialize(att_spec, os.path.join(task_dir, "assets", "input"))
    written = att_result["written"] if att_result else []
    pending = [fn for fn in att_plan if fn not in written]
    input_names = written + pending

    card = {
        "task_id": spec["task_id"],
        "领域": domain,
        "子域": spec.get("子域", ""),
        "任务名": name,
        "prompt": spec["prompt"],
        "交付要求": deliver,
        "环境": {
            "input": "只读输入：" + "、".join(input_names) if input_names else "只读输入",
            "software": "预装软件（按需声明）",
            "output": "agent 唯一可写目录",
            "reference": "标准答案，仅判分时注入，agent 全程不可见",
        },
        "priorComplexity": {
            "steps": 0, "inputDocs": len(input_names), "crossDocJoins": 0,
            "plantedTraps": 0, "hardStops": 0, "outputFields": len(deliver),
            "conventionRulings": len(spec.get("conventions", []) or []),
            "说明": "脚手架自动填的只有可数项（输入份数/交付文件数/口径条数）；steps 等需作者补。先验复杂度不是难度（判分契约 §6.1）。",
        },
        "measuredDifficulty": [],
        "evaluation": {
            "type": "numeric_tabular",
            "artifactModes": ["structured_tabular"],
            "composition": ["gate_and_score"],
            "locale": "host",
            "scorer": "scripts/score_outputs.py",
            "scorerVersion": "1.0",   # 改判分器就改这里：旧 measuredDifficulty 随即作废（§6.2）
            "passRule": "交付文件齐全 AND 集合/键一致（硬闸门）AND 逐字段全对",
            "hardGates": [
                "缺任一交付文件 → 0",
                "CSV 首列行键集合与标准答案不一致（漏报/多报）→ 0",
                "JSON 缺任一顶层键 → 0",
                "JSON 数组条目集合（按行键）与标准答案不一致 → 0",
            ],
            "tolerance": {"mode": "absolute", "value": 0.01,
                          "rationale": "脚手架默认按分位 absolute 比对；作者按实际量纲改成 relative/banded（判分契约 §9.2）。"},
            "weights": {"说明": "等权，score = 正确字段数 / 总字段数（判分契约 §5）。"},
            "fixtureScores": {
                "output_test_pos": {"expected": 1.0, "observed": None},
                "output_test_neg": {"expected": 0.0, "observed": None},
                "output_test_random": {"expected": 0.0, "observed": None},
                "output_test_partial": {"expected": 0.5, "observed": None},
            },
        },
        "元数据": spec.get("元数据", {"机器规格": "2C4G", "超时秒": 1800, "判分构成": "纯代码",
                                      "生成方式": "", "schema": "判分契约 v1.0"}),
        "scoringBasis": [],
    }

    todos = []
    _write(os.path.join(task_dir, "task_card.json"),
           json.dumps(card, ensure_ascii=False, indent=2) + "\n")
    _write(os.path.join(task_dir, "main.py"),
           MAIN_TEMPLATE.replace("__TASK_TITLE__", name))
    _write(os.path.join(task_dir, "scripts", "score_outputs.py"), SCORER_TEMPLATE)

    # 附件（input）与标准答案（reference/expected）占位，等作者填真实数据。
    # 已由附件规格造出真实文件的不再写占位符，也不进 todos。
    for fn in pending:
        p = os.path.join(task_dir, "assets", "input", fn)
        _write(p, _placeholder(fn))
        todos.append(os.path.relpath(p, tasks_dir))
    for fn in deliver:
        p = os.path.join(task_dir, "assets", "reference", "expected", fn)
        _write(p, _placeholder(fn))
        todos.append(os.path.relpath(p, tasks_dir))

    todos.append("填 scoringBasis（判分契约 §7：每个判分点标 material/external 出处；空数组不可进库）")
    todos.append("填 元数据.生成方式（程序化生成 / 人工构造 / 混合；数据集 schema 的「生成方式」列读它）")
    todos.append("造 output_test_random/ 与 output_test_partial/ 夹具（判分契约 §4；需先填好标准答案）")

    out = {"dir": task_dir, "todos": todos}
    if att_result:
        # 造附件失败的也要让人看见：errors 里每一条都是一份缺掉的附件
        out["附件"] = att_result
        todos.extend("造附件失败：" + e for e in att_result["errors"])
    return out


def _describe_json(obj):
    if isinstance(obj, dict):
        parts = []
        for k, v in obj.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                parts.append("%s(数组,元素含键: %s)" % (k, "、".join(map(str, v[0].keys()))))
            elif isinstance(v, dict):
                parts.append("%s(对象,含键: %s)" % (k, "、".join(map(str, v.keys()))))
            else:
                parts.append(str(k))
        return "JSON 对象，顶层键必须且仅为: " + "，".join(parts)
    if isinstance(obj, list):
        return "JSON 数组"
    return "JSON 值"


def _describe_csv(text):
    header = text.splitlines()[0] if text.strip() else ""
    return "CSV，表头必须为: " + header


def derive_schema(expected_dir):
    """从标准答案 reference/expected/ 反推每个交付文件的字段说明。"""
    out = {}
    if not os.path.isdir(expected_dir):
        return out
    for name in sorted(os.listdir(expected_dir)):
        p = os.path.join(expected_dir, name)
        if not os.path.isfile(p):
            continue
        try:
            if name.endswith(".json"):
                out[name] = _describe_json(json.load(open(p, encoding="utf-8")))
            elif name.endswith(".csv"):
                out[name] = _describe_csv(open(p, encoding="utf-8-sig").read())
            else:
                out[name] = "文本文件"
        except (ValueError, OSError):
            out[name] = "（无法解析，请手动填写）"
    return out


def sync_schema(task_dir):
    """把从标准答案反推的字段说明写回 task_card 的「交付要求」，消除字段名歧义。

    仅适用于脚手架式任务（有 assets/reference/expected/）。返回更新后的交付要求。
    """
    task_dir = os.path.abspath(task_dir)
    card_path = os.path.join(task_dir, "task_card.json")
    expected = os.path.join(task_dir, "assets", "reference", "expected")
    if not os.path.isdir(expected):
        return {"skipped": "无 reference/expected/（可能是手写独立复算任务），未改动"}
    schema = derive_schema(expected)
    if not schema:
        return {"skipped": "expected/ 为空"}
    with open(card_path, encoding="utf-8") as f:
        card = json.load(f)
    deliver = card.get("交付要求", {})
    for fn, desc in schema.items():
        deliver[fn] = desc
    card["交付要求"] = deliver
    _write(card_path, json.dumps(card, ensure_ascii=False, indent=2) + "\n")
    return {"updated": deliver}



