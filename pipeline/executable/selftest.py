"""受限引擎自测代理（服务 N7 的 agent-file-engine 工具集，判分契约 §9.5）。

给 agent 在解题过程中一条「把自己造的产物丢到真实引擎上跑自己写的场景」的通道——
这正是真实工程师会做的事（部署、起实例、看跑不跑得通）。但三条红线必须守住：

    1. 不泄答案：只读 agent 自己的 output/ 与 agent 当场提供的场景；**绝不碰** reference/、
     golden、negatives、隐藏评测场景。隐藏考点仍然隐藏。
     2. 生产隔离：不可信自测默认（EXEC_ENGINE_SANDBOX）起一套一次性 Flowable+PG 沙箱
     （独立 project/网络/卷/随机端口/随机口令，见 engine_sandbox.py），本轮自测在其中跑、
     用完连卷销毁；恶意 BPMN 碰不到正式评测引擎与他人数据，也只跑候选定义、不部署 original。
     3. 非等价：用了这个工具集的测量记为 toolset=agent-file-engine，**不参与难度定级**
     （qc 只认 agent-file-python / agent-file-tools）——拿到引擎反馈的难度和盲测不可比。

安全边界：不可信模式每轮解题独占一套即用即弃引擎+库，解题结束 `compose down -v` 销毁，
不残留、不与正式评测引擎共享状态。仅在可信参考产物功能验证时可置 EXEC_ENGINE_SANDBOX=0
复用长驻 smoke 栈省时——该退路不得开放给不可信 agent。
"""
import os

from .engines import get_adapter
from .engines.base import EngineUnavailable
from .engine_sandbox import DisposableFlowable

MAX_SCENARIOS_PER_CALL = 20   # 单次自测最多跑这么多场景，挡住把引擎当免费算力刷
MAX_CALLS_PER_RUN = 10        # 单次 N7 解题最多自测这么多轮，超过即拒（预算纪律）

# 不可信隔离默认开：此工具集本就是给不可信 agent 的，默认每次解题起一次性沙箱引擎。
# 仅在可信参考产物功能验证、想复用长驻 smoke 栈省时时，置 EXEC_ENGINE_SANDBOX=0。
DEFAULT_UNTRUSTED = os.getenv("EXEC_ENGINE_SANDBOX", "1") != "0"


