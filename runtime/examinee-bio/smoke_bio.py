"""生命科学镜像构建期冒烟：新加的包能 import、命令行工具能跑。任何一项失败就让 docker build 失败。"""
import importlib
import subprocess
import sys

MODULES = ["Bio", "Bio.SeqIO", "pysam", "anndata", "scanpy", "skimage", "tifffile", "h5py", "sklearn"]
bad = []
for m in MODULES:
    try:
        importlib.import_module(m)
    except Exception as e:  # noqa: BLE001
        bad.append("%s: %s: %s" % (m, type(e).__name__, e))
for argv in (["samtools", "--version"], ["bcftools", "--version"], ["bwa"], ["minimap2", "--version"]):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=60)
        out = (r.stdout + r.stderr).lower()
        if r.returncode not in (0, 1) or not out.strip():   # bwa 不带参数时打用法、退出码 1
            bad.append("%s 退出码 %d" % (argv[0], r.returncode))
    except Exception as e:  # noqa: BLE001
        bad.append("%s: %s" % (argv[0], e))
# scanpy 真跑一遍最常用的流水线（小矩阵），装上不等于能用
try:
    import numpy as np, anndata as ad, scanpy as sc
    a = ad.AnnData(np.random.default_rng(0).poisson(1.0, (60, 40)).astype("float32"))
    sc.pp.normalize_total(a); sc.pp.log1p(a); sc.pp.pca(a, n_comps=5); sc.pp.neighbors(a); sc.tl.umap(a)
except Exception as e:  # noqa: BLE001
    bad.append("scanpy 流水线: %s: %s" % (type(e).__name__, e))
if bad:
    print("冒烟失败：\n  " + "\n  ".join(bad))
    sys.exit(1)
print("冒烟通过：%d 个模块 + samtools/bcftools/bwa/minimap2 + scanpy 小流水线" % len(MODULES))
