"""差异排版与 renderer 模块适配；PIL worker 可独立导入，不加载机器人上下文。"""

from __future__ import annotations

import asyncio
import base64
import difflib
import io
import struct
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from html import escape
from pathlib import Path

WIDTH = 1200
MARGIN = 32
ROW_HEIGHT = 32
CONTENT_X = 220
FOOTER_HEIGHT = 48
NAMES = {"added": "新增文档", "modified": "修改文档", "removed": "删除文档"}
COLORS = {"added": "#238636", "modified": "#1677df", "removed": "#cf3345"}
FONT_FAMILY = "QQDocsCustom, 'Microsoft YaHei', 'Noto Sans CJK SC', 'WenQuanYi Zen Hei', sans-serif"


@dataclass(slots=True)
class DiffRow:
    kind: str
    old: int | None
    new: int | None
    text: str


@dataclass(slots=True)
class DiffSection:
    kind: str
    title: str
    url: str
    rows: list[DiffRow]
    added: int
    removed: int


@dataclass(slots=True)
class LayoutPage:
    # 仅保存简单数据，PIL 子进程与 HTML 渲染共用同一份排版。
    commands: list[dict] = field(default_factory=list)
    height: int = 0


def build_sections(changes: list[dict], context: int = 2) -> list[DiffSection]:
    sections = []
    for change in changes:
        before, after = change["before"].splitlines(), change["after"].splitlines()
        rows: list[DiffRow] = []
        if change["kind"] in ("added", "removed"):
            added = change["kind"] == "added"
            for index, line in enumerate(after if added else before, 1):
                rows.append(
                    DiffRow(
                        "added" if added else "removed",
                        None if added else index,
                        index if added else None,
                        line,
                    )
                )
        else:
            matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
            for group_index, group in enumerate(matcher.get_grouped_opcodes(context)):
                if group_index:
                    rows.append(DiffRow("gap", None, None, "···"))
                for tag, i1, i2, j1, j2 in group:
                    if tag == "equal":
                        rows.extend(
                            DiffRow("context", i + 1, j1 + i - i1 + 1, before[i])
                            for i in range(i1, i2)
                        )
                    else:
                        if tag in ("delete", "replace"):
                            rows.extend(
                                DiffRow("removed", i + 1, None, before[i])
                                for i in range(i1, i2)
                            )
                        if tag in ("insert", "replace"):
                            rows.extend(
                                DiffRow("added", None, j + 1, after[j])
                                for j in range(j1, j2)
                            )
        counts = Counter(row.kind for row in rows)
        if not rows:
            rows.append(
                DiffRow("context", None, None, "页面状态发生变化，正文无可显示差异。")
            )
        sections.append(
            DiffSection(
                change["kind"],
                change["title"],
                change["url"],
                rows,
                counts["added"],
                counts["removed"],
            )
        )
    return sections


def _clean_text(value: str) -> str:
    return "".join(char for char in value.expandtabs(4) if ord(char) >= 32)


def _wrap(
    text: str, width: int, measure: Callable, size: int, bold: bool = False
) -> list[str]:
    text = _clean_text(text)
    if not text:
        return [""]
    lines = []
    while text:
        low, high = 1, len(text)
        while low <= high:
            middle = (low + high) // 2
            if measure(text[:middle], size, bold) <= width:
                low = middle + 1
            else:
                high = middle - 1
        count = max(1, high)
        lines.append(text[:count])
        text = text[count:]
    return lines


