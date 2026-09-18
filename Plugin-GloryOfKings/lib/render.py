"""王者荣耀渲染 — 使用 necessary/resources 中的模板与 Elaina 主题"""

import os
import re
import sys
import json
import time
import base64
import asyncio
import tempfile
from urllib.parse import urlparse

import aiohttp

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RES = os.path.join(_BASE, "necessary", "resources")
_HTML_DIR = os.path.join(_RES, "html")
_render_sem = asyncio.Semaphore(2)
_remote_img_cache: dict[str, str | None] = {}
_REMOTE_IMAGE_KEYS = re.compile(
    r"(?:avatar|icon|cover|photo|image|img|hero|skin|rank|role|head|portrait)", re.I
)

# 截图前的固定沉降等待 (ms)。已改为"自适应等待图片/字体就绪" (_READY_JS),
_RENDER_SETTLE_MS = 80
# 自适应就绪条件: 所有 <img> 已 complete (含失败) 且字体已加载。
_READY_JS = (
    "() => { try {"
    " const imgs = Array.from(document.images || []);"
    " if (!imgs.every(i => i.complete)) return false;"
    " if (document.fonts && document.fonts.status !== 'loaded') return false;"
    " return true; } catch (e) { return true; } }"
)
# 自适应等待的上限 (ms), 防止个别外链图迟迟不返回时卡住整体渲染。
_READY_TIMEOUT_MS = 6000


# 固定菜单的图床直链缓存: key -> (url, w, h, expire_ts)。
_LINK_CACHE: dict[str, tuple[str, int, int, float]] = {}


def _cache_get(key: str):
    item = _LINK_CACHE.get(key)
    if not item:
        return None
    url, w, h, exp = item
    if time.time() >= exp:
        _LINK_CACHE.pop(key, None)
        return None
    return url, w, h


def _cache_put(key: str, url: str, w: int, h: int, ttl: int) -> None:
    if key and url and ttl > 0:
        _LINK_CACHE[key] = (url, w, h, time.time() + ttl)


def _cache_drop(key: str) -> None:
    """丢掉一条直链缓存 (直链失效时用, 下次重新渲染上传)"""
    if key:
        _LINK_CACHE.pop(key, None)


_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
    ".css": "text/css",
}

_lib_cache = ""
_resmap_cache: dict | None = None


def _get_module(name: str):
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref and _bot_manager_ref.module_manager:
            return _bot_manager_ref.module_manager.get(name)
    except Exception:
        pass
    return None


