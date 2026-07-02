"""play 插件配置读取。

优先级: 面板覆盖(data/runtime_config.json) > settings.yaml 的 play 段 > 环境变量 > 默认值。
"""

import json
import os
import threading

from core.base.config import cfg

DEFAULTS = {
    'enabled': True,
    'prefix': '',
    'enable_meme': True,
    'meme_api_url': 'http://datukuai.top:2233',
    'max_file_size': 10,
    'enable_master_protect': True,
    'owner_qqs': '',
    'debug': False,
    'enable_music': True,
    'music_api_url': 'https://a.aa.cab',
    'enable_draw': True,
    'draw_active': 'i1',
    'draw_i1_type': 'openai',
    'draw_i1_url': '',
    'draw_i1_key': '',
    'draw_i1_model': 'gpt-image-2',
    'draw_i2_type': 'custom',
    'draw_i2_url': '',
    'draw_i2_key': '',
    'draw_i2_model': 'gpt-image-2',
    'draw_presets': [],
    'hajimi_url': 'https://i.elaina.vin/api/%E5%93%88%E5%9F%BA%E7%B1%B3/',
}

_WRITABLE = (
    'enabled', 'prefix', 'enable_meme', 'meme_api_url', 'max_file_size',
    'enable_master_protect', 'owner_qqs', 'debug', 'enable_music',
    'music_api_url', 'enable_draw', 'draw_active',
    'draw_i1_type', 'draw_i1_url', 'draw_i1_key', 'draw_i1_model',
    'draw_i2_type', 'draw_i2_url', 'draw_i2_key', 'draw_i2_model',
    'draw_presets', 'hajimi_url',
)

_BOOL_FIELDS = ('enabled', 'enable_meme', 'enable_master_protect', 'debug',
                'enable_music', 'enable_draw')
_INT_FIELDS = ('max_file_size',)
_LIST_FIELDS = ('draw_presets',)

_OVERRIDE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'runtime_config.json')
_lock = threading.Lock()
_override_cache = None


def _load_override() -> dict:
    global _override_cache
    if _override_cache is not None:
        return _override_cache
    data = {}
    try:
        if os.path.exists(_OVERRIDE_FILE):
            with open(_OVERRIDE_FILE, encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = {k: v for k, v in loaded.items() if k in _WRITABLE}
    except Exception:  # noqa: BLE001
        data = {}
    _override_cache = data
    return data


def _coerce(key, value):
    if key in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on')
    if key in _INT_FIELDS:
        try:
            return int(value)
        except (TypeError, ValueError):
            return DEFAULTS.get(key)
    if key in _LIST_FIELDS:
        return _coerce_presets(value)
    return value


def _coerce_presets(value) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for it in value:
        if not isinstance(it, dict):
            continue
        name = str(it.get('name') or '').strip()
        prompt = str(it.get('prompt') or '').strip()
        if name and prompt:
            out.append({'name': name, 'prompt': prompt})
    return out


def set_runtime(updates: dict) -> dict:
    global _override_cache
    with _lock:
        cur = dict(_load_override())
        for k, v in (updates or {}).items():
            if k not in _WRITABLE:
                continue
            if v is None:
                cur.pop(k, None)
            elif k in _LIST_FIELDS:
                cur[k] = _coerce(k, v)
            elif isinstance(v, str) and v.strip() == '' and k not in _BOOL_FIELDS and k != 'prefix' and k != 'owner_qqs':
                cur.pop(k, None)
            else:
                cur[k] = _coerce(k, v)
        os.makedirs(os.path.dirname(_OVERRIDE_FILE), exist_ok=True)
        tmp = _OVERRIDE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _OVERRIDE_FILE)
        _override_cache = cur
        return dict(cur)


def _yaml(key: str, default=None):
    return cfg.get('settings', f'play.{key}', default)


def get(key: str):
    ov = _load_override().get(key)
    if ov is not None and ov != '':
        return _coerce(key, ov)
    if key in _override_cache and key in ('prefix', 'owner_qqs'):
        return _coerce(key, ov)
    val = _yaml(key, None)
    if val is None or val == '':
        return DEFAULTS.get(key)
    return _coerce(key, val)


def _get_bool(key: str) -> bool:
    ov = _load_override().get(key)
    if ov is not None:
        return _coerce(key, ov)
    val = _yaml(key, None)
    if val is not None:
        return _coerce(key, val)
    return bool(DEFAULTS.get(key))


