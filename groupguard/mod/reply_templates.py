"""JSON-backed GroupGuard reply template storage."""

import copy
import json
import math
import os
import string
import tempfile
import threading


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(ROOT_DIR, 'reply_templates.json')

_lock = threading.RLock()
_cache = None
_cache_mtime = None
_formatter = string.Formatter()

_ALLOWED_FONT_SIZES = {'', 'small', 'middle', 'large'}
_ALLOWED_BUTTON_MODES = {'', 'join_requests', 'verify_options'}
_ALLOWED_SEND_KWARGS = {'msg_type', 'auto_delete_time', 'skip_suffix'}
_STRING_FIELDS = {
    'label', 'category', 'button_mode', 'item_content',
    'next_page_content', 'overflow_content', 'success_text',
    'failure_text', 'true_text', 'false_text', 'unknown_user_text',
    'empty_text', 'scope_text', 'failed_content', 'retry_content',
    'decision_text', 'blacklisted_text', 'unknown_time_text',
}
_RENDERED_FIELDS = {
    'content', 'buttons', 'prompt_buttons', 'button_style', 'send_kwargs',
    'item_content', 'next_page_content', 'overflow_content', 'success_text',
    'failure_text', 'true_text', 'false_text', 'unknown_user_text',
    'empty_text', 'scope_text', 'failed_content', 'retry_content',
    'decision_text', 'blacklisted_text', 'unknown_time_text',
}
_ALLOWED_TEMPLATE_KEYS = _STRING_FIELDS | {
    'content', 'buttons', 'prompt_buttons', 'button_font_size',
    'button_style', 'send_kwargs', 'at_user', 'action_labels', 'mode_labels',
}
_ALLOWED_BUTTON_KEYS = {
    'id', 'render_data', 'action', 'show', 'text', 'style', 'type', 'data',
    'link', 'enter', 'reply', 'permission', 'role', 'list', 'admin', 'limit',
    'tips', 'modal', 'subscribe', 'subscribe_data', 'click_limit',
    'unsupport_tips', 'anchor',
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


def _validate_prompt_button(button):
    if not isinstance(button, dict):
        raise ValueError('输入区小按钮必须是对象')
    unknown = set(button) - {'id', 'render_data', 'action'}
    if unknown:
        raise ValueError(
            f'输入区小按钮包含不支持的字段：{", ".join(sorted(unknown))}'
        )
    render_data = button.get('render_data')
    if not isinstance(render_data, dict):
        raise ValueError('输入区小按钮的 render_data 必须是对象')
    unknown_render = set(render_data) - {'label', 'visited_label', 'style'}
    if unknown_render:
        raise ValueError('输入区小按钮的 render_data 包含不支持的字段')
    if not isinstance(render_data.get('label'), str):
        raise ValueError('输入区小按钮必须包含文本 label')
    if len(render_data['label']) > 128:
        raise ValueError('输入区小按钮文本过长')
    if 'visited_label' in render_data and not isinstance(
            render_data['visited_label'], str):
        raise ValueError('输入区小按钮的 visited_label 必须是字符串')
    if 'style' in render_data and not _is_int_between(
            render_data['style'], 0, 4):
        raise ValueError('输入区小按钮的 style 必须是 0 至 4 的整数')
    action = button.get('action')
    if action is not None:
        if not isinstance(action, dict):
            raise ValueError('输入区小按钮的 action 必须是对象')
        allowed_action = {
            'type', 'data', 'enter', 'reply', 'permission', 'click_limit',
            'unsupport_tips', 'modal', 'subscribe_data', 'anchor',
        }
        unknown_action = set(action) - allowed_action
        if unknown_action:
            raise ValueError('输入区小按钮的 action 包含不支持的字段')
        if 'type' in action and not _is_int_between(action['type'], 0, 4):
            raise ValueError('输入区小按钮的 action.type 必须是 0 至 4 的整数')
        if 'data' in action and not isinstance(action['data'], str):
            raise ValueError('输入区小按钮的 action.data 必须是字符串')
        for field in ('enter', 'reply'):
            if field in action and not isinstance(action[field], bool):
                raise ValueError(f'输入区小按钮的 action.{field} 必须是布尔值')


def _button_rows(value, name):
    if isinstance(value, dict):
        unknown = set(value) - {'rows', 'buttons', 'btns', 'font_size', 'style'}
        if unknown:
            raise ValueError(f'{name}包含不支持的字段：{", ".join(sorted(unknown))}')
        if value.get('font_size', '') not in _ALLOWED_FONT_SIZES:
            raise ValueError(f'{name}的 font_size 无效')
        if 'style' in value and not isinstance(value['style'], dict):
            raise ValueError(f'{name}的 style 必须是对象')
        return value.get('rows') or value.get('buttons') or value.get('btns') or []
    return value


def _validate_button_rows(value, name):
    if value is None:
        return
    rows = _button_rows(value, name)
    if not isinstance(rows, list):
        raise ValueError(f'{name}必须是按钮行数组或键盘对象')
    if len(rows) > 5:
        raise ValueError(f'{name}最多包含 5 行')
    for row_index, row in enumerate(rows, 1):
        if isinstance(row, dict):
            unknown = set(row) - {'buttons', 'btns'}
            if unknown:
                raise ValueError(f'{name}第 {row_index} 行包含不支持的字段')
            row = row.get('buttons') or row.get('btns') or []
        if not isinstance(row, list):
            raise ValueError(f'{name}第 {row_index} 行必须是数组')
        if len(row) > 5:
            raise ValueError(f'{name}第 {row_index} 行最多包含 5 个按钮')
        for button in row:
            _validate_button(button, name)


def _validate_prompt_buttons(value):
    if value is None:
        return
    if isinstance(value, str):
        if len(value) > 128:
            raise ValueError('输入区小按钮文本过长')
        return
    if isinstance(value, dict):
        if set(value) != {'content'} or not isinstance(value['content'], dict):
            raise ValueError('输入区小按钮对象必须包含 content')
        content = value['content']
        if set(content) != {'rows'} or not isinstance(content['rows'], list):
            raise ValueError('输入区小按钮 content 必须包含 rows 数组')
        if len(content['rows']) != 1:
            raise ValueError('输入区小按钮对象必须且只能包含 1 行')
        row = content['rows'][0]
        if not isinstance(row, dict) or set(row) != {'buttons'}:
            raise ValueError('输入区小按钮行必须包含 buttons 数组')
        if not isinstance(row['buttons'], list) or len(row['buttons']) > 3:
            raise ValueError('输入区小按钮最多包含 3 个按钮')
        for button in row['buttons']:
            _validate_prompt_button(button)
        return
    if not isinstance(value, list):
        raise ValueError('输入区小按钮必须是字符串、数组或对象')
    if len(value) > 3:
        raise ValueError('输入区小按钮最多包含 3 个按钮')
    for item in value:
        if isinstance(item, str):
            if len(item) > 128:
                raise ValueError('输入区小按钮文本过长')
        elif isinstance(item, list):
            if not item or len(item) > 2 or not isinstance(item[0], str):
                raise ValueError('输入区小按钮数组格式无效')
            if len(item) == 2 and not _is_int_between(item[1], 0, 4):
                raise ValueError('输入区小按钮的 style 必须是 0 至 4 的整数')
        elif isinstance(item, dict):
            _validate_prompt_button(item)
        else:
            raise ValueError('输入区小按钮项目格式无效')


def _validate_send_kwargs(value):
    if not isinstance(value, dict):
        raise ValueError('send_kwargs 必须是对象')
    unknown = set(value) - _ALLOWED_SEND_KWARGS
    if unknown:
        raise ValueError(f'send_kwargs 包含不安全或不支持的字段：{", ".join(sorted(unknown))}')
    if 'msg_type' in value and value['msg_type'] not in (0, 2):
        raise ValueError('msg_type 仅支持 0（文本）或 2（Markdown）')
    if 'skip_suffix' in value and not isinstance(value['skip_suffix'], bool):
        raise ValueError('skip_suffix 必须是布尔值')
    if 'auto_delete_time' in value:
        seconds = value['auto_delete_time']
        if not isinstance(seconds, int) or isinstance(seconds, bool) or not 0 <= seconds <= 86400:
            raise ValueError('auto_delete_time 必须是 0 至 86400 的整数')


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
    _validate_button_rows(value.get('buttons'), '普通按钮')
    dynamic_rows = _button_rows(value.get('buttons') or [], '普通按钮')
    if value.get('button_mode') == 'join_requests' and len(dynamic_rows) != 1:
        raise ValueError('join_requests 模板必须且只能配置 1 行按钮')
    if value.get('button_mode') == 'verify_options':
        if len(dynamic_rows) != 1 or len(dynamic_rows[0]) != 1:
            raise ValueError('verify_options 模板必须且只能配置 1 个按钮原型')
    _validate_prompt_buttons(value.get('prompt_buttons'))
    font_size = value.get('button_font_size') or ''
    if font_size not in _ALLOWED_FONT_SIZES:
        raise ValueError('按钮尺寸必须为 default、small、middle 或 large')
    for field in ('button_style', 'action_labels', 'mode_labels'):
        if field in value and not isinstance(value[field], dict):
            raise ValueError(f'{field} 必须是对象')
    _validate_send_kwargs(value.get('send_kwargs') or {})
    if 'at_user' in value and not isinstance(value['at_user'], bool):
        raise ValueError('at_user 必须是布尔值')
    _validate_json_tree(value, '模板')
    for field in _RENDERED_FIELDS:
        if field in value:
            _validate_rendered_strings(value[field], field)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded) > 200000:
        raise ValueError('单个模板配置过大')
    return copy.deepcopy(value)


