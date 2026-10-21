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

from . import floor, harness, sandbox, scaffold, subdomains, tasks


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


class UnsafePayloadPath(RuntimeError):
    """worker 回传的文件名不安全（绝对路径 / 含 .. / realpath 跳出目标目录）。

    映射成 materialize 阶段的返修线索，不是框架故障：这类键只可能来自模型写的
    `render_inputs()` / `deliver()`，属案例侧问题。
    """


def _safe_under(base, rel, what):
    """把 worker 给的相对键安全地解析到 base 下，越界即抛 UnsafePayloadPath。

    **这是沙箱写限制的宿主侧补丁**：worker 进程只许写 scratch，但它回传的**文件名**
    会被宿主用来落盘，而宿主是完整权限。模型让 `render_inputs()` 返回 `"../../../x"`
    或 `/tmp/x` 时，`os.path.join(indir, rel)` 会跳出任务目录（绝对路径更是直接顶掉 base）——
    等于绕过沙箱。所以这里拒收绝对路径与任何 `..` 分量，并在 realpath 后断言仍在 base 之下。
    """
    if not isinstance(rel, str) or not rel.strip():
        raise UnsafePayloadPath("%s 的文件名必须是非空字符串，实得 %r" % (what, rel))
    if os.path.isabs(rel) or os.path.splitdrive(rel)[0]:
        raise UnsafePayloadPath("%s 的文件名不许是绝对路径：%r" % (what, rel))
    parts = rel.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        raise UnsafePayloadPath("%s 的文件名不许含 '..'：%r" % (what, rel))
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_real, rel))
    if target != base_real and not target.startswith(base_real + os.sep):
        raise UnsafePayloadPath("%s 的文件名解析后跳出了目标目录：%r → %s" % (what, rel, target))
    return target


def _write_payload_files(base, mapping, what):
    """把 {文件名: 文本} 落盘到 base 下，逐个走 _safe_under 校验。"""
    for rel, text in (mapping or {}).items():
        path = _safe_under(base, rel, what)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text if isinstance(text, str) else str(text))


def _invoke_worker(case_dir, mode, seed=0, n=200, hardness=None):
    """在沙箱子进程里跑 casegen_worker，取回判定/渲染 payload（模型代码不进本进程）。"""
    argv = ["-m", "pipeline.casegen_worker", os.path.abspath(case_dir),
            "--mode", mode, "--seed", str(seed), "--n", str(n)]
    if hardness is not None:
        import dataclasses
        argv += ["--hardness-json", json.dumps(dataclasses.asdict(hardness))]
    return sandbox.run_worker(argv, read_dirs=[os.path.abspath(case_dir)])


def materialize(case_dir, out_root, seed=0, subdomain=None):
    """把一个已过 crosscheck 的案例物化成任务。模型代码（solve/物化）在沙箱 worker 里跑，
    本函数只拿回渲染文本 + 计数做可信落盘。"""
    p = _invoke_worker(case_dir, "render", seed=seed)
    if p.get("stage") != "ok":
        raise RuntimeError("物化 worker 未就绪：%s" % (p.get("error") or p))
    return _materialize_from_payload(p, out_root, subdomain)


