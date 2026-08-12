"""Persistent full-chain audit events and management statistics."""

import json
import time
import uuid
import weakref
from types import SimpleNamespace

from .core import get_db

AUDIT_LOG_TTL = 180 * 86400
_trace_context = {}
_last_cleanup = 0

_MANAGEMENT_ACTIONS = {
    'mute', 'unmute', 'recall', 'speak_recall', 'cancel_recall',
    'approve_join', 'decline_join', 'blacklist_join', 'verify_pass',
    'verify_failure_mute', 'spam_punish', 'config_change',
    'forbidden_add', 'forbidden_delete', 'forbidden_clear', 'cache_clear',
}


def _event_reference(event):
    try:
        return weakref.ref(event)
    except TypeError:
        return event


def _same_event(context, event):
    reference = context.get('event')
    return (reference() if isinstance(reference, weakref.ReferenceType) else reference) is event


def _event_value(event, name, default=''):
    value = getattr(event, name, default)
    return str(value or default)


def ensure_trace(event, source='command'):
    """Attach one trace to an incoming event and reuse it across every phase."""
    event_key = id(event)
    trace_id = getattr(event, '_groupguard_trace_id', '')
    if not trace_id:
        context = _trace_context.get(event_key)
        if (context and _same_event(context, event)
                and time.monotonic() - context['created'] < 3600):
            trace_id = context['trace_id']
    if trace_id:
        return trace_id
    trace_id = uuid.uuid4().hex[:16]
    try:
        event._groupguard_trace_id = trace_id
        event._groupguard_trace_started = time.monotonic()
        event._groupguard_trace_source = source
    except Exception:
        pass
    _trace_context[event_key] = {
        'trace_id': trace_id, 'created': time.monotonic(), 'source': source,
        'action': '', 'event': _event_reference(event),
    }
    if len(_trace_context) > 10000:
        cutoff = time.monotonic() - 3600
        for key in [key for key, value in _trace_context.items() if value['created'] < cutoff]:
            _trace_context.pop(key, None)
        if len(_trace_context) > 10000:
            oldest = sorted(
                _trace_context, key=lambda key: _trace_context[key]['created']
            )[:len(_trace_context) - 8000]
            for key in oldest:
                _trace_context.pop(key, None)
    return trace_id


def current_action(event, default=''):
    context = _trace_context.get(id(event), {})
    fallback = context.get('action', '') if _same_event(context, event) else ''
    return getattr(event, '_groupguard_action', '') or fallback or default


def current_source(event, default='command'):
    context = _trace_context.get(id(event), {})
    fallback = context.get('source', '') if _same_event(context, event) else ''
    return getattr(event, '_groupguard_trace_source', '') or fallback or default


def record_audit(
    event,
    action,
    phase,
    *,
    success=None,
    affected_count=0,
    target_id='',
    details=None,
    source=None,
):
    """Persist one structured audit phase to SQLite."""
    global _last_cleanup
    trace_id = ensure_trace(event, source or current_source(event))
    started = getattr(event, '_groupguard_trace_started', None)
    duration_ms = int((time.monotonic() - started) * 1000) if started else 0
    payload = {
        'trace_id': trace_id,
        'time': int(time.time()),
        'appid': _event_value(event, 'appid'),
        'group_id': _event_value(event, 'group_id'),
        'operator_id': _event_value(event, 'user_id'),
        'target_id': str(target_id or ''),
        'message_id': _event_value(event, 'message_id'),
        'source': str(source or current_source(event)),
        'action': str(action),
        'phase': str(phase),
        'success': None if success is None else int(bool(success)),
        'affected_count': max(0, int(affected_count or 0)),
        'duration_ms': duration_ms,
        'details': details if isinstance(details, dict) else {},
    }
    details_json = json.dumps(payload['details'], ensure_ascii=False, separators=(',', ':'))
    connection = get_db()
    connection.execute(
        'INSERT INTO audit_log '
        '(trace_id, time, appid, group_id, operator_id, target_id, message_id, '
        'source, action, phase, success, affected_count, duration_ms, details) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            payload['trace_id'], payload['time'], payload['appid'], payload['group_id'],
            payload['operator_id'], payload['target_id'], payload['message_id'],
            payload['source'], payload['action'], payload['phase'], payload['success'],
            payload['affected_count'], payload['duration_ms'], details_json,
        ),
    )
    if payload['time'] - _last_cleanup >= 3600:
        connection.execute('DELETE FROM audit_log WHERE time < ?',
                           (payload['time'] - AUDIT_LOG_TTL,))
        _last_cleanup = payload['time']
    connection.commit()
    connection.close()
    return trace_id


