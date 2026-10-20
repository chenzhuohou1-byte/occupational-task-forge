#!/usr/bin/env python3
"""可执行产物范式自测（判分契约 §9.5）。

分两部分：
  A. 离线纯逻辑（不触引擎，CI 必跑）：范式识别、负例清单解析、engine 工具集守门、
     runner 的「不信任自报」交叉核对核心。
  B. 真实引擎集成（需迁好的 tasks/ 可执行任务 + 引擎可达，自动发现、够不着就跳过并说明）：
     golden 1.0 / 每条负例被拒 / faked 负例因自报不一致被拒 / N6/N8 可执行闸门 / 引擎不可达记故障。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import qc, harness                      # noqa: E402
from pipeline.executable import runner, selftest      # noqa: E402
from pipeline.tasks import Task, load_task, discover_tasks  # noqa: E402

ALE_RUNS = os.path.join(os.path.dirname(ROOT), "ale-runs")


# ---------------- A. 离线纯逻辑 ----------------
def test_is_executable():
    exec_task = Task("t", "/tmp/x", {"evaluation": {"type": "executable_artifact"}})
    num_task = Task("t", "/tmp/x", {"evaluation": {"type": "numeric_tabular"}})
    assert qc._is_executable(exec_task)
    assert not qc._is_executable(num_task)
    assert not qc._is_executable(Task("t", "/tmp/x", {}))
    print("✓ 范式识别 _is_executable")


def test_list_negatives():
    """自包含临时夹具，不依赖私有 ale-runs。"""
    import json
    with tempfile.TemporaryDirectory() as g1:
        negdir = os.path.join(g1, "assets", "reference", "negatives")
        os.makedirs(negdir)
        with open(os.path.join(negdir, "manifest.json"), "w") as f:
            json.dump([{"name": "neg6_faked_test_results", "faked_results": True,
                        "target_check": "self_report_matches_engine"}], f)
        negs = qc._list_negatives(Task("g1", g1, {}))
    assert negs, "没解析出任何负例"
    faked = [n for n in negs if n["faked"]]
    assert len(faked) == 1 and faked[0]["name"] == "neg6_faked_test_results", faked
    assert all(n.get("target") for n in negs), "manifest 的 target_check 没读出来"
    print("✓ 负例清单解析（manifest faked/target 标记，faked=%s）" % faked[0]["name"])


def test_selftest_gating():
    """engine 自测代理只对 executable+flowable 任务装配，其它返回 None。"""
    num = Task("t", "/tmp/x", {"evaluation": {"type": "numeric_tabular"}})
    assert selftest.build_selftest(num, "/tmp/x") is None
    container = Task("t", "/tmp/x", {"evaluation": {
        "type": "executable_artifact", "engine": {"adapter": "container"}}})
    assert selftest.build_selftest(container, "/tmp/x") is None, "容器类不该开 engine 自测"
    print("✓ engine 自测代理守门（非 executable+flowable → None）")


def test_crosscheck_core():
    """runner._crosscheck：自报与引擎一致=无不符；伪造全通过=抓出不符。"""
    import json
    cases = [{"id": "s1"}, {"id": "s2"}]
    engine_pass = {"s1": True, "s2": False}   # 引擎实算：s2 没过
    with tempfile.TemporaryDirectory() as d:
        meta = {"self_report": "test_results.json"}
        # 诚实自报：与引擎一致
        json.dump({"scenarios": [{"scenario_id": "s1", "pass": True},
                                 {"scenario_id": "s2", "pass": False}]},
                  open(os.path.join(d, "test_results.json"), "w"))
        errs, mis = runner._crosscheck(d, meta, cases, engine_pass)
        assert not errs and not mis, (errs, mis)
        # 伪造全通过：声称 s2 也过 → 必须抓出 1 处不符
        json.dump({"scenarios": [{"scenario_id": "s1", "pass": True},
                                 {"scenario_id": "s2", "pass": True}]},
                  open(os.path.join(d, "test_results.json"), "w"))
        errs, mis = runner._crosscheck(d, meta, cases, engine_pass)
        assert not errs and len(mis) == 1 and mis[0]["scenario_id"] == "s2", (errs, mis)
    print("✓ 不信任自报：诚实自报放行、伪造全通过被交叉核对抓出")


def test_sandbox_isolation_config():
    """一次性沙箱：每实例独立 project/端口/随机口令，互不复用；base/container 自洽。"""
    from pipeline.executable.engine_sandbox import DisposableFlowable
    a, b = DisposableFlowable(), DisposableFlowable()
    for s in (a, b):
        assert s.project.startswith("ale-engine-sbx-"), s.project
        assert isinstance(s.port, int) and 1 <= s.port <= 65535 and s.port != 18080
        assert s.container == s.project + "-flowable-rest-1"
        assert (":%d/" % s.port) in s.base
        env = s._env()
        for k in ("FLOWABLE_HOST_PORT", "FLOWABLE_DB_PASSWORD", "FLOWABLE_PASSWORD"):
            assert env.get(k), "沙箱 compose 环境缺 %s" % k
    # 不复用：project/端口/口令两两不同
    assert a.project != b.project and a.port != b.port
    assert a._admin_pw != b._admin_pw and a._db_pw != b._db_pw
    assert a._admin_pw != a._db_pw
    print("✓ 一次性沙箱配置隔离（独立 project/端口/随机口令，不复用 smoke 栈）")


def test_sandbox_lifecycle_and_teardown():
    """start→close 的 docker compose 调用序列正确、close 幂等（monkeypatch，不触 docker）。"""
    from subprocess import CompletedProcess
    from unittest.mock import patch
    from pipeline.executable import engine_sandbox as es

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return CompletedProcess(cmd, 0, "", "")

    class _StubAdapter:
        def probe(self):   # 立即就绪，不触网
            return {}

    sbx = es.DisposableFlowable()
    with patch.object(es.subprocess, "run", side_effect=fake_run), \
         patch.object(es.DisposableFlowable, "adapter", return_value=_StubAdapter()):
        sbx.start()
        sbx.close()
        sbx.close()   # 幂等：第二次不应再发 down

    def _subcmds(verb):
        return [c for c in calls if len(c) > 2 and c[0] == "docker" and c[1] == "compose" and verb in c]
    assert _subcmds("up"), "start 没发 compose up"
    downs = _subcmds("down")
    assert len(downs) == 1, "close 应恰好 down 一次（幂等），实际 %d 次" % len(downs)
    assert "-v" in downs[0], "down 必须带 -v 连数据卷一起销毁"
    assert all(("-p" in c and sbx.project in c) for c in _subcmds("up") + downs), \
        "compose 命令必须带本沙箱独立 project"
    print("✓ 沙箱生命周期：up→down -v 序列正确、close 幂等、始终 project 作用域")


def test_selftest_close_and_untrusted():
    """build_selftest 返回的闭包带 .close/.untrusted；untrusted 可显式关、缺省读默认。"""
    task = Task("t", "/tmp/x", {"evaluation": {
        "type": "executable_artifact", "engine": {"adapter": "flowable"}}})
    st = selftest.build_selftest(task, "/tmp/x", untrusted=False)
    assert callable(st) and callable(getattr(st, "close", None))
    assert st.untrusted is False
    st.close()   # 没起沙箱时 close 应安全无副作用
    st2 = selftest.build_selftest(task, "/tmp/x")
    assert st2.untrusted == selftest.DEFAULT_UNTRUSTED
    print("✓ selftest 闭包带 close/untrusted（默认=%s，可显式关）" % selftest.DEFAULT_UNTRUSTED)


# ---------------- B. 真实引擎集成（自动发现 + 够不着就跳过）----------------
def _find_executable_tasks():
    tdir = os.environ.get("ALE_TASKS_DIR", os.path.join(ROOT, "tasks"))
    return [t for t in discover_tasks(tdir) if qc._is_executable(t)]


def test_integration():
    tasks = _find_executable_tasks()
    if not tasks:
        print("· 跳过真实引擎集成（tasks/ 下暂无 executable_artifact 任务；迁移完成后自动生效）")
        return
    for t in tasks:
        adapter = (t.card.get("evaluation") or {}).get("engine", {}).get("adapter")
        # golden 必须 1.0；引擎够不着则记故障跳过（不误判为做错）
        g = harness.run_task(t, solver=harness.golden_solver)
        if g.fault:
            raise AssertionError("%s 集成故障（不能按跳过算通过）：%s" % (t.task_id, g.fault))
        assert abs(g.score - 1.0) < 1e-9 and g.passed, \
            "%s golden 非满分：score=%s passed=%s errors=%s" % (t.task_id, g.score, g.passed, g.errors[:5])
        print("✓ [%s/%s] golden=1.0 passed" % (t.task_id, adapter))

        # N6 可执行闸门全过
        n6_ok, n6 = qc.n6_verify(t)
        assert n6_ok, "%s N6 未过：%s" % (t.task_id, n6)
        print("✓ [%s] N6 可执行闸门全过（golden/可复现/负例被拒/反信任自报/不泄答案）" % t.task_id)

        # N8 地板：空交付 + 伪造自报都被否决
        n8_ok, n8 = qc.n8_floor_audit(t)
        assert n8_ok, "%s N8 地板未被否决：%s" % (t.task_id, n8)
        print("✓ [%s] N8 地板否决（空交付=0 且伪造自报被判负）" % t.task_id)


def test_fault_and_empty_cases():
    import json
    from subprocess import CompletedProcess
    from unittest.mock import patch
    from pipeline.executable.engines.base import DeployResult, CaseResult

    task = Task('test', '/unused', {'evaluation': {'type': 'executable_artifact'}})
    for code, payload in [(2, {'score': 0, 'passed': False}),
                          (0, {'score': 0, 'passed': False, 'fault': 'offline'})]:
        with patch.object(harness, '_run', return_value=CompletedProcess([], code, json.dumps(payload), '')):
            assert harness.evaluate(task, '/unused').fault

    class Adapter:
        def probe(self): return {}
        def deploy(self, *args): return DeployResult(True, handle={})
        def run_case(self, handle, case, meta): return CaseResult(case['id'], False, {'score': 0.0})
        def teardown(self, handle): pass

    with tempfile.TemporaryDirectory() as d, patch.object(runner, 'get_adapter', return_value=Adapter()):
        report, code = runner.evaluate(d, d, {'adapter': 'container'}, {'cases': []})
        assert code == 2 and report['fault']
        report, code = runner.evaluate(d, d, {'adapter': 'container'}, {'cases': [{'id': 'bad'}]})
        assert code == 0 and report['score'] == 0 and not report['passed']
        with open(os.path.join(d, 'report.json'), 'w') as f:
            json.dump({'scenarios': [None, {'id': [], 'pass': True},
                                    {'id': 'bad', 'pass': False}, {'id': 'bad', 'pass': False}]}, f)
        errors, _ = runner._crosscheck(d, {'self_report': 'report.json'}, [{'id': 'bad'}], {'bad': False})
        assert len(errors) == 3, errors
    print('✓ 非零退出/fault 透传、空 cases 拒绝、门槛不白送分、自报格式错误正常判负')


def main():
    test_fault_and_empty_cases()
    test_is_executable()
    test_list_negatives()
    test_selftest_gating()
    test_crosscheck_core()
    test_sandbox_isolation_config()
    test_sandbox_lifecycle_and_teardown()
    test_selftest_close_and_untrusted()
    if "--integration" in sys.argv:
        test_integration()
    print("可执行范式%s自测全过" % ("真实集成" if "--integration" in sys.argv else "离线"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
