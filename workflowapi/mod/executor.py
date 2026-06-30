"""工作流执行引擎: 按「节点图」遍历执行。

触发节点命中后, 从它出发沿 connections 深度遍历:
  - api      : 发 HTTP 请求, 结果存 save_as, 走 output_1
  - condition: 判断真假, 真走 output_1 假走 output_2 (分支)
  - reply    : 发送任意类型消息 (接入框架全部接口)
  - set_var  : 写入变量供后续节点引用
  - delay    : 等待若干秒
变量: {1}/{$1} 正则分组, {content}/{user_id}.. 基础, {date}/{random1-100} 动态,
      {r1.json.路径} API 响应取值 (支持 a.b[0].c), {自定义变量} set_var 写入。
"""

import asyncio
import json
import random
import re
import time
from datetime import datetime

import aiohttp
from core.base.logger import PLUGIN, get_logger

log = get_logger(PLUGIN, '工作流API')

# 共享 HTTP 会话
_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()

_TOKEN_RE = re.compile(r'\{([^{}]+)\}')
_WEEKDAYS = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
_DAYMAP = {'周一': 1, '周二': 2, '周三': 3, '周四': 4, '周五': 5, '周六': 6, '周日': 0,
           '星期一': 1, '星期二': 2, '星期三': 3, '星期四': 4, '星期五': 5, '星期六': 6, '星期日': 0}
_MAX_NODES = 100  # 单次执行最多访问节点数, 防环


async def http_session() -> aiohttp.ClientSession:
    global _session
    if _session is not None and not _session.closed:
        return _session
    async with _session_lock:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=30, ttl_dns_cache=300))
        return _session


async def close_http_session():
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


# ==================== 变量上下文 ====================


def build_context(event, match):
    """组装基础变量字典 (事件字段 + 正则分组)。返回 (base_vars, store)。"""
    base = {
        'content': getattr(event, 'content', '') or '',
        'raw_content': getattr(event, 'raw_content', '') or '',
        'user_id': getattr(event, 'user_id', '') or '',
        'group_id': getattr(event, 'group_id', '') or '',
        'channel_id': getattr(event, 'channel_id', '') or '',
        'guild_id': getattr(event, 'guild_id', '') or '',
        'username': getattr(event, 'username', '') or '',
        'message_id': getattr(event, 'message_id', '') or '',
        'message_type': getattr(event, 'message_type', '') or '',
        'event_type': getattr(event, 'event_type', '') or '',
        'appid': getattr(event, 'appid', '') or '',
        'image_url': getattr(event, 'image_url', '') or '',
    }
    if match is not None:
        try:
            base['0'] = match.group(0) or ''
            base['$0'] = base['0']
            for i, g in enumerate(match.groups(), 1):
                v = g if g is not None else ''
                base[str(i)] = v
                base[f'${i}'] = v
            for k, v in (match.groupdict() or {}).items():
                base[k] = v if v is not None else ''
        except Exception:
            pass
    return base, {}


def _dynamic(token):
    """时间/随机类动态变量; 命中返回字符串, 否则返回 None。"""
    now = datetime.now()
    table = {
        'date': now.strftime('%Y-%m-%d'),
        'today': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H:%M:%S'),
        'datetime': now.strftime('%Y-%m-%d %H:%M:%S'),
        'timestamp': str(int(time.time())),
        'year': now.strftime('%Y'),
        'month': now.strftime('%m'),
        'day': now.strftime('%d'),
        'hour': now.strftime('%H'),
        'minute': now.strftime('%M'),
        'second': now.strftime('%S'),
        'weekday': _WEEKDAYS[now.weekday()],
        'random': str(random.randint(0, 999999)),
    }
    if token in table:
        return table[token]
    m = re.fullmatch(r'random(-?\d+)-(-?\d+)', token)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return str(random.randint(lo, hi))
    return None


# ==================== JSON 路径取值 ====================


