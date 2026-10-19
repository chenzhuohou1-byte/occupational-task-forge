"""可执行产物判分范式（executable_artifact）。

G1（BPMN/Flowable 真实引擎执行）与 E1（容器内跑交付脚本）验证出的范式：
交付可执行工程产物 → 宿主侧用真实工具链执行 → 从运行状态独立重算 →
不信任候选自报结果 → 退出码纪律（agent 失败记分 / 基建故障记测量故障）。

- `engines/base.py`：范式无关的适配器接口（EngineAdapter / EngineUnavailable）。
- `engines/flowable.py`：REST 引擎适配器（G1）。
- `engines/container.py`：容器跑脚本适配器（E1）。
- `runner.py`：编排器——部署→跑 case→读状态→重算→交叉核对自报→成形判分 JSON。
"""
