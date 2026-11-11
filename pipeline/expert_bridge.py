"""外部专家题桥接：把专家交付包（agent/ + evaluator/ + 百分制判分器）转成我们的任务目录，
并把外部 agent 已有的作答结果导进 measuredDifficulty。

为什么要有：专家包原样过不了 qc（没有 main.py 五子命令、判分是百分制、缺四档夹具），
逐题手包一层约半天。照 `pipeline/ale_bridge.py` 的结构写一个通用转换器：
  - start：把 agent/input/ 拷进沙箱 input/（另带 任务信息.md、软件环境说明.md）；
  - evaluate：子进程原样调专家判分器（**判分逻辑一行不改**），百分制换算成 0–1，
    `passed` = 未触发硬闸门 且 核心项全过 且 总分 ≥ passScore（专家口径「单次满分」）；
    逐项结果放进 details。判分器报 NOT_SCORED 或软件缺失 → 退出码 2 + fault（测量故障，非 0 分）。
  - 夹具：pos = evaluator/reference，partial = 专家给的单错负样例 neg_fixture，
    neg = golden 删掉一个交付文件（触发交付齐全硬闸门），random = floor.synthesize 从 reference 合成。

硬闸门（`bridge.hardGates`）：专家判分器对「结构合法、内容全空」的作答也给几分（病理空交付 6/100），
过不了 N8 地板。桥接层把若干项声明为硬闸门：任一项不过整题 0 分。**只许选外部实跑里每一轮都过的
机械项**——这样已有测量一分不变，只把退化作答压到 0；build 时会核对 model_evaluation 下的专家实跑。

用法：
    python3 -m pipeline.expert_bridge build <专家包根目录> --conf <桥接配置.json> --out <数据仓里的任务目录>
    python3 -m pipeline.expert_bridge import <任务目录> --runs <runN/ 所在目录> --agent zulu \
        --model "Claude Sonnet 5.5" --effort extra_high [--write-card]

桥接配置（每题手写一次，约 1 小时）：task_id/任务名/领域/子域/taxonomy/用途/材料来源/software/
scoringBasis/元数据 照抄进卡片；`bridge` 段给判分器入口、参数模板、外部软件、核心项、硬闸门。
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

SHIM_MAIN = r'''#!/usr/bin/env python3
"""外部专家题 shim（由 pipeline/expert_bridge.py 生成，勿手改）。判分原样调专家判分器。"""
import argparse, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(HERE, "assets", "reference")


def _card():
    with open(os.path.join(HERE, "task_card.json"), encoding="utf-8") as f:
        return json.load(f)


def _conf():
    return _card()["evaluation"]["bridge"]


def _fault(msg):
    print(json.dumps({"score": 0.0, "passed": False, "errors": [msg], "details": [], "fault": msg},
                     ensure_ascii=False))
    return 2


def cmd_start(work):
    dst = os.path.join(work, "input")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(os.path.join(HERE, "assets", "input"), dst)
    os.makedirs(os.path.join(work, "output"), exist_ok=True)
    assert not os.path.exists(os.path.join(work, "reference")), "答案泄漏：沙箱不应有 reference/"


def _copy_reference(out, drop=None):
    os.makedirs(out, exist_ok=True)
    src = os.path.join(REF, _conf()["golden"])
    for fn in sorted(os.listdir(src)):
        if fn != drop:
            shutil.copy2(os.path.join(src, fn), os.path.join(out, fn))


def cmd_neg(out):
    """删掉一个交付文件：触发「交付齐全」硬闸门 → 整题 0。"""
    files = sorted(os.listdir(os.path.join(REF, _conf()["golden"])))
    _copy_reference(out, drop=files[0])


def _tool(name, spec):
    p = os.environ.get(spec.get("env", ""), "") or os.path.expanduser(spec.get("default") or "")
    if not p and spec.get("which"):
        p = shutil.which(spec["which"]) or ""
    return p if p and os.path.exists(p) else ""


def cmd_evaluate(output, reference):
    c = _conf()
    py = os.environ.get("ALE_EXPERT_PYTHON") or os.path.expanduser(c.get("python") or "") or sys.executable
    if not os.path.exists(py):
        py = sys.executable
    tools = {}
    for name, spec in (c.get("tools") or {}).items():
        tools[name] = _tool(name, spec)
        if not tools[name]:
            return _fault("判分要用 %s，本机找不到（设环境变量 %s）" % (name, spec.get("env")))
    tmp = tempfile.mkdtemp(prefix="expert_eval_")
    out_json = os.path.join(tmp, "score.json")
    fill = {"candidate": os.path.abspath(output), "reference": os.path.abspath(reference),
            "input": os.path.join(HERE, "assets", "input"), "out": out_json,
            **{"tool_" + k: v for k, v in tools.items()}}
    args = [a.format(**fill) for a in c["args"]]
    env = dict(os.environ, **(c.get("env") or {}))
    try:
        r = subprocess.run([py, "-B", os.path.join(reference, c["grader"])] + args, cwd=tmp, env=env,
                           capture_output=True, text=True, timeout=int(c.get("timeoutSec", 1800)))
    except subprocess.TimeoutExpired:
        return _fault("专家判分器超时（%ss）" % c.get("timeoutSec", 1800))
    try:
        d = json.load(open(out_json, encoding="utf-8"))
    except (OSError, ValueError):
        return _fault("专家判分器没写出结果（退出码 %d）：%s" % (r.returncode, (r.stderr or r.stdout)[-600:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if d.get("status") != "SCORED":
        return _fault("专家判分器 %s：%s" % (d.get("status"), str(d.get("error", ""))[:400]))
    rows = d.get("rows") or []
    failed = [x["id"] for x in rows if not x.get("passed")]
    gate = [g for g in c.get("hardGates") or [] if g in failed]
    core = [g for g in c.get("core") or [] if g in failed]
    raw = float(d["score"]) / float(d.get("max_score") or 100)
    score = 0.0 if gate else round(raw, 4)
    passed = not gate and not core and float(d["score"]) >= float(c.get("passScore", 80))
    errors = (["硬闸门未过：%s" % gate] if gate else []) + (["核心项未过：%s" % core] if core else []) + failed
    details = [{"path": x["id"], "expected": True, "observed": bool(x.get("passed")),
                "correct": bool(x.get("passed")), "weight": x.get("weight")} for x in rows]
    print(json.dumps({"score": score, "passed": passed, "errors": errors, "details": details,
                      "_expert": {"score": d["score"], "max_score": d.get("max_score"),
                                  "failed": failed, **{k: d[k] for k in ("m02_mode",) if k in d}}},
                     ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("load")
    p = sub.add_parser("start"); p.add_argument("--work", required=True)
    p = sub.add_parser("golden"); p.add_argument("--out", required=True)
    p = sub.add_parser("neg"); p.add_argument("--out", required=True)
    p = sub.add_parser("evaluate"); p.add_argument("--output", required=True)
    p.add_argument("--reference", required=True); p.add_argument("--variant", default=None)
    a = ap.parse_args()
    if a.cmd == "load":
        sys.stdout.write(open(os.path.join(HERE, "task_card.json"), encoding="utf-8").read())
    elif a.cmd == "start":
        cmd_start(a.work)
    elif a.cmd == "golden":
        _copy_reference(a.out)
    elif a.cmd == "neg":
        cmd_neg(a.out)
    elif a.cmd == "evaluate":
        return cmd_evaluate(a.output, a.reference)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
'''

NOTE = """

