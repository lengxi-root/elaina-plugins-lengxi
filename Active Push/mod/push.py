"""群聊/私聊主动推送核心。"""

import asyncio
import contextlib
import json

from core.base.logger import PLUGIN, get_logger

from . import store

log = get_logger(PLUGIN, '主动推送')

_push_lock = asyncio.Lock()

_RAW_MAX = 1000
_MODES = {'group', 'private'}


def build_button_rows(buttons_cfg):
    """按钮配置 = 框架原生按钮行 [[{text, data/link, enter, ...}, ...], ...],
    原样透传给 build_keyboard; 兼容扁平 [{...}, ...] (视为单行); 空则 None。"""
    raw = buttons_cfg
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or '[]')
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(raw, list) or not raw:
        return None
    rows = [r for r in raw if isinstance(r, list) and r]
    if rows:
        return rows
    flat = [b for b in raw if isinstance(b, dict)]
    return [flat] if flat else None


def _raw(data):
    """序列化发送接口的原始返回 (截断保存)"""
    try:
        s = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        s = str(data)
    return s[:_RAW_MAX]


def _get_bot(appid=''):
    """按 appid 取 bot, 留空/取不到时回退第一个 bot"""
    try:
        from core.bot.manager import _bot_manager_ref

        if not _bot_manager_ref:
            return None
        if appid:
            try:
                bot = _bot_manager_ref.get_bot(appid)
                if bot:
                    return bot
            except Exception:
                pass
        return next(iter(_bot_manager_ref._bots.values()), None)
    except Exception:
        return None


def _bot_appid(bot):
    return str(getattr(bot, 'appid', '') or '')


def get_full_access_groups(appid=''):
    """读取全量群列表 (data.db full_access_groups, 按 appid 各存一份)"""
    bot = _get_bot(appid)
    if bot is None:
        return []
    try:
        rows = bot.log_service.query_data('SELECT group_id FROM full_access_groups ORDER BY first_seen DESC')
        return [r['group_id'] for r in rows if r.get('group_id')]
    except Exception as e:
        log.warning(f'读取全量群失败: {e}')
        return []


def get_private_users(appid=''):
    """读取 data.db members 表中的全部私聊用户。"""
    bot = _get_bot(appid)
    if bot is None:
        return []
    try:
        rows = bot.log_service.query_data('SELECT user_id FROM members ORDER BY user_id')
        return [row['user_id'] for row in rows if row.get('user_id')]
    except Exception as e:
        log.warning(f'读取私聊用户失败: {e}')
        return []


def get_targets(mode, appid=''):
    return get_full_access_groups(appid) if mode == 'group' else get_private_users(appid)


def list_bots():
    """枚举已配置的机器人 (appid + robot_qq), 供面板下拉选择"""
    try:
        from core.base.config import cfg as core_cfg

        out = []
        for b in core_cfg.get_bot_configs() or []:
            appid = str(b.get('appid') or '').strip()
            if appid:
                out.append({'appid': appid, 'qq': str(b.get('robot_qq') or '').strip()})
        return out
    except Exception:
        return []


async def _send(bot, mode, target_id, content, buttons):
    if mode == 'group':
        return await bot.sender.send_to_group(target_id, content, buttons=buttons, skip_suffix=True)
    return await bot.sender.send_to_user(target_id, content, buttons=buttons, skip_suffix=True)


async def send_to_test_target(mode, content, source='web'):
    """发送到当前模式配置的测试目标，测试发送不写入已推送记录。"""
    if mode not in _MODES:
        return False, '未知推送模式'
    cfg = store.get_mode_config(mode)
    target_id = str(cfg.get('test_target') or '').strip()
    if not target_id:
        label = '测试群' if mode == 'group' else '测试用户'
        return False, f'未设置{label}, 请先在面板保存'
    bot = _get_bot(cfg.get('push_appid', ''))
    if bot is None or not getattr(bot, 'sender', None):
        return False, '没有可用的机器人'
    buttons = build_button_rows(cfg.get('buttons'))
    try:
        ok, data, _payload = await _send(bot, mode, target_id, content, buttons)
    except Exception as e:
        ok, data = False, {'message': f'发送异常: {e}'}
    if ok:
        target_label = '测试群' if mode == 'group' else '测试用户'
        return True, f'已发送到{target_label} {target_id}'
    msg = data.get('message', str(data)) if isinstance(data, dict) else str(data)
    return False, f'发送失败: {msg}'


async def broadcast(mode, content, source='web', progress=None, continue_only=False):
    """向全部目标逐个发送主动消息。

    progress: 可选回调 async fn(done, total, ok_count), 每发 20 个目标通知一次。
    """
    if mode not in _MODES:
        return {'ok': False, 'message': '未知推送模式'}
    if _push_lock.locked():
        return {'ok': False, 'message': '已有推送任务进行中, 请稍后再试'}
    async with _push_lock:
        cfg = store.get_mode_config(mode)
        appid = cfg.get('push_appid', '')
        bot = _get_bot(appid)
        if bot is None or not getattr(bot, 'sender', None):
            return {'ok': False, 'message': '没有可用的机器人'}
        resolved_appid = _bot_appid(bot)
        targets = get_targets(mode, appid)
        target_label = '全量群' if mode == 'group' else '私聊用户'
        if not targets:
            return {'ok': False, 'message': f'暂无{target_label}记录'}
        skipped = 0
        if continue_only:
            sent = store.get_sent_target_ids(resolved_appid, mode)
            pending = [target_id for target_id in targets if target_id not in sent]
            skipped = len(targets) - len(pending)
            targets = pending
            if not targets:
                return {
                    'ok': True,
                    'message': f'没有待推送的{target_label}, 已推送记录共 {skipped} 条',
                    'total': 0,
                    'success': 0,
                    'skipped': skipped,
                    'failed': [],
                }
        interval = max(float(cfg.get('interval', 0.5) or 0), 0.1)
        buttons = build_button_rows(cfg.get('buttons'))
        ok_count = 0
        failed = []
        for i, target_id in enumerate(targets, 1):
            try:
                ok, data, _payload = await _send(bot, mode, target_id, content, buttons)
            except Exception as e:
                ok, data = False, {'message': str(e)}
            if ok:
                try:
                    await store.aadd_sent_target(
                        resolved_appid,
                        mode,
                        target_id,
                        content,
                        source,
                        _raw(data),
                    )
                except Exception as e:
                    log.error(f'目标 {target_id} 已送达但写入推送记录失败: {e}')
                    return {
                        'ok': False,
                        'message': f'目标 {target_id} 已送达但记录失败, 已停止后续推送',
                        'total': len(targets),
                        'success': ok_count,
                        'skipped': skipped,
                        'failed': failed,
                    }
                ok_count += 1
            else:
                msg = data.get('message', str(data)) if isinstance(data, dict) else str(data)
                failed.append({'target_id': target_id, 'message': str(msg)[:100]})
            if progress and i % 20 == 0:
                with contextlib.suppress(Exception):
                    await progress(i, len(targets), ok_count)
            if i < len(targets):
                await asyncio.sleep(interval)
        action = '继续推送' if continue_only else '全量推送'
        log.info(f'{target_label}{action}完成: 成功 {ok_count}/{len(targets)}, 跳过 {skipped}')
        return {
            'ok': True,
            'message': f'推送完成: 成功 {ok_count}/{len(targets)}' + (f', 跳过 {skipped}' if skipped else ''),
            'total': len(targets),
            'success': ok_count,
            'skipped': skipped,
            'failed': failed[:20],
        }