def enabled() -> bool:
    return _get_bool('enabled')


def prefix() -> str:
    ov = _load_override().get('prefix')
    if ov is not None:
        return str(ov)
    val = _yaml('prefix', None)
    if val is not None:
        return str(val)
    return DEFAULTS['prefix']


def enable_meme() -> bool:
    return _get_bool('enable_meme')


def enable_music() -> bool:
    return _get_bool('enable_music')


def enable_draw() -> bool:
    return _get_bool('enable_draw')


def enable_master_protect() -> bool:
    return _get_bool('enable_master_protect')


def debug() -> bool:
    return _get_bool('debug')


def meme_api_url() -> str:
    return str(get('meme_api_url') or DEFAULTS['meme_api_url']).rstrip('/')


def music_api_url() -> str:
    return str(get('music_api_url') or DEFAULTS['music_api_url']).rstrip('/')


def draw_active() -> str:
    val = str(get('draw_active') or DEFAULTS['draw_active']).strip().lower()
    return val if val in ('i1', 'i2') else 'i1'


def _iface_type(n: str) -> str:
    val = str(get(f'draw_{n}_type') or 'openai').strip().lower()
    return val if val in ('openai', 'custom') else 'openai'


def draw_interface() -> dict:
    """当前选中的生图接口配置 (type/url/key/model)。"""
    n = draw_active()
    return {
        'type': _iface_type(n),
        'url': str(get(f'draw_{n}_url') or '').strip().rstrip('/'),
        'key': str(get(f'draw_{n}_key') or '').strip(),
        'model': str(get(f'draw_{n}_model') or DEFAULTS[f'draw_{n}_model']).strip(),
    }


def draw_presets() -> list:
    val = get('draw_presets')
    return _coerce_presets(val) if isinstance(val, list) else []


def hajimi_url() -> str:
    return str(get('hajimi_url') or DEFAULTS['hajimi_url'])


def max_file_size() -> int:
    try:
        return int(get('max_file_size'))
    except (TypeError, ValueError):
        return DEFAULTS['max_file_size']


def _split(raw: str) -> list:
    out = []
    for part in str(raw or '').replace('，', ',').replace('\n', ',').replace(' ', ',').split(','):
        part = part.strip()
        if part:
            out.append(part)
    return out


def owner_qqs() -> list:
    ov = _load_override().get('owner_qqs')
    raw = ov if ov is not None else _yaml('owner_qqs', '')
    ids = _split(raw or '')
    if ids:
        return ids
    fw = cfg.get('settings', 'owner.ids', []) or []
    return [str(x) for x in fw if str(x).strip()]


def set_owner_qqs(ids: list):
    set_runtime({'owner_qqs': ','.join(str(i) for i in ids)})


def is_master(user_id) -> bool:
    return str(user_id) in owner_qqs()


def public_config() -> dict:
    return {
        'enabled': enabled(),
        'prefix': prefix(),
        'enable_meme': enable_meme(),
        'meme_api_url': meme_api_url(),
        'max_file_size': max_file_size(),
        'enable_master_protect': enable_master_protect(),
        'owner_qqs': str(_load_override().get('owner_qqs')
                         if _load_override().get('owner_qqs') is not None
                         else (_yaml('owner_qqs', '') or '')),
        'debug': debug(),
        'enable_music': enable_music(),
        'music_api_url': music_api_url(),
        'enable_draw': enable_draw(),
        'draw_active': draw_active(),
        'draw_i1_type': _iface_type('i1'),
        'draw_i1_url': str(get('draw_i1_url') or '').strip().rstrip('/'),
        'draw_i1_key': str(get('draw_i1_key') or '').strip(),
        'draw_i1_model': str(get('draw_i1_model') or DEFAULTS['draw_i1_model']).strip(),
        'draw_i2_type': _iface_type('i2'),
        'draw_i2_url': str(get('draw_i2_url') or '').strip().rstrip('/'),
        'draw_i2_key': str(get('draw_i2_key') or '').strip(),
        'draw_i2_model': str(get('draw_i2_model') or DEFAULTS['draw_i2_model']).strip(),
        'draw_presets': draw_presets(),
        'hajimi_url': hajimi_url(),
    }
