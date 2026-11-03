# 扩展考生镜像（序列/基因组区间 pyranges、HMM pyhmmer、医学影像 pydicom/SimpleITK、生存分析 lifelines、栅格 rasterio、供水管网 wntr）。
#
# 构建（在仓根，先建好 v1 与 bio-v2）：
#   docker build -t ale-examinee:py3.12-sci-v1 runtime/examinee
#   docker build -t ale-examinee:py3.12-bio-v2 runtime/examinee-bio
#   docker build -t ale-examinee:py3.12-ext-v3 runtime/examinee-ext
# 只在 bio-v2 之上**加**包，已有分发一个版本都不动（pip check 兜底）。
# ncls、sorted_nearest 在 PyPI 上没有 aarch64 轮子：在一次性构建阶段用 gcc 编出轮子，最终镜像不带编译器。

FROM python:3.12-slim AS wheelbuilder
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ libc6-dev && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-cache-dir --no-deps -i https://mirrors.aliyun.com/pypi/simple/ -w /wheels \
        numpy==2.5.4 Cython ncls==0.0.71 sorted_nearest==0.0.42

FROM ale-examinee:py3.12-bio-v2
COPY --from=wheelbuilder /wheels/ncls-*.whl /wheels/sorted_nearest-*.whl /opt/ale/native-wheels/
COPY requirements-ext.txt /opt/ale/requirements-ext.txt
RUN grep -vE '^(ncls|sorted-nearest)==' /opt/ale/requirements-ext.txt > /opt/ale/requirements-ext-pypi.txt \
 && pip install --no-cache-dir --no-deps --only-binary=:all: -i https://mirrors.aliyun.com/pypi/simple/ \
        -r /opt/ale/requirements-ext-pypi.txt \
 && pip install --no-cache-dir --no-deps --no-index /opt/ale/native-wheels/*.whl \
 && pip check

COPY smoke_ext.py /opt/ale/smoke_ext.py
RUN python /opt/ale/smoke_ext.py && chmod -R a+rX /usr/local/lib/python3.12/site-packages
WORKDIR /work
