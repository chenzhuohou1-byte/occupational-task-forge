"""物化元数据：把本案例（已过 crosscheck 的 规则+生成器+solve）落成一条标准 ALE 任务。

casegen 的物化半段读这里：选定一个世界实例 → 渲染成 input 附件 → solve 产 expected →
套判分器+四档夹具+task_card。纯确定性、不调模型。
"""

# 选定的世界实例（确定性，不靠随机采样）：覆盖 超标/保底/未备案/单价==封顶 四种行为。
INSTANCE = {
    "城市标准": {"北京": 503, "上海": 517, "成都": 349},
    "明细": [
        {"行号": 1, "城市": "北京", "住宿单价": 600, "天数": 3},   # 超标：600>503
        {"行号": 2, "城市": "成都", "住宿单价": 300, "天数": 2},   # 保底：300<349
        {"行号": 3, "城市": "广州", "住宿单价": 400, "天数": 2},   # 未备案 → 异常、可报0
        {"行号": 4, "城市": "上海", "住宿单价": 517, "天数": 1},   # 单价==封顶：不超标
    ],
}

TASK = {
    "领域": "财务",
    "任务名": "差旅住宿报销核算(合成)",
    "task_id": "demo-travel-reimburse-syn-001",
    "子域": "按城市封顶的住宿报销逐行核算（crosscheck 验证 + 程序化物化）",
    "prompt": (
        "你是报销核算岗。input/ 下有《城市住宿标准表》《报销明细》和《核算规则》。"
        "请严格按《核算规则》逐行核算每行可报住宿费，判定超标与城市未备案，并汇总。"
        "一律以 input/ 材料为准，不得引用材料之外的标准。"
        "在 output/ 下交付 明细结果.csv 与 判定汇总.json。"),
    "交付要求": {
        "明细结果.csv": "CSV，表头必须为: 行号,可报",
        "判定汇总.json": "JSON 对象，顶层键必须且仅为: 可报总额，超标行清单，异常行清单",
    },
    "scoringBasis": [
        {"point": "每行可报 = min(住宿单价, 该城市每日封顶) × 天数", "source": "material",
         "ref": "input/核算规则.md"},
        {"point": "住宿单价>封顶判超标(严格大于)；城市未备案→可报0且计异常", "source": "material",
         "ref": "input/核算规则.md"},
        {"point": "各城市每日住宿封顶取值", "source": "material",
         "ref": "input/城市住宿标准表.csv"},
    ],
}


def render_inputs(world):
    """世界参数 → input 附件文本（规则.md 由 casegen 另行拷入）。"""
    std = "城市,每日住宿封顶\n" + "".join(
        "%s,%d\n" % (c, v) for c, v in world["城市标准"].items())
    rows = "行号,城市,住宿单价,天数\n" + "".join(
        "%s,%s,%s,%s\n" % (r["行号"], r["城市"], r["住宿单价"], r["天数"])
        for r in world["明细"])
    return {"城市住宿标准表.csv": std, "报销明细.csv": rows}


def deliver(answer):
    """solve 输出 → 交付文件文本。行号用字符串，判分器按首列行键对齐。"""
    import json
    csv_rows = "行号,可报\n" + "".join(
        "%s,%.2f\n" % (r["行号"], r["可报"]) for r in answer["明细结果"])
    return {"明细结果.csv": csv_rows,
            "判定汇总.json": json.dumps(answer["汇总"], ensure_ascii=False, indent=2)}
