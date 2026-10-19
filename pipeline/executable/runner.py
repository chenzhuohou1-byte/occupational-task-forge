"""可执行判分编排器（范式无关）。

流程：选适配器 → probe → deploy 候选产物 → 对每个 case run_case（engine-recomputed）
→ 跑范式无关的 contract 检查（交付齐全 / 不信任自报的交叉核对）→ 聚合成判分契约 JSON。

退出码纪律（判分契约）：
  · agent 侧问题（部署失败 / case 不过 / 交付缺失 / 伪造自报）→ 退出码 0、score 有意义、fault 空。
  · 基建故障（EngineUnavailable）→ 退出码 2、fault 非空、不记成 agent 低分。

reference 结构（由 task_meta 指定，runner 读出后透传适配器）：
  cases：场景/隐藏样例清单（REST 引擎=scenarios，容器=隐藏 case 名）。
  self_report：候选自报文件名（如 test_results.json），走交叉核对、不采信。
"""
import json
import os
from pathlib import Path

from .engines import get_adapter
from .engines.base import EngineUnavailable


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _report(score=0.0, passed=False, errors=None, details=None, fault="", extra=None):
    rep = {"score": round(float(score), 4), "passed": bool(passed),
           "errors": list(errors or []), "details": list(details or []),
           "fault": fault}
    if extra:
        rep.update(extra)
    return rep


