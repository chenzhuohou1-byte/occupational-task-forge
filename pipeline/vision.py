"""agent 档 `view_image` 动作的图片处理（toolset=agent-file-vision，判分契约 §6.2）。

为什么要有：原协议只有纯文本动作，图片附件模型拿不到像素，只能经 run_python 算统计量。
「对照切片改类别」「渲染后对照上游外观」这类考点在旧考法下是 harness 能力缺口、不是难度
（契约 §6.3 校准附注 4）。这里把图片编成 OpenAI 兼容的 image_url 内容块交给模型看。

三条口径（都记进 measuredDifficulty.budget，换了就不可比）：
  · 长边上限 VISION_MAX_SIDE（默认 1024）：裁剪后再缩，模型看细节靠 crop，不靠大图；
  · 上下文里只留最近 VISION_KEEP 张（默认 3），更早的换成文字占位，防长任务撑爆上下文；
  · 只许看工作目录里的 input/、output/、_scratch/ —— 模型可以先用 run_python 画叠加图或调
    软件渲染到 _scratch/，再看结果。路径解析后必须仍在这三个目录内（挡 ../ 与符号链接逃逸）。
"""
import base64
import hashlib
import io
import os

MAX_SIDE = int(os.environ.get("ALE_N7_VISION_MAX_SIDE", "1024"))
KEEP = int(os.environ.get("ALE_N7_VISION_KEEP", "3"))
ROOTS = ("input", "output", "_scratch")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp")
# 原图像素上限：病理全尺寸 ROI 约 2–3 千万像素，留余量；再大按解压炸弹拒绝
MAX_SOURCE_PIXELS = int(os.environ.get("ALE_N7_VISION_MAX_PIXELS", str(200_000_000)))


def available():
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def is_image(name):
    return name.lower().endswith(IMAGE_EXTS)


def resolve(wd, path):
    """模型给的路径 → 绝对路径；不在允许目录内返回 (None, 原因)。"""
    p = str(path or "").strip().lstrip("/")
    if not p:
        return None, "view_image 需要 path"
    wd = os.path.realpath(wd)
    full = os.path.realpath(os.path.join(wd, p))
    for root in ROOTS:
        base = os.path.join(wd, root)
        if full == base or full.startswith(base + os.sep):
            if not os.path.isfile(full):
                return None, "没有这个文件：%s" % p
            return full, ""
    return None, "view_image 只能看 %s 下的文件（路径要带目录前缀，如 input/xxx.png）" % "/、".join(ROOTS)


def load(full, crop=None):
    """读图、按 crop=[x,y,w,h]（原图像素）裁剪、缩到长边 MAX_SIDE，返回 (编码字节, mime, 说明文字)。
    出错抛 ValueError，由调用方转成给模型的观察文本（看图失败是 agent 侧问题，不是测量故障）。"""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS
    try:
        im = Image.open(full)
        im.load()
    except Exception as e:  # noqa: BLE001 坏图/不支持的格式/解压炸弹都交给模型看原因
        raise ValueError("打不开这张图（%s: %s）。SVG 等矢量图请先用 run_python 渲染成 PNG 再看"
                         % (type(e).__name__, str(e)[:120]))
    W, H = im.size
    box = (0, 0, W, H)
    if crop is not None:
        try:
            x, y, w, h = [int(round(float(v))) for v in crop]
        except (TypeError, ValueError):
            raise ValueError("crop 要写成 [x, y, w, h]（原图像素，左上为原点）")
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + w), min(H, y + h)
        if x1 <= x0 or y1 <= y0:
            raise ValueError("crop 区域落在图外：原图 %dx%d，给的是 %s" % (W, H, list(crop)))
        box = (x0, y0, x1, y1)
        im = im.crop(box)
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, "white")
        rgba = im.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    cw, ch = im.size
    scale = min(1.0, float(MAX_SIDE) / max(cw, ch))
    if scale < 1.0:
        im = im.resize((max(1, int(cw * scale)), max(1, int(ch * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    data, mime = buf.getvalue(), "image/png"
    if len(data) > 1_500_000:   # 照片类大图 PNG 太胖，换 JPEG；像素数不变，token 不变
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=90)
        data, mime = buf.getvalue(), "image/jpeg"
    note = ("原图 %dx%d；本次区域 [x=%d, y=%d, w=%d, h=%d]；显示尺寸 %dx%d（缩放 %.4f，"
            "显示坐标 ÷ 缩放 + 区域左上角 = 原图坐标）"
            % (W, H, box[0], box[1], box[2] - box[0], box[3] - box[1], im.size[0], im.size[1], scale))
    return data, mime, note


def content_blocks(text, data, mime):
    url = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
    return [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": url}}]


def _is_image_block(b):
    return isinstance(b, dict) and b.get("type") == "image_url"


def prune(msgs, keep=None):
    """只留最近 keep 张图，其余图片块换成文字占位（就地改）。返回被移出的张数。"""
    keep = KEEP if keep is None else keep
    seen, dropped = 0, 0
    for m in reversed(msgs):
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for i in range(len(c) - 1, -1, -1):
            if _is_image_block(c[i]):
                seen += 1
                if seen > keep:
                    c[i] = {"type": "text", "text": "[这张图已移出上下文以节省长度；需要再看就重新 view_image]"}
                    dropped += 1
    return dropped


def sanitize(msgs):
    """落盘用的对话副本：图片 base64 换成摘要（sha256 前 16 位 + 字节数），轨迹文件不被撑大。"""
    out = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, list):
            nc = []
            for b in c:
                if _is_image_block(b):
                    url = b["image_url"].get("url", "")
                    raw = url.split(",", 1)[-1].encode("ascii", "ignore")
                    nc.append({"type": "image_url", "image_url": {
                        "url": "<image sha256:%s, %d base64 chars>"
                               % (hashlib.sha256(raw).hexdigest()[:16], len(raw))}})
                else:
                    nc.append(b)
            out.append(dict(m, content=nc))
        else:
            out.append(m)
    return out
