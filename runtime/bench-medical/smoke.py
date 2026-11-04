#!/usr/bin/env python3
"""bench-medical:v1 构建期冒烟：主力包能 import，且能真算出结果。

「装上」和「能用」是两件事：matplotlib 缺字体/无显示后端、statsmodels 与 numpy
ABI 不配、pyarrow 缺共享库，都会 import 成功但一跑就炸。所以这里不只 import，
还跑一遍本方向题目真正用到的最小计算链：
  pandas 读写+分组聚合 → statsmodels OLS 拟合 → scipy 假设检验 → sklearn 拟合
  → openpyxl 写 xlsx → pyarrow 往返 → matplotlib(Agg) 出图
任一步抛异常则构建失败，避免「镜像建成了、判分时才发现用不了」。
"""
import io
import sys

FAIL = []


def check(name, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 冒烟就是要把任何异常都记下来
        FAIL.append("%s: %s: %s" % (name, type(e).__name__, e))


def t_pandas():
    import pandas as pd
    df = pd.DataFrame({"g": ["a", "a", "b"], "x": [1.0, 3.0, 5.0]})
    assert abs(df.groupby("g")["x"].mean()["a"] - 2.0) < 1e-12
    assert pd.read_csv(io.StringIO("a,b\n1,2\n")).shape == (1, 2)


def t_numpy_scipy():
    import numpy as np
    from scipy import stats
    a, b = np.array([1.0, 2, 3, 4]), np.array([2.0, 3, 4, 6])
    assert np.isclose(np.corrcoef(a, b)[0, 1], 0.9827076, atol=1e-5)
    assert stats.ttest_rel(a, b).pvalue < 0.1


def t_statsmodels():
    import numpy as np
    import statsmodels.api as sm
    x = np.arange(10.0)
    y = 2.0 * x + 1.0
    r = sm.OLS(y, sm.add_constant(x)).fit()
    assert np.isclose(r.params[1], 2.0, atol=1e-9), r.params


def t_sklearn():
    from sklearn.linear_model import LinearRegression
    m = LinearRegression().fit([[0], [1], [2]], [1.0, 3.0, 5.0])
    assert abs(m.coef_[0] - 2.0) < 1e-9


def t_openpyxl():
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active["A1"] = 1.5
    buf = io.BytesIO()
    wb.save(buf)
    assert openpyxl.load_workbook(io.BytesIO(buf.getvalue())).active["A1"].value == 1.5


def t_pyarrow():
    import pandas as pd
    import pyarrow as pa
    df = pd.DataFrame({"x": [1, 2]})
    assert pa.Table.from_pandas(df).to_pandas().equals(df)


def t_matplotlib():
    import matplotlib
    matplotlib.use("Agg")           # 无显示环境必须先切后端
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    assert buf.getbuffer().nbytes > 0


def t_misc():
    import seaborn, yaml, dateutil, pytz, tzdata  # noqa: F401
    assert yaml.safe_load("a: 1") == {"a": 1}


for nm, f in [("pandas", t_pandas), ("numpy+scipy", t_numpy_scipy),
              ("statsmodels", t_statsmodels), ("sklearn", t_sklearn),
              ("openpyxl", t_openpyxl), ("pyarrow", t_pyarrow),
              ("matplotlib", t_matplotlib), ("seaborn/yaml/tz", t_misc)]:
    check(nm, f)

print("python", sys.version.split()[0])
if FAIL:
    print("冒烟失败：", *FAIL, sep="\n  ", file=sys.stderr)
    sys.exit(1)
print("bench-medical 冒烟通过：pandas/numpy/scipy/statsmodels/sklearn/openpyxl/pyarrow/matplotlib 均可真算")
