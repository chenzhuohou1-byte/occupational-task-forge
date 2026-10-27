# 考生沙箱 · Docker 后端（`ALE_RUN_SANDBOX=docker`）

N7 agent 档里考生的 `run_python` 默认走 macOS `sandbox-exec`（`pipeline/agentsolve.py`）。Linux 上没有它、
run_python 直接拒跑；只有 Linux 包的软件（如 OpenROAD）也装不进 macOS 考生环境。本节说明容器后端怎么用。
**不设 `ALE_RUN_SANDBOX` 时一切照旧**：默认后端、`ALE_RUN_PYTHON/SOFTWARE/PATH/RUNTIME` 的语义、已有测量口径都没动。

## 1. 构建考生镜像

```bash
cd ~/occupational-task-forge
docker build -t ale-examinee:py3.12-sci-v1 runtime/examinee
```

- 基础镜像：`docker.m.daocloud.io/library/python:3.12-slim`，钉 digest（docker.io 直连可能不通，daocloud 是同一份内容）。
- Python 包：`runtime/examinee/requirements.txt`，**78 个分发全量钉死**（含传递依赖），与 macOS 考生环境
  `py3.12-sci-v3` 逐个同版本；`--no-deps --only-binary=:all:` 安装 + `pip check` 兜底，装什么只由这张表决定。
  源是 HTTPS 阿里 PyPI 镜像，**正常证书校验**（没有 `--trusted-host`）。
- 系统库：冒烟实测 pinocchio 缺 `libgomp`、SUMO 二进制缺 `libatomic/libX11/libexpat` 等，apt 源换成 HTTPS 阿里
  Debian 镜像补装（apt 签名校验照常）。本机首次构建约 45 分钟（主要是 1 GB 轮子下载），镜像 3.11 GB。
- 构建期冒烟（`runtime/examinee/smoke.py`）：主力包 import + `sumo/netconvert --version`；YoWASP 的 wasm
  预编译缓存预热进镜像（`YOWASP_CACHE_DIR=/opt/yowasp-cache`），否则只读根文件系统下每次调用都要重编。
- 与 macOS 版的差异：`embreex`（trimesh 可选的光线求交加速）PyPI 没有 linux/aarch64 轮子，只在 x86_64 镜像里装；
  arm64 镜像里 trimesh 退回纯 numpy 求交。macOS 版另有的 KiCad / Blender / Inkscape 是宿主 GUI 应用，**镜像里没有**
  ——依赖它们的题（C2/C5/C10 等）不能直接切到 docker 后端，要另做镜像。

**tag 就是测量口径**：改 Dockerfile 或 requirements 就换 tag（`py3.12-sci-v2`…）。

## 2. 切后端

```bash
export ALE_RUN_SANDBOX=docker                              # 默认 sandbox-exec；拼错直接报错，不会悄悄退回默认
export ALE_RUN_DOCKER_IMAGE=ale-examinee:py3.12-sci-v1     # 可省，缺省即此
# 可选：ALE_RUN_DOCKER_MEMORY=2g  ALE_RUN_DOCKER_CPUS=2  ALE_RUN_DOCKER_PIDS=256
#       ALE_RUN_DOCKER_PLATFORM=linux/amd64（arm64 本机跑 amd64 镜像，见 §4）
#       ALE_RUN_DOCKER_GRACE=60（宿主硬上限 = 脚本超时 + 这么多秒，超了算 docker 卡死）
python3 n7_run.py ...
```

- 测量记录里 `measuredDifficulty.budget.runtime` 记为 **`docker-<镜像 tag>`**（如 `docker-py3.12-sci-v1`），
  与 macOS 版（`py3.12-sci-v3` 等）天然分开；**旧测量不回灌、两种环境不可比**。
- docker 后端下 `ALE_RUN_PYTHON / ALE_RUN_SOFTWARE / ALE_RUN_PATH / ALE_RUN_RUNTIME` 一概不看（它们是宿主路径/标签）。
- 镜像必须事先在本地构建好；`docker run --pull never`，**不会自动拉取**同名镜像。

## 3. 隔离边界

| 项 | 做法 | 实测（`tests/test_docker_sandbox.py` 真容器段） |
|---|---|---|
| 网络 | `--network none` | HTTPS 与裸 IP TCP 都连不上 |
| 答案 | 只挂工作目录的 `input/`（ro）、`output/`（rw）、`_scratch/`（rw，脚本+假 HOME+TMPDIR）、`software/`（ro，存在才挂）；**不挂 reference/、家目录、仓根** | `../reference/`、宿主绝对路径都读不到 |
| 家目录 / .env | 同上；容器里的 HOME 是 `_scratch/_home` | 读宿主 `~` 下文件、列 `~` 都失败 |
| 写 | `--read-only` 根文件系统 + `/tmp` tmpfs(256m) | 写 `input/`、`/work`、`/usr`、`/etc` 都失败；只有 `output/` 写得进 |
| 环境变量 | 只给固定白名单（HOME/LANG/TMPDIR/XDG_*/KICAD_CONFIG_HOME/MPLCONFIGDIR），一律 `-e K=V`，无 `--env-file` | 宿主的 `*KEY*` 变量进不来 |
| 权限 | `--user` 宿主 uid:gid（宿主为 root 时用 65534）、`--cap-drop ALL`、`no-new-privileges` | 容器内 uid≠0 |
| 资源 | `--memory`=`--memory-swap`（默认 2g）、`--cpus`（默认 2）、`--pids-limit`（默认 256） | — |
| 生命周期 | `--rm` + finally `docker rm -f`；容器带 label `ale.examinee-sandbox=1` | 跑完无残留容器 |

**故障口径**（同判分侧 `EngineUnavailable`）：docker 不可达、镜像不在本地、docker 自身报错（退出码 125 等）、
容器超过宿主硬上限仍未结束 → 抛 `SandboxUnavailable`（`RuntimeError` 子类）→ n7_run 记**测量故障**、补跑，不是 0 分。
考生脚本自己超时（容器内 `timeout`）或报错 → 和 sandbox-exec 后端一样作为观察文本回给模型，不算故障。

## 4. 已知限制

- **arm64 本机跑 amd64 镜像要模拟**（Docker Desktop 的 Rosetta/QEMU），设 `ALE_RUN_DOCKER_PLATFORM=linux/amd64`；
  CPU 密集脚本会慢数倍，可能把正常解法推过 `ALE_RUN_PYTHON_TIMEOUT`——在模拟下测出的难度要单独标注，别与原生混。
- 本机 Docker Desktop 只分到 4 GB 内存，考生容器默认 2g；大附件题若 OOM 会以「退出码 137」回给模型（不是故障）。
- 起容器每次约 0.3–1 s 开销，比 sandbox-exec 慢；不计入考生脚本超时。
- 只覆盖 N7 的 `run_python`。casegen 的 worker（`pipeline/sandbox.py`）仍只支持 sandbox-exec。
- macOS 上 bind mount 走 Docker Desktop 的文件共享（VirtioFS），大量小文件 IO 比原生慢。
- `output/` 里文件归宿主 uid；若宿主为 root，文件归 65534。
