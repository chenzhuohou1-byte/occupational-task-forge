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
  - run_command（10-28 加）：约一半 ALE 判分器是「把 verifier 脚本写进 VM 再 `python verify.py …`」，
    或在判分时重跑考生交的 predict.sh / CLI。LocalSession 在本机 bash 里跑这些命令：裸 python/python3
    指向 ALE_EVAL_CMD_PYTHON（默认 ALE_PY），`uv run …` 去掉选项直接执行；macOS 上套 sandbox-exec
    （无网、只能写判分临时区与本次 output/）。需要 R/Stata/GPU/联网装包的判分器照样跑不了，冒烟会暴露。

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
# build 时 ALE 仓的 HEAD。shim 判分时现场 import ALE_REPO 里的 main.py，不是自带一份代码：
# ALE 仓一换版本，旧 shim 就悄悄改用新判分器（10-29 实证：切 v1.1 后 v1 shim 判出 v1.1 的分）。
ALE_COMMIT = %(ale_commit)r


def _commit_guard():
    """ALE 仓 HEAD 与 build 时不一致 → 判分器故障（退出码 2），不出分。ALE_ALLOW_COMMIT_DRIFT=1 放行。"""
    if not ALE_COMMIT or os.environ.get("ALE_ALLOW_COMMIT_DRIFT") == "1":
        return
    r = __import__("subprocess").run(["git", "-C", ALE_REPO, "rev-parse", "HEAD"],
                                     capture_output=True, text=True)
    head = r.stdout.strip()
    if head != ALE_COMMIT:
        sys.stderr.write("shim 是按 ALE %%s build 的，ALE 仓现在是 %%s：判分代码已变，拒绝出分。"
                         "重新 build shim，或确认无影响后设 ALE_ALLOW_COMMIT_DRIFT=1\n"
                         %% (ALE_COMMIT[:12], head[:12] or "（读不到）"))
        sys.exit(2)


