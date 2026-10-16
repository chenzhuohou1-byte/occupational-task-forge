"""ALE 领域/子域码表：为选题提供锚点，并让任务卡的 taxonomy 自动带上编码。

替代了原来的 `occupations.json`（中国职业码表，18 领域/265 职业）——那套轴与 ALE 的
两级 domain→subdomain 正交，每张卡都要再做一次映射，已整体废弃。

`subdomains.json` 两部分：
- domains：14 个一级领域（domain_id → domain_code）
- subdomains：**57 个槽位** = 上游实测 51 个 + 6 个编号空洞占位

为什么留空洞与做不了的槽位：对照表要完整才看得出缺口（缺口分「能力缺口」与「素材缺口」，
解法和催办对象不同）；且环境限制是暂时的，机器到了直接能填，结构里没位就得重构。

锚点口径：选题 prompt 不再喂「职业」，而是喂「子域 + 该子域下 ALE 的真实任务 title/summary」。
这比职业名更强——职业名只给行业暗示，ALE 样例给的是真实题型与材料构成，且天然对齐目标分布。

重建：python3 -m pipeline.subdomains --rebuild <agents-last-exam-metadata 目录>
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(HERE, "subdomains.json")

# 暂时做不了的子域。留槽位、标原因，不参与派活。
# 口径依据 `ALE领域与公开源_建题可行性.md`（本机实测公开源 + 软件开源性）——注意它与
# 「方向调整文档第六节」说的不是一回事：那边说的是**跑 ALE 自己那些任务**要 x86 Linux 虚拟机，
# 而我们是**自己造题**，Blender/Inkscape/KiCad/Qiskit 在 arm64 上原生可跑，故不属做不了。
BLOCKED = {
    "8.4": ("授权软件", "ALE 原题用 Cubase/Dorico（需授权）；音频文件可自造，待评估后再开"),
}

# 可派活但取数要绕的子域：源被挡或软件较重，派活前先看这条。
SOURCE_NOTES = {
    "6.1": "SEC（sec.gov）本机被挡，换 data.gov / Kaggle 取数",
    "6.4": "SEC 被挡，同 6.1",
    "9.6": "Docker/k8s/Terraform 均开源，但嵌套 Docker 需本地 Docker 环境",
    "1.11": "MuJoCo 开源可用；Isaac 较重",
    "2.1": "多数 pip 可装；DFT 类计算重",
    "2.2": "RDKit 可 pip；QE/BerkeleyGW 重，必要时降级",
    "2.3": "Astropy 可 pip；MAST/Vizier 可达性待再探",
    "4.1": "大医学影像数据集多为门控（TCIA/NIH 部分），先用可达小样本",
    "4.2": "同 4.1，门控数据",
    "4.5": "同 4.1，门控数据",
    "1.1": "PLAXIS/Civil3D/PowerMill/Moldex3D 需授权；可换 FreeCAD/ezdxf/EnergyPlus 等开源替代",
    "1.3": "同 1.1，授权软件为主；地理部分可用 Natural Earth/USGS",
    "1.5": "同 1.1，授权软件为主",
    "1.6": "EnergyPlus/FDS 开源可用；部分工具需授权",
    "8.1": "Blender 开源、arm64 原生可跑；ALE 原题里的 After Effects 部分需授权，避开",
    "3.1": "RDKit/Biopython 可 pip；Amber+CUDA+SLURM 类需 GPU，避开",
}

# 上游 subdomain_id 的编号空洞。名称只存在于未公开的 1490 条任务里，无从还原。
# 官方宣称 55 子域、实测 51、留位 57——这三个数的差异显式保留在码表里，不要抹平。
GAPS = {"1.4": "1", "1.7": "1", "1.9": "1", "8.3": "8", "10.1": "10", "10.2": "10"}


def load():
    with open(_PATH, encoding="utf-8") as f:
        return json.load(f)


def domains():
    """返回 [(domain_id, domain_code, 子域数, ALE 任务数), ...]，按 domain_id 数值序。"""
    d = load()
    subs = d["subdomains"]
    out = []
    for did, info in sorted(d["domains"].items(), key=lambda kv: int(kv[0])):
        mine = [s for s in subs.values() if s["domain_id"] == did]
        out.append((did, info["code"], len(mine), info["ale_n"]))
    return out


def _sid_key(sid):
    a, _, b = sid.partition(".")
    return (int(a), int(b or 0))


def subdomains(domain=None, enabled_only=False):
    """列出子域 id。domain 为 domain_id 或 domain_code；enabled_only 跳过做不了的。"""
    d = load()
    out = []
    for sid, s in d["subdomains"].items():
        if enabled_only and not s["enabled"]:
            continue
        if domain and domain != s["domain_id"] and domain != _domain_code(d, s["domain_id"]):
            continue
        out.append(sid)
    return sorted(out, key=_sid_key)


def _domain_code(d, did):
    return (d["domains"].get(did) or {}).get("code", "")


def get(sid):
    """取一个子域的完整记录；id 不存在直接抛 KeyError（选题写错不该静默放过）。"""
    d = load()
    s = d["subdomains"].get(str(sid))
    if s is None:
        raise KeyError("没有子域 %r。可用：%s" % (sid, "、".join(subdomains(enabled_only=True))))
    return dict(s, id=str(sid), domain_code=_domain_code(d, s["domain_id"]))


def taxonomy(sid):
    """任务卡 taxonomy 字段的值（判分契约 §9.1 的领域轴编码）。"""
    s = get(sid)
    return {"domain_id": s["domain_id"], "domain_code": s["domain_code"],
            "subdomain_id": s["id"], "subdomain_code": s["code"],
            "subdomain_name": s["name"]}


def anchor(sid, max_examples=6):
    """选题锚点文本：子域名 + 该子域下 ALE 的真实任务（同类不同题，供模型对标题型）。"""
    s = get(sid)
    lines = ["子域「%s %s」（ALE 一级领域 %s %s，该子域下 ALE 有 %d 条任务）"
             % (s["id"], s["name"] or "（ALE 未公开）", s["domain_id"], s["domain_code"],
                s["ale_n"])]
    ex = s.get("ale_examples") or []
    if ex:
        lines.append("该子域下 ALE 的真实任务（**同类不同题，不要照抄**，只用来对标题型与材料构成）：")
        for e in ex[:max_examples]:
            lines.append("- %s：%s" % (e["title"], e["summary"]))
    return "\n".join(lines)


def _rebuild(meta_dir, out_path=_PATH):
    """从 agents-last-exam-metadata/task_cards.parquet 重建码表。上游更新后重跑即可。"""
    import collections

    import pandas as pd
    df = pd.read_parquet(os.path.join(meta_dir, "task_cards.parquet"))

    def _tx(v):
        if isinstance(v, dict):
            return v
        try:
            return json.loads(v) if isinstance(v, str) else {}
        except ValueError:
            return {}

    rows = []
    for _, r in df.iterrows():
        tx = _tx(r["taxonomy"])
        sid = str(tx.get("subdomain_id") or "")
        if not sid:
            continue
        rows.append({
            "sid": sid, "did": str(tx.get("domain_id") or sid.split(".")[0]),
            "dcode": str(tx.get("domain_code") or ""),
            "scode": str(tx.get("subdomain_code") or tx.get("subdomain") or ""),
            "sname": str(tx.get("subdomain_name") or r["subdomain"] or ""),
            "title": str(r["title"] or ""), "summary": str(r["summary"] or "")[:220],
        })

    dom_n = collections.Counter(x["did"] for x in rows)
    doms = {}
    for x in rows:
        doms.setdefault(x["did"], {"code": x["dcode"], "ale_n": dom_n[x["did"]]})

    subs = {}
    for x in rows:
        s = subs.setdefault(x["sid"], {
            "domain_id": x["did"], "code": x["scode"], "name": x["sname"],
            "ale_n": 0, "enabled": True, "blocked_by": None, "blocked_note": None,
            "source_note": None, "ale_examples": []})
        s["ale_n"] += 1
        s["ale_examples"].append({"title": x["title"], "summary": x["summary"]})

    for sid, (why, note) in BLOCKED.items():      # 做不了的：留槽位 + 留样例，机器到了直接填
        if sid in subs:
            subs[sid].update(enabled=False, blocked_by=why, blocked_note=note)
    for sid, note in SOURCE_NOTES.items():        # 可派活但取数要绕
        if sid in subs:
            subs[sid]["source_note"] = note
    for sid, did in GAPS.items():                 # 编号空洞：名称无从还原，占位
        subs.setdefault(sid, {
            "domain_id": did, "code": None, "name": None, "ale_n": 0, "enabled": False,
            "blocked_by": "ALE未公开",
            "blocked_note": "上游 subdomain_id 编号空洞，名称只存在于未公开的 1490 条任务里",
            "source_note": None, "ale_examples": []})

    data = {"_provenance": {
        "来源": "agents-last-exam-metadata/task_cards.parquet（%d 行，%d 行有 subdomain_id）"
                % (len(df), len(rows)),
        "取数时间": "2026-10-14",
        "口径": "只用 taxonomy.domain_id/subdomain_id；不用 category——它与 domain_code 有 18 行"
                "以上不一致，且这批标签是 gpt-4o-mini 回填的（110/157）",
        "槽位": "实测 %d 个 + 编号空洞 %d 个 = %d。官方宣称 55、实测 %d，差异显式保留不抹平"
                % (len(subs) - len(GAPS), len(GAPS), len(subs), len(subs) - len(GAPS)),
        "重建": "python3 -m pipeline.subdomains --rebuild <metadata 目录>"},
        "domains": doms,
        "subdomains": {k: subs[k] for k in sorted(subs, key=_sid_key)}}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return {"子域槽位": len(subs), "可做": sum(1 for s in subs.values() if s["enabled"]),
            "一级领域": len(doms), "样例条数": sum(len(s["ale_examples"]) for s in subs.values())}


def _selftest():
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("✓ " if cond else "✗ ") + msg)
        ok = ok and bool(cond)

    d = load()
    subs = d["subdomains"]
    check(len(subs) == 57 and len(d["domains"]) == 14,
          "码表规模：57 个子域槽位 / 14 个一级领域（实测 %d / %d）" % (len(subs), len(d["domains"])))
    check(all(s in subs and not subs[s]["enabled"] for s in GAPS),
          "编号空洞 %s 都留了槽位且标为不可做" % "、".join(sorted(GAPS, key=_sid_key)))
    check(all(subs[s]["blocked_by"] and not subs[s]["enabled"] for s in BLOCKED),
          "做不了的子域标了 blocked_by、不参与派活")
    check(all(subs[s]["ale_examples"] for s in BLOCKED),
          "做不了的子域仍保留 ALE 样例（机器到了直接能填，对照表也看得出放弃了什么）")
    n_on = len(subdomains(enabled_only=True))
    check(n_on == 57 - len(GAPS) - len(BLOCKED),
          "可派活子域 %d 个（57 − 空洞 %d − 做不了 %d）" % (n_on, len(GAPS), len(BLOCKED)))
    check(all(subs[s]["source_note"] for s in SOURCE_NOTES if s in subs),
          "取数要绕的 %d 个子域都带 source_note（派活前先看）" % len(SOURCE_NOTES))
    check(subs["8.1"]["enabled"] and subs["9.7"]["enabled"] and subs["8.2"]["enabled"],
          "8.1/8.2/9.7 可派活：Blender/Inkscape/Qiskit 都是开源且 arm64 原生可跑")
    tx = taxonomy("6.1")
    check(tx["domain_code"] == "business_finance" and tx["subdomain_code"] == "accounting"
          and set(tx) == {"domain_id", "domain_code", "subdomain_id", "subdomain_code",
                          "subdomain_name"},
          "taxonomy('6.1') 五个字段齐全：%s" % json.dumps(tx, ensure_ascii=False))
    a = anchor("6.1")
    check("Accounting & Finance" in a and "不要照抄" in a and a.count("\n- ") >= 3,
          "选题锚点带子域名 + ≥3 条 ALE 真实任务 + 「不要照抄」约束")
    check(anchor("1.4").startswith("子域「1.4 （ALE 未公开）"),
          "空洞子域的锚点不崩（名称缺失时降级显示）")
    try:
        get("99.9")
        check(False, "不存在的子域应抛 KeyError")
    except KeyError:
        check(True, "不存在的子域抛 KeyError（选题写错不静默放过）")
    print("\n%s" % ("subdomains 自测通过" if ok else "subdomains 自测失败"))
    return ok


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="pipeline.subdomains")
    ap.add_argument("--rebuild", metavar="META_DIR", help="从 task_cards.parquet 重建码表")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.rebuild:
        print(json.dumps(_rebuild(a.rebuild), ensure_ascii=False, indent=2))
        return 0
    return 0 if _selftest() else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())



