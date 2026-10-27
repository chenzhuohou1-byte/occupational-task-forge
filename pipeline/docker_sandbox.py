"""考生 run_python 的 **Docker 容器后端**（`ALE_RUN_SANDBOX=docker` 时启用）。

为什么要有：默认后端是 macOS `sandbox-exec`，Linux 开发机上没有它、run_python 直接拒跑；
只有 Linux 包的软件（如 OpenROAD）也只能进容器。这里给出同一套隔离纪律的容器实现，
**默认后端一个字不动**——不设 ALE_RUN_SANDBOX 时 agentsolve 走的还是原来那条路。

隔离边界（与 sandbox-exec 后端对齐，逐条可在 `_selftest_docker` 里实测）：
  · `--network none`：连不上网；
  · 只挂工作目录里的 input/（只读）、output/（可写）、_scratch/（可写，放脚本/假 HOME/TMPDIR）、
    software/（只读，存在才挂）——**不挂 reference/、不挂家目录、不挂仓根**（.env 在仓根）；
  · 容器根文件系统只读（--read-only），/tmp 是有大小上限的 tmpfs；
  · 环境变量只给固定白名单（HOME/LANG/TMPDIR/XDG_*…），**不透传宿主任何变量**，API key 进不来；
  · 非 root（宿主 uid:gid；宿主是 root 时退到 65534 nobody），--cap-drop ALL、no-new-privileges；
  · 内存/CPU/进程数上限，用完即删（--rm + finally `docker rm -f` 兜底）。

故障口径（判分契约 §9.5、engines/base.py 的 EngineUnavailable 同一精神）：
  · docker 不可达 / 镜像缺失 / docker 自身报错（退出码 125 等）/ 容器整体卡死超过宿主硬上限
    → 抛 SandboxUnavailable（RuntimeError 子类）→ agent_solve 向上抛 → n7_run 记**测量故障**，不是 0 分。
  · 考生脚本自己跑超时（容器内 `timeout`）、自己报错退出 → 和 sandbox-exec 后端一样作为观察回给模型，
    不算故障（否则模型写个死循环就能把一跑变成「故障补跑」，口径就不一致了）。
"""
import os
import subprocess
import time
import uuid

from .dockercli import docker_bin

DEFAULT_IMAGE = "ale-examinee:py3.12-sci-v1"
LABEL = "ale.examinee-sandbox=1"   # 泄漏的容器可用 `docker ps -a --filter label=ale.examinee-sandbox=1` 找回
# 容器里考生的工作目录。路径刻意与宿主无关：模型看不到宿主真实路径。
WORK = "/work"
# docker run 退出码 125 = docker 守护进程/参数错误；126/127 = 容器入口不可执行/找不到，
# 但脚本自己也能 sys.exit(126/127)，所以这两档还要看 stderr 是不是 docker/OCI 的报错。
_DOCKER_ERR_MARKERS = ("docker:", "OCI runtime", "executable file not found", "Error response from daemon")


class SandboxUnavailable(RuntimeError):
    """考生沙箱的基建故障（docker 不可达、镜像缺失、容器卡死）。

    与判分侧 EngineUnavailable 同义：调用方必须记**测量故障**，绝不记成「模型不会做」。
    继承 RuntimeError，是为了沿用 agentsolve 原有「沙箱缺失 → RuntimeError」的接法。
    """


def image():
    return os.environ.get("ALE_RUN_DOCKER_IMAGE", "").strip() or DEFAULT_IMAGE


def runtime_tag(img=None):
    """镜像标签 → measuredDifficulty.budget.runtime。一律带 `docker-` 前缀，和 macOS 版
    （py3.12-sci-v3 等）区分开：两种环境的测量不可比，旧测量不回灌。
    `ale-examinee:py3.12-sci-v1` → `docker-py3.12-sci-v1`；没写 tag 的按 docker 惯例记 latest。"""
    img = img or image()
    name = img.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    tag = last.split(":", 1)[1] if ":" in last else "latest"
    return "docker-" + tag


_PROBED = {}