def layout_report(
    sections: list[DiffSection], created_at: str, max_height: int, measure: Callable
) -> list[LayoutPage]:
    """按实际字宽换行，跨图重复页头及文档标题，不截断变更行。"""
    if not sections:
        return []
    counts = Counter(section.kind for section in sections)
    try:
        timestamp = datetime.fromisoformat(created_at).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        timestamp = created_at[:32]
    pages: list[LayoutPage] = []
    page = LayoutPage()
    y = 0

    def rect(x: int, top: int, width: int, height: int, color: str):
        page.commands.append(
            {
                "type": "rect",
                "x": x,
                "y": top,
                "width": width,
                "height": height,
                "color": color,
            }
        )

    def text(
        x: int,
        top: int,
        value: str,
        size: int = 21,
        color: str = "#303841",
        bold: bool = False,
    ):
        page.commands.append(
            {
                "type": "text",
                "x": x,
                "y": top,
                "text": value,
                "size": size,
                "color": color,
                "bold": bold,
            }
        )

    def begin():
        nonlocal page, y
        page = LayoutPage()
        rect(0, 0, WIDTH, 66, "#20262d")
        text(MARGIN, 21, "QQ 开放平台 / 文档更新", 24, "#ffffff", True)
        text(
            WIDTH - MARGIN - int(measure(timestamp, 18, False)),
            25,
            timestamp,
            18,
            "#bec8d2",
        )
        text(MARGIN, 91, "本轮变更", 20, "#6c7785")
        for index, (kind, label, background) in enumerate(
            (
                ("added", "新增", "#def9e5"),
                ("modified", "修改", "#dff0ff"),
                ("removed", "删除", "#ffe5e8"),
            )
        ):
            x = 174 + index * 164
            rect(x, 85, 146, 34, background)
            text(x + 16, 91, f"{label} {counts[kind]}", 20, COLORS[kind], True)
        y = 140

    def finish():
        page.height = y + FOOTER_HEIGHT
        pages.append(page)

    def section_header(section: DiffSection, continued: bool = False):
        nonlocal y
        titles = _wrap(
            section.title + ("（续）" if continued else ""),
            WIDTH - MARGIN * 2 - 174,
            measure,
            30,
            True,
        )
        urls = _wrap(section.url, WIDTH - MARGIN * 2, measure, 18)
        required = 14 + 38 * len(titles) + 26 * len(urls) + 34 + 28
        if y + required + ROW_HEIGHT + FOOTER_HEIGHT > max_height:
            if y == 140:
                raise ValueError("文档标题或链接过长，无法放入单张差异图片")
            finish()
            begin()
        y += 14
        text(
            WIDTH - MARGIN - 138,
            y + 7,
            NAMES[section.kind],
            20,
            COLORS[section.kind],
            True,
        )
        for title in titles:
            text(MARGIN, y, title, 30, bold=True)
            y += 38
        for url in urls:
            text(MARGIN, y, url, 18, "#74808e")
            y += 26
        text(MARGIN, y + 3, f"+ {section.added} 行", 20, COLORS["added"], True)
        text(
            MARGIN + 160, y + 3, f"− {section.removed} 行", 20, COLORS["removed"], True
        )
        y += 34
        rect(MARGIN, y, WIDTH - MARGIN * 2, 28, "#e6ebf1")
        text(MARGIN + 22, y + 5, "原", 16, "#7d8995")
        text(MARGIN + 94, y + 5, "现", 16, "#7d8995")
        text(CONTENT_X, y + 5, "文档内容", 16, "#7d8995")
        y += 28

    begin()
    for section in sections:
        section_header(section)
        for row in section.rows:
            for line_index, line in enumerate(
                _wrap(row.text, WIDTH - MARGIN - CONTENT_X - 14, measure, 21)
            ):
                if y + ROW_HEIGHT + FOOTER_HEIGHT > max_height:
                    finish()
                    begin()
                    section_header(section, True)
                background, gutter, foreground, sign = {
                    "added": ("#e6ffed", "#c9f4d5", "#238636", "+"),
                    "removed": ("#ffebed", "#ffd6db", "#cf3345", "−"),
                    "context": ("#ffffff", "#f0f3f7", "#303841", ""),
                    "gap": ("#eaf0f6", "#e3eaf2", "#7d8995", ""),
                }[row.kind]
                rect(MARGIN, y, WIDTH - MARGIN * 2, ROW_HEIGHT, background)
                rect(MARGIN, y, 144, ROW_HEIGHT, gutter)
                if line_index == 0:
                    for column, number in enumerate((row.old, row.new)):
                        if number is not None:
                            value = str(number)
                            x = (
                                MARGIN
                                + (column + 1) * 72
                                - 12
                                - int(measure(value, 16, False))
                            )
                            text(x, y + 8, value, 16, "#7d8995")
                    text(MARGIN + 154, y + 4, sign, 21, foreground, True)
                text(CONTENT_X, y + 4, line, 21, foreground)
                y += ROW_HEIGHT
        y = min(y + 14, max_height - FOOTER_HEIGHT)
    finish()
    for index, page in enumerate(pages, 1):
        text(MARGIN, page.height - 29, "QQ 机器人官方文档 · 变更对比", 16, "#7d8995")
        label = f"{index}/{len(pages)}  ·  绿色新增 / 红色删除"
        text(
            WIDTH - MARGIN - int(measure(label, 16, False)),
            page.height - 29,
            label,
            16,
            "#7d8995",
        )
    return pages


