"""qc 的两条提示：规模、软件（契约 §8.2，2026-11-02）。

**只提示、不拦进库。** 库里现有一批小附件、只声明通用库的题，直接卡死会一次踢出去一大片；
先把不达标的标出来，作者自己看得到，等大家适应了再决定要不要升成闸门。

- 规模：附件总量 ≥ 100KB（ALE 纯命令行子集 P25）、含通用文本之外的格式、有子目录
  （真题配方 v1 第 1 条，见总览 §4 S2′）。
- 软件：卡上 `software` 写的每一项，考生环境里得真有。对照表 = runtime/examinee/requirements.txt
  + runtime/software_registry.json。可执行范式的题另查 `evaluation.engine.adapter_args.image`
  是否已登记（只在作者本机的镜像，别人跑不了 qc）。
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIREMENTS = os.path.join(ROOT, "runtime", "examinee", "requirements.txt")
REGISTRY = os.path.join(ROOT, "runtime", "software_registry.json")

MIN_INPUT_KB = 100
# 通用文本格式：有了它们不算「真实工作格式」。.vcf/.gb/.pdb/.tf/.pcap 这类领域格式即使是文本也算
TEXT_EXT = {".csv", ".tsv", ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".html", ""}

_PY_STDLIB = re.compile(r"^python(\s*\d[\d.]*)?$|^python\s*标准库|^python3?$")
_CUT = re.compile(r"[\s（(=,，:：]")


def _norm(name):
    return name.strip().lower().replace("_", "-")


def scale_hint(task):
    d = os.path.join(task.dir, "assets", "input")
    files = []
    for root, _, fs in os.walk(d):
        for f in fs:
            p = os.path.join(root, f)
            files.append((os.path.relpath(p, d), os.path.getsize(p)))
    kb = round(sum(s for _, s in files) / 1024, 1)
    exts = sorted({os.path.splitext(f)[1].lower() for f, _ in files} - TEXT_EXT)
    subdirs = any(os.path.dirname(f) for f, _ in files)
    lack = []
    if kb < MIN_INPUT_KB:
        lack.append("附件总量 %.1fKB < %dKB" % (kb, MIN_INPUT_KB))
    if not exts:
        lack.append("只有通用文本格式（csv/md/json/txt 等）")
    if not subdirs:
        lack.append("没有子目录")
    return {"达标": not lack, "缺": lack, "附件KB": kb, "文件数": len(files),
            "非通用格式": exts, "有子目录": subdirs}


def _available():
    pkgs = set()
    if os.path.exists(REQUIREMENTS):
        for ln in open(REQUIREMENTS, encoding="utf-8"):
            ln = ln.split("#", 1)[0].strip()
            if ln:
                pkgs.add(_norm(re.split(r"[=<>!~;\[ ]", ln, 1)[0]))
    reg = {}
    if os.path.exists(REGISTRY):
        with open(REGISTRY, encoding="utf-8") as f:
            reg = json.load(f)
    return pkgs, reg


def _declared(card):
    out = []
    for s in card.get("software") or []:
        name = s.get("name") if isinstance(s, dict) else s
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def software_hint(task, _cache={}):
    if "v" not in _cache:
        _cache["v"] = _available()
    pkgs, reg = _cache["v"]
    aliases = {_norm(k): _norm(v) for k, v in (reg.get("aliases") or {}).items()}
    host = {_norm(k) for k in (reg.get("hostApps") or {})}
    images = reg.get("images") or {}
    image = (((task.card.get("evaluation") or {}).get("engine") or {})
             .get("adapter_args") or {}).get("image")
    extra = {_norm(x) for x in (reg.get("imageSoftware") or {}).get(image or "", [])}

    missing, ok = [], []
    for raw in _declared(task.card):
        low = raw.strip().lower()
        if _PY_STDLIB.match(low):
            ok.append(raw)
            continue
        key = _norm(_CUT.split(low, 1)[0])
        key = aliases.get(key, key)
        (ok if key in pkgs or key in host or key in extra else missing).append(raw)
    out = {"达标": not missing, "未装": missing, "已装": ok}
    if image:
        out["镜像"] = image
        if image not in images:
            out["达标"] = False
            out["镜像未登记"] = True
    return out


def hints(task):
    return {"规模": scale_hint(task), "软件": software_hint(task)}
