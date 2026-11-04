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

## 5. 生命科学镜像 `ale-examinee:py3.12-bio-v2`（2026-11-02）

给 3.2 基因组、3.3 细胞与成像出题用。**叠在 v1 上**，v1 的 78 个分发一个版本都不动，只新增：

- Python 48 个分发（`runtime/examinee-bio/requirements-bio.txt`，全量钉死）：biopython、pysam、scanpy、anndata、
  scikit-image、tifffile、h5py、scikit-learn，以及它们拉进来的 numba、umap-learn、matplotlib、statsmodels 等。
- 命令行工具（Debian 包）：samtools 1.21、bcftools、tabix、bwa、minimap2。

```bash
docker build -t ale-examinee:py3.12-sci-v1 runtime/examinee        # 先有基础镜像（已有就跳过）
docker build -t ale-examinee:py3.12-bio-v2 runtime/examinee-bio    # 增量层，本机约 5 分钟，镜像 3.9 GB
export ALE_RUN_SANDBOX=docker ALE_RUN_DOCKER_IMAGE=ale-examinee:py3.12-bio-v2
```

- 测量记录的 runtime 标签是 `docker-py3.12-bio-v2`，和 v1 下的测量分开算。默认镜像仍是 v1，只有生命科学题需要切。
- 构建期冒烟（`smoke_bio.py`）会真跑一遍 scanpy 的 normalize → PCA → neighbors → UMAP；在 `--read-only`、
  `--network none`、非 root 用户下也实测通过（numba 缓存写到一次性的 HOME）。
- 内存：Docker Desktop 只分到约 4 GB、考生容器默认 2g。单细胞矩阵题请把输入控制在几万细胞以内，
  或给这类题单独设 `ALE_RUN_DOCKER_MEMORY`（并在卡上注明）。
- **没装 cellpose**：它要 PyTorch（CPU 轮子约 1 GB 级），先看 3.3 的题是否真需要深度学习分割再定；
  传统分割用 scikit-image 即可。

## 6. 扩展镜像 `ale-examinee:py3.12-ext-v3`（2026-11-03）

补 bio-v2 里没有的专业包，给序列/区间、HMM、医学影像、生存分析、栅格、供水管网题用。**叠在 bio-v2 上**，已有分发一个版本都不动，只新增
（`runtime/examinee-ext/requirements-ext.txt`，全量钉死）：

- pyranges 0.1.7（含 ncls、sorted-nearest、tabulate）、pyhmmer 0.12.3（含 psutil）、pydicom 3.0.2、SimpleITK 2.5.7、
  lifelines 0.30.0（含 autograd、autograd-gamma）、rasterio 1.5.2（含 affine）、wntr 1.5.0（带 EPANET 2.2 求解器）。

```bash
# x86_64（先有 v1、bio-v2）
docker build -t ale-examinee:py3.12-ext-v3 runtime/examinee-ext
# arm64：装不了 wntr，只能建不含 wntr 的变体，tag 不同
docker build --build-arg WITH_WNTR=0 -t ale-examinee:py3.12-ext-v3-nowntr runtime/examinee-ext
export ALE_RUN_SANDBOX=docker ALE_RUN_DOCKER_IMAGE=ale-examinee:py3.12-ext-v3   # 或 …-ext-v3-nowntr
```

- 测量记录的 runtime 标签是 `docker-py3.12-ext-v3` 或 `docker-py3.12-ext-v3-nowntr`。两者除 wntr 外内容相同，
  不用 wntr 的题两种测量可以互认；**用 wntr 的题只认 ext-v3 的测量**。
- ncls、sorted-nearest 在 PyPI 上没有 aarch64 轮子，Dockerfile 用一次性的构建阶段（gcc）编出轮子再装，最终镜像不带编译器。
  构建阶段的基础镜像与 v1 同一个 digest，Cython/setuptools 版本钉死。
- wntr 1.5.0 在 PyPI 上只有 x86_64 Linux 轮子，源码包里的 EPANET 求解器也只带 linux-x64 的 `.so`，arm64 上装不了也跑不了。
  arm64 本机要测 wntr 题：把 v1、bio-v2、ext-v3 都用 `docker build --platform linux/amd64` 建一遍，
  再用 `ALE_RUN_DOCKER_PLATFORM=linux/amd64` 跑（模拟，慢）；或在 x86_64 机器上测。
- 冒烟：WITH_WNTR=1 时缺 wntr 会让构建失败；WITH_WNTR=0 时镜像里出现 wntr 也会失败，两种 tag 不会混。
  pyhmmer 冒烟真跑一次 build_msa + hmmsearch。
