"""官方 sitemap、正文与通知内容；不依赖机器人运行环境。"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

SITEMAP_URL = "https://bot.q.qq.com/wiki/sitemap.xml"
DOC_PREFIX = "https://bot.q.qq.com/wiki/"
LEGACY_DOC_PREFIX = "https://bot.q.qq.com/wiki/develop/api-v2/"
_VOID_TAGS = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)
_BLOCK_TAGS = frozenset(
    [
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    ]
)
_IGNORED_TAGS = frozenset({"script", "style", "noscript", "svg", "nav", "footer"})
_IGNORED_CLASSES = frozenset(
    {"header-anchor", "sr-only", "copy-code-button", "page-edit", "page-nav"}
)


def normalize_inline(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_title(value: str) -> str:
    return normalize_inline(value).removesuffix(" | QQ 机器人官方文档").strip()


def normalize_content(value: str) -> str:
    # VuePress 的可访问性说明不属于文档正文；同时用于旧快照迁移。
    value = value.replace("(opens new window)", "")
    lines: list[str] = []
    for raw in value.splitlines():
        line = normalize_inline(raw)
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines)


def content_digest(title: str, content: str) -> str:
    return hashlib.sha256(f"{title}\0{content}".encode()).hexdigest()


def official_url(value: str, base: str = SITEMAP_URL) -> str | None:
    parsed = urlsplit(urljoin(base, value.strip()))
    if (
        parsed.scheme not in ("http", "https")
        or parsed.netloc.lower() != "bot.q.qq.com"
        or not parsed.path.startswith("/wiki/")
    ):
        return None
    return urlunsplit(("https", "bot.q.qq.com", parsed.path, "", ""))


@dataclass(slots=True)
class Sitemap:
    pages: dict[str, str] = field(default_factory=dict)
    indexes: list[str] = field(default_factory=list)


def parse_sitemap(xml_text: str, source: str = SITEMAP_URL) -> Sitemap:
    root = ElementTree.fromstring(xml_text)
    name = root.tag.rsplit("}", 1)[-1]
    if name not in ("urlset", "sitemapindex"):
        raise ValueError("响应不是 sitemap urlset/sitemapindex")
    result = Sitemap()
    item_name = "url" if name == "urlset" else "sitemap"
    for item in root:
        if item.tag.rsplit("}", 1)[-1] != item_name:
            continue
        values = {
            node.tag.rsplit("}", 1)[-1]: (node.text or "").strip() for node in item
        }
        url = official_url(values.get("loc", ""), source)
        if not url:
            if name == "sitemapindex":
                raise ValueError("sitemap 索引含站外地址，未使用不完整目录")
            continue
        if name == "sitemapindex":
            result.indexes.append(url)
        else:
            path = urlsplit(url).path
            # 官方目录也收录了 404 模板，它不是文档，不能反复作为抓取失败页。
            if path in ("/wiki/404.html", "/wiki/404/", "/wiki/404"):
                continue
            if not path.endswith((".xml", ".js", ".css", ".png", ".jpg", ".svg")):
                result.pages[url] = values.get("lastmod", "")
    return result


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list = field(default_factory=list)


class _HTMLTree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.nodes: list[_Node] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _classes(node: _Node) -> set[str]:
    return set((node.attrs.get("class") or "").split())


def _text(node: _Node, parts: list[str], resources: set[str], base: str) -> None:
    if node.tag in _IGNORED_TAGS or _classes(node) & _IGNORED_CLASSES:
        return
    if node.tag in _BLOCK_TAGS:
        parts.append("\n")
    resource = (
        node.attrs.get("href")
        if node.tag == "a"
        else node.attrs.get("src")
        if node.tag == "img"
        else None
    )
    if resource and not resource.startswith(("#", "javascript:", "data:")):
        resources.add(f"{node.tag}: {urljoin(base, resource)}")
    if node.tag == "img" and node.attrs.get("alt"):
        resources.add(f"img-alt: {node.attrs['alt']}")
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        else:
            _text(child, parts, resources, base)
    if node.tag in _BLOCK_TAGS:
        parts.append("\n")


@dataclass(slots=True)
class PageSnapshot:
    url: str
    lastmod: str
    title: str
    content: str
    resources: list[str]

    def to_dict(self) -> dict:
        return {
            "lastmod": self.lastmod,
            "title": self.title,
            "content": self.content,
            "digest": content_digest(self.title, self.content),
            "resources": self.resources,
            "missing_checks": 0,
        }


@dataclass(slots=True)
class PageChange:
    kind: str
    url: str
    title: str
    before: str = ""
    after: str = ""


def parse_page(url: str, lastmod: str, html: str) -> PageSnapshot:
    parser = _HTMLTree()
    parser.feed(html)
    parser.close()
    content_node = None
    for selector in (
        "theme-default-content",
        "vp-doc",
        "markdown-body",
        "content__default",
    ):
        content_node = next(
            (node for node in parser.nodes if selector in _classes(node)), None
        )
        if content_node:
            break
    if content_node is None:
        content_node = next(
            (node for node in parser.nodes if node.tag == "article"), None
        )
    if content_node is None:
        content_node = next((node for node in parser.nodes if node.tag == "main"), None)
    if content_node is None:
        raise ValueError("页面没有可识别的文档正文（可能是验证页或站点结构变化）")
    parts: list[str] = []
    resources: set[str] = set()
    _text(content_node, parts, resources, url)
    content = normalize_content("".join(parts))
    title_node = next((node for node in parser.nodes if node.tag == "title"), None)
    title_parts: list[str] = []
    if title_node:
        _text(title_node, title_parts, set(), url)
    title = normalize_title("".join(title_parts)) or content.split("\n", 1)[0]
    if not content or re.match(
        r"^(?:404\b|page not found\b|页面不存在|页面未找到)", title, re.I
    ):
        raise ValueError("文档正文为空或返回了软 404 页面")
    return PageSnapshot(url, lastmod, title, content, sorted(resources))


def compare_page(old: dict, snapshot: PageSnapshot) -> PageChange | None:
    current = snapshot.to_dict()
    text_changed = old.get("digest") != current["digest"]
    # 旧快照没有资源列表；第一次补齐，不将提取器升级误判为链接更新。
    resources_changed = "resources" in old and old["resources"] != current["resources"]
    if not text_changed and not resources_changed:
        return None
    before = f"{old.get('title', '')}\n{old.get('content', '')}"
    after = f"{snapshot.title}\n{snapshot.content}"
    if resources_changed:
        before += "\n链接与图片：\n" + "\n".join(old["resources"])
        after += "\n链接与图片：\n" + "\n".join(snapshot.resources)
    return PageChange("modified", snapshot.url, snapshot.title, before, after)


def _split_utf8(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size and size + len(line.encode()) > limit:
            chunks.append("".join(current).rstrip())
            current, size = [], 0
        for char in line:
            char_size = len(char.encode())
            if size + char_size > limit:
                chunks.append("".join(current).rstrip())
                current, size = [], 0
            current.append(char)
            size += char_size
    if current:
        chunks.append("".join(current).rstrip())
    return [chunk for chunk in chunks if chunk]


def build_notifications(changes: list[PageChange], max_bytes: int = 3500) -> list[str]:
    counts = Counter(change.kind for change in changes)
    names = {"added": "新增", "modified": "修改", "removed": "删除"}
    sections = [
        f"新增 {counts['added']} 页，修改 {counts['modified']} 页，删除 {counts['removed']} 页"
    ]
    for index, change in enumerate(changes, 1):
        section = f"{index}. [{names[change.kind]}] {change.title}\n{change.url}"
        sections.append(section)
    chunks = _split_utf8("\n\n".join(sections), max_bytes - 100)
    return [
        f"QQ 开放平台官方文档更新（{index}/{len(chunks)}）\n{chunk}"
        for index, chunk in enumerate(chunks, 1)
    ]


def links_only_legacy_message(message: str) -> str:
    """过滤旧版队列中的正文，保留原分段数量以兼容每个目标的发送进度。"""
    lines = message.splitlines()
    if not lines or not re.fullmatch(r"QQ 开放平台官方文档更新（\d+/\d+）", lines[0]):
        return message
    result = [lines[0]]
    for index, line in enumerate(lines[1:], 1):
        if re.fullmatch(r"新增 \d+ 页，修改 \d+ 页，删除 \d+ 页", line):
            result.append(line)
        elif re.match(r"^\d+\. \[(?:新增|修改|删除)\] ", line):
            result.extend(["", line])
        elif (
            line.startswith(DOC_PREFIX)
            and official_url(line) == line
            and (
                index == 1
                or re.match(r"^\d+\. \[(?:新增|修改|删除)\] ", lines[index - 1])
            )
        ):
            result.append(line)
    if len(result) == 1:
        result.append("文档已更新，请打开本批通知中的页面链接查看。")
    return "\n".join(result)
