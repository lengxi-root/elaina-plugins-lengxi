"""发包/取包功能 (从 NapCat aicat 移植到 ElainaBot OneBot v11)。

指令 (主人可用):
  api {json}            调用任意 OneBot 动作, 支持 {"action":..,"params":..} 或首行动作名+JSON
  pb {json}             以 PbSendMsg 发送一个 elem (字段号 dict)
  pbl {json}            发送长消息 (合并转发底层协议)
  raw <cmd> {json}      直接对指定协议 cmd 发送数据包并回显响应
  模式取1 / 模式取2      切换取包结果展示模式 (平铺 / 嵌套)
指令 (可配置公开):
  取                     取被回复消息的 protobuf 数据
  取 <seq>               按 real_seq 取群消息
  取上一条               取被回复消息(或当前消息)的上一条
"""

import json
import re

from core.base.logger import PLUGIN, get_logger

from .protobuf import json_dumps_with_bytes, pb, process_json, random_uint

log = get_logger(PLUGIN, 'aicat')

# 取包结果展示模式: 1=平铺 2=嵌套
_packet_mode = 2


def set_packet_mode(mode: int):
    global _packet_mode
    _packet_mode = 1 if int(mode) == 1 else 2


def get_packet_mode() -> int:
    return _packet_mode


async def _call(event, action: str, params: dict):
    return await event.call_api(action, params or {})


def _resp_data(resp):
    """OneBot 响应统一取 data 段 (retcode==0 时用 data, 否则响应本身作为扁平数据)。"""
    if not isinstance(resp, dict):
        return {}
    if resp.get('retcode') == 0 and isinstance(resp.get('data'), dict):
        return resp['data']
    return resp


def _extract_real_seq(resp):
    data = _resp_data(resp)
    return data.get('real_seq') if isinstance(data, dict) else None


