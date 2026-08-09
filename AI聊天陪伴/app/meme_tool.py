"""Independent complete meme catalog for AI companion."""
from __future__ import annotations

import asyncio
import json

import aiohttp

_MEME_API = 'http://datukuai.top:2233/memes'

# key, image count, label, text count, circle crop
_ROWS = [
    ('divorce',1,'离婚',0,False),('add_chaos',1,'添乱',0,False),('marriage',1,'结婚登记',0,False),
    ('always_like',1,'我永远喜欢',0,False),('crawl',1,'爬',0,False),('decent_kiss',1,'像样的亲亲',0,False),
    ('eat',1,'吃',0,False),('zzdd',1,'指指点点',0,False),('kiss',2,'亲',0,False),
    ('hutao_bite',1,'胡桃啃',0,False),('my_wife',1,'我老婆',0,False),('perfect',1,'完美',0,False),
    ('adoption',1,'收养',0,False),('roll',1,'滚',0,False),('throw',1,'丢',0,False),
    ('twist',1,'搓',0,False),('petpet',1,'摸',0,True),('a_jj_play_baseball',1,'打棒球',0,False),
    ('alike',1,'一样',0,False),('all_the_days',2,'一生一世',0,False),('always',1,'一直',0,False),
    ('xiatou',1,'下头',0,False),('arona_throw',1,'阿罗娜扔',0,False),('ask',1,'问问',0,False),
    ('azur_lane_cheshire_thumbs_up',1,'柴郡点赞',0,False),('back_to_work',1,'继续干活',0,False),
    ('beg_foster_care',1,'求收养',0,False),('beloveds',1,'亲亲抱抱举高高',0,False),
    ('blood_pressure',1,'高血压',0,False),('bocchi_draft',1,'波奇手稿',0,False),
    ('capoo_draw',1,'咖波画画',0,False),('capoo_love',1,'咖波爱你',0,False),
    ('capoo_point',1,'咖波指',0,False),('capoo_rub',1,'咖波蹭',0,False),
    ('capoo_take_sleep',1,'咖波睡觉',0,False),('cat_lick',1,'猫猫舔',0,False),
    ('charpic',1,'字符画',0,False),('chillet_deer',1,'寒霜鹿',0,False),
    ('cinderella_eat',1,'灰姑娘吃',0,False),('coupon',1,'陪睡券',0,False),
    ('cover_face',1,'捂脸',0,False),('cyan',1,'群青',0,False),('dinosaur',1,'恐龙',0,False),
    ('distracted',1,'分心',0,False),('dog_girl',1,'狗狗女孩',0,False),('dog_of_vtb',1,'vtb的狗',0,False),
    ('fight_with_sunuo',1,'与宿傩战斗',0,False),('funny_mirror',1,'哈哈镜',0,False),
    ('guichu',1,'鬼畜',0,False),('hammer',1,'锤',0,False),('ignite',1,'燃起来了',0,False),
    ('kurogames_jinhsi_eat',1,'今汐吃',0,False),('kurogames_lupa_eat',1,'露帕吃',0,False),
    ('kurogames_phoebe_score_sheet',1,'菲比评分表',0,False),('kurogames_phrolova_eat',1,'芙洛洛吃',0,False),
    ('left_right_jump',1,'左右横跳',0,False),('let_me_in',1,'让我进去',0,False),
    ('listen_music',1,'听音乐',0,False),('louvre',1,'卢浮宫',0,False),
    ('mygo_sakiko_togawa',1,'祥子',0,False),('my_friend',1,'我朋友说',1,False),
    ('myplay',1,'笨死了',0,False),('no_response',1,'没有反应',0,False),('paint',1,'这像画吗',0,False),
    ('xile',1,'洗了',0,False),('oshi_no_ko',1,'推的孩子',0,False),('punch',1,'打拳',0,False),
    ('potato_mines',1,'土豆地雷',0,False),('sekaiichi_kawaii',1,'世界第一可爱',0,False),
    ('worship',1,'膜拜',0,False),('shake_head',1,'摇头',0,False),('shock',1,'震惊',0,False),
    ('speechless',1,'无语',0,False),('stare_at_you',1,'盯着你',0,False),('stew',1,'炖',0,False),
    ('thermometer_gun',1,'体温枪',0,False),('trance',1,'恍惚',0,False),
    ('upside_down',1,'我看你们是反了',0,False),('what_I_want_to_do',1,'我想要做的事',0,False),
]
COMMAND_CONFIG = {key: {'images': images, 'keywords': label, 'texts': texts, 'circle': circle}
                  for key, images, label, texts, circle in _ROWS}
