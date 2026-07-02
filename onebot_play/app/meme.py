"""Meme 表情包 — 数据加载 + 指令处理 (列表/搜索/详情/随机/更新/生成/主人管理)。

meme 生成走外部 meme API (config.meme_api_url), 表情数据来自 assets/bq.json。
"""

import base64
import json
import os
import random
import re

import aiohttp

from core.base.logger import PLUGIN, get_logger

from . import config
from .message import (avatar_url, extract_at_users, extract_image_urls,
                      get_reply_images, send_image_base64, send_reply)

log = get_logger(PLUGIN, 'play')

_ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets')
_BQ_JSON = os.path.join(_ASSETS, 'bq.json')
_LIST_PNG = os.path.join(_ASSETS, 'meme-list.png')

HELP_MESSAGE = (
    '【meme列表】查看表情列表\n'
    '【表情名@人】制作表情（需前缀）\n'
    '【meme搜索+词】搜索表情\n'
    '【表情名+详情】查看用法\n'
    '【设置/删除主人+QQ】管理主人\n'
    '【点歌+歌名】搜索并点歌\n'
    '【听+序号】播放搜索到的歌曲\n'
    '【画+描述】AI绘画\n'
    '提示：仅表情生成需要前缀'
)

_key_map: dict = {}      # keyword -> code
_infos: dict = {}        # code -> info
_list_img_cache = None
_loaded = False


def _load():
    global _key_map, _infos, _loaded
    if _loaded:
        return
    data = {}
    try:
        if os.path.exists(_BQ_JSON):
            with open(_BQ_JSON, encoding='utf-8') as f:
                data = json.load(f)
    except Exception as e:  # noqa: BLE001
        log.warning(f'加载 bq.json 失败: {e}')
    key_map, infos = {}, {}
    for code, info in (data or {}).items():
        infos[code] = info
        for kw in info.get('keywords') or []:
            key_map[kw] = code
    _key_map, _infos = key_map, infos
    _loaded = True
    log.info(f'Meme 数据加载完成, 共 {len(_key_map)} 个关键词')


def reload_data():
    global _loaded, _list_img_cache
    _loaded = False
    _list_img_cache = None
    _load()


def _list_image_b64():
    global _list_img_cache
    if _list_img_cache is not None:
        return _list_img_cache
    if os.path.exists(_LIST_PNG):
        with open(_LIST_PNG, 'rb') as f:
            _list_img_cache = base64.b64encode(f.read()).decode('ascii')
        return _list_img_cache
    return None


def _find_longest_key(msg: str):
    keys = [k for k in _key_map if msg.startswith(k)]
    if not keys:
        return None
    keys.sort(key=len, reverse=True)
    return keys[0]


def _detail(code: str) -> str:
    d = _infos.get(code)
    if not d:
        return '未找到该表情信息'
    pt = d.get('params_type', {})
    ins = (f"【代码】{d.get('key', code)}\n【名称】{'、'.join(d.get('keywords', []))}\n"
           f"【图片】{pt.get('min_images', 0)}-{pt.get('max_images', 0)}\n"
           f"【文本】{pt.get('min_texts', 0)}-{pt.get('max_texts', 0)}")
    if (pt.get('args_type') or {}).get('parser_options'):
        ins += '\n【参数】支持额外参数'
    return ins


def _search(kw: str) -> list:
    return [k for k in _key_map if kw in k]


def _random_key():
    keys = [k for k, i in _infos.items()
            if i.get('params_type', {}).get('min_images') == 1
            and i.get('params_type', {}).get('min_texts') == 0]
    if not keys:
        return None
    return _infos[random.choice(keys)].get('keywords', [None])[0]


def _trim_char(s: str, ch: str) -> str:
    return re.sub(rf'^[{ch}]+|[{ch}]+$', '', s or '')


def _build_args(code: str, args: str, user_infos: list) -> str:
    obj = {}
    info = _infos.get(code, {})
    args_type = info.get('params_type', {}).get('args_type')
    if args_type:
        props = args_type.get('args_model', {}).get('properties', {})
        parser_options = args_type.get('parser_options', []) or []
        for prop, pi in props.items():
            if prop == 'user_infos':
                continue
            if pi.get('enum'):
                m = {}
                for o in parser_options:
                    act = o.get('action') or {}
                    if o.get('dest') == prop and act.get('type') == 0 and act.get('value'):
                        for n in o.get('names', []):
                            m[n.lstrip('-')] = act['value']
                obj[prop] = m.get(args.strip(), pi.get('default'))
            elif pi.get('type') in ('integer', 'number'):
                if re.match(r'^\d+$', args.strip()):
                    obj[prop] = int(args.strip())
    obj['user_infos'] = [{'name': _trim_char(u.get('text', ''), '@'),
                          'gender': u.get('gender', 'unknown')} for u in user_infos]
    return json.dumps(obj, ensure_ascii=False)