def _data_uri(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    mime = _MIME.get(ext, "application/octet-stream")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


_CSS_INLINE_CACHE: dict[str, str] = {}
_FONT_FACE_RE = re.compile(r"@font-face\s*\{[^}]*\}", re.S)
_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*['\"]?([^'\";]+)")


def _inline_css(css_path: str) -> str:
    """把 CSS 里的 ../font /../img 资源 url() 内联成 data URI, 仅保留 woff (Chromium 够用)"""
    cached = _CSS_INLINE_CACHE.get(css_path)
    if cached is not None:
        return cached
    with open(css_path, "r", encoding="utf-8") as f:
        css = f.read()
    # @font-face: url(../font/x.woff) format("woff"), url(../font/x.ttf) format("truetype")
    css = re.sub(
        r',\s*url\(\.\./font/[^)]+\)\s*format\(["\']truetype["\']\)', "", css)

    def repl(m):
        rel = m.group(1).strip("'\"")
        target = os.path.normpath(os.path.join(os.path.dirname(css_path), rel))
        if os.path.isfile(target):
            return f"url({_data_uri(target)})"
        return m.group(0)

    result = re.sub(r"url\((\.\./[^)]+)\)", repl, css)
    _CSS_INLINE_CACHE[css_path] = result
    return result


def _prune_css_fonts(css_text: str, page_text: str, seen: set) -> str:
    """剔除没人用的 @font-face 与跨 CSS 的重复字体。"""
    body = _FONT_FACE_RE.sub("", css_text)

    def repl(m):
        face = m.group(0)
        matched = _FONT_FAMILY_RE.search(face)
        name = matched.group(1).strip() if matched else ""
        if not name:
            return face
        if name in seen:
            return ""                      # 同页前面的 CSS 已提供该字体
        if name not in body and name not in page_text:
            return ""                      # 没人用
        seen.add(name)
        return face

    return _FONT_FACE_RE.sub(repl, css_text)


def _resmap() -> dict:
    """{ 'img/flag1.png': 'data:...', 'css/MyKingHomepage.css': 'data:...', ... } (缓存)"""
    global _resmap_cache
    if _resmap_cache is not None:
        return _resmap_cache
    m: dict[str, str] = {}
    d = os.path.join(_RES, "img")
    if os.path.isdir(d):
        for fn in os.listdir(d):
            fp = os.path.join(d, fn)
            if os.path.isfile(fp):
                m[f"img/{fn}"] = _data_uri(fp)
    # CSS 不在这里收录: 每个页面只链 1-2 个, 且要先按页面裁剪字体,
    _resmap_cache = m
    return m


def _lib() -> str:
    global _lib_cache
    if not _lib_cache:
        with open(os.path.join(_RES, "template-web.js"), "r", encoding="utf-8") as f:
            _lib_cache = f.read()
    return _lib_cache


def _load_template(name: str) -> str:
    with open(os.path.join(_HTML_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


def _esc_script(s: str) -> str:
    """JSON 内嵌 <script> 时避免 </script> 提前闭合标签。"""
    return s.replace("</", "<\\/")


def _remote_image_key(key: str, value: str) -> bool:
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return False
    if _REMOTE_IMAGE_KEYS.search(str(key)):
        return True
    return os.path.splitext(urlparse(value).path)[1].lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def _guess_image_mime(url: str) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return _MIME.get(ext, "image/jpeg")


# 内联远程图的尺寸上限。卡片里最大也就 300px 宽, 缩到 420 足够清晰;
_INLINE_MAX_WIDTH = 420
_INLINE_JPEG_QUALITY = 82


def _shrink_image(raw: bytes) -> tuple[bytes, str]:
    """大图缩到卡片尺寸再内联; PIL 不可用/解码失败就原样返回。"""
    if len(raw) <= 200 * 1024:
        return raw, ""
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as img:
            if img.width <= _INLINE_MAX_WIDTH:
                return raw, ""
            has_alpha = img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info)
            ratio = _INLINE_MAX_WIDTH / img.width
            size = (_INLINE_MAX_WIDTH, max(1, int(img.height * ratio)))
            out = io.BytesIO()
            if has_alpha:
                resized = img.convert("RGBA").resize(size, Image.LANCZOS)
                resized.save(out, format="PNG", optimize=True)
                return out.getvalue(), "image/png"
            resized = img.convert("RGB").resize(size, Image.LANCZOS)
            resized.save(out, format="JPEG", quality=_INLINE_JPEG_QUALITY, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return raw, ""


async def _fetch_remote_image(session, url: str):
    """抓一张远程图并转 data URI 写缓存。失败写 None (负缓存, 不再重试)。"""
    try:
        async with session.get(url, ssl=False) as resp:
            content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0]
            if resp.status != 200 or (content_type and not content_type.startswith("image/")):
                _remote_img_cache[url] = None
                return
            raw = await resp.read()
            if not raw or len(raw) > 6 * 1024 * 1024:
                _remote_img_cache[url] = None
                return
            raw, forced_mime = await asyncio.to_thread(_shrink_image, raw)
            mime = forced_mime or (content_type if content_type.startswith("image/")
                                   else _guess_image_mime(url))
            _remote_img_cache[url] = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception:
        _remote_img_cache[url] = None


# 并发抓图的上限。营地/官网 CDN 承受得住, 又不至于把带宽打满
_INLINE_CONCURRENCY = 8
# 单页最多内联多少张远程图 (超出部分不内联, 保留原 URL)
_INLINE_MAX_IMAGES = 60


async def _inline_remote_images(value, key: str = "", session=None):
    """把模板数据中的远程图片转成 data URI (避免截图浏览器加载外链失败/变慢)。"""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8),
            headers={"User-Agent": "Mozilla/5.0 ElainaBot renderer"},
        )

    def walk(node, k=""):
        """收集远程图 URL (按数据顺序去重)"""
        if _remote_image_key(k, node):
            if node not in seen:
                seen.add(node)
                urls.append(node)
            return
        if isinstance(node, dict):
            for kk, vv in node.items():
                walk(vv, str(kk))
        elif isinstance(node, (list, tuple)):
            for vv in node:
                walk(vv, k)

    def rebuild(node, k=""):
        if _remote_image_key(k, node):
            cached = _remote_img_cache.get(node)
            return cached if cached else node
        if isinstance(node, dict):
            return {kk: rebuild(vv, str(kk)) for kk, vv in node.items()}
        if isinstance(node, (list, tuple)):
            return [rebuild(vv, k) for vv in node]
        return node

    try:
        urls, seen = [], set()
        walk(value, key)
        pending = [u for u in urls if u not in _remote_img_cache]
        # 一页内联太多图会把 HTML 撑到十几 MB (set_content 直接卡住)。
        if len(pending) > _INLINE_MAX_IMAGES:
            pending = pending[:_INLINE_MAX_IMAGES]
        if pending:
            sem = asyncio.Semaphore(_INLINE_CONCURRENCY)

            async def guarded(url):
                async with sem:
                    await _fetch_remote_image(session, url)

            await asyncio.gather(*(guarded(u) for u in pending))
        return rebuild(value, key)
    finally:
        if own_session:
            await session.close()


