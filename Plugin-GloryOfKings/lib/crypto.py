"""加解密原语 — 纯 Python 实现 (无第三方依赖), 与 Gitee 原版逐位对齐。"""

import os
import json
import base64

_DELTA = 0x9E3779B9
_MASK = 0xFFFFFFFF


# ==================== XXTEA ====================

def _to_uint32(buf: bytes, include_length: bool) -> list:
    n = len(buf)
    words = []
    for i in range(0, n, 4):
        v = (buf[i] if i < n else 0)
        if i + 1 < n:
            v |= buf[i + 1] << 8
        if i + 2 < n:
            v |= buf[i + 2] << 16
        if i + 3 < n:
            v |= buf[i + 3] << 24
        words.append(v & _MASK)
    if include_length:
        words.append(n & _MASK)
    return words


def _to_bytes(words: list, include_length: bool) -> bytes:
    size = len(words) << 2
    if include_length:
        length = words[-1]
        if length < 0 or length > size - 4:
            return b""
        size = length
    out = bytearray(size)
    for i in range(size):
        out[i] = (words[i >> 2] >> ((i & 3) << 3)) & 0xFF
    return bytes(out)


def _normalize_key(key: bytes) -> list:
    words = _to_uint32(key, False)
    while len(words) < 4:
        words.append(0)
    return words


def _mx(s, y, z, p, e, key) -> int:
    return (((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4)))
             ^ ((s ^ y) + (key[(p & 3) ^ e] ^ z)))) & _MASK


def xxtea_encrypt(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    words = _to_uint32(data, True)
    key_words = _normalize_key(key)
    n = len(words)
    last = n - 1
    rounds = 6 + 52 // n
    s = 0
    z = words[last]
    for _ in range(rounds):
        s = (s + _DELTA) & _MASK
        e = (s >> 2) & 3
        for i in range(last):
            y = words[i + 1]
            z = words[i] = (words[i] + _mx(s, y, z, i, e, key_words)) & _MASK
        y = words[0]
        z = words[last] = (words[last] + _mx(s, y, z, last, e, key_words)) & _MASK
    return _to_bytes(words, False)


def xxtea_decrypt(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    words = _to_uint32(data, False)
    key_words = _normalize_key(key)
    n = len(words)
    last = n - 1
    rounds = 6 + 52 // n
    s = (rounds * _DELTA) & _MASK
    y = words[0]
    while s != 0:
        e = (s >> 2) & 3
        for i in range(last, 0, -1):
            z = words[i - 1]
            y = words[i] = (words[i] - _mx(s, y, z, i, e, key_words)) & _MASK
        z = words[last]
        y = words[0] = (words[0] - _mx(s, y, z, 0, e, key_words)) & _MASK
        s = (s - _DELTA) & _MASK
    return _to_bytes(words, True)


def xxtea_encrypt_b64(data: bytes, key: bytes) -> str:
    return base64.b64encode(xxtea_encrypt(data, key)).decode("ascii")


# ==================== ASN.1 / RSA ====================

def _read_len(buf: bytes, pos: int):
    first = buf[pos]
    pos += 1
    if first < 0x80:
        return first, pos
    num = first & 0x7F
    val = int.from_bytes(buf[pos:pos + num], "big")
    return val, pos + num


def _read_tlv(buf: bytes, pos: int):
    """返回 (tag, value_bytes, next_pos)。"""
    tag = buf[pos]
    length, pos = _read_len(buf, pos + 1)
    value = buf[pos:pos + length]
    return tag, value, pos + length


def parse_public_key(b64_spki: str):
    """解析 X.509 SubjectPublicKeyInfo (base64 DER), 返回 (n, e)。"""
    der = base64.b64decode(b64_spki)
    _, spki, _ = _read_tlv(der, 0)            # 外层 SEQUENCE
    # SEQUENCE { AlgorithmIdentifier(SEQUENCE), BIT STRING }
    _, _, pos = _read_tlv(spki, 0)            # 跳过 AlgorithmIdentifier
    _, bitstr, _ = _read_tlv(spki, pos)       # BIT STRING
    rsa_der = bitstr[1:]                       # 去掉首字节 (unused-bits=0)
    _, rsa_seq, _ = _read_tlv(rsa_der, 0)     # RSAPublicKey SEQUENCE
    _, n_bytes, p2 = _read_tlv(rsa_seq, 0)    # INTEGER n
    _, e_bytes, _ = _read_tlv(rsa_seq, p2)    # INTEGER e
    n = int.from_bytes(n_bytes, "big")
    e = int.from_bytes(e_bytes, "big")
    return n, e


def _key_bytes(n: int) -> int:
    return (n.bit_length() + 7) // 8


def rsa_public_encrypt(data: bytes, n: int, e: int) -> bytes:
    """RSA PKCS#1 v1.5 公钥加密 (type 02), 自动按块切分。"""
    k = _key_bytes(n)
    max_len = k - 11
    out = bytearray()
    for off in range(0, len(data), max_len):
        chunk = data[off:off + max_len]
        ps_len = k - 3 - len(chunk)
        ps = bytearray()
        while len(ps) < ps_len:
            for b in os.urandom(ps_len - len(ps)):
                if b != 0:
                    ps.append(b)
        em = b"\x00\x02" + bytes(ps) + b"\x00" + chunk
        c = pow(int.from_bytes(em, "big"), e, n)
        out += c.to_bytes(k, "big")
    return bytes(out)


def rsa_public_decrypt(data: bytes, n: int, e: int) -> bytes:
    """RSA 公钥运算解开私钥加密的数据 (PKCS#1 v1.5 type 01), 返回明文。"""
    k = _key_bytes(n)
    out = bytearray()
    for off in range(0, len(data), k):
        block = data[off:off + k]
        m = pow(int.from_bytes(block, "big"), e, n)
        em = m.to_bytes(k, "big")
        if em[0] != 0x00 or em[1] != 0x01:
            raise ValueError("RSA 解密填充错误")
        idx = em.find(b"\x00", 2)
        if idx < 0:
            raise ValueError("RSA 解密分隔符缺失")
        out += em[idx + 1:]
    return bytes(out)


def decode_encode_res(encode_res_b64: str, n: int, e: int) -> dict:
    """解开登录返回的 encodeRes (base64), 解析为 JSON。失败返回 {}。"""
    if not encode_res_b64:
        return {}
    try:
        plain = rsa_public_decrypt(base64.b64decode(encode_res_b64), n, e)
        return json.loads(plain.decode("utf-8"))
    except Exception:
        return {}
