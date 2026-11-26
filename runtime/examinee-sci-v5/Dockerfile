# 生命科学成像考生镜像（3.1 结构 / 3.2 基因组 / 3.3 细胞与成像 / 3.4 单细胞）。见 docs/考生沙箱-docker.md §10。
#
# 构建（在仓根，先建好 v1 与 bio-v2；cellpose 权重 models/cpsam 不入库，构建前按下方说明放好）：
#   docker build -t ale-examinee:py3.12-sci-v1 runtime/examinee
#   docker build -t ale-examinee:py3.12-bio-v2 runtime/examinee-bio
#   docker build -t ale-examinee:py3.12-sci-v5 runtime/examinee-sci-v5
# 已有测量用镜像 c49679fd 的机器不要用同一 tag 重建（tag 会被挪到新镜像上）；
# 验证本配方请另起 tag，如 ale-examinee:py3.12-sci-v5-rebuild。
# 只在 bio-v2 之上**加**：v1、bio-v2 已有的分发一个版本都不动（--no-deps + pip check 兜底），
# 新增 requirements-sci-v5.txt 的 82 个分发和 cellpose 默认权重 cpsam。命令行工具沿用 bio-v2 的
# samtools / bcftools / tabix / bwa / minimap2，不另装。
#
# 来历：测量用的本机镜像 sha256:c49679fd6f102383c6acced2cec4cc27cfd9d798fb0a3c8e2b541e8283d3a350
# （arm64，2026-11-04 构建）当时是用一份单层配方从与 v1 相同 digest 的 python:3.12-slim 直接建的，没有叠层。
# 它的容器内 pip freeze 见 pip-freeze-c49679fd.txt：等于 v1 ∪ bio-v2 ∪ 本目录新增表，逐个同版本；
# 命令行工具为 Debian 13 的 samtools/bcftools/tabix 1.21、bwa 0.7.18、minimap2 2.27。
# 按本文件叠层重建，镜像 ID 会与 c49679fd 不同；测量口径仍是 tag：docker-py3.12-sci-v5。
FROM ale-examinee:py3.12-bio-v2

COPY requirements-sci-v5.txt /opt/ale/requirements-sci-v5.txt

# torch/torchvision 取 PyTorch CPU 源的 +cpu 轮子：阿里 PyPI 上没有，默认 PyPI 的轮子会拖入整套 nvidia-*。
# docrep / pims / trackpy 只有源码包、且是纯 Python（无 C 扩展），单独放开 --only-binary，仍然 --no-deps。
# pip check 要等三只都装上再跑：omnipath、squidpy 依赖 docrep，dask-image 依赖 pims。
RUN grep -E '^(torch|torchvision)==' /opt/ale/requirements-sci-v5.txt > /tmp/torch.txt \
 && pip install --no-cache-dir --no-deps --only-binary=:all: \
        --index-url https://download.pytorch.org/whl/cpu \
        -r /tmp/torch.txt \
 && grep -Ev '^(torch|torchvision|docrep|pims|trackpy)==' /opt/ale/requirements-sci-v5.txt > /tmp/rest.txt \
 && pip install --no-cache-dir --no-deps --only-binary=:all: \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        -r /tmp/rest.txt \
 && grep -E '^(docrep|pims|trackpy)==' /opt/ale/requirements-sci-v5.txt > /tmp/sdist.txt \
 && pip install --no-cache-dir --no-deps \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        -r /tmp/sdist.txt \
 && pip check

# cellpose 默认模型 cpsam（cellpose 4 的默认权重，1233587898 字节）。考生容器和判分容器都是 --network none，
# 运行时下载必然失败，所以构建期放进镜像；cellpose 读 CELLPOSE_LOCAL_MODELS_PATH，缺文件才去 huggingface 下载。
# 权重不入库（.gitignore 排除 runtime/examinee-sci-v5/models/）：构建前放到 runtime/examinee-sci-v5/models/cpsam，
# 来源 https://huggingface.co/mouseland/cellpose-sam （国内用 hf-mirror.com，同一份内容）。
ENV CELLPOSE_LOCAL_MODELS_PATH=/opt/cellpose/models
COPY models/cpsam /opt/cellpose/models/cpsam
RUN echo "e1440429eb384f95afe32bcba6510f90d518eaedc917ede549bed6804004abe2  /opt/cellpose/models/cpsam" | sha256sum -c - \
 && chmod -R a+rX /opt/cellpose

# 冒烟：新加的包能 import、版本与表一致、Leiden 两种后端真跑、权重在位；任何一项失败就让 docker build 失败。
COPY smoke_sci_v5.py /opt/ale/smoke_sci_v5.py
RUN python /opt/ale/smoke_sci_v5.py && chmod -R a+rX /usr/local/lib/python3.12/site-packages /usr/local/bin
WORKDIR /work
