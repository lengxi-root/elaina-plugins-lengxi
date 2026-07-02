"""AI 绘画 — 画/绘/draw + 描述, 预设关键词, @某人/引用图片使用图片编辑。

生图接口不内置, 需在面板「AI 绘画」自行配置 (接口类型 / API 域名 / 密钥 / 模型 / 尺寸)。
两种接口共用同一套配置与预设关键词, 区别仅在图生图的请求方式:
- openai (标准): 文生图 POST {域名}/v1/images/generations (JSON);
  图生图 POST {域名}/v1/images/edits (multipart 上传图片文件)。
- custom (图片URL): 图生图 POST {域名}/v1/images/edits, JSON 用 images 传参考图 URL 数组,
  并附 Origin/Referer/X-Requested-With 头。
响应解析 data[0] 的 b64_json / url。

预设关键词也在面板配置 (名字 + 提示词): 发送「名字」并上传/引用一张图片,
即用该预设提示词修改这张图片。
"""

import base64
import binascii
import re

import aiohttp

from core.base.logger import PLUGIN, get_logger

from . import config
from .message import (avatar_url, extract_at_users, extract_image_urls,
                      get_reply_images, send_image, send_reply)

log = get_logger(PLUGIN, 'play')

_DRAW_TIMEOUT = 300
_API_ERROR_MSG = '⚠ 因上游接口超限导致，暂时无法使用。'


def _presets() -> dict:
    """面板配置的预设关键词: {名字: 提示词}。"""
    out = {}
    for p in config.draw_presets():
        name = str(p.get('name') or '').strip()
        prompt = str(p.get('prompt') or '').strip()
        if name and prompt:
            out[name] = prompt
    return out


def preset_names() -> list:
    return list(_presets().keys())


def _draw_ready() -> bool:
    iface = config.draw_interface()
    return bool(iface['url'] and iface['key'])


def _not_ready_msg() -> str:
    return '绘画功能未配置，请在面板「AI 绘画」选择接口并填写 API 域名与密钥'


async def handle(event) -> bool:
    if not config.enable_draw():
        return False
    text = re.sub(r'\[CQ:[^\]]+\]', '', event.raw_message or '').strip()
    if not text:
        text = (event.content or '').strip()

    presets = _presets()

    if re.match(r'^(预设提示词|提示词列表|画图预设)$', text):
        if not presets:
            await send_reply(event, '暂无预设关键词，请在面板「AI 绘画」配置')
        else:
            names = list(presets.keys())
            body = '\n'.join(f'{i + 1}. {k}' for i, k in enumerate(names))
            await send_reply(event, f'🎨 预设关键词列表：\n{body}\n\n使用方式：\n'
                                    f'• 发送「{names[0]}」并上传/引用一张图片\n'
                                    f'• {names[0]}@某人\n• {names[0]}+QQ号')
        return True

    for name, prompt in presets.items():
        m = re.match(rf'^{re.escape(name)}\s*(.*)$', text, re.IGNORECASE)
        if m:
            return await _preset_draw(event, name, prompt, m.group(1).strip())

    m = re.match(r'^(?:画|绘|draw)\s*(.+)$', text, re.IGNORECASE)
    if not m:
        return False
    prompt = re.sub(r'\[CQ:at,[^\]]+\]', '', m.group(1).strip()).strip()
    if not prompt:
        hint = f"\n预设: {'、'.join(presets)}" if presets else ''
        await send_reply(event, f'请输入绘画描述，例如：画一只可爱的猫咪\n'
                                f'支持引用图片、附带图片或@某人使用头像{hint}')
        return True
    if not _draw_ready():
        await send_reply(event, _not_ready_msg())
        return True

    used_preset = None
    if prompt in presets:
        used_preset = prompt
        prompt = presets[prompt]

    image_urls = await get_reply_images(event)
    if not image_urls:
        image_urls = extract_image_urls(event)
    if not image_urls:
        at_users = extract_at_users(event)
        if at_users and at_users[0].get('qq'):
            image_urls = [avatar_url(at_users[0]['qq'])]
    return await _execute(event, prompt, image_urls, used_preset)