def extract_path(obj, path):
    """按 a.b[0].c 形式从对象取值; 取不到返回 ''。"""
    if not path:
        return _stringify(obj)
    cur = obj
    for part in _split_path(path):
        if isinstance(part, int):
            if isinstance(cur, list | tuple) and -len(cur) <= part < len(cur):
                cur = cur[part]
            else:
                return ''
        else:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return ''
    return _stringify(cur)


def _split_path(path):
    """把 'a.b[0].c' 拆成 ['a','b',0,'c']。"""
    out = []
    for seg in path.split('.'):
        m = re.match(r'^([^\[\]]*)((?:\[\d+\])*)$', seg)
        if not m:
            out.append(seg)
            continue
        key, idxs = m.group(1), m.group(2)
        if key:
            out.append(key)
        for im in re.finditer(r'\[(\d+)\]', idxs):
            out.append(int(im.group(1)))
    return out


def _stringify(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, str):
        return v
    if isinstance(v, int | float):
        return str(v)
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)


# ==================== 模板渲染 ====================


def resolve_token(token, base, store):
    """解析单个 {token}; 顺序: 保存的响应 > 基础/自定义变量 > 动态变量。"""
    token = token.strip()
    head, _, rest = token.partition('.')
    if head in store:
        entry = store[head]
        if not rest:
            return entry.get('text', '')
        if rest == '__status__':
            return str(entry.get('status', ''))
        if rest == '__text__':
            return entry.get('text', '')
        if rest in ('__output__', 'out'):
            return entry.get('output', entry.get('text', ''))
        data = entry.get('json')
        return '' if data is None else extract_path(data, rest)
    if token in base:
        return _stringify(base[token])
    if head in base and not rest:
        return _stringify(base[head])
    dyn = _dynamic(token)
    if dyn is not None:
        return dyn
    return ''


def render(value, base, store):
    """递归渲染: 字符串中的 {token} 替换; dict/list 递归 (保留结构)。"""
    if isinstance(value, str):
        return _TOKEN_RE.sub(lambda m: resolve_token(m.group(1), base, store), value)
    if isinstance(value, dict):
        return {render(k, base, store): render(v, base, store) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, base, store) for v in value]
    return value


# ==================== 条件判断 ====================


def eval_condition(data, base, store):
    """条件节点求值, 返回 True/False。"""
    ctype = data.get('condition_type', 'contains')
    val = render(data.get('condition_value', ''), base, store)
    content = base.get('content', '')
    var_name = data.get('var_name', '')
    var_val = _stringify(base.get(var_name, '')) if var_name else ''

    try:
        if ctype == 'contains':
            return val in content
        if ctype == 'not_contains':
            return val not in content
        if ctype == 'equals':
            return content == val
        if ctype == 'ne':
            return content != val
        if ctype == 'regex':
            return re.search(val, content) is not None
        if ctype == 'random':
            return random.random() * 100 < float(val or 0)
        if ctype == 'user_id':
            return base.get('user_id', '') == val
        if ctype == 'group_id':
            return base.get('group_id', '') == val
        if ctype == 'empty':
            return var_val == '' if var_name else content == ''
        if ctype == 'not_empty':
            return var_val != '' if var_name else content != ''
        if ctype == 'var_equals':
            return var_val == val
        if ctype == 'var_gt':
            return float(var_val or 0) > float(val or 0)
        if ctype == 'var_lt':
            return float(var_val or 0) < float(val or 0)
        if ctype == 'time_range':
            s, e = (int(x.strip()) for x in val.split('-'))
            h = datetime.now().hour
            return s <= h <= e if s <= e else (h >= s or h <= e)
        if ctype == 'weekday_in':
            today = datetime.now().weekday()  # 0=周一
            today_sun = (today + 1) % 7  # 0=周日, 兼容数字
            days = [d.strip() for d in val.split('|') if d.strip()]
            for d in days:
                if d in _DAYMAP and _DAYMAP[d] == today_sun:
                    return True
                if d.isdigit() and int(d) == today_sun:
                    return True
            return False
        if ctype == 'expression':
            return _safe_expr(val)
    except Exception as e:
        log.debug(f'条件求值失败 [{ctype}]: {e}')
    return False


