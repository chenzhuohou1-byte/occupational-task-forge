"""独立实现 B（**注错版**，仅供 crosscheck 自测）：超标判定把「严格大于」误写成「大于等于」。

后果：住宿单价**恰等于**该城市每日封顶时，本实现把该行误判为「超标」，而规则/solve.py 不算超标。
这个 bug 只在「单价==封顶」这个切换点暴露——封顶取非 10 整数、随机单价取 10 的倍数，
故均匀随机几乎永远采不到，边界采样必然撞上。crosscheck 应在前几个世界内抓到并打印反例。
"""


def _line(r, std):
    city = r["城市"]
    if city not in std:
        return 0.0, "异常"
    cap = std[city]
    pay = round(min(r["住宿单价"], cap) * r["天数"], 2)
    return pay, ("超标" if r["住宿单价"] >= cap else "正常")  # BUG: >= 应为 >


def solve(world):
    std = world["城市标准"]
    detail, over, abn = [], [], []
    for r in world["明细"]:
        pay, tag = _line(r, std)
        detail.append({"行号": r["行号"], "可报": round(pay, 2)})
        if tag == "超标":
            over.append(r["行号"])
        elif tag == "异常":
            abn.append(r["行号"])
    total = round(sum(d["可报"] for d in detail), 2)
    return {"明细结果": sorted(detail, key=lambda x: x["行号"]),
            "汇总": {"可报总额": total, "超标行清单": sorted(over), "异常行清单": sorted(abn)}}
