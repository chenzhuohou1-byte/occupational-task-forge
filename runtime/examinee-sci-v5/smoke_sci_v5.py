"""py3.12-sci-v5 构建期冒烟：本层新加的包能 import、版本与表一致，几样带原生库的包真跑一下，cellpose 权重在位。
在 Dockerfile 里以 root、有网的构建环境执行；任何一项失败就让 docker build 失败。
bio-v2 的包（Bio、pysam、scanpy、skimage…）和命令行工具已由下层的 smoke_bio.py 检查，这里只查本层新增的部分。
"""
import importlib
import importlib.metadata as metadata
import os
import re
import sys
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS = os.path.join(HERE, "requirements-sci-v5.txt")
WEIGHTS = "cpsam"
WEIGHTS_BYTES = 1233587898

MODULES = ["cellpose", "cellpose.models", "torch", "torchvision", "cv2", "fastremap", "fill_voids",
           "imagecodecs", "roifile", "segment_anything", "trackpy", "pims", "squidpy", "igraph",
           "leidenalg", "dask", "dask_image"]

bad = []
for m in MODULES:
    try:
        importlib.import_module(m)
    except Exception as e:  # noqa: BLE001
        bad.append("%s: %s: %s" % (m, type(e).__name__, e))

# 装上的版本必须与表逐个一致：镜像源换了构建产物或漏装，在这里就会发现。
for line in open(REQUIREMENTS, encoding="utf-8"):
    line = line.split("#", 1)[0].strip()
    if not line:
        continue
    name, pinned = line.split("==")
    try:
        got = metadata.version(name)
    except metadata.PackageNotFoundError:
        bad.append("%s 未安装" % name)
        continue
    if got != pinned:
        bad.append("%s 版本 %s ≠ 表 %s" % (name, got, pinned))

try:
    import numpy as np
    import torch
    if not torch.__version__.endswith("+cpu") or torch.cuda.is_available():
        bad.append("torch 应为 CPU 轮子：%s" % torch.__version__)
    x = torch.arange(6, dtype=torch.float32).reshape(2, 3)
    if float((x @ x.T).sum()) != 83.0:   # [[5, 14], [14, 50]]
        bad.append("torch 矩阵乘结果不符")
    import cv2
    img = np.zeros((32, 32), np.uint8)
    img[10:20, 12:22] = 255
    n, _ = cv2.connectedComponents(img)
    if n != 2:
        bad.append("cv2 连通域数 %d ≠ 2" % n)
    # scanpy 的 Leiden 两种后端都真跑一遍（v1/bio-v2 里没有 igraph、leidenalg）
    import anndata as ad
    import scanpy as sc
    rng = np.random.default_rng(5)
    counts = rng.poisson(1, (90, 30)).astype("float32")
    for g, rows in enumerate(np.array_split(np.arange(90), 3)):
        counts[rows, g * 10:(g + 1) * 10] += rng.poisson(6, (len(rows), 10))
    a = ad.AnnData(counts)
    sc.pp.normalize_total(a)
    sc.pp.log1p(a)
    sc.pp.pca(a, n_comps=8, random_state=5)
    sc.pp.neighbors(a, n_neighbors=10, random_state=5)
    for flavor in ("leidenalg", "igraph"):
        with warnings.catch_warnings():   # flavor=leidenalg 会提示改用 igraph，这里就是要两种都跑
            warnings.simplefilter("ignore", UserWarning)
            sc.tl.leiden(a, flavor=flavor, directed=False, n_iterations=2, random_state=5,
                         key_added="leiden_" + flavor)
        if not 1 < a.obs["leiden_" + flavor].nunique() < a.n_obs:
            bad.append("Leiden(%s) 分群数异常" % flavor)
except Exception as e:  # noqa: BLE001
    bad.append("功能检查: %s: %s" % (type(e).__name__, e))

# cellpose 只从本地目录取权重：容器 --network none，缺文件就会去 huggingface 下载而失败。
models_dir = os.environ.get("CELLPOSE_LOCAL_MODELS_PATH", "")
path = os.path.join(models_dir, WEIGHTS)
if not models_dir or not os.path.isfile(path) or os.path.getsize(path) != WEIGHTS_BYTES:
    bad.append("cellpose 权重缺失或大小不符：%s" % path)
else:
    from cellpose import models
    if str(getattr(models, "MODEL_DIR", "")) != models_dir:
        bad.append("cellpose 未读 CELLPOSE_LOCAL_MODELS_PATH：%s" % getattr(models, "MODEL_DIR", None))

if bad:
    print("冒烟失败：\n  " + "\n  ".join(bad))
    sys.exit(1)
print("冒烟通过：%d 个模块、%d 个钉版分发、torch CPU、cv2、Leiden(leidenalg/igraph)、cellpose 权重"
      % (len(MODULES), sum(1 for ln in open(REQUIREMENTS, encoding="utf-8") if re.match(r"^[^#\s]", ln))))
