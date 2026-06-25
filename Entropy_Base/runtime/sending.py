"""统一发送: UMO 解析 + 官机 markdown 发图 (失败回退富媒体)。"""

from __future__ import annotations

import asyncio
import struct

from . import state
from .components import (
    At,
    AtAll,
    Image,
    MessageChain,
    MessageEventResult,
    Plain,
    Record,
)

log = state.log


# ---- UMO (统一会话标识): elaina:<appid>:group|c2c:<id> ----

def make_umo(event) -> str:
    appid = str(getattr(event, "appid", "") or "") or state.default_appid()
    gid = getattr(event, "group_id", "") or ""
    if gid:
        return f"elaina:{appid}:group:{gid}"
    uid = getattr(event, "user_id", "") or ""
    return f"elaina:{appid}:c2c:{uid}"


def parse_umo(umo: str):
    """返回 (appid, scene, target_id); 失败返回 (None, None, None)。"""
    if not umo or not isinstance(umo, str):
        return None, None, None
    parts = umo.split(":")
    if len(parts) >= 4 and parts[0] == "elaina":
        return parts[1], parts[2], ":".join(parts[3:])
    if len(parts) >= 3:  # 兼容 AstrBot 原生 UMO: <platform>:<MessageType>:<id>
        scene = "group" if "group" in parts[1].lower() else "c2c"
        return None, scene, parts[-1]
    return None, None, None


# ---- 图片尺寸 (官机 markdown 发图需 #wpx #hpx 与实际像素一致) ----

def _dims_from_bytes(data: bytes) -> tuple[int, int]:
    """读图片文件头取 (w, h), 不依赖 PIL; 支持 PNG/GIF/BMP/JPEG/WEBP。"""
    if not isinstance(data, bytes) or len(data) < 24:
        return 0, 0
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return int(w), int(h)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
            return int(w), int(h)
        if data[:2] == b"BM":
            w, h = struct.unpack("<ii", data[18:26])
            return int(w), abs(int(h))
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fmt = data[12:16]
            if fmt == b"VP8 ":
                w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
                h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
                return int(w), int(h)
            if fmt == b"VP8L":
                b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
                w = ((b1 & 0x3F) << 8 | b0) + 1
                h = ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1
                return int(w), int(h)
            if fmt == b"VP8X":
                w = (data[24] | data[25] << 8 | data[26] << 16) + 1
                h = (data[27] | data[28] << 8 | data[29] << 16) + 1
                return int(w), int(h)
        if data[:2] == b"\xff\xd8":  # JPEG: 扫描 SOF 段
            i, n = 2, len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return int(w), int(h)
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except Exception:
        pass
    return 0, 0


def _parse_size(size) -> tuple[int, int]:
    """兼容框架 get_image_size 返回 (dict 或 序列)。"""
    try:
        if isinstance(size, dict):
            return int(size.get("width") or 0), int(size.get("height") or 0)
        if isinstance(size, (tuple, list)) and len(size) >= 2:
            return int(size[0]), int(size[1])
    except Exception:
        pass
    return 0, 0


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _prepare_image(sender, image):
    """归一为 (bytes_or_url, w, h)。本地路径→字节; URL/bytes→原样。"""
    data = image
    if isinstance(image, str) and not image.startswith(("http://", "https://")):
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _read_bytes, image)
        except Exception as e:
            log.warning(f"[熵增基座] 读取本地图片失败: {e}")
            data = None
    w = h = 0
    if data is not None:
        try:
            w, h = _parse_size(await sender.get_image_size(data))
        except Exception:
            pass
        if (not w or not h) and isinstance(data, bytes):
            w, h = _dims_from_bytes(data)
    return data, w, h


# ---- 图床: image_host 选其一 -> (上传方法, 可用性检查, 是否需密钥) ----
_IMAGE_HOSTS = {
    "nature": ("upload_nature", "is_nature_available", False),
    "chatglm": ("upload_chatglm", "is_chatglm_available", False),
    "ukaka": ("upload_ukaka", "is_ukaka_available", False),
    "xingye": ("upload_xingye", "is_xingye_available", False),
    "cos": ("upload_cos", "is_cos_available", True),
    "bilibili": ("upload_bilibili", "is_bilibili_available", True),
    "qq_channel": ("upload_qq", "is_qq_available", True),
}


def _host_url(r):
    if isinstance(r, dict):
        return r.get("file_url")
    if isinstance(r, str) and r.startswith(("http://", "https://")):
        return r
    return None


async def _host_image(img_bytes: bytes):
    """按 image_host 上传到框架图床, 返回直链或 None。"""
    if not isinstance(img_bytes, bytes):
        return None
    hosting = state.get_module("image_hosting")
    if not hosting:
        log.warning("[熵增基座] 跳过 markdown 发图: 框架未加载「图床服务」模块")
        return None
    provider = (state._CONFIG.get("image_host") or "nature").strip()
    if provider not in _IMAGE_HOSTS:
        provider = "nature"
    method_name, avail_name, needs_secret = _IMAGE_HOSTS[provider]
    try:
        if not needs_secret:  # 免密钥图床: 既已选用, 框架未开则运行时开启
            avail = getattr(hosting, avail_name, None)
            if callable(avail) and not avail():
                cfg = getattr(hosting, "_cfg", None)
                if isinstance(cfg, dict):
                    cfg.setdefault(provider, {})["enabled"] = True
        method = getattr(hosting, method_name, None)
        if method is None:
            return None
        if provider == "cos":
            import time
            r = await method(img_bytes, f"entropy_{int(time.time() * 1000)}.png",
                             custom_path="entropy_base/")
        else:
            r = await method(img_bytes)
        url = _host_url(r)
        if url:
            log.info(f"[熵增基座] 图床[{provider}]上传成功: {url}")
        elif isinstance(r, tuple):
            log.warning(f"[熵增基座] 图床[{provider}]上传失败: {r[1] if len(r) > 1 else r}")
        return url
    except Exception as e:
        log.warning(f"[熵增基座] 图床[{provider}]上传失败: {e}")
    return None


