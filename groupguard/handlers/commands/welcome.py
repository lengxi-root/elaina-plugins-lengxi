"""欢迎通用命令；群内文案提交后交由面板审核。"""

from core.plugin.decorators import handler

from ...services import welcome
from ...services.permissions import get_event_member_role, is_group_admin
from ...storage import welcome as store


@handler(r"^(开启欢迎|关闭欢迎)$", name="欢迎开关", group_only=True)
async def cmd_welcome_toggle(event, match):
    if not is_group_admin(event, await get_event_member_role(event)):
        return
    enabled = match.group(1) == "开启欢迎"
    store.save_config(event.appid, event.group_id, {"enabled": enabled})
    await event.reply("已开启本群欢迎" if enabled else "已关闭本群欢迎")


@handler(r"^设置欢迎语\s+(.+)$", name="设置欢迎语", group_only=True)
async def cmd_welcome_submit(event, match):
    if not is_group_admin(event, await get_event_member_role(event)):
        return
    try:
        review_id = store.submit_review(event.appid, event.group_id, event.user_id, match.group(1))
    except ValueError as error:
        await event.reply(str(error))
        return
    await event.reply(f"欢迎语已提交审核（#{review_id}），通过后生效。")


@handler(r"^(开启|关闭)群成员入群监控$", name="欢迎监控开关", owner_only=True)
async def cmd_welcome_monitor(event, match):
    enabled = match.group(1) == "开启"
    store.save_settings(event.appid, {"enabled": enabled})
    await event.reply("已开启入群欢迎监控" if enabled else "已关闭入群欢迎监控")


@handler(r"^测试发送(合并)?模板$", name="测试欢迎模板", owner_only=True, group_only=True)
async def cmd_welcome_test(event, match):
    config = store.get_config(event.appid, event.group_id)
    members = {str(event.user_id): str(getattr(event, "username", "") or event.user_id)}
    if match.group(1):
        record = await event.get_group_record(event.group_id)
        for member in (record or {}).get("users", [])[:3]:
            uid = str(member.get("userid", "")) if isinstance(member, dict) else str(member)
            if uid:
                members[uid] = uid
    if not await welcome.send(event, members, config["recall_template"] if config["mode"] == "recall" else config["template"], config["recall_buttons"] if config["mode"] == "recall" else config["buttons"]):
        await event.reply("欢迎模板发送失败，请在面板查看发送记录。")


@handler(r"^提取群列表$", name="提取不活跃群", owner_only=True)
async def cmd_welcome_extract(event, match):
    from ...web.routes import _group_catalog
    from core.application import get_app
    bot = get_app()._bots.get(str(event.appid))
    if bot is None:
        return
    groups = {item["group_id"] for item in _group_catalog() if item["appid"] == str(event.appid)}
    added = await welcome.extract_inactive(event.appid, bot.log_service, groups)
    await event.reply(f"群列表提取完成，新增 {added} 个不活跃群。")
