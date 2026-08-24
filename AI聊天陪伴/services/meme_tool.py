"""聊天陪伴插件独立维护的完整表情包目录。"""

from __future__ import annotations

import asyncio
import json

import aiohttp

_MEME_API = "http://datukuai.top:2233/memes"

# 配置依次为：键、图片数量、名称、文字数量、是否圆形裁剪。
_ROWS = [
    ("add_chaos", 1, "添乱", 0, False),
    ("always_like", 1, "我永远喜欢", 0, False),
    ("eat", 1, "吃", 0, False),
    ("zzdd", 1, "指指点点", 0, False),
    ("kiss", 2, "亲", 0, False),
    ("perfect", 1, "完美", 0, False),
    ("throw", 1, "丢", 0, False),
    ("twist", 1, "搓", 0, False),
    ("petpet", 1, "摸摸头", 0, True),
    ("ask", 1, "问问", 0, False),
    ("azur_lane_cheshire_thumbs_up", 1, "点赞", 0, False),
    ("back_to_work", 1, "继续干活", 0, False),
    ("blood_pressure", 1, "高血压", 0, False),
    ("capoo_draw", 1, "画画", 0, False),
    ("capoo_love", 1, "喜欢你", 0, False),
    ("capoo_point", 1, "指你", 0, False),
    ("capoo_rub", 1, "蹭蹭", 0, False),
    ("capoo_take_sleep", 1, "睡觉", 0, False),
    ("cover_face", 1, "捂脸", 0, False),
    ("distracted", 1, "分心", 0, False),
    ("funny_mirror", 1, "哈哈镜", 0, False),
    ("hammer", 1, "锤", 0, False),
    ("ignite", 1, "燃起来了", 0, False),
    ("left_right_jump", 1, "左右横跳", 0, False),
    ("listen_music", 1, "听音乐", 0, False),
    ("my_friend", 1, "我朋友说", 1, False),
    ("no_response", 1, "没有反应", 0, False),
    ("sekaiichi_kawaii", 1, "世界第一可爱", 0, False),
    ("worship", 1, "膜拜", 0, False),
    ("shake_head", 1, "摇头", 0, False),
    ("shock", 1, "震惊", 0, False),
    ("speechless", 1, "无语", 0, False),
    ("stare_at_you", 1, "盯着你", 0, False),
    ("trance", 1, "恍惚", 0, False),
]
COMMAND_CONFIG = {
    key: {"images": images, "keywords": label, "texts": texts, "circle": circle}
    for key, images, label, texts, circle in _ROWS
}
_CATALOG = "；".join(
    f"{key}={item['keywords']}" for key, item in COMMAND_CONFIG.items()
)

TOOL = {
    "type": "function",
    "function": {
        "name": "generate_meme",
        "description": (
            "对话气氛自然适合表情包时，从完整模板表选择一个。单图固定只使用对方头像；"
            "双图固定按 AI 自己、对方的顺序使用；需要文字时由你按语境填写 texts。"
            "不要频繁调用，不要报告工具状态。模板：" + _CATALOG
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "template": {"type": "string", "enum": list(COMMAND_CONFIG)},
                "texts": {
                    "type": "array",
                    "description": "需要文字时按当前语境填写",
                    "items": {"type": "string", "maxLength": 80},
                    "maxItems": 4,
                },
            },
            "required": ["template"],
            "additionalProperties": False,
        },
    },
}


def _bot_avatar_url(appid: str, fallback_self_id: str = "") -> str:
    try:
        from core.bot.manager import _bot_manager_ref

        bot = _bot_manager_ref.get_bot(appid) if _bot_manager_ref else None
        url = str(getattr(bot, "avatar_url", "") or "").strip()
        if url.startswith(("http://", "https://")):
            return url
    except (AttributeError, ImportError, KeyError):
        pass
    return (
        f"https://q.qlogo.cn/qqapp/{appid}/{fallback_self_id}/640"
        if fallback_self_id
        else ""
    )


async def _download(session: aiohttp.ClientSession, url: str) -> bytes | None:
    try:
        async with session.get(url) as response:
            data = await response.read() if response.status == 200 else b""
            return data if 0 < len(data) <= 15 * 1024 * 1024 else None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def _generate(session, key, images, texts, circle):
    args = {"user_infos": [{"name": "", "gender": "unknown"}]}
    if circle:
        args["circle"] = True
    form = aiohttp.FormData()
    form.add_field("args", json.dumps(args, ensure_ascii=False))
    for name, data in images:
        form.add_field("images", data, filename=name, content_type="image/jpeg")
    for value in texts:
        form.add_field("texts", value)
    try:
        async with session.post(f"{_MEME_API}/{key}/", data=form) as response:
            return await response.read() if response.status == 200 else None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def run(arguments: dict, context: dict, config: dict) -> dict:
    template = str(arguments.get("template") or "")
    item = COMMAND_CONFIG.get(template)
    event = context.get("event")
    user_id, appid = str(context.get("user_id") or ""), str(context.get("appid") or "")
    if item is None or event is None or not user_id or not appid:
        return {"ok": True, "sent": False}
    texts = [
        str(value).strip()[:80]
        for value in arguments.get("texts", [])
        if str(value).strip()
    ]
    required = int(item.get("texts") or 0)
    if required and len(texts) < required:
        return {"ok": True, "sent": False}
    texts = texts[:required] if required else []
    target_url = f"https://q.qlogo.cn/qqapp/{appid}/{user_id}/640"
    self_url = _bot_avatar_url(appid, str(context.get("self_id") or "").strip())
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            if item["images"] == 2:
                if not self_url:
                    return {"ok": True, "sent": False}
                own, target = await asyncio.gather(
                    _download(session, self_url), _download(session, target_url)
                )
                if not own or not target:
                    return {"ok": True, "sent": False}
                images = [("self.jpg", own), ("target.jpg", target)]
            else:
                target = await _download(session, target_url)
                if not target:
                    return {"ok": True, "sent": False}
                images = [("target.jpg", target)]
            result = await _generate(
                session, template, images, texts, bool(item.get("circle"))
            )
        if not result:
            return {"ok": True, "sent": False}
        mention = f"<@{user_id}>" if getattr(event, "is_group", False) else ""
        await event.reply_image(result, content=mention)
        return {"ok": True, "sent": True}
    except Exception:
        return {"ok": True, "sent": False}
