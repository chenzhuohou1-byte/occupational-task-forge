# 天文方向可执行范式题的考生/判分容器镜像（ALE_RUN_SANDBOX=docker 时用，
# 或 task_card.evaluation.engine.adapter_args.image=bench-astro:v1 的容器判分适配器）。
# 服务题目：H7b 光变曲线周期提取、H8b 坐标转换与可观测窗口、H9b 星系多版本测光一致性。
#
# 构建（在仓根）：
#   docker build -t bench-astro:v1 runtime/bench-astro
# tag 就是测量口径：改这个文件或 requirements.txt 就必须换 tag（v2、v3…），旧测量与新测量不可比、不回灌。
#
# 基础镜像钉 digest（多架构清单，arm64/amd64 都能用同一行）。docker.io 直连可能不通，走 daocloud 镜像站；
# 两者是同一份内容（digest 相同）。与 runtime/examinee/Dockerfile 用的是同一个 python:3.12-slim 基座。
FROM docker.m.daocloud.io/library/python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# 运行期不需要任何构建工具：requirements 里全部是预编译 manylinux 轮子（--only-binary）。
COPY requirements.txt /opt/ale/requirements.txt

# HTTPS 阿里 PyPI 镜像，正常证书校验（不加 --trusted-host）。
# --no-deps：装什么只由钉死的全量表决定；pip check 兜底保证依赖闭包完整。
RUN pip install --no-cache-dir --no-deps --only-binary=:all: \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        -r /opt/ale/requirements.txt \
 && pip check

# 冒烟：主力包都能 import（装上不等于能用）；额外校验 astropy 能在断网环境下用自带 IERS 数据
# 完成一次坐标转换（H8b 的核心依赖路径），避免「import 成功但功能跑不起来」的假阳性。
COPY smoke.py /opt/ale/smoke.py
RUN python /opt/ale/smoke.py

# 运行期由 agentsolve/判分适配器指定 --user 宿主 uid:gid、--read-only、--network none 等，
# 镜像里不设 USER：宿主 uid 事先未知，写死一个用户反而会让 output/ 里的文件归属不对。
RUN chmod -R a+rX /usr/local/lib/python3.12/site-packages /usr/local/bin
WORKDIR /work
