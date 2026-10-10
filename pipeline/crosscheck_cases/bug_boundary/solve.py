"""规则的形式化实现 A（标准答案由它产出）。"""


def solve(world):
    std = world["城市标准"]
    rows, over, abn, total = [], [], [], 0.0
    for r in world["明细"]:
        no, city = r["行号"], r["城市"]
        if city not in std:
            abn.append(no)
            pay = 0.0
        else:
            cap = std[city]
            pay = round(min(r["住宿单价"], cap) * r["天数"], 2)
            if r["住宿单价"] > cap:
                over.append(no)
        rows.append({"行号": no, "可报": round(pay, 2)})
        total += pay
    rows.sort(key=lambda x: x["行号"])
    return {"明细结果": rows,
            "汇总": {"可报总额": round(total, 2),
                   "超标行清单": sorted(over), "异常行清单": sorted(abn)}}
