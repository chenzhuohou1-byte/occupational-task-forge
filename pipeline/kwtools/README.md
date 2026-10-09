# kwtools —— 造附件的写文件底座（仓内自带）

`attachments.py`（N4）用这几个模块把声明式附件规格落成真实文件。
**这份是框架自带的权威副本**：原来只在 `~/Desktop/KW数据产线/KW数据构造_通用工具`
那个仓外目录里找，于是 clone 下来的人造不出 xlsx/pdf/docx —— CI 当场报了这个错。

| 模块 | 产物 | 依赖 |
|---|---|---|
| `kw_csv.py` | csv | 标准库 |
| `kw_docx.py` | docx | **标准库**（zipfile + 手写 XML；原版依赖 macOS `textutil`，Linux 上没有） |
| `kw_xlsx.py` | xlsx | `openpyxl` |
| `kw_pdf.py` | pdf | `reportlab`（中文用其自带 `STSong-Light`，不需装字体） |
| `kw_pack.py` | 打包 | 标准库 |

```bash
python3 -m pip install openpyxl reportlab     # 只有 xlsx / pdf 需要
python3 pipeline/kwtools/kw_docx.py           # docx 自测（纯标准库）
```

依赖缺失不会抛异常：那一份附件进 `errors[]` 降级跳过，其余照常产出。

查找顺序：`KW_TOOLS_DIR`（显式覆盖）→ 本目录 → 仓外兄弟目录 → 旧默认路径。
**注意**：仓外那份 `kw_docx.py` 仍是 textutil 版，与本目录这份已分叉；框架以本目录为准。
