"""ALE 原题桥接：把 ALE 公开任务包装成我们的任务契约，用自己的 N7 去量它 —— 难度锚点（S1）。

为什么要有：我们的 L1–L4 全是用自己的模型、自己的考法、在自己的题上定的。只有拿 ALE 原题
过同一把尺子，看「我们量出的满分率」和「ALE 论文报的通过率」对不对得上，分级才有外部刻度。

怎么桥：每条 ALE 任务生成一个 shim 目录（task_card.json + main.py，符合我们的五子命令契约）：
  - start：把 ALE 数据档案里的 `<variant>/input` 拷进沙箱 input/；
  - evaluate：在子进程里 import ALE 自己的 main.py，喂一个读本地文件的假 DesktopSession，
    原样跑它的 `evaluate()`，返回 [score] → 我们的 {score, passed}。passed = 满分（ALE 论文的
    「full pass」就是满分，见 2606.05405 §实验）。**判分逻辑一行不改，用的是 ALE 自己的判分器。**
  - prompt：ALE 的 task_description 把绝对路径换成相对 input/ output/，并说明 wrapper 不可用、
    改用 run_python。

**合规边界（数据集许可，必须守）**：reference 不得用于训练、不得在评测时暴露给模型、不得再分发。
所以 shim 里不拷 reference、只在 evaluate 时按路径读；shim 与 ALE 数据都放在 `~/ale-data`
（不在任何 git 仓里）；本文件只含桥接代码、不含任何 ALE 数据。

用法：
    python3 -m pipeline.ale_bridge build --ale-repo ~/agents-last-exam --ale-data ~/ale-data/tasks \
        --out ~/ale-data/bridge business_finance/american_option_pricing_ls ...
    然后 ALE_TASKS_DIR=~/ale-data/bridge N7_TASK=ale-<domain>-<task> ... python3 n7_run.py
"""
import argparse
import json
import os
import re
import subprocess
import sys

# ALE 判分器用了 3.10+ 语法（`str | None`），本机默认 python3 是 3.9。判分与读 config 走这个解释器；
# 考生的 run_python 仍用产线默认解释器（与我们自己的测量口径一致）。
ALE_PY = os.environ.get("ALE_EVAL_PYTHON", os.path.expanduser("~/ale-data/.venv312/bin/python"))

