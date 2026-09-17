"""QQ 开放平台文档变更检测核心。"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag
from xml.etree import ElementTree

import httpx

SITEMAP_URL = 'https://bot.q.qq.com/wiki/sitemap.xml'
DOC_PREFIX = 'https://bot.q.qq.com/wiki/develop/api-v2/'
STATE_VERSION = 1
_BLOCK_TAGS = frozenset(
    'address article aside blockquote br dd div dl dt figcaption figure footer '
    'h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tbody td '
    'tfoot th thead tr ul'.split()
)
_IGNORED_TAGS = frozenset({'script', 'style', 'noscript', 'svg'})
_VOID_TAGS = frozenset('area base br col embed hr img input link meta param source track wbr'.split())


@dataclass(slots=True)
class PageSnapshot:
    url: str
    lastmod: str
    title: str
    content: str
    digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            'lastmod': self.lastmod,
            'title': self.title,
            'content': self.content,
            'digest': self.digest,
        }


@dataclass(slots=True)
class PageChange:
    kind: str
    url: str
    title: str
    before: str = ''
    after: str = ''


@dataclass(slots=True)
class CheckResult:
    initialized: bool
    changes: list[PageChange]
    page_count: int
    failed_urls: list[str]
    failure_details: dict[str, str] = field(default_factory=dict)


class _ContentParser(HTMLParser):
    """提取 VuePress 正文。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._content_depth = 0
        self._ignored_depth = 0
        self._parts: list[str] = []
        self._title_depth = 0
        self._title_parts: list[str] = []
        self._anchor_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        classes = set((attrs_map.get('class') or '').split())
        if not self._content_depth and 'theme-default-content' in classes:
            self._content_depth = 1
        elif self._content_depth and tag not in _VOID_TAGS:
            self._content_depth += 1

        if tag == 'title':
            self._title_depth += 1
        if not self._content_depth:
            return
        if tag in _IGNORED_TAGS:
            self._ignored_depth += 1
        if tag == 'a':
            self._anchor_depth += 1
        if not self._ignored_depth and tag in _BLOCK_TAGS:
            self._parts.append('\n')

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == 'title' and self._title_depth:
            self._title_depth -= 1
        if not self._content_depth:
            return
        if not self._ignored_depth and tag in _BLOCK_TAGS:
            self._parts.append('\n')
        if tag == 'a' and self._anchor_depth:
            self._anchor_depth -= 1
        if tag in _IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_parts.append(data)
        if not self._content_depth or self._ignored_depth:
            return
        if self._anchor_depth and data.strip() == '#':
            return
        self._parts.append(data)

    @property
    def title(self) -> str:
        title = _normalize_inline(' '.join(self._title_parts))
        return title.removesuffix(' | QQ 机器人官方文档').strip()

    @property
    def content(self) -> str:
        lines = []
        for raw_line in ''.join(self._parts).splitlines():
            line = _normalize_inline(raw_line)
            if line and (not lines or line != lines[-1]):
                lines.append(line)
        return '\n'.join(lines)


def _normalize_inline(value: str) -> str:
    return re.sub(r'\s+', ' ', value).strip()


def parse_sitemap(xml_text: str) -> dict[str, str]:
    root = ElementTree.fromstring(xml_text)
    pages: dict[str, str] = {}
    for node in root.findall('{http://www.sitemaps.org/schemas/sitemap/0.9}url'):
        loc = node.findtext('{http://www.sitemaps.org/schemas/sitemap/0.9}loc', '').strip()
        if not loc.startswith(DOC_PREFIX):
            continue
        url = urldefrag(loc)[0]
        lastmod = node.findtext('{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod', '').strip()
        pages[url] = lastmod
    return dict(sorted(pages.items()))


def parse_page(url: str, lastmod: str, html: str) -> PageSnapshot:
    parser = _ContentParser()
    parser.feed(html)
    content = parser.content
    if not content:
        raise ValueError(f'页面没有可识别的正文: {url}')
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    title = parser.title or url.rstrip('/').rsplit('/', 1)[-1]
    return PageSnapshot(url=url, lastmod=lastmod, title=title, content=content, digest=digest)


def build_notification(changes: list[PageChange]) -> str:
    modified = [change for change in changes if change.kind == 'modified']
    counts = Counter(change.kind for change in changes)
    kind_names = {'added': '新增', 'modified': '修改', 'removed': '删除'}
    summary_lines = [
        'QQ 开放平台官方文档检测到更新',
        '',
        f'新增 {counts["added"]} 页，修改 {counts["modified"]} 页，删除 {counts["removed"]} 页',
        '',
    ]
    for index, change in enumerate(changes, 1):
        summary_lines.extend(
            [
                f'{index}. [{kind_names[change.kind]}] {change.title}',
                change.url,
                '',
            ]
        )
    sections = ['\n'.join(summary_lines).rstrip()]

    for change in modified:
        diff = '\n'.join(
            difflib.unified_diff(
                change.before.splitlines(),
                change.after.splitlines(),
                fromfile='更新前',
                tofile='更新后',
                lineterm='',
                n=3,
            )
        ) or '正文规范化后没有文本差异。'
        header = f'页面内容对比：{change.title}\n{change.url}\n'
        sections.append(f'{header}```diff\n{diff}\n```')
    return '\n\n'.join(sections)