@lru_cache(maxsize=8)
def find_font(font_path: str = "") -> str:
    if font_path:
        if not Path(font_path).is_file():
            raise RuntimeError(f"中文字体文件不存在：{font_path}")
        return font_path
    candidates = (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf",
        "/System/Library/Fonts/PingFang.ttc",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    for directory in (
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".local/share/fonts",
    ):
        if directory.is_dir():
            for candidate in sorted(directory.rglob("*")):
                name = candidate.name.lower()
                if candidate.suffix.lower() in (".ttf", ".ttc", ".otf") and any(
                    hint in name
                    for hint in ("cjk", "notosanssc", "sourcehan", "zenhei", "pingfang")
                ):
                    return str(candidate)
    raise RuntimeError("PIL 未找到中文字体，请安装 Noto CJK / 文泉驿或设置 font_path")


def render_report(
    changes: list[dict],
    created_at: str,
    max_height: int = 12000,
    context: int = 2,
    font_path: str = "",
) -> list[tuple[bytes, int, int]]:
    """renderer PIL 公开协议的模块级入口；仅接收、返回可序列化的数据。"""
    from PIL import Image, ImageDraw, ImageFont

    font_file = find_font(font_path)
    bold_file = (
        Path(font_file).with_name("msyhbd.ttc")
        if Path(font_file).name == "msyh.ttc"
        else Path(font_file)
    )
    if not bold_file.is_file():
        bold_file = Path(font_file)

    @lru_cache(maxsize=16)
    def font(size: int, bold: bool = False):
        return ImageFont.truetype(str(bold_file) if bold else font_file, size)

    def measure(value: str, size: int, bold: bool = False):
        return font(size, bold).getlength(value)

    pages = layout_report(
        build_sections(changes, context), created_at, max_height, measure
    )
    result = []
    for page in pages:
        with Image.new("RGB", (WIDTH, page.height), "#f5f7fa") as image:
            draw = ImageDraw.Draw(image)
            for command in page.commands:
                x, y = command["x"], command["y"]
                if command["type"] == "rect":
                    draw.rectangle(
                        (x, y, x + command["width"] - 1, y + command["height"] - 1),
                        fill=command["color"],
                    )
                else:
                    draw.text(
                        (x, y),
                        command["text"],
                        font=font(command["size"], command["bold"]),
                        fill=command["color"],
                        anchor="lt",
                    )
            output = io.BytesIO()
            image.save(output, format="PNG")
            result.append((output.getvalue(), WIDTH, page.height))
    return result


def _font_css(font_path: str) -> str:
    if not font_path:
        return ""
    font_file = find_font(font_path)
    data = base64.b64encode(Path(font_file).read_bytes()).decode("ascii")
    return (
        f"@font-face{{font-family:QQDocsCustom;src:url(data:font/ttf;base64,{data})}}"
    )


def _html(page: LayoutPage, font_css: str) -> str:
    elements = []
    for command in page.commands:
        style = f"position:absolute;left:{command['x']}px;top:{command['y']}px;"
        if command["type"] == "rect":
            style += f"width:{command['width']}px;height:{command['height']}px;background:{command['color']}"
            content = ""
        else:
            style += f"font-size:{command['size']}px;color:{command['color']};font-weight:{700 if command['bold'] else 400}"
            content = escape(command["text"])
        elements.append(f'<div style="{style}">{content}</div>')
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>'
        f"{font_css}body{{margin:0}}#report{{position:relative;width:{WIDTH}px;height:{page.height}px;background:#f5f7fa;"
        f"font-family:{FONT_FAMILY};font-kerning:none;line-height:1;white-space:pre}}"
        '</style><main id="report">' + "".join(elements) + "</main></html>"
    )


