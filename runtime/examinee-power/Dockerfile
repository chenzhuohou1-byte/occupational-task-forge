# 电力系统考生镜像（配电网潮流 OpenDSS）。见 docs/考生沙箱-docker.md §8。
#
# 构建（在仓根，先建好基础镜像 v1）：
#   docker build -t ale-examinee:py3.12-sci-v1 runtime/examinee
#   docker build -t ale-examinee:py3.12-power-v4 runtime/examinee-power
# 只在 v1 之上**加**包，v1 里已有的分发一个版本都不动（pip check 兜底）。
# 叠在 v1 上而不是 ext-v3 上：ext-v3 的 wntr 在 arm64 上装不了，叠上去本机就建不出来。
FROM ale-examinee:py3.12-sci-v1

COPY requirements-power.txt /opt/ale/requirements-power.txt
RUN pip install --no-cache-dir --no-deps --only-binary=:all: \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        -r /opt/ale/requirements-power.txt \
 && pip check

COPY smoke_power.py /opt/ale/smoke_power.py
RUN python /opt/ale/smoke_power.py && chmod -R a+rX /usr/local/lib/python3.12/site-packages
WORKDIR /work
