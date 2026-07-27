"""联网工具: web_search (百度/必应搜索) 与 fetch_url (抓取网页正文), 从 NapCat aicat web-tools 移植。"""

import re
from urllib.parse import quote

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

_BAIDU_RE = re.compile(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?</h3>', re.DOTALL)
_BING_RE = re.compile(
    r'<li class="b_algo"[^>]*>.*?<h2><a href="([^"]+)"[^>]*>([^<]+)</a></h2>.*?<p[^>]*>([^<]*)</p>',
    re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')
_TITLE_RE = re.compile(r'<title[^>]*>([^<]+)</title>', re.IGNORECASE)
_SCRIPT_RE = re.compile(r'<script[^>]*>[\s\S]*?</script>', re.IGNORECASE)
_STYLE_RE = re.compile(r'<style[^>]*>[\s\S]*?</style>', re.IGNORECASE)


async def _fetch(url: str) -> str:
    async with (aiohttp.ClientSession(timeout=_TIMEOUT) as session,
                session.get(url, headers=_HEADERS) as resp):
        return await resp.text(errors='replace')


async def _search_baidu(query: str, count: int) -> dict:
    try:
        html = await _fetch(f'https://www.baidu.com/s?wd={quote(query)}&rn={count}')
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    results = []
    for m in _BAIDU_RE.finditer(html):
        title = _TAG_RE.sub('', m.group(2)).strip()
        if title and m.group(1):
            results.append({'title': title, 'url': m.group(1), 'snippet': ''})
        if len(results) >= count:
            break
    return {'ok': True, 'result': {'engine': 'baidu', 'query': query, 'results': results}}


async def _search_bing(query: str, count: int) -> dict:
    try:
        html = await _fetch(f'https://www.bing.com/search?q={quote(query)}&count={count}')
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    results = []
    for m in _BING_RE.finditer(html):
        results.append({
            'title': _TAG_RE.sub('', m.group(2)).strip(),
            'url': m.group(1),
            'snippet': m.group(3)[:200],
        })
        if len(results) >= count:
            break
    return {'ok': True, 'result': {'engine': 'bing', 'query': query, 'results': results}}


async def web_search(query: str, engine: str = 'auto', count: int = 5) -> dict:
    """搜索互联网; engine: auto(百度优先, 无结果退必应)/baidu/bing。"""
    if not query:
        return {'ok': False, 'error': '缺少 query'}
    count = max(1, min(int(count or 5), 10))
    engine = (engine or 'auto').lower()
    if engine == 'baidu':
        return await _search_baidu(query, count)
    if engine == 'bing':
        return await _search_bing(query, count)
    res = await _search_baidu(query, count)
    if res.get('ok') and (res.get('result') or {}).get('results'):
        return res
    return await _search_bing(query, count)


async def fetch_url(url: str, max_length: int = 2000) -> dict:
    """抓取网页, 返回标题与去标签后的正文文本。"""
    if not url or not url.lower().startswith(('http://', 'https://')):
        return {'ok': False, 'error': 'url 需以 http(s):// 开头'}
    max_length = max(100, min(int(max_length or 2000), 20000))
    try:
        html = await _fetch(url)
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    m = _TITLE_RE.search(html)
    title = m.group(1).strip() if m else ''
    html = _STYLE_RE.sub('', _SCRIPT_RE.sub('', html))
    text = re.sub(r'\s+', ' ', _TAG_RE.sub(' ', html)).strip()
    if len(text) > max_length:
        text = text[:max_length] + '...'
    return {'ok': True, 'result': {'url': url, 'title': title, 'content': text}}