# 模板里静态引用的资源 (css/xx.css / img/xx.png), 以及可能被动态拼接的前缀
_RES_REF_RE = re.compile(r"(css|img)/[\w.\-]+")
_RES_PREFIX_RE = re.compile(r"(css|img)/[\w.\-]*$")


def _select_resmap(tpl: str) -> dict:
    """只注入模板真正引用到的本地资源。"""
    full = _resmap()
    wanted: set = set()
    prefixes: set = set()
    for m in _RES_REF_RE.finditer(tpl):
        ref = m.group(0)
        tail = tpl[m.end():m.end() + 2]
        (prefixes if tail.startswith("{{") else wanted).add(ref)
    if not wanted and not prefixes:
        if "css/" in tpl:
            refs = sorted(os.listdir(os.path.join(_RES, "css")))
            wanted = {f"css/{fn}" for fn in refs}
        elif "img/" in tpl:
            return {k: v for k, v in full.items() if k.startswith("img/")}
        else:
            return {}

    selected = {}
    seen_fonts: set = set()
    for key, value in full.items():
        if key in wanted or any(key.startswith(p) for p in prefixes):
            selected[key] = value
    for key in [k for k in wanted if k.startswith("css/")]:
        entry = _css_entry(key, tpl, seen_fonts)
        if entry:
            selected[key] = entry
    return selected


def _css_entry(key: str, page_text: str, seen_fonts: set) -> str:
    """按页面裁剪字体后的 CSS data URI (字体 data URI 一张几 MB, 能省则省)。"""
    path = os.path.join(_RES, key)
    if not os.path.isfile(path):
        return ""
    text = _prune_css_fonts(_inline_css(path), page_text, seen_fonts)
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"data:text/css;base64,{b64}"


def build_page(template: str, data: dict) -> str:
    """组装自包含的宿主页面: art-template 渲染原版模板 + 内联资源 + 执行脚本。"""
    data = dict(data)
    # _res_path 设为哨兵, 渲染后由 RESMAP 替换为内联 data URI
    data["_res_path"] = "__RES__/"
    tpl = _load_template(template)
    resmap = _select_resmap(tpl)
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        "<script>" + _lib() + "</script>"
        "<script>\n"
        "var __TPL__ = " + _esc_script(json.dumps(tpl)) + ";\n"
        "var __DATA__ = " + _esc_script(json.dumps(data, ensure_ascii=False)) + ";\n"
        "var __RESMAP__ = " + _esc_script(json.dumps(resmap)) + ";\n"
        "var out = template.render(__TPL__, __DATA__);\n"
        # {{_res_path}}img/x.png -> __RES__/img/x.png ; CSS 内联 ../img /../font 同理
        "out = out.replace(/(?:__RES__\\/|\\.\\.\\/)(img|font|css)\\/[\\w.\\-]+/g,"
        " function(s){var k=s.replace('__RES__/','').replace('../','');"
        " return __RESMAP__[k]||s;});\n"
        "document.open();document.write(out);document.close();\n"
        # 统一底栏: 所有页面都带一行版权
        "(function(){\n"
        "  var COPY='Created By ElainaBot v2 & Plugin-GloryOfKings';\n"
        "  var st=document.createElement('style');\n"
        "  st.textContent='.elaina-copyright{display:block;text-align:center;font-size:12px;letter-spacing:.04em;color:rgba(35,43,54,.55);margin-top:6px;width:100%;grid-column:1/-1;column-span:all}';\n"
        "  document.head.appendChild(st);\n"
        "  var el=document.createElement('span'); el.className='elaina-copyright'; el.textContent=COPY;\n"
        "  var foot=document.querySelector('.footer');\n"
        "  if(foot){foot.appendChild(el);}\n"
        "  else{\n"
        # 页面还有背景层(主页这种没删背景的)就把版权放回背景上;
        # 背景已删掉的页面(面板就是最底层)才放进最外面那块面板里
        "    var bc=getComputedStyle(document.body);\n"
        "    var bodyBg=(bc.backgroundColor&&bc.backgroundColor!=='rgba(0, 0, 0, 0)')||bc.backgroundImage!=='none';\n"
        "    var bodyPad=(parseFloat(bc.paddingTop)||0)+(parseFloat(bc.paddingBottom)||0);\n"
        "    if(bodyBg||bodyPad>0){ el.style.marginTop='14px'; document.body.appendChild(el); return; }\n"
        "    var last=null, kids=document.body.children;\n"
        "    for(var i=0;i<kids.length;i++){\n"
        "      var n=kids[i];\n"
        "      if(!n.tagName||n.tagName==='SCRIPT'||n.tagName==='STYLE') continue;\n"
        "      if(n.offsetWidth<80||n.offsetHeight<40) continue;\n"
        "      last=n;\n"
        "    }\n"
        "    el.style.marginTop='14px';\n"
        "    (last||document.body).appendChild(el);\n"
        "  }\n"
        "})();\n"
        "</script></body></html>"
    )


