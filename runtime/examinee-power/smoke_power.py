"""电力系统镜像构建期冒烟：OpenDSS 真解一次三相电路，未收敛就让 docker build 失败。"""
import sys

import opendssdirect as dss

dss.Text.Command("Clear")
dss.Text.Command("New Circuit.smoke basekv=12.66 pu=1 phases=3")
dss.Text.Command("New Load.test bus1=sourcebus phases=3 kv=12.66 kw=100 kvar=30")
dss.Solution.Solve()
if not dss.Solution.Converged():
    sys.exit("冒烟失败：OpenDSS 三相电路未收敛")
print("冒烟通过：%s" % dss.Basic.Version())