def build_selftest(task, work_dir, untrusted=None):
    """按任务卡装配一个「只认候选产物 + agent 自带场景」的引擎自测闭包。

    仅对 evaluation.type==executable_artifact 且 engine.adapter==flowable 的任务可用；
    其它一律返回 None（调用方据此拒绝开 engine 工具集）。

    untrusted（缺省读 EXEC_ENGINE_SANDBOX，默认 True）：不可信隔离。为 True 时首次自测
    会起一套一次性 Flowable+PG 沙箱（独立 project/网络/库/端口），本次解题的所有自测都在
    这套沙箱里跑，解题结束由调用方 close() 连卷销毁——恶意 BPMN 碰不到正式评测引擎与他人数据。
    为 False 时复用长驻 smoke 栈（只应用于可信参考产物的功能验证）。

    返回的闭包带 .close()：调用方务必在解题 finally 里调用，确保沙箱被销毁、不残留跑着的引擎。
    """
    ev = task.card.get("evaluation") or {}
    if str(ev.get("type", "")).strip() != "executable_artifact":
        return None
    engine_cfg = ev.get("engine") or {}
    if engine_cfg.get("adapter") != "flowable":
        return None   # 目前只有 Flowable 适配器支持 agent 侧自测；容器类自测另议

    untrusted = DEFAULT_UNTRUSTED if untrusted is None else bool(untrusted)
    base_meta = dict(ev.get("task_meta") or {})
    out_dir = os.path.join(work_dir, "output")
    # 自测 task_meta：只保留跑候选定义需要的字段，org_hierarchy 指向 input 里的公开副本。
    # 刻意不带 original_bpmn / cases_manifest / self_report —— 自测不碰 original、不碰隐藏场景。
    meta = {"bpmn_file": base_meta.get("bpmn_file", "modified_process.bpmn20.xml")}
    if base_meta.get("timer"):
        meta["timer"] = base_meta["timer"]
    org = base_meta.get("org_hierarchy")
    if isinstance(org, str):
        # task_meta 里存的是任务目录下的相对/绝对路径；自测要用 agent 工作目录 input/ 的公开副本
        cand = os.path.join(work_dir, "input", "starter_project", "org_hierarchy.json")
        meta["org_hierarchy"] = cand if os.path.isfile(cand) else org

    state = {"calls": 0, "sandbox": None}

    def _acquire_adapter():
        """取本轮自测用的适配器：不可信→懒起一次性沙箱并复用；可信→长驻 smoke 栈。

        可能抛 EngineUnavailable（沙箱起不来/引擎不可达），由 run() 收敛成 engine_unavailable。
        """
        if not untrusted:
            adapter = get_adapter("flowable", **(engine_cfg.get("adapter_args") or {}))
            adapter.probe()
            return adapter
        if state["sandbox"] is None:
            sbx = DisposableFlowable()
            sbx.start()                 # 起栈+就绪轮询；失败会自行销毁再抛
            state["sandbox"] = sbx
        return state["sandbox"].adapter()

    def run(scenarios):
        """跑一轮自测，返回给 agent 看的结果 dict（永不抛，异常收敛成可读文本）。"""
        state["calls"] += 1
        if state["calls"] > MAX_CALLS_PER_RUN:
            return {"ok": False, "error": "引擎自测次数超上限（%d 次），请凭已有反馈定稿后交卷"
                    % MAX_CALLS_PER_RUN}
        if not isinstance(scenarios, list) or not scenarios:
            return {"ok": False, "error": "engine_selftest 需要非空的 scenarios 列表"
                    "（每个场景至少含 id；可带 input_variables/expected_end_event/"
                    "must_activities/forbidden_activities/advance_timer 等，口径同 test_scenarios.json）"}
        if len(scenarios) > MAX_SCENARIOS_PER_CALL:
            return {"ok": False, "error": "单次自测场景数超上限（%d），请分批"
                    % MAX_SCENARIOS_PER_CALL}
        bpmn = os.path.join(out_dir, meta["bpmn_file"])
        if not os.path.isfile(bpmn):
            return {"ok": False, "error": "还没在 output/ 写出 %s，无法部署自测" % meta["bpmn_file"]}

        try:
            adapter = _acquire_adapter()
        except EngineUnavailable as exc:
            return {"ok": False, "engine_unavailable": True,
                    "error": "真实引擎当前不可达，本轮无法自测（不影响你继续写文件）：" + str(exc)[:200]}

        handle = None
        try:
            deploy = adapter.deploy(out_dir, meta)
            if not deploy.ok:
                return {"ok": True, "deployed": False,
                        "deploy_error": deploy.detail,
                        "hint": "候选 BPMN 无法部署到真实引擎（schema/结构问题），先修部署再谈场景"}
            handle = deploy.handle
            results = []
            for sc in scenarios[:MAX_SCENARIOS_PER_CALL]:
                if not isinstance(sc, dict) or not sc.get("id"):
                    results.append({"scenario_id": None, "pass": False,
                                    "reasons": ["场景缺 id 字段"]})
                    continue
                try:
                    cr = adapter.run_case(handle, sc, meta)
                    results.append({"scenario_id": cr.case_id, "pass": cr.passed,
                                    "reasons": cr.detail.get("reasons") or []})
                except EngineUnavailable as exc:
                    return {"ok": False, "engine_unavailable": True,
                            "error": "自测途中引擎不可达：" + str(exc)[:200]}
                except Exception as exc:  # noqa: BLE001 单场景崩不该拖垮整轮自测
                    results.append({"scenario_id": sc.get("id"), "pass": False,
                                    "reasons": ["场景执行异常：" + str(exc)[:200]]})
            return {"ok": True, "deployed": True,
                    "deployment": deploy.detail.get("deploymentId"),
                    "scenarios_passed": sum(1 for r in results if r.get("pass")),
                    "scenarios_total": len(results), "results": results,
                    "note": "这是你自带场景的引擎实跑结果；评测用的隐藏场景与标准答案你看不到。"}
        finally:
            if handle is not None:
                adapter.teardown(handle)

    def close():
        """销毁本轮解题起的一次性沙箱（幂等）。调用方务必在解题 finally 里调。"""
        sbx = state.get("sandbox")
        if sbx is not None:
            sbx.close()
            state["sandbox"] = None

    run.close = close
    run.untrusted = untrusted
    return run
