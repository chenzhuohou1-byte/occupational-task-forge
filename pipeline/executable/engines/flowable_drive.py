"""Flowable 单场景驱动与重算（源自 G1 engine.py 的 run_scenario / _grade）。

拆成独立模块：把「起实例→逐 userTask 完成→推进 timer→读历史→按场景口径重算」
的大段逻辑和适配器壳子分开。所有引擎读写都经 adapter（FlowableAdapter）。
"""
import json
import os
import time
from pathlib import Path

from .base import EngineUnavailable


def _typed(name, value):
    if isinstance(value, bool):
        t = "boolean"
    elif isinstance(value, int):
        t = "integer"
    elif isinstance(value, float):
        t = "double"
    else:
        t = "string"
    return {"name": name, "value": value, "type": t, "scope": "global"}


def _start(ad, definition_id, business_key, variables):
    p = ad._api("runtime/process-instances", {
        "processDefinitionId": definition_id, "businessKey": business_key,
        "variables": [_typed(k, v) for k, v in variables.items()]})
    return p["id"]


def _tasks(ad, pid):
    return ad._rows("runtime/tasks", processInstanceId=pid)


def _complete(ad, task_id, outputs):
    body = {"action": "complete"}
    if outputs:
        body["variables"] = [_typed(k, v) for k, v in outputs.items()]
    ad._api("runtime/tasks/" + task_id, body)


def _ended(ad, pid):
    hp = ad._api("history/historic-process-instances/" + pid)
    return bool(hp.get("endTime")), hp


def _acts(ad, pid):
    return ad._rows("history/historic-activity-instances", processInstanceId=pid)


def _vars(ad, pid):
    out = {}
    for v in ad._rows("history/historic-variable-instances", processInstanceId=pid):
        var = v.get("variable", {})
        out[var.get("name")] = var.get("value")
    return out


def _timers(ad, pid):
    return ad._rows("management/timer-jobs", processInstanceId=pid)


def _deadletters(ad, pid):
    return ad._rows("management/deadletter-jobs", processInstanceId=pid)


def _next_output(outs, counter, key):
    val = outs.get(key)
    if val is None:
        return {}
    if isinstance(val, list):
        idx = counter.get(key, 0)
        counter[key] = idx + 1
        return val[idx] if idx < len(val) else (val[-1] if val else {})
    return val


def _org_allowed(task_meta, key):
    org_path = task_meta.get("org_hierarchy")
    if not org_path or not os.path.isfile(org_path):
        return None
    roles = json.loads(Path(org_path).read_text())["roles"]
    return ["g1-" + r for r, spec in roles.items() if key in spec.get("may_assign", [])]


def _wait_timer(ad, pid, timer_wait, evidence, timer_task_key):
    start = time.monotonic()
    deadline = start + timer_wait
    while True:
        ended, _ = _ended(ad, pid)
        non_timer = [t for t in _tasks(ad, pid) if t["taskDefinitionKey"] != timer_task_key]
        if ended or non_timer:
            evidence.setdefault("timer_observed_wait_s", round(time.monotonic() - start, 2))
            return
        if time.monotonic() >= deadline:
            evidence["engine_error"] = "real timer did not fire within %ss" % timer_wait
            dl = _deadletters(ad, pid)
            evidence["timerDeadletters"] = dl
            if dl:
                raise RuntimeError("candidate timer job failed; deadletter evidence captured")
            raise EngineUnavailable("real timer wall-clock observation timeout")
        time.sleep(1)


