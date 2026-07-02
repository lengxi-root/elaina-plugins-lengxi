"""轻量 ProtoBuf 编解码 (从 NapCat aicat packet 功能移植)。

用字段号做 key 的 dict 直接编解码, 供发包(pb/pbl/raw)与取包(取/取上一条)使用。
无需 .proto 定义: encode 接收 {字段号: 值} 的 dict, decode 返回同构 dict。
"""

import json
import random
import re

_MAX_SAFE_INT = 9007199254740991


class Writer:
    def __init__(self):
        self.buf = bytearray()

    def uint32(self, value):
        value = int(value)
        if value < 0:
            value &= (1 << 64) - 1
        while value > 0x7F:
            self.buf.append((value & 0x7F) | 0x80)
            value >>= 7
        self.buf.append(value & 0x7F)
        return self

    def int32(self, value):
        value = int(value)
        if value < 0:
            value += 1 << 32
        return self.uint32(value)

    def int64(self, value):
        num = int(value) if not isinstance(value, str) else int(value)
        if num < 0:
            num += 1 << 64
        return self.uint32(num)

    def string(self, value):
        data = str(value).encode('utf-8')
        self.uint32(len(data))
        self.buf.extend(data)
        return self

    def bool(self, value):
        return self.uint32(1 if value else 0)

    def bytes(self, value):
        arr = bytes(value)
        self.uint32(len(arr))
        self.buf.extend(arr)
        return self

    def finish(self):
        return bytes(self.buf)


class Reader:
    def __init__(self, data: bytes):
        self.buf = data
        self.pos = 0
        self.len = len(data)

    def read_varint(self):
        result = 0
        shift = 0
        max_bytes = 10
        while max_bytes > 0 and self.pos < self.len:
            max_bytes -= 1
            byte = self.buf[self.pos]
            self.pos += 1
            result |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                break
            shift += 7
        return result

    def uint32(self):
        return self.read_varint() & 0xFFFFFFFF

    def int64(self):
        value = self.read_varint()
        if value > _MAX_SAFE_INT:
            return str(value)
        return value

    def fixed32(self):
        data = self.buf[self.pos:self.pos + 4]
        self.pos += 4
        val = data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)
        if val >= 0x80000000:
            val -= 0x100000000
        return val

    def fixed64(self):
        low = self.fixed32() & 0xFFFFFFFF
        high = self.fixed32()
        return low + high * 0x100000000

    def bytes(self):
        length = self.uint32()
        data = self.buf[self.pos:self.pos + length]
        self.pos += length
        return bytes(data)