def _safe_expr(expr):
    """极简安全比较表达式: 仅支持 a op b (== != > < >= <=) 与 and/or。"""
    expr = expr.strip()
    for sep, fn in (('&&', all), ('||', any)):
        if sep in expr:
            return fn(_safe_expr(p) for p in expr.split(sep))
    m = re.match(r'^\s*(.+?)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$', expr)
    if not m:
        return bool(expr) and expr.lower() not in ('false', '0', '')
    a, op, b = m.group(1).strip(), m.group(2), m.group(3).strip()

    def _n(x):
        try:
            return float(x)
        except ValueError:
            return None
    na, nb = _n(a), _n(b)
    if na is not None and nb is not None:
        a, b = na, nb
    return {'==': a == b, '!=': a != b, '>': a > b, '<': a < b, '>=': a >= b, '<=': a <= b}[op]


# ==================== API 调用 ====================


def _render_json_body(body, base, store):
    """渲染 JSON 请求体, 保证变量值被安全地放入 JSON。

    关键: 先把模板按 JSON 结构解析, 再对结构内的字符串叶子渲染 ——
    这样变量值里出现的引号/反斜杠/换行等会由序列化器正确转义, 不会破坏 JSON。
    若模板本身不是合法 JSON (如往数值位插变量 {"id":{user_id}}), 退回先渲染再解析。
    """
    if isinstance(body, dict | list):
        return render(body, base, store)
    if isinstance(body, str):
        try:
            structure = json.loads(body)
        except Exception:
            structure = None
        if structure is not None:
            return render(structure, base, store)
        rendered = render(body, base, store)
        if isinstance(rendered, str):
            try:
                return json.loads(rendered)
            except Exception:
                return rendered
        return rendered
    return body


async def run_api(request, base, store):
    """执行一次 HTTP 请求, 返回 {status, text, json, bytes, error}。"""
    url = render(request.get('url', ''), base, store)
    if not url:
        return {'status': 0, 'text': '', 'json': None, 'bytes': b'', 'error': 'URL 为空'}
    method = request.get('method', 'GET').upper()
    headers = render(request.get('headers', {}), base, store)
    timeout = aiohttp.ClientTimeout(total=request.get('timeout', 15))
    resp_kind = request.get('response', 'json')

    kwargs = {'headers': headers, 'timeout': timeout}
    body_type = request.get('body_type', 'json')
    body = request.get('body', '')
    if method in ('POST', 'PUT', 'PATCH', 'DELETE') and body not in ('', None):
        if body_type == 'json':
            kwargs['json'] = _render_json_body(body, base, store)
        elif body_type == 'form':
            rendered = render(body, base, store)
            kwargs['data'] = {str(k): str(v) for k, v in rendered.items()} if isinstance(rendered, dict) else rendered
        else:
            rendered = render(body, base, store)
            kwargs['data'] = rendered if isinstance(rendered, str | bytes) else json.dumps(rendered, ensure_ascii=False)

    try:
        session = await http_session()
        async with session.request(method, url, **kwargs) as r:
            raw = await r.read()
            text = raw.decode('utf-8', errors='replace') if resp_kind != 'binary' else ''
            parsed = None
            if resp_kind == 'json':
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = None
            return {'status': r.status, 'text': text, 'json': parsed, 'bytes': raw, 'error': ''}
    except Exception as e:
        log.warning(f'API 请求失败 [{url}]: {e}')
        return {'status': 0, 'text': '', 'json': None, 'bytes': b'', 'error': str(e)}


# ==================== 回复发送 ====================


