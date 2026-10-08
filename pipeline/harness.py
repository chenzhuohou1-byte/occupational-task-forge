"""任务 harness：布置沙箱环境 → 跑候选解 → 调判分器 → 返回结果。

沙箱四目录由任务 main.py 的 start 铺设：input/ 只读、software/ 预装、output/ 唯一可写；
reference/（标准答案）仅判分时注入，不进沙箱。任务须实现 start / golden / evaluate 三个子命令。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field


@dataclass
class EvalResult:
    task_id: str
    score: float
    passed: bool
    details: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def error(self):
        """把 errors[] 拼成一行，给只想显示一句话的地方用。"""
        return "；".join(self.errors)



def _run(cmd, cwd=None, timeout=1800):
    return subprocess.run(cmd, cwd=cwd, timeout=timeout,
                          capture_output=True, text=True)


def setup_env(task, work_dir):
    """调用任务的 start，把 input/ 等铺到 work_dir。"""
    r = _run([sys.executable, "main.py", "start", "--work", work_dir], cwd=task.dir)
    if r.returncode != 0:
        raise RuntimeError("start 失败: " + (r.stderr or r.stdout))


# ---- 候选解 solver：签名 (task, work_dir)，负责产出 work_dir/output/ ----
def empty_solver(task, work_dir):
    """不做任何事——用于 N8 地板审计（看空解能拿多少分）。"""
    return


def golden_solver(task, work_dir):
    """跑任务自带 golden，写出标准正确 output/——正例（应得 1.0）。"""
    r = _run([sys.executable, "main.py", "golden",
              "--out", os.path.join(work_dir, "output")], cwd=task.dir)
    if r.returncode != 0:
        raise RuntimeError("golden 失败: " + (r.stderr or r.stdout))


def neg_solver(task, work_dir):
    """跑任务自带 neg，写出刻意做坏的 output/——负例（应得 0.0）。"""
    r = _run([sys.executable, "main.py", "neg",
              "--out", os.path.join(work_dir, "output")], cwd=task.dir)
    if r.returncode != 0:
        raise RuntimeError("neg 失败: " + (r.stderr or r.stdout))


def fixture_solver(name):
    """把 assets/reference/fixtures/<name>/ 原样拷成 output/ 的 solver 工厂。

    夹具是出题人自证义务的载体（判分契约 §4）。N8 地板审计用 output_test_random
    这一档——「文件名/表头/行键/字段全对、数值全错」，这才是有效地板，空解不是。
    """
    def solver(task, work_dir):
        src = os.path.join(task.dir, "assets", "reference", "fixtures", name)
        if not os.path.isdir(src):
            raise RuntimeError("夹具不存在: " + src)
        dst = os.path.join(work_dir, "output")
        os.makedirs(dst, exist_ok=True)
        for fn in sorted(os.listdir(src)):
            s = os.path.join(src, fn)
            if os.path.isfile(s):
                shutil.copy(s, os.path.join(dst, fn))
    solver.__name__ = "fixture_solver_" + name
    return solver



def evaluate(task, work_dir):
    """调用任务判分器，解析 stdout 的契约 JSON。

    契约（判分契约 v1.0 §2）：score / **passed(bool)** / errors[] /
    details[{path,expected,observed,correct}]。
    判分器退出码非 0 = 判分器自身故障（如标准答案缺失），不是 agent 得 0 分。
    """
    r = _run([sys.executable, "main.py", "evaluate",
              "--output", os.path.join(work_dir, "output"),
              "--reference", task.reference_dir], cwd=task.dir)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        why = ("判分器故障（退出码 %d）：" % r.returncode) if r.returncode else "判分器未输出合法 JSON："
        return EvalResult(task.task_id, 0.0, False,
                          errors=[why + (r.stderr or r.stdout)[:800]])
    return EvalResult(task.task_id, data.get("score", 0.0),
                      bool(data.get("passed", False)),
                      details=data.get("details", []),
                      errors=list(data.get("errors", [])), raw=data)



def run_task(task, solver=golden_solver, work_root=None):
    """完整跑一遍：布置环境 → solver 产出 → 判分。"""
    work_dir = work_root or tempfile.mkdtemp(prefix="ale_%s_" % task.task_id)
    setup_env(task, work_dir)
    solver(task, work_dir)
    return evaluate(task, work_dir)
