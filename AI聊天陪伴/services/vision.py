"""将 QQ 临时图片转为各视觉接口均可接收的内联数据。"""

from __future__ import annotations

import base64
from urllib.parse import urljoin

import aiohttp

from .network_tools import SafeResolver, validate_url

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 15 * 1024 * 1024


def image_data_url(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("图片内容不是支持的 PNG、JPEG、GIF 或 WebP")
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


async def download_images(urls: list[str]) -> list[str]:
    if not urls:
        return []
    connector = aiohttp.TCPConnector(resolver=SafeResolver(), ttl_dns_cache=0)
    timeout = aiohttp.ClientTimeout(total=45, connect=8)
    result = []
    total_bytes = 0
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for url in urls:
            current = validate_url(url)
            for _ in range(4):
                async with session.get(current, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError("图片重定向缺少目标地址")
                        current = validate_url(urljoin(current, location))
                        continue
                    if response.status != 200:
                        raise ValueError(f"图片下载失败: HTTP {response.status}")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        total_bytes += len(chunk)
                        if len(data) > MAX_IMAGE_BYTES or total_bytes > MAX_TOTAL_BYTES:
                            raise ValueError("图片超过单张 10MB 或本轮总计 15MB 限制")
                    result.append(image_data_url(bytes(data)))
                    break
            else:
                raise ValueError("图片重定向次数过多")
    return result
