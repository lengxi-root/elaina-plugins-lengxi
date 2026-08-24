"""受限联网工具：仅访问公网 HTTP(S)，阻止 SSRF 并隐藏 IP。"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin, urlsplit

import aiohttp

from . import safety

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索公开互联网，返回网页搜索结果摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "读取管理员域名白名单内的公开 HTTP(S) 网页正文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "公开网页 URL"}
                },
                "required": ["url"],
            },
        },
    },
]


class NetworkToolError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data):
        if not self._ignored and data.strip():
            self.parts.append(data.strip())


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global


def validate_url(url: str, allowed_domains: list[str] | None = None) -> str:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NetworkToolError("仅允许公开 HTTP(S) URL")
    if parsed.username or parsed.password:
        raise NetworkToolError("URL 不得包含登录凭据")
    host = parsed.hostname.rstrip(".").casefold()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise NetworkToolError("禁止访问本机或内网地址")
    if allowed_domains is not None and not any(
        host == domain or host.endswith(f".{domain}") for domain in allowed_domains
    ):
        raise NetworkToolError("目标域名不在联网白名单中")
    try:
        if not _is_public_ip(host):
            ipaddress.ip_address(host)
            raise NetworkToolError("禁止访问本机或内网地址")
    except ValueError:
        pass
    return parsed.geturl()


class SafeResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        loop = asyncio.get_running_loop()
        rows = await loop.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, family=family
        )
        resolved = []
        for family_value, _, proto, _, sockaddr in rows:
            address = sockaddr[0]
            if not _is_public_ip(address):
                raise OSError("目标解析到非公网地址")
            resolved.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": family_value,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not resolved:
            raise OSError("域名无法解析")
        return resolved

    async def close(self):
        return None


async def _download(
    url: str,
    allowed_domains: list[str],
    max_bytes: int = 1_000_000,
) -> tuple[str, str]:
    current = validate_url(url, allowed_domains)
    connector = aiohttp.TCPConnector(resolver=SafeResolver(), ttl_dns_cache=0)
    timeout = aiohttp.ClientTimeout(total=20, connect=8)
    headers = {"User-Agent": "ElainaBot-AICompanion/1.1"}
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, headers=headers
    ) as session:
        for _ in range(4):
            async with session.get(current, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    if not location:
                        raise NetworkToolError("重定向缺少目标地址")
                    current = validate_url(urljoin(current, location), allowed_domains)
                    continue
                if response.status < 200 or response.status >= 300:
                    raise NetworkToolError(f"网页返回 HTTP {response.status}")
                content_type = response.headers.get("Content-Type", "")
                raw = await response.content.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise NetworkToolError("网页内容过大")
                return raw.decode(
                    response.charset or "utf-8", errors="replace"
                ), content_type
    raise NetworkToolError("网页重定向次数过多")


def _plain_text(raw: str, content_type: str) -> str:
    if "html" not in content_type.casefold() and "<html" not in raw[:500].casefold():
        return safety.redact_ips(raw[:6000])
    parser = _TextExtractor()
    parser.feed(raw)
    text = "\n".join(parser.parts)
    text = re.sub(r"\n{3,}", "\n\n", html.unescape(text))
    return safety.redact_ips(text[:6000])


async def fetch_url(url: str, allowed_domains: list[str]) -> dict:
    if not allowed_domains:
        raise NetworkToolError("网页读取未配置域名白名单")
    raw, content_type = await _download(url, allowed_domains)
    return {"url": url, "content": _plain_text(raw, content_type)}


async def web_search(query: str) -> dict:
    query = str(query or "").strip()[:200]
    if not query:
        raise NetworkToolError("搜索关键词不能为空")
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    raw, content_type = await _download(url, ["duckduckgo.com"])
    return {"query": query, "results": _plain_text(raw, content_type)[:5000]}


async def run(name: str, arguments: dict, allowed_domains: list[str]) -> dict:
    try:
        if name == "web_search":
            return {"ok": True, **await web_search(arguments.get("query", ""))}
        if name == "fetch_url":
            return {
                "ok": True,
                **await fetch_url(arguments.get("url", ""), allowed_domains),
            }
        return {"ok": False, "error": "未知联网工具"}
    except (NetworkToolError, OSError, aiohttp.ClientError, TimeoutError) as error:
        return {"ok": False, "error": safety.redact_ips(str(error))}
