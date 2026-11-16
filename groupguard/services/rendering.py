"""渲染并上传脱敏的违禁词列表。"""

import asyncio
import contextlib
import hashlib
import io
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


def mask_word(word: str) -> str:
    """脱敏: 只显示开头一个字, 其余以 *** 代替"""
    return (word[0] if word else "") + "***"


def _get_hosting():
    with contextlib.suppress(ImportError, AttributeError):
        from core.application import get_app

        app = get_app()
        if app and app.module_manager:
            return app.module_manager.get("image_hosting")
    return None


async def _upload_meme(image_data: bytes, name: str):
    """上传到 COS 桶 meme/ 目录, 返回 dict(file_url, px, ...) 或 None"""
    hosting = _get_hosting()
    if not hosting or not hosting.is_cos_available():
        return None
    fn = f"{name}_{hashlib.sha256(image_data).hexdigest()[:12]}.png"
    r = await hosting.upload_cos(image_data, fn, custom_path=f"meme/{fn}")
    return r if isinstance(r, dict) and r.get("file_url") else None


_font_cache: dict[tuple[int, bool], Any] = {}


def _font(size, bold=False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    names = ("msyhbd.ttc", "msyh.ttc") if bold else ("msyh.ttc",)
    paths = [
        base + n
        for n in names
        for base in ("/usr/share/fonts/truetype/", "C:/Windows/Fonts/")
    ]
    paths += [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in paths:
        with contextlib.suppress(OSError):
            f = ImageFont.truetype(path, size)
            _font_cache[key] = f
            return f
    f = ImageFont.load_default()
    _font_cache[key] = f
    return f


_S = 1
_MAX_RENDERED_WORDS = 300
_render_lock = asyncio.Lock()
_TEXT = (45, 52, 70)
_MUTED = (135, 144, 165)
_ACCENT = (205, 70, 70)
_CARD = (255, 255, 255, 240)
_COLS = 3  # 每行列数
_ROWS_PER_COL = 15  # 每列最多条数


def _render_sync(words: list) -> bytes:
    scale = _S
    n = len(words)
    cols = min(_COLS, (n + _ROWS_PER_COL - 1) // _ROWS_PER_COL)
    rows = (n + cols - 1) // cols

    pad = 26 * scale
    col_w = 220 * scale
    row_h = 44 * scale
    header_h = 96 * scale

    f_title = _font(28 * scale, bold=True)
    f_sub = _font(15 * scale)
    f_txt = _font(19 * scale)
    f_num = _font(17 * scale, bold=True)

    # 宽度自适应: 条目少时也不能窄于标题/副标题宽度, 避免文字被截断
    _m = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_w = int(_m.textlength("违禁词列表", font=f_title)) + 22 * scale
    sub_w = (
        int(
            _m.textlength(
                f"共 {n} 个 · 已脱敏, 删除请用编号: 违禁词删除 编号", font=f_sub
            )
        )
        + 22 * scale
    )
    width = max(pad + (col_w + pad) * cols, pad * 2 + max(title_w, sub_w))
    height = header_h + rows * row_h + pad + 40 * scale

    img = Image.new("RGB", (width, height), (250, 240, 240))
    d = ImageDraw.Draw(img, "RGBA")

    d.rounded_rectangle(
        (pad, 20 * scale, pad + 8 * scale, 20 * scale + 52 * scale), 4 * scale, fill=_ACCENT
    )
    d.text((pad + 22 * scale, 18 * scale), "违禁词列表", font=f_title, fill=_TEXT)
    d.text(
        (pad + 22 * scale, 60 * scale),
        f"共 {n} 个 · 已脱敏, 删除请用编号: 违禁词删除 编号",
        font=f_sub,
        fill=_MUTED,
    )

    for i, w in enumerate(words):
        c, r = divmod(i, rows)
        x0 = pad + (col_w + pad) * c
        y0 = header_h + r * row_h
        d.rounded_rectangle(
            (x0, y0, x0 + col_w, y0 + row_h - 8 * scale), 10 * scale, fill=_CARD
        )
        cy = y0 + (row_h - 8 * scale) // 2
        num = str(i + 1)
        nw = d.textlength(num, font=f_num)
        d.ellipse(
            (x0 + 12 * scale, cy - 13 * scale, x0 + 38 * scale, cy + 13 * scale), fill=_ACCENT + (36,)
        )
        d.text((x0 + 25 * scale - nw / 2, cy - 11 * scale), num, font=f_num, fill=_ACCENT)
        d.text((x0 + 50 * scale, cy - 13 * scale), mask_word(w), font=f_txt, fill=_TEXT)

    ft = "ElainaBot · 群管"
    fw = d.textlength(ft, font=f_sub)
    d.text(((width - fw) / 2, height - 30 * scale), ft, font=f_sub, fill=_MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=6)
    img.close()
    return buf.getvalue()


async def render_forbidden_list(words: list):
    """渲染脱敏违禁词列表图并上传 COS meme/, 返回 dict(file_url, px) 或 None (PIL 缺失/上传失败时回退文本)"""
    if not words or not PIL_AVAILABLE:
        return None
    try:
        async with _render_lock:
            png = await asyncio.to_thread(_render_sync, words[:_MAX_RENDERED_WORDS])
    except Exception:
        return None
    return await _upload_meme(png, "fw_list")