def _materialize_from_payload(payload, out_root, subdomain=None):
    """宿主侧可信半段：scaffold → 写 input/expected → 四档夹具 → 回填 card → sidecar。
    不 import 任何模型代码，只消费 worker 回传的 task/inputs/expected/rules_md/meta。"""
    task = payload["task"]
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
            # §3 材料来源：casegen 物化出来的一律是 A 类闭世界、产线自动造。
            "材料来源": {"class": "synthetic", "生成方式": "pipeline_auto",
                     "note": "casegen 闭世界程序化生成（参数生成器 + solve/solve2 双实现交叉复算）"},
            "元数据": {"机器规格": "2C4G", "超时秒": 1800, "判分构成": "纯代码",
                     "schema": "判分契约 v1.0", "生成方式": "程序化生成"}}
    res = scaffold.scaffold_task(spec, out_root)
    task_dir = res["dir"]
    assets = os.path.join(task_dir, "assets")

    # 真实 input：worker 渲染好的附件 + 规则.md（判分依据的出处）
    # 文件名来自不可信 worker，必须过 _safe_under —— 否则等于绕过沙箱的写限制。
    indir = os.path.join(assets, "input")
    _clear_dir(indir)
    _write_payload_files(indir, payload["inputs"], "render_inputs")
    with open(os.path.join(indir, "核算规则.md"), "w", encoding="utf-8") as f:
        f.write(payload.get("rules_md", ""))

    # 真实 expected：worker 侧 solve 复算好的交付（绝不手抄）
    expdir = os.path.join(assets, "reference", "expected")
    _clear_dir(expdir)
    _write_payload_files(expdir, payload["expected"], "deliver")

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
    meta = payload.get("meta") or {}
    card["priorComplexity"] = {
        "steps": meta.get("明细数", 0) * 3, "inputDocs": len(os.listdir(indir)), "crossDocJoins": 1,
        "plantedTraps": meta.get("陷阱类数", 0) or 2, "hardStops": 0,
        "outputFields": meta.get("输出字段数", 0) * 2 + 3,
        "conventionRulings": len(task["scoringBasis"]),
        "说明": "程序化物化自动填的可数项；难度非先验，须 N7 实测（§6.1）。"}
    with open(card_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(card, ensure_ascii=False, indent=2) + "\n")
    with open(os.path.join(task_dir, "dataset.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(_sidecar(task, sub), ensure_ascii=False, indent=2) + "\n")
    return {"dir": task_dir, "fixtureScores": obs}


def _path_guard_selftest():
    """worker 回传的文件名必须被挡在目标目录内（评审意见①：沙箱写限制的宿主侧补丁）。"""
    import tempfile
    base = tempfile.mkdtemp(prefix="casegen_guard_")
    try:
        for bad in ("../../../escaped.txt", "/tmp/absolute_overwrite.txt", "a/../../b.txt",
                    "", "   ", None, "..\\..\\win.txt"):
            try:
                _safe_under(base, bad, "render_inputs")
            except UnsafePayloadPath:
                continue
            raise AssertionError("越界键未被拦住：%r" % (bad,))
        assert _safe_under(base, "ok.csv", "x") == os.path.join(os.path.realpath(base), "ok.csv")
        assert _safe_under(base, "sub/ok.csv", "x").startswith(os.path.realpath(base) + os.sep)
        # 真落盘：越界键不许在磁盘上留下任何东西
        outside = os.path.join(os.path.dirname(base), "ESCAPED_SHOULD_NOT_EXIST.txt")
        try:
            _write_payload_files(base, {"../ESCAPED_SHOULD_NOT_EXIST.txt": "x"}, "render_inputs")
        except UnsafePayloadPath:
            pass
        assert not os.path.exists(outside), "越界写竟然落盘了：%s" % outside
        _write_payload_files(base, {"good.csv": "1"}, "render_inputs")
        assert os.path.isfile(os.path.join(base, "good.csv")), "正常键应能落盘"
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("✓ 路径守卫：绝对路径/../ /空键被拒且不落盘，正常键照常写入")
    return True


def _worker_timeout_selftest():
    """worker 超时必须变成 RuntimeError（评审意见②：否则 TimeoutExpired 会打崩生成循环）。"""
    try:
        sandbox.run_worker(["-c", "import time; time.sleep(5)"], [], timeout=1,
                           label="timeout-probe")
    except RuntimeError as e:
        assert "超时" in str(e), "超时异常的说明应点明超时：%s" % e
        print("✓ worker 超时 → RuntimeError（_check_round 的 except RuntimeError 能接住）")
        return True
    except BaseException as e:  # noqa: BLE001 任何别的异常类型都意味着调用方接不住
        raise AssertionError("超时应抛 RuntimeError，实际抛 %s" % type(e).__name__)
    raise AssertionError("超时竟然没抛异常")


def _selftest():
    """离线自测：把 crosscheck_cases/good 物化成任务，跑 qc 要 N6/N8 全过（难度未测→不进库）。"""
    import tempfile
    from . import qc
    guard_ok = _path_guard_selftest()
    timeout_ok = _worker_timeout_selftest()
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
        ok = n6_ok and n8_ok and not diff_ok and guard_ok and timeout_ok
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