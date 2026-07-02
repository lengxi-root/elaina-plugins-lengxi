"""aicat 配置读取, 优先级: 面板覆盖(data/runtime_config.json) > settings.yaml 的 aicat 段 > 环境变量 > 默认值。

所有字段均可在插件 Web 面板中修改并持久化。
"""

import json
import os
import threading
import uuid

from core.base.config import cfg

# 内置 YTea 中转 (NewAPI, OpenAI 兼容) 固定域名
YTEA_BASE_URL = 'https://api.ytea.top/v1'

DEFAULTS = {
    'enabled': True,
    # 接口提供方: ytea=内置 YTea 中转(api.ytea.top) / custom=自定义 OpenAI 兼容
    'provider': 'ytea',
    'base_url': 'https://api.openai.com/v1',
    'model': 'gpt-4o',
    # 模型优先级列表 + 自动切换 + 可用性轮询
    'model_priority': [],
    'auto_switch': False,
    'health_check_interval': 300,
    'temperature': 0.7,
    'max_rounds': 20,
    'request_timeout': 120,
    'reasoning_effort': '',
    # 触发
    'prefix': 'cat',
    'allow_at_trigger': True,
    # 人设
    'bot_name': '汐雨',
    'personality': '可爱猫娘助手，说话带"喵"等语气词，活泼俏皮会撒娇',
    'system_prompt': '',
    # 回复行为
    'send_confirm_message': True,
    'confirm_message': '汐雨收到喵～',
    'safety_filter': True,
    'enable_tools': True,
    # 长文本自动合并转发 (超过阈值字符数时用合并消息发送)
    'long_text_forward': True,
    'forward_threshold': 400,
    # 随机回复: 用户未触发时, 按概率(%)决定是否用 AI 回复该消息 (不发确认提示语)
    'random_reply': False,
    'random_reply_chance': 0,
    # 上下文
    'max_context_turns': 30,
    'context_expire_seconds': 600,
    # 权限
    'owner_qqs': '',
}

# 允许面板写入并持久化的字段 (api_key 单独处理)
_WRITABLE = (
    'enabled', 'provider', 'base_url', 'api_key', 'model', 'model_priority',
    'auto_switch', 'health_check_interval', 'temperature', 'max_rounds',
    'request_timeout', 'reasoning_effort', 'prefix', 'allow_at_trigger',
    'bot_name', 'personality', 'system_prompt', 'send_confirm_message',
    'confirm_message', 'safety_filter', 'enable_tools', 'max_context_turns',
    'context_expire_seconds', 'owner_qqs',
    'long_text_forward', 'forward_threshold', 'random_reply', 'random_reply_chance',
)

_BOOL_FIELDS = ('enabled', 'allow_at_trigger', 'send_confirm_message', 'safety_filter', 'enable_tools', 'auto_switch',
                'long_text_forward', 'random_reply')
_INT_FIELDS = ('max_rounds', 'request_timeout', 'max_context_turns', 'context_expire_seconds', 'health_check_interval',
               'forward_threshold')
_FLOAT_FIELDS = ('temperature', 'random_reply_chance')

_OVERRIDE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'runtime_config.json')
_lock = threading.Lock()
_override_cache = None

# ---- 多站点: 统一管理多个 OpenAI 兼容接口 (含内置 YTea), 随时切换激活站点 ----
# 每个站点: {id, name, base_url, api_key, builtin}; 激活站点提供生效的 base_url/api_key。
_SITES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'sites.json')
_YTEA_ID = 'ytea'
_sites_cache = None


def _builtin_ytea() -> dict:
    return {'id': _YTEA_ID, 'name': 'YTea 中转 (api.ytea.top)', 'base_url': YTEA_BASE_URL,
            'api_key': '', 'builtin': True}


def _load_sites_raw() -> dict:
    global _sites_cache
    if _sites_cache is not None:
        return _sites_cache
    data = {'active_id': '', 'sites': []}
    try:
        if os.path.exists(_SITES_FILE):
            with open(_SITES_FILE, encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and isinstance(loaded.get('sites'), list):
                data = {'active_id': str(loaded.get('active_id') or ''), 'sites': loaded['sites']}
    except Exception:  # noqa: BLE001
        data = {'active_id': '', 'sites': []}
    # 保证内置 YTea 站点始终存在 (域名固定)
    ytea = next((s for s in data['sites'] if s.get('id') == _YTEA_ID), None)
    if ytea is None:
        # 首次: 若旧版扁平配置里有 base_url/api_key, 迁移为一个自定义站点
        legacy = _load_override()
        if (legacy.get('api_key') or legacy.get('base_url')) and legacy.get('provider') != 'ytea':
            data['sites'].append({
                'id': uuid.uuid4().hex[:8], 'name': '自定义接口',
                'base_url': str(legacy.get('base_url') or DEFAULTS['base_url']).rstrip('/'),
                'api_key': str(legacy.get('api_key') or ''), 'builtin': False,
            })
        data['sites'].insert(0, _builtin_ytea())
        if not data['active_id']:
            data['active_id'] = _YTEA_ID if legacy.get('provider', 'ytea') == 'ytea' else data['sites'][-1]['id']
    else:
        ytea['base_url'] = YTEA_BASE_URL
        ytea['builtin'] = True
    if not any(s.get('id') == data['active_id'] for s in data['sites']):
        data['active_id'] = data['sites'][0]['id'] if data['sites'] else ''
    _sites_cache = data
    return data


def _save_sites_raw(data: dict):
    global _sites_cache
    os.makedirs(os.path.dirname(_SITES_FILE), exist_ok=True)
    tmp = _SITES_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _SITES_FILE)
    _sites_cache = data


