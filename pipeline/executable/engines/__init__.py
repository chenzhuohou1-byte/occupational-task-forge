"""engines 包：可执行判分适配器。

base 定义接口，flowable / container 是两个已验证的实现。
按名字取适配器用 get_adapter(name)。
"""
from . import base


def get_adapter(name, **kwargs):
    """按 task_card.evaluation.engine.adapter 取适配器实例。"""
    if name == "flowable":
        from . import flowable
        return flowable.FlowableAdapter(**kwargs)
    if name == "container":
        from . import container
        return container.ContainerAdapter(**kwargs)
    raise ValueError("未知 executable 适配器：%r（可选 flowable / container）" % name)


__all__ = ["base", "get_adapter"]