def _try_decode_hex(hex_str):
    if not hex_str or not isinstance(hex_str, str):
        return None
    if not all(c in '0123456789abcdefABCDEF' for c in hex_str) or len(hex_str) % 2 != 0:
        return None
    try:
        decoded = pb.decode(hex_str)
        return decoded if decoded and len(decoded) > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _extract_hex_data(obj):
    if isinstance(obj, str):
        if len(obj) > 10 and len(obj) % 2 == 0 and all(c in '0123456789abcdefABCDEF' for c in obj):
            return obj
        return None
    if isinstance(obj, dict):
        if obj.get('data') is not None:
            found = _extract_hex_data(obj['data'])
            if found:
                return found
        for value in obj.values():
            found = _extract_hex_data(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _extract_hex_data(value)
            if found:
                return found
    return None


def _decode_response_data(data):
    if isinstance(data, str):
        decoded = _try_decode_hex(data)
        return decoded if decoded is not None else data
    if isinstance(data, list):
        return [_decode_response_data(item) for item in data]
    if isinstance(data, dict):
        return {key: _decode_response_data(value) for key, value in data.items()}
    return data


async def send_packet(event, cmd: str, content) -> dict:
    """发送一个协议数据包 (content 为字段号 dict), 返回 {success, data|error}。"""
    try:
        data_hex = pb.bytes_to_hex(pb.encode(process_json(content)))
        result = await _call(event, 'send_packet', {'cmd': cmd, 'data': data_hex})

        if isinstance(result, dict):
            hex_data = _extract_hex_data(result)
            if hex_data:
                decoded = _try_decode_hex(hex_data)
                if decoded is not None:
                    return {'success': True, 'data': decoded}
            return {'success': True, 'data': _decode_response_data(result)}
        if isinstance(result, str):
            decoded = _try_decode_hex(result)
            if decoded is not None:
                return {'success': True, 'data': decoded}
        return {'success': True, 'data': result}
    except Exception as e:  # noqa: BLE001
        return {'success': False, 'error': f'发送数据包失败: {e}'}


async def send_elem(event, target_id: str, is_group: bool, content) -> dict:
    packet = {
        1: {(2 if is_group else 1): {1: int(target_id)}},
        2: {1: 1, 2: 0, 3: 0},
        3: {1: {2: process_json(content)}},
        4: random_uint(),
        5: random_uint(),
    }
    return await send_packet(event, 'MessageSvc.PbSendMsg', packet)


async def _upload_long(event, target_id: str, is_group: bool, content):
    data = {2: {1: 'MultiMsg', 2: {1: [{3: {1: {2: process_json(content)}}}]}}}
    packet = {
        2: {1: 3 if is_group else 1, 2: {2: int(target_id)}, 3: str(target_id), 4: pb.encode(data)},
        15: {1: 4, 2: 2, 3: 9, 4: 0},
    }
    resp = await send_packet(event, 'trpc.group.long_msg_interface.MsgService.SsoSendLongMsg', packet)
    if resp.get('success') and isinstance(resp.get('data'), dict):
        field2 = resp['data'].get(2)
        if isinstance(field2, dict):
            return field2.get(3)
    return None


async def send_long(event, target_id: str, is_group: bool, content) -> dict:
    resid = await _upload_long(event, target_id, is_group, content)
    if not resid:
        return {'success': False, 'error': '上传长消息失败'}
    elem = {37: {6: 1, 7: resid, 17: 0, 19: {15: 0, 31: 0, 41: 0}}}
    return await send_elem(event, target_id, is_group, elem)


async def get_message_pb(event, group_id: str, message_id: str, real_seq=None) -> dict:
    if not real_seq:
        real_seq = _extract_real_seq(await _call(event, 'get_msg', {'message_id': int(message_id)}))
        if not real_seq:
            return {'success': False, 'error': '未找到 real_seq'}
    seq = int(real_seq)
    packet = {1: {1: int(group_id), 2: seq, 3: seq}, 2: True}
    return await send_packet(event, 'trpc.msg.register_proxy.RegisterProxy.SsoGetGroupMsg', packet)


def extract_sender_info(pb_data: dict):
    try:
        field3 = pb_data.get(3) if isinstance(pb_data, dict) else None
        field6 = field3.get(6) if isinstance(field3, dict) else None
        field1_in_6 = field6.get(1) if isinstance(field6, dict) else None
        sender_qq = None
        sender_name = None
        if isinstance(field1_in_6, dict):
            if field1_in_6.get(1) is not None:
                sender_qq = str(field1_in_6[1])
            field8 = field1_in_6.get(8)
            if isinstance(field8, dict) and isinstance(field8.get(4), str):
                sender_name = field8[4]
        if sender_qq and not (sender_qq.isdigit() and 5 <= len(sender_qq) <= 12):
            sender_qq = None
        return sender_qq, sender_name
    except Exception:  # noqa: BLE001
        return None, None


def extract_body_data(pb_data: dict):
    try:
        node = pb_data.get(3).get(6).get(3).get(1).get(2)
        return node
    except Exception:  # noqa: BLE001
        return None


def _flat_node(bot_id, title, content):
    return {'type': 'node', 'data': {'user_id': bot_id, 'nickname': title,
                                     'content': [{'type': 'text', 'data': {'text': content}}]}}


def _nested_node(bot_id, title, description, content):
    return {'type': 'node', 'data': {'user_id': bot_id, 'nickname': title, 'content': [
        {'type': 'node', 'data': {'user_id': bot_id, 'nickname': '📌 说明',
                                  'content': [{'type': 'text', 'data': {'text': description}}]}},
        {'type': 'node', 'data': {'user_id': bot_id, 'nickname': '📄 数据',
                                  'content': [{'type': 'text', 'data': {'text': content}}]}},
    ]}}


def build_message_nodes(bot_id, bot_name, real_seq, sender_qq, sender_name, pb_data, onebot_data=None):
    nodes = []
    info_parts = ['📦 消息基本信息', f'Real Seq: {real_seq}']
    if sender_qq:
        info_parts.append(f'发送者QQ: {sender_qq}')
    if sender_name:
        info_parts.append(f'发送者昵称: {sender_name}')
    nodes.append({'type': 'node', 'data': {'user_id': bot_id, 'nickname': bot_name,
                                           'content': [{'type': 'text', 'data': {'text': '\n'.join(info_parts)}}]}})

    body_data = extract_body_data(pb_data)

    if _packet_mode == 1:
        if onebot_data is not None:
            nodes.append(_flat_node(bot_id, '📋', 'OneBot 数据'))
            nodes.append(_flat_node(bot_id, '📋', json.dumps(onebot_data, ensure_ascii=False, indent=2)))
        if body_data is not None:
            nodes.append(_flat_node(bot_id, '📦', 'Body 数据'))
            nodes.append(_flat_node(bot_id, '📦', json_dumps_with_bytes(body_data)))
        nodes.append(_flat_node(bot_id, '🔍', 'ProtoBuf 数据'))
        nodes.append(_flat_node(bot_id, '🔍', json_dumps_with_bytes(pb_data)))
    else:
        if onebot_data is not None:
            nodes.append(_nested_node(bot_id, '📋 OneBot 数据', 'OneBot 标准格式的消息数据',
                                      json.dumps(onebot_data, ensure_ascii=False, indent=2)))
        if body_data is not None:
            nodes.append(_nested_node(bot_id, '📦 Body 数据', 'Body 数据', json_dumps_with_bytes(body_data)))
        nodes.append(_nested_node(bot_id, '🔍 ProtoBuf 数据', 'ProtoBuf 数据', json_dumps_with_bytes(pb_data)))
    return nodes


# ─────────────────────────── 指令处理 ───────────────────────────

def _reply_id(event):
    for seg in getattr(event, 'message', []) or []:
        if isinstance(seg, dict) and seg.get('type') == 'reply':
            rid = seg.get('data', {}).get('id')
            if rid is not None:
                return str(rid)
    return None


def _bot_id(event) -> str:
    return str(getattr(event, 'self_id', '') or '')


async def _send_forward(event, group_id, nodes):
    await _call(event, 'send_group_forward_msg', {'group_id': int(group_id), 'messages': nodes})


async def handle_get_by_seq(event, target_seq: str, group_id: str):
    try:
        result = await get_message_pb(event, group_id, '', target_seq)
        if not result.get('success') or result.get('data') is None:
            await event.reply(f'❌ 未找到 Real Seq {target_seq} 的消息')
            return
        pb_data = result['data']
        sender_qq, sender_name = extract_sender_info(pb_data)
        nodes = build_message_nodes(_bot_id(event), 'Bot', int(target_seq), sender_qq, sender_name, pb_data)
        await _send_forward(event, group_id, nodes)
    except Exception as e:  # noqa: BLE001
        await event.reply(f'❌ 获取消息失败: {e}')


async def handle_get_reply(event, group_id: str):
    reply_id = _reply_id(event)
    if not reply_id:
        await event.reply('请回复要获取的消息')
        return
    try:
        msg_info = await _call(event, 'get_msg', {'message_id': int(reply_id)})
        msg_data = _resp_data(msg_info)
        real_seq = msg_data.get('real_seq') if isinstance(msg_data, dict) else None

        pb_data = None
        if real_seq:
            result = await get_message_pb(event, group_id, reply_id, real_seq)
            if result.get('success'):
                pb_data = result.get('data')

        sender = msg_data.get('sender') if isinstance(msg_data, dict) else None
        sender_qq = str(sender['user_id']) if isinstance(sender, dict) and sender.get('user_id') else None
        sender_name = sender.get('nickname') if isinstance(sender, dict) else None

        nodes = build_message_nodes(
            _bot_id(event), 'Bot', int(real_seq) if real_seq else 0,
            sender_qq, sender_name, pb_data or {}, msg_data.get('message') if isinstance(msg_data, dict) else None)
        await _send_forward(event, group_id, nodes)
    except Exception as e:  # noqa: BLE001
        await event.reply(f'❌ 获取消息失败: {e}')


async def handle_get_previous(event, group_id: str):
    try:
        target_real_seq = None
        reply_id = _reply_id(event)
        if reply_id:
            target_real_seq = _extract_real_seq(await _call(event, 'get_msg', {'message_id': int(reply_id)}))

        if not target_real_seq:
            target_real_seq = _extract_real_seq(
                await _call(event, 'get_msg', {'message_id': int(event.message_id)}))

        try:
            target_real_seq = int(target_real_seq)
        except (TypeError, ValueError):
            await event.reply('❌ 无法获取消息的 real_seq')
            return

        previous_seq = target_real_seq - 1
        result = await get_message_pb(event, group_id, '', str(previous_seq))
        if not result.get('success') or result.get('data') is None:
            await event.reply('❌ 未找到上一条消息')
            return

        pb_data = result['data']
        sender_qq, sender_name = extract_sender_info(pb_data)
        nodes = build_message_nodes(_bot_id(event), 'Bot', previous_seq, sender_qq, sender_name, pb_data)
        await _send_forward(event, group_id, nodes)
    except Exception as e:  # noqa: BLE001
        await event.reply(f'❌ 获取上一条消息失败: {e}')


async def handle_api_command(event, body: str):
    try:
        trimmed = body.strip()
        if trimmed.startswith('{'):
            parsed = json.loads(trimmed)
            action_name = parsed.get('action') or ''
            params = parsed.get('params') if isinstance(parsed.get('params'), dict) else {
                k: v for k, v in parsed.items() if k != 'action'}
        else:
            lines = trimmed.split('\n')
            action_name = lines[0].strip()
            json_part = '\n'.join(lines[1:]).strip()
            params = json.loads(json_part) if json_part else {}
        if not action_name:
            await event.reply('❌ 缺少 action 参数')
            return
        result = await _call(event, action_name, params)
        await event.reply(f'API 调用结果:\n{json.dumps(result, ensure_ascii=False, indent=2, default=str)}'[:3000])
    except Exception as e:  # noqa: BLE001
        await event.reply(f'❌ API 调用失败: {e}')


async def handle_pb_command(event, json_str: str, group_id: str):
    try:
        elem_content = json.loads(json_str)
        result = await send_elem(event, group_id, True, elem_content)
        await event.reply('✅ 发送成功' if result.get('success') else f'❌ {result.get("error")}')
    except Exception as e:  # noqa: BLE001
        await event.reply(f'❌ PB 发送失败: {e}')


async def handle_pbl_command(event, json_str: str, group_id: str):
    try:
        elem_content = json.loads(json_str)
        result = await send_long(event, group_id, True, elem_content)
        await event.reply('✅ 长消息发送成功' if result.get('success') else f'❌ {result.get("error")}')
    except Exception as e:  # noqa: BLE001
        await event.reply(f'❌ PBL 发送失败: {e}')


async def handle_raw_command(event, cmd: str, json_str: str):
    try:
        packet_content = json.loads(json_str)
        result = await send_packet(event, cmd, packet_content)
        if result.get('success'):
            await event.reply(f'✅ 发送成功\n响应:\n{json_dumps_with_bytes(result.get("data"))}'[:3000])
        else:
            await event.reply(f'❌ {result.get("error")}')
    except Exception as e:  # noqa: BLE001
        await event.reply(f'❌ RAW 发送失败: {e}')


def _clean_content(event) -> str:
    """去掉 reply/at 的 CQ 码, 取纯指令文本 (event.content 已是文本段拼接, 这里额外兜底)。"""
    text = (event.content or '')
    text = re.sub(r'\[CQ:reply,id=-?\d+\]', '', text)
    text = re.sub(r'\[CQ:at,qq=\d+\]', '', text)
    return text.strip()


_GET_BY_SEQ_RE = re.compile(r'^取\s*(\d+)$')
_API_RE = re.compile(r'^(?:api|API)\s*(\{[\s\S]*\}|\w+[\s\S]*)$')
_PB_RE = re.compile(r'^(?:pb|PB)\s*(\{[\s\S]*\})$')
_PBL_RE = re.compile(r'^(?:pbl|PBL)\s*(\{[\s\S]*\})$')
_RAW_RE = re.compile(r'^(?:raw|RAW)\s+(\S+)[\s\n]+(\{[\s\S]*\})$')


async def handle_public_commands(event) -> bool:
    """公开取包指令 (取 / 取 <seq> / 取上一条); 命中返回 True。"""
    content = _clean_content(event)
    group_id = str(event.group_id) if event.group_id else None
    if not group_id:
        return False

    m = _GET_BY_SEQ_RE.match(content)
    if m:
        await handle_get_by_seq(event, m.group(1), group_id)
        return True
    if content == '取':
        await handle_get_reply(event, group_id)
        return True
    if content == '取上一条':
        await handle_get_previous(event, group_id)
        return True
    return False


async def handle_owner_commands(event) -> bool:
    """主人发包/取包指令; 命中返回 True。"""
    content = _clean_content(event)
    group_id = str(event.group_id) if event.group_id else None

    m = _PBL_RE.match(content)
    if m and group_id:
        await handle_pbl_command(event, m.group(1), group_id)
        return True

    m = _PB_RE.match(content)
    if m and group_id:
        await handle_pb_command(event, m.group(1), group_id)
        return True

    m = _API_RE.match(content)
    if m:
        await handle_api_command(event, m.group(1))
        return True

    m = _RAW_RE.match(content)
    if m:
        await handle_raw_command(event, m.group(1), m.group(2))
        return True

    if content == '模式取1':
        set_packet_mode(1)
        await event.reply('✅ 已切换到模式1：平铺模式')
        return True
    if content == '模式取2':
        set_packet_mode(2)
        await event.reply('✅ 已切换到模式2：嵌套模式')
        return True

    return await handle_public_commands(event)