def list_sites() -> dict:
    """返回站点列表 (api_key 仅标记是否已设) 与当前激活站点 id。"""
    raw = _load_sites_raw()
    sites = [{
        'id': s.get('id', ''),
        'name': s.get('name', '') or '未命名站点',
        'base_url': s.get('base_url', ''),
        'builtin': bool(s.get('builtin')),
        'api_key_set': bool(s.get('api_key')),
    } for s in raw['sites'] if s.get('id')]
    return {'active_id': raw.get('active_id', ''), 'sites': sites}


def active_site() -> dict:
    raw = _load_sites_raw()
    return next((s for s in raw['sites'] if s.get('id') == raw.get('active_id')), None) or (
        raw['sites'][0] if raw['sites'] else _builtin_ytea())


def save_site(p: dict) -> dict:
    """新增或更新站点 (按 id 匹配, 无 id 则新建)。api_key 留空不改、null 清除。内置站点仅可改名/密钥。"""
    with _lock:
        raw = _load_sites_raw()
        sid = str((p or {}).get('id') or '').strip()
        item = next((s for s in raw['sites'] if s.get('id') == sid), None) if sid else None
        if item is None:
            sid = uuid.uuid4().hex[:8]
            item = {'id': sid, 'builtin': False}
            raw['sites'].append(item)
        item['name'] = str(p.get('name') or '').strip() or item.get('name') or '未命名站点'
        if not item.get('builtin') and 'base_url' in p:
            item['base_url'] = str(p.get('base_url') or '').strip().rstrip('/') or DEFAULTS['base_url']
        if 'api_key' in p:
            ak = p['api_key']
            if ak is None:
                item['api_key'] = ''
            elif isinstance(ak, str) and ak.strip():
                item['api_key'] = ak.strip()
        if not raw.get('active_id'):
            raw['active_id'] = sid
        _save_sites_raw(raw)
        return {**list_sites(), 'saved_id': sid}


def delete_site(sid: str) -> dict:
    with _lock:
        raw = _load_sites_raw()
        raw['sites'] = [s for s in raw['sites'] if not (s.get('id') == sid and not s.get('builtin'))]
        if raw.get('active_id') == sid:
            raw['active_id'] = raw['sites'][0]['id'] if raw['sites'] else ''
        _save_sites_raw(raw)
        return list_sites()


def apply_site(sid: str):
    """切换激活站点; 不存在返回 None。"""
    with _lock:
        raw = _load_sites_raw()
        if not any(s.get('id') == sid for s in raw['sites']):
            return None
        raw['active_id'] = sid
        _save_sites_raw(raw)
        return list_sites()


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
    """按字段类型规整面板传入的值。"""
    if key in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on')
    if key in _INT_FIELDS:
        try:
            return int(value)
        except (TypeError, ValueError):
            return DEFAULTS.get(key)
    if key in _FLOAT_FIELDS:
        try:
            return float(value)
        except (TypeError, ValueError):
            return DEFAULTS.get(key)
    return value


def set_runtime(updates: dict) -> dict:
    """合并并持久化面板可写字段, 返回最新覆盖值。"""
    global _override_cache
    with _lock:
        cur = dict(_load_override())
        for k, v in (updates or {}).items():
            if k not in _WRITABLE:
                continue
            if v is None:
                cur.pop(k, None)
            elif isinstance(v, str) and v.strip() == '' and k not in _BOOL_FIELDS:
                # 空字符串表示清除该覆盖, 回退到 settings.yaml / 环境变量 / 默认
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
    return cfg.get('settings', f'aicat.{key}', default)


def get(key: str):
    ov = _load_override().get(key)
    if ov is not None and ov != '':
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


def provider() -> str:
    v = str(get('provider') or DEFAULTS['provider']).strip().lower()
    return v if v in ('ytea', 'custom') else DEFAULTS['provider']


def base_url() -> str:
    """生效 base_url: 取当前激活站点。"""
    site = active_site()
    url = str(site.get('base_url') or DEFAULTS['base_url']).rstrip('/')
    return url


def api_key() -> str:
    """生效 api_key: 优先当前激活站点, 回退 settings.yaml / 环境变量。"""
    key = str(active_site().get('api_key') or '')
    if not key:
        key = _yaml('api_key', '') or ''
    if not key:
        key = os.environ.get('AICAT_API_KEY') or os.environ.get('OPENAI_API_KEY') or ''
    return str(key)


