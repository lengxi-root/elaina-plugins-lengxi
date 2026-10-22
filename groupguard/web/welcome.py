"""只通过框架登录鉴权访问的通用欢迎管理接口。"""

from types import SimpleNamespace

from ..services import welcome
from ..storage import welcome as store
from ..storage.audit import record_web_action


def routes():
    return [("GET", "welcome", get_welcome, True), ("PUT", "welcome", save_welcome, True),
            ("POST", "welcome/review", review_welcome, True), ("POST", "welcome/test", test_welcome, True),
            ("PUT", "welcome/list", save_list, True), ("POST", "welcome/extract", extract, True)]


async def _context(request, query=False):
    from .routes import _json, _require_managed_context
    body = request.query if query else await _json(request)
    group_id, appid, _ = _require_managed_context(body.get("group_id"), body.get("appid"))
    return body, group_id, appid


async def _run(request, action, query=False):
    from .routes import _failure, _success
    try:
        body, group_id, appid = await _context(request, query)
        result = await action(body, group_id, appid)
        return _success(result)
    except (ValueError, KeyError, TypeError) as error:
        return _failure(str(error))


async def get_welcome(request):
    async def action(body, gid, appid):
        return store.snapshot(appid, gid)
    return await _run(request, action, True)


async def save_welcome(request):
    async def action(body, gid, appid):
        config = body.get("config", {})
        settings = body.get("settings", {})
        # 配置先全量校验，避免错误表单出现部分保存。
        from ..storage.welcome import DEFAULTS, SETTING_DEFAULTS, _integer
        if not isinstance(config, dict) or set(config) - set(DEFAULTS):
            raise ValueError("欢迎配置包含不支持的字段")
        if not isinstance(settings, dict) or set(settings) - set(SETTING_DEFAULTS):
            raise ValueError("监控配置包含不支持的字段")
        merged = {**store.get_settings(appid), **settings}
        if not isinstance(merged["enabled"], bool) or merged["list_mode"] not in ("all", "whitelist", "blacklist"):
            raise ValueError("监控设置无效")
        _integer(merged["inactive_days"], 1, 365, "不活跃天数")
        store.save_config(appid, gid, config)
        store.save_settings(appid, settings)
        record_web_action(gid, "config_change", True, appid=appid, details={"section": "welcome"})
        return store.snapshot(appid, gid)
    return await _run(request, action)


async def review_welcome(request):
    async def action(body, gid, appid):
        if not isinstance(body.get("approve"), bool) or type(body.get("review_id")) is not int:
            raise ValueError("审核编号或审核结果无效")
        store.review(appid, gid, body["review_id"], body["approve"], body.get("note", ""))
        record_web_action(gid, "welcome_review", True, appid=appid,
                          details={"review_id": body["review_id"], "approved": body["approve"]})
        return store.snapshot(appid, gid)
    return await _run(request, action)


async def test_welcome(request):
    async def action(body, gid, appid):
        from core.application import get_app
        app = get_app()
        bot = app.get_bot(appid) if app else None
        if bot is None:
            raise ValueError("机器人未运行")
        template = store.validate_template(body.get("template"))
        event = SimpleNamespace(appid=appid, group_id=gid, user_id="", message_id="", sender=bot.sender)
        from ..services.templates import validate_template as validate_reply
        buttons = validate_reply("welcome", {"content": "", "buttons": body.get("buttons", [])})["buttons"]
        if not await welcome.send(event, {"测试成员": "测试成员"}, template, buttons):
            raise ValueError("测试发送失败，请查看发送记录")
        return {"sent": True}
    return await _run(request, action)


async def save_list(request):
    async def action(body, gid, appid):
        if not isinstance(body.get("listed"), bool):
            raise ValueError("群列表状态必须为布尔值")
        store.set_listed(appid, gid, body["listed"])
        record_web_action(gid, "config_change", True, appid=appid, details={"section": "welcome_list"})
        return store.snapshot(appid, gid)
    return await _run(request, action)


async def extract(request):
    async def action(body, gid, appid):
        from core.application import get_app
        from .routes import _group_catalog
        app = get_app()
        bot = app.get_bot(appid) if app else None
        if bot is None:
            raise ValueError("机器人未运行")
        groups = {item["group_id"] for item in _group_catalog() if item["appid"] == appid}
        added = await welcome.extract_inactive(appid, bot.log_service, groups)
        return {**store.snapshot(appid, gid), "added": added}
    return await _run(request, action)
