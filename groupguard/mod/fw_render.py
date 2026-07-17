"""违禁词列表渲染 — PIL 绘制脱敏列表图 + COS 图床上传 (meme/)

违禁词本身不外发明文 (避免机器人消息因包含违禁词被平台判违规),
列表仅展示「编号 + 首字 + ***」, 删除时按编号操作。
"""

import io
import asyncio
import hashlib

from PIL import Image, ImageDraw, ImageFont


def mask_word(word: str) -> str:
    """脱敏: 只显示开头一个字, 其余以 *** 代替"""
    return (word[0] if word else '') + '***'


# ==================== 图床 (框架 image_hosting 模块, COS 渠道) ====================

def _get_hosting():
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref and _bot_manager_ref.module_manager:
            return _bot_manager_ref.module_manager.get('image_hosting')
    except Exception:
        pass
    return None


async def _upload_meme(image_data: bytes, name: str):
    """上传到 COS 桶 meme/ 目录, 返回 dict(file_url, px, ...) 或 None"""
    hosting = _get_hosting()
    if not hosting or not hosting.is_cos_available():
        return None
    fn = f"{name}_{hashlib.md5(image_data).hexdigest()[:8]}.png"
    r = await hosting.upload_cos(image_data, fn, custom_path=f"meme/{fn}")
    return r if isinstance(r, dict) and r.get('file_url') else None


# ==================== 字体 ====================

_font_cache = {}


def _font(size, bold=False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    names = ('msyhbd.ttc', 'msyh.ttc') if bold else ('msyh.ttc',)
    paths = [base + n for n in names
             for base in ('/usr/share/fonts/truetype/', 'C:/Windows/Fonts/')]
    paths += ['/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']
    for path in paths:
        try:
            f = ImageFont.truetype(path, size)
            _font_cache[key] = f
            return f
        except Exception:
            pass
    f = ImageFont.load_default()
    _font_cache[key] = f
    return f


# ==================== 渲染 ====================

_S = 2  # 2x 超采样
_TEXT = (45, 52, 70)
_MUTED = (135, 144, 165)
_ACCENT = (205, 70, 70)
_CARD = (255, 255, 255, 240)
_COLS = 3          # 每行列数
_ROWS_PER_COL = 15  # 每列最多条数


def _render_sync(words: list) -> bytes:
    S = _S
    n = len(words)
    cols = min(_COLS, (n + _ROWS_PER_COL - 1) // _ROWS_PER_COL)
    rows = (n + cols - 1) // cols

    pad = 26 * S
    col_w = 220 * S
    row_h = 44 * S
    header_h = 96 * S

    f_title = _font(28 * S, bold=True)
    f_sub = _font(15 * S)
    f_txt = _font(19 * S)
    f_num = _font(17 * S, bold=True)

    # 宽度自适应: 条目少时也不能窄于标题/副标题宽度, 避免文字被截断
    _m = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    title_w = int(_m.textlength('违禁词列表', font=f_title)) + 22 * S
    sub_w = int(_m.textlength(
        f'共 {n} 个 · 已脱敏, 删除请用编号: 违禁词删除 编号', font=f_sub)) + 22 * S
    W = max(pad + (col_w + pad) * cols, pad * 2 + max(title_w, sub_w))
    H = header_h + rows * row_h + pad + 40 * S

    img = Image.new('RGB', (W, H), (250, 240, 240))
    d = ImageDraw.Draw(img, 'RGBA')

    d.rounded_rectangle((pad, 20 * S, pad + 8 * S, 20 * S + 52 * S), 4 * S, fill=_ACCENT)
    d.text((pad + 22 * S, 18 * S), '违禁词列表', font=f_title, fill=_TEXT)
    d.text((pad + 22 * S, 60 * S), f'共 {n} 个 · 已脱敏, 删除请用编号: 违禁词删除 编号',
           font=f_sub, fill=_MUTED)

    for i, w in enumerate(words):
        c, r = divmod(i, rows)
        x0 = pad + (col_w + pad) * c
        y0 = header_h + r * row_h
        d.rounded_rectangle((x0, y0, x0 + col_w, y0 + row_h - 8 * S), 10 * S, fill=_CARD)
        cy = y0 + (row_h - 8 * S) // 2
        num = str(i + 1)
        nw = d.textlength(num, font=f_num)
        d.ellipse((x0 + 12 * S, cy - 13 * S, x0 + 38 * S, cy + 13 * S), fill=_ACCENT + (36,))
        d.text((x0 + 25 * S - nw / 2, cy - 11 * S), num, font=f_num, fill=_ACCENT)
        d.text((x0 + 50 * S, cy - 13 * S), mask_word(w), font=f_txt, fill=_TEXT)

    ft = 'ElainaBot · 群管'
    fw = d.textlength(ft, font=f_sub)
    d.text(((W - fw) / 2, H - 30 * S), ft, font=f_sub, fill=_MUTED)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    img.close()
    return buf.getvalue()


async def render_forbidden_list(words: list):
    """渲染脱敏违禁词列表图并上传 COS meme/, 返回 dict(file_url, px) 或 None"""
    if not words:
        return None
    png = await asyncio.to_thread(_render_sync, words)
    return await _upload_meme(png, 'fw_list')