SHIM_MAIN = r'''#!/usr/bin/env python3
"""ALE 原题 shim（由 pipeline/ale_bridge.py 生成，勿手改）。判分用 ALE 自己的 evaluate()。"""
import argparse, asyncio, json, os, shutil, sys, tempfile, types

ALE_REPO = %(ale_repo)r
DATA_DIR = %(data_dir)r            # <ale-data>/<domain>/<task>/<variant>
DOMAIN, TASK, VARIANT = %(domain)r, %(task)r, %(variant)r
ALE_PY = %(ale_py)r


def cmd_start(work):
    dst = os.path.join(work, "input")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(os.path.join(DATA_DIR, "input"), dst, symlinks=False)
    os.makedirs(os.path.join(work, "output"), exist_ok=True)
    assert not os.path.exists(os.path.join(work, "reference")), "答案泄漏：沙箱不应有 reference/"


def _stub_cua_bench():
    cb = types.ModuleType("cua_bench")
    ident = lambda *a, **k: (lambda f: f)                     # noqa: E731
    cb.tasks_config = cb.setup_task = cb.evaluate_task = ident
    cb.Task = lambda **k: types.SimpleNamespace(**k)
    cb.DesktopSession = object
    sys.modules["cua_bench"] = cb


class LocalSession:
    """ALE 的 DesktopSession 只用到读文件这几样；全部映射到本地文件系统。"""

    async def file_exists(self, p):
        return os.path.isfile(p)

    async def directory_exists(self, p):
        return os.path.isdir(p)

    async def read_bytes(self, p):
        with open(p, "rb") as f:
            return f.read()

    async def read_file(self, p, *a, **k):
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()

    async def list_dir(self, p):
        return sorted(os.listdir(p))

    async def run_command(self, *a, **k):
        raise RuntimeError("shim 不支持 run_command（该 ALE 任务要在 VM 里跑判分脚本，不在校准集内）")


def cmd_evaluate(output, reference):
    if os.path.realpath(sys.executable) != os.path.realpath(ALE_PY) and os.path.exists(ALE_PY):
        r = __import__("subprocess").run([ALE_PY, __file__, "evaluate", "--output", output,
                                          "--reference", reference], capture_output=True, text=True)
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
        return r.returncode
    root = tempfile.mkdtemp(prefix="ale_eval_")
    td = os.path.join(root, DOMAIN, TASK, VARIANT)
    os.makedirs(td)
    os.symlink(os.path.abspath(output), os.path.join(td, "output"))
    os.symlink(os.path.join(DATA_DIR, "reference"), os.path.join(td, "reference"))
    os.symlink(os.path.join(DATA_DIR, "input"), os.path.join(td, "input"))
    if os.path.isdir(os.path.join(DATA_DIR, "software")):
        os.symlink(os.path.join(DATA_DIR, "software"), os.path.join(td, "software"))
    os.environ["REMOTE_ROOT_DIR"] = root
    _stub_cua_bench()
    sys.path.insert(0, ALE_REPO)
    import importlib.util
    mp = os.path.join(ALE_REPO, "tasks", DOMAIN, TASK, "main.py")
    spec = importlib.util.spec_from_file_location("tasks.%%s.%%s.main" %% (DOMAIN, TASK), mp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    tasks = mod.load()
    pick = [t for t in tasks if (t.metadata or {}).get("task_dir", "").rstrip("/").endswith("/" + VARIANT)]
    cfg = types.SimpleNamespace(metadata=(pick or tasks)[0].metadata)
    import io, logging
    buf = io.StringIO(); h = logging.StreamHandler(buf); logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)
    res = asyncio.run(mod.evaluate(cfg, LocalSession()))
    report = buf.getvalue()[-6000:]   # ALE 判分器把逐项报告写进日志；留下来供复盘（不含 reference 原文）
    score = float(res[0] if isinstance(res, (list, tuple)) else res)
    print(json.dumps({"score": score, "passed": score >= 1.0 - 1e-9, "errors": [],
                      "details": [], "_ale": {"raw": res, "report_log": report}}, default=str))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start"); p.add_argument("--work", required=True)
    p = sub.add_parser("evaluate"); p.add_argument("--output", required=True)
    p.add_argument("--reference", required=True); p.add_argument("--variant", default=None)
    sub.add_parser("load")
    a = ap.parse_args()
    if a.cmd == "start":
        cmd_start(a.work)
    elif a.cmd == "evaluate":
        sys.exit(cmd_evaluate(a.output, a.reference))
    else:
        sys.stdout.write(open(os.path.join(os.path.dirname(__file__), "task_card.json")).read())


if __name__ == "__main__":
    main()
'''

NOTE = """

---
本环境说明（ALE 原题在我们 harness 下运行，路径已改为相对工作目录）：
- 输入在 input/，交付物写到 output/；没有 shell，software/ 下的 wrapper 不可用。
- 用 run_python 动作执行 Python 3（可用 numpy / scipy / pandas / openpyxl / Pillow；
  scikit-learn / statsmodels / matplotlib 不可用，需要时用 numpy 自己实现）。
- 若题面要求的 Python 包本机没有，用等价的标准库 / numpy 实现。
"""


