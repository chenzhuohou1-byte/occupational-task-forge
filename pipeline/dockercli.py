"""定位 docker 可执行文件（考生沙箱 / 可执行范式判分器共用）。

为什么要有：从 GUI、launchd、IDE 起的进程 PATH 里常常没有 /usr/local/bin，裸调 "docker"
直接 `[Errno 2] No such file or directory: 'docker'`，可执行题整批被记成「引擎不可达」。
PATH 上找得到就照旧用 "docker"（命令行与以往一字不差）；找不到才按常见安装位置回退。
都找不到仍返回 "docker"，调用方照旧拿到 OSError → 记基建故障，口径不变。
"""
import os
import shutil

# 常见安装位置：Docker Desktop 的软链、Homebrew(arm64)、Docker.app 自带
CANDIDATES = ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
              "/Applications/Docker.app/Contents/Resources/bin/docker")


def docker_bin():
    if shutil.which("docker"):
        return "docker"
    for p in CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return "docker"
