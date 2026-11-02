# 生命科学考生镜像（3.2 基因组 / 3.3 细胞与成像）。见 docs/考生沙箱-docker.md §5。
#
# 构建（在仓根，先建好基础镜像 v1，它的 78 个包全钉死）：
#   docker build -t ale-examinee:py3.12-sci-v1 runtime/examinee
#   docker build -t ale-examinee:py3.12-bio-v2 runtime/examinee-bio
# 只在 v1 之上**加**包，v1 里已有的 78 个分发一个版本都不动（pip check 兜底）——
# 所以 v1 下的测量在 v2 里同样成立；但 tag 不同，measuredDifficulty.budget.runtime 仍按 tag 区分。
# 叠在 v1 上而不是另起一份：省掉重下 1 GB 轮子，且两者的公共部分必然一致。
FROM ale-examinee:py3.12-sci-v1

COPY requirements-bio.txt /opt/ale/requirements-bio.txt
RUN pip install --no-cache-dir --no-deps --only-binary=:all: \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        -r /opt/ale/requirements-bio.txt \
 && pip check

# 命令行生信工具走 Debian 包（apt 签名校验照常）：samtools/bcftools/tabix（htslib）、bwa、minimap2、seqkit 没有 deb 就不装
RUN apt-get update \
 && apt-get install -y --no-install-recommends samtools bcftools tabix bwa minimap2 \
 && rm -rf /var/lib/apt/lists/*

COPY smoke_bio.py /opt/ale/smoke_bio.py
RUN python /opt/ale/smoke_bio.py && chmod -R a+rX /usr/local/lib/python3.12/site-packages
WORKDIR /work