def _meta(ale_repo, domain, task, root="/__ALE__"):
    """在子进程里拿 ALE config 的 task_description 与 output_files（不同任务字段不一）。"""
    code = r'''
import json, os, sys, types, importlib.util
os.environ["REMOTE_ROOT_DIR"] = %r
cb = types.ModuleType("cua_bench"); ident = lambda *a, **k: (lambda f: f)
cb.tasks_config = cb.setup_task = cb.evaluate_task = ident
cb.Task = lambda **k: types.SimpleNamespace(**k); cb.DesktopSession = object
sys.modules["cua_bench"] = cb; sys.path.insert(0, %r)
mp = os.path.join(%r, "tasks", %r, %r, "main.py")
spec = importlib.util.spec_from_file_location("tasks.%s.%s.main", mp)
mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
tasks = mod.load(); t0 = tasks[0]; md = dict(getattr(t0, "metadata", {}) or {})
td = md.get("task_dir") or md.get("remote_task_dir") or ""
print(json.dumps({"desc": getattr(t0, "description", "") or "", "task_dir": td,
                  "variant": os.path.basename(td.rstrip("/")) if td else "base",
                  "n_variants": len(tasks), "outputs": md.get("output_files") or {}}, default=str))
''' % (root, ale_repo, ale_repo, domain, task, domain, task)
    r = subprocess.run([ALE_PY, "-c", code], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("读 ALE config 失败 %s/%s：%s" % (domain, task, r.stderr[-600:]))
    return json.loads(r.stdout.strip().splitlines()[-1])


def _relativize(text, task_dir):
    text = text.replace(task_dir + "/", "").replace(task_dir, "工作目录（.）")
    return re.sub(r"`?\"?software/[\w./-]+\"?`?", "（software wrapper：本环境不可用，改用 run_python）", text)


def build(ale_repo, ale_data, out_dir, ids):
    made = []
    for ident in ids:
        domain, task = ident.strip("/").split("/", 1)
        m = _meta(ale_repo, domain, task)
        data_dir = os.path.join(ale_data, domain, task, m["variant"])
        if not os.path.isdir(os.path.join(data_dir, "input")):
            print("跳过 %s：数据档案里没有 %s/input" % (ident, data_dir), file=sys.stderr)
            continue
        card = json.load(open(os.path.join(ale_repo, "tasks", domain, task, "task_card.json")))
        outs = {}
        for name, path in (m["outputs"] or {}).items():
            rel = path.replace(m["task_dir"] + "/", "")
            outs[rel] = "ALE 原题交付物（判分用 ALE 自己的 evaluate）"
        tiers = [t for t in ("near-term", "full-spectrum", "last-exam")
                 if ident in {l.strip() for l in open(os.path.join(
                     ale_repo, "selected_tasks", "full", t + ".txt")) if not l.startswith("#")}]
        tid = "ale-%s-%s" % (domain, task)
        sdir = os.path.join(out_dir, domain, task)
        os.makedirs(sdir, exist_ok=True)
        shim_card = {
            "task_id": tid, "title": card.get("title", task),
            "prompt": _relativize(m["desc"] or card.get("taskPrompt", ""), m["task_dir"]) + NOTE,
            "交付要求": outs or {"output/": "见题面 Required outputs"},
            "evaluation": {"scorer": "ALE evaluate()（原样）", "scorerVersion": "ale-v1.1"},
            "元数据": {"来源": "ALE v1.1 公开任务（仅用于难度校准，不得训练/分发）",
                     "ALE层级": tiers, "ALE原卡software": card.get("software")},
        }
        json.dump(shim_card, open(os.path.join(sdir, "task_card.json"), "w"), ensure_ascii=False, indent=2)
        with open(os.path.join(sdir, "main.py"), "w") as f:
            f.write(SHIM_MAIN % {"ale_repo": ale_repo, "data_dir": data_dir,
                                 "domain": domain, "task": task, "variant": m["variant"],
                                 "ale_py": ALE_PY})
        made.append((tid, tiers, len(outs)))
        print("✓ %s  层级=%s  交付=%d" % (tid, tiers, len(outs)))
    return made


def main():
    ap = argparse.ArgumentParser(description="把 ALE 公开任务包装成我们的任务契约")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--ale-repo", default=os.path.expanduser("~/agents-last-exam"))
    b.add_argument("--ale-data", default=os.path.expanduser("~/ale-data/tasks"))
    b.add_argument("--out", default=os.path.expanduser("~/ale-data/bridge"))
    b.add_argument("ids", nargs="+", help="<domain>/<task>")
    a = ap.parse_args()
    build(os.path.abspath(os.path.expanduser(a.ale_repo)), os.path.abspath(os.path.expanduser(a.ale_data)),
          os.path.abspath(os.path.expanduser(a.out)), a.ids)


if __name__ == "__main__":
    main()
