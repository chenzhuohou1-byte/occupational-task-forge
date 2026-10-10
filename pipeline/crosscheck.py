"""双实现交叉复算（判分契约配套工具）。

把"人来验算标准答案"换成"两条独立推导必须撞上"：solve.py 与 solve2.py（后者只读
规则.md、看不到 solve.py）在同一批「世界参数」上各算一遍，逐字段比对，对不上即案例有问题
（规则有歧义，或某份实现写错）。再把 solve 的输出跑**跨材料一致性断言**，机械兜一部分共模错误。

案例目录（code 契约）：
  规则.md          闭世界事实/规则（人读，也喂给产 solve2 的模型）
  参数生成器.py     def worlds(n, seed=0, boundary=True) -> list[dict]：**边界世界前置**，再随机补满
  solve.py          def solve(world) -> dict：规则的形式化，产标准答案
  solve2.py         def solve(world) -> dict：只据 规则.md 独立再实现一遍
  断言.py (可选)     def assertions(world, answer) -> list[str]：被破坏的跨材料恒等式（空=都成立）
  容差.json (可选)   {字段路径或字段名: 容差}，逐字段覆盖全局 --tol；**只放容差、不放字段清单**
                     （不构成第二份字段真值源——同事裁决去 schema.json，这里只补 §9.2 的逐字段容差）

不设 schema.json。比对两份输出的全部叶子：数值按容差（默认 absolute），**标量数组按集合**
（顺序无关），**对象数组按行键对齐**（行键=每行第一个字段）。字段/行键集合不一致即不一致。

退出码：全一致 0 / 有不一致（含断言破坏）1 / 案例不完整或 solve2 独立性可疑 2。
"""
import argparse
import importlib.util
import json
import os
import re
import sys

REQUIRED = ("规则.md", "参数生成器.py", "solve.py", "solve2.py")
# solve2 只许见 规则.md：只认代码构造（import / open 读 solve 或标准答案），不误杀注释里的字样
_LEAK = re.compile(
    r"(?m)^\s*(?:import|from)\s+solve\b"
    r"|open\s*\([^)]*(?:solve\.py|expected|reference|标准答案|答案)")


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Tol:
    """容差解析：优先 容差.json 里按【完整路径】或【字段名】命中，否则用全局 absolute 容差。"""

    def __init__(self, global_abs=1e-6, table=None):
        self.g = float(global_abs)
        self.t = table or {}

    def _spec(self, path):
        leaf = path.rsplit("/", 1)[-1].split("[")[0]
        s = self.t.get(path, self.t.get(leaf))
        if s is None:
            return "absolute", self.g
        if isinstance(s, dict):
            return s.get("mode", "absolute"), float(s.get("value", self.g))
        return "absolute", float(s)

    def eq(self, a, b, path):
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return str(a) == str(b)
        mode, val = self._spec(path)
        if mode == "relative":
            return abs(fa - fb) / max(abs(fa), abs(fb), 1e-9) <= val
        return abs(fa - fb) <= val


def _row_key(row):
    """对象数组的对齐键：取该 dict 的第一个字段值（约定第一列是行键，同判分器）。"""
    return str(next(iter(row.values()))) if isinstance(row, dict) and row else repr(row)


def _cmp(a, b, tol, path, diffs):
    """递归比对 a/b，把不一致追加进 diffs。list 分两类：标量→集合比；对象→行键对齐。"""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                diffs.append({"path": path + "/" + str(k), "a": a.get(k, "<缺>"), "b": b.get(k, "<缺>")})
            else:
                _cmp(a[k], b[k], tol, path + "/" + str(k), diffs)
    elif isinstance(a, list) and isinstance(b, list):
        scalar = all(not isinstance(x, (dict, list)) for x in a + b)
        if scalar:
            if sorted(map(str, a)) != sorted(map(str, b)):
                diffs.append({"path": path + "[]集合", "a": a, "b": b})
        else:
            ma = {_row_key(r): r for r in a}
            mb = {_row_key(r): r for r in b}
            if set(ma) != set(mb):
                diffs.append({"path": path + "[]行键集合", "a": sorted(ma), "b": sorted(mb)})
            else:
                for k in sorted(ma):
                    _cmp(ma[k], mb[k], tol, "%s[%s]" % (path, k), diffs)
    else:
        if not tol.eq(a, b, path):
            diffs.append({"path": path or "/", "a": a, "b": b})


class CaseError(Exception):
    """案例目录不完整 / solve2 独立性可疑 —— 映射退出码 2。"""


def load_case(case_dir):
    miss = [f for f in REQUIRED if not os.path.isfile(os.path.join(case_dir, f))]
    if miss:
        raise CaseError("案例缺文件：" + "、".join(miss))
    src2 = open(os.path.join(case_dir, "solve2.py"), encoding="utf-8").read()
    hit = _LEAK.search(src2)
    if hit:
        raise CaseError("solve2.py 独立性可疑：出现 %r（只许读 规则.md）" % hit.group(0))
    base = "_cc_%d" % (abs(hash(os.path.abspath(case_dir))) % 10 ** 8)
    gen = _load_module(os.path.join(case_dir, "参数生成器.py"), base + "_gen")
    s1 = _load_module(os.path.join(case_dir, "solve.py"), base + "_s1")
    s2 = _load_module(os.path.join(case_dir, "solve2.py"), base + "_s2")
    ap = os.path.join(case_dir, "断言.py")
    asserts = _load_module(ap, base + "_as") if os.path.isfile(ap) else None
    tp = os.path.join(case_dir, "容差.json")
    table = json.load(open(tp, encoding="utf-8")) if os.path.isfile(tp) else None
    return gen, s1, s2, asserts, table


