"""安全过滤: 清洗 AI 回复中的危险富媒体 CQ 码, 并对联网/接口回显中的公网 IP 脱敏。

- 危险 CQ 码: 防止普通用户通过 prompt 注入诱导机器人发送 图片/语音/视频/闪照 等富媒体。
- 公网 IP 脱敏: AI 调用联网工具或 OneBot 接口后, 返回内容可能含服务器公网 IP,
  统一替换为占位符, 防止公网地址泄漏给普通用户。
"""

import ipaddress
import re

# 富媒体类危险 CQ 码 (普通用户不应能诱导机器人主动发送)
_DANGEROUS_CQ_TYPES = {'image', 'record', 'video', 'flash'}
_TYPE_LABELS = {'image': '图片', 'record': '语音', 'video': '视频', 'flash': '闪照'}

_CQ_CODE_RE = re.compile(r'\[CQ:(\w+)(?:,[^\]]*)?\]')

_IPV4_RE = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
# 粗略匹配 IPv6 (含 :: 压缩), 命中后再用 ipaddress 校验是否公网, 避免误伤时间戳/MAC 等
_IPV6_RE = re.compile(r'(?<![:.\w])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![:.\w])')

_IP_PLACEHOLDER = '[已隐藏IP]'


def sanitize_reply_text(text: str) -> str:
    """清洗 AI 纯文本回复中的危险 CQ 码, 替换为提示占位符 (防 prompt 注入)。"""
    if not text:
        return text

    def _repl(m: 're.Match') -> str:
        t = m.group(1).lower()
        if t in _DANGEROUS_CQ_TYPES:
            return f'[{_TYPE_LABELS.get(t, t)}已过滤]'
        return m.group(0)

    return _CQ_CODE_RE.sub(_repl, text)


def _is_public_ip(s: str) -> bool:
    """仅公网(全局可路由)地址需要脱敏; 内网/回环/链路本地/保留段保留原样。"""
    try:
        return ipaddress.ip_address(s).is_global
    except ValueError:
        return False


def redact_ips(text: str) -> str:
    """将文本中的公网 IPv4 / IPv6 地址替换为占位符 (内网/回环地址保留)。"""
    if not text:
        return text

    def _repl(m: 're.Match') -> str:
        s = m.group(0)
        return _IP_PLACEHOLDER if _is_public_ip(s) else s

    text = _IPV4_RE.sub(_repl, text)
    return _IPV6_RE.sub(_repl, text)