def record_received(event, action, *, source='command', details=None):
    try:
        event._groupguard_action = action
        event._groupguard_trace_source = source
    except Exception:
        pass
    context = _trace_context.setdefault(
        id(event),
        {'trace_id': ensure_trace(event, source), 'created': time.monotonic(),
         'source': source, 'action': action, 'event': _event_reference(event)},
    )
    context.update({'source': source, 'action': action})
    return record_audit(event, action, 'received', source=source, details=details)


def record_result(
    event, action, success, *, affected_count=0, target_id='', details=None, source=None
):
    return record_audit(
        event, action, 'result', success=success, affected_count=affected_count,
        target_id=target_id, details=details, source=source,
    )


def record_web_action(
    group_id,
    action,
    success,
    *,
    affected_count=0,
    details=None,
    operator_id='web',
    appid='',
):
    """Record a Web panel operation using the same trace phases as message actions."""
    event = SimpleNamespace(
        appid=str(appid or ''),
        group_id=str(group_id or ''),
        user_id=str(operator_id or 'web'),
        message_id='',
    )
    try:
        record_received(event, action, source='web', details=details)
        return record_result(
            event,
            action,
            success,
            affected_count=affected_count,
            details=details,
            source='web',
        )
    finally:
        _trace_context.pop(id(event), None)


def get_management_stats(group_id, days=30):
    days = max(1, min(3650, int(days)))
    since = int(time.time()) - days * 86400
    connection = get_db()
    rows = connection.execute(
        "SELECT action, COUNT(DISTINCT trace_id) AS operations, "
        "SUM(affected_count) AS affected "
        "FROM audit_log WHERE group_id = ? AND time >= ? AND phase = 'result' "
        "AND success = 1 GROUP BY action",
        (group_id, since),
    ).fetchall()
    source_rows = connection.execute(
        "SELECT source, COUNT(DISTINCT trace_id || ':' || action) AS operations "
        "FROM audit_log WHERE group_id = ? "
        "AND time >= ? AND phase = 'result' AND success = 1 "
        f"AND action IN ({','.join('?' for _ in _MANAGEMENT_ACTIONS)}) GROUP BY source",
        (group_id, since, *sorted(_MANAGEMENT_ACTIONS)),
    ).fetchall()
    failed_row = connection.execute(
        "SELECT COUNT(DISTINCT trace_id || ':' || action) AS operations "
        "FROM audit_log WHERE group_id = ? AND time >= ? "
        "AND phase = 'result' AND success = 0 "
        f"AND action IN ({','.join('?' for _ in _MANAGEMENT_ACTIONS)})",
        (group_id, since, *sorted(_MANAGEMENT_ACTIONS)),
    ).fetchone()
    connection.close()
    by_action = {
        row['action']: {
            'operations': int(row['operations'] or 0),
            'affected': int(row['affected'] or 0),
        }
        for row in rows
    }
    management_count = sum(
        item['operations'] for action, item in by_action.items()
        if action in _MANAGEMENT_ACTIONS
    )
    by_source = {row['source']: int(row['operations'] or 0) for row in source_rows}
    manual_count = by_source.get('command', 0) + by_source.get('web', 0)
    return {
        'days': days,
        'management_count': management_count,
        'manual_count': manual_count,
        'automatic_count': management_count - manual_count,
        'failed_count': int(failed_row['operations'] or 0),
        'mute_count': by_action.get('mute', {}).get('affected', 0)
        + by_action.get('verify_failure_mute', {}).get('affected', 0),
        'unmute_count': by_action.get('unmute', {}).get('affected', 0),
        'recall_count': by_action.get('recall', {}).get('affected', 0),
        'approve_count': by_action.get('approve_join', {}).get('affected', 0),
        'decline_count': by_action.get('decline_join', {}).get('affected', 0)
        + by_action.get('blacklist_join', {}).get('affected', 0),
        'punish_count': by_action.get('speak_recall', {}).get('affected', 0)
        + by_action.get('spam_punish', {}).get('affected', 0),
        'config_count': by_action.get('config_change', {}).get('operations', 0),
        'by_action': by_action,
        'by_source': by_source,
    }


def get_recent_audit(group_id, limit=10):
    limit = max(1, min(50, int(limit)))
    placeholders = ','.join('?' for _ in _MANAGEMENT_ACTIONS)
    actions = sorted(_MANAGEMENT_ACTIONS)
    connection = get_db()
    rows = connection.execute(
        "SELECT a.time, a.trace_id, a.operator_id, a.target_id, a.action, "
        "a.success, a.source, latest.affected_count FROM audit_log a JOIN ("
        "SELECT MAX(id) AS id, SUM(affected_count) AS affected_count "
        "FROM audit_log WHERE group_id = ? AND phase = 'result' "
        f"AND action IN ({placeholders}) GROUP BY trace_id, action"
        ") latest ON latest.id = a.id ORDER BY a.id DESC LIMIT ?",
        (group_id, *actions, limit),
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]
