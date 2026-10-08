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

from . import attachments as att

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
import csv
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
EXPECTED = os.path.join(ASSETS, "reference", "expected")
TOL = 0.01


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


def _cmp_json(name, exp, got):
    fe = _flatten(exp)
    gg = _flatten(got) if got is not None else {}
    details = []
    passed = 0
    for k, v in fe.items():
        ok = k in gg and _eq(v, gg[k])
        passed += 1 if ok else 0
        details.append({"path": name + "#" + k, "expected": v,
                        "observed": gg.get(k), "correct": ok})
    return passed, len(fe), details


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
    details = []
    passed = 0
    total = 0
    for er in exp_rows:
        rk = (er.get(key) or "").strip()
        gr = gmap.get(rk, {})
        for col, v in er.items():
            total += 1
            ok = _eq(v, gr.get(col))
            passed += 1 if ok else 0
            details.append({"path": "%s#%s.%s" % (name, rk, col), "expected": v,
                            "observed": gr.get(col), "correct": ok})
    return passed, total, details


def _read_json(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _fail(errors):
    return {"score": 0.0, "passed": False, "errors": errors, "details": []}


def evaluate(output_dir, reference_dir):
    expected = os.path.join(reference_dir, "expected")
    exp_files = sorted(os.listdir(expected))
    errors = []

    # 硬闸门1：交付文件齐全
    for name in exp_files:
        if not os.path.exists(os.path.join(output_dir, name)):
            errors.append("缺少交付文件 " + name)
    if errors:
        return _fail(errors)

    # 硬闸门2/3：JSON 缺顶层键 / CSV 首列行键集合不一致
    parsed = {}
    for name in exp_files:
        ep = os.path.join(expected, name)
        gp = os.path.join(output_dir, name)
        if name.endswith(".json"):
            e = _read_json(ep)
            g = _read_json(gp)
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
                miss = sorted(_key_set_csv(e) - _key_set_csv(g))
                extra = sorted(_key_set_csv(g) - _key_set_csv(e))
                errors.append("%s 行集合不一致（漏报=%s 多报=%s）" % (name, miss or "无", extra or "无"))
            parsed[name] = ("csv", e, g)
        else:
            parsed[name] = ("text",
                            re.sub(r"\s+", "", open(ep, encoding="utf-8", errors="ignore").read()),
                            re.sub(r"\s+", "", open(gp, encoding="utf-8", errors="ignore").read()))
    if errors:
        return _fail(errors)

    # 过闸后：字段值层给部分分
    details = []
    passed = 0
    total = 0
    for name in exp_files:
        kind, e, g = parsed[name]
        if kind == "json":
            p, t, d = _cmp_json(name, e, g)
        elif kind == "csv":
            p, t, d = _cmp_csv(name, e, g)
        else:
            eq = e == g
            p, t, d = (1 if eq else 0), 1, [{"path": name, "expected": "<text>",
                                             "observed": "<text>", "correct": eq}]
        passed += p
        total += t
        details += d
    score = round(passed / total, 4) if total else 0.0
    return {"score": score, "passed": score >= 1.0, "errors": [], "details": details}


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


def draft_to_spec(occupation, face, draft, idx=0):
    """把 N3~N5 草案转成 scaffold_task 可用的 spec；缺 prompt/交付要求则返回 None。"""
    if not isinstance(draft, dict) or not draft.get("prompt") or not draft.get("交付要求"):
        return None
    spec = {
        "task_id": "auto-%s-%03d" % (occupation, idx),
        "领域": occupation,
        "任务名": str(face)[:40],
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
    task_dir = os.path.join(tasks_dir, domain, name)
    deliver = spec.get("交付要求", {})
    att_plan = spec.get("附件计划", [])

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
        "难度层": spec.get("难度层", "L1-基础"),
        "prompt": spec["prompt"],
        "交付要求": deliver,
        "环境": {
            "input": "只读输入：" + "、".join(input_names) if input_names else "只读输入",
            "software": "预装软件（按需声明）",
            "output": "agent 唯一可写目录",
            "reference": "标准答案，仅判分时注入，agent 全程不可见",
        },
        "evaluation": {
            "type": "code",
            "command": "python main.py evaluate --output <output_dir> --reference <reference_dir>",
            "scheme": "gate-and-score",
            "tolerance": 0.01,
            "passThreshold": 1.0,
            "hardGates": [
                "缺任一交付文件 → 0",
                "CSV 首列行键集合与标准答案不一致（漏报/多报）→ 0",
                "JSON 缺任一顶层键 → 0",
            ],
            "partialCredit": "过闸后逐字段比对（JSON 逐叶子 / CSV 逐格 / 文本规范化），score = 正确数 / 总数",
            "fixtureScores": {
                "output_test_pos": "1.0（golden：标准答案原样提交）",
                "output_test_neg": "0.0（neg：破坏第一个交付文件触发硬闸门）",
            },
            "outputContract": "score(0~1) / passed(bool) / errors[] / details[{path,expected,observed,correct}]",
        },
        "元数据": spec.get("元数据", {"机器规格": "2C4G", "超时秒": 1800, "判分构成": "纯代码"}),
        "口径来源": spec.get("conventions", []),
    }

    todos = []
    _write(os.path.join(task_dir, "task_card.json"),
           json.dumps(card, ensure_ascii=False, indent=2) + "\n")
    _write(os.path.join(task_dir, "main.py"),
           MAIN_TEMPLATE.replace("__TASK_TITLE__", name))

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



