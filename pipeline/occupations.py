"""中国职业分类码表：为 N1 选材提供职业清单（来自《【导入】中国职业.xlsx》）。

occupations.json 两部分：
- positions_798：18 个 L1 领域 → 职业清单（选材主用）
- gb_2022：中华人民共和国职业分类大典(2022) 8 大类 → 中类（权威参照）
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(HERE, "occupations.json")


def load():
    with open(_PATH, encoding="utf-8") as f:
        return json.load(f)


def domains():
    """返回 [(领域, 职业数), ...]。"""
    pos = load()["positions_798"]
    return [(k, len(v)) for k, v in pos.items()]


def occupations(domain=None):
    """列出某领域的职业；domain 为空则返回全部职业（打平）。"""
    pos = load()["positions_798"]
    if domain:
        return list(pos.get(domain, []))
    return [o for v in pos.values() for o in v]
