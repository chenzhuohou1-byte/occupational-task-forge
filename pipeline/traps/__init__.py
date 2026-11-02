"""陷阱模板库：存取 pipeline/traps/*.md，供 build-case --trap-ref 复用。

不是机器契约——文件内格式自由（见 README.md），这里只做「发现 / 列目录 / 读全文」，
不解析、不校验陷阱条目的结构。陷阱是否真的让模型答错，照常走 N7 实测。
"""
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXCLUDE = {"README.md", "__init__.py"}


def _files():
    """返回 {名称(不带扩展名): 绝对路径}，按文件名排序。"""
    out = {}
    for fn in sorted(os.listdir(_HERE)):
        if fn in _EXCLUDE or not fn.endswith(".md"):
            continue
        out[fn[:-3]] = os.path.join(_HERE, fn)
    return out


def _titles(text):
    """抓文件里的 `## 标题` 清单，供 list 命令做摘要（不解析内容）。"""
    return [m.group(1).strip() for m in re.finditer(r"(?m)^##\s+(.+)$", text)]


def list_traps():
    """列出所有领域文件 + 每个文件里的陷阱标题清单。给 CLI / 人读。"""
    out = []
    for name, path in _files().items():
        with open(path, encoding="utf-8") as f:
            text = f.read()
        out.append({"名称": name, "文件": os.path.basename(path), "陷阱条目": _titles(text)})
    return out


def read_trap(name):
    """按名称（不带扩展名，等于文件名去掉 .md）读全文；找不到抛 KeyError 并列出可用名称。"""
    files = _files()
    key = name[:-3] if name.endswith(".md") else name
    if key not in files:
        raise KeyError("没有陷阱模板 %r。可用：%s" % (name, "、".join(sorted(files)) or "（空）"))
    with open(files[key], encoding="utf-8") as f:
        return f.read()


class InvalidTrapName(ValueError):
    """名称非法（路径穿越 / 空 / 带扩展名之外的怪字符），拒绝落盘。"""


def _safe_name(name):
    """名称只许「文字/数字/中文/连字符/下划线」，杜绝路径穿越（../、绝对路径、分隔符）。

    这是本模块唯一一处会落盘写文件的入口（write_trap），必须比 read_trap 更谨慎——
    read_trap 顶多读错文件，write_trap 写错文件是真的会改坏仓库。
    """
    key = name[:-3] if name.endswith(".md") else name
    key = key.strip()
    # _EXCLUDE 存的是带扩展名的文件名；这里比的是去掉扩展名后的 key，两种形式都要挡，
    # 否则 name="README" 会落成 README.md、把库说明覆盖掉。
    reserved = _EXCLUDE | {os.path.splitext(x)[0] for x in _EXCLUDE}
    if not key or os.sep in key or "/" in key or ".." in key or key in reserved:
        raise InvalidTrapName("模板名非法：%r（不许路径分隔符/空/../保留名 README、__init__）" % name)
    return key


def write_trap(name, text):
    """新建或覆盖一个陷阱模板文件。纯文本落盘，不解析、不校验内容结构（见模块说明）。

    返回 (名称, 是否为新建)。覆盖已有文件前不做额外确认——这是前端/CLI 的职责
    （调用前应先 read_trap 比对或提示用户），本函数只管「名字合法就写」。
    """
    key = _safe_name(name)
    path = os.path.join(_HERE, key + ".md")
    is_new = not os.path.isfile(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    return key, is_new
