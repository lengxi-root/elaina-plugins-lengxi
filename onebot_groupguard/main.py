"""OneBot 群管理插件。"""

import asyncio
import os
import re

from core.plugins import PLUGIN, get_logger
from core.plugins import handler, on_load, on_unload
from core.plugins import register_page, unregister_page

from .services import commands, guard, logbuf, verify
from .services.runtime import get_runtime, stop_background
from .services.utils import call_api, is_bot_admin, is_valid_qq
from .storage import repository as store
from .web import routes as webpanel

__plugin_meta__ = {
    "name": "群管 (groupguard)",
    "author": "冷曦",
    "description": "全功能群管理: 入群验证/违禁词/防撤回/刷屏检测/问答/黑白名单/名片锁定/活跃统计, 支持 Web 面板配置",
    "version": "1.0.1",
}

log = get_logger(PLUGIN, "groupguard")

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PANEL_HTML = os.path.join(_PLUGIN_DIR, "assets", "panel.html")
_PAGE_KEY = "groupguard"

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 2l7 3v6c0 5-3.5 8-7 9-3.5-1-7-4-7-9V5z"/></svg>'
)


async def _activity_saver():
    while True:
        try:
            await asyncio.sleep(120)
            await asyncio.to_thread(store.save_activity)
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            log.error(f"活跃统计保存失败: {e}")


@on_load
async def init():
    await asyncio.to_thread(store.load)
    await asyncio.to_thread(store.load_activity)
    await asyncio.to_thread(store.load_mute_records)
    logbuf.install(log)
    rt = get_runtime()
    register_page(
        key=_PAGE_KEY,
        label="群管",
        source="plugin",
        source_name="groupguard",
        icon=_ICON,
        html_file=_PANEL_HTML,
    )
    webpanel.register_routes()
    if rt.save_task is None or rt.save_task.done():
        rt.save_task = asyncio.create_task(_activity_saver())
    log.info("群管插件已加载")


@on_unload
async def cleanup():
    unregister_page(_PAGE_KEY)
    rt = get_runtime()
    if rt.save_task and not rt.save_task.done():
        rt.save_task.cancel()
        await asyncio.gather(rt.save_task, return_exceptions=True)
    rt.save_task = None
    await verify.clear_all_sessions()
    await stop_background()
    await asyncio.to_thread(store.save_activity, force=True)


@handler(
    r"[\s\S]*",
    name="groupguard",
    desc="群管消息处理管线",
    priority=50,
    group_only=True,
    event_types=["message"],
)
async def on_group_message(event, match):
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    if not is_valid_qq(user_id):
        log.warning(f"忽略无效群消息成员号: {user_id or '(空)'}@{group_id}")
        return
    message_id = event.message_id
    text = event.content or ""
    segments = getattr(event, "message", []) or []
    self_id = str(getattr(event, "self_id", "") or "")
    is_white = store.is_whitelisted(user_id)
    settings = store.get_group_settings(group_id)

    await guard.handle_card_lock_on_message(group_id, user_id, event.sender_card)

    if not is_white and await guard.handle_blacklist(
        group_id, user_id, message_id, settings
    ):
        return

    if await commands.handle_command(event):
        return

    if await guard.handle_qa(group_id, user_id, text, settings):
        store.record_activity(group_id, user_id)
        guard.cache_message(
            message_id,
            user_id,
            group_id,
            text,
            segments,
            self_id=self_id,
        )
        return

    if not is_white and await guard.handle_auto_recall(
        group_id, user_id, message_id, settings
    ):
        return

    if not is_white and await guard.handle_filter_keywords(
        group_id, user_id, message_id, text, settings
    ):
        return

    if not is_white and await guard.handle_msg_type_filter(
        group_id, user_id, message_id, text, segments, settings
    ):
        return

    if not is_white:
        await guard.handle_spam_detect(
            group_id, user_id, settings, self_id=self_id
        )

    store.record_activity(group_id, user_id)

    guard.cache_message(
        message_id,
        user_id,
        group_id,
        text,
        segments,
        self_id=self_id,
    )

    await guard.handle_emoji_react(group_id, user_id, message_id, self_id)

    await verify.handle_verify_answer(self_id, group_id, user_id, text, message_id)


@handler(
    r".*", name="groupguard_join_request", event_types=["request.group"], priority=50
)
async def on_group_request(event, match):
    if event.sub_type != "add":
        return
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    if not is_valid_qq(user_id):
        log.warning(f"忽略无效入群申请成员号: {user_id or '(空)'}@{group_id}")
        return
    flag = event.flag

    if not store.is_owner(user_id) and store.is_blacklisted(user_id):
        log.info(f"黑名单用户 {user_id} 申请加入群 {group_id}, 自动拒绝(全局黑名单)")
        if flag:
            await call_api(
                "set_group_add_request",
                {
                    "flag": flag,
                    "sub_type": "add",
                    "approve": False,
                    "reason": "你已被列入黑名单",
                },
            )
        return

    settings = store.get_group_settings(group_id)
    if not store.is_owner(user_id) and user_id in (
        settings.get("groupBlacklist") or []
    ):
        log.info(f"黑名单用户 {user_id} 申请加入群 {group_id}, 自动拒绝(群独立黑名单)")
        if flag:
            await call_api(
                "set_group_add_request",
                {
                    "flag": flag,
                    "sub_type": "add",
                    "approve": False,
                    "reason": "你已被列入黑名单",
                },
            )
        return

    if not settings.get("autoApprove"):
        return

    reject_kw = (
        settings.get("rejectKeywords") or store.config().get("rejectKeywords") or []
    )
    comment = event.comment or ""
    if reject_kw and comment:
        comment_text = re.sub(r"^问题：", "", comment)
        comment_text = re.sub(r"\s*答案：", " ", comment_text)
        matched = next((k for k in reject_kw if k in comment_text), None)
        if matched:
            log.info(
                f"入群审核拒绝: 用户 {user_id}@{group_id} 含拒绝关键词「{matched}」"
            )
            if flag:
                await call_api(
                    "set_group_add_request",
                    {
                        "flag": flag,
                        "sub_type": "add",
                        "approve": False,
                        "reason": "验证信息包含拒绝关键词",
                    },
                )
            return

    rt = get_runtime()
    if comment:
        rt.pending_comments[f"{event.self_id}:{group_id}:{user_id}"] = comment
    log.info(f"自动通过入群申请: 用户 {user_id}@{group_id}")
    if flag:
        await call_api(
            "set_group_add_request", {"flag": flag, "sub_type": "add", "approve": True}
        )


