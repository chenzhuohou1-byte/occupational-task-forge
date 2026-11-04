# 扩展考生镜像（序列/基因组区间 pyranges、HMM pyhmmer、医学影像 pydicom/SimpleITK、生存分析 lifelines、栅格 rasterio、供水管网 wntr）。
#
# 构建（在仓根，先建好 v1 与 bio-v2）：
#   docker build -t ale-examinee:py3.12-sci-v1 runtime/examinee
#   docker build -t ale-examinee:py3.12-bio-v2 runtime/examinee-bio
#   docker build -t ale-examinee:py3.12-ext-v3 runtime/examinee-ext                                   # x86_64
#   docker build --build-arg WITH_WNTR=0 -t ale-examinee:py3.12-ext-v3-nowntr runtime/examinee-ext    # arm64
# 只在 bio-v2 之上**加**包，已有分发一个版本都不动（pip check 兜底）。
# wntr 1.5.0 只有 x86_64 Linux 轮子，源码包里的 EPANET 求解器也只带 linux-x64 的 .so，arm64 装不上也跑不了。
# 不装 wntr 的变体内容不同，所以 tag 必须不同（-nowntr），不能和 ext-v3 混记测量口径。

# ncls、sorted_nearest 在 PyPI 上没有 aarch64 轮子：在一次性构建阶段用 gcc 编出轮子，最终镜像不带编译器。
# 基础镜像与 v1 同一个 digest（同一个 Python 3.12 ABI）；构建依赖钉死、关掉构建隔离，免得 pip 现拉最新 Cython。
FROM docker.m.daocloud.io/library/python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS wheelbuilder
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
        Cython==3.3.1 setuptools==84.2.1 wheel==0.48.0 setuptools-scm==10.3.4 \
 && pip wheel --no-cache-dir --no-deps --no-build-isolation -i https://mirrors.aliyun.com/pypi/simple/ -w /wheels \
        ncls==0.0.71 sorted_nearest==0.0.42

FROM ale-examinee:py3.12-bio-v2
ARG WITH_WNTR=1
COPY --from=wheelbuilder /wheels/ncls-*.whl /wheels/sorted_nearest-*.whl /opt/ale/native-wheels/
COPY requirements-ext.txt /opt/ale/requirements-ext.txt
RUN skip='^(#|$|ncls==|sorted-nearest==)'; [ "$WITH_WNTR" = 1 ] || skip='^(#|$|ncls==|sorted-nearest==|wntr==)'; \
    grep -vE "$skip" /opt/ale/requirements-ext.txt > /opt/ale/requirements-ext-pypi.txt \
 && pip install --no-cache-dir --no-deps --only-binary=:all: -i https://mirrors.aliyun.com/pypi/simple/ \
        -r /opt/ale/requirements-ext-pypi.txt \
 && pip install --no-cache-dir --no-deps --no-index /opt/ale/native-wheels/*.whl \
 && pip check

COPY smoke_ext.py /opt/ale/smoke_ext.py
RUN WITH_WNTR=$WITH_WNTR python /opt/ale/smoke_ext.py && chmod -R a+rX /usr/local/lib/python3.12/site-packages
WORKDIR /work