def crosscheck(case_dir, n=200, seed=0, global_tol=1e-6):
    """跑 n 个世界，逐字段比对两份实现，并对 solve 输出跑一致性断言。返回报告 dict。"""
    gen, s1, s2, asserts, table = load_case(case_dir)
    tol = Tol(global_tol, table)
    worlds = gen.worlds(n, seed)
    mism, broken = [], []
    for i, w in enumerate(worlds):
        a = s1.solve(dict(w))
        diffs = []
        _cmp(a, s2.solve(dict(w)), tol, "", diffs)
        if diffs:
            mism.append({"序号": i, "world": w, "差异": diffs})
        if asserts is not None:
            v = asserts.assertions(dict(w), a)
            if v:
                broken.append({"序号": i, "world": w, "破坏的恒等式": v})
    return {
        "case": os.path.basename(os.path.abspath(case_dir)),
        "世界数": len(worlds), "全局容差": global_tol, "逐字段容差条数": len(table or {}),
        "不一致数": len(mism), "断言破坏数": len(broken), "一致": not mism and not broken,
        "首次不一致出现在第几个世界": (mism[0]["序号"] if mism else None),
        "第一个反例": mism[0] if mism else None,
        "第一个断言破坏": broken[0] if broken else None,
    }


def run_cli(case_dir, n, seed, global_tol=1e-6):
    try:
        rep = crosscheck(case_dir, n, seed, global_tol)
    except CaseError as e:
        print(json.dumps({"错误": str(e)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if rep["一致"] else 1


_CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crosscheck_cases")


def _diffs(a, b, tol=None):
    d = []
    _cmp(a, b, tol or Tol(1e-6), "", d)
    return d


def _selftest():
    """不触网自测：好案例全一致；档位切换点注错案例被边界采样抓到、均匀随机漏掉；容差/独立性守卫可用。"""
    ok = True

    rep = crosscheck(os.path.join(_CASES, "good"), n=200, seed=0)
    c = rep["一致"]
    print(("✓" if c else "✗") + " good：200 世界全一致、无断言破坏 =%s" % c); ok &= c

    bug = os.path.join(_CASES, "bug_boundary")
    rep = crosscheck(bug, n=50, seed=0)
    c = (not rep["一致"]) and rep["首次不一致出现在第几个世界"] is not None \
        and rep["首次不一致出现在第几个世界"] <= 5
    print(("✓" if c else "✗") + " bug_boundary：边界采样 ≤5 世界内抓到（第 %s 个）"
          % rep["首次不一致出现在第几个世界"]); ok &= c

    gen, s1, s2, _, _ = load_case(bug)
    rnd = gen.worlds(200, 0, boundary=False)
    missed = sum(1 for w in rnd if _diffs(s1.solve(dict(w)), s2.solve(dict(w))))
    c = missed == 0
    print(("✓" if c else "✗") + " 边界价值：均匀随机 200 世界漏掉该 bug（不一致=%d）" % missed); ok &= c

    # 逐字段容差：同一对数值，紧容差判不一致、按字段名放宽后判一致
    a, b = {"可报": 100.0}, {"可报": 100.4}
    c = bool(_diffs(a, b, Tol(0.01))) and not _diffs(a, b, Tol(0.01, {"可报": 0.5}))
    print(("✓" if c else "✗") + " 容差.json 逐字段覆盖生效"); ok &= c

    # 独立性守卫：伪造一个 import solve 的 solve2，应判 CaseError
    c = _guard_catches()
    print(("✓" if c else "✗") + " 独立性守卫：solve2 偷看 solve 被拦"); ok &= c

    print("\n%s" % ("crosscheck 自测通过" if ok else "crosscheck 自测失败"))
    return ok


def _guard_catches():
    import tempfile
    import shutil
    good = os.path.join(_CASES, "good")
    tmp = tempfile.mkdtemp(prefix="cc_guard_")
    try:
        for f in REQUIRED:
            shutil.copy(os.path.join(good, f), os.path.join(tmp, f))
        with open(os.path.join(tmp, "solve2.py"), "w", encoding="utf-8") as fp:
            fp.write("import solve\n\ndef solve(world):\n    return {}\n")
        try:
            load_case(tmp)
            return False
        except CaseError:
            return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pipeline.crosscheck")
    ap.add_argument("--case")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tol", type=float, default=1e-6, help="全局 absolute 容差，可被 容差.json 覆盖")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest or not a.case:
        return 0 if _selftest() else 1
    return run_cli(a.case, a.n, a.seed, a.tol)


if __name__ == "__main__":
    sys.exit(main())