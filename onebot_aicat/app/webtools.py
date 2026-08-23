"""联网工具: web_search (百度/必应搜索) 与 fetch_url (抓取网页正文), 从 NapCat aicat web-tools 移植。"""

import re
import time
from urllib.parse import quote

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_BAIDU_RE = re.compile(
    r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?</h3>', re.DOTALL
)
_BING_RE = re.compile(
    r'<li class="b_algo"[^>]*>.*?<h2><a href="([^"]+)"[^>]*>([^<]+)</a></h2>.*?<p[^>]*>([^<]*)</p>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script[^>]*>[\s\S]*?</script>", re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>[\s\S]*?</style>", re.IGNORECASE)


async def _fetch(url: str) -> str:
    async with (
        aiohttp.ClientSession(timeout=_TIMEOUT) as session,
        session.get(url, headers=_HEADERS) as resp,
    ):
        return await resp.text(errors="replace")


async def _search_baidu(query: str, count: int) -> dict:
    try:
        html = await _fetch(f"https://www.baidu.com/s?wd={quote(query)}&rn={count}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    results = []
    for m in _BAIDU_RE.finditer(html):
        title = _TAG_RE.sub("", m.group(2)).strip()
        if title and m.group(1):
            results.append({"title": title, "url": m.group(1), "snippet": ""})
        if len(results) >= count:
            break
    return {
        "ok": True,
        "result": {"engine": "baidu", "query": query, "results": results},
    }


async def _search_bing(query: str, count: int) -> dict:
    try:
        html = await _fetch(
            f"https://www.bing.com/search?q={quote(query)}&count={count}"
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    results = []
    for m in _BING_RE.finditer(html):
        results.append(
            {
                "title": _TAG_RE.sub("", m.group(2)).strip(),
                "url": m.group(1),
                "snippet": m.group(3)[:200],
            }
        )
        if len(results) >= count:
            break
    return {
        "ok": True,
        "result": {"engine": "bing", "query": query, "results": results},
    }


async def web_search(query: str, engine: str = "auto", count: int = 5) -> dict:
    """搜索互联网; engine: auto(百度优先, 无结果退必应)/baidu/bing。"""
    if not query:
        return {"ok": False, "error": "缺少 query"}
    count = max(1, min(int(count or 5), 10))
    engine = (engine or "auto").lower()
    if engine == "baidu":
        return await _search_baidu(query, count)
    if engine == "bing":
        return await _search_bing(query, count)
    res = await _search_baidu(query, count)
    if res.get("ok") and (res.get("result") or {}).get("results"):
        return res
    return await _search_bing(query, count)


_NAPCAT_LLMS_URL = "https://napcat.apifox.cn/llms.txt"
_NAPCAT_DOC_URL_RE = re.compile(r"^https://napcat\.apifox\.cn/[A-Za-z0-9]+\.md$")
_NAPCAT_LINE_RE = re.compile(
    r"^(?:-\s*)?(?P<cat>[^\[]*)\[(?P<title>[^\]]+)\]\((?P<url>[^)]+)\)(?::\s*(?P<desc>.*))?$"
)
_napcat_index_cache = {"text": "", "time": 0.0}
_NAPCAT_CACHE_TTL = 3600


async def _napcat_index() -> str:
    """获取 llms.txt 接口目录 (服务端缓存 1 小时)。"""
    now = time.time()
    if (
        _napcat_index_cache["text"]
        and now - _napcat_index_cache["time"] < _NAPCAT_CACHE_TTL
    ):
        return _napcat_index_cache["text"]
    text = await _fetch(_NAPCAT_LLMS_URL)
    _napcat_index_cache["text"] = text
    _napcat_index_cache["time"] = now
    return text


async def search_napcat_apis(keyword: str, limit: int = 10) -> dict:
    """在 NapCat 接口目录 (llms.txt) 中按关键词搜索, 只返回匹配条目, 节省 token。"""
    if not keyword or not str(keyword).strip():
        return {"ok": False, "error": "缺少 keyword"}
    limit = max(1, min(int(limit or 10), 30))
    try:
        text = await _napcat_index()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"获取接口目录失败: {type(e).__name__}: {e}"}
    kws = [k.lower() for k in str(keyword).split() if k.strip()]
    results = []
    for line in text.splitlines():
        line = line.strip()
        m = _NAPCAT_LINE_RE.match(line)
        if not m:
            continue
        hay = line.lower()
        if not all(k in hay for k in kws):
            continue
        results.append(
            {
                "category": m.group("cat").strip(" -"),
                "title": m.group("title").strip(),
                "url": m.group("url").strip(),
                "description": (m.group("desc") or "").strip(),
            }
        )
        if len(results) >= limit:
            break
    if not results:
        return {
            "ok": True,
            "result": [],
            "count": 0,
            "message": f'目录中没有匹配 "{keyword}" 的接口, 可换关键词重试',
        }
    return {"ok": True, "result": results, "count": len(results)}


async def get_napcat_api_doc(url: str, max_length: int = 6000) -> dict:
    """抓取 NapCat 接口文档页 (search_napcat_apis 返回的 url), 返回 markdown 原文。"""
    url = str(url or "").strip()
    if not _NAPCAT_DOC_URL_RE.match(url):
        return {
            "ok": False,
            "error": "url 需为 search_napcat_apis 返回的 https://napcat.apifox.cn/xxx.md 链接",
        }
    max_length = max(500, min(int(max_length or 6000), 20000))
    try:
        text = await _fetch(url)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    text = text.strip()
    if len(text) > max_length:
        text = text[:max_length] + "\n...(已截断)"
    return {"ok": True, "result": {"url": url, "content": text}}


async def fetch_url(url: str, max_length: int = 2000) -> dict:
    """抓取网页, 返回标题与去标签后的正文文本。"""
    if not url or not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "url 需以 http(s):// 开头"}
    max_length = max(100, min(int(max_length or 2000), 20000))
    try:
        html = await _fetch(url)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    m = _TITLE_RE.search(html)
    title = m.group(1).strip() if m else ""
    html = _STYLE_RE.sub("", _SCRIPT_RE.sub("", html))
    text = re.sub(r"\s+", " ", _TAG_RE.sub(" ", html)).strip()
    if len(text) > max_length:
        text = text[:max_length] + "..."
    return {"ok": True, "result": {"url": url, "title": title, "content": text}}
