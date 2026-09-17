"""王者荣耀 API 客户端"""

import os
import re
import json
import time
import uuid
import base64
import asyncio
from urllib.parse import unquote

import aiohttp

from . import crypto
from .auth import POOL_AUTH_DEFAULTS, _PUB_N, _PUB_E

_MAIN = "https://kohcamp.qq.com"
# 营地玩法接口 (form-urlencoded, 非加密), 个人皮肤墙等走此 host。
_GAME = "https://ssl.kohsocialapp.qq.com:10001"
_TIMEOUT = aiohttp.ClientTimeout(total=15)

# 登录态失效 / 风控的 returnCode (供账号池逐候选回退判定)
_AUTH_FAIL_CODES = {-30001, -30003, -30100, -30107, -73, 1000}
_AUTH_FAIL_CODES_STR = {str(c) for c in _AUTH_FAIL_CODES}
# 登录态账号池基础设备头 (营地客户端 10.111.0323)
_POOL_BASE_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "User-Agent": "okhttp/4.9.1",
    "Content-Encrypt": "",
    "Accept-Encrypt": "",
    "NOENCRYPT": "1",
    "X-Client-Proto": "https",
}


class AuthFailure(Exception):
    """鉴权失效 (token 失效/风控), 上层据此回退登录态账号池。"""


# ==================== Protobuf 编/解码 (昵称搜索) ====================

def _encode_varint(value: int) -> bytes:
    """Varint 编码"""
    out = []
    value = value & 0xFFFFFFFFFFFFFFFF
    while True:
        if (value & ~0x7F) == 0:
            out.append(value & 0xFF)
            break
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def _encode_int_field(field_number: int, value: int) -> bytes:
    tag = (field_number << 3) | 0
    return _encode_varint(tag) + _encode_varint(value)


def _encode_string_field(field_number: int, value: str) -> bytes:
    data = value.encode("utf-8")
    tag = (field_number << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(data)) + data


def _build_search_protobuf(nickname: str) -> bytes:
    """构建昵称搜索请求 protobuf 二进制消息"""
    return (
        _encode_int_field(1, 1011)
        + _encode_string_field(2, nickname)
        + _encode_int_field(3, 1)
        + _encode_int_field(4, 10)
        + _encode_string_field(7, "0")
    )


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """解码 varint, 返回 (value, new_pos)"""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if (b & 0x80) == 0:
            break
        shift += 7
    return result, pos


def _decode_protobuf(data: bytes) -> dict:
    """简易 protobuf 解码, 返回 {field_number: [values]}"""
    fields: dict = {}
    pos = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            value, pos = _decode_varint(data, pos)
        elif wire_type == 2:  # length-delimited
            length, pos = _decode_varint(data, pos)
            value = data[pos:pos + length]
            pos += length
        elif wire_type == 5:  # 32-bit
            value = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        elif wire_type == 1:  # 64-bit
            value = int.from_bytes(data[pos:pos + 8], "little")
            pos += 8
        else:
            break
        fields.setdefault(field_number, []).append(value)
    return fields


def _parse_search_response(raw: bytes) -> list:
    """解析昵称搜索的 protobuf 响应"""
    results = []
    try:
        resp_fields = _decode_protobuf(raw)
        data_bytes_list = resp_fields.get(3, [])
        for data_bytes in data_bytes_list:
            if not isinstance(data_bytes, bytes):
                continue
            data_fields = _decode_protobuf(data_bytes)
            for ug_bytes in data_fields.get(1, []):
                if not isinstance(ug_bytes, bytes):
                    continue
                ug_fields = _decode_protobuf(ug_bytes)
                for user_bytes in ug_fields.get(25, []):
                    if not isinstance(user_bytes, bytes):
                        continue
                    user_fields = _decode_protobuf(user_bytes)
                    for detail_bytes in user_fields.get(2, []):
                        if not isinstance(detail_bytes, bytes):
                            continue
                        d = _decode_protobuf(detail_bytes)
                        uid = d.get(17, [0])[0]
                        nickname = d.get(2, [b""])[0]
                        avatar = d.get(3, [b""])[0]
                        level = d.get(10, [0])[0]
                        title1 = d.get(28, [b""])[0]
                        region = d.get(37, [b""])[0]
                        results.append({
                            "uid": str(uid),
                            "name": nickname.decode("utf-8", errors="replace")
                                if isinstance(nickname, bytes) else str(nickname),
                            "region": region.decode("utf-8", errors="replace")
                                if isinstance(region, bytes) else str(region),
                            "level": str(level),
                            "avatar": avatar.decode("utf-8", errors="replace")
                                if isinstance(avatar, bytes) else str(avatar),
                            "dw": title1.decode("utf-8", errors="replace")
                                if isinstance(title1, bytes) else str(title1),
                        })
    except Exception:
        pass
    return results


