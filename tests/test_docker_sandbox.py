#!/usr/bin/env python3
"""考生 run_python 的 Docker 后端自测（`ALE_RUN_SANDBOX=docker`）。不花钱、不触网。

两层：
  A) 纯函数（任何机器都跑，CI 也跑）：后端选择、runtime 标签派生、docker run 参数里的隔离项
     （--network none / 只读 input / 不挂 reference 与家目录 / 不透传环境变量 / 非 root / 用完即删）、
     docker 不在 PATH 时抛 SandboxUnavailable（测量故障，不是 0 分）、默认后端不受影响。
  B) 真容器（本机有 docker 且考生镜像已构建才跑，否则打印跳过）：读不到 reference/、读不到 ~、
     连不上网、写不出 output/ 之外、拿不到宿主环境变量、非 root、超时如实回报、镜像缺失=测量故障、
     agent_solve 走 docker 后端能把交付物写进 output/。镜像不在本地时**不拉取**（--pull never）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import agentsolve, docker_sandbox  # noqa: E402
from pipeline.docker_sandbox import SandboxUnavailable  # noqa: E402

_KEYS = ("ALE_RUN_SANDBOX", "ALE_RUN_DOCKER_IMAGE", "ALE_RUN_PYTHON", "ALE_RUN_RUNTIME", "PATH")


class _Env:
    """临时改环境变量，退出时原样恢复（含删除原本不存在的键）。"""

    def __init__(self, **kv):
        self.kv, self.saved = kv, {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        docker_sandbox._PROBED.clear()
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        docker_sandbox._PROBED.clear()


def _workdir():
    """仿 harness.setup_env 的工作目录，外加一个**同级**的 reference/（真任务里答案就在旁边）。"""
    base = tempfile.mkdtemp(prefix="ale_dockertest_")
    wd = os.path.join(base, "work")
    for d in ("input", "output", "software"):
        os.makedirs(os.path.join(wd, d))
    with open(os.path.join(wd, "input", "a.txt"), "w") as f:
        f.write("input-ok")
    ref = os.path.join(base, "reference")
    os.makedirs(ref)
    with open(os.path.join(ref, "answer.txt"), "w") as f:
        f.write("SECRET-ANSWER")
    return base, wd, ref


def test_pure():
    with _Env(ALE_RUN_SANDBOX=None):
        assert agentsolve.sandbox_backend() == "sandbox-exec"
    with _Env(ALE_RUN_SANDBOX="Docker"):
        assert agentsolve.sandbox_backend() == "docker"
    with _Env(ALE_RUN_SANDBOX="dokcer"):
        try:
            agentsolve.sandbox_backend()
        except ValueError:
            pass
        else:
            raise AssertionError("拼错的后端名居然没报错（会悄悄退回默认后端）")

    assert docker_sandbox.runtime_tag("ale-examinee:py3.12-sci-v1") == "docker-py3.12-sci-v1"
    assert docker_sandbox.runtime_tag("reg.example:5000/ns/ale-examinee:v2@sha256:ab") == "docker-v2"
    assert docker_sandbox.runtime_tag("ale-examinee") == "docker-latest"
    with _Env(ALE_RUN_SANDBOX="docker", ALE_RUN_DOCKER_IMAGE=None,
              ALE_RUN_PYTHON="/should/be/ignored", ALE_RUN_RUNTIME="py3.12-sci-v3"):
        rt = agentsolve.runtime()
        assert rt[3] == "docker-py3.12-sci-v1", rt   # 不许沿用 macOS 标签
        assert rt[1] == [] and rt[2] == [], rt       # 宿主路径一概不放行
    with _Env(ALE_RUN_SANDBOX=None, ALE_RUN_PYTHON=None, ALE_RUN_RUNTIME="x-tag"):
        assert agentsolve.runtime()[3] == "x-tag"    # 默认后端标签逻辑不变
    print("✓ 后端选择 / runtime 标签派生（docker-<tag>，与 macOS 标签隔开）")

    base, wd, ref = _workdir()
    try:
        home = os.path.realpath(os.path.expanduser("~"))
        os.environ["ALE_FAKE_API_KEY"] = "sk-should-not-leak"
        cmd = docker_sandbox.build_cmd(wd, "_scratch/x.py", 30, "ale-examinee-test")
        os.environ.pop("ALE_FAKE_API_KEY")
        joined = " ".join(cmd)
        i = cmd.index("--entrypoint")
        opts = cmd[:i]
        assert opts[:3] == ["docker", "run", "--rm"], opts
        assert ["--network", "none"] == opts[opts.index("--network"):opts.index("--network") + 2]
        assert "--read-only" in opts and "--cap-drop" in opts and "no-new-privileges" in opts
        assert ["--pull", "never"] == opts[opts.index("--pull"):opts.index("--pull") + 2]
        for flag in ("--memory", "--cpus", "--pids-limit"):
            assert flag in opts, flag
        user = opts[opts.index("--user") + 1]
        assert not user.startswith("0:"), "容器不许以 root 跑：%s" % user
        mounts = [opts[k + 1] for k, x in enumerate(opts) if x == "--mount"]
        srcs = {m.split("src=", 1)[1].split(",dst=", 1)[0]: m for m in mounts}
        assert set(srcs) == {os.path.join(wd, d) for d in ("input", "output", "_scratch", "software")}, srcs
        assert srcs[os.path.join(wd, "input")].endswith(",readonly")
        assert srcs[os.path.join(wd, "software")].endswith(",readonly")
        assert not srcs[os.path.join(wd, "output")].endswith(",readonly")
        assert ref not in joined and "reference" not in joined, "reference/ 被挂进去了"
        assert not any(s == home or s.rstrip("/") == os.path.dirname(wd) for s in srcs), "挂了家目录/上级目录"
        assert "-v" not in opts, "只许 --mount（-v 按冒号切路径）"
        envs = [opts[k + 1] for k, x in enumerate(opts) if x == "-e"]
        assert all("=" in e for e in envs), "裸 -e KEY 会透传宿主变量：%s" % envs
        assert {e.split("=", 1)[0] for e in envs} <= {
            "HOME", "LANG", "TMPDIR", "MPLCONFIGDIR", "KICAD_CONFIG_HOME",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"}, envs
        assert "sk-should-not-leak" not in joined and "--env-file" not in opts
        assert cmd[i + 1:i + 3] == ["timeout", docker_sandbox.image()], cmd[i:]
        print("✓ docker run 参数：无网 / 只读 input / 不挂 reference 与 ~ / 环境变量白名单 / 非 root / 用完即删")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    # docker 不在 PATH → SandboxUnavailable（RuntimeError 子类，n7_run 记测量故障）
    empty = tempfile.mkdtemp(prefix="ale_nodocker_")
    base, wd, _ = _workdir()
    try:
        with _Env(ALE_RUN_SANDBOX="docker", PATH=empty):
            try:
                agentsolve.run_python(wd, "print(1)", "nodocker")
            except SandboxUnavailable as e:
                assert isinstance(e, RuntimeError) and "docker" in str(e), e
            else:
                raise AssertionError("docker 不可用却没抛 SandboxUnavailable")
        print("✓ docker 不可用 → SandboxUnavailable（测量故障，不是 0 分）")
    finally:
        shutil.rmtree(empty, ignore_errors=True)
        shutil.rmtree(base, ignore_errors=True)


def _docker_ready():
    if not shutil.which("docker"):
        return "本机没有 docker"
    try:
        docker_sandbox.probe()
    except SandboxUnavailable as e:
        return str(e)[:160]
    return None


def test_live():
    why = _docker_ready()
    if why:
        print("- 跳过真容器隔离测试：%s" % why)
        return
    base, wd, ref = _workdir()
    home = os.path.realpath(os.path.expanduser("~"))
    probe_file = os.path.abspath(__file__)   # 宿主家目录下一定存在的文件
    os.environ["ALE_FAKE_API_KEY"] = "sk-should-not-leak"
    try:
        with _Env(ALE_RUN_SANDBOX="docker"):
            code = "\n".join([
                "import os, socket, urllib.request",
                "def t(name, fn):",
                "    try: fn(); print('LEAK', name)",
                "    except Exception as e: print('blocked', name, type(e).__name__)",
                "t('read_reference_rel', lambda: open('../reference/answer.txt').read())",
                "t('read_reference_abs', lambda: open(%r).read())" % os.path.join(ref, "answer.txt"),
                "t('read_home_file', lambda: open(%r).read())" % probe_file,
                "t('list_home', lambda: os.listdir(%r))" % home,
                "t('net_http', lambda: urllib.request.urlopen('https://example.com', timeout=5))",
                "t('net_ip', lambda: socket.create_connection(('1.1.1.1', 53), timeout=5))",
                "t('write_input', lambda: open('input/x', 'w').write('x'))",
                "t('write_workroot', lambda: open('/work/x', 'w').write('x'))",
                "t('write_usr', lambda: open('/usr/local/x', 'w').write('x'))",
                "t('write_etc', lambda: open('/etc/x', 'w').write('x'))",
                # 基础镜像自带 GPG_KEY（Python 发布签名公钥 ID，不是密钥），不能按「名字含 KEY」判；
                # 直接查宿主那个假 key 的名字与值，再查宿主独有的 PATH/USER 没跟进来
                "print('env_leak' if ('ALE_FAKE_API_KEY' in os.environ or any('sk-should-not-leak' in v"
                " for v in os.environ.values()) or 'USER' in os.environ) else 'env_clean')",
                "print('uid', os.getuid())",
                "print('input', open('input/a.txt').read())",
                "open('output/res.txt', 'w').write('ok')",
            ])
            obs, changed = agentsolve.run_python(wd, code, "iso")
        n_block = 10
        assert "LEAK" not in obs and obs.count("blocked") == n_block, obs
        assert "env_clean" in obs and "uid 0\n" not in obs and "input input-ok" in obs, obs
        assert changed == ["res.txt"] and open(os.path.join(wd, "output", "res.txt")).read() == "ok"
        # 宿主侧：只有 output/ 与 _scratch/ 有新东西，input/ 原样
        assert sorted(os.listdir(os.path.join(wd, "input"))) == ["a.txt"]
        assert sorted(os.listdir(wd)) == ["_scratch", "input", "output", "software"], os.listdir(wd)
        print("✓ 真容器隔离：reference/ 与 ~ 读不到、断网、只写得了 output/、无宿主环境变量、非 root")

        with _Env(ALE_RUN_SANDBOX="docker"):
            obs, _ = agentsolve.run_python(wd, "import time\nprint('start', flush=True)\ntime.sleep(60)",
                                           "slow", timeout=3)
        assert "超时（3s）被终止" in obs, obs
        print("✓ 考生脚本超时 → 如实回报给模型（与 sandbox-exec 后端同一口径）")

        left = subprocess.run(["docker", "ps", "-aq", "--filter", "label=" + docker_sandbox.LABEL],
                              capture_output=True, text=True, timeout=30).stdout.split()
        assert not left, "有容器没删干净：%s" % left
        print("✓ 容器用完即删（无残留）")

        with _Env(ALE_RUN_SANDBOX="docker", ALE_RUN_DOCKER_IMAGE="ale-examinee-missing:nope"):
            try:
                agentsolve.run_python(wd, "print(1)", "noimg")
            except SandboxUnavailable as e:
                assert "不在本地" in str(e), e
            else:
                raise AssertionError("镜像缺失却没抛 SandboxUnavailable")
        print("✓ 镜像缺失 → SandboxUnavailable（不自动拉取）")

        # agent_solve 端到端：docker 后端下 run_python 写交付物、runtime 标签是 docker-*
        from pipeline.tasks import load_task
        t = load_task(os.path.join(ROOT, "tasks", "_demo", "采购清单核算"))
        deliver = list(t.card.get("交付要求", {}))[0]
        code = ("import os\nprint(sorted(os.listdir('input')))\n"
                "open(os.path.join('output', %r), 'w', encoding='utf-8').write('{}')\n" % deliver)
        with _Env(ALE_RUN_SANDBOX="docker"):
            _, r = agentsolve.agent_solve(t, agentsolve._ScriptedClient([
                json.dumps({"action": "run_python", "code": code}),
                json.dumps({"action": "finish"})]), max_steps=4)
            assert agentsolve.runtime()[3].startswith("docker-")
        assert r["run_python次数"] == 1 and r["写出文件数"] == 1 and r["write次数"] == 0, r
        print("✓ agent_solve 走 docker 后端写交付物 OK（score %.4f）" % r["score"])
    finally:
        os.environ.pop("ALE_FAKE_API_KEY", None)
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    test_pure()
    test_live()
