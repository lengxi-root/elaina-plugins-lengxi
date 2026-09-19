"""登录态账号池 / 微信扫码登录 (Phase 2)"""

import time
import asyncio

from ..lib.handlers import handler
from ..lib import render, auth as A
from ..lib import qq_login as QQ

# 每个用户同时仅允许一个进行中的扫码会话
_login_locks: dict = {}
_LOGIN_COOLDOWN = 10
_bg_tasks: set = set()
_qq_sessions: dict = {}


def _get_runtime():
    from .. import get_runtime
    return get_runtime()


def _mask(value: str, keep_start: int = 3, keep_end: int = 3) -> str:
    text = str(value or "")
    if len(text) <= keep_start + keep_end:
        return text
    return f"{text[:keep_start]}****{text[-keep_end:]}"


def _track(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


# ==================== 扫码登录 ====================

async def _start_login(event, *, is_global: bool):
    rt = _get_runtime()
    if not rt:
        return
    qq = str(event.user_id)
    now = time.time()
    if qq in _login_locks and now - _login_locks[qq] < _LOGIN_COOLDOWN:
        return await event.reply(f"<@{event.user_id}> 你刚发起过扫码, 请稍后再试")
    _login_locks[qq] = now

    try:
        session_info = await A.create_login_session()
    except Exception:
        _login_locks.pop(qq, None)
        return await event.reply(f"<@{event.user_id}> 获取登录二维码失败, 请稍后重试")

    ok = await render.send_html(
        event, "scanCodeLogin.html", {"qrCodeFile": session_info["qrcodeBase64"]},
        caption=f"<@{event.user_id}> 请用王者营地App扫码登录 (120秒内有效)",
        name_hint="login")
    if not ok:
        _login_locks.pop(qq, None)
        return await event.reply(f"<@{event.user_id}> 生成登录二维码图片失败, 请稍后重试")

    sender = event._sender
    _track(_poll_and_finish(event, sender, qq, session_info, is_global))


async def _poll_and_finish(event, sender, qq, session_info, is_global):
    rt = _get_runtime()
    try:
        result = await A.wait_for_login(session_info, timeout_s=120)
    except A.LoginError as e:
        await _safe_reply(sender, event, f"<@{qq}> 扫码登录失败: {e}")
        return
    except Exception:
        await _safe_reply(sender, event, f"<@{qq}> 扫码登录异常, 请稍后重试")
        return
    finally:
        _login_locks.pop(qq, None)

    account = result["account"]
    user_id = account.get("userId")
    nickname = account.get("nickname") or account.get("snsnickname") or user_id
    try:
        if is_global:
            rt.auth.upsert_global_account(account)
            await _safe_reply(
                sender, event,
                f"全局登录态设置成功\n营地: {nickname} ({_mask(user_id)})\n"
                f"已设为全局默认, 之后所有查询都用这个登录态")
        else:
            account = {**account, "ownerBotUserId": qq, "resetAuthState": True}
            rt.auth.upsert_account(account)
            rt.db.add_binding(qq, str(user_id), role_name=str(nickname or ""))
            await _safe_reply(
                sender, event,
                f"<@{qq}> 扫码登录成功\n营地: {nickname} ({_mask(user_id)})\n"
                f"已绑定该营地ID, 之后查询优先用这个登录态")
    except Exception as e:
        await _safe_reply(sender, event, f"<@{qq}> 登录成功但保存登录态失败: {e}")


async def _safe_reply(sender, event, content, buttons=None):
    try:
        await sender.reply(event, content, buttons=buttons)
    except Exception:
        pass


@handler(r'^王者(?:wx|微信|扫码)登录$', name='王者wx登录', desc='微信扫码登录王者营地, 记入个人登录态')
async def cmd_wx_login(event, match):
    await _start_login(event, is_global=False)


@handler(r'^王者(?:wx|微信|扫码)全局登录$', name='王者wx全局登录',
         desc='微信扫码登录并设为全局默认登录态', owner_only=True)
async def cmd_wx_global_login(event, match):
    await _start_login(event, is_global=True)


async def _start_qq_login(event, *, is_global: bool):
    rt = _get_runtime()
    if not rt:
        return
    qq = str(event.user_id)
    if qq in _qq_sessions:
        return await event.reply(f"<@{qq}> 当前已有 QQ 扫码登录任务，请先完成或等待超时")
    try:
        session = await QQ.create_login_session()
    except QQ.LoginError as exc:
        return await event.reply(f"<@{qq}> {exc}")
    except Exception as exc:
        return await event.reply(f"<@{qq}> 获取 QQ 登录二维码失败: {exc}")
    _qq_sessions[qq] = session
    try:
        caption = f"<@{qq}> 请用手机 QQ 扫码并确认授权登录王者营地 (180秒内有效)"
        if hasattr(event, 'reply_image'):
            await event.reply_image(session.qrcode, caption)
        else:
            qr = __import__('base64').b64encode(session.qrcode).decode('ascii')
            await event.reply(f"{caption}\nbase64://{qr}")
    except Exception:
        _qq_sessions.pop(qq, None)
        await session.close()
        return await event.reply(f"<@{qq}> 发送 QQ 登录二维码失败")
    _track(_poll_qq_login(event, session, qq, is_global))


async def _poll_qq_login(event, session, qq: str, is_global: bool):
    async def on_status(status):
        if status == "scanned":
            try:
                await event.reply(f"<@{qq}> 已扫码, 请在手机 QQ 上确认授权")
            except Exception:
                pass

    try:
        result = await QQ.wait_for_login(session, timeout_s=180, on_status=on_status)
        account = result['account']
        if is_global:
            saved = _get_runtime().auth.upsert_global_account(account)
            await event.reply(f"<@{qq}> QQ 全局登录成功\n营地: {saved.get('nickname') or saved.get('userId')} ({_mask(saved.get('userId'))})")
        else:
            account = {**account, 'ownerBotUserId': qq, 'resetAuthState': True}
            saved = _get_runtime().auth.upsert_account(account)
            _get_runtime().db.add_binding(qq, str(saved.get('userId')), role_name=str(saved.get('nickname') or ''))
            await event.reply(f"<@{qq}> QQ 登录成功\n营地: {saved.get('nickname') or saved.get('userId')} ({_mask(saved.get('userId'))})\n已绑定当前营地ID")
    except QQ.LoginError as exc:
        await event.reply(f"<@{qq}> QQ 扫码登录失败: {exc}")
    except Exception as exc:
        await event.reply(f"<@{qq}> QQ 登录异常: {exc}")
    finally:
        _qq_sessions.pop(qq, None)


@handler(r"^王者QQ登录$", name="王者QQ登录",
         desc="使用手机QQ扫码登录王者营地, 记入个人登录态")
async def cmd_qq_login(event, match):
    await _start_qq_login(event, is_global=False)


@handler(r"^王者QQ全局登录$", name="王者QQ全局登录",
         desc="使用手机QQ扫码登录并设为全局默认登录态",
         owner_only=True)
async def cmd_qq_global_login(event, match):
    await _start_qq_login(event, is_global=True)


# ==================== 账号池管理 ====================

def _build_overview(rt) -> dict:
    accounts = rt.auth.list_accounts()

    owners: dict = {}
    unowned: list = []
    global_count = valid_count = invalid_count = 0

    for acc in accounts:
        uid = str(acc.get("userId"))
        invalid = bool(acc.get("authInvalid"))
        if invalid:
            invalid_count += 1
        else:
            valid_count += 1
        if acc.get("isGlobalDefault"):
            global_count += 1

        owner_qq = str(acc.get("ownerBotUserId") or "")
        if owner_qq:
            owners.setdefault(owner_qq, []).append(acc)
        else:
            nickname = (acc.get("nickname") or acc.get("snsnickname")
                        or ("全局默认" if acc.get("isGlobalDefault") else "未绑定属主"))
            unowned.append({
                "maskedCampUserId": _mask(uid),
                "nickname": nickname,
                "statusClass": "invalid" if invalid else "valid",
            })

    owner_sections = []
    for owner_qq, accs in owners.items():
        current_camp = rt.db.get_current(owner_qq) or ""
        valid_n = sum(0 if a.get("authInvalid") else 1 for a in accs)
        invalid_n = sum(1 if a.get("authInvalid") else 0 for a in accs)
        entries = []
        for a in accs:
            uid = str(a.get("userId"))
            is_current = bool(current_camp and uid == str(current_camp))
            if a.get("authInvalid"):
                badge_class, badge_text = "invalid", "失效"
            else:
                badge_class, badge_text = "valid", "有效"
            entries.append({
                "maskedCampUserId": _mask(uid),
                "isCurrent": is_current,
                "badgeClass": badge_class,
                "badgeText": badge_text,
            })
        owner_sections.append({
            "maskedQqId": _mask(owner_qq),
            "currentMaskedCampId": _mask(current_camp) if current_camp else "-",
            "tokenCount": len(accs),
            "validTokenCount": valid_n,
            "invalidTokenCount": invalid_n,
            "uidEntries": entries,
        })

    displayed = owner_sections[:60]
    overview_cards = [
        {"label": "绑定QQ", "value": str(len(owner_sections)), "tone": "blue"},
        {"label": "登录态总数", "value": str(len(accounts)), "tone": "purple"},
        {"label": "可用", "value": str(valid_count), "tone": "green"},
        {"label": "失效", "value": str(invalid_count), "tone": "red"},
        {"label": "全局默认", "value": str(global_count), "tone": "blue"},
    ]
    return {
        "timestamp": time.strftime("%Y/%m/%d %H:%M:%S"),
        "overviewCards": overview_cards,
        "ownerSections": displayed,
        "omittedOwnerCount": max(0, len(owner_sections) - len(displayed)),
        "unownedAccounts": unowned,
    }


@handler(r'^王者(?:账号池|用户统计|登录态总览)$', name='王者账号池',
         desc='查看登录态账号池总览', owner_only=True)
async def cmd_pool_overview(event, match):
    rt = _get_runtime()
    if not rt:
        return
    data = _build_overview(rt)
    ok = await render.send_html(event, "authPoolOverview.html", data,
                                caption=f"<@{event.user_id}> 登录态账号池总览",
                                name_hint="authpool")
    if not ok:
        await event.reply(
            f"<@{event.user_id}> 账号池: 登录态 {len(rt.auth.list_accounts())} 个, "
            f"渲染失败")


@handler(r'^王者清理失效$', name='王者清理失效', desc='清理失效的非全局登录态', owner_only=True)
async def cmd_clear_invalid(event, match):
    rt = _get_runtime()
    if not rt:
        return
    res = rt.auth.clear_invalid_accounts()
    removed = len(res.get("removedAccounts") or [])
    skipped = len(res.get("skippedGlobalAccounts") or [])
    msg = f"<@{event.user_id}> 已清理 {removed} 个失效登录态"
    if skipped:
        msg += f"\n保留 {skipped} 个失效的全局默认账号 (请用全局登录重新覆盖)"
    await event.reply(msg)
