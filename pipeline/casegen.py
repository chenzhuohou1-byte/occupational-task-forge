"""casegen 物化半段：把一个**已过 crosscheck 的案例**落成一条标准 ALE 任务（纯代码、不调模型）。

案例目录需含：参数生成器.py / solve.py / 规则.md / 物化.py（见 crosscheck_cases/good）。
物化.py 提供 INSTANCE(选定世界)、TASK(任务卡元数据)、render_inputs(世界→附件)、deliver(答案→交付文件)。

流程：scaffold 骨架 → 写真实 input（含规则.md）→ solve 产 expected → 四档夹具（pos/neg/random/partial，
random 用 floor 程序化合成）→ 跑判分器回填 observed → 补 scoringBasis/生成方式。产出可直接过 qc 的 N6/N8。
生成半段（模型产 规则/生成器/solve/solve2 + crosscheck 把关）后续再接，不在本模块。
"""
import datetime
import importlib.util
import json
import os
import shutil
import sys

from . import floor, harness, scaffold, subdomains, tasks


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # 把文件所在目录放进 sys.path，让案例里 物化.py 之类能 import 同目录的 参数生成器 等
    here = os.path.dirname(os.path.abspath(path))
    added = here not in sys.path
    if added:
        sys.path.insert(0, here)
    try:
        spec.loader.exec_module(mod)
    finally:
        if added and sys.path and sys.path[0] == here:
            sys.path.pop(0)
    return mod


def _clear_dir(d):
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d, exist_ok=True)


def _copy_expected(expected_dir, dst):
    _clear_dir(dst)
    for fn in os.listdir(expected_dir):
        src = os.path.join(expected_dir, fn)
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(dst, fn))


def _break_first(fix_dir):
    """neg：破坏按文件名排序的第一个交付文件，触发硬闸门（JSON 删键 / CSV 删行 / 否则删文件）。"""
    names = sorted(os.listdir(fix_dir))
    if not names:
        return
    first = os.path.join(fix_dir, names[0])
    if names[0].endswith(".json"):
        obj = json.load(open(first, encoding="utf-8"))
        if isinstance(obj, dict) and obj:
            obj.pop(sorted(obj)[0])
            open(first, "w", encoding="utf-8").write(json.dumps(obj, ensure_ascii=False))
            return
    if names[0].endswith(".csv"):
        rows = open(first, encoding="utf-8-sig").read().splitlines()
        if len(rows) > 1:
            open(first, "w", encoding="utf-8").write("\n".join(rows[:-1]) + "\n")
            return
    os.remove(first)


class PartialPerturbationError(RuntimeError):
    """partial 没有找到可扰动的数值字段：这是夹具生成器/框架侧问题。"""