async def _preset_draw(event, preset_name, prompt, extra) -> bool:
    if not _draw_ready():
        await send_reply(event, _not_ready_msg())
        return True
    image_urls = await get_reply_images(event)
    if not image_urls:
        image_urls = extract_image_urls(event)
    if not image_urls:
        m = re.search(r'(\d{5,11})', extra)
        if m:
            image_urls = [avatar_url(m.group(1))]
    if not image_urls:
        at_users = extract_at_users(event)
        if at_users and at_users[0].get('qq'):
            image_urls = [avatar_url(at_users[0]['qq'])]
    if not image_urls:
        image_urls = [avatar_url(event.user_id)]
    return await _execute(event, prompt, image_urls, preset_name)


def _image_out(result) -> str | None:
    if not isinstance(result, dict):
        return None
    data = result.get('data') or []
    if not data or not isinstance(data[0], dict):
        return None
    item = data[0]
    if item.get('b64_json'):
        return f"base64://{item['b64_json']}"
    if item.get('url'):
        return item['url']
    return None


async def _fetch_image_bytes(url) -> tuple:
    """返回 (bytes, content_type)；支持 http(s) 与 base64:// 两种来源。"""
    if url.startswith('base64://'):
        try:
            return base64.b64decode(url[len('base64://'):]), 'image/png'
        except (binascii.Error, ValueError):
            return None, None
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession() as s, s.get(url, timeout=timeout) as r:
        if r.status != 200:
            return None, None
        ct = (r.headers.get('Content-Type') or 'image/png').split(';')[0].strip()
        return await r.read(), ct


async def _post(url, headers, *, json=None, data=None) -> dict | None:
    timeout = aiohttp.ClientTimeout(total=_DRAW_TIMEOUT)
    async with aiohttp.ClientSession() as s, s.post(
            url, json=json, data=data, headers=headers, timeout=timeout) as r:
        result = await r.json(content_type=None)
        if r.status != 200 or (isinstance(result, dict) and result.get('error')):
            if config.debug():
                log.info(f'生图失败 {r.status}: {result}')
            return None
        return result


async def _execute(event, prompt, image_urls, preset_name=None) -> bool:
    if not _draw_ready():
        await send_reply(event, _not_ready_msg())
        return True
    iface = config.draw_interface()
    base = iface['url']
    key = iface['key']
    model = iface['model']
    provider = iface['type']
    size = 'auto'
    has_image = bool(image_urls)
    await send_reply(event, '🎨 正在修改图片，请稍候...' if has_image else '🎨 正在绘制中，请稍候...')

    auth = {'Authorization': f'Bearer {key}'}
    try:
        if not has_image:
            payload = {'model': model, 'prompt': prompt, 'n': 1, 'size': size,
                       'response_format': 'b64_json'}
            result = await _post(f'{base}/v1/images/generations',
                                 {**auth, 'Content-Type': 'application/json'}, json=payload)
        elif provider == 'custom':
            payload = {'prompt': prompt, 'model': model, 'n': 1, 'size': size, 'images': image_urls}
            headers = {**auth, 'Content-Type': 'application/json',
                       'Origin': base, 'Referer': f'{base}/image/', 'X-Requested-With': 'mark.via'}
            result = await _post(f'{base}/v1/images/edits', headers, json=payload)
        else:  # openai 标准: multipart 上传图片文件
            img_bytes, ct = await _fetch_image_bytes(image_urls[0])
            if not img_bytes:
                await send_reply(event, _API_ERROR_MSG)
                return True
            ext = {'image/jpeg': 'jpg', 'image/webp': 'webp'}.get(ct, 'png')
            form = aiohttp.FormData()
            form.add_field('model', model)
            form.add_field('prompt', prompt)
            form.add_field('n', '1')
            form.add_field('size', size)
            form.add_field('image', img_bytes, filename=f'image.{ext}', content_type=ct)
            result = await _post(f'{base}/v1/images/edits', auth, data=form)
    except Exception as e:  # noqa: BLE001
        if config.debug():
            log.info(f'生图异常: {e}')
        await send_reply(event, _API_ERROR_MSG)
        return True

    if not result:
        await send_reply(event, _API_ERROR_MSG)
        return True
    image_url = _image_out(result)
    if image_url:
        await send_image(event, image_url, reply=True)
    else:
        await send_reply(event, _API_ERROR_MSG)
    return True
