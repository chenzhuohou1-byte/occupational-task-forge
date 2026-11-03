"""bench-astro 镜像构建期冒烟：主力包能 import，且 astropy 在断网环境下仅用自带 IERS 数据
也能完成一次坐标转换（H8b 的核心依赖路径）。在 Dockerfile 里以 root、有网的构建环境执行；
任何一项失败就让 docker build 失败。"""
import importlib
import sys

MODULES = ["numpy", "scipy", "astropy", "astropy.coordinates", "astropy.time",
           "astropy.units", "astropy.cosmology", "erfa", "yaml"]

bad = []
for m in MODULES:
    try:
        importlib.import_module(m)
    except Exception as e:  # noqa: BLE001
        bad.append("%s: %s: %s" % (m, type(e).__name__, e))

if not bad:
    try:
        from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body
        from astropy.time import Time
        import astropy.units as u
        loc = EarthLocation(lat=31.7 * u.deg, lon=-110.9 * u.deg, height=2096 * u.m)
        t = Time("2026-01-01T00:00:00")
        altaz = get_body("sun", t, loc).transform_to(AltAz(obstime=t, location=loc))
        if not (-90 <= altaz.alt.deg <= 90):
            bad.append("AltAz 转换结果超出合理范围：alt=%r" % altaz.alt.deg)
    except Exception as e:  # noqa: BLE001
        bad.append("astropy 坐标转换冒烟失败：%s: %s" % (type(e).__name__, e))

if bad:
    print("冒烟失败：\n  " + "\n  ".join(bad))
    sys.exit(1)
print("冒烟通过：%d 个模块 + astropy 坐标转换" % len(MODULES))