def cmd_start(work):
    dst = os.path.join(work, "input")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    # 有的题 input/ 里带 VM 上建的 venv，链接指向 /home/user/... 的绝对路径，本机是悬空链接，跳过
    # （不用 ignore_dangling_symlinks：py3.9 按 cwd 解析相对链接，误判）
    dangling = lambda d, names: [n for n in names if os.path.islink(os.path.join(d, n))   # noqa: E731
                                 and not os.path.exists(os.path.join(d, n))]
    shutil.copytree(os.path.join(DATA_DIR, "input"), dst, symlinks=False, ignore=dangling)
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

    # —— 以下几样是「判分时在 VM 里落盘 verifier 脚本再用 python 跑」那一类 ALE 判分器要用的 ——
    def __init__(self, root, output):
        self.root, self.output = root, os.path.realpath(output)
        self.interface = types.SimpleNamespace(create_dir=self._create_dir, delete_dir=self._delete_dir)
        self._bin = tempfile.mkdtemp(prefix="ale_cmdbin_")
        for name in ("python", "python3"):          # verifier 命令写的是裸 python / python3
            os.symlink(CMD_PY, os.path.join(self._bin, name))
        with open(os.path.join(self._bin, "uv"), "w") as f:   # 少数判分器写 `uv run … python verify.py`
            f.write(UV_SHIM)
        os.chmod(os.path.join(self._bin, "uv"), 0o755)

    def _writable(self, p):
        rp = os.path.realpath(p)
        return any(rp == w or rp.startswith(w + os.sep) for w in _write_roots(self.root, self.output))

    async def _create_dir(self, p, *a, **k):
        if not self._writable(p):
            raise RuntimeError("shim 拒绝在判分临时区外建目录：%%s" %% p)
        os.makedirs(p, exist_ok=True)

    async def _delete_dir(self, p, *a, **k):
        if not self._writable(p):
            raise RuntimeError("shim 拒绝删判分临时区外的目录：%%s" %% p)
        shutil.rmtree(p, ignore_errors=True)

    async def write_file(self, p, content, *a, **k):
        await self._create_dir(os.path.dirname(p) or ".")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

    async def write_bytes(self, p, data, *a, **k):
        await self._create_dir(os.path.dirname(p) or ".")
        with open(p, "wb") as f:
            f.write(data)

    async def run_command(self, command, check=False, timeout=None, **k):
        """本地 bash 跑 ALE 判分命令。macOS 上套 sandbox-exec：无网；只能写判分临时区、REMOTE_ROOT_DIR
        与本次交付目录（有的 verifier 会重跑考生 CLI 往 output/ 写）。读不限（verifier 要读 reference）。"""
        env = dict(os.environ, PATH=os.pathsep.join([self._bin, "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
                   PYTHONDONTWRITEBYTECODE="1")
        cmd = ["bash", "-c", command]
        if shutil.which("sandbox-exec"):
            q = lambda s: '"%%s"' %% s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
            prof = "\n".join(["(version 1)", "(allow default)", "(deny network*)", "(deny file-write*)",
                              "(allow file-write* %%s (literal \"/dev/null\") (regex #\"^/dev/tty\"))"
                              %% " ".join("(subpath %%s)" %% q(w) for w in _write_roots(self.root, self.output))])
            cmd = ["sandbox-exec", "-p", prof] + cmd
        try:
            r = __import__("subprocess").run(cmd, cwd=self.root, env=env, capture_output=True, text=True,
                                             errors="replace", timeout=timeout or CMD_TIMEOUT)
            res = {"stdout": r.stdout, "stderr": r.stderr, "return_code": r.returncode,
                   "success": r.returncode == 0}
        except __import__("subprocess").TimeoutExpired as e:
            res = {"stdout": "", "stderr": "shim: 判分命令超时 %%ss" %% e.timeout, "return_code": 124,
                   "success": False}
        if check and res["return_code"] != 0:
            raise RuntimeError("判分命令失败 rc=%%s：%%s" %% (res["return_code"], res["stderr"][-1000:]))
        return res


CMD_PY = os.environ.get("ALE_EVAL_CMD_PYTHON", ALE_PY)   # verifier 命令里裸 python 指向的解释器
CMD_TIMEOUT = int(os.environ.get("ALE_EVAL_CMD_TIMEOUT", "1800"))
# uv 替身：只认 `uv run [选项] <命令…>`，丢掉选项后直接执行命令（命令里的 python 指向 CMD_PY）。
# 不联网装依赖；判分器真要 uv 拉包的题（如 simglucose）照样失败，冒烟能看出来。
UV_SHIM = """#!/bin/bash
[ "$1" = run ] || { echo "shim uv: only 'uv run' is supported" >&2; exit 2; }
shift
while [ $# -gt 0 ]; do case "$1" in
  --) shift; break;;
  --isolated|--no-project|--frozen|--no-sync|--locked|--offline|-q|--quiet) shift;;
  --python|--project|--with|--directory|-p) shift 2;;
  --*=*) shift;;
  *) break;;
esac; done
exec "$@"
"""


def _write_roots(root, output):
    tmp = os.path.realpath(tempfile.gettempdir())
    return sorted({os.path.realpath(root), output, os.path.realpath("/tmp/agenthle_eval"),
                   os.path.realpath("/dev/shm") if os.path.isdir("/dev/shm") else tmp, tmp})


def cmd_evaluate(output, reference):
    if os.path.realpath(sys.executable) != os.path.realpath(ALE_PY) and os.path.exists(ALE_PY):
        r = __import__("subprocess").run([ALE_PY, __file__, "evaluate", "--output", output,
                                          "--reference", reference], capture_output=True, text=True)
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
        return r.returncode
    _commit_guard()
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
    res = asyncio.run(mod.evaluate(cfg, LocalSession(root, output)))
    # ALE 判分器把逐项报告写进日志；留下来供复盘（不含 reference 原文）。留头尾各 3000 字：
    # 汇总字段（各项准确率、pass_fail）常在头部，只留尾部会被逐条明细挤掉（basel 实例）
    report = buf.getvalue()
    if len(report) > 6000:
        report = report[:3000] + "\n…（中段截去）…\n" + report[-3000:]
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


def _version_guard(ale_repo, ale_data, allow_mismatch=False):
    """ALE 代码与数据档案必须同一发布版本，返回写进 task_card 的版本串。

    为什么要有：数据档案（release-v*.json）和 ALE 仓各自更新。10-29 实证：v1.1 数据（题面写明
    「大小写/标点不影响匹配」）配 v1 代码（判分器逐字比对）→ crf_sdtm_mapping_1 前沿档 3 跑交付
    全对、被判 0。核对方式＝ALE 仓 releases/v<ver>/assets.json 里要出现数据档案的 data_manifest_sha256。
    """
    try:
        head = subprocess.run(["git", "-C", ale_repo, "rev-parse", "--short=12", "HEAD"],
                              capture_output=True, text=True).stdout.strip() or "unknown"
    except OSError:
        head = "unknown"
    rels = [os.path.join(d, n) for d in (os.path.dirname(ale_data), ale_data) if os.path.isdir(d)
            for n in sorted(os.listdir(d)) if re.fullmatch(r"release-v[\d.]+\.json", n)]
    if not rels:
        print("警告：没找到数据档案的 release-v*.json，ALE 代码/数据版本未核对", file=sys.stderr)
        return "ale@%s（版本未核对）" % head
    rel = json.load(open(rels[-1]))
    ver, want = rel.get("version"), rel.get("data_manifest_sha256") or ""
    assets = os.path.join(ale_repo, "releases", "v%s" % ver, "assets.json")
    if want and os.path.isfile(assets) and want in open(assets).read():
        return "ale-v%s@%s" % (ver, head)
    msg = ("ALE 仓 %s（HEAD %s）与数据档案 %s（v%s，manifest %s…）不是同一版本：%s 里没有该 manifest。"
           "先把 ALE 仓切到对应发布版本再 build" % (ale_repo, head, rels[-1], ver, want[:12], assets))
    if not allow_mismatch:
        raise SystemExit(msg + "；确需沿用旧代码加 --allow-version-mismatch")
    print("警告：" + msg, file=sys.stderr)
    return "ale@%s（与数据 v%s 不匹配）" % (head, ver)


def _retired(ale_repo):
    """ALE 仓 releases/v*/assets.json 里的下线题（retired_tasks）。下线题在新版仓里没有 main.py，
    build 会报一长串 FileNotFoundError，先按发布清单给出明确原因。"""
    out = {}
    rd = os.path.join(ale_repo, "releases")
    for v in sorted(os.listdir(rd)) if os.path.isdir(rd) else []:
        p = os.path.join(rd, v, "assets.json")
        if os.path.isfile(p):
            try:
                out.update((json.load(open(p)).get("scope") or {}).get("retired_tasks") or {})
            except ValueError:
                pass
    return out


def build(ale_repo, ale_data, out_dir, ids, allow_mismatch=False):
    scorer_version = _version_guard(ale_repo, ale_data, allow_mismatch)
    ale_commit = subprocess.run(["git", "-C", ale_repo, "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
    retired = _retired(ale_repo)
    made = []
    for ident in ids:
        domain, task = ident.strip("/").split("/", 1)
        if ident.strip("/") in retired and not os.path.isfile(os.path.join(ale_repo, "tasks", domain, task, "main.py")):
            print("跳过 %s：该发布版本已下线（%s）" % (ident, retired[ident.strip("/")]), file=sys.stderr)
            continue
        try:
            m = _meta(ale_repo, domain, task)
        except RuntimeError as e:   # 一条读不了 config（如判分器依赖 openai）不该打断整批
            print("跳过 %s：%s" % (ident, str(e)[-300:]), file=sys.stderr)
            continue
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
            "evaluation": {"scorer": "ALE evaluate()（原样）", "scorerVersion": scorer_version},
            "元数据": {"来源": "ALE v1.1 公开任务（仅用于难度校准，不得训练/分发）",
                     "ALE层级": tiers, "ALE原卡software": card.get("software")},
        }
        json.dump(shim_card, open(os.path.join(sdir, "task_card.json"), "w"), ensure_ascii=False, indent=2)
        with open(os.path.join(sdir, "main.py"), "w") as f:
            f.write(SHIM_MAIN % {"ale_repo": ale_repo, "data_dir": data_dir,
                                 "domain": domain, "task": task, "variant": m["variant"],
                                 "ale_py": ALE_PY, "ale_commit": ale_commit})
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
    b.add_argument("--allow-version-mismatch", action="store_true",
                   help="ALE 代码与数据档案版本不一致时仍然 build（只告警；判分口径可能与题面不符）")
    b.add_argument("ids", nargs="+", help="<domain>/<task>")
    a = ap.parse_args()
    build(os.path.abspath(os.path.expanduser(a.ale_repo)), os.path.abspath(os.path.expanduser(a.ale_data)),
          os.path.abspath(os.path.expanduser(a.out)), a.ids, a.allow_version_mismatch)


if __name__ == "__main__":
    main()
