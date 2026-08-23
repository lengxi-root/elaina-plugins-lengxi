"""JSON-backed GroupGuard reply template storage."""

import copy
import json
import math
import os
import string
import tempfile
import threading

from .default_templates import (
    DEFAULT_PAYLOAD,
    JOIN_REQUEST_ITEM_CONTENT,
    LEGACY_JOIN_REQUEST_ITEM_CONTENT,
)


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, 'data')
TEMPLATE_PATH = os.path.join(DATA_DIR, 'reply_templates.json')

_lock = threading.RLock()
_cache = None
_cache_mtime = None
_formatter = string.Formatter()

_LEGACY_BOT_NAME_TEXT = '授权后无需@伊蕾娜也可以处理指令'
_DYNAMIC_BOT_NAME_TEXT = '授权后无需@{bot_name}也可以处理指令'
_FULL_TEMPLATE_REFRESH_VERSION = 6

_ALLOWED_BUTTON_MODES = {'', 'join_requests', 'verify_options'}
_STRING_FIELDS = {
    'label', 'category', 'button_mode', 'item_content',
    'next_page_content', 'overflow_content', 'success_text',
    'failure_text', 'true_text', 'false_text', 'unknown_user_text',
    'empty_text', 'scope_text', 'failed_content', 'retry_content',
    'decision_text', 'blacklisted_text', 'unknown_time_text',
}
_RENDERED_FIELDS = {
    'content', 'buttons',
    'item_content', 'next_page_content', 'overflow_content', 'success_text',
    'failure_text', 'true_text', 'false_text', 'unknown_user_text',
    'empty_text', 'scope_text', 'failed_content', 'retry_content',
    'decision_text', 'blacklisted_text', 'unknown_time_text',
}
_ALLOWED_TEMPLATE_KEYS = _STRING_FIELDS | {
    'content', 'buttons', 'small_buttons', 'msg_type', 'at_user',
    'action_labels', 'mode_labels',
}
_ALLOWED_BUTTON_KEYS = {
    'id', 'render_data', 'action', 'show', 'text', 'style', 'type', 'data',
    'link', 'enter', 'reply', 'permission', 'role', 'list', 'admin', 'limit',
    'tips', 'modal', 'subscribe', 'subscribe_data', 'click_limit',
    'unsupport_tips', 'anchor',
}
_LEGACY_TEMPLATE_KEYS = {
    'prompt_buttons', 'button_font_size', 'button_style', 'send_kwargs',
}


def _is_int_between(value, minimum, maximum):
    return (
        isinstance(value, int) and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _validate_json_tree(value, name, depth=0):
    if depth > 10:
        raise ValueError(f'{name}嵌套层级不能超过 10 层')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f'{name}不能包含 NaN 或 Infinity')
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        if len(value) > 100:
            raise ValueError(f'{name}最多包含 100 项')
        for item in value:
            _validate_json_tree(item, name, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 100:
            raise ValueError(f'{name}最多包含 100 个字段')
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f'{name}的字段名必须是字符串')
            _validate_json_tree(item, name, depth + 1)
        return
    raise ValueError(f'{name}包含不支持的数据类型')


def _validate_format_string(value, name):
    try:
        fields = _formatter.parse(value)
        for _literal, field_name, format_spec, conversion in fields:
            if field_name is None:
                continue
            if not field_name or not field_name.isascii() or not field_name.isidentifier():
                raise ValueError(f'{name}仅支持简单占位符，例如 {{count}}')
            if format_spec or conversion:
                raise ValueError(f'{name}不支持格式说明符或类型转换')
    except ValueError as error:
        if str(error).startswith(name):
            raise
        raise ValueError(f'{name}包含无效占位符或未转义的大括号') from error


def _validate_rendered_strings(value, name):
    if isinstance(value, str):
        _validate_format_string(value, name)
    elif isinstance(value, list):
        for item in value:
            _validate_rendered_strings(item, name)
    elif isinstance(value, dict):
        for item in value.values():
            _validate_rendered_strings(item, name)


def _validate_button(button, name):
    if not isinstance(button, dict):
        raise ValueError(f'{name}中的按钮必须是对象')
    unknown = set(button) - _ALLOWED_BUTTON_KEYS
    if unknown:
        raise ValueError(f'{name}按钮包含不支持的字段：{", ".join(sorted(unknown))}')
    for field in ('render_data', 'action', 'permission'):
        if field in button and not isinstance(button[field], dict):
            raise ValueError(f'{name}按钮的 {field} 必须是对象')
    for field in ('enter', 'reply', 'admin'):
        if field in button and not isinstance(button[field], bool):
            raise ValueError(f'{name}按钮的 {field} 必须是布尔值')
    for field in ('text', 'show', 'data', 'link', 'tips'):
        if field in button and not isinstance(button[field], str):
            raise ValueError(f'{name}按钮的 {field} 必须是字符串')
        if field in button and len(button[field]) > 2048:
            raise ValueError(f'{name}按钮的 {field} 过长')
    for field in ('style', 'type'):
        if field in button and not _is_int_between(button[field], 0, 4):
            raise ValueError(f'{name}按钮的 {field} 必须是 0 至 4 的整数')


