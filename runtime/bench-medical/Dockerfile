# 医疗/心理/社科/量化方向可执行与数值题的考生/判分容器镜像
# （task_card.evaluation.engine.adapter_args.image=bench-medical:v1 的容器判分适配器）。
# 服务题目：H3 PhysioNet ECG、H6/H7/H8（医疗）、H2/H4/H5（心理神经）、
#           H9/H10（经济社科）、H1b/H2b/H3b（量化金融）共 12 道。
#
# 构建（在仓根）：
#   docker build -t bench-medical:v1 runtime/bench-medical
# tag 就是测量口径：改这个文件或 requirements.txt 就必须换 tag（v2、v3…），旧测量与新测量不可比、不回灌。
#
# 基础镜像钉 digest（多架构清单，arm64/amd64 同一行）。docker.io 直连可能不通，走 daocloud 镜像站；
# 两者是同一份内容（digest 相同）。这里用 3.11-slim（原镜像 PYTHON_VERSION=3.11.15），
# 与 runtime/examinee 的 3.12-slim 刻意不同，见 requirements.txt 的说明。
FROM docker.m.daocloud.io/library/python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

# 运行期不需要构建工具：requirements 里全部是预编译 manylinux 轮子（--only-binary）。
COPY requirements.txt /opt/ale/requirements.txt

# HTTPS 阿里 PyPI 镜像，正常证书校验（不加 --trusted-host）。
# --no-deps：装什么只由钉死的全量表决定；pip check 兜底保证依赖闭包完整。
RUN pip install --no-cache-dir --no-deps --only-binary=:all: \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        -r /opt/ale/requirements.txt \
 && pip check

# 冒烟：主力包都能 import，并真算一遍 pandas 分组 + statsmodels OLS + scipy 检验
# （装上不等于能用；matplotlib 要在无显示环境下用 Agg 后端，这里一并验）。
COPY smoke.py /opt/ale/smoke.py
RUN python /opt/ale/smoke.py

# 运行期由 agentsolve/判分适配器指定 --user 宿主 uid:gid、--read-only、--network none 等，
# 镜像里不设 USER：宿主 uid 事先未知，写死一个用户反而会让 output/ 里的文件归属不对。
RUN chmod -R a+rX /usr/local/lib/python3.11/site-packages /usr/local/bin
