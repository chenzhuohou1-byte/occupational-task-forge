"""可执行判分适配器接口。

两种已验证形态必须都能套进这一个接口：
  · REST 引擎类（G1/Flowable）：部署 BPMN 定义 → 按场景起实例、驱动任务、读历史。
  · 容器跑脚本类（E1）：把交付脚本丢进隔离容器 → 对每个隐藏 case 跑一遍、比对输出。

抽象的共性是「一次运行 = 一个 case，产出一个 CaseResult」：
  REST 引擎的 case = 一个场景（start→drive→collect）；
  容器类的 case = 一个隐藏输入样例（run script→compare）。
适配器只管「怎么把候选产物在真实工具链上对一个 case 跑出可判定的结果」，
判分聚合、交叉核对自报、退出码纪律全在 runner.py，范式无关。

退出码纪律（判分契约）：
  · agent 侧问题（部署失败 / case 不过 / 交付缺失 / 伪造自报）→ 记分，不是故障。
  · 基建故障（引擎不可达 / 容器引擎缺失 / 超时）→ 抛 EngineUnavailable，
    runner 记成测量故障（fault 非空、退出码 2），绝不记成 agent 不会做。
"""
from dataclasses import dataclass, field


class EngineUnavailable(Exception):
    """基建级故障：引擎/容器运行时不可达、超时、认证失败等。

    由 runner 捕获 → 判分 JSON 填 fault、退出码 2 → 调用方记测量故障。
    **绝不**用它表示「候选做错了」——那是正常的低分。
    """


@dataclass
class CaseResult:
    """单个 case 在真实工具链上跑出来的、可判定的结果。

    passed：这个 case 是否通过（由适配器按 case 的判定口径算出，engine-recomputed）。
    detail：诊断信息（失败原因、活动历史、输出 diff 等），进判分 details。
    """
    case_id: str
    passed: bool
    detail: dict = field(default_factory=dict)


@dataclass
class DeployResult:
    """候选产物部署/装载到真实工具链的结果。

    ok=False 表示 agent 侧问题（如 BPMN schema 不合法、脚本缺失）——记 0 分，非故障。
    handle：供后续 run_case 使用的句柄（如 definitionId、容器工作目录）。
    """
    ok: bool
    handle: object = None
    detail: dict = field(default_factory=dict)


class EngineAdapter:
    """可执行判分适配器接口。实现者：flowable.py / container.py。

    生命周期：probe → deploy →（对每个 case）run_case → teardown。
    参考元数据（隐藏 cases/scenarios、original 定义等）由 runner 从 reference 目录
    读出后透传，适配器不自己去碰 reference 以外的东西。
    """

    name = "base"

    def probe(self):
        """探活真实工具链；不可用必须抛 EngineUnavailable。返回环境信息 dict。"""
        raise NotImplementedError

    def deploy(self, output_dir, task_meta):
        """把候选交付（output_dir）装载到真实工具链。返回 DeployResult。

        装载失败属 agent 侧（返回 ok=False），基建故障抛 EngineUnavailable。
        """
        raise NotImplementedError

    def run_case(self, handle, case, task_meta):
        """在真实工具链上把候选产物对一个 case 跑一遍，返回 CaseResult。

        case 是 runner 从 reference 读出的单条（场景 / 隐藏输入样例）。
        基建故障抛 EngineUnavailable；候选跑错 → CaseResult(passed=False)。
        """
        raise NotImplementedError

    def collect_report(self, handle, task_meta):
        """可选：返回整体运行侧的额外证据（如引擎版本、残留检查）。默认空。"""
        return {}

    def teardown(self, handle):
        """清理本次评测产生的临时资源（部署、容器、临时目录）。幂等、不抛。"""
        return None