def load_json(path: str | Path) -> dict:
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_json(path: str | Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    fd, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_state(path: str | Path) -> dict:
    default = {'version': STATE_VERSION, 'initialized': False, 'pages': {}}
    data = load_json(path)
    pages = data.get('pages')
    valid_pages = isinstance(pages, dict) and all(
        isinstance(url, str) and isinstance(page, dict) for url, page in pages.items()
    )
    return data if data.get('version') == STATE_VERSION and valid_pages else default


class DocsMonitor:
    def __init__(
        self,
        state_path: str | Path,
        notify: Callable[[str], Awaitable[bool]],
        *,
        timeout: float = 30,
        concurrency: int = 8,
        client: httpx.AsyncClient | None = None,
    ):
        self.state_path = Path(state_path)
        self.notify = notify
        self.timeout = max(5.0, float(timeout))
        self.concurrency = max(1, int(concurrency))
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    async def check_once(self) -> CheckResult:
        async with self._lock:
            return await self._check_once_locked()

    async def _check_once_locked(self) -> CheckResult:
        state = await asyncio.to_thread(load_state, self.state_path)
        sitemap_text = await self._get_text(SITEMAP_URL)
        try:
            sitemap = parse_sitemap(sitemap_text)
        except Exception as exc:
            raise RuntimeError(f'{exc}; 原始响应：{sitemap_text}') from exc
        if not sitemap:
            raise RuntimeError('官方 sitemap 中没有开发文档页面')

        old_pages: dict[str, dict] = state['pages']
        initialized = bool(state.get('initialized'))
        current_urls = set(sitemap)
        removed_urls = sorted(set(old_pages) - current_urls)
        fetch_urls = sorted(
            url
            for url, lastmod in sitemap.items()
            if url not in old_pages
            or not old_pages[url].get('digest')
            or old_pages[url].get('lastmod') != lastmod
        )
        fetched, failed_urls, failure_details = await self._fetch_pages(fetch_urls, sitemap)

        next_pages = {url: dict(value) for url, value in old_pages.items() if url in current_urls}
        changes: list[PageChange] = []
        for url in fetch_urls:
            snapshot = fetched.get(url)
            if snapshot is None:
                if url not in next_pages and not initialized:
                    next_pages[url] = {
                        'lastmod': sitemap[url],
                        'title': url.rstrip('/').rsplit('/', 1)[-1],
                        'content': '',
                        'digest': '',
                    }
                continue
            old = old_pages.get(url)
            next_pages[url] = snapshot.to_dict()
            if not initialized or not old or not old.get('digest'):
                if initialized and old is None:
                    changes.append(PageChange('added', url, snapshot.title, after=snapshot.content))
                continue
            if old.get('digest') != snapshot.digest:
                changes.append(
                    PageChange(
                        'modified',
                        url,
                        snapshot.title,
                        before=str(old.get('content', '')),
                        after=snapshot.content,
                    )
                )

        if initialized:
            for url in removed_urls:
                old = old_pages[url]
                changes.append(
                    PageChange(
                        'removed',
                        url,
                        str(old.get('title') or url.rstrip('/').rsplit('/', 1)[-1]),
                        before=str(old.get('content', '')),
                    )
                )

        changes.sort(key=lambda item: (item.kind, item.url))
        next_state = {
            'version': STATE_VERSION,
            'initialized': True,
            'checked_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'pages': dict(sorted(next_pages.items())),
            'failure_details': failure_details,
        }

        if changes:
            message = build_notification(changes)
            if not await self.notify(message):
                raise RuntimeError('更新通知没有成功发送给所有通知目标')

        await asyncio.to_thread(save_json, self.state_path, next_state)
        return CheckResult(
            initialized=not initialized,
            changes=changes,
            page_count=len(sitemap),
            failed_urls=failed_urls,
            failure_details=failure_details,
        )

    async def _get_text(self, url: str) -> str:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.timeout,
                headers={'User-Agent': 'ElainaBot-QQDocsMonitor/1.0'},
            )
        response = await self._client.get(url)
        if response.is_error:
            raise RuntimeError(
                f'HTTP {response.status_code} {response.reason_phrase}; 原始响应：{response.text}'
            )
        return response.text

    async def _fetch_pages(
        self, urls: list[str], sitemap: dict[str, str]
    ) -> tuple[dict[str, PageSnapshot], list[str], dict[str, str]]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def fetch(url: str) -> tuple[str, PageSnapshot | Exception]:
            async with semaphore:
                try:
                    html = await self._get_text(url)
                    try:
                        return url, parse_page(url, sitemap[url], html)
                    except Exception as exc:
                        return url, RuntimeError(f'{exc}; 原始响应：{html}')
                except Exception as exc:
                    return url, exc

        results = await asyncio.gather(*(fetch(url) for url in urls))
        fetched = {url: result for url, result in results if isinstance(result, PageSnapshot)}
        failed = [url for url, result in results if isinstance(result, Exception)]
        details = {url: str(result) for url, result in results if isinstance(result, Exception)}
        return fetched, failed, details