---
本环境说明（外部专家题在我们 harness 下运行，路径已改为相对工作目录）：
- 材料在 input/（只读），成果写到 output/。input/constraint/任务与输出契约.md 里说的 deliverables/ 就是 output/。
- input/任务信息.md 是逐文件说明，input/software_环境说明.md 是软件环境说明。
"""
SKIP_NAMES = {"__pycache__", ".DS_Store"}


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _copytree(src, dst):
    shutil.copytree(src, dst, ignore=lambda d, names: [n for n in names if n in SKIP_NAMES])


def _verify_manifest(pkg):
    """专家包自带 package_manifest.json（逐文件 sha256）：拷之前先核一遍，包被改过就停。"""
    p = os.path.join(pkg, "package_manifest.json")
    if not os.path.isfile(p):
        return None, ["包里没有 package_manifest.json，未核对文件哈希"]
    man = json.load(open(p, encoding="utf-8"))
    bad = [f["path"] for f in man.get("files") or []
           if not os.path.isfile(os.path.join(pkg, f["path"])) or _sha(os.path.join(pkg, f["path"])) != f.get("sha256")]
    return man, bad


def _prompt(pkg):
    text = open(os.path.join(pkg, "agent", "任务描述.md"), encoding="utf-8").read().strip()
    return text.replace("`deliverables/`", "`output/`").replace("deliverables/", "output/") + NOTE


def _input_summary(d):
    n, size = 0, 0
    for root, _, files in os.walk(d):
        for fn in files:
            n += 1
            size += os.path.getsize(os.path.join(root, fn))
    return n, size


def build(pkg, conf, out_dir, force=False):
    """专家包 + 桥接配置 → 数据仓任务目录。返回卡片路径。"""
    pkg = os.path.abspath(pkg)
    man, bad = _verify_manifest(pkg)
    if bad and man is not None:
        raise SystemExit("专家包和 package_manifest.json 对不上（%d 个文件）：%s" % (len(bad), bad[:5]))
    if os.path.exists(out_dir) and os.listdir(out_dir):
        if not force:
            raise SystemExit("%s 已存在且非空；确认要覆盖加 --force" % out_dir)
        shutil.rmtree(out_dir)
    b = conf["bridge"]
    ev_src = os.path.join(pkg, "evaluator")
    gate_hit = []
    own = os.path.join(ev_src, "model_evaluation", "grade.json")
    if os.path.isfile(own):   # 硬闸门只许选外部实跑每轮都过的项：先拿专家自己那一轮核
        gate_hit = [g for g in b.get("hardGates") or [] if g in (json.load(open(own)).get("failed") or [])]
    if gate_hit:
        raise SystemExit("硬闸门 %s 在专家自己的实跑里没过：选它会改变已有测量的分数" % gate_hit)

    inp = os.path.join(out_dir, "assets", "input")
    ref = os.path.join(out_dir, "assets", "reference")
    _copytree(os.path.join(pkg, "agent", "input"), inp)
    shutil.copy2(os.path.join(pkg, "agent", "任务信息.md"), os.path.join(inp, "任务信息.md"))
    sw = os.path.join(pkg, "agent", "software", "环境说明.md")
    if os.path.isfile(sw):
        shutil.copy2(sw, os.path.join(inp, "software_环境说明.md"))
    _copytree(ev_src, ref)
    fx = os.path.join(ref, "fixtures")
    _copytree(os.path.join(ref, b["negFixture"]), os.path.join(fx, "output_test_partial"))
    _copytree(os.path.join(ref, b["golden"]), os.path.join(fx, "output_test_random"))
    from . import floor
    floor.synthesize(os.path.join(fx, "output_test_random"))
    with open(os.path.join(out_dir, "main.py"), "w", encoding="utf-8") as f:
        f.write(SHIM_MAIN)

    n_in, sz_in = _input_summary(inp)
    golden_files = sorted(os.listdir(os.path.join(ref, b["golden"])))
    card = dict(conf["card"])
    card.update({
        "prompt": _prompt(pkg),
        "交付要求": {fn: "格式见 input/constraint/任务与输出契约.md" for fn in golden_files},
        "环境": {"input": "只读输入：专家包 agent/input/ 原样（%d 个文件、约 %.1f MB），另附 任务信息.md、"
                          "software_环境说明.md" % (n_in, sz_in / 1e6),
                 "software": conf.get("softwareNote", ""),
                 "output": "agent 唯一可写目录（专家契约里的 deliverables/）",
                 "reference": "专家包 evaluator/ 原样（参考产物、判分器、负样例、来源与复核记录），仅判分时注入"},
        "measuredDifficulty": card.get("measuredDifficulty") or [],
    })
    card["evaluation"] = {
        "type": "expert_bridge", "artifactModes": conf.get("artifactModes", []),
        "composition": ["gate_and_score"], "locale": "host",
        "scorer": os.path.join("assets", "reference", b["grader"]), "scorerVersion": b["scorerVersion"],
        **({"visionRequired": True} if b.get("visionRequired") else {}),
        "passRule": "未触发硬闸门 AND 核心项 %s 全过 AND 专家百分制总分 ≥ %s" % (
            "/".join(b.get("core") or []), b.get("passScore", 80)),
        "hardGates": ["%s 未过 → 0（桥接层硬闸门：专家判分器对退化作答也给分，见 pipeline/expert_bridge.py）" % g
                      for g in b.get("hardGates") or []],
        "tolerance": {"mode": "banded", "rationale": "容差由专家判分器与输出契约的「判定口径」一节定（交并比/相对误差分档），桥接不改"},
        "weights": {"说明": "沿用专家百分制权重，score = 专家总分 / 满分"},
        "fixtureScores": {k: {"expected": None, "observed": None} for k in
                          ("output_test_pos", "output_test_neg", "output_test_random", "output_test_partial")},
        "bridge": b,
    }
    meta = dict(card.get("元数据") or {})
    meta["专家包"] = {"路径名": os.path.basename(pkg), "version": (man or {}).get("version"),
                     "revision": (man or {}).get("revision"), "manifest已核对": man is not None and not bad,
                     "转换日期": datetime.date.today().isoformat()}
    card["元数据"] = meta
    path = os.path.join(out_dir, "task_card.json")
    _dump(path, card)
    return path, _fixture_scores(out_dir, man)


def _dump(path, card):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(card, ensure_ascii=False, indent=2) + "\n")


def _fixture_scores(out_dir, man):
    """四档夹具实跑一遍，把实测分写成声明分；pos≠1、neg/random≠0 时报出来（不替作者圆）。"""
    from . import harness, tasks
    task = tasks.load_task(out_dir)
    runs = {"output_test_pos": harness.golden_solver, "output_test_neg": harness.neg_solver,
            "output_test_random": harness.fixture_solver("output_test_random"),
            "output_test_partial": harness.fixture_solver("output_test_partial")}
    got, problems = {}, []
    for k, solver in runs.items():
        r = harness.run_task(task, solver=solver)
        if r.fault:
            raise SystemExit("夹具 %s 判分故障：%s" % (k, r.fault[:300]))
        got[k] = r.score
    if got["output_test_pos"] != 1.0:
        problems.append("参考产物只得 %s" % got["output_test_pos"])
    for k in ("output_test_neg", "output_test_random"):
        if got[k] != 0.0:
            problems.append("%s 得 %s，应为 0" % (k, got[k]))
    want = ((man or {}).get("checks") or {}).get("negative_native_recheck_score")
    if want is not None and abs(got["output_test_partial"] * 100 - want) > 1e-6:
        problems.append("专家负样例得 %s，专家包声明 %s" % (got["output_test_partial"], want))
    card = task.card
    for k, v in got.items():
        card["evaluation"]["fixtureScores"][k]["expected"] = v
    _dump(os.path.join(out_dir, "task_card.json"), card)
    return got, problems


# 轨迹审计里这两类标记＝作答碰过评测侧文件或联网，整轮作废；「目录外」只记账（多是 /tmp 草稿）
DISQUALIFY = ("评测侧", "联网")


def _find_runs(runs_dir, deliverables, exclude=None):
    out = []
    for d in sorted(glob.glob(os.path.join(runs_dir, "*")), key=lambda p: [int(x) if x.isdigit() else x
                                                                           for x in re.split(r"(\d+)", p)]):
        name = os.path.basename(d)
        if os.path.isdir(os.path.join(d, deliverables)) and not name.startswith(("_", ".")):
            if exclude and re.search(exclude, name):
                continue
            out.append((name, os.path.join(d, deliverables), os.path.join(runs_dir, name + "_meta.json")))
    return out


_EV_RE = re.compile(r"(evaluator|grade_v2|neg_fixture|grading_truth|Rubrics|考点清单|评分项|评分表|author_review)")
_NET_RE = re.compile(r"(\bcurl\b|\bwget\b|urlopen|urllib\.request|requests\.get|git clone|pip3? install)")


def _audit_session(runs_dir, name):
    """zulu 会话记录里逐个工具调用查两类：碰评测侧文件、联网。找不到会话记录返回 None（只能信 meta）。"""
    cand = glob.glob(os.path.join(runs_dir, name + "_session.json")) + \
        glob.glob(os.path.join(runs_dir, glob.escape(name) + "__*session*.json"))
    if not cand:
        return None
    flags = set()
    s = json.load(open(cand[0], encoding="utf-8"))
    for m in s.get("messages") or []:
        for el in m.get("elements") or []:
            for ch in el.get("children") or []:
                if ch.get("type") != "TOOL":
                    continue
                p = json.dumps(ch.get("params"), ensure_ascii=False)
                if ch.get("toolName") in ("web_fetch", "web_search") or _NET_RE.search(p):
                    flags.add("联网")
                if _EV_RE.search(p):
                    flags.add("评测侧")
    return sorted(flags)


def import_runs(task_dir, runs_dir, agent, model, effort, deliverables="deliverables", timeout_min=None,
                exclude=None, measured_at=None):
    """外部 agent 的逐轮交付物 → 用卡上判分器重判 → 一条 measuredDifficulty。返回 (entry, 逐轮明细)。

    不信外部自己记的分：每轮交付物原件拷进沙箱 output/，走 harness.evaluate 重判；外部记的分只拿来对账。
    会话记录在的话逐个工具调用复查一遍，碰过评测侧文件或联网的轮次作废。
    """
    from . import harness, tasks
    task = tasks.load_task(task_dir)
    sv = task.card["evaluation"]["scorerVersion"]
    rows, versions, dates = [], set(), []
    for name, deliv, meta_p in _find_runs(runs_dir, deliverables, exclude):
        meta = json.load(open(meta_p, encoding="utf-8")) if os.path.isfile(meta_p) else {}
        own = os.path.join(os.path.dirname(deliv), "判分结果.json")   # 侯雅婷的跑分脚本把外部记分放在轮次目录里
        if "score" not in meta and os.path.isfile(own):
            meta["score"] = json.load(open(own, encoding="utf-8")).get("score")
        flags = {f[0] if isinstance(f, (list, tuple)) else str(f) for f in meta.get("flags") or []}
        audited = _audit_session(runs_dir, name)
        flags |= set(audited or [])
        row = {"run": name, "flags": sorted(flags), "外部记分": meta.get("score"),
               "会话复查": "无会话记录" if audited is None else "已查"}
        if meta.get("zulu_version") or meta.get("agent_version"):
            versions.add(str(meta.get("zulu_version") or meta.get("agent_version")))
        if meta.get("started"):
            dates.append(str(meta["started"])[:10])
        if any(f in DISQUALIFY for f in flags) or meta.get("timed_out") or meta.get("stalled"):
            row["作废"] = "轨迹审计：%s" % (row["flags"] or "超时/挂起")
            rows.append(row)
            continue
        wd = tempfile.mkdtemp(prefix="expert_import_")
        try:
            harness.setup_env(task, wd)
            shutil.rmtree(os.path.join(wd, "output"))
            shutil.copytree(deliv, os.path.join(wd, "output"))
            r = harness.evaluate(task, wd)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        if r.fault:
            row["故障"] = r.fault[:300]
        else:
            ex = (r.raw or {}).get("_expert") or {}
            row.update(score=r.score, passed=r.passed, 专家分=ex.get("score"), failed=ex.get("failed"))
            if meta.get("score") is not None and ex.get("score") != meta.get("score"):
                row["对账"] = "重判 %s ≠ 外部记分 %s" % (ex.get("score"), meta.get("score"))
        rows.append(row)
    ok = [x for x in rows if "score" in x]
    entry = {
        "model": model, "toolset": "external:" + agent,
        "budget": {"agent": agent + ((" " + "/".join(sorted(versions))) if versions else ""),
                   "reasoningEffort": effort, "maxSteps": None, "wallClockMin": timeout_min,
                   "runtime": "外部 agent 自带环境（交付物由我们重判）"},
        "scorerVersion": sv, "nRuns": len(ok),
        "fullPassRate": round(sum(1 for x in ok if x["passed"]) / len(ok), 4) if ok else None,
        "meanScore": round(sum(x["score"] for x in ok) / len(ok), 4) if ok else None,
        "measuredAt": measured_at or (max(dates) if dates else datetime.date.today().isoformat()),
        "外部导入": {"轮次目录": os.path.basename(os.path.abspath(runs_dir)),
                   "作废": len([x for x in rows if "作废" in x]), "故障": len([x for x in rows if "故障" in x])},
    }
    return entry, rows


def main():
    ap = argparse.ArgumentParser(description="外部专家题桥接")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("pkg"); b.add_argument("--conf", required=True); b.add_argument("--out", required=True)
    b.add_argument("--force", action="store_true")
    i = sub.add_parser("import")
    i.add_argument("task_dir"); i.add_argument("--runs", required=True)
    i.add_argument("--agent", required=True); i.add_argument("--model", required=True)
    i.add_argument("--effort", default=None); i.add_argument("--deliverables", default="deliverables")
    i.add_argument("--timeout-min", type=int, default=None)
    i.add_argument("--measured-at", help="轮次记录里没有开始时间时，手填实跑日期 YYYY-MM-DD")
    i.add_argument("--exclude", help="轮次目录名匹配这个正则的不导入（如外部已剔除的不独立轮）")
    i.add_argument("--out", help="逐轮明细写到这里（不入库）"); i.add_argument("--write-card", action="store_true")
    a = ap.parse_args()
    if a.cmd == "build":
        path, (got, problems) = build(a.pkg, json.load(open(a.conf, encoding="utf-8")), a.out, a.force)
        print("✓ %s\n  夹具：%s" % (path, got))
        for p in problems:
            print("  ⚠️  " + p)
        return 1 if problems else 0
    entry, rows = import_runs(a.task_dir, a.runs, a.agent, a.model, a.effort, a.deliverables, a.timeout_min,
                              a.exclude, a.measured_at)
    for x in rows:
        print("  %-8s %s" % (x["run"], {k: v for k, v in x.items() if k != "run"}))
    print(json.dumps(entry, ensure_ascii=False, indent=1))
    if a.out:
        json.dump({"entry": entry, "runs": rows}, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if a.write_card:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import n7_run
        from . import tasks
        print(n7_run.write_card(tasks.load_task(a.task_dir), entry)[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
