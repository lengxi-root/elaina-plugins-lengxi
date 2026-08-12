"""JSON-backed GroupGuard reply template storage."""

import copy
import json
import os
import threading


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(ROOT_DIR, 'reply_templates.json')

_lock = threading.RLock()
_cache = None
_cache_mtime = None

_ALLOWED_FONT_SIZES = ('', 'small', 'middle', 'large')
_ALLOWED_TEMPLATE_KEYS = {
    'label', 'category', 'content', 'buttons', 'prompt_buttons',
    'button_font_size', 'button_style', 'send_kwargs', 'at_user',
    'button_mode', 'item_content', 'next_page_content', 'overflow_content',
    'action_labels', 'mode_labels', 'success_text', 'failure_text',
    'true_text', 'false_text', 'unknown_user_text', 'empty_text',
    'scope_text', 'failed_content', 'retry_content', 'decision_text',
    'blacklisted_text', 'unknown_time_text',
}


def _validate_button_rows(value, name):
    if value is None:
        return
    if not isinstance(value, (list, dict)):
        raise ValueError(f'{name}必须是数组或对象')


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
    for field in ('label', 'category', 'button_mode', 'item_content',
                  'next_page_content', 'overflow_content', 'success_text',
                  'failure_text', 'true_text', 'false_text',
                  'unknown_user_text', 'empty_text', 'scope_text',
                  'failed_content', 'retry_content', 'decision_text'):
        if field in value and not isinstance(value[field], str):
            raise ValueError(f'{field}必须是字符串')
    _validate_button_rows(value.get('buttons'), '普通按钮')
    prompt_buttons = value.get('prompt_buttons')
    if prompt_buttons is not None and not isinstance(prompt_buttons, (str, list, dict)):
        raise ValueError('输入区小按钮必须是字符串、数组或对象')
    font_size = value.get('button_font_size') or ''
    if font_size not in _ALLOWED_FONT_SIZES:
        raise ValueError('按钮尺寸必须为 default、small、middle 或 large')
    for field in ('button_style', 'send_kwargs', 'action_labels', 'mode_labels'):
        if field in value and not isinstance(value[field], dict):
            raise ValueError(f'{field}必须是对象')
    if 'at_user' in value and value['at_user'] is not None and not isinstance(value['at_user'], bool):
        raise ValueError('at_user 必须是布尔值或 null')
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError('模板包含无法保存的值') from error
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


def load_reply_templates(force=False):
    global _cache, _cache_mtime
    with _lock:
        try:
            mtime = os.stat(TEMPLATE_PATH).st_mtime_ns
        except OSError:
            mtime = None
        if not force and _cache is not None and mtime == _cache_mtime:
            return copy.deepcopy(_cache)
        payload = _read_file()
        _cache = payload
        _cache_mtime = mtime
        return copy.deepcopy(payload)


def get_reply_template(key):
    templates = load_reply_templates()['templates']
    if key not in templates:
        raise KeyError(f'unknown groupguard reply: {key}')
    return templates[key]


def list_reply_templates():
    return load_reply_templates()['templates']


def save_reply_template(key, value):
    global _cache, _cache_mtime
    template = validate_template(key, value)
    with _lock:
        payload = _read_file()
        if key not in payload['templates']:
            raise KeyError(f'unknown groupguard reply: {key}')
        payload['templates'][key] = template
        temp_path = TEMPLATE_PATH + '.tmp'
        try:
            with open(temp_path, 'w', encoding='utf-8', newline='\n') as file:
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