async def _download(url: str):
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession() as s, s.get(url, timeout=timeout) as r:
            if r.status == 200:
                return await r.read()
    except Exception as e:  # noqa: BLE001
        log.warning(f'下载图片失败 {url}: {e}')
    return None


async def _generate(code: str, images: list, texts: list, args: str):
    url = f'{config.meme_api_url()}/memes/{code}/'
    form = aiohttp.FormData()
    for i, b in enumerate(images):
        form.add_field('images', b, filename=f'img{i}.jpg', content_type='image/jpeg')
    for t in texts:
        form.add_field('texts', t)
    if args:
        form.add_field('args', args)
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession() as s, s.post(url, data=form, timeout=timeout) as r:
            if r.status == 200:
                return await r.read()
            text = await r.text()
            try:
                j = json.loads(text)
                err = j.get('detail') or j.get('error') or j.get('message') or '未知错误'
            except Exception:  # noqa: BLE001
                err = '生成失败' if len(text) > 50 else text
            return f'meme表情生成【{code}】出现了错误：{err}'
    except Exception as e:  # noqa: BLE001
        return f'meme表情生成【{code}】出现了错误：{e}\nAPI地址: {config.meme_api_url()}'


def _apply_master_protection(imgs: list, sender_id: str, at_users: list) -> list:
    if not config.enable_master_protect():
        return imgs
    masters = config.owner_qqs()
    if not masters or sender_id in masters:
        return imgs
    sender_ava = avatar_url(sender_id)
    at_master = next((a for a in at_users if str(a.get('qq')) in masters), None)

    def _qq_of(url):
        m = re.search(r'nk=(\d+)', url)
        return m.group(1) if m else None

    if at_master:
        if len(imgs) == 1:
            if _qq_of(imgs[0]) in masters:
                return [sender_ava]
        elif len(imgs) >= 2:
            return [avatar_url(at_master['qq']), sender_ava, *imgs[2:]]
    else:
        for i, url in enumerate(imgs):
            if _qq_of(url) in masters:
                if len(imgs) == 1:
                    return [sender_ava]
                new = list(imgs)
                new[0] = imgs[i]
                new[1] = sender_ava
                return new
    return imgs


# ---------- 指令入口 ----------

async def handle(event) -> bool:
    """处理 meme 相关指令; 返回 True 表示已消费。"""
    _load()
    raw = event.raw_message or ''
    cleaned = re.sub(r'\[CQ:at,qq=\d+\]', '', raw)
    cleaned = re.sub(r'\[CQ:reply,id=-?\d+\]', '', cleaned).strip()
    if not cleaned:
        cleaned = (event.content or '').strip()
    user_id = str(event.user_id)

    # 管理命令 (无需前缀)
    if re.match(r'^设置主人\s*\d+', cleaned):
        return await _add_master(event, cleaned, user_id)
    if re.match(r'^删除主人\s*\d+', cleaned):
        return await _remove_master(event, cleaned, user_id)
    if re.match(r'^主人列表$', cleaned):
        return await _master_list(event)

    # meme 辅助命令 (无需前缀)
    if re.match(r'^(meme(s)?|表情包)列表$', cleaned):
        return await _meme_list(event)
    if re.match(r'^随机(meme(s)?|表情包)', cleaned):
        return await _random_meme(event)
    if re.match(r'^(meme(s)?|表情包)帮助', cleaned):
        await send_reply(event, HELP_MESSAGE)
        return True
    if re.match(r'^(meme(s)?|表情包)搜索', cleaned):
        return await _meme_search(event, cleaned)
    if re.match(r'^(meme(s)?|表情包)更新', cleaned):
        reload_data()
        await send_reply(event, '表情数据已重新加载')
        return True

    # 表情生成 (需要前缀)
    pre = config.prefix() or ''
    if pre and not cleaned.startswith(pre):
        return False
    content = cleaned[len(pre):].strip() if pre else cleaned
    target = _find_longest_key(content)
    if target:
        await _generate_meme(event, content, target)
        return True
    return False


async def _add_master(event, msg, user_id) -> bool:
    if not config.is_master(user_id):
        await send_reply(event, '只有主人才能设置')
        return True
    m = re.search(r'(\d+)', msg)
    qq = m.group(1)
    ids = config.owner_qqs()
    if qq in ids:
        await send_reply(event, f'{qq} 已是主人')
        return True
    ids.append(qq)
    config.set_owner_qqs(ids)
    await send_reply(event, f'已添加主人：{qq}')
    return True


async def _remove_master(event, msg, user_id) -> bool:
    if not config.is_master(user_id):
        await send_reply(event, '只有主人才能删除')
        return True
    m = re.search(r'(\d+)', msg)
    qq = m.group(1)
    ids = config.owner_qqs()
    if qq not in ids:
        await send_reply(event, f'{qq} 不是主人')
        return True
    if qq == user_id and len(ids) == 1:
        await send_reply(event, '不能删除唯一主人')
        return True
    config.set_owner_qqs([q for q in ids if q != qq])
    await send_reply(event, f'已删除主人：{qq}')
    return True