def _validate_buttons(value):
    if value is None:
        return
    if not isinstance(value, list):
        raise ValueError('按钮必须是 [{"text": "按钮", "data": "/命令"}] 格式的数组')
    if len(value) > 15:
        raise ValueError('按钮最多包含 15 个')
    for button in value:
        _validate_button(button, '按钮')


def validate_template(key, value):
    if not isinstance(key, str) or not key or len(key) > 80:
        raise ValueError('模板键无效')
    if not isinstance(value, dict):
        raise ValueError('模板必须是对象')
    unknown = set(value) - _ALLOWED_TEMPLATE_KEYS
    if unknown:
        raise ValueError(f'模板包含不支持的字段：{", ".join(sorted(unknown))}')
    content = value.get('content')
    if not isinstance(content, str):
        raise ValueError('模板正文必须是字符串')
    if len(content) > 30000:
        raise ValueError('模板正文不能超过 30000 个字符')
    for field in _STRING_FIELDS:
        if field in value and not isinstance(value[field], str):
            raise ValueError(f'{field} 必须是字符串')
        if field in value and len(value[field]) > 30000:
            raise ValueError(f'{field} 内容过长')
    if value.get('button_mode', '') not in _ALLOWED_BUTTON_MODES:
        raise ValueError('button_mode 必须为空、join_requests 或 verify_options')
    _validate_buttons(value.get('buttons'))
    button_items = value.get('buttons') or []
    if value.get('button_mode') == 'join_requests' and not button_items:
        raise ValueError('join_requests 模板必须配置按钮原型')
    if value.get('button_mode') == 'verify_options':
        if len(button_items) != 1:
            raise ValueError('verify_options 模板必须且只能配置 1 个按钮原型')
    for field in ('action_labels', 'mode_labels'):
        if field in value and not isinstance(value[field], dict):
            raise ValueError(f'{field} 必须是对象')
    for field in ('small_buttons', 'at_user'):
        if field in value and not isinstance(value[field], bool):
            raise ValueError(f'{field} 必须是布尔值')
    if value.get('msg_type') not in (None, 0, 2):
        raise ValueError('msg_type 仅支持 0（文本）或 2（Markdown）')
    _validate_json_tree(value, '模板')
    for field in _RENDERED_FIELDS:
        if field in value:
            _validate_rendered_strings(value[field], field)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded) > 200000:
        raise ValueError('单个模板配置过大')
    return copy.deepcopy(value)


def _flatten_legacy_buttons(value):
    changed = isinstance(value, dict)
    if isinstance(value, dict):
        value = value.get('rows') or value.get('buttons') or value.get('btns') or []
    if not isinstance(value, list):
        return value, changed
    buttons = []
    for item in value:
        if isinstance(item, list):
            buttons.extend(item)
            changed = True
        elif isinstance(item, dict) and ('buttons' in item or 'btns' in item):
            row = item.get('buttons') or item.get('btns') or []
            buttons.extend(row if isinstance(row, list) else [item])
            changed = True
        else:
            buttons.append(item)
    return buttons, changed


def _flatten_legacy_prompt_buttons(value):
    if not value:
        return []
    if isinstance(value, dict) and isinstance(value.get('content'), dict):
        rows = value['content'].get('rows') or []
        value = []
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get('buttons'), list):
                value.extend(row['buttons'])
    elif not isinstance(value, list):
        value = [value]
    buttons = []
    for index, item in enumerate(value, 1):
        if isinstance(item, str):
            item = {'text': item, 'data': 'elaina', 'type': 2, 'enter': True}
        elif isinstance(item, list) and item and isinstance(item[0], str):
            item = {'text': item[0], 'data': 'elaina', 'type': 2, 'enter': True,
                    **({'style': item[1]} if len(item) > 1 else {})}
        else:
            item = copy.deepcopy(item)
            if isinstance(item, dict):
                item.setdefault('id', str(index))
        buttons.append(item)
    return buttons


def _migrate_legacy_template(value):
    if not isinstance(value, dict):
        return value, False
    buttons, rows_changed = _flatten_legacy_buttons(value.get('buttons'))
    legacy_keys = _LEGACY_TEMPLATE_KEYS.intersection(value)
    if not legacy_keys and not rows_changed:
        return value, False

    migrated = copy.deepcopy(value)
    prompt_buttons = _flatten_legacy_prompt_buttons(
        migrated.pop('prompt_buttons', None)
    )
    if isinstance(buttons, list):
        migrated['buttons'] = buttons + prompt_buttons
    elif buttons is None and prompt_buttons:
        migrated['buttons'] = prompt_buttons

    font_size = migrated.pop('button_font_size', '') or ''
    button_style = migrated.pop('button_style', None)
    if isinstance(button_style, dict):
        font_size = font_size or button_style.get('font_size') or ''
    if font_size == 'small' and 'small_buttons' not in migrated:
        migrated['small_buttons'] = True

    send_kwargs = migrated.pop('send_kwargs', None)
    if (isinstance(send_kwargs, dict) and 'msg_type' in send_kwargs
            and 'msg_type' not in migrated):
        migrated['msg_type'] = send_kwargs['msg_type']
    return migrated, True


