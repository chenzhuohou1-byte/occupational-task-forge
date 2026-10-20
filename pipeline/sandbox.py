"""生题环节的沙箱启动器（判分契约 §9.5 的生产隔离精神用到数值 casegen 侧）。

为什么：casegen 生成半段里模型写的 参数生成器/solve/solve2/物化 经 importlib 在宿主进程
直接执行，拥有完整宿主权限——能读 .env（网关密钥就在仓根）、能联网外泄、能写任意路径。
本模块把这些模型代码放进 macOS sandbox-exec 子进程：无网络、读不了 ~（含 ~/.ssh/.aws）、
仓内 .env 显式拒读、只许写一个临时 scratch。没有 sandbox-exec 的机器（如 Linux CI）默认
**拒跑**；仅可信离线自测可设 ALE_CASEGEN_UNSANDBOXED=1 放行（跑的是 crosscheck_cases/good，
不是不可信模型产物）。

与 agentsolve.run_python 同一套纪律，但它服务 N7 测量那条载荷路径、不动它，这里独立实现。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_UNSANDBOXED_ENV = "ALE_CASEGEN_UNSANDBOXED"


def _profile(read_dirs, scratch):
    """SBPL：无网；读不了 ~，但放行 python 与仓代码/案例目录；仓内 .env* 显式拒读（最后一条，覆盖前面的放行）；
    只许写 scratch。SBPL 规则后者覆盖前者，故 deny .env 必须排在 allow 之后。"""
    home = os.path.realpath(os.path.expanduser("~"))
    q = lambda p: '"%s"' % p.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    reads = sorted({os.path.realpath(p) for p in read_dirs})
    return "\n".join([
        "(version 1)", "(allow default)", "(deny network*)",
        "(deny file-read* (subpath %s))" % q(home),
        "(allow file-read* (literal %s) %s)" % (q(home),
            " ".join("(subpath %s)" % q(p) for p in reads)),
        # 仓根密钥：即使仓目录被放行读，也要把 .env 单独拒掉
        '(deny file-read* (literal %s) (regex #"%s/\\.env(\\..*)?$"))'
        % (q(os.path.join(ROOT, ".env")), ROOT.replace("\\", "\\\\")),
        "(deny file-write*)",
        '(allow file-write* (subpath %s) (literal "/dev/null") (regex #"^/dev/tty"))' % q(scratch),
    ])


def run_worker(argv, read_dirs, timeout=900, label="casegen-worker"):
    """在沙箱里跑 `python argv...`，解析最后一行 stdout 为 JSON 返回。

    无 sandbox-exec 且未设 ALE_CASEGEN_UNSANDBOXED=1 → 抛 RuntimeError（拒绝裸跑模型代码）。
    读权限放行 python 前缀 + 仓根 + read_dirs；写权限只给临时 scratch；环境变量清到最小、不带密钥。
    """
    scratch = tempfile.mkdtemp(prefix="casegen_sbx_")
    try:
        reads = set(read_dirs) | {sys.prefix, sys.base_prefix, ROOT, scratch}
        try:
            import site
            reads.add(site.getusersitepackages())
        except Exception:  # noqa: BLE001
            pass
        reads.add(os.path.join(os.path.expanduser("~"), "Library", "Python"))
        env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"),
               "LANG": "en_US.UTF-8", "TMPDIR": scratch, "MPLCONFIGDIR": scratch,
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ROOT}
        cmd = [sys.executable, "-X", "utf8"] + list(argv)
        if shutil.which("sandbox-exec"):
            prof = os.path.join(scratch, "_sandbox.sb")
            with open(prof, "w", encoding="utf-8") as f:
                f.write(_profile(reads, scratch))
            cmd = ["sandbox-exec", "-f", prof] + cmd
        elif os.environ.get(_UNSANDBOXED_ENV) != "1":
            raise RuntimeError(
                "%s 需要沙箱（macOS sandbox-exec）；本机没有，拒跑以免模型代码读到 .env/密钥或联网。"
                "可信离线自测可设 %s=1 放行。" % (label, _UNSANDBOXED_ENV))
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()
        if not out:
            raise RuntimeError("%s 无输出（退出码 %d）：%s" % (label, r.returncode, (r.stderr or "")[:600]))
        try:
            return json.loads(out.splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            raise RuntimeError("%s 未输出合法 JSON：%s\nstderr:%s"
                               % (label, out[:600], (r.stderr or "")[:600]))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