async def _master_list(event) -> bool:
    ids = config.owner_qqs()
    if ids:
        body = '\n'.join(f'{i + 1}. {q}' for i, q in enumerate(ids))
        await send_reply(event, f'主人列表：\n{body}')
    else:
        await send_reply(event, '当前没有设置主人')
    return True


async def _meme_list(event) -> bool:
    img = _list_image_b64()
    if img:
        await send_image_base64(event, img)
        return True
    kws = ' '.join(f'【{k}】' for k in list(_key_map.keys())[:30])
    await send_reply(event, f'【Meme列表】共 {len(_key_map)} 个\n\n{kws} ...\n\n发送【meme搜索+词】搜索更多')
    return True


async def _random_meme(event) -> bool:
    kw = _random_key()
    if not kw:
        await send_reply(event, '暂无可用随机meme')
        return True
    await _generate_meme(event, kw, kw)
    return True


async def _meme_search(event, msg) -> bool:
    s = re.sub(r'^#?(meme(s)?|表情包)搜索', '', msg).strip()
    if not s:
        await send_reply(event, '请输入关键词')
        return True
    hits = _search(s)
    if hits:
        txt = '\n'.join(f'{i + 1}. {k}' for i, k in enumerate(hits[:20]))
        if len(hits) > 20:
            txt += f'\n...共{len(hits)}个'
    else:
        txt = '无结果'
    await send_reply(event, f'搜索结果：\n{txt}')
    return True


async def _member_infos(event, at_users):
    if not (at_users and event.is_group):
        return None
    try:
        res = await event.call_api('get_group_member_list', {'group_id': str(event.group_id)})
    except Exception:  # noqa: BLE001
        return None
    members = res.get('data') if isinstance(res, dict) else res
    if not isinstance(members, list):
        return None
    out = []
    for a in at_users:
        m = next((x for x in members if str(x.get('user_id')) == str(a['qq'])), None)
        out.append({'qq': a['qq'],
                    'text': (m or {}).get('card') or (m or {}).get('nickname') or a.get('text'),
                    'gender': (m or {}).get('sex', 'unknown')})
    return out


async def _generate_meme(event, msg, target):
    try:
        code = _key_map.get(target)
        info = _infos.get(code)
        if not info:
            await send_reply(event, '未找到该表情')
            return
        pt = info.get('params_type', {})
        text1 = msg.replace(target, '', 1)
        if text1.strip() in ('详情', '帮助'):
            await send_reply(event, _detail(code))
            return

        parts = text1.split('#')
        text = parts[0]
        args = parts[1] if len(parts) > 1 else ''
        user_id = str(event.user_id)
        sender = event.sender or {}
        at_users = extract_at_users(event)

        imgs = []
        if pt.get('max_images', 0) > 0:
            imgs = [*(await get_reply_images(event)), *extract_image_urls(event)]
            if not imgs and at_users:
                imgs = [avatar_url(a['qq']) for a in at_users]
            if not imgs and pt.get('min_images', 0) > 0:
                imgs.append(avatar_url(user_id))
            if len(imgs) < pt.get('min_images', 0) and avatar_url(user_id) not in imgs:
                imgs = [avatar_url(user_id), *imgs]
            imgs = _apply_master_protection(imgs, user_id, at_users)
            imgs = imgs[:pt.get('max_images', 0)]

        texts = []
        if text and pt.get('max_texts', 0) == 0:
            return
        default_name = (at_users[0]['text'].replace('@', '').strip() if at_users and at_users[0].get('text')
                        else sender.get('card') or sender.get('nickname') or '用户')
        if not text and pt.get('min_texts', 0) > 0:
            texts.append(default_name)
        elif text:
            texts = text.split('/')[:pt.get('max_texts', 0)]
        if len(texts) < pt.get('min_texts', 0):
            await send_reply(event, f"需要{pt.get('min_texts', 0)}个文本，用/隔开")
            return
        if pt.get('max_texts', 0) > 0 and not texts:
            texts.append(default_name)

        user_infos = await _member_infos(event, at_users)
        if user_infos is None:
            user_infos = at_users or [{'text': default_name,
                                       'gender': sender.get('sex', 'unknown')}]

        buffers = []
        for url in imgs:
            b = await _download(url)
            if b:
                buffers.append(b)
        if pt.get('min_images', 0) > 0 and not buffers:
            await send_reply(event, '图片下载失败')
            return
        max_bytes = config.max_file_size() * 1024 * 1024
        if buffers and any(len(b) >= max_bytes for b in buffers):
            await send_reply(event, f'文件超限，最大{config.max_file_size()}MB')
            return

        result = await _generate(code, buffers, texts, _build_args(code, args, user_infos))
        if isinstance(result, str):
            await send_reply(event, result)
        else:
            await send_image_base64(event, base64.b64encode(result).decode('ascii'))
    except Exception as e:  # noqa: BLE001
        log.warning(f'表情生成出错: {e}')
        await send_reply(event, '表情生成出错')
