"""Outgoing-message audit rules and OpenAI-compatible result parsing."""
from __future__ import annotations

import json
import re

from . import central, safety

AUDIT_PENDING = 0
AUDIT_PASS = 1
AUDIT_REJECT = 2

GROUP_ID_RE = re.compile(r'(?<!\d)\d{5,12}(?!\d)')
URL_RE = re.compile(r'(?i)(?:https?://|www\.)\S+')

AUDIT_PROMPT = """你是严格的聊天内容安全审核器。审核即将发送给用户的文本，检查：暴力、危险行为、血腥、色情或性暗示、政治和反动内容、违法犯罪、广告引流、国家/旗帜争议、辱骂骚扰、联系方式、虚假或误导信息，以及服务器 IP、内网地址、密钥、环境信息。
必须识别规避写法：谐音、拆字、插入符号、数字或键盘替换、表情、繁体字、错别字、编码片段、中英文混写和其他变体。
只输出一个 JSON 对象，不要 Markdown，不要解释。字段必须是：safe（1=通过，2=拒绝）、reason（简短中文原因）、violation_words（命中的词或短语数组）、score（1-100）。分数 1-60 表示通过，61-90 表示待审核，91-100 表示拒绝；待审核也必须返回 safe=2。"""


def precheck(text: str) -> dict | None:
    """Fast local checks borrowed from the supplied audit implementation."""
    if GROUP_ID_RE.search(text):
        return result(AUDIT_REJECT, '疑似群号或内部标识', ['数字标识'], 100, 'local')
    if URL_RE.search(text):
        return result(AUDIT_REJECT, '包含网址或外部链接', ['网址'], 100, 'local')
    return None


def result(safe: int, reason: str, violation_words: list[str] | None, score: int, model: str = '') -> dict:
    return {
        'safe': int(safe),
        'reason': safety.redact_ips(str(reason or ''))[:300],
        'violation_words': [safety.redact_ips(str(item))[:80] for item in (violation_words or [])][:20],
        'score': min(100, max(1, int(score))),
        'model': str(model or ''),
    }


def parse_response(content: str, model: str = '') -> dict:
    cleaned = str(content or '').strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', cleaned, flags=re.I | re.S).strip()
    match = re.search(r'\{.*\}', cleaned, flags=re.S)
    if match:
        cleaned = match.group(0)
    try:
        payload = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError):
        return result(AUDIT_PENDING, '审核接口返回格式异常', [], 70, model)
    if not isinstance(payload, dict):
        return result(AUDIT_PENDING, '审核接口返回格式异常', [], 70, model)
    try:
        score = int(payload.get('score', 70))
    except (TypeError, ValueError):
        score = 70
    safe_value = payload.get('safe')
    try:
        safe_value = int(safe_value)
    except (TypeError, ValueError):
        safe_value = 1 if score <= 60 else 2
    if safe_value not in (1, 2):
        safe_value = 1 if score <= 60 else 2
    if 61 <= score <= 90:
        safe_value = 2
    words = payload.get('violation_words', [])
    if isinstance(words, str):
        words = [words]
    if not isinstance(words, list):
        words = []
    return result(safe_value, str(payload.get('reason') or '未说明'), words, score, model)


async def audit_text(config: dict, text: str) -> dict:
    text = str(text or '')[:int(config.get('audit_max_text', 4000))]
    local = precheck(text)
    if local:
        return local
    try:
        content, model = await central.audit_completion(config, AUDIT_PROMPT, text)
        return parse_response(content, model)
    except Exception as error:
        return result(AUDIT_PENDING, f'审核接口暂时不可用：{error}', [], 70)