_CATALOG = '；'.join(f'{key}={item["keywords"]}' for key, item in COMMAND_CONFIG.items())

TOOL = {'type': 'function', 'function': {
    'name': 'generate_meme',
    'description': ('对话气氛自然适合表情包时，从完整模板表选择一个。单图固定只使用对方头像；'
                    '双图固定按 AI 自己、对方的顺序使用；需要文字时由你按语境填写 texts。'
                    '不要频繁调用，不要报告工具状态。模板：' + _CATALOG),
    'parameters': {'type': 'object', 'properties': {
        'template': {'type': 'string', 'enum': list(COMMAND_CONFIG)},
        'texts': {'type': 'array', 'description': '需要文字时按当前语境填写',
                  'items': {'type': 'string', 'maxLength': 80}, 'maxItems': 4}},
        'required': ['template'], 'additionalProperties': False}}}

_CUES = ('表情包','meme','摸摸','摸头','亲亲','抱抱','哈哈','笑死','好笑','可爱','完美','厉害',
         '加油','辛苦','离谱','尴尬','捂脸','震惊','无语','盯着','摇头','点赞','喜欢','结婚','吃掉')

def should_offer(text: str) -> bool:
    value = str(text or '').casefold()
    return any(cue in value for cue in _CUES)


def _bot_avatar_url(appid: str, fallback_self_id: str = '') -> str:
    try:
        from core.bot.manager import _bot_manager_ref
        bot = _bot_manager_ref.get_bot(appid) if _bot_manager_ref else None
        url = str(getattr(bot, 'avatar_url', '') or '').strip()
        if url.startswith(('http://', 'https://')):
            return url
    except (AttributeError, ImportError, KeyError):
        pass
    return (
        f'https://q.qlogo.cn/qqapp/{appid}/{fallback_self_id}/640'
        if fallback_self_id else ''
    )

async def _download(session: aiohttp.ClientSession, url: str) -> bytes | None:
    try:
        async with session.get(url) as response:
            data = await response.read() if response.status == 200 else b''
            return data if 0 < len(data) <= 15 * 1024 * 1024 else None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None

async def _generate(session, key, images, texts, circle):
    args = {'user_infos': [{'name': '', 'gender': 'unknown'}]}
    if circle:
        args['circle'] = True
    form = aiohttp.FormData()
    form.add_field('args', json.dumps(args, ensure_ascii=False))
    for name, data in images:
        form.add_field('images', data, filename=name, content_type='image/jpeg')
    for value in texts:
        form.add_field('texts', value)
    try:
        async with session.post(f'{_MEME_API}/{key}/', data=form) as response:
            return await response.read() if response.status == 200 else None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None

async def run(arguments: dict, context: dict, config: dict) -> dict:
    template = str(arguments.get('template') or '')
    item = COMMAND_CONFIG.get(template)
    event = context.get('event')
    user_id, appid = str(context.get('user_id') or ''), str(context.get('appid') or '')
    if item is None or event is None or not user_id or not appid:
        return {'ok': True, 'sent': False}
    texts = [str(value).strip()[:80] for value in arguments.get('texts', []) if str(value).strip()]
    required = int(item.get('texts') or 0)
    if required and len(texts) < required:
        return {'ok': True, 'sent': False}
    texts = texts[:required] if required else []
    target_url = f'https://q.qlogo.cn/qqapp/{appid}/{user_id}/640'
    self_url = _bot_avatar_url(appid, str(context.get('self_id') or '').strip())
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            if item['images'] == 2:
                if not self_url:
                    return {'ok': True, 'sent': False}
                own, target = await asyncio.gather(
                    _download(session, self_url), _download(session, target_url)
                )
                if not own or not target:
                    return {'ok': True, 'sent': False}
                images = [('self.jpg', own), ('target.jpg', target)]
            else:
                target = await _download(session, target_url)
                if not target:
                    return {'ok': True, 'sent': False}
                images = [('target.jpg', target)]
            result = await _generate(session, template, images, texts, bool(item.get('circle')))
        if not result:
            return {'ok': True, 'sent': False}
        mention = f'<@{user_id}>' if getattr(event, 'is_group', False) else ''
        await event.reply_image(result, content=mention)
        return {'ok': True, 'sent': True}
    except Exception:
        return {'ok': True, 'sent': False}