class Protobuf:
    def encode(self, obj: dict) -> bytes:
        writer = Writer()
        for tag in sorted(int(k) for k in obj):
            self._encode(writer, tag, obj[tag] if tag in obj else obj[str(tag)])
        return writer.finish()

    def _encode(self, writer: Writer, tag: int, value):
        if value is None:
            return
        if isinstance(value, float) and not value.is_integer():
            return
        if isinstance(value, bool):
            writer.uint32((tag << 3) | 0).bool(value)
        elif isinstance(value, (int, float)):
            value = int(value)
            writer.uint32((tag << 3) | 0)
            (writer.int64 if abs(value) > 2147483647 else writer.int32)(value)
        elif isinstance(value, str):
            writer.uint32((tag << 3) | 2).string(value)
        elif isinstance(value, (bytes, bytearray)):
            writer.uint32((tag << 3) | 2).bytes(value)
        elif isinstance(value, list):
            for item in value:
                self._encode(writer, tag, item)
        elif isinstance(value, dict):
            writer.uint32((tag << 3) | 2).bytes(self.encode(value))

    def decode(self, buffer) -> dict:
        if isinstance(buffer, str):
            buffer = self.hex_to_bytes(buffer)
        result: dict = {}
        reader = Reader(buffer)
        while reader.pos < reader.len:
            k = reader.uint32()
            tag = k >> 3
            wire_type = k & 0b111
            if wire_type == 0:
                value = self._long2int(reader.int64())
            elif wire_type == 1:
                value = self._long2int(reader.fixed64())
            elif wire_type == 2:
                raw = reader.bytes()
                try:
                    decoded = self.decode(raw)
                    value = self._bytes_to_readable_string(raw) if self._should_keep_as_string(decoded, raw) else decoded
                except Exception:  # noqa: BLE001
                    value = self._bytes_to_readable_string(raw)
            elif wire_type == 5:
                value = reader.fixed32()
            else:
                raise ValueError(f'Unsupported wire type: {wire_type}')

            if tag in result:
                existing = result[tag]
                result[tag] = [*existing, value] if isinstance(existing, list) else [existing, value]
            else:
                result[tag] = value
        return result

    def _bytes_to_readable_string(self, data: bytes) -> str:
        try:
            s = data.decode('utf-8')
            if self._is_readable_string(s):
                return s
        except UnicodeDecodeError:
            pass
        return 'hex->' + self.bytes_to_hex(data)

    def _is_readable_string(self, s: str) -> bool:
        if not s:
            return False
        bad = 0
        for ch in s:
            code = ord(ch)
            if (code < 32 and code not in (9, 10, 13)) or (0x7F <= code < 0xA0) or code == 0xFFFD:
                bad += 1
        return bad <= 3 and (len(s) == 0 or bad / len(s) <= 0.1)

    def _should_keep_as_string(self, decoded: dict, original: bytes) -> bool:
        try:
            s = original.decode('utf-8', errors='replace')
            if len(s.encode('utf-8')) != len(original):
                return False
            if self._has_too_many_control_chars(s):
                return False
            return self._looks_like_misdecoded_string(decoded)
        except Exception:  # noqa: BLE001
            return False

    def _has_too_many_control_chars(self, text: str) -> bool:
        if not text:
            return False
        count = 0
        for ch in text:
            code = ord(ch)
            if (code < 32 and code not in (10, 13, 9)) or (0x7F <= code < 0xA0):
                count += 1
        return count / len(text) > 0.05 or count > 10

    def _looks_like_misdecoded_string(self, value: dict) -> bool:
        keys = list(value.keys())
        key_count = len(keys)
        if key_count < 1 or key_count > 4:
            return False
        for key in keys:
            if abs(int(key)) > 1000000:
                return True
        has_string = has_list = has_nested_dict = False
        simple_count = 0
        for key in keys:
            val = value[key]
            if isinstance(val, str):
                has_string = True
                simple_count += 1
            elif isinstance(val, (int, float, bool)):
                simple_count += 1
            elif isinstance(val, list):
                has_list = True
                if any(isinstance(i, dict) for i in val):
                    has_nested_dict = True
            elif isinstance(val, dict):
                has_nested_dict = True
        return ((has_string and has_list and not has_nested_dict)
                or (simple_count == key_count and key_count <= 3)
                or (has_string and simple_count >= key_count - 1 and key_count <= 3 and not has_nested_dict))

    def _long2int(self, value):
        if isinstance(value, str):
            return value
        return value if abs(value) <= _MAX_SAFE_INT else str(value)

    def hex_to_bytes(self, hex_str: str) -> bytes:
        return bytes.fromhex(hex_str)

    def bytes_to_hex(self, data: bytes) -> str:
        return bytes(data).hex()


pb = Protobuf()


def random_uint() -> int:
    return random.randint(0, 0xFFFFFFFF)


def process_json(data):
    """把 {字段号(字符串/数字): 值} 的 JSON 结构转成 encode 可用的 dict; 支持 hex-> 前缀表示字节。"""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return {}
    return _process_recursive(data)


def _process_recursive(obj):
    if isinstance(obj, (bytes, bytearray)):
        return obj
    if isinstance(obj, list):
        return [_process_recursive(item) for item in obj]
    if isinstance(obj, dict):
        result: dict = {}
        for key, value in obj.items():
            try:
                num_key = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, str) and value.startswith('hex->'):
                hex_str = value[5:]
                if re.fullmatch(r'[0-9a-fA-F]+', hex_str) and len(hex_str) % 2 == 0:
                    result[num_key] = pb.hex_to_bytes(hex_str)
                else:
                    result[num_key] = value
            elif isinstance(value, (dict, list)):
                result[num_key] = _process_recursive(value)
            else:
                result[num_key] = value
        return result
    return obj


def _bytes_default(o):
    if isinstance(o, (bytes, bytearray)):
        return 'hex->' + bytes(o).hex()
    raise TypeError(f'Object of type {type(o).__name__} is not JSON serializable')


def json_dumps_with_bytes(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=_bytes_default)