_QQ_CONTENT_MAX = 4000   # QQ markdown/文本单条上限约 4096, 留余量
_CAPTION_MAX = 1024      # 图片说明超此长基本是「原始数据误当文案」


def _clip_text(content: str) -> str:
    if content and len(content) > _QQ_CONTENT_MAX:
        log.info(f"[熵增基座] 文本过长({len(content)}字)已截断")
        return content[:_QQ_CONTENT_MAX - 16] + "\n…(内容过长已截断)"
    return content


async def _send_text(sender, *, event=None, group_id=None, user_id=None, content, buttons=None):
    if not content:
        return False
    content = _clip_text(content)
    try:
        if event is not None:
            await sender.reply(event, content, buttons=buttons)
        elif group_id:
            await sender.send_to_group(group_id, content, buttons=buttons)
        elif user_id:
            await sender.send_to_user(user_id, content, buttons=buttons)
        else:
            return False
        return True
    except Exception as e:
        log.warning(f"[熵增基座] 文本发送失败: {e}")
        return False


async def _send_one_image(sender, *, event=None, group_id=None, user_id=None, image, content=""):
    """单图: 优先官机 markdown 直链, 失败回退 QQ 原生富媒体。"""
    data, w, h = await _prepare_image(sender, image)
    if data is None:
        if isinstance(image, str) and image.startswith(("http://", "https://")):
            data = image
        else:
            return False

    caption = content or ""
    if len(caption) > _CAPTION_MAX:
        log.info(f"[熵增基座] 图片说明过长({len(caption)}字)疑似原始数据, 已省略")
        caption = ""

    if state._CONFIG.get("image_as_markdown", True):
        url = data if (isinstance(data, str) and data.startswith(("http://", "https://"))) else None
        if url is None and isinstance(data, bytes):
            url = await _host_image(data)
        if url and w and h:
            md = f"{caption}\n![图片 #{w}px #{h}px]({url})".strip() if caption else f"![图片 #{w}px #{h}px]({url})"
            if await _send_text(sender, event=event, group_id=group_id, user_id=user_id, content=md):
                log.info(f"[熵增基座] markdown 发图 ({w}x{h})")
                return True
            log.info("[熵增基座] markdown 发图被拒, 回退富媒体")
        else:
            log.info(f"[熵增基座] 未走 markdown 发图 (url={bool(url)}, w={w}, h={h}), 回退富媒体")

    try:
        if event is not None:
            await sender.reply_image(event, data, caption)
        elif group_id:
            await sender.send_image("group", group_id, data, content=caption)
        elif user_id:
            await sender.send_image("user", user_id, data, content=caption)
        else:
            return False
        return True
    except Exception as e:
        log.warning(f"[熵增基座] 原生图片发送失败: {e}")
        return False


def _chain_items(chain):
    if isinstance(chain, MessageChain):
        return chain.chain
    return chain if isinstance(chain, list) else []


async def send_chain(sender, chain, *, event=None, group_id=None, user_id=None):
    """发送一条 MessageChain: 文本(含 @ 文本化)先发, 图片逐张发。"""
    if sender is None or chain is None:
        return
    if group_id and event is None and not state.is_full_access(str(group_id)):
        log.debug(f"[熵增基座] 群 {group_id} 未开全量, 跳过主动推送")
        return

    text_parts: list[str] = []
    images: list = []
    for c in _chain_items(chain):
        if isinstance(c, Plain):
            text_parts.append(c.text)
        elif isinstance(c, AtAll):
            text_parts.append("@全体成员 ")
        elif isinstance(c, At):
            uid = str(c.qq).strip() if c.qq is not None else ""
            if uid:  # 官机 @ 语法: <@用户id>
                text_parts.append(f"<@{uid}>")
            elif c.name:
                text_parts.append(f"@{c.name} ")
        elif isinstance(c, Image):
            images.append(c.bytes_ if hasattr(c, "bytes_") else c.file)
        elif isinstance(c, Record):
            images.append(None)

    text = "".join(text_parts).strip()
    if len(images) == 1 and images[0] is not None:  # 单图+文本: 文本作 caption 合并发
        await _send_one_image(sender, event=event, group_id=group_id, user_id=user_id,
                              image=images[0], content=text)
        return
    if text:
        await _send_text(sender, event=event, group_id=group_id, user_id=user_id, content=text)
    for img in images:
        if img is not None:
            await _send_one_image(sender, event=event, group_id=group_id, user_id=user_id, image=img)


async def send_result(sender, event, result):
    """处理被动 yield 的单个结果。"""
    if result is None:
        return
    if isinstance(result, MessageEventResult):
        await send_chain(sender, result.chain, event=event)
    elif isinstance(result, (MessageChain, list)):
        await send_chain(sender, result, event=event)
    elif isinstance(result, str):
        await _send_text(sender, event=event, content=result)
