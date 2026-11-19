# 神经/生理信号方向可执行与数值题的考生/判分容器镜像
# （task_card.evaluation.engine.adapter_args.image=bench-neuro:v1 的容器判分适配器）。
# 服务题目：实验心理与神经科学 5.1/5.2（EEG/MEG、神经影像）、医疗健康 4.1（ECG/生理信号）等，
#           考生需真调 wfdb / mne / nibabel / nilearn 读取 PhysioNet/BIDS 原生数据并复算。
#
# 构建（在仓根）：
#   docker build -t bench-neuro:v1 runtime/bench-neuro
# tag 就是测量口径：改这个文件或 requirements.txt 就必须换 tag（v2、v3…），旧测量与新测量不可比、不回灌。
#
# 基础镜像钉 digest（多架构清单，arm64/amd64 同一行），与 bench-medical 同一份 3.11-slim。
# docker.io 直连可能不通，走 daocloud 镜像站；两者内容相同（digest 相同）。
FROM docker.m.daocloud.io/library/python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

# requirements.txt 是 python:3.11-slim 容器内真解一遍 `pip install wfdb mne nibabel nilearn
# numpy scipy pandas scikit-learn` 后 `pip freeze` 回填的全量钉死表（含全部传递依赖）。
COPY requirements.txt /opt/ale/requirements.txt

# --no-deps：装什么只由钉死的全量表决定；pip check 兜底保证依赖闭包完整。
# 预编译 manylinux 轮子（--only-binary）；HTTPS 阿里 PyPI 镜像，正常证书校验。
RUN pip install --no-cache-dir --no-deps --only-binary=:all: \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        -r /opt/ale/requirements.txt \
 && pip check

# 冒烟：主力包能 import 且真算一遍（读 WFDB 记录、建 MNE Raw、load NIfTI、nilearn 掩膜）。
COPY smoke.py /opt/ale/smoke.py
RUN python /opt/ale/smoke.py

# 运行期由 agentsolve/判分适配器指定 --user、--read-only、--network none 等，镜像里不设 USER。
RUN chmod -R a+rX /usr/local/lib/python3.11/site-packages /usr/local/bin