async def send_reply(event, data, base, store):
    """根据 reply 节点配置发送一条消息, 接入框架全部消息接口。"""
    rtype = data.get('reply_type', 'text')
    content = render(data.get('content', ''), base, store)
    buttons = render(data.get('buttons'), base, store) or None

    if rtype in ('text', 'markdown'):
        if content or buttons:
            await event.reply(content, buttons=buttons)
        return
    if rtype == 'image':
        if data.get('image_source') == 'response':
            src = data.get('from_step', '') or _last_step_key(store)
            blob = store.get(src, {}).get('bytes', b'')
            if blob:
                await event.reply_image(blob, content=content)
            return
        url = render(data.get('image_value', '') or content, base, store)
        if url:
            await event.reply_image(url, content='' if url == content else content)
        return
    if rtype == 'voice':
        if content:
            await event.reply_voice(content)
        return
    if rtype == 'video':
        if content:
            await event.reply_video(content)
        return
    if rtype == 'file':
        if content:
            await event.reply_file(content, file_name=render(data.get('file_name', ''), base, store) or None)
        return
    if rtype == 'ark':
        kv = _build_ark_kv(data.get('ark_kv'), base, store)
        if kv:
            await event.reply_ark(data.get('ark_template_id', 23), kv, content=content)
        return


def _build_ark_kv(ark_kv, base, store):
    if isinstance(ark_kv, dict):
        ark_kv = [ark_kv]
    if not isinstance(ark_kv, list) or not ark_kv:
        return None
    if all(isinstance(x, dict) for x in ark_kv):
        return [{'key': render(x.get('key', ''), base, store), 'value': render(x.get('value', ''), base, store)} for x in ark_kv]
    return [render(x if isinstance(x, str) else str(x), base, store) for x in ark_kv]


def _last_step_key(store):
    keys = list(store.keys())
    return keys[-1] if keys else ''


# ==================== 图遍历 ====================


def _next_nodes(conns, node_id, output):
    return [c['to_node'] for c in conns if c['from_node'] == node_id and c.get('from_output', 'output_1') == output]


async def _run_node(node_id, nodes, conns, event, base, store, default_timeout, visited):
    """从 node_id 开始深度执行; 节点产出决定走哪个 output 分支。"""
    if node_id not in nodes or len(visited) >= _MAX_NODES:
        return
    visited.append(node_id)
    node = nodes[node_id]
    ntype, data = node['type'], node.get('data', {})
    output = 'output_1'

    try:
        if ntype == 'api':
            req = dict(data)
            req.setdefault('timeout', default_timeout)
            key = data.get('save_as') or 'r1'
            store[key] = await run_api(req, base, store)
            tpl = (data.get('output_template') or '').strip()
            if tpl:
                store[key]['output'] = render(tpl, base, store)
        elif ntype == 'condition':
            output = 'output_1' if eval_condition(data, base, store) else 'output_2'
        elif ntype == 'reply':
            d = dict(data)
            d.setdefault('from_step', _last_step_key(store))
            await send_reply(event, d, base, store)
        elif ntype == 'set_var':
            name = data.get('var_name', '')
            if name:
                base[name] = render(data.get('var_value', ''), base, store)
        elif ntype == 'delay':
            await asyncio.sleep(min(float(data.get('seconds', 1) or 0), 30))
        # trigger 节点本身不产出, 直接往下走 output_1
    except Exception as e:
        log.warning(f'节点执行异常 [{node_id}/{ntype}]: {e}')

    for nxt in _next_nodes(conns, node_id, output):
        await _run_node(nxt, nodes, conns, event, base, store, default_timeout, visited)


async def execute_workflow(workflow, event, match, default_timeout=15):
    """触发命中后执行整张图: 从 trigger 节点出发。"""
    nodes = {n['id']: n for n in workflow.get('nodes', [])}
    conns = workflow.get('connections', [])
    trigger = next((n for n in workflow.get('nodes', []) if n['type'] == 'trigger'), None)
    if not trigger:
        return
    base, store = build_context(event, match)
    try:
        await _run_node(trigger['id'], nodes, conns, event, base, store, default_timeout, [])
    except Exception as e:
        log.warning(f"工作流执行异常 [{workflow.get('name')}]: {e}")
