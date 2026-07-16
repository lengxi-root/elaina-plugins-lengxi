"""群聊/私聊主动推送核心。"""

import asyncio
import contextlib
import json
import time

from core.base.logger import PLUGIN, get_logger

from . import store

log = get_logger(PLUGIN, '主动推送')

_push_lock = asyncio.Lock()
_job_task = None
_job_status = {
    'state': 'idle',
    'mode': '',
    'total': 0,
    'done': 0,
    'success': 0,
    'failed_count': 0,
    'skipped': 0,
    'failed': [],
    'error_stats': [],
    'message': '',
    '_started_at': 0.0,
    '_finished_at': 0.0,
}

_RAW_MAX = 1000
_MAX_ERROR_TYPES = 100
_INVALID_PRIVATE_USER_CODES = {'11255', '40054004'}
_PERMANENT_SKIP_PRIVATE_USER_CODES = {'11500', '40054013'}
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


async def _remove_invalid_private_user(bot, mode, target_id, data):
    if (
        mode != 'private'
        or not isinstance(data, dict)
        or str(data.get('code')) not in _INVALID_PRIVATE_USER_CODES
    ):
        return
    try:
        await bot.log_service.db_execute(
            'DELETE FROM members WHERE user_id=?',
            (target_id,),
        )
    except Exception as e:
        log.warning(f'移除失效私聊用户 {target_id} 失败: {e}')


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
    await _remove_invalid_private_user(bot, mode, target_id, data)
    msg = data.get('message', str(data)) if isinstance(data, dict) else str(data)
    return False, f'发送失败: {msg}'


def get_broadcast_status():
    started_at = _job_status.get('_started_at', 0.0)
    finished_at = _job_status.get('_finished_at', 0.0)
    elapsed = max((finished_at or time.monotonic()) - started_at, 0.0) if started_at else 0.0
    done = _job_status.get('done', 0)
    per_second = done / elapsed if elapsed else 0.0
    status = {
        **_job_status,
        'failed': list(_job_status.get('failed') or []),
        'error_stats': [dict(item) for item in _job_status.get('error_stats') or []],
        'elapsed': round(elapsed, 1),
        'average_per_second': round(per_second, 2),
        'average_per_minute': round(per_second * 60, 2),
    }
    status.pop('_started_at', None)
    status.pop('_finished_at', None)
    return status


