"""扩展镜像构建期冒烟：新加的包能 import 并真跑一个最小用例。任何一项失败就让 docker build 失败。"""
import importlib.util
import os
import sys

bad = []


def check(name, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        bad.append("%s: %s: %s" % (name, type(e).__name__, e))


def t_pyranges():
    import pandas as pd
    import pyranges as pr
    a = pr.PyRanges(pd.DataFrame({"Chromosome": ["1", "1"], "Start": [0, 5], "End": [10, 20]}))
    assert len(a.merge()) == 1 and len(a.overlap(a)) == 2


def t_pyhmmer():
    import pyhmmer
    from pyhmmer.easel import Alphabet, TextMSA, TextSequence
    from pyhmmer.plan7 import Background, Builder
    abc = Alphabet.amino()
    seqs = [TextSequence(name=b"s%d" % i, sequence=s) for i, s in enumerate(
        ["MKVLAAGIVGLLLAQ", "MKVLSAGIVGLLLAQ", "MKILAAGIVGLMLAQ", "MKVLAAGLVGLLLSQ"])]
    hmm, _, _ = Builder(abc).build_msa(TextMSA(name=b"m", sequences=seqs).digitize(abc), Background(abc))
    targets = [TextSequence(name=b"hit", sequence="PPPPMKVLAAGIVGLLLAQPPPP").digitize(abc),
               TextSequence(name=b"miss", sequence="WWWWWWWWWWWWWWWWWWWWWWW").digitize(abc)]
    hits = next(iter(pyhmmer.hmmsearch([hmm], targets, cpus=1)))
    names = [h.name.decode() if isinstance(h.name, bytes) else h.name for h in hits if h.included]
    assert names == ["hit"], names


def t_pydicom():
    import pydicom
    ds = pydicom.Dataset()
    ds.PatientName = "x^y"
    assert str(ds.PatientName) == "x^y"


def t_sitk():
    import SimpleITK as sitk
    im = sitk.Image(4, 4, 2, sitk.sitkFloat32)
    assert im.GetSize() == (4, 4, 2)


def t_lifelines():
    from lifelines import KaplanMeierFitter
    k = KaplanMeierFitter().fit([1, 2, 3, 4, 5], [1, 1, 0, 1, 1])
    assert 0 < float(k.predict(3)) < 1


def t_rasterio():
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=4, height=4, count=1, dtype="float32") as dst:
            dst.write(np.ones((1, 4, 4), "float32"))
        with mf.open() as src:
            assert src.read(1).sum() == 16


def t_wntr():
    import wntr
    wn = wntr.network.WaterNetworkModel()
    wn.add_junction("J1", base_demand=0.001, elevation=0)
    wn.add_reservoir("R1", base_head=50)
    wn.add_pipe("P1", "R1", "J1", length=100, diameter=0.2, roughness=100)
    res = wntr.sim.EpanetSimulator(wn).run_sim()
    assert float(res.node["pressure"]["J1"].iloc[0]) > 0


# 只有 -nowntr 变体（Dockerfile 的 WITH_WNTR=0）才允许没有 wntr；正式 ext-v3 缺了它就让构建失败。
with_wntr = os.environ.get("WITH_WNTR", "1") == "1"
for n, f in [("pyranges", t_pyranges), ("pyhmmer", t_pyhmmer), ("pydicom", t_pydicom), ("SimpleITK", t_sitk),
             ("lifelines", t_lifelines), ("rasterio", t_rasterio)] + ([("wntr", t_wntr)] if with_wntr else []):
    check(n, f)
if not with_wntr and importlib.util.find_spec("wntr"):
    bad.append("wntr: WITH_WNTR=0 的变体里不该有 wntr")
if bad:
    print("冒烟失败：\n  " + "\n  ".join(bad))
    sys.exit(1)
print("冒烟通过：pyranges/pyhmmer/pydicom/SimpleITK/lifelines/rasterio" + ("/wntr" if with_wntr else "（-nowntr 变体，不含 wntr）"))
