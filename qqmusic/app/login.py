"""扫码登录、个人状态与账号数量查询。"""

from datetime import datetime, timedelta, timezone

from core.message.event import Event
from core.plugin.decorators import handler

from .common import _BTN, _LOGIN_BTN, _label, _reply, _reply_api_error
from .qr_image import QRImageError, login_qr_markdown
from .runtime import accounts


@handler(r'^音乐在线账号数量$', name='音乐在线账号数量', desc='查看非绿钻和绿钻账号数量')
async def account_count(event, match):
    try:
        counts = accounts.account_counts(event)
    except Exception as exc:  # noqa: BLE001 - 保持消息异常边界一致
        return await _reply_api_error(event, exc, '音乐在线账号数量')
    lines = [
        f'<@{event.user_id}> 音乐在线账号数量',
        f'非绿钻账号：{counts["normal"]} 个',
        f'绿钻账号：{counts["vip"]} 个',
    ]
    if counts['pending']:
        lines.append(f'待核验账号：{counts["pending"]} 个')
    lines.append(f'合计：{counts["total"]} 个')
    await _reply(event, '\n'.join(lines), buttons=_LOGIN_BTN)


@handler(r'^音乐登录状态$', name='音乐登录状态', desc='查看QQ音乐登录及自动续期状态')
async def login_status(event, match):
    try:
        status = await accounts.status(event)
    except Exception as exc:  # noqa: BLE001 - 边界仅记录异常类型，防止泄露请求票据
        return await _reply_api_error(event, exc, '音乐登录状态')
    states = {
        'waiting': '等待扫码',
        'scanned': '已扫码，请在手机 QQ 上确认',
        'confirming': '正在完成登录',
        'logged_in': '登录成功',
        'expired': '二维码已过期',
        'rejected': '登录已取消',
        'failed': '登录失败',
        'invalid': '账号已失效，请重新扫码登录',
        'cancelled': '登录已取消',
    }
    if status['login_started']:
        state = states.get(status['login_status'], '正在登录')
    elif status['logged_in']:
        state = f'已登录（账号 {status["uin"]}）'
    else:
        state = '未登录，请发送 音乐扫码登录'

    lines = [f'<@{event.user_id}> 音乐登录状态', f'登录状态：{state}']
    if status['nickname']:
        lines.append(f'昵称：{_label(status["nickname"])}')
    if status['login_status'] and not status['login_started']:
        lines.append(f'最近扫码：{states.get(status["login_status"], "未知")}')
    if status['logged_in']:
        pool = {'vip': '绿钻及以上', 'normal': '非绿钻', 'pending': '待核验'}.get(status['pool'], '待核验')
        lines.append(f'账号池：{pool}')
    if status.get('check_error'):
        lines.append('状态核验暂未完成，已保留账号等待重试')
    lines.append(f'自动续期：{"开启" if status["auto_refresh"] else "关闭"}')
    lines.append(f'可刷新凭据：{"是" if status["has_refresh"] else "否"}')
    if status['expires_at']:
        expires = datetime.fromtimestamp(status['expires_at'], timezone(timedelta(hours=8)))
        lines.append(f'凭据到期时间：{expires:%Y-%m-%d %H:%M:%S}（北京时间）')
    if status['refresh_error']:
        lines.append('最近续期失败，请重新发送 音乐扫码登录')
    await _reply(event, '\n'.join(lines), buttons=_BTN)


@handler(r'^音乐扫码登录$', name='音乐扫码登录', desc='扫码登录QQ音乐', direct_only=True)
async def scan_login(event, match):
    uid = str(event.user_id)
    target = Event()
    for name in (
        'appid', 'event_type', 'event_id', 'message_id', 'user_id', 'raw_user_id',
        'group_id', 'guild_id', 'channel_id', 'is_direct', 'is_channel',
        '_reply_log_cb', '_reply_plugin_name',
    ):
        setattr(target, name, getattr(event, name, None))
    target._sender = event.sender

    async def notify_result(qr):
        messages = {
            'logged_in': '音乐登录成功，可以点歌了',
            'expired': '音乐登录二维码已过期，请重新发送 音乐扫码登录',
            'rejected': '音乐扫码登录已取消，请重新发送 音乐扫码登录',
        }
        message = messages.get(qr.status, '音乐登录失败，请稍后重新发送 音乐扫码登录')
        await _reply(target, f'<@{uid}> {message}', buttons=_BTN)

    qr = None
    try:
        qr = await accounts.begin_login(event, on_complete=notify_result)
        image = await login_qr_markdown(event, qr.image)
        if qr.status not in ('waiting', 'scanned', 'confirming'):
            return
        sent = await _reply(event, f'<@{uid}> 请使用QQ扫码登录\n\n{image}', force_verify_image_resource=True)
        if sent is not None:
            return
        await accounts.cancel_login(event, qr)
        await _reply(event, f'<@{uid}> 登录二维码发送失败，请重新发送 音乐扫码登录', buttons=_BTN)
    except QRImageError as exc:
        if qr is not None:
            await accounts.cancel_login(event, qr)
        await _reply(event, f'<@{uid}> {exc}', buttons=_BTN)
    except Exception as exc:  # noqa: BLE001 - 边界仅记录异常类型，防止泄露请求票据
        if qr is not None:
            await accounts.cancel_login(event, qr)
        await _reply_api_error(event, exc, '音乐登录')