class DiffRenderer:
    def __init__(
        self,
        get_module: Callable,
        *,
        backend: str = "auto",
        max_height: int = 12000,
        context: int = 2,
        font_path: str = "",
        warning: Callable | None = None,
    ):
        self.get_module = get_module
        self.backend = backend
        self.max_height = max_height
        self.context = context
        self.font_path = font_path
        self.warning = warning
        self.last_backend = ""

    async def render(
        self, changes: list[dict], created_at: str
    ) -> list[tuple[bytes, int, int]]:
        renderer = self.get_module("renderer")
        if renderer is None:
            raise RuntimeError("请安装并启用 renderer 模块，差异图片将自动重试")
        failures = []
        for backend in (
            ("pil", "playwright") if self.backend == "auto" else (self.backend,)
        ):
            try:
                if backend == "pil":
                    if not renderer.pil_available():
                        raise RuntimeError("renderer 的 PIL 引擎未启用")
                    from modules.renderer.protocol import render_pil

                    images = await render_pil(
                        f"{__name__}:render_report",
                        changes,
                        created_at,
                        self.max_height,
                        self.context,
                        self.font_path,
                    )
                else:
                    if not renderer.playwright_available():
                        raise RuntimeError("renderer 的 Playwright 引擎未启用")
                    images = await self._playwright(
                        renderer.playwright, changes, created_at
                    )
                if not images:
                    raise RuntimeError("渲染器未返回图片")
                self.last_backend = backend
                return images
            except Exception as exc:
                failures.append(f"{backend}: {exc}")
                if self.warning:
                    self.warning(f"文档差异图片渲染失败 [{backend}]：{exc}")
        raise RuntimeError("；".join(failures))

    async def _playwright(
        self, engine, changes: list[dict], created_at: str
    ) -> list[tuple[bytes, int, int]]:
        sections = await asyncio.to_thread(build_sections, changes, self.context)
        font_css = await asyncio.to_thread(_font_css, self.font_path)
        characters = sorted(
            set(
                "0123456789 +−（续）"
                + "".join(
                    section.title
                    + section.url
                    + "".join(row.text for row in section.rows)
                    for section in sections
                )
            )
        )
        async with engine.new_page(viewport=(WIDTH, 800)) as page:
            # 先在共享浏览器中测量字体，再复用与 PIL 一致的排版和分图逻辑。
            await page.set_content(
                f"<meta charset='utf-8'><style>{font_css}</style>", wait_until="load"
            )
            await page.evaluate("() => document.fonts.ready")
            metrics = await page.evaluate(
                """async ({characters, family}) => {
                    await document.fonts.load(`21px ${family}`);
                    const ctx = document.createElement('canvas').getContext('2d');
                    ctx.fontKerning = 'none';
                    const result = {};
                    for (const size of [16, 18, 20, 21, 24, 30]) {
                        for (const bold of [false, true]) {
                            ctx.font = `${bold ? 700 : 400} ${size}px ${family}`;
                            result[`${size}:${bold ? 1 : 0}`] = Object.fromEntries(
                                characters.map(c => [c, ctx.measureText(c).width]));
                        }
                    }
                    return result;
                }""",
                {"characters": characters, "family": FONT_FAMILY},
            )

            def measure(value: str, size: int, bold: bool = False):
                widths = metrics[f"{size}:{int(bold)}"]
                return sum(widths.get(char, size) for char in value)

            pages = await asyncio.to_thread(
                layout_report, sections, created_at, self.max_height, measure
            )
            images = []
            for layout in pages:
                await page.set_content(_html(layout, font_css), wait_until="load")
                await page.evaluate("() => document.fonts.ready")
                image = await page.locator("#report").screenshot(type="png")
                images.append((image, *png_size(image)))
            return images


def png_size(image: bytes) -> tuple[int, int]:
    if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) < 24:
        raise ValueError("渲染器未返回有效 PNG 图片")
    return struct.unpack(">II", image[16:24])