@handler(
    r".*",
    name="groupguard_increase",
    event_types=["notice.group_increase"],
    priority=50,
)
async def on_group_increase(event, match):
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    self_id = str(getattr(event, "self_id", "") or "")
    if not is_valid_qq(user_id):
        log.warning(f"忽略无效进群成员号: {user_id or '(空)'}@{group_id}")
        return
    if not is_valid_qq(self_id):
        log.warning(f"无法确定处理群 {group_id} 入群事件的机器人账号")
        return

    if user_id == self_id:
        return
    if not await is_bot_admin(group_id, self_id):
        log.warning(
            f"新成员 {user_id}@{group_id}: 机器人 {self_id} 非管理员, 跳过入群验证/欢迎"
        )
        return
    if await guard.handle_rejoin_ban(group_id, user_id):
        return

    settings = store.get_group_settings(group_id)
    if not settings.get("enableVerify"):
        await guard.send_welcome_message(group_id, user_id)
        return

    rt = get_runtime()
    key = f"{self_id}:{group_id}:{user_id}"
    comment = rt.pending_comments.pop(key, "")

    if settings.get("skipBotVerify") and verify.is_bot_qq(user_id):
        log.info(f"新成员 {user_id}@{group_id} 属于机器人号段, 跳过入群验证")
        await guard.send_welcome_message(group_id, user_id)
        return

    tpl = settings.get("welcomeMessage")
    if tpl is None or tpl == "":
        tpl = store.config().get("welcomeMessage") or ""
    welcome_text = (
        tpl.replace("{user}", user_id).replace("{group}", group_id) if tpl else ""
    )
    log.info(f"新成员进群: 用户 {user_id}@{group_id}, 发起验证")
    verify.create_verify_session(self_id, group_id, user_id, comment, welcome_text)


@handler(
    r".*", name="groupguard_recall", event_types=["notice.group_recall"], priority=50
)
async def on_group_recall(event, match):
    message_id = event.raw_data.get("message_id")
    await guard.handle_anti_recall(
        str(event.group_id),
        message_id,
        str(event.user_id),
        self_id=str(getattr(event, "self_id", "") or ""),
    )


@handler(r".*", name="groupguard_card", event_types=["notice.group_card"], priority=50)
async def on_group_card(event, match):
    if not is_valid_qq(event.user_id):
        return
    raw_data = getattr(event, "raw_data", None)
    card_known = isinstance(raw_data, dict) and "card_new" in raw_data
    current_card = raw_data.get("card_new") if card_known else None
    if not card_known:
        current_card = getattr(event, "card_new", None)
        card_known = current_card is not None
    if card_known:
        await guard.handle_card_lock_on_message(
            str(event.group_id), str(event.user_id), str(current_card or "")
        )
        return
    await guard.handle_card_lock_check(str(event.group_id), str(event.user_id))


@handler(r".*", name="groupguard_ban", event_types=["notice.group_ban"], priority=50)
async def on_group_ban(event, match):
    if not is_valid_qq(event.user_id):
        return
    duration = event.raw_data.get("duration", 0) if event.sub_type == "ban" else 0
    await asyncio.to_thread(
        store.record_group_ban,
        str(event.group_id),
        str(event.user_id),
        duration,
    )


@handler(
    r".*",
    name="groupguard_decrease",
    event_types=["notice.group_decrease"],
    priority=50,
)
async def on_group_decrease(event, match):
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    self_id = str(getattr(event, "self_id", "") or "")
    if not is_valid_qq(user_id):
        log.warning(f"忽略无效退群成员号: {user_id or '(空)'}@{group_id}")
        return
    rt = get_runtime()
    rt.pending_comments.pop(f"{self_id}:{group_id}:{user_id}", None)
    verify.cancel_session(self_id, group_id, user_id)
    if event.sub_type != "leave":
        return
    remaining = await asyncio.to_thread(
        store.freeze_group_ban_on_leave,
        group_id,
        user_id,
    )
    if remaining:
        log.info(f"禁言用户退群: 用户 {user_id}@{group_id}, 冻结剩余 {remaining} 秒")
    conf = store.config()
    settings = store.get_group_settings(group_id)
    group_enabled = bool(settings.get("leaveBlacklist"))
    if (not conf.get("leaveBlacklist") and not group_enabled) or store.is_owner(
        user_id
    ):
        return
    target = store.ensure_group(group_id) if group_enabled else conf
    key = "groupBlacklist" if group_enabled else "blacklist"
    bl = target.setdefault(key, [])
    if user_id not in bl:
        bl.append(user_id)
        await store.save()
        log.info(f"退群拉黑: 用户 {user_id} 退出群 {group_id}")
