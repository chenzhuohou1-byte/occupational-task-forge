"""成本统计：按节点/模型累计 token，按网关价目估算人民币成本。

**价目表不在本文件里。** 它是内部商务信息（含结算倍率），放在 `pricing.local.json`，
不入版本库。缺这个文件时产线照常跑，所有模型记为「未计价」——不静默按 0，避免误导。
路径可用环境变量 `ALE_PRICING_FILE` 覆盖。
- 峰谷计价的模型（DeepSeek）价目文件里按**高峰价**保守估。
"""
import json
import os
from dataclasses import dataclass, field

from . import config as _config      # 先 import config，它会把仓库根的 .env 读进 os.environ

_HERE = os.path.dirname(os.path.abspath(__file__))
PRICING_FILE = os.environ.get("ALE_PRICING_FILE", os.path.join(_HERE, "pricing.local.json"))


def _load_pricing(path=PRICING_FILE):
    """读价目文件。缺失或损坏时返回空表（全部记为未计价），不抛异常。"""
    empty = {"prices": {"mock": (0.0, 0.0)}, "multipliers": {},
             "rmb_per_request": 0.3, "bill_url": "",
             "note": "未加载价目表（缺 pricing.local.json），全部模型记为未计价"}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return empty
    prices = {k: tuple(v) for k, v in (raw.get("prices") or {}).items()}
    prices.setdefault("mock", (0.0, 0.0))
    return {"prices": prices,
            "multipliers": raw.get("multipliers") or {},
            "rmb_per_request": raw.get("rmb_per_request", 0.3),
            "bill_url": raw.get("bill_url", ""),
            "note": raw.get("note", empty["note"])}


_P = _load_pricing()
PRICE_PER_M = _P["prices"]          # (输入价, 输出价)，单位 元 / 1M token
MULT = _P["multipliers"]            # 结算倍率
RMB_PER_REQUEST = _P["rmb_per_request"]
BILL_URL = _P["bill_url"]
COST_NOTE = _P["note"]
PRICING_LOADED = len(PRICE_PER_M) > 1



def _norm(model):
    return (model or "").strip().lower().replace(" ", "-")


def _lookup(model):
    """按模型名查价：先精确匹配，再前缀匹配（网关常带版本后缀，如
    deepseek-v4-flash-ga-260731）。返回 (matched_key, price) 或 (None, None)。"""
    key = _norm(model)
    if key in PRICE_PER_M:
        return key, PRICE_PER_M[key]
    cands = [k for k in PRICE_PER_M if k != "mock" and key.startswith(k)]
    if cands:
        best = max(cands, key=len)      # 取最长前缀，避免误配到更短的键
        return best, PRICE_PER_M[best]
    return None, None

@dataclass
class CostTracker:
    rows: list = field(default_factory=list)

    def add(self, node, res):
        matched, price = _lookup(res.model)
        if price is None:
            # 未计价：记下但不计入总额，避免静默按 0 误导
            self.rows.append({"node": node, "model": res.model, "priced": False,
                              "prompt_tokens": res.prompt_tokens,
                              "completion_tokens": res.completion_tokens,
                              "cost_rmb": None})
            return None
        pin, pout = price
        mult = MULT.get(matched, 1.0)
        cost = (res.prompt_tokens / 1e6 * pin + res.completion_tokens / 1e6 * pout) * mult
        self.rows.append({"node": node, "model": res.model, "priced": True,
                          "prompt_tokens": res.prompt_tokens,
                          "completion_tokens": res.completion_tokens,
                          "cost_rmb": round(cost, 6),
                          "requests": round(cost / RMB_PER_REQUEST, 4)})
        return cost

    def total(self):
        return round(sum(r["cost_rmb"] for r in self.rows if r.get("priced")), 6)

    def unpriced_models(self):
        return sorted({r["model"] for r in self.rows if not r.get("priced")})

    def dump(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        total = self.total()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"rows": self.rows, "total_rmb": total,
                       "total_requests": round(total / RMB_PER_REQUEST, 4),
                       "unpriced_models": self.unpriced_models(),
                       "note": COST_NOTE, "bill_url": BILL_URL},
                      f, ensure_ascii=False, indent=2)