def probe(img=None):
    """docker 守护进程可达且镜像在本地。只缓存成功结果；失败抛 SandboxUnavailable。
    不自动 pull：考生镜像必须是本地按 runtime/examinee/Dockerfile 构建的那份，拉来的同名镜像不可信。"""
    img = img or image()
    if _PROBED.get(img):
        return _PROBED[img]
    try:
        v = subprocess.run([docker_bin(), "version", "--format", "{{.Server.Version}} {{.Server.Arch}}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SandboxUnavailable("docker 调用失败：%s" % e)
    if v.returncode != 0:
        raise SandboxUnavailable("docker 不可用：%s" % (v.stderr or v.stdout).strip()[:300])
    try:
        i = subprocess.run([docker_bin(), "image", "inspect", "--format", "{{.Id}} {{.Architecture}}", img],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SandboxUnavailable("docker image inspect 失败：%s" % e)
    if i.returncode != 0:
        raise SandboxUnavailable(
            "考生镜像 %s 不在本地。先构建：docker build -t %s runtime/examinee（见 docs/考生沙箱-docker.md）"
            % (img, img))
    info = {"docker": v.stdout.strip(), "image": img, "imageId": i.stdout.strip()}
    _PROBED[img] = info
    return info


def _user():
    """宿主 uid:gid（output/ 里写出的文件归宿主用户，Linux 上也能直接删）；宿主是 root 就退到 nobody。"""
    uid, gid = (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (65534, 65534)
    return ("%d:%d" % (uid, gid)) if uid != 0 else "65534:65534"


def _mount(src, dst, readonly):
    # --mount 而不是 -v：路径里有冒号/逗号也不会被误切（-v 用冒号分段）
    return ["--mount", "type=bind,src=%s,dst=%s%s" % (src, dst, ",readonly" if readonly else "")]


def build_cmd(wd, script_rel, timeout, name, img=None):
    """拼 docker run 命令（纯函数，便于离线断言隔离参数）。

    wd：宿主工作目录（含 input/ output/ _scratch/）；script_rel：脚本相对 wd 的路径。
    """
    img = img or image()
    mem = os.environ.get("ALE_RUN_DOCKER_MEMORY", "2g")
    cpus = os.environ.get("ALE_RUN_DOCKER_CPUS", "2")
    pids = os.environ.get("ALE_RUN_DOCKER_PIDS", "256")
    platform = os.environ.get("ALE_RUN_DOCKER_PLATFORM", "").strip()
    scratch = WORK + "/_scratch"
    home = scratch + "/_home"
    # 与 sandbox-exec 后端同一组环境变量（假 HOME 在草稿目录里），但没有 PYTHONUSERBASE：
    # 容器里不存在宿主的用户 site-packages，包全在镜像里。
    env = {"HOME": home, "LANG": "C.UTF-8", "TMPDIR": scratch, "MPLCONFIGDIR": scratch,
           "KICAD_CONFIG_HOME": home + "/kicad", "XDG_CONFIG_HOME": home + "/.config",
           "XDG_CACHE_HOME": home + "/.cache", "XDG_DATA_HOME": home + "/.local/share"}
    cmd = [docker_bin(), "run", "--rm", "--pull", "never", "--name", name, "--label", LABEL,
           "--network", "none", "--read-only",
           "--tmpfs", "/tmp:rw,nosuid,size=256m",
           "--user", _user(), "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--memory", mem, "--memory-swap", mem, "--cpus", cpus, "--pids-limit", pids,
           "--init", "--workdir", WORK]
    if platform:   # 例：arm64 本机跑 amd64 镜像（qemu 模拟，慢一个量级）
        cmd += ["--platform", platform]
    cmd += _mount(os.path.join(wd, "input"), WORK + "/input", True)
    cmd += _mount(os.path.join(wd, "output"), WORK + "/output", False)
    cmd += _mount(os.path.join(wd, "_scratch"), scratch, False)
    if os.path.isdir(os.path.join(wd, "software")):
        cmd += _mount(os.path.join(wd, "software"), WORK + "/software", True)
    for k, v in sorted(env.items()):
        cmd += ["-e", "%s=%s" % (k, v)]   # 一律 K=V：裸 `-e K` 会透传宿主同名变量
    # 容器内 `timeout`：只计考生脚本自己的时间，不把起容器的开销算进模型的预算
    cmd += ["--entrypoint", "timeout", img, "--kill-after=5", str(int(timeout)),
            "python", "-X", "utf8", WORK + "/" + script_rel.replace(os.sep, "/")]
    return cmd


def run(wd, script, timeout):
    """在容器里跑 wd 下的 script（宿主绝对路径，须在 wd/_scratch 里）。

    返回 (head, text)，与 sandbox-exec 后端拼观察文本的口径一致：
      head = "退出码 N" 或 "超时（Ns）被终止"；text = stdout + [stderr]。
    基建故障抛 SandboxUnavailable。
    """
    img = image()
    probe(img)
    # bind 源目录不存在时 --mount 直接报错；先建好（_home 给假 HOME 用）
    for d in ("output", os.path.join("_scratch", "_home")):
        os.makedirs(os.path.join(wd, d), exist_ok=True)
    if not os.path.isdir(os.path.join(wd, "input")):
        raise ValueError("工作目录缺 input/：%s" % wd)
    rel = os.path.relpath(script, wd)
    if rel.startswith(".."):
        raise ValueError("脚本必须在工作目录里：%s" % script)
    name = "ale-examinee-" + uuid.uuid4().hex[:12]
    cmd = build_cmd(wd, rel, timeout, name, img)
    # 宿主硬上限 = 脚本超时 + 起停容器的余量；超过它说明 docker 本身卡住了 → 基建故障
    grace = int(os.environ.get("ALE_RUN_DOCKER_GRACE", "60"))
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout + grace)
    except subprocess.TimeoutExpired:
        raise SandboxUnavailable("容器在 %ds 内没结束（脚本超时 %ds + 余量 %ds），docker 卡住了"
                                 % (timeout + grace, timeout, grace))
    except OSError as e:
        raise SandboxUnavailable("docker 调用失败：%s" % e)
    finally:
        subprocess.run([docker_bin(), "rm", "-f", name], capture_output=True, timeout=30)
    err = r.stderr or ""
    if r.returncode == 125 or (r.returncode in (126, 127)
                               and any(m in err for m in _DOCKER_ERR_MARKERS)):
        raise SandboxUnavailable("docker 运行容器失败（退出码 %d）：%s" % (r.returncode, err.strip()[:500]))
    text = (r.stdout or "") + (("\n[stderr]\n" + err) if err else "")
    # coreutils timeout：到点 TERM → 124；TERM 后 5 秒还不退被 KILL → 137（此时已跑满 timeout 秒）
    if r.returncode == 124 or (r.returncode == 137 and time.monotonic() - t0 >= timeout):
        return "超时（%ds）被终止" % timeout, text
    if r.returncode == 137:   # 没到点就被 SIGKILL：多半撞了内存上限
        return "退出码 137（被 SIGKILL，多半超出内存上限 %s）" % os.environ.get(
            "ALE_RUN_DOCKER_MEMORY", "2g"), text
    return "退出码 %d" % r.returncode, text
