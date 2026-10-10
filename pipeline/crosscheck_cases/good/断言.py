"""跨材料一致性断言：对 solve 的输出跑结构恒等式，返回被破坏的条目（空=都成立）。

与 solve/solve2 相互独立，用来机械兜一部分"两份实现一起错"的共模错误。
"""


def assertions(world, answer):
    bad = []
    detail = answer.get("明细结果", [])
    summ = answer.get("汇总", {})
    ids = {r["行号"] for r in detail}

    s = round(sum(r.get("可报", 0) for r in detail), 2)
    if round(summ.get("可报总额", -1), 2) != s:
        bad.append("可报总额(%s) != 明细之和(%s)" % (summ.get("可报总额"), s))

    for key in ("超标行清单", "异常行清单"):
        extra = set(summ.get(key, [])) - ids
        if extra:
            bad.append("%s 含不存在的行号 %s" % (key, sorted(extra)))

    std = world.get("城市标准", {})
    abn_true = {r["行号"] for r in world.get("明细", []) if r["城市"] not in std}
    pay = {r["行号"]: r.get("可报", 0) for r in detail}
    for no in abn_true:
        if pay.get(no, 0) not in (0, 0.0):
            bad.append("未备案行 %s 可报应为 0，实际 %s" % (no, pay.get(no)))
    return bad
