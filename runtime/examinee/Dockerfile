# 考生 run_python 的容器镜像（ALE_RUN_SANDBOX=docker 时用）。见 docs/考生沙箱-docker.md。
#
# 构建（在仓根）：
#   docker build -t ale-examinee:py3.12-sci-v1 runtime/examinee
# tag 就是测量口径：agentsolve 把它记成 measuredDifficulty.budget.runtime = docker-py3.12-sci-v1。
# 改这个文件或 requirements.txt 就必须换 tag（v2、v3…），旧测量与新测量不可比、不回灌。
#
# 基础镜像钉 digest（多架构清单，arm64/amd64 都能用同一行）。docker.io 直连可能不通，走 daocloud 镜像站；
# 两者是同一份内容（digest 相同）。
FROM docker.m.daocloud.io/library/python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# 运行期不需要任何构建工具：requirements 里全部是预编译 manylinux 轮子（--only-binary），
# 原生依赖都由 auditwheel 打包在轮子里。刻意不跑 apt：外部源只有基础镜像站 + 阿里 PyPI 两处。
# 若以后某个轮子缺系统库，smoke.py 会让 build 失败，届时再显式加 apt 一步（并在文档里记来源）。
COPY requirements.txt /opt/ale/requirements.txt

# HTTPS 阿里 PyPI 镜像，正常证书校验（不加 --trusted-host）。
# --no-deps：装什么只由钉死的全量表决定；pip check 兜底保证依赖闭包完整。
RUN pip install --no-cache-dir --no-deps --only-binary=:all: \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        -r /opt/ale/requirements.txt \
 && pip check

# YoWASP（yosys 的 wasm 版）首次调用要把 wasm 编译成本机码（数十秒）。在镜像里预热到只读目录，
# 考生容器根文件系统只读、HOME 是一次性的草稿目录——不预热的话每次 run_python 都要重编。
ENV YOWASP_CACHE_DIR=/opt/yowasp-cache
RUN yowasp-yosys -V && chmod -R a+rX /opt/yowasp-cache

# 冒烟：主力包都能 import（装上不等于能用——缺系统库的轮子在这里就会炸）。
COPY smoke.py /opt/ale/smoke.py
RUN python /opt/ale/smoke.py

# 运行期由 agentsolve 指定 --user 宿主 uid:gid、--read-only、--network none 等，镜像里不设 USER：
# 宿主 uid 事先未知，写死一个用户反而会让 output/ 里的文件归属不对。这里只保证不以 root 跑时也都可读。
RUN chmod -R a+rX /usr/local/lib/python3.12/site-packages /usr/local/bin
WORKDIR /work