def _img_size(data: bytes) -> tuple[int, int] | None:
    """从 PNG / JPEG 字节解析真实像素宽高。解析失败返回 None。"""
    if (len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n"
            and data[12:16] == b"IHDR"):
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        if w > 0 and h > 0:
            return w, h
    if len(data) >= 2 and data[0] == 0xFF and data[1] == 0xD8:
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return (w, h) if w > 0 and h > 0 else None
            seg = int.from_bytes(data[i + 2:i + 4], "big")
            if seg < 2:
                break
            i += 2 + seg
    return None


async def _screenshot_module(page_html: str, tag: str = "wzry") -> tuple[bytes, int, int] | None:
    """框架内置 playwright 模块渲染 (set_content, 页面已自包含)。不可用返回 None。"""
    rd = _get_module("renderer")
    pw = rd.playwright if rd else _get_module("playwright")
    if not pw or not pw.is_available():
        return None
    try:
        async with _render_sem:
            async with pw.new_page(viewport=(1400, 900)) as page:
                await page.set_content(page_html, wait_until="domcontentloaded")
                try:
                    await page.wait_for_function(_READY_JS, timeout=_READY_TIMEOUT_MS)
                except Exception:
                    pass
                await page.wait_for_timeout(_RENDER_SETTLE_MS)
                # 只截内容根元素 (Gitee/Yunzai 同款 #container||.container||body):
                el = (await page.query_selector("#container")
                      or await page.query_selector(".container")
                      or await page.query_selector("body"))
                img = await el.screenshot(type="jpeg", quality=90)
        size = _img_size(img) or (1400, 900)
        return img, size[0], size[1]
    except Exception:
        return None


async def _screenshot_subprocess(page_html: str, tag: str) -> tuple[bytes, int, int] | None:
    ts = int(time.time() * 1000)
    tmp = tempfile.gettempdir()
    hp = os.path.join(tmp, f"{tag}_{ts}.html")
    ip = os.path.join(tmp, f"{tag}_{ts}.jpg")
    sp = os.path.join(tmp, f"{tag}_r_{ts}.py")
    script = (
        "import sys\n"
        "from playwright.sync_api import sync_playwright\n"
        "try:\n"
        "    with sync_playwright() as p:\n"
        "        b=p.chromium.launch(timeout=60000)\n"
        "        pg=b.new_page(viewport={\"width\":1400,\"height\":900})\n"
        f"        pg.goto(\"file:///{hp.replace(chr(92), '/')}\",wait_until=\"domcontentloaded\")\n"
        f"        try: pg.wait_for_function({_READY_JS!r}, timeout={_READY_TIMEOUT_MS})\n"
        "        except Exception: pass\n"
        f"        pg.wait_for_timeout({_RENDER_SETTLE_MS})\n"
        "        el=(pg.query_selector(\"#container\") or pg.query_selector(\".container\") or pg.query_selector(\"body\"))\n"
        f"        el.screenshot(path=r\"{ip}\",type=\"jpeg\",quality=90)\n"
        "        b.close()\n"
        "        print(\"SUCCESS\")\n"
        "except Exception as e:\n"
        "    print(\"ERROR:\"+str(e));sys.exit(1)\n"
    )
    try:
        with open(hp, "w", encoding="utf-8") as f:
            f.write(page_html)
        with open(sp, "w", encoding="utf-8") as f:
            f.write(script)
        async with _render_sem:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, sp,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        out = stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0 or "SUCCESS" not in out or not os.path.exists(ip):
            return None
        with open(ip, "rb") as f:
            img = f.read()
        size = _img_size(img) or (1200, 900)
        return img, size[0], size[1]
    except Exception:
        return None
    finally:
        for p in (sp, hp, ip):
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except Exception:
                pass


