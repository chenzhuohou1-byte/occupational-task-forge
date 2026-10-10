"""独立实现 B：只据 规则.md 另写一遍（换一种写法）。good 案例里它是对的。"""


def _line(r, std):
    city = r["城市"]
    if city not in std:
        return 0.0, "异常"
    cap = std[city]
    pay = round(min(r["住宿单价"], cap) * r["天数"], 2)
    return pay, ("超标" if r["住宿单价"] > cap else "正常")


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
