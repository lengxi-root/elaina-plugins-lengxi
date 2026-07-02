"""模型管理: 可用性探测(轮询) + 按优先级自动切换。

- 优先级列表 (aiconfig.model_priority) 决定候选模型顺序。
- 探测: 向 OpenAI 兼容接口发一次极小请求, 记录每个模型的可用性与延迟。
- 自动切换: 请求时按「健康优先 -> 未探测 -> 不健康」的顺序给出候选, 调用失败自动降级。
"""

import asyncio
import time

import aiohttp

from . import aiconfig

# model -> {'ok': bool|None, 'latency_ms': int, 'checked_at': float, 'error': str}
_health: dict = {}
_lock = asyncio.Lock()


def status() -> dict:
    """返回可用性快照 (供前端展示)。"""
    return {
        m: {
            'ok': v.get('ok'),
            'latency_ms': v.get('latency_ms', 0),
            'checked_at': v.get('checked_at', 0),
            'error': v.get('error', ''),
        }
        for m, v in _health.items()
    }


def record_result(model: str, ok: bool, latency_ms: int = 0, error: str = ''):
    if not model:
        return
    _health[model] = {
        'ok': ok,
        'latency_ms': int(latency_ms),
        'checked_at': time.time(),
        'error': (error or '')[:200],
    }


async def check_model(session: aiohttp.ClientSession, model: str) -> dict:
    """对单个模型做一次极小可用性探测, 返回 {ok, latency_ms, error}。"""
    base = aiconfig.base_url()
    key = aiconfig.api_key()
    url = base + '/chat/completions'
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'ping'}],
        'max_tokens': 1,
    }
    start = time.monotonic()
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        for attempt in range(2):
            async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                text = await resp.text()
                latency = int((time.monotonic() - start) * 1000)
                if resp.status == 200:
                    record_result(model, True, latency)
                    return {'ok': True, 'latency_ms': latency, 'error': ''}
                low = text.lower()
                # 部分模型不支持 max_tokens (如 gpt-5 用 max_completion_tokens), 去掉重试
                if attempt == 0 and resp.status == 400 and 'max_tokens' in low:
                    payload.pop('max_tokens', None)
                    continue
                err = f'HTTP {resp.status}: {text[:120]}'
                record_result(model, False, latency, err)
                return {'ok': False, 'latency_ms': latency, 'error': err}
    except (TimeoutError, aiohttp.ClientError, Exception) as e:  # noqa: BLE001
        latency = int((time.monotonic() - start) * 1000)
        record_result(model, False, latency, str(e))
        return {'ok': False, 'latency_ms': latency, 'error': str(e)}
    return {'ok': False, 'latency_ms': 0, 'error': 'unknown'}


async def check_models(models: list) -> dict:
    """并发探测一组模型 (限并发), 返回 {model: {ok, latency_ms, error}}。"""
    models = [m for m in dict.fromkeys(models) if m]
    if not models:
        return {}
    sem = asyncio.Semaphore(4)
    result: dict = {}

    async with aiohttp.ClientSession() as session:
        async def _run(m: str):
            async with sem:
                result[m] = await check_model(session, m)

        await asyncio.gather(*[_run(m) for m in models])
    return result


def candidate_models() -> list:
    """返回请求时应依次尝试的模型列表。

    开启自动切换且配置了优先级时: 健康 -> 未探测 -> 不健康 (均保持优先级内相对顺序),
    末尾补上默认 model 作兜底; 否则仅返回默认 model。
    """
    default = aiconfig.model()
    if not (aiconfig.auto_switch() and aiconfig.model_priority()):
        return [default] if default else []

    priority = aiconfig.model_priority()
    healthy, untested, unhealthy = [], [], []
    for m in priority:
        ok = _health.get(m, {}).get('ok')
        if ok is True:
            healthy.append(m)
        elif ok is None:
            untested.append(m)
        else:
            unhealthy.append(m)
    ordered = healthy + untested + unhealthy
    if default and default not in ordered:
        ordered.append(default)
    return ordered


async def poll_loop(interval_getter, stop_event: asyncio.Event):
    """后台轮询: 周期性探测优先级列表中的模型可用性 (仅在自动切换开启时)。"""
    while not stop_event.is_set():
        try:
            if aiconfig.auto_switch() and aiconfig.is_configured():
                models = aiconfig.model_priority()
                if models:
                    await check_models(models)
        except Exception:  # noqa: BLE001
            pass
        interval = max(30, int(interval_getter()))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except (TimeoutError, asyncio.TimeoutError):
            pass
