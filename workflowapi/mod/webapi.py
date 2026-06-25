"""Web 面板后端接口: 读取/保存配置、测试 API、渲染预览、元信息。

复用框架已鉴权的 aiohttp app; 所有接口都经过 require_auth 包装。
"""

import json

from aiohttp import web
from core.base.logger import PLUGIN, get_logger

from . import executor, store, watcher

log = get_logger(PLUGIN, '工作流API')

API_PREFIX = '/api/workflow-api'

# 供面板动态渲染的元信息
META = {
    'reply_types': [
        {'value': 'text', 'label': '文本'},
        {'value': 'markdown', 'label': 'Markdown'},
        {'value': 'image', 'label': '图片'},
        {'value': 'voice', 'label': '语音'},
        {'value': 'video', 'label': '视频'},
        {'value': 'file', 'label': '文件'},
        {'value': 'ark', 'label': 'ARK卡片'},
    ],
    'node_types': [
        {'value': 'trigger', 'label': '触发', 'color': '#22c55e', 'outputs': 1, 'single': True},
        {'value': 'api', 'label': 'API请求', 'color': '#3b82f6', 'outputs': 1},
        {'value': 'condition', 'label': '条件', 'color': '#f59e0b', 'outputs': 2},
        {'value': 'reply', 'label': '回复', 'color': '#8b5cf6', 'outputs': 1},
        {'value': 'set_var', 'label': '设变量', 'color': '#06b6d4', 'outputs': 1},
        {'value': 'delay', 'label': '延时', 'color': '#64748b', 'outputs': 1},
    ],
    'trigger_types': [
        {'value': 'exact', 'label': '精确匹配'},
        {'value': 'prefix', 'label': '前缀匹配'},
        {'value': 'contains', 'label': '包含匹配'},
        {'value': 'any', 'label': '任意关键词(|分隔)'},
        {'value': 'regex', 'label': '正则表达式'},
    ],
    'methods': ['GET', 'POST', 'PUT', 'DELETE', 'PATCH'],
    'condition_ops': [
        {'value': 'contains', 'label': '消息包含'},
        {'value': 'not_contains', 'label': '消息不包含'},
        {'value': 'equals', 'label': '消息等于'},
        {'value': 'ne', 'label': '消息不等于'},
        {'value': 'regex', 'label': '消息匹配正则'},
        {'value': 'random', 'label': '随机(几率%)'},
        {'value': 'user_id', 'label': '发送者ID等于'},
        {'value': 'group_id', 'label': '群ID等于'},
        {'value': 'empty', 'label': '变量/消息为空'},
        {'value': 'not_empty', 'label': '变量/消息非空'},
        {'value': 'var_equals', 'label': '变量等于'},
        {'value': 'var_gt', 'label': '变量大于'},
        {'value': 'var_lt', 'label': '变量小于'},
        {'value': 'time_range', 'label': '当前小时在区间(如9-18)'},
        {'value': 'weekday_in', 'label': '星期在(周一|周三)'},
        {'value': 'expression', 'label': '表达式(a>b)'},
    ],
    'reply_image_sources': [
        {'value': 'url', 'label': 'URL/模板'},
        {'value': 'response', 'label': 'API二进制响应'},
    ],
    'body_types': [
        {'value': 'json', 'label': 'JSON'},
        {'value': 'form', 'label': '表单'},
        {'value': 'raw', 'label': '原始'},
    ],
    'response_types': [
        {'value': 'json', 'label': 'JSON'},
        {'value': 'text', 'label': '文本'},
        {'value': 'binary', 'label': '二进制(图/音/视频)'},
    ],
    'event_types': [
        'GROUP_AT_MESSAGE_CREATE',
        'GROUP_MESSAGE_CREATE',
        'C2C_MESSAGE_CREATE',
        'AT_MESSAGE_CREATE',
        'DIRECT_MESSAGE_CREATE',
        'MESSAGE_CREATE',
        'INTERACTION_CREATE',
    ],
    'variables': [
        {'token': '{content}', 'desc': '消息内容(去@后)'},
        {'token': '{user_id}', 'desc': '发送者ID'},
        {'token': '{group_id}', 'desc': '群ID'},
        {'token': '{channel_id}', 'desc': '子频道ID'},
        {'token': '{username}', 'desc': '昵称'},
        {'token': '{appid}', 'desc': '机器人ID'},
        {'token': '{1} {2} ...', 'desc': '正则捕获组, {0}为整体匹配'},
        {'token': '{date} {time} {datetime}', 'desc': '当前日期/时间'},
        {'token': '{timestamp}', 'desc': '秒级时间戳'},
        {'token': '{weekday}', 'desc': '星期几'},
        {'token': '{random} {random1-100}', 'desc': '随机数/区间随机'},
        {'token': '{r1.字段.路径}', 'desc': 'API响应JSON取值, 支持 a.b[0].c'},
        {'token': '{r1}', 'desc': '响应原始文本'},
        {'token': '{r1.__status__}', 'desc': 'HTTP状态码'},
    ],
}


