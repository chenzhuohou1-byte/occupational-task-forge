"""考生镜像构建期冒烟：主力包能 import、几样带原生库/CLI 的软件能真跑一下。
在 Dockerfile 里以 root、有网的构建环境执行；任何一项失败就让 docker build 失败。"""
import importlib
import subprocess
import sys

MODULES = [
    "numpy", "pandas", "scipy", "openpyxl", "pypdf", "rdkit.Chem", "geopandas", "pyogrio", "pyproj",
    "shapely", "xarray", "netCDF4", "cftime", "sumolib", "traci", "sgp4.api", "skyfield.api",
    "mujoco", "pinocchio", "yourdfpy", "trimesh", "openap", "networkx", "lxml.etree", "yaml",
    "PIL.Image", "jsonschema", "rtree", "manifold3d",
]

bad = []
for m in MODULES:
    try:
        importlib.import_module(m)
    except Exception as e:  # noqa: BLE001
        bad.append("%s: %s: %s" % (m, type(e).__name__, e))

# 带 CLI 的：sumo 二进制随 eclipse-sumo 轮子装进 bin/
for argv in (["sumo", "--version"], ["netconvert", "--version"]):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            bad.append("%s 退出码 %d：%s" % (argv[0], r.returncode, (r.stderr or r.stdout)[:300]))
    except Exception as e:  # noqa: BLE001
        bad.append("%s: %s" % (argv[0], e))

if bad:
    print("冒烟失败：\n  " + "\n  ".join(bad))
    sys.exit(1)
print("冒烟通过：%d 个模块 + sumo/netconvert" % len(MODULES))