def _perturb_json_value(value, preserve_row_key=False):
    """递归找到第一个数值标量并改掉；返回 (新值, 是否改动)。

    对对象数组保留每行第一个字段（行键）不变，避免 partial 夹具触发集合硬闸门。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if preserve_row_key:
            return value, False
        return round(float(value) + 777.0, 2), True
    if isinstance(value, dict):
        out = dict(value)
        for i, key in enumerate(out):
            out[key], changed = _perturb_json_value(out[key], preserve_row_key and i == 0)
            if changed:
                return out, True
        return out, False
    if isinstance(value, list):
        out = list(value)
        for i, item in enumerate(out):
            out[i], changed = _perturb_json_value(item, isinstance(item, dict))
            if changed:
                return out, True
        return out, False
    return value, False


def _perturb_partial(fix_dir):
    """partial：递归只改一个数值、保持文件/键/行键集合不变，得到 0<x<1。"""
    for fn in sorted(os.listdir(fix_dir)):
        p = os.path.join(fix_dir, fn)
        if fn.endswith(".json"):
            with open(p, encoding="utf-8") as f:
                obj = json.load(f)
            obj, changed = _perturb_json_value(obj)
            if changed:
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False)
                return
        elif fn.endswith(".csv"):
            rows = open(p, encoding="utf-8-sig").read().splitlines()
            if len(rows) >= 2:
                cells = rows[1].split(",")
                for i in range(1, len(cells)):
                    try:
                        cells[i] = "%.2f" % (float(cells[i]) + 500.0)
                        rows[1] = ",".join(cells)
                        open(p, "w", encoding="utf-8").write("\n".join(rows) + "\n")
                        return
                    except ValueError:
                        continue
    raise PartialPerturbationError("partial 夹具没有找到可扰动的数值字段；无法保证 0 < score < 1")


def _make_fixtures(task_dir):
    """从 expected 派生四档夹具并回填 observed（判分契约 §4）。返回 {name: observed}。"""
    exp = os.path.join(task_dir, "assets", "reference", "expected")
    fixroot = os.path.join(task_dir, "assets", "reference", "fixtures")
    _copy_expected(exp, os.path.join(fixroot, "output_test_pos"))
    _copy_expected(exp, os.path.join(fixroot, "output_test_neg"))
    _break_first(os.path.join(fixroot, "output_test_neg"))
    _copy_expected(exp, os.path.join(fixroot, "output_test_random"))
    floor.synthesize(os.path.join(fixroot, "output_test_random"))
    _copy_expected(exp, os.path.join(fixroot, "output_test_partial"))
    try:
        _perturb_partial(os.path.join(fixroot, "output_test_partial"))
    except PartialPerturbationError as e:
        raise FixtureMismatch(str(e), ["output_test_partial"], {
            "output_test_partial": {"score": 1.0, "错误": str(e)}}) from e
    t = tasks.load_task(task_dir)
    obs, det = {}, {}
    for name in ("output_test_pos", "output_test_neg", "output_test_random", "output_test_partial"):
        r = harness.run_task(t, solver=harness.fixture_solver(name))
        obs[name], det[name] = r.score, r
    return obs, det


class FixtureMismatch(RuntimeError):
    """夹具实测不符预期。.bad 是不符的档名；.info 带逐字段线索（地板在哪些字段拿了分 / 正例错在哪）。

    neg/random 不符多半是**框架侧**（判分器或地板合成器）的问题，案例返修修不好；
    pos/partial 不符多半是案例侧（交付格式、物化渲染）的问题，可以返修。
    """

    FRAMEWORK_SIDE = ("output_test_neg", "output_test_random")

    def __init__(self, msg, bad, info):
        super().__init__(msg)
        self.bad, self.info = bad, info

    @property
    def framework_side(self):
        return any(b in self.FRAMEWORK_SIDE for b in self.bad) or any(
            isinstance(v, dict) and "错误" in v
            for v in self.info.values() if isinstance(self.info, dict))


DRAFT_FIELDS = ("一句话概括", "操作路径", "答复要点梗概")


def _sidecar(task, subdomain_label):
    """数据集视图 sidecar（数据仓 _meta/dataset-schema.md v0.1）。pipeline 不读它；
    模型写在 物化.TASK 里的三个草稿字段原样搬过来、标 _draft=true 待人核，提交人留空给人填。

    「职位」留空给人填——职业码表已废弃，选题轴换成 ALE 子域，另记在 ALE子域 字段里。
    """
    def txt(v):
        return "\n".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v or "")
    side = {"_schema": "KA类数据集 sidecar v0.1",
            "_note": "本文件只服务数据集视图，pipeline 不读。模型草稿字段人核后把 _draft 删除或置 false。",
            "提交人": "", "职位": "", "ALE子域": subdomain_label or "",
            "一句话概括": "", "自动化类型": "闭世界",
            "构建日期": datetime.date.today().isoformat(), "操作路径": "", "答复要点梗概": ""}
    for k in DRAFT_FIELDS:
        side[k] = txt(task.get(k))
    side["_draft"] = {k: bool(side[k]) for k in DRAFT_FIELDS}
    return side


def materialize(case_dir, out_root, seed=0, subdomain=None, instance=None):
    物化 = _load(os.path.join(case_dir, "物化.py"), "_cg_mat")
    solve_mod = _load(os.path.join(case_dir, "solve.py"), "_cg_solve")
    # 优先用调用方（难度门）挑好的世界；否则 物化.INSTANCE；再否则生成器第一个世界
    world = instance if instance is not None else getattr(物化, "INSTANCE", None)
    if world is None:
        gen = _load(os.path.join(case_dir, "参数生成器.py"), "_cg_gen")
        world = gen.worlds(1, seed)[0]
    task = 物化.TASK

    # 领域/子域/taxonomy 以 ALE 码表为准，不采信模型自己写的 TASK["领域"]
    tx, domain, sub = {}, task["领域"], task.get("子域", "")
    if subdomain:
        s = subdomains.get(subdomain)
        tx = subdomains.taxonomy(subdomain)
        domain = s["domain_code"]
        sub = "%s %s" % (s["id"], s["name"] or "")
    spec = {"领域": domain, "任务名": task["任务名"], "task_id": task["task_id"],
            "子域": sub, "taxonomy": tx, "prompt": task["prompt"],
            "交付要求": task["交付要求"],
            # §3 材料来源：casegen 物化出来的一律是 A 类闭世界、产线自动造。这里直接写死，
            # 因为这条路径的来源是确定的；B 类真实源题由作者在卡上自行声明。
            "材料来源": {"class": "synthetic", "生成方式": "pipeline_auto",
                     "note": "casegen 闭世界程序化生成（参数生成器 + solve/solve2 双实现交叉复算）"},
            "元数据": {"机器规格": "2C4G", "超时秒": 1800, "判分构成": "纯代码",
                     "schema": "判分契约 v1.0", "生成方式": "程序化生成"}}
    res = scaffold.scaffold_task(spec, out_root)
    task_dir = res["dir"]
    assets = os.path.join(task_dir, "assets")

    # 真实 input：渲染世界参数 + 拷入规则.md（判分依据的出处）
    indir = os.path.join(assets, "input")
    _clear_dir(indir)
    for rel, text in 物化.render_inputs(world).items():
        with open(os.path.join(indir, rel), "w", encoding="utf-8") as f:
            f.write(text)
    shutil.copy(os.path.join(case_dir, "规则.md"), os.path.join(indir, "核算规则.md"))

    # 真实 expected：solve 复算（绝不手抄）
    expdir = os.path.join(assets, "reference", "expected")
    _clear_dir(expdir)
    answer = solve_mod.solve(dict(world))
    for fn, text in 物化.deliver(answer).items():
        with open(os.path.join(expdir, fn), "w", encoding="utf-8") as f:
            f.write(text)

    obs, det = _make_fixtures(task_dir)
    bad, msgs = [], []
    for name, want in (("output_test_pos", 1.0), ("output_test_neg", 0.0),
                       ("output_test_random", 0.0)):
        if abs(obs[name] - want) > 1e-9:
            bad.append(name)
            msgs.append("%s 实测 %s ≠ 期望 %s" % (name, obs[name], want))
    if not (0.0 < obs["output_test_partial"] < 1.0):
        bad.append("output_test_partial")
        msgs.append("output_test_partial 实测 %s 不在 (0,1)" % obs["output_test_partial"])
    if bad:
        info = {}
        for name in bad:
            r = det[name]
            # 地板/负例要看「白拿分的字段」，正例/部分分要看「判错的字段」
            want_correct = name in FixtureMismatch.FRAMEWORK_SIDE
            hit = [d for d in (r.details or []) if bool(d.get("correct")) == want_correct]
            info[name] = {"score": r.score, "errors": r.errors,
                          ("白拿分的字段" if want_correct else "判错的字段"): hit[:12]}
        raise FixtureMismatch("物化出的夹具不符预期：" + "；".join(msgs), bad, info)

    # 回填 task_card：fixtureScores(observed=期望)、scoringBasis、priorComplexity
    card_path = os.path.join(task_dir, "task_card.json")
    card = json.load(open(card_path, encoding="utf-8"))
    fs = card["evaluation"]["fixtureScores"]
    for name in fs:
        if name in obs:
            fs[name] = {"expected": round(obs[name], 4), "observed": round(obs[name], 4)}
    card["scoringBasis"] = task["scoringBasis"]
    card["priorComplexity"] = {
        "steps": len(world.get("明细", [])) * 3, "inputDocs": len(os.listdir(indir)), "crossDocJoins": 1,
        "plantedTraps": len(getattr(物化, "TRAPS", None) or {}) or 2, "hardStops": 0,
        "outputFields": len(answer.get("明细结果", [])) * 2 + 3,
        "conventionRulings": len(task["scoringBasis"]),
        "说明": "程序化物化自动填的可数项；难度非先验，须 N7 实测（§6.1）。"}
    with open(card_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(card, ensure_ascii=False, indent=2) + "\n")
    with open(os.path.join(task_dir, "dataset.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(_sidecar(task, sub), ensure_ascii=False, indent=2) + "\n")
    return {"dir": task_dir, "fixtureScores": obs}


def _selftest():
    """离线自测：把 crosscheck_cases/good 物化成任务，跑 qc 要 N6/N8 全过（难度未测→不进库）。"""
    import tempfile
    from . import qc
    case = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crosscheck_cases", "good")
    out = tempfile.mkdtemp(prefix="casegen_selftest_")
    try:
        r = materialize(case, out)
        t = tasks.load_task(r["dir"])
        n6_ok, n6 = qc.n6_verify(t)
        n8_ok, n8 = qc.n8_floor_audit(t)
        diff_ok, _ = qc.difficulty_tier(t)
        print(("✓" if n6_ok else "✗") + " N6 全过" +
              ("" if n6_ok else "：%s" % [c["检查"] for c in n6["checks"] if not c["通过"]]))
        print(("✓" if n8_ok else "✗") + " N8 地板=0（%s）" % n8.get("各档地板"))
        print(("✓" if not diff_ok else "✗") + " 难度未测→定不出级→不进库（%s）" %
              (not diff_ok))
        ok = n6_ok and n8_ok and not diff_ok
        print("\n%s" % ("casegen 物化半段自测通过" if ok else "casegen 物化半段自测失败"))
        return ok
    finally:
        shutil.rmtree(out, ignore_errors=True)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="pipeline.casegen")
    ap.add_argument("--case", help="已过 crosscheck 的案例目录")
    ap.add_argument("--out", default="tasks", help="物化到哪个 tasks 根目录")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest or not a.case:
        return 0 if _selftest() else 1
    r = materialize(a.case, a.out)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())