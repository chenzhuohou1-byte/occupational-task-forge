"""扩展镜像构建期冒烟：新加的包能 import 并真跑一个最小用例。任何一项失败就让 docker build 失败。"""
import importlib.util
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
    assert pyhmmer.easel.Alphabet.amino().K == 20


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


for n, f in [("pyranges", t_pyranges), ("pyhmmer", t_pyhmmer), ("pydicom", t_pydicom), ("SimpleITK", t_sitk),
             ("lifelines", t_lifelines), ("rasterio", t_rasterio)] + ([("wntr", t_wntr)] if importlib.util.find_spec("wntr") else []):
    check(n, f)
if bad:
    print("冒烟失败：\n  " + "\n  ".join(bad))
    sys.exit(1)
print("冒烟通过：pyranges/pyhmmer/pydicom/SimpleITK/lifelines/rasterio" + ("/wntr" if importlib.util.find_spec("wntr") else "（wntr 未装）"))