def start_broadcast(mode, content, source='web', continue_only=False):
    global _job_task
    if mode not in _MODES:
        return {'ok': False, 'message': '未知推送模式'}
    if (_job_task is not None and not _job_task.done()) or _push_lock.locked():
        return {'ok': False, 'message': '已有推送任务进行中, 请稍后再试'}

    error_counts = {}
    _job_status.update(
        {
            'state': 'queued',
            'mode': mode,
            'total': 0,
            'done': 0,
            'success': 0,
            'failed_count': 0,
            'skipped': 0,
            'failed': [],
            'error_stats': [],
            'message': '推送任务已进入后台队列',
            '_started_at': time.monotonic(),
            '_finished_at': 0.0,
        }
    )

    async def progress(done, total, ok_count):
        _job_status.update(
            {
                'state': 'running',
                'total': total,
                'done': done,
                'success': ok_count,
                'failed_count': done - ok_count,
            }
        )

    async def failure(data):
        if isinstance(data, dict):
            code = str(data.get('code') or '')
            message = str(data.get('message') or data)[:100]
        else:
            code = ''
            message = str(data)[:100]
        key = (code, message)
        if key not in error_counts and len(error_counts) >= _MAX_ERROR_TYPES:
            key = ('other', '其他错误')
        error_counts[key] = error_counts.get(key, 0) + 1
        _job_status['error_stats'] = [
            {'code': item_code, 'message': item_message, 'count': count}
            for (item_code, item_message), count in sorted(
                error_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ]

    async def run():
        try:
            result = await broadcast(
                mode,
                content,
                source=source,
                progress=progress,
                continue_only=continue_only,
                progress_every=1,
                failure=failure,
            )
            _job_status['_finished_at'] = time.monotonic()
            _job_status.update(
                {
                    'state': 'completed' if result.get('ok') else 'failed',
                    'total': result.get('total', 0),
                    'done': result.get('total', 0) if result.get('ok') else _job_status['done'],
                    'success': result.get('success', 0),
                    'failed_count': result.get('failed_count', 0),
                    'skipped': result.get('skipped', 0),
                    'failed': result.get('failed', []),
                    'message': result.get('message', '推送结束'),
                }
            )
        except asyncio.CancelledError:
            _job_status.update(
                {
                    'state': 'cancelled',
                    'message': '推送任务已取消',
                    '_finished_at': time.monotonic(),
                }
            )
            raise
        except Exception as e:
            log.exception(f'后台推送任务异常: {e}')
            _job_status.update(
                {
                    'state': 'failed',
                    'message': f'推送异常: {e}',
                    '_finished_at': time.monotonic(),
                }
            )

    _job_task = asyncio.create_task(run(), name=f'broadcast-{mode}')
    return {'ok': True, 'message': '推送任务已在后台启动'}


async def stop_broadcast():
    global _job_task
    task = _job_task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _job_task = None


async def broadcast(
    mode,
    content,
    source='web',
    progress=None,
    continue_only=False,
    progress_every=20,
    failure=None,
):
    """向全部目标异步并发发送主动消息。

    progress: 可选回调 async fn(done, total, ok_count)。
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
                    'failed_count': 0,
                    'skipped': skipped,
                    'failed': [],
                }
        interval = min(max(float(cfg.get('interval', 0.5) or 0), 0.1), 10)
        concurrency = min(max(int(cfg.get('concurrency', 20) or 1), 1), 100)
        progress_every = max(int(progress_every or 1), 1)
        buttons = build_button_rows(cfg.get('buttons'))
        ok_count = 0
        failed = []
        completed = 0
        fatal_error = ''
        target_iter = iter(targets)
        rate_lock = asyncio.Lock()
        next_launch = asyncio.get_running_loop().time()

        async def wait_launch_slot():
            nonlocal next_launch
            async with rate_lock:
                now = asyncio.get_running_loop().time()
                wait = next_launch - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = asyncio.get_running_loop().time()
                next_launch = max(next_launch + interval, now)

        async def worker():
            nonlocal ok_count, completed, fatal_error
            while not fatal_error:
                target_id = next(target_iter, None)
                if target_id is None:
                    return
                await wait_launch_slot()
                if fatal_error:
                    return
                try:
                    ok, data, _payload = await _send(bot, mode, target_id, content, buttons)
                except Exception as e:
                    ok, data = False, {'message': str(e)}
                record_error = ''
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
                        fatal_error = f'目标 {target_id} 已送达但记录失败, 已停止后续推送'
                        return
                    ok_count += 1
                else:
                    msg = data.get('message', str(data)) if isinstance(data, dict) else str(data)
                    await _remove_invalid_private_user(bot, mode, target_id, data)
                    code = str(data.get('code')) if isinstance(data, dict) else ''
                    if mode == 'private' and code in _PERMANENT_SKIP_PRIVATE_USER_CODES:
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
                            log.error(f'目标 {target_id} 跳过记录写入失败: {e}')
                            record_error = f'目标 {target_id} 跳过记录失败, 已停止后续推送'
                    if failure:
                        with contextlib.suppress(Exception):
                            await failure(data)
                    if len(failed) < 20:
                        failed.append({'target_id': target_id, 'message': str(msg)[:100]})
                completed += 1
                if progress and (completed % progress_every == 0 or completed == len(targets)):
                    with contextlib.suppress(Exception):
                        await progress(completed, len(targets), ok_count)
                if not ok and record_error:
                    fatal_error = record_error
                    return

        if progress:
            with contextlib.suppress(Exception):
                await progress(0, len(targets), 0)
        await asyncio.gather(*(worker() for _ in range(min(concurrency, len(targets)))))
        if fatal_error:
            if progress:
                with contextlib.suppress(Exception):
                    await progress(completed, len(targets), ok_count)
            return {
                'ok': False,
                'message': fatal_error,
                'total': len(targets),
                'success': ok_count,
                'failed_count': completed - ok_count,
                'skipped': skipped,
                'failed': failed[:20],
            }
        action = '继续推送' if continue_only else '全量推送'
        failed_count = completed - ok_count
        log.info(f'{target_label}{action}完成: 成功 {ok_count}, 失败 {failed_count}, 跳过 {skipped}')
        return {
            'ok': True,
            'message': f'推送完成: 成功 {ok_count}, 失败 {failed_count}' + (f', 跳过 {skipped}' if skipped else ''),
            'total': len(targets),
            'success': ok_count,
            'failed_count': failed_count,
            'skipped': skipped,
            'failed': failed[:20],
        }
