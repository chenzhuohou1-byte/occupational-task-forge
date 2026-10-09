"""程序化地板：从 golden 产物自动合成「同结构随机假答案」。

为什么要有这个模块：N8 地板审计原来只用作者自己造的 `output_test_random` 夹具，
而**作者造的地板证不了作者自己判分器的清白**。「行键列被计进分母、照抄题面清单
就白拿约一成」这个缺陷在两条数据上各出现一次，两次都靠人工发现——人多了必然按
人头复现。本模块从 golden 产物机械合成假答案：文件名、键名、表头、行键全部保留，
**所有被判分的值替换成明显错的值**，再跑判分器，得分应当为 0；高于 0 即判分器在
给「格式对、内容错」送分。

局限（诚实声明）：纯文本交付物（md/txt）只扰动其中的数字，散文本身保留——所以
对「按关键词给分」的文本判分器，本地板偏松。作者的 random 夹具仍然保留为互补证据，
N8 取两者的**较高分**当地板。
"""
import csv
import io
import json
import os
import random
import re

FAKE_STR = "随机假值"


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _perturb_number(v, rng):
    """把数值改成明显不同的值：乘一个 2~9 的因子再加偏移，保证不等于原值。"""
    f = rng.choice([2, 3, 5, 7, 9])
    if isinstance(v, int):
        return int(v * f) + 7 if v else rng.randint(101, 9999)
    out = round(v * f + 1.37, 2)
    return out if abs(out - v) > 1e-9 else round(v + 13.71, 2)


def _fake_str(rng):
    return "%s%04d" % (FAKE_STR, rng.randint(0, 9999))


def _id_keys(items):
    """识别一组同构 dict 里的「行键」字段：**只认一个**——按出场顺序第一个取值
    两两不同且全是短字符串的键。

    行键必须保留，否则判分器根本对不上行、拿到 0 分是因为对不齐而不是因为值错，
    地板就测不到「结构对、值错」这档。只认一个是因为两三行的样本里任何「判定」
    这类枚举列也会碰巧值值不同，多认就把考点列当行键放过去了。
    """
    dicts = [x for x in items if isinstance(x, dict)]
    if len(dicts) < 2:
        return set()
    keys = [k for k in dicts[0] if all(k in d for d in dicts[1:])]
    for k in keys:
        vals = [d.get(k) for d in dicts]
        if all(isinstance(v, str) and 0 < len(v) <= 40 for v in vals) \
                and len(set(vals)) == len(vals):
            return {k}
    return set()



def _perturb(obj, rng, keep=frozenset()):
    """递归扰动：键名一律保留，叶子值一律替换；keep 里的键按原值保留（行键）。"""
    if isinstance(obj, dict):
        return {k: (v if k in keep else _perturb(v, rng, keep)) for k, v in obj.items()}
    if isinstance(obj, list):
        ids = _id_keys(obj)
        return [_perturb(x, rng, keep | ids) for x in obj]
    if isinstance(obj, bool):
        return not obj
    if _is_number(obj):
        return _perturb_number(obj, rng)
    if isinstance(obj, str):
        return _fake_str(rng)
    return obj


def _perturb_csv(text, rng):
    """CSV：表头与第一列（行键）原样保留，其余单元格全部换成错值。"""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return text
    out = [rows[0]]
    for row in rows[1:]:
        new = list(row)
        for i in range(1, len(new)):
            cell = new[i].strip()
            if not cell:
                continue
            try:
                v = float(cell.replace(",", ""))
            except ValueError:
                new[i] = _fake_str(rng)
            else:
                new[i] = ("%g" % _perturb_number(v, rng))
        out.append(new)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(out)
    return buf.getvalue()


def _perturb_text(text, rng):
    """纯文本：只把数字换成同长度的随机数字，散文保留（见模块 docstring 的局限）。"""
    def sub(m):
        s = m.group(0)
        return "".join(str(rng.randint(0, 9)) if ch.isdigit() else ch for ch in s)
    return re.sub(r"\d[\d,]*\.?\d*", sub, text)


def synthesize(output_dir, seed=20261009):
    """把 output_dir 里的 golden 产物原地改写成同结构随机假答案。返回处理过的文件清单。"""
    rng = random.Random(seed)
    touched = []
    for root, _, files in os.walk(output_dir):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            try:
                text = open(p, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue  # 二进制产物不扰动，交给作者的 random 夹具兜
            ext = fn.lower().rsplit(".", 1)[-1] if "." in fn else ""
            if ext == "json":
                try:
                    new = json.dumps(_perturb(json.loads(text), rng),
                                     ensure_ascii=False, indent=2)
                except (json.JSONDecodeError, TypeError):
                    new = _perturb_text(text, rng)
            elif ext == "csv":
                new = _perturb_csv(text, rng)
            else:
                new = _perturb_text(text, rng)
            with open(p, "w", encoding="utf-8") as f:
                f.write(new)
            touched.append(os.path.relpath(p, output_dir))
    return touched


def _selftest():
    """离线自测：键名/表头/行键必须保留，值必须全变。CI 跑这个。"""
    import tempfile
    d = tempfile.mkdtemp(prefix="floor_selftest_")
    gold = {"总计": 1014625.98, "明细": [
        {"编号": "BG-01", "金额": 1000.0, "判定": "核定"},
        {"编号": "BG-02", "金额": 2000.5, "判定": "驳回"}]}
    with open(os.path.join(d, "a.json"), "w", encoding="utf-8") as f:
        json.dump(gold, f, ensure_ascii=False)
    with open(os.path.join(d, "b.csv"), "w", encoding="utf-8") as f:
        f.write("姓名,应纳税额,税率\n张伟,9216.00,0.1\n李娜,56670.00,0.2\n")
    synthesize(d)
    got = json.load(open(os.path.join(d, "a.json"), encoding="utf-8"))
    assert set(got) == set(gold), "JSON 顶层键名被改了"
    assert got["总计"] != gold["总计"], "金额没被扰动"
    assert [r["编号"] for r in got["明细"]] == ["BG-01", "BG-02"], "行键没保住"
    assert all(set(r) == {"编号", "金额", "判定"} for r in got["明细"]), "行内键名被改了"
    assert got["明细"][0]["金额"] != 1000.0 and got["明细"][0]["判定"] != "核定", \
        "行内值没被扰动"
    rows = list(csv.reader(io.StringIO(open(os.path.join(d, "b.csv"), encoding="utf-8").read())))
    assert rows[0] == ["姓名", "应纳税额", "税率"], "CSV 表头被改了"
    assert [r[0] for r in rows[1:]] == ["张伟", "李娜"], "CSV 行键列没保住"
    assert rows[1][1] != "9216.00", "CSV 数值没被扰动"
    print("floor 自测通过：键名/表头/行键保留、值全变（%d 文件）" % 2)
    return True


if __name__ == "__main__":
    _selftest()