def model() -> str:
    return str(get('model') or DEFAULTS['model'])


def model_priority() -> list:
    """模型优先级列表 (按顺序尝试); 兼容逗号分隔字符串。"""
    raw = get('model_priority')
    items = []
    if isinstance(raw, list):
        items = [str(x).strip() for x in raw]
    elif isinstance(raw, str) and raw.strip():
        items = [p.strip() for p in raw.replace('\n', ',').split(',')]
    return [m for m in dict.fromkeys(items) if m]


def auto_switch() -> bool:
    return _get_bool('auto_switch')


def health_check_interval() -> int:
    try:
        return max(30, int(get('health_check_interval')))
    except (TypeError, ValueError):
        return DEFAULTS['health_check_interval']


def temperature() -> float:
    try:
        return float(get('temperature'))
    except (TypeError, ValueError):
        return DEFAULTS['temperature']


def max_rounds() -> int:
    try:
        return max(1, int(get('max_rounds')))
    except (TypeError, ValueError):
        return DEFAULTS['max_rounds']


def request_timeout() -> int:
    try:
        return int(get('request_timeout'))
    except (TypeError, ValueError):
        return DEFAULTS['request_timeout']


def reasoning_effort() -> str:
    v = str(get('reasoning_effort') or '').strip().lower()
    return v if v in ('minimal', 'low', 'medium', 'high') else ''


def prefix() -> str:
    v = _load_override().get('prefix')
    if v is None:
        v = _yaml('prefix', None)
    if v is None:
        v = DEFAULTS['prefix']
    return str(v)


def allow_at_trigger() -> bool:
    return _get_bool('allow_at_trigger')


def bot_name() -> str:
    return str(get('bot_name') or DEFAULTS['bot_name'])


def personality() -> str:
    return str(get('personality') or DEFAULTS['personality'])


def system_prompt() -> str:
    return str(get('system_prompt') or '')


def send_confirm_message() -> bool:
    return _get_bool('send_confirm_message')


def confirm_message() -> str:
    return str(get('confirm_message') or DEFAULTS['confirm_message'])


def safety_filter() -> bool:
    return _get_bool('safety_filter')


def enable_tools() -> bool:
    return _get_bool('enable_tools')


def long_text_forward() -> bool:
    return _get_bool('long_text_forward')


def forward_threshold() -> int:
    try:
        return max(1, int(get('forward_threshold')))
    except (TypeError, ValueError):
        return DEFAULTS['forward_threshold']


def random_reply() -> bool:
    return _get_bool('random_reply')


def random_reply_chance() -> float:
    """随机回复概率 (0-100, 单位 %); 例如 1 表示 1% 概率触发。"""
    try:
        v = float(get('random_reply_chance'))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, v))


def max_context_turns() -> int:
    try:
        return max(1, int(get('max_context_turns')))
    except (TypeError, ValueError):
        return DEFAULTS['max_context_turns']


def context_expire_seconds() -> int:
    try:
        return int(get('context_expire_seconds'))
    except (TypeError, ValueError):
        return DEFAULTS['context_expire_seconds']


def owner_qqs() -> list:
    """插件自定义主人列表 (逗号/空格/换行分隔); 为空则回退框架 owner.ids。"""
    raw = str(get('owner_qqs') or '').strip()
    ids = []
    if raw:
        for part in raw.replace('，', ',').replace('\n', ',').replace(' ', ',').split(','):
            part = part.strip()
            if part:
                ids.append(part)
    if ids:
        return ids
    fw = cfg.get('settings', 'owner.ids', []) or []
    return [str(x) for x in fw if str(x).strip()]


def is_owner(user_id) -> bool:
    return str(user_id) in owner_qqs()


def is_configured() -> bool:
    return bool(api_key())


def public_config() -> dict:
    """返回可暴露给前端的配置 (不含 api_key 明文)。"""
    return {
        'enabled': enabled(),
        'ytea_base_url': YTEA_BASE_URL,
        'model': model(),
        'model_priority': model_priority(),
        'auto_switch': auto_switch(),
        'health_check_interval': health_check_interval(),
        'temperature': temperature(),
        'max_rounds': max_rounds(),
        'request_timeout': request_timeout(),
        'reasoning_effort': reasoning_effort(),
        'prefix': prefix(),
        'allow_at_trigger': allow_at_trigger(),
        'bot_name': bot_name(),
        'personality': personality(),
        'system_prompt': system_prompt(),
        'send_confirm_message': send_confirm_message(),
        'confirm_message': confirm_message(),
        'safety_filter': safety_filter(),
        'enable_tools': enable_tools(),
        'long_text_forward': long_text_forward(),
        'forward_threshold': forward_threshold(),
        'random_reply': random_reply(),
        'random_reply_chance': random_reply_chance(),
        'max_context_turns': max_context_turns(),
        'context_expire_seconds': context_expire_seconds(),
        'owner_qqs': str(get('owner_qqs') or ''),
        'api_key_set': is_configured(),
    }