def _read_file():
    with open(TEMPLATE_PATH, 'r', encoding='utf-8') as file:
        payload = json.load(file)
    if not isinstance(payload, dict) or not isinstance(payload.get('templates'), dict):
        raise ValueError('回复模板文件格式无效')
    templates = {
        key: validate_template(key, value)
        for key, value in payload['templates'].items()
    }
    if not templates:
        raise ValueError('回复模板文件不能为空')
    return {'version': int(payload.get('version') or 1), 'templates': templates}


def _load_cached(force=False):
    global _cache, _cache_mtime
    with _lock:
        try:
            mtime = os.stat(TEMPLATE_PATH).st_mtime_ns
        except OSError:
            mtime = None
        if not force and _cache is not None and mtime == _cache_mtime:
            return _cache
        try:
            payload = _read_file()
        except (OSError, ValueError, json.JSONDecodeError):
            if force or _cache is None:
                raise
            _cache_mtime = mtime
            return _cache
        _cache = payload
        _cache_mtime = mtime
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
        payload = _read_file()
        if key not in payload['templates']:
            raise KeyError(f'unknown groupguard reply: {key}')
        payload['templates'][key] = template
        descriptor, temp_path = tempfile.mkstemp(
            dir=ROOT_DIR, prefix='.reply_templates.', suffix='.tmp'
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
        _cache = payload
        _cache_mtime = os.stat(TEMPLATE_PATH).st_mtime_ns
    return copy.deepcopy(template)
