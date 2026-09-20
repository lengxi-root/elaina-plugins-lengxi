"""Compare member overlap and QQ levels across QQ groups from the Web panel."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from aiohttp import ContentTypeError, web

from core.plugins import current_plugin, get_api, register_page, register_route

__plugin_meta__ = {
    'name': '重复群成员查询',
    'version': '1.1.1',
    'author': 'ElainaQQ',
    'description': '在 Web 面板选择多个群，查询并汇总同时存在于多个群的成员，也可按 QQ 等级筛出低等级成员并移出群。',
}

_ctx = current_plugin()
_MAX_GROUPS = 50
_FETCH_CONCURRENCY = 4
_MAX_KICK_TARGETS = 200
_KICK_CONCURRENCY = 4
_MIN_QQ_LEVEL = 1
_MAX_QQ_LEVEL = 256
# 逐个补查等级要打服务端接口 (QLinux/Lagrange 的陌生人资料是 OIDB 单查),
# QQ 会按频率限流 (错误 153 queries beyond limit), 所以默认只少量抽查、串行并留间隔。
_MAX_LEVEL_PROBES = 30
_MAX_LEVEL_PROBES_LIMIT = 500
_LEVEL_PROBE_INTERVAL = 1.0
_RATE_LIMIT_HINTS = (
    'beyond limit',
    'queries beyond',
    'too many request',
    'rate limit',
    '频繁',
    '限流',
    '超出限制',
)
# 群成员数据里的 level 是群等级, 只有 qq_level/qqLevel 才是 QQ 等级
_MEMBER_LEVEL_FIELDS = ('qq_level', 'qqLevel')
# 资料类接口 (OneBot v11 的 get_stranger_info.level 即 QQ 等级)
_PROFILE_LEVEL_FIELDS = ('qq_level', 'qqLevel', 'level')

register_page(
    key='group-member-overlap',
    label='重复群成员',
    source='plugin',
    source_name=_ctx.name,
    html_file=_ctx.get_resource_path('panel.html'),
    icon='people',
)


def _error(message: str, *, status: int = 400, **fields: Any) -> web.Response:
    return web.json_response(
        {'success': False, 'message': message, 'error': message, **fields},
        status=status,
    )


def _onebot_data(response: Any, action_label: str) -> Any:
    if not isinstance(response, dict):
        raise RuntimeError(f'{action_label}未返回有效响应')
    if response.get('status') != 'ok' or str(response.get('retcode', -1)) != '0':
        detail = response.get('wording') or response.get('message') or 'OneBot 接口调用失败'
        raise RuntimeError(f'{action_label}失败：{detail}')
    return response.get('data')


def _identity(value: Any, label: str) -> str:
    text = str(value or '').strip()
    if not text or not text.isdigit() or len(text) > 20:
        raise ValueError(f'{label}格式无效')
    return text


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _level_value(value: Any) -> int:
    """QQ 等级可能是数字, 也可能是 {crownNum, sunNum, moonNum, starNum} 结构。"""
    if isinstance(value, dict):
        return (
            _integer(value.get('crownNum')) * 64
            + _integer(value.get('sunNum')) * 16
            + _integer(value.get('moonNum')) * 4
            + _integer(value.get('starNum'))
        )
    level = _integer(value)
    return level if level > 0 else 0


def _level_from(payload: Any, fields: tuple[str, ...]) -> int:
    """按候选字段名从接口数据中取 QQ 等级, 取不到返回 0。"""
    if not isinstance(payload, dict):
        return 0
    for key in fields:
        level = _level_value(payload.get(key))
        if level > 0:
            return level
    return 0


def _level_threshold(value: Any) -> int:
    text = str(value).strip() if value is not None else ''
    if not text:
        raise ValueError('请输入 QQ 等级阈值')
    try:
        threshold = int(text)
    except ValueError as exc:
        raise ValueError('QQ 等级阈值必须是整数') from exc
    if not _MIN_QQ_LEVEL <= threshold <= _MAX_QQ_LEVEL:
        raise ValueError(f'QQ 等级阈值需在 {_MIN_QQ_LEVEL} - {_MAX_QQ_LEVEL} 之间')
    return threshold


def _probe_limit(value: Any) -> int:
    """逐个补查的人数上限, 0 表示只用列表自带的等级。"""
    if value is None or str(value).strip() == '':
        return _MAX_LEVEL_PROBES
    try:
        limit = int(str(value).strip())
    except ValueError as exc:
        raise ValueError('逐个补查上限必须是整数') from exc
    if limit < 0:
        raise ValueError('逐个补查上限不能为负数')
    return min(limit, _MAX_LEVEL_PROBES_LIMIT)


def _is_rate_limited(message: str) -> bool:
    text = str(message or '').casefold()
    return any(hint in text for hint in _RATE_LIMIT_HINTS)


async def _json_object(request: web.Request) -> dict[str, Any]:
    if request.content_length is not None and request.content_length > 64 * 1024:
        raise ValueError('请求正文不能超过 64 KB')
    try:
        body = await request.json()
    except (ContentTypeError, ValueError) as exc:
        raise ValueError('请求正文必须是 JSON 对象') from exc
    if not isinstance(body, dict):
        raise ValueError('请求正文必须是 JSON 对象')
    return body


@register_route('GET', '/api/ext/group-member-overlap/groups', timeout=45)
async def list_groups(request: web.Request) -> web.Response:
    try:
        self_id = _identity(request.query.get('self_id'), '机器人账号')
        response = await get_api().get_group_list(no_cache=True, self_id=self_id)
        raw_groups = _onebot_data(response, '获取群列表')
        if not isinstance(raw_groups, list):
            raise RuntimeError('获取群列表未返回数组')
    except ValueError as exc:
        return _error(str(exc))
    except RuntimeError as exc:
        return _error(str(exc), status=502)

    groups = []
    for item in raw_groups:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get('group_id') or '').strip()
        if not group_id:
            continue
        groups.append(
            {
                'group_id': group_id,
                'group_name': str(item.get('group_name') or group_id),
                'member_count': _integer(item.get('member_count')),
                'max_member_count': _integer(item.get('max_member_count')),
            }
        )
    groups.sort(key=lambda item: (item['group_name'].casefold(), item['group_id']))
    return web.json_response({'success': True, 'groups': groups})


def _member_name(member: dict[str, Any]) -> str:
    return str(member.get('card') or member.get('nickname') or member.get('user_id') or '')


def _member_entry(member: dict[str, Any], group_id: str) -> dict[str, Any]:
    return {
        'group_id': group_id,
        'card': str(member.get('card') or ''),
        'nickname': str(member.get('nickname') or ''),
        'role': str(member.get('role') or 'member'),
        'join_time': _integer(member.get('join_time')),
        'last_sent_time': _integer(member.get('last_sent_time')),
    }


async def _fetch_members(
    self_id: str,
    group_id: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, list[dict[str, Any]] | None, str]:
    try:
        async with semaphore:
            response = await get_api().get_group_member_list(
                int(group_id),
                no_cache=True,
                self_id=self_id,
            )
        data = _onebot_data(response, f'获取群 {group_id} 成员')
        if not isinstance(data, list):
            raise RuntimeError(f'群 {group_id} 的成员数据不是数组')
        return group_id, [item for item in data if isinstance(item, dict)], ''
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _ctx.log.warning('获取群 %s 成员失败: %s', group_id, exc)
        return group_id, None, str(exc)


async def _fetch_group_members(
    self_id: str,
    group_ids: list[str],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)
    fetched = await asyncio.gather(
        *(_fetch_members(self_id, group_id, semaphore) for group_id in group_ids)
    )
    successful: dict[str, list[dict[str, Any]]] = {}
    failed: list[dict[str, str]] = []
    for group_id, members, error in fetched:
        if members is None:
            failed.append({'group_id': group_id, 'message': error})
        else:
            successful[group_id] = members
    return successful, failed


def _collect_appearances(
    successful: dict[str, list[dict[str, Any]]],
    self_id: str,
    exclude_self: bool,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], dict[str, int], int]:
    """按用户汇总各群成员记录，并顺带收集昵称与 QQ 等级。"""
    appearances: dict[str, list[dict[str, Any]]] = defaultdict(list)
    names: dict[str, str] = {}
    levels: dict[str, int] = {}
    total_members = 0
    for group_id, members in successful.items():
        seen_in_group: set[str] = set()
        for member in members:
            user_id = str(member.get('user_id') or '').strip()
            if not user_id or user_id in seen_in_group:
                continue
            seen_in_group.add(user_id)
            if exclude_self and user_id == self_id:
                continue
            total_members += 1
            appearances[user_id].append(_member_entry(member, group_id))
            candidate = _member_name(member)
            if candidate and (user_id not in names or member.get('card')):
                names[user_id] = candidate
            level = _level_from(member, _MEMBER_LEVEL_FIELDS)
            if level > levels.get(user_id, 0):
                levels[user_id] = level
    return appearances, names, levels, total_members


@register_route('POST', '/api/ext/group-member-overlap/compare', timeout=180)
async def compare_members(request: web.Request) -> web.Response:
    try:
        body = await _json_object(request)
        self_id = _identity(body.get('self_id'), '机器人账号')
        raw_group_ids = body.get('group_ids')
        if not isinstance(raw_group_ids, list):
            raise ValueError('group_ids 必须是数组')
        group_ids = list(dict.fromkeys(_identity(value, '群号') for value in raw_group_ids))
        if len(group_ids) < 2:
            raise ValueError('请至少选择两个不同的群')
        if len(group_ids) > _MAX_GROUPS:
            raise ValueError(f'一次最多对比 {_MAX_GROUPS} 个群')
        exclude_self = body.get('exclude_self', True) is not False
    except ValueError as exc:
        return _error(str(exc))

    successful, failed = await _fetch_group_members(self_id, group_ids)
    if len(successful) < 2:
        return _error(
            '成功获取的群不足两个，无法进行对比',
            status=502,
            failed_groups=failed,
        )

    appearances, names, _levels, total_members = _collect_appearances(
        successful, self_id, exclude_self
    )

    duplicates = [
        {
            'user_id': user_id,
            'display_name': names.get(user_id, user_id),
            'group_count': len(groups),
            'groups': groups,
        }
        for user_id, groups in appearances.items()
        if len(groups) >= 2
    ]
    duplicates.sort(
        key=lambda item: (
            -item['group_count'],
            item['display_name'].casefold(),
            item['user_id'],
        )
    )

    return web.json_response(
        {
            'success': True,
            'duplicates': duplicates,
            'failed_groups': failed,
            'stats': {
                'requested_groups': len(group_ids),
                'compared_groups': len(successful),
                'member_records': total_members,
                'unique_members': len(appearances),
                'duplicate_members': len(duplicates),
            },
        }
    )


async def _member_info_level(
    self_id: str,
    user_id: str,
    group_id: str,
) -> tuple[int, str, list[str]]:
    """群成员资料里的 QQ 等级。"""
    try:
        response = await get_api().get_group_member_info(
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=False,
            self_id=self_id,
        )
        data = _onebot_data(response, f'查询用户 {user_id} 的群成员资料')
        if not isinstance(data, dict):
            raise RuntimeError(f'用户 {user_id} 的群成员资料不是对象')
        return _level_from(data, _MEMBER_LEVEL_FIELDS), '', sorted(data)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _ctx.log.debug('查询用户 %s 的群成员资料失败: %s', user_id, exc)
        return 0, str(exc), []


async def _stranger_info_level(
    self_id: str,
    user_id: str,
) -> tuple[int, str, list[str]]:
    """陌生人资料里的 QQ 等级 (部分协议端只在这里给出等级)。"""
    try:
        response = await get_api().get_stranger_info(
            user_id=int(user_id),
            no_cache=False,
            self_id=self_id,
        )
        data = _onebot_data(response, f'查询用户 {user_id} 的陌生人资料')
        if not isinstance(data, dict):
            raise RuntimeError(f'用户 {user_id} 的陌生人资料不是对象')
        return _level_from(data, _PROFILE_LEVEL_FIELDS), '', sorted(data)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _ctx.log.debug('查询用户 %s 的陌生人资料失败: %s', user_id, exc)
        return 0, str(exc), []


async def _probe_member_level(
    self_id: str,
    user_id: str,
    group_id: str,
) -> dict[str, Any]:
    """补查单个成员的 QQ 等级: 先群成员资料 (通常走缓存), 再陌生人资料 (服务端单查)。"""
    errors: list[str] = []
    fields: dict[str, list[str]] = {}
    level, error, keys = await _member_info_level(self_id, user_id, group_id)
    if keys:
        fields['member_info'] = keys
    if level > 0:
        return {
            'user_id': user_id,
            'level': level,
            'source': 'member_info',
            'error': '',
            'fields': fields,
            'deep': False,
        }
    if error:
        errors.append(error)

    level, error, keys = await _stranger_info_level(self_id, user_id)
    if keys:
        fields['stranger_info'] = keys
    if level > 0:
        return {
            'user_id': user_id,
            'level': level,
            'source': 'stranger_info',
            'error': '',
            'fields': fields,
            'deep': True,
        }
    if error:
        errors.append(error)
    return {
        'user_id': user_id,
        'level': 0,
        'source': '',
        'error': '；'.join(errors),
        'fields': fields,
        'deep': True,
    }


async def _fetch_friend_levels(self_id: str) -> dict[str, int]:
    """好友列表一次性带上 QQ 等级, 可以补齐一部分成员, 失败不影响主流程。"""
    try:
        response = await get_api().get_friend_list(no_cache=False, self_id=self_id)
        data = _onebot_data(response, '获取好友列表')
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _ctx.log.debug('获取好友列表失败, 跳过等级补全: %s', exc)
        return {}
    if not isinstance(data, list):
        return {}
    levels: dict[str, int] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get('user_id') or '').strip()
        level = _level_from(item, _PROFILE_LEVEL_FIELDS)
        if user_id and level > 0:
            levels[user_id] = level
    return levels


@register_route('POST', '/api/ext/group-member-overlap/level-scan', timeout=180)
async def scan_member_levels(request: web.Request) -> web.Response:
    try:
        body = await _json_object(request)
        self_id = _identity(body.get('self_id'), '机器人账号')
        raw_group_ids = body.get('group_ids')
        if not isinstance(raw_group_ids, list):
            raise ValueError('group_ids 必须是数组')
        group_ids = list(dict.fromkeys(_identity(value, '群号') for value in raw_group_ids))
        if not group_ids:
            raise ValueError('请至少选择一个群')
        if len(group_ids) > _MAX_GROUPS:
            raise ValueError(f'一次最多检查 {_MAX_GROUPS} 个群')
        threshold = _level_threshold(body.get('threshold'))
        probe_limit = _probe_limit(body.get('probe_limit'))
        exclude_self = body.get('exclude_self', True) is not False
    except ValueError as exc:
        return _error(str(exc))

    successful, failed = await _fetch_group_members(self_id, group_ids)
    if not successful:
        return _error('没有成功获取任何群的成员列表', status=502, failed_groups=failed)

    appearances, names, levels, total_members = _collect_appearances(
        successful, self_id, exclude_self
    )

    sources = {'member_list': sum(1 for level in levels.values() if level > 0)}
    # 协议端返回了哪些字段: 拿不到等级时用它说明原因
    sample_fields: dict[str, list[str]] = {}
    for members in successful.values():
        if members:
            sample_fields['member_list'] = sorted(members[0])
            break
    pending = [user_id for user_id in appearances if levels.get(user_id, 0) <= 0]

    if pending:
        friend_levels = await _fetch_friend_levels(self_id)
        for user_id in list(pending):
            level = friend_levels.get(user_id, 0)
            if level > 0:
                levels[user_id] = level
                pending.remove(user_id)
                sources['friend_list'] = sources.get('friend_list', 0) + 1

    probe_targets = pending[:probe_limit]
    probe_error = ''
    probe_stopped = False
    probe_attempted = 0
    if probe_targets:
        for index, user_id in enumerate(probe_targets):
            item = await _probe_member_level(
                self_id,
                user_id,
                str(appearances[user_id][0]['group_id']),
            )
            probe_attempted += 1
            for source, keys in item['fields'].items():
                sample_fields.setdefault(source, keys)
            if item['level'] > 0:
                levels[item['user_id']] = item['level']
                sources[item['source']] = sources.get(item['source'], 0) + 1
            elif item['error']:
                if not probe_error:
                    probe_error = item['error'][:160]
                if _is_rate_limited(item['error']):
                    probe_stopped = True
                    break
            # 陌生人资料是服务端单查, 留出间隔避免触发 QQ 频率限制
            if item['deep'] and index < len(probe_targets) - 1:
                await asyncio.sleep(_LEVEL_PROBE_INTERVAL)

    members = [
        {
            'user_id': user_id,
            'display_name': names.get(user_id, user_id),
            'qq_level': levels[user_id],
            'group_count': len(groups),
            'groups': groups,
        }
        for user_id, groups in appearances.items()
        if 0 < levels.get(user_id, 0) < threshold
    ]
    members.sort(
        key=lambda item: (
            item['qq_level'],
            item['display_name'].casefold(),
            item['user_id'],
        )
    )

    return web.json_response(
        {
            'success': True,
            'threshold': threshold,
            'members': members,
            'failed_groups': failed,
            'stats': {
                'requested_groups': len(group_ids),
                'scanned_groups': len(successful),
                'member_records': total_members,
                'unique_members': len(appearances),
                'matched_members': len(members),
                'unknown_level': sum(
                    1 for user_id in appearances if levels.get(user_id, 0) <= 0
                ),
                'level_probed': probe_attempted,
                'probe_limit': probe_limit,
                'probe_pending': max(0, len(pending) - probe_attempted),
                'probe_stopped': probe_stopped,
                'level_sources': sources,
                'probe_error': probe_error,
                'sample_fields': sample_fields,
                'threshold': threshold,
            },
        }
    )


async def _kick_member(
    self_id: str,
    user_id: str,
    group_id: str,
    reject_add: bool,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    try:
        async with semaphore:
            response = await get_api().set_group_kick(
                int(group_id),
                int(user_id),
                reject_add=reject_add,
                self_id=self_id,
            )
        _onebot_data(response, f'从群 {group_id} 移出用户 {user_id}')
        return {'user_id': user_id, 'group_id': group_id, 'success': True, 'message': ''}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _ctx.log.warning('从群 %s 移出用户 %s 失败: %s', group_id, user_id, exc)
        return {
            'user_id': user_id,
            'group_id': group_id,
            'success': False,
            'message': str(exc),
        }


@register_route('POST', '/api/ext/group-member-overlap/kick', timeout=180)
async def kick_members(request: web.Request) -> web.Response:
    try:
        body = await _json_object(request)
        self_id = _identity(body.get('self_id'), '机器人账号')
        raw_targets = body.get('targets')
        if not isinstance(raw_targets, list):
            raise ValueError('targets 必须是数组')
        targets: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw_target in raw_targets:
            if not isinstance(raw_target, dict):
                raise ValueError('移群目标格式无效')
            user_id = _identity(raw_target.get('user_id'), '用户 QQ')
            group_id = _identity(raw_target.get('group_id'), '群号')
            if user_id == self_id:
                raise ValueError('不能将机器人账号自身移出群')
            target = (user_id, group_id)
            if target not in seen:
                seen.add(target)
                targets.append(target)
        if not targets:
            raise ValueError('没有选择要执行的用户和群')
        if len(targets) > _MAX_KICK_TARGETS:
            raise ValueError(f'一次最多执行 {_MAX_KICK_TARGETS} 个移群操作')
        reject_add = body.get('reject_add') is True
    except ValueError as exc:
        return _error(str(exc))

    semaphore = asyncio.Semaphore(_KICK_CONCURRENCY)
    results = await asyncio.gather(
        *(
            _kick_member(self_id, user_id, group_id, reject_add, semaphore)
            for user_id, group_id in targets
        )
    )
    success_count = sum(1 for item in results if item['success'])
    failed_count = len(results) - success_count
    return web.json_response(
        {
            'success': True,
            'all_succeeded': failed_count == 0,
            'success_count': success_count,
            'failed_count': failed_count,
            'results': results,
        }
    )