class WzryAPI:
    """王者营地接口客户端 (共享 aiohttp 会话, 统一走登录态账号池)"""

    __slots__ = ("_session", "_lock", "_auth_store")

    def __init__(self, auth_store=None):
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._auth_store = auth_store

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ==================== 通用请求 ====================

    async def _auth_request(self, endpoint: str, body: dict,
                            retries: int = 2, requester_qq: str = "") -> dict:
        """统一走登录态账号池签名请求。"""
        return await self._pool_request(endpoint, body, requester_qq, retries)

    # ==================== 登录态账号池请求 ====================

    @staticmethod
    def _resolve_user_key(acc: dict) -> str:
        key = acc.get("userKey") or ""
        if key:
            return key
        encode_res = acc.get("encodeRes") or ""
        if encode_res:
            return str(crypto.decode_encode_res(encode_res, _PUB_N, _PUB_E).get("userKey") or "")
        return ""

    def _build_pool_headers(self, acc: dict) -> dict:
        user_key = self._resolve_user_key(acc)
        ts = int(time.time() * 1000)
        headers = {
            **_POOL_BASE_HEADERS,
            **POOL_AUTH_DEFAULTS,
            "Host": "kohcamp.qq.com",
            "x-log-uid": str(uuid.uuid4()).upper(),
            "traceparent": f"00-{os.urandom(16).hex()}-{os.urandom(8).hex()}-01",
            "crand": str(ts),
            "token": str(acc.get("token", "")),
            "userid": str(acc.get("userId", "")),
            "gameid": "20001",
        }
        if user_key:
            nonce = f"{acc.get('userId', '')}:{uuid.uuid4().hex}:{ts}"
            payload = json.dumps({"timestamp": ts, "nonce": nonce}, separators=(",", ":"))
            headers["encodeParam"] = crypto.xxtea_encrypt_b64(
                payload.encode("utf-8"), user_key.encode("utf-8"))
        else:
            headers["specialEncodeParam"] = self._build_special_encode_param(ts)
        for src, dst in (("openId", "openid"), ("gameOpenId", "gameopenid"),
                         ("gameRoleId", "gameroleid"), ("gameServerId", "gameserverid"),
                         ("gameAreaId", "gameareaid"), ("gameUserSex", "gameusersex"),
                         ("kohDimGender", "kohdimgender")):
            if acc.get(src):
                headers[dst] = str(acc[src])
        return headers

    @staticmethod
    def _build_special_encode_param(ts: int) -> str:
        """RSA 加密的 specialEncodeParam (无 userKey 时的回退签名)。"""
        payload = json.dumps({
            "timestamp": ts,
            "nonce": f":{uuid.uuid4().hex}:{ts}",
        }, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(
            crypto.rsa_public_encrypt(payload, _PUB_N, _PUB_E)).decode("ascii")

    async def _pool_request(self, endpoint: str, body: dict,
                            requester_qq: str, retries: int = 2) -> dict:
        """遍历登录态账号池候选签名请求, 全部失效才抛 AuthFailure。"""
        store = self._auth_store
        if store is None:
            raise AuthFailure("未配置登录态账号池, 请先使用【王者wx登录】扫码登录")
        candidates = store.get_auth_candidates(requester_qq=requester_qq)
        if not candidates:
            raise AuthFailure("未找到可用的营地登录态, 请先使用【王者wx登录】扫码登录")
        url = f"{_MAIN}{endpoint}"
        last_err = ""
        request_body = json.dumps(body, separators=(",", ":"))

        for acc in candidates:
            headers = self._build_pool_headers(acc)
            user_key = self._resolve_user_key(acc)
            auth_failed = False

            for attempt in range(retries + 1):
                data = None
                try:
                    session = await self._get_session()
                    async with session.post(
                        url, data=request_body, headers=headers,
                    ) as resp:
                        enc_err = (resp.headers.get("encryptparamerr")
                                   or resp.headers.get("encryptParamErr"))
                        if enc_err:
                            last_err = (f"接口安全参数校验失败"
                                        f"(encryptParamErr={enc_err})")
                            store.mark_auth_failure(
                                acc.get("userId"), last_err)
                            auth_failed = True
                            break

                        rc = (resp.headers.get("returncode")
                              or resp.headers.get("returnCode"))
                        rmsg_raw = (resp.headers.get("returnmsg")
                                    or resp.headers.get("returnMsg") or "")
                        rmsg = self._decode_header_value(rmsg_raw)
                        text = await resp.text()

                        if (resp.headers.get("campencrypt")
                                or "").lower() == "true" and text:
                            if not user_key:
                                last_err = ("接口响应已加密, "
                                            "但当前账号缺少 userKey")
                                store.mark_auth_failure(
                                    acc.get("userId"), last_err)
                                auth_failed = True
                                break
                            try:
                                text = crypto.xxtea_decrypt(
                                    base64.b64decode(text.strip()),
                                    user_key.encode("utf-8"),
                                ).decode("utf-8").rstrip("\x00")
                            except Exception as de:
                                last_err = f"响应解密失败: {de}"
                                store.mark_auth_failure(
                                    acc.get("userId"), last_err)
                                auth_failed = True
                                break
                        data = json.loads(text) if text else {}
                except Exception as e:
                    last_err = str(e)
                    if attempt < retries:
                        await asyncio.sleep(1 * (2 ** attempt))
                        continue
                    store.mark_auth_failure(acc.get("userId"), last_err)
                    auth_failed = True
                    break

                # 请求成功, 检查响应内容
                body_rc = data.get("returnCode")
                body_msg = (data.get("returnMsg")
                            or data.get("message")
                            or data.get("msg") or "")
                if (rc in _AUTH_FAIL_CODES_STR) \
                        or (body_rc in _AUTH_FAIL_CODES) \
                        or self._is_auth_fail_msg(body_msg):
                    last_err = (body_msg or rmsg
                                or f"登录态失效(returnCode={body_rc or rc})")
                    store.mark_auth_failure(acc.get("userId"), last_err)
                    auth_failed = True
                    break

                store.mark_auth_success(acc.get("userId"))
                if body_rc is None and rc is not None:
                    data.setdefault(
                        "returnCode",
                        int(rc) if rc.lstrip("-").isdigit() else rc)
                return data

            if auth_failed:
                continue
        raise AuthFailure(last_err or "登录态账号池均不可用, 请重新扫码登录")

    @staticmethod
    def _decode_header_value(value: str) -> str:
        if not value:
            return ""
        try:
            return unquote(value)
        except Exception:
            return value

    @staticmethod
    def _is_auth_fail_msg(msg: str) -> bool:
        if not msg:
            return False
        return bool(re.search(r"登录|登录态|token|鉴权|安全参数|重新登录|权限", msg, re.I))

    # ==================== 业务接口 ====================

    async def get_profile(self, camp_id: str, requester_qq: str = "") -> dict:
        """主页/资料 (营地ID)"""
        return await self._auth_request("/game/koh/profile", {
            "targetUserId": str(camp_id),
            "targetRoleId": "0",
            "resVersion": "3",
            "recommendPrivacy": "0",
            "apiVersion": "2",
        }, requester_qq=requester_qq)

    async def get_more_battle_list(self, camp_id: str, requester_qq: str = "",
                                   last_time: int = 0) -> dict:
        """战绩列表 (营地ID)。第一页 30 场, 之后每页 10 场; 翻页传上一页最后一场的 dtEventTime"""
        return await self._auth_request("/game/morebattlelist", {
            "lastTime": int(last_time or 0),
            "recommendPrivacy": 0,
            "apiVersion": 5,
            "friendUserId": str(camp_id),
            "option": 0,
        }, requester_qq=requester_qq)

    async def get_battle_detail(self, camp_id: str, battle_type, game_svr: str,
                                relay_svr: str, target_role_id: str,
                                game_seq: str, requester_qq: str = "") -> dict:
        """单局详情"""
        return await self._auth_request("/game/battledetail", {
            "recommendPrivacy": 0,
            "battleType": battle_type,
            "gameSvr": game_svr,
            "relaySvr": relay_svr,
            "targetRoleId": str(target_role_id),
            "gameSeq": str(game_seq),
            "friendUserId": str(camp_id),
        }, requester_qq=requester_qq)

    # -------- 资料卡英雄列表 (需鉴权) --------

    async def get_profile_hero_list(self, camp_id: str, role_id: str = "0",
                                    requester_qq: str = "") -> dict:
        """资料卡常用英雄列表 (含 heroFightPower)"""
        return await self._auth_request("/game/profile/herolist", {
            "targetUserId": str(camp_id),
            "recommendPrivacy": 0,
            "targetRoleId": str(role_id),
        }, requester_qq=requester_qq)

    # -------- 昵称搜索 (需鉴权) --------

    async def search_player_by_nickname(self, nickname: str,
                                        requester_qq: str = "") -> list:
        """通过游戏昵称搜索玩家, 返回 [{uid, name, region, level, avatar, dw}]"""
        body = _build_search_protobuf(nickname)
        store = self._auth_store
        if store is None:
            raise AuthFailure("未配置登录态账号池")
        candidates = store.get_auth_candidates(requester_qq=requester_qq)
        if not candidates:
            raise AuthFailure("未找到可用的营地登录态")

        last_err = ""
        for acc in candidates:
            headers = self._build_pool_headers(acc)
            headers["Content-Type"] = "application/x-protobuf"
            try:
                session = await self._get_session()
                async with session.post(
                    f"{_MAIN}/search/getbytype",
                    data=body, headers=headers,
                ) as resp:
                    raw = await resp.read()
                return _parse_search_response(raw)
            except AuthFailure:
                raise
            except Exception as e:
                last_err = str(e)
                continue
        raise AuthFailure(last_err or "昵称搜索失败")

    # -------- 个人皮肤墙 (需鉴权) --------

    async def get_skin_list(self, camp_id: str, requester_qq: str = "") -> dict:
        """个人皮肤墙 — 拉取某营地账号已拥有的皮肤列表。"""
        store = self._auth_store
        if store is None:
            raise AuthFailure("未配置登录态账号池, 请先使用【王者wx登录】扫码登录")
        candidates = store.get_auth_candidates(requester_qq=requester_qq)
        if not candidates:
            raise AuthFailure("未找到可用的营地登录态, 请先使用【王者wx登录】扫码登录")

        url = f"{_GAME}/play/h5getheroskinlist"
        last_err = ""
        for acc in candidates:
            token = str(acc.get("token", ""))
            user_id = str(acc.get("userId", ""))
            if not token or not user_id:
                continue
            headers = {
                "content-encrypt": "",
                "accept-encrypt": "",
                "noencrypt": "1",
                "x-client-proto": "https",
                "kohdimgender": str(acc.get("kohDimGender") or "1"),
                "content-type": "application/x-www-form-urlencoded",
                "accept-encoding": "gzip",
                "user-agent": "okhttp/4.9.1",
                "token": token,
                "userid": user_id,
            }
            form = {
                "noCache": "0",
                "recommendPrivacy": "0",
                "friendUserId": str(camp_id),
                "cChannelId": "2002",
                "cCurrentGameId": "20001",
                "cGameId": "20001",
                "cGzip": "1",
                "cSystem": "android",
                "gameAreaId": str(acc.get("gameAreaId") or "0"),
                "gameId": "20001",
                "gameRoleId": str(acc.get("gameRoleId") or "0"),
                "gameServerId": str(acc.get("gameServerId") or "0"),
                "gameUserSex": str(acc.get("gameUserSex") or "1"),
                "openId": str(acc.get("openId") or ""),
                "token": token,
                "userId": user_id,
            }
            try:
                session = await self._get_session()
                async with session.post(
                    url, data=form, headers=headers, ssl=False,
                ) as resp:
                    text = await resp.text()
                data = json.loads(text) if text else {}
            except Exception as e:
                last_err = str(e)
                continue

            rc = data.get("returnCode")
            msg = (data.get("returnMsg") or data.get("message")
                   or data.get("msg") or "")
            if rc in _AUTH_FAIL_CODES or self._is_auth_fail_msg(msg):
                last_err = msg or f"登录态失效(returnCode={rc})"
                store.mark_auth_failure(acc.get("userId"), last_err)
                continue
            if rc not in (0, None):
                last_err = msg or f"接口返回 returnCode={rc}"
                continue
            store.mark_auth_success(acc.get("userId"))
            return data.get("data") or {}
        raise AuthFailure(last_err or "登录态账号池均不可用, 请重新扫码登录")

    # -------- 扩展接口（与原版 GloryOfKings 对齐） --------

    async def get_season_page(self, role_id: str, season_id: int = 0, requester_qq: str = "") -> dict:
        return await self._auth_request("/game/seasonpage", {"recommendPrivacy": 0, "seasonId": season_id, "roleId": str(role_id)}, requester_qq=requester_qq)

    async def get_fight_data(self, role_id: str, requester_qq: str = "", game_battle_type: int = 10, branch_type: int = 0, date_type: int = 2) -> dict:
        return await self._auth_request("/game/getfightdata", {"recommendPrivacy": 0, "dateType": date_type, "roleId": str(role_id), "roleFriendId": 0, "branchType": branch_type, "source": 1, "gameBattleType": game_battle_type, "card": 0}, requester_qq=requester_qq)

    async def get_rank_list(self, segment: int = 3, position: int = 0, requester_qq: str = "") -> dict:
        return await self._auth_request("/hero/getdetailranklistbyid", {"bottomTab": "", "rankId": 0, "segment": segment, "position": position, "recommendPrivacy": 0}, requester_qq=requester_qq)

    async def get_hero_record_details(self, role_id: str, hero_id: int, role_name: str = "", server_id: str = "", requester_qq: str = "") -> dict:
        return await self._auth_request("/gametoolbox/hero/record/pagedetails", {"roleId": str(role_id), "heroid": int(hero_id), "roleName": role_name, "h5Get": 1, "serverId": server_id}, requester_qq=requester_qq)

    async def get_hero_best_equip(self, hero_id: int, requester_qq: str = "") -> dict:
        return await self._auth_request("/gametoolbox/equip/hero/getherobestequip", {"heroId": int(hero_id)}, requester_qq=requester_qq)

    async def get_hero_fringe_data(self, hero_id: int, requester_qq: str = "") -> dict:
        return await self._auth_request("/gametoolbox/hero/getherofringedata", {"heroId": int(hero_id)}, requester_qq=requester_qq)

    async def get_season_usually_hero_list(self, role_id: str, requester_qq: str = "", season_id: int = 0) -> dict:
        return await self._auth_request("/hero/getseasonusaullyherolist", {"recommendPrivacy": 0, "seasonId": season_id, "roleId": str(role_id)}, requester_qq=requester_qq)

    async def get_game_hero_list(self, camp_id: str, requester_qq: str = "") -> dict:
        # 游戏侧表单接口，复用皮肤墙的 token/userid 账号池
        store = self._auth_store
        if store is None: raise AuthFailure("未配置登录态账号池")
        candidates = store.get_auth_candidates(requester_qq=requester_qq)
        if not candidates: raise AuthFailure("未找到可用的营地登录态")
        url = f"{_GAME}/play/h5getherolist"
        for acc in candidates:
            token, uid = str(acc.get("token", "")), str(acc.get("userId", ""))
            headers = {"content-encrypt":"", "accept-encrypt":"", "noencrypt":"1", "x-client-proto":"https", "content-type":"application/x-www-form-urlencoded", "user-agent":"okhttp/4.9.1", "token":token, "userid":uid}
            form = {"noCache":"0", "recommendPrivacy":"0", "friendUserId":str(camp_id), "cChannelId":"2002", "cCurrentGameId":"20001", "cGameId":"20001", "cGzip":"1", "cSystem":"android", "gameAreaId":str(acc.get("gameAreaId") or "0"), "gameId":"20001", "gameRoleId":str(acc.get("gameRoleId") or "0"), "gameServerId":str(acc.get("gameServerId") or "0"), "gameUserSex":str(acc.get("gameUserSex") or "1"), "openId":str(acc.get("openId") or ""), "token":token, "userId":uid}
            try:
                session=await self._get_session()
                async with session.post(url,data=form,headers=headers,ssl=False) as resp: data=json.loads(await resp.text())
                if data.get("returnCode") in _AUTH_FAIL_CODES: store.mark_auth_failure(uid, str(data)); continue
                store.mark_auth_success(uid)
                # 这个接口的形状不稳定 (有时顶层就是 heroList, 有时多包一层 data),
                payload = data.get("data")
                if not isinstance(payload, dict):
                    payload = data
                return {"returnCode": data.get("returnCode", 0), "data": payload}
            except Exception: continue
        raise AuthFailure("登录态账号池均不可用")

    async def get_pvp_skin_list(self) -> list:
        session = await self._get_session()
        async with session.get("https://pvp.qq.com/zlkdatasys/heroskinlist.json") as resp:
            return await resp.json(content_type=None)

    async def get_pvp_item_list(self) -> list:
        """官网装备表 (item.json, UTF-8): 出装只给 ID, 名字要靠它翻译"""
        try:
            session = await self._get_session()
            async with session.get(
                    "https://pvp.qq.com/web201605/js/item.json") as resp:
                return await resp.json(content_type=None)
        except Exception:
            return []

    async def get_hero_detail_page(self, pinyin: str) -> str:
        session = await self._get_session()
        async with session.get(f"https://pvp.qq.com/web201605/herodetail/{pinyin}.shtml") as resp:
            raw = await resp.read()
        return raw.decode("gb18030", errors="replace")

    # -------- 英雄 (公开接口) --------

    async def get_hero_fighting_capacity(self, hero_name: str) -> list:
        """英雄最低战力 (四端: 安卓QQ/安卓微信/iOSQQ/iOS微信)"""
        regions = ["aqq", "awx", "iqq", "iwx"]
        session = await self._get_session()
        out: list = []
        for region in regions:
            try:
                url = (f"https://www.sapi.run/hero/select.php"
                       f"?hero={hero_name}&type={region}")
                async with session.get(url) as resp:
                    payload = await resp.json(content_type=None)
                if payload.get("code") == 200 and payload.get("data"):
                    item = dict(payload["data"])
                    item["type"] = region
                    out.append(item)
            except Exception:
                pass
        return out

    async def get_hero_list(self) -> list:
        """全英雄列表 (pvp 官网)"""
        try:
            session = await self._get_session()
            async with session.get(
                    "https://pvp.qq.com/web201605/js/herolist.json") as resp:
                return await resp.json(content_type=None)
        except Exception:
            return []

    async def probe_skin_url(self, ename, index: int) -> bool:
        """探测某皮肤大图是否存在 (HEAD)。"""
        url = (f"https://game.gtimg.cn/images/yxzj/img201606/skin/hero-info/"
               f"{ename}/{ename}-bigskin-{index}.jpg")
        try:
            session = await self._get_session()
            async with session.head(url) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def skin_url(ename, index: int) -> str:
        return (f"https://game.gtimg.cn/images/yxzj/img201606/skin/hero-info/"
                f"{ename}/{ename}-bigskin-{index}.jpg")