def evaluate(output_dir, reference_dir, engine_cfg, task_meta, extra_checks=None):
    """返回 (判分契约 dict, exit_code)。不 print、不 sys.exit —— 交给薄壳 scorer。

    engine_cfg: task_card.evaluation.engine，至少含 {"adapter": "flowable"|"container"}。
    task_meta:  运行所需的路径与口径（bpmn_file / deliverable / cases_dir / original_bpmn /
                container_runner / org_hierarchy / timer / self_report / self_report_crosscheck）。
    extra_checks: 可选，list[callable(output_dir, task_meta)->list[{name,passed,detail}]]，
                  范式专属静态检查（如 G1 的 contract/structural checks）。
    """
    adapter_name = (engine_cfg or {}).get("adapter")
    try:
        adapter = get_adapter(adapter_name, **(engine_cfg.get("adapter_args") or {}))
    except ValueError as exc:
        return _report(fault="adapter 配置错误：%s" % exc), 2

    checks, details = [], []

    def gate(name, ok, detail=""):
        checks.append({"name": name, "passed": bool(ok), "detail": detail})
        return ok

    # ---- 交付齐全（agent 侧）----
    required = task_meta.get("required_deliverables") or [task_meta.get("deliverable")]
    required = [f for f in required if f]
    missing = [f for f in required if not os.path.isfile(os.path.join(output_dir, f))]
    if not gate("deliverables_present", not missing, {"missing": missing}):
        return _report(0.0, False, ["缺交付文件：%s" % "、".join(missing)],
                       extra={"checks": checks}), 0

    # ---- 范式专属静态检查（不碰引擎，不信任自报）----
    for fn in (extra_checks or []):
        try:
            checks.extend(fn(output_dir, task_meta))
        except Exception as exc:  # noqa: BLE001
            return _report(fault="范式静态检查崩溃：%s" % exc, extra={"checks": checks}), 2

    # ---- 读 cases（reference）----
    cases_manifest = task_meta.get("cases_manifest")
    if cases_manifest:
        doc, err = _load_json(os.path.join(reference_dir, cases_manifest)
                              if not os.path.isabs(cases_manifest) else cases_manifest)
        if err:
            return _report(fault="cases 清单不可读：%s" % err, extra={"checks": checks}), 2
        cases = doc["scenarios"] if isinstance(doc, dict) and "scenarios" in doc else doc
    else:
        cases = task_meta.get("cases") or []

    # 空/损坏的评测清单属于坏题，不能因 all([]) 被当作满分。
    if not isinstance(cases, list) or not cases:
        return _report(fault="cases 必须是非空列表"), 2
    ids = [c.get("id") if isinstance(c, dict) else c for c in cases]
    if any(not isinstance(cid, str) or not cid for cid in ids) or len(set(ids)) != len(ids):
        return _report(fault="cases id 必须非空且唯一"), 2

    # ---- 真实工具链执行 ----
    try:
        env_info = adapter.probe()
        deploy = adapter.deploy(output_dir, task_meta)
    except EngineUnavailable as exc:
        return _report(fault="engine_unavailable: " + str(exc), extra={"checks": checks}), 2

    handle = None
    case_results = []
    try:
        if not gate("deploys_on_real_engine", deploy.ok, deploy.detail):
            return _report(0.0, False, ["候选产物无法部署/装载：%s" % deploy.detail],
                           extra={"checks": checks, "engine": env_info}), 0
        handle = deploy.handle
        for case in cases:
            cid = case["id"] if isinstance(case, dict) else case
            cr = adapter.run_case(handle, case, task_meta)
            case_results.append(cr)
            details.append({"path": "case:" + cid, "passed": cr.passed,
                            "detail": cr.detail.get("reasons") or cr.detail.get("err") or ""})
    except EngineUnavailable as exc:
        return _report(fault="engine_unavailable_during_run: " + str(exc),
                       extra={"checks": checks}), 2
    finally:
        if handle is not None:
            adapter.teardown(handle)

    engine_pass = {c.case_id: c.passed for c in case_results}

    # ---- 不信任自报：交叉核对 ----
    if task_meta.get("self_report_crosscheck") and task_meta.get("self_report"):
        cv_errors, mismatches = _crosscheck(output_dir, task_meta, cases, engine_pass)
        ok = not cv_errors and not mismatches
        gate("self_report_matches_engine", ok,
             {"schema_errors": cv_errors, "mismatches": mismatches[:10]})
        if mismatches:
            errmsg = "自报与引擎实算不一致 %d 处（报告完整性缺陷）" % len(mismatches)
            details.append({"path": "self_report", "passed": False, "detail": errmsg})

    # ---- 聚合 ----
    booleans = [c["passed"] for c in checks] + [c.passed for c in case_results]
    deploy_ok = any(c["name"] == "deploys_on_real_engine" and c["passed"] for c in checks)
    # 文件存在/能装载只是前置条件，不能给空结果程序白送分。
    scored_checks = [c["passed"] for c in checks
                     if c["name"] not in ("deliverables_present", "deploys_on_real_engine")]
    case_scores = [float(c.detail.get("score", float(c.passed))) for c in case_results]
    scores = [float(x) for x in scored_checks] + case_scores
    score = 0.0 if not deploy_ok else sum(scores) / max(1, len(scores))
    passed = deploy_ok and all(booleans) and not missing
    errors = [c["name"] for c in checks if not c["passed"]] + \
             ["case:" + cid for cid, ok in engine_pass.items() if not ok]
    return _report(score, passed, errors, details,
                   extra={"checks": checks, "engine": env_info,
                          "scenarios_passed": sum(engine_pass.values()),
                          "scenarios_total": len(engine_pass)}), 0


def _crosscheck(output_dir, task_meta, cases, engine_pass):
    """比对候选自报（如 test_results.json）与引擎实算，任一不符即报告完整性缺陷。"""
    errors, mismatches = [], []
    doc, err = _load_json(os.path.join(output_dir, task_meta["self_report"]))
    if err:
        return ["自报文件不可读：" + err], []
    rows = doc.get("scenarios") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return ["自报缺 scenarios 列表"], []
    claimed = {}
    expected_ids = {(c["id"] if isinstance(c, dict) else c) for c in cases}
    for row in rows:
        if not isinstance(row, dict):
            errors.append("自报条目必须为对象")
            continue
        sid = row.get("scenario_id") or row.get("id")
        p = row.get("pass")
        if not isinstance(sid, str):
            errors.append("scenario_id 必须为字符串")
            continue
        if sid in claimed:
            errors.append("自报场景重复：" + sid)
        if sid not in expected_ids or not isinstance(p, bool):
            errors.append("非法自报条目：%s" % sid)
        claimed[sid] = p
    if set(claimed) != expected_ids:
        errors.append("自报场景集合与 cases 不一致")
    for sid, eng in engine_pass.items():
        if claimed.get(sid) != eng:
            mismatches.append({"scenario_id": sid, "claimed": claimed.get(sid), "engine": eng})
    return errors, mismatches