def _prepare_payload(payload, allow_empty=False):
    if not isinstance(payload, dict) or not isinstance(payload.get('templates'), dict):
        return _normalize_payload(payload, allow_empty=allow_empty), False
    migrated = copy.deepcopy(payload)
    changed = False
    for key, template in migrated['templates'].items():
        migrated['templates'][key], template_changed = _migrate_legacy_template(
            template
        )
        changed = changed or template_changed
    return _normalize_payload(migrated, allow_empty=allow_empty), changed


def _normalize_payload(payload, allow_empty=False):
    if not isinstance(payload, dict) or not isinstance(payload.get('templates'), dict):
        raise ValueError('回复模板文件格式无效')
    templates = {
        key: validate_template(key, value)
        for key, value in payload['templates'].items()
    }
    if not templates and not allow_empty:
        raise ValueError('回复模板文件不能为空')
    try:
        version = int(payload.get('version', 1))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('模板版本号无效') from error
    if version < 1:
        raise ValueError('模板版本号无效')
    return {'version': version, 'templates': templates}


def _read_file(path, allow_empty=False):
    with open(path, 'r', encoding='utf-8') as file:
        return _prepare_payload(json.load(file), allow_empty=allow_empty)


def _load_defaults():
    return _normalize_payload(copy.deepcopy(DEFAULT_PAYLOAD))


def _write_file(payload):
    os.makedirs(DATA_DIR, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(
        dir=DATA_DIR, prefix='.reply_templates.', suffix='.tmp'
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, TEMPLATE_PATH)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def initialize_reply_templates():
    """Create the editable file and append new defaults without replacing edits."""
    with _lock:
        defaults = _load_defaults()
        if not os.path.exists(TEMPLATE_PATH):
            _write_file(defaults)
            return copy.deepcopy(defaults)

        payload, changed = _read_file(TEMPLATE_PATH)
        if payload.get('version', 1) < _FULL_TEMPLATE_REFRESH_VERSION:
            payload = copy.deepcopy(defaults)
            changed = True
        else:
            for key, template in defaults['templates'].items():
                if key not in payload['templates']:
                    payload['templates'][key] = template
                    changed = True
            legacy_template = payload['templates'].get('full_message_required')
            default_template = defaults['templates'].get(
                'full_message_required',
            ) or {}
            default_content = default_template.get('content')
            legacy_content = (
                default_content.replace(
                    _DYNAMIC_BOT_NAME_TEXT, _LEGACY_BOT_NAME_TEXT,
                )
                if isinstance(default_content, str) else None
            )
            if (isinstance(legacy_template, dict)
                    and isinstance(legacy_content, str)
                    and legacy_template.get('content') == legacy_content):
                legacy_template['content'] = default_content
                changed = True
            join_requests_template = payload['templates'].get('join_requests')
            if (isinstance(join_requests_template, dict)
                    and join_requests_template.get('item_content')
                    == LEGACY_JOIN_REQUEST_ITEM_CONTENT):
                join_requests_template['item_content'] = (
                    JOIN_REQUEST_ITEM_CONTENT
                )
                changed = True
        if changed:
            _write_file(payload)
        return copy.deepcopy(payload)


def _template_signature():
    try:
        return os.stat(TEMPLATE_PATH).st_mtime_ns
    except OSError:
        return None


def _load_cached(force=False):
    global _cache, _cache_mtime
    with _lock:
        mtime = _template_signature()
        if not force and _cache is not None and mtime == _cache_mtime:
            return _cache
        try:
            payload = initialize_reply_templates()
        except (OSError, ValueError, json.JSONDecodeError):
            if force or _cache is None:
                raise
            _cache_mtime = mtime
            return _cache
        _cache = payload
        _cache_mtime = _template_signature()
        return _cache


def load_reply_templates(force=False):
    return copy.deepcopy(_load_cached(force))


def _get_cached_template(key):
    templates = _load_cached()['templates']
    if key not in templates:
        raise KeyError(f'unknown groupguard reply: {key}')
    return templates[key]


def get_reply_template(key):
    return copy.deepcopy(_get_cached_template(key))


def list_reply_templates():
    return copy.deepcopy(_load_cached()['templates'])


def save_reply_template(key, value):
    global _cache, _cache_mtime
    template = validate_template(key, value)
    with _lock:
        payload = initialize_reply_templates()
        if key not in payload['templates']:
            raise KeyError(f'unknown groupguard reply: {key}')
        payload['templates'][key] = template
        _write_file(payload)
        _cache = payload
        _cache_mtime = _template_signature()
    return copy.deepcopy(template)
