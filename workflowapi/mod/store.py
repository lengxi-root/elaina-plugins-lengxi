"""工作流存储: data/commands.json 的读写、校验、默认值。

JSON 是唯一数据源 —— 每条工作流是一张「节点图」(nodes + connections),
触发节点的匹配规则即指令正则。插件启动时逐条注册独立 handler; 文件变更时热重载。

schema v2 (节点图):
{
  "version": 2,
  "settings": {"enabled": bool, "default_timeout": int},
  "workflows": [
    {"id","name","enabled",
     "nodes": [{"id","type","x","y","data":{...}}],
     "connections": [{"from_node","from_output","to_node"}]}
  ]
}
节点类型: trigger / api / condition / reply / set_var / delay
"""

import json
import os
import re
import time

# 插件根目录 (mod/ 的上一级) 与数据文件路径
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, 'data')
COMMANDS_FILE = os.path.join(DATA_DIR, 'commands.json')

SCHEMA_VERSION = 2

NODE_TYPES = ('trigger', 'api', 'condition', 'reply', 'set_var', 'delay')

# ==================== 默认内容 (含一个示例工作流, 方便小白照抄) ====================

DEFAULT_DATA = {
    'version': SCHEMA_VERSION,
    'settings': {'enabled': True, 'default_timeout': 15},
    'workflows': [
        {
            'id': 'example_ip',
            'name': 'IP查询(示例)',
            'enabled': False,
            'nodes': [
                {
                    'id': 'n_trigger',
                    'type': 'trigger',
                    'x': 80,
                    'y': 160,
                    'data': {
                        'trigger_type': 'regex',
                        'trigger_content': r'^ip\s+(\S+)$',
                        'owner_only': False,
                        'group_only': False,
                        'direct_only': False,
                        'channel_only': False,
                        'ignore_at_check': False,
                        'priority': 0,
                        'cooldown': 0,
                        'event_types': [],
                    },
                },
                {
                    'id': 'n_api',
                    'type': 'api',
                    'x': 360,
                    'y': 160,
                    'data': {
                        'method': 'GET',
                        'url': 'https://ip.011102.xyz/{$1}',
                        'headers': {},
                        'body': '',
                        'body_type': 'json',
                        'timeout': 10,
                        'response': 'json',
                        'save_as': 'r1',
                    },
                },
                {
                    'id': 'n_reply',
                    'type': 'reply',
                    'x': 640,
                    'y': 160,
                    'data': {
                        'reply_type': 'text',
                        'content': '查询: {$1}\n国家: {r1.country}\n城市: {r1.city}',
                    },
                },
            ],
            'connections': [
                {'from_node': 'n_trigger', 'from_output': 'output_1', 'to_node': 'n_api'},
                {'from_node': 'n_api', 'from_output': 'output_1', 'to_node': 'n_reply'},
            ],
        }
    ],
}


# ==================== 读写 ====================


def ensure_file():
    """确保 data/commands.json 存在 (不存在则写入默认)。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.isfile(COMMANDS_FILE):
        write_data(DEFAULT_DATA)


def read_data():
    """读取并返回完整数据 dict; 文件缺失/损坏返回默认副本。"""
    if not os.path.isfile(COMMANDS_FILE):
        return json.loads(json.dumps(DEFAULT_DATA))
    try:
        with open(COMMANDS_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return json.loads(json.dumps(DEFAULT_DATA))
    return normalize(data)


def write_data(data):
    """原子写入 (tmp + replace), 避免热重载读到半截文件。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    data = normalize(data)
    tmp = COMMANDS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, COMMANDS_FILE)
    return data


def mtime():
    """返回 commands.json 的修改时间 (不存在返回 0)。"""
    try:
        return os.path.getmtime(COMMANDS_FILE)
    except OSError:
        return 0.0


# ==================== 基础类型助手 ====================


def _str(v, default=''):
    return v if isinstance(v, str) else (default if v is None else str(v))


def _bool(v, default=False):
    return bool(v) if isinstance(v, bool) else default


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _num(v, default=0):
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return default


# ==================== 校验 / 规范化 ====================

_VALID_TRIGGER = ('exact', 'prefix', 'startswith', 'contains', 'regex', 'any')
_VALID_REPLY = ('text', 'image', 'voice', 'video', 'file', 'ark', 'markdown')
_VALID_COND = (
    'contains',
    'not_contains',
    'equals',
    'ne',
    'regex',
    'random',
    'user_id',
    'group_id',
    'empty',
    'not_empty',
    'var_equals',
    'var_gt',
    'var_lt',
    'time_range',
    'weekday_in',
    'expression',
)


def normalize(data):
    """补全缺失字段, 过滤非法值, 保证结构稳定。兼容旧版 (commands -> workflows)。"""
    if not isinstance(data, dict):
        data = {}
    raw = data.get('workflows')
    if not isinstance(raw, list):
        # 兼容字段名 commands
        raw = data.get('commands') if isinstance(data.get('commands'), list) else []
    out = {
        'version': SCHEMA_VERSION,
        'settings': _norm_settings(data.get('settings')),
        'workflows': [],
    }
    for w in raw:
        nw = _norm_workflow(w)
        if nw:
            out['workflows'].append(nw)
    return out


def _norm_settings(s):
    if not isinstance(s, dict):
        s = {}
    return {
        'enabled': _bool(s.get('enabled'), True),
        'default_timeout': max(1, _int(s.get('default_timeout'), 15)),
    }


def _norm_workflow(w):
    if not isinstance(w, dict):
        return None
    wid = _str(w.get('id')).strip() or f'wf_{int(time.time() * 1000)}'
    nodes = w.get('nodes') if isinstance(w.get('nodes'), list) else []
    conns = w.get('connections') if isinstance(w.get('connections'), list) else []
    return {
        'id': wid,
        'name': _str(w.get('name')) or wid,
        'enabled': _bool(w.get('enabled'), True),
        'nodes': [n for n in (_norm_node(x) for x in nodes) if n],
        'connections': [c for c in (_norm_conn(x) for x in conns) if c],
    }


def _norm_node(n):
    if not isinstance(n, dict):
        return None
    ntype = _str(n.get('type'))
    if ntype not in NODE_TYPES:
        return None
    nid = _str(n.get('id')).strip() or f'node_{int(time.time() * 1000)}'
    data = n.get('data') if isinstance(n.get('data'), dict) else {}
    norm = {
        'trigger': _norm_trigger,
        'api': _norm_api,
        'condition': _norm_condition,
        'reply': _norm_reply,
        'set_var': _norm_setvar,
        'delay': _norm_delay,
    }[ntype](data)
    return {'id': nid, 'type': ntype, 'x': _num(n.get('x'), 80), 'y': _num(n.get('y'), 80), 'data': norm}


def _norm_conn(c):
    if not isinstance(c, dict):
        return None
    fn = _str(c.get('from_node'))
    tn = _str(c.get('to_node'))
    if not fn or not tn:
        return None
    out = _str(c.get('from_output'), 'output_1') or 'output_1'
    if out in ('output', 'output-1'):
        out = 'output_1'
    elif out == 'output-2':
        out = 'output_2'
    if out not in ('output_1', 'output_2'):
        out = 'output_1'
    return {'from_node': fn, 'from_output': out, 'to_node': tn}


def _norm_trigger(d):
    mode = _str(d.get('trigger_type'), 'regex')
    if mode not in _VALID_TRIGGER:
        mode = 'regex'
    et = d.get('event_types')
    et = [_str(x) for x in et if _str(x)] if isinstance(et, list) else []
    return {
        'trigger_type': mode,
        'trigger_content': _str(d.get('trigger_content')),
        'owner_only': _bool(d.get('owner_only')),
        'group_only': _bool(d.get('group_only')),
        'direct_only': _bool(d.get('direct_only')),
        'channel_only': _bool(d.get('channel_only')),
        'ignore_at_check': _bool(d.get('ignore_at_check')),
        'priority': _int(d.get('priority')),
        'cooldown': max(0, _int(d.get('cooldown'))),
        'event_types': et,
    }


def _norm_api(d):
    method = _str(d.get('method'), 'GET').upper()
    if method not in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH'):
        method = 'GET'
    body_type = _str(d.get('body_type'), 'json')
    if body_type not in ('json', 'form', 'raw'):
        body_type = 'json'
    resp = _str(d.get('response'), 'json')
    if resp not in ('json', 'text', 'binary'):
        resp = 'json'
    headers = d.get('headers') if isinstance(d.get('headers'), dict) else {}
    headers = {_str(k): _str(v) for k, v in headers.items()}
    body = d.get('body')
    if not isinstance(body, str | dict | list):
        body = ''
    return {
        'method': method,
        'url': _str(d.get('url')),
        'headers': headers,
        'body': body,
        'body_type': body_type,
        'timeout': max(1, _int(d.get('timeout'), 15)),
        'response': resp,
        'save_as': _str(d.get('save_as')) or 'r1',
        'output_template': _str(d.get('output_template') or d.get('reply_template')),
    }


def _norm_condition(d):
    ctype = _str(d.get('condition_type'), 'contains')
    if ctype not in _VALID_COND:
        ctype = 'contains'
    return {
        'condition_type': ctype,
        'condition_value': _str(d.get('condition_value')),
        'var_name': _str(d.get('var_name')),
    }


def _norm_reply(d):
    rtype = _str(d.get('reply_type'), 'text')
    if rtype not in _VALID_REPLY:
        rtype = 'text'
    btns = d.get('buttons')
    btns = btns if isinstance(btns, list) else []
    img_src = _str(d.get('image_source'), 'url')
    if img_src not in ('url', 'response'):
        img_src = 'url'
    return {
        'reply_type': rtype,
        'content': _str(d.get('content')),
        'image_source': img_src,
        'image_value': _str(d.get('image_value')),
        'from_step': _str(d.get('from_step')),
        'file_name': _str(d.get('file_name')),
        'ark_template_id': _int(d.get('ark_template_id'), 23),
        'ark_kv': d.get('ark_kv') if isinstance(d.get('ark_kv'), list | dict) else [],
        'buttons': btns,
    }


def _norm_setvar(d):
    return {'var_name': _str(d.get('var_name')), 'var_value': _str(d.get('var_value'))}


def _norm_delay(d):
    return {'seconds': max(0, _num(d.get('seconds'), 1))}


# ==================== 正则编译 ====================


def build_pattern(trigger):
    """根据触发节点 data 把匹配配置转成正则字符串。"""
    mode = trigger.get('trigger_type', 'regex')
    value = trigger.get('trigger_content', '') or ''
    if mode == 'exact':
        return f'^{re.escape(value)}$'
    if mode in ('prefix', 'startswith'):
        return f'^{re.escape(value)}'
    if mode == 'contains':
        return re.escape(value)
    if mode == 'any':
        parts = [re.escape(k.strip()) for k in value.split('|') if k.strip()]
        return '|'.join(parts) if parts else ''
    return value  # regex: 原样使用


def find_trigger(workflow):
    """返回工作流的首个 trigger 节点 (没有则 None)。"""
    for n in workflow.get('nodes', []):
        if n.get('type') == 'trigger':
            return n
    return None