# 截图面积上限 (像素)。正常页面最大也就 1500×3000 量级, 45M 留了足够余量;
_MAX_SHOT_PIXELS = 45_000_000


async def render_template(template: str, data: dict,
                          tag: str = "wzry") -> tuple[bytes, int, int] | None:
    """渲染原版模板, 返回 (png_bytes, width, height)。失败返回 None。"""
    data = await _inline_remote_images(data)
    page_html = build_page(template, data)
    shot = await _screenshot_module(page_html, tag)
    if shot is None:
        shot = await _screenshot_subprocess(page_html, tag)
    if shot and shot[1] * shot[2] > _MAX_SHOT_PIXELS:
        print(f"[render] {template} 出图 {shot[1]}x{shot[2]} 超过 {_MAX_SHOT_PIXELS} 像素上限, "
              "多半是模板变量与视图数据不匹配, 已放弃发送")
        return None
    return shot


async def _render_and_host(template: str, data: dict, name_hint: str = ""):
    """渲染并上传图床。返回 (img, w, h, url|None); 渲染失败返回 None。

    上传走图床模块的 upload_any(): 按各图床在模块配置里的 priority 从小到大依次尝试,
    全部失败或模块不可用时 url 为 None, 由调用方回退成直接发图。
    """
    tag = name_hint or "wzry"
    shot = await render_template(template, data, tag)
    if not shot:
        return None
    img, w, h = shot
    size = _img_size(img)
    if size:
        w, h = size
    url = None
    hosting = _get_module("image_hosting")
    if hosting:
        try:
            fname = f"wzry_{name_hint or 'card'}_{int(time.time())}.jpg"
            url = await hosting.upload_any(img, fname)
        except Exception:
            url = None
    return img, w, h, url


async def send_html(event, template: str, data: dict, caption: str = "",
                    buttons=None, name_hint: str = "",
                    cache_key: str = "", cache_ttl: int = 0) -> bool:
    """渲染模板并发送。优先图床 markdown (带尺寸), 发不出去就回退字节图。"""
    if cache_key:
        cached = _cache_get(cache_key)
        if cached:
            url, w, h = cached
            if await event.reply(f"{caption}\n![战绩 #{w}px #{h}px]({url})".strip(), buttons=buttons):
                return True
            # 直链发不出去 (图床挂了 / 平台不认): 丢掉缓存, 重新渲染并重新上传
            _cache_drop(cache_key)
    res = await _render_and_host(template, data, name_hint)
    if not res:
        return False
    img, w, h, url = res
    if url and await event.reply(
            f"{caption}\n![战绩 #{w}px #{h}px]({url})".strip(), buttons=buttons):
        # 只缓存发成功了的直链, 免得坏链被复用 12 小时
        _cache_put(cache_key, url, w, h, cache_ttl)
        return True
    await event.reply_image(img, caption or "王者荣耀")
    return True


async def send_html_to_group(sender, group_id: str, template: str, data: dict,
                             caption: str = "", buttons=None, name_hint: str = "") -> bool:
    """主动推送: ①图床直链 markdown → ②QQ 原生图片上传 → 都失败返回 False。"""
    res = await _render_and_host(template, data, name_hint)
    if not res:
        return False
    img, w, h, url = res
    if url:
        try:
            ok, _, _ = await sender.send_to_group(
                group_id, f"{caption}\n![战绩 #{w}px #{h}px]({url})".strip(), buttons=buttons)
            if ok:
                return True
        except Exception:
            pass
    try:
        ok, _ = await sender.send_image("group", group_id, img, content=caption)
        if ok:
            return True
    except Exception:
        pass
    return False