def _json(data, status=200):
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        content_type='application/json',
        status=status,
    )


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


# ==================== 接口 ====================


async def handle_get_config(request):
    return _json({'success': True, 'data': store.read_data()})


async def handle_get_meta(request):
    return _json({'success': True, 'meta': META})


async def handle_save_config(request):
    body = await _body(request)
    data = body.get('data')
    if not isinstance(data, dict):
        return _json({'success': False, 'message': '数据格式错误'}, status=400)
    # 校验各工作流触发节点的正则可编译
    import re as _re

    for w in data.get('workflows', []):
        if not isinstance(w, dict):
            continue
        trig = next((n for n in (w.get('nodes') or []) if isinstance(n, dict) and n.get('type') == 'trigger'), None)
        if not trig:
            continue
        try:
            _re.compile(store.build_pattern(trig.get('data', {})))
        except _re.error as e:
            return _json({'success': False, 'message': f"工作流「{w.get('name', '?')}」触发正则错误: {e}"}, status=400)
    saved = store.write_data(data)
    await watcher.trigger_reload()
    return _json({'success': True, 'data': saved, 'message': '已保存并热重载'})


async def handle_test_api(request):
    """服务端执行一次 API 请求, 返回原始/解析结果, 供面板可视化取字段。"""
    body = await _body(request)
    req = body.get('request')
    if not isinstance(req, dict):
        return _json({'success': False, 'message': '缺少 request'}, status=400)
    req = store._norm_api(req)
    base = {str(k): str(v) for k, v in (body.get('vars') or {}).items()}
    result = await executor.run_api(req, base, {})
    return _json(
        {
            'success': not result.get('error'),
            'status': result.get('status'),
            'text': result.get('text', '')[:20000],
            'json': result.get('json'),
            'is_binary': bool(result.get('bytes')) and not result.get('text'),
            'bytes_len': len(result.get('bytes') or b''),
            'error': result.get('error', ''),
        }
    )


async def handle_render(request):
    """渲染模板预览 (不真正发送)。"""
    body = await _body(request)
    template = body.get('template', '')
    base = {str(k): str(v) for k, v in (body.get('vars') or {}).items()}
    store_data = {}
    for k, v in (body.get('responses') or {}).items():
        if isinstance(v, dict):
            store_data[k] = {
                'text': v.get('text', '') if isinstance(v.get('text'), str) else json.dumps(v.get('json'), ensure_ascii=False),
                'json': v.get('json'),
                'status': v.get('status', 200),
                'bytes': b'',
            }
    rendered = executor.render(template, base, store_data)
    return _json({'success': True, 'rendered': rendered})


# (method, path, handler)
ROUTES = [
    ('GET', '/config', handle_get_config),
    ('GET', '/meta', handle_get_meta),
    ('POST', '/save', handle_save_config),
    ('POST', '/test_api', handle_test_api),
    ('POST', '/render', handle_render),
]