def run_scenario(ad, definition_id, scenario, task_meta, max_steps=60, timer_wait=120):
    """在真实引擎上驱动一个场景，返回含 pass/reasons 的证据 dict。"""
    sid = scenario["id"]
    variables = dict(scenario.get("input_variables") or {})
    for rv in ad.role_vars:
        variables.setdefault(rv, "g1-" + rv)
    try:
        pid = _start(ad, definition_id, "ex-" + sid + "-" + str(int(time.time() * 1000)), variables)
    except RuntimeError as exc:
        return {"scenario_id": sid, "pass": False, "reasons": ["start failed: " + str(exc)],
                "engine_error": str(exc), "steps": []}

    ev = {"scenario_id": sid, "process_instance_id": pid, "steps": [], "engine_error": None,
          "runtime_checks": [], "variable_snapshots": []}
    task_outputs = scenario.get("task_outputs") or {}
    counter = {}
    advance_timer = bool(scenario.get("advance_timer"))
    timer_task_key = scenario.get("timer_task")

    try:
        steps = 0
        while steps < max_steps:
            steps += 1
            if _ended(ad, pid)[0]:
                break
            ts = _tasks(ad, pid)
            if not ts:
                if advance_timer:
                    if not _timers(ad, pid):
                        ev["engine_error"] = "no active tasks and no timer job"
                        break
                    _wait_timer(ad, pid, timer_wait, ev, timer_task_key)
                    continue
                ev["engine_error"] = "no active tasks but instance not ended (possible deadlock)"
                break
            progressed = False
            for t in ts:
                key = t["taskDefinitionKey"]
                if advance_timer and key == timer_task_key:
                    continue
                allowed = _org_allowed(task_meta, key)
                if allowed is not None:
                    ev["runtime_checks"].append({"name": "runtime_authorization:" + key,
                                                 "passed": t.get("assignee") in allowed})
                outs = _next_output(task_outputs, counter, key) if key not in ad.auto_complete else {}
                _complete(ad, t["id"], outs)
                ev["steps"].append({"completed": key, "outputs": outs})
                snap = _vars(ad, pid)
                ev["variable_snapshots"].append({"after": key, "replanCount": snap.get("replanCount"),
                    "out_complianceLatch": snap.get("out_complianceLatch"),
                    "out_recallFlagged": snap.get("out_recallFlagged")})
                progressed = True
            if not progressed:
                if advance_timer and _timers(ad, pid):
                    _wait_timer(ad, pid, timer_wait, ev, timer_task_key)
                else:
                    ev["engine_error"] = "stuck: active tasks but none completable"
                    break

        ended, hp = _ended(ad, pid)
        acts = _acts(ad, pid)
        hv = _vars(ad, pid)
        ended_acts = sorted({a["activityId"] for a in acts if a.get("endTime")})
        all_acts = sorted({a["activityId"] for a in acts})
        ev.update({"ended": ended, "ended_activities": ended_acts, "variables": hv,
                   "replanCount": hv.get("replanCount")})
        residue = {"tasks": _tasks(ad, pid),
                   "timer_jobs": _timers(ad, pid), "deadletter_jobs": _deadletters(ad, pid),
                   "jobs": ad._rows("management/jobs", processInstanceId=pid)}
        ev["residue_counts"] = {k: len(v) for k, v in residue.items()}
        ev["runtime_checks"].append({"name": "no_residual_activity_or_failed_job",
                                     "passed": ended and not any(residue.values())})
        for before, after in scenario.get("required_order", []):
            ba = [a for a in acts if a["activityId"] == before and a.get("endTime")]
            aa = [a for a in acts if a["activityId"] == after]
            ordered = not aa or (bool(ba) and min(a["endTime"] for a in ba) <= min(a["startTime"] for a in aa))
            ev["runtime_checks"].append({"name": "order:" + before + "->" + after, "passed": ordered})
        for key, cnt in scenario.get("activity_counts", {}).items():
            ev["runtime_checks"].append({"name": "count:" + key,
                                         "passed": sum(a["activityId"] == key for a in acts) == cnt})
        if "expected_replanCount" in scenario:
            ev["runtime_checks"].append({"name": "replan_boundary",
                "passed": hv.get("replanCount") == scenario["expected_replanCount"]})
        ev["runtime_checks"].append({"name": "latch_written_by_engine",
            "passed": hv.get("out_complianceLatch") is bool(hv.get("out_recallFlagged"))
            and all(s["out_complianceLatch"] is True for s in ev["variable_snapshots"]
                    if s.get("out_recallFlagged") is True)})

        ev["pass"], ev["reasons"] = _grade(scenario, ended, ended_acts, all_acts, hv, ev)
        return ev
    except EngineUnavailable:
        raise
    except RuntimeError as exc:
        ev["engine_error"] = str(exc)
        ev["pass"] = False
        ev["reasons"] = ["engine runtime error: " + str(exc)]
        return ev


def _grade(scenario, ended, ended_acts, all_acts, hv, ev):
    reasons = []
    ok = True
    if scenario.get("should_complete", True) and not ended:
        ok = False
        reasons.append("instance did not complete")
    exp_end = scenario.get("expected_end_event")
    if exp_end:
        if exp_end not in ended_acts:
            ok = False
            reasons.append("expected end event %s not reached" % exp_end)
        # 冲突终点：场景显式给 conflicting_end_events 用之；否则沿用 G1 的
        # normalEnd/escalationEnd 二选一默认（两个业务终点互斥）。
        conflicting = scenario.get("conflicting_end_events")
        if conflicting is None and exp_end in ("normalEnd", "escalationEnd"):
            conflicting = ["escalationEnd" if exp_end == "normalEnd" else "normalEnd"]
        for other in (conflicting or []):
            if other in ended_acts:
                ok = False
                reasons.append("conflicting end event %s also reached" % other)
    for must in scenario.get("must_activities", []):
        if must not in all_acts:
            ok = False
            reasons.append("missing required activity %s" % must)
    for forb in scenario.get("forbidden_activities", []):
        if forb in all_acts:
            ok = False
            reasons.append("forbidden activity %s occurred" % forb)
    for name, expected in (scenario.get("must_variables") or {}).items():
        if hv.get(name) != expected:
            ok = False
            reasons.append("variable %s expected %r got %r" % (name, expected, hv.get(name)))
    for check in ev.get("runtime_checks", []):
        if not check["passed"]:
            ok = False
            reasons.append(check["name"])
    if ev.get("engine_error"):
        ok = False
        reasons.append("engine_error: " + ev["engine_error"])
    return ok, reasons