- **没装 pydssp**：它 import 时无条件 import torch，x86_64 的 PyPI torch 还会拖 CUDA 全家桶（数 GB）。
  二级结构题请改用 Bio.PDB 自己实现判据，或另议 CPU 版 torch 的装法。

## 7. bench-astro:v1（天文方向专用镜像）

天文与天体物理方向的可执行范式题（H7b 光变曲线周期提取、H8b 坐标转换与可观测窗口、
H9b 星系多版本测光一致性）在 `ale-examinee:py3.12-sci-v1` 之外额外依赖 `numpy`/`astropy`/
`scipy`，单独登记为 `bench-astro:v1`，不与通用考生镜像合并（避免给所有题都装天文方向
才需要的重依赖）。构建方式与 `runtime/examinee/` 同构：

```bash
cd ~/occupational-task-forge
docker build -t bench-astro:v1 runtime/bench-astro
```

- 版本钉死依据：原镜像构建于 2026-10-26，`pip install numpy astropy scipy` 时未钉版本号，
  现用 `pip freeze` 回填实际装上的版本到 `runtime/bench-astro/requirements.txt`，使重建结果
  与原镜像一致。
- `astropy-iers-data` 与 `astropy` 一起钉死：H8b 用到的坐标/时间转换依赖地球自转参数
  （IERS 表），考生容器断网运行，只能用这个包自带的数据；数据版本不同，转换结果可能在
  小数位上变化，足以让 1e-5/1e-4 容差的判分翻车。
- 同样遵守"tag 就是测量口径"：改 `runtime/bench-astro/requirements.txt` 或 Dockerfile 就要换
  tag（v2…），旧测量不回灌。

## 8. 电力系统镜像 `ale-examinee:py3.12-power-v4`（2026-11-04）

给配电网潮流题（X2 配电网开关方案与电压越限复核）用。**叠在 v1 上**，v1 的分发一个版本都不动，只新增
（`runtime/examinee-power/requirements-power.txt`，全量钉死）：OpenDSSDirect.py 0.9.4、dss-python 0.15.7、
dss-python-backend 0.14.5（DSS C-API）、cffi 2.0.0、pycparser 2.23。

```bash
docker build -t ale-examinee:py3.12-power-v4 runtime/examinee-power    # 先有 v1
export ALE_RUN_SANDBOX=docker ALE_RUN_DOCKER_IMAGE=ale-examinee:py3.12-power-v4
```

- 测量记录的 runtime 标签是 `docker-py3.12-power-v4`。arm64 / x86_64 都有轮子，两边内容相同。
- 不叠在 ext-v3 上：ext-v3 的 wntr 在 arm64 上装不了。同一道题要同时用 OpenDSS 和 ext-v3 的包时再另议。
- 冒烟真解一次三相电路，未收敛构建失败。

## 9. bench-medical:v1（医疗/心理/社科/量化方向专用镜像）

12 道可执行与数值题在用：H3 PhysioNet ECG、H6/H7/H8（医疗健康），H2/H4/H5（实验心理与
神经科学），H9/H10（经济与定量社会科学），H1b/H2b/H3b（量化金融）。依赖 pandas/numpy/
scipy/scikit-learn/statsmodels/matplotlib/openpyxl/pyarrow 这一组数据分析栈，单独登记为
`bench-medical:v1`：

```bash
cd ~/occupational-task-forge
docker build -t bench-medical:v1 runtime/bench-medical
```

- **Python 是 3.11.15**（基础镜像 `python:3.11-slim`，digest 已钉），与 `ale-examinee` 系列的
  3.12 刻意不同：这 12 道题的测量是在 pandas 2.2.2 / numpy 1.26.4 / scipy 1.13.1 这一组
  3.11 轮子上做的，换 3.12 要重解依赖并换 tag、全部重测。
- 版本钉死依据：原镜像构建于 2026-08-03、build 时未钉版本号，现用
  `docker run --rm bench-medical:v1 pip freeze` 回填实际装上的 29 个包到
  `runtime/bench-medical/requirements.txt`，使重建结果与原镜像一致。
- `pytest` / `pytest-timeout` 一并保留：部分题的判分侧在容器内跑用例，缺了起不来。
- 构建期冒烟（`smoke.py`）不只 import，还真跑一遍 pandas 分组聚合 → statsmodels OLS →
  scipy 检验 → sklearn 拟合 → openpyxl 往返 → pyarrow 往返 → matplotlib(Agg) 出图；
  "装上但用不了"（缺字体、无显示后端、ABI 不配）会在构建期就失败。
- 同样遵守"tag 就是测量口径"：改 `runtime/bench-medical/requirements.txt` 或 Dockerfile 就要
  换 tag（v2…），旧测量不回灌。
