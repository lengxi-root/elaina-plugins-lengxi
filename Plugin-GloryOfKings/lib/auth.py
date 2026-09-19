"""登录态账号池 + 微信扫码登录 (营地接口唯一鉴权来源)"""

import json
import time
import uuid
import base64
import asyncio
import hashlib
import threading
import datetime

import aiohttp

from . import crypto

# 营地 / 微信登录常量 (Gitee utils/wechatLogin.js)
APPID_WX = "wxf4b1e8a3e9aaf978"
CAMP_BASE_URL = "https://ssl.kohsocialapp.qq.com:10001"
WX_QR_URL = "https://open.weixin.qq.com/connect/sdk/qrconnect"
WX_POLL_URL = "https://long.open.weixin.qq.com/connect/l/qrconnect"
DEFAULT_PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC0h62mV/zjJtFsNdfFNlxksfUOpjDI2KCc"
    "BrPiA8T7szABT4InLDTrdXAW84QyGNiazB0i7pgPCNGSAYbiJrCRutZ5jQsVS0Wg/RnXfwVQ"
    "DJcAHJDjP5IXyroeLX7NUxDai8nPcpfRsvq6sneobyPexZSH0TlVSnecsJZTj5wu/wIDAQAB")

_PUB_N, _PUB_E = crypto.parse_public_key(DEFAULT_PUBLIC_KEY)

_COMMON_HEADERS = {
    "Content-Encrypt": "",
    "Accept-Encrypt": "",
    "NOENCRYPT": "1",
    "X-Client-Proto": "https",
    "User-Agent": "okhttp/4.9.1",
}

# 登录态账号池签名/请求默认值 (营地客户端 10.111.0323, 对齐 Gitee #getBaseAuthConfig)
POOL_AUTH_DEFAULTS = {
    "cchannelid": "10003391",
    "cclientversioncode": "2057957801",
    "cclientversionname": "10.111.0323",
    "ccurrentgameid": "20001",
    "cgameid": "20001",
    "cgzip": "1",
    "cisarm64": "true",
    "csupportarm64": "true",
    "csystem": "android",
    "csystemversioncode": "34",
    "csystemversionname": "14",
    "cpuhardware": "qcom",
    "gameid": "20001",
    "tinkerid": "2057957801_64_0",
    "gameareaid": "1",
    "gameusersex": "1",
    "kohdimgender": "2",
    "istrpcrequest": "true",
}

_TIMEOUT = aiohttp.ClientTimeout(total=30)


def _now_iso() -> str:
    return datetime.datetime.now().isoformat()


def _uuid_upper() -> str:
    return str(uuid.uuid4()).upper()


def _uuid_hex() -> str:
    return uuid.uuid4().hex


def _s(value) -> str:
    if value is None:
        return ""
    return str(value)


# ==================== 账号池 ====================

# 账号字段 (与 Gitee 一致, 仅保留本框架用得到的)
_ACCOUNT_STR_FIELDS = (
    "userId", "token", "userKey", "encodeRes", "openId", "gameOpenId",
    "gameRoleId", "gameServerId", "gameAreaId", "gameUserSex", "kohDimGender",
    "accessToken", "refreshToken", "appOpenid", "avatar", "bigAvatar", "icon",
    "nickname", "snsnickname", "userName", "sex", "expires", "uin", "userSig",
    "loginPlatform", "ownerBotUserId", "remark", "lastLoginAt", "lastSuccessAt",
    "lastAuthErrorAt", "lastAuthErrorMessage",
)


def _is_usable(acc: dict) -> bool:
    return bool(acc.get("token") and acc.get("userId")
               and (acc.get("userKey") or acc.get("encodeRes")))


class AuthStore:
    """登录态账号池 (AuthPool.json)。"""

    __slots__ = ("_path", "_lock", "_pool")

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._pool = self._load()

    # -------- 持久化 --------

    def _load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError
            data.setdefault("accounts", {})
            return data
        except FileNotFoundError:
            return {"accounts": {}}
        except Exception:
            return {"accounts": {}}

    def _save(self):
        import os
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._pool, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    @staticmethod
    def _normalize(account: dict, existing: dict | None = None) -> dict:
        existing = existing or {}
        out = dict(existing)
        out.update(account)
        out["userId"] = _s(account.get("userId") or existing.get("userId"))
        for field in _ACCOUNT_STR_FIELDS:
            if field == "userId":
                continue
            out[field] = _s(account.get(field, existing.get(field, "")))
        out["authInvalid"] = bool(account.get(
            "authInvalid", existing.get("authInvalid", False)))
        out["authErrorCount"] = int(account.get(
            "authErrorCount", existing.get("authErrorCount", 0)) or 0)
        out["isGlobalDefault"] = bool(account.get(
            "isGlobalDefault", existing.get("isGlobalDefault", False)))
        try:
            out["priority"] = int(account.get(
                "priority", existing.get("priority", 100)))
        except (TypeError, ValueError):
            out["priority"] = 100
        out["updatedAt"] = _now_iso()
        return out

    # -------- 查询 --------

    def list_accounts(self) -> list:
        with self._lock:
            return [json.loads(json.dumps(a))
                    for a in self._pool["accounts"].values()]

    @staticmethod
    def _sort_by_priority(accounts: list) -> list:
        return sorted(accounts, key=lambda a: (
            0 if a.get("isGlobalDefault") else 1,
            int(a.get("priority", 100) or 100),
            _s(a.get("userId")),
        ))

    def get_auth_candidates(self, requester_qq: str = "") -> list:
        """返回可用候选账号 (深拷贝), 顺序: 本人的营地账号 → 全局登录态。

        requester_qq 是发起查询的 QQ: 他个人扫码登录过 (王者wx登录 / 王者QQ登录) 就优先用
        他自己那个账号; 没有 (或已失效) 再回落到全局登录态 (王者wx全局登录 / 王者QQ全局登录
        写入的账号)。
        """
        with self._lock:
            accounts = self._pool["accounts"]
            out: list = []
            seen: set = set()

            def push(acc):
                if not acc or not _is_usable(acc) or acc.get("authInvalid"):
                    return
                uid = _s(acc.get("userId"))
                if not uid or uid in seen:
                    return
                seen.add(uid)
                out.append(json.loads(json.dumps(acc)))

            if requester_qq:
                for acc in self._sort_by_priority(
                        [a for a in accounts.values()
                         if _s(a.get("ownerBotUserId")) == _s(requester_qq)]):
                    push(acc)
            for acc in self._sort_by_priority(
                    [a for a in accounts.values() if a.get("isGlobalDefault")]):
                push(acc)
            return out

    # -------- 写入 --------

    def upsert_account(self, account: dict) -> dict:
        uid = _s(account.get("userId"))
        if not uid:
            raise ValueError("缺少营地 userId, 无法写入账号池")
        with self._lock:
            existing = self._pool["accounts"].get(uid)
            if account.get("resetAuthState"):
                account = dict(account)
                account.update(authInvalid=False, authErrorCount=0,
                               lastAuthErrorAt="", lastAuthErrorMessage="")
                account.pop("resetAuthState", None)
            nxt = self._normalize(account, existing)
            nxt.setdefault("lastLoginAt", _now_iso())
            self._pool["accounts"][uid] = nxt
            self._save()
            return json.loads(json.dumps(nxt))

    def upsert_global_account(self, account: dict) -> dict:
        account = dict(account)
        account["isGlobalDefault"] = True
        account["resetAuthState"] = True
        saved = self.upsert_account(account)
        self.set_global_account(saved["userId"])
        return saved

    def set_global_account(self, user_id: str = "") -> str:
        uid = _s(user_id)
        with self._lock:
            accounts = self._pool["accounts"]
            found = not uid
            for aid, acc in accounts.items():
                should = bool(uid and aid == uid)
                if should:
                    found = True
                if bool(acc.get("isGlobalDefault")) == should:
                    continue
                acc["isGlobalDefault"] = should
            if not found:
                raise ValueError(f"账号池中不存在营地账号 {uid}")
            self._save()
        return uid

    def mark_auth_failure(self, user_id: str, message: str = "") -> dict | None:
        uid = _s(user_id)
        if not uid:
            return None
        with self._lock:
            acc = self._pool["accounts"].get(uid)
            if not acc:
                return None
            newly = not bool(acc.get("authInvalid"))
            acc["authInvalid"] = True
            acc["authErrorCount"] = int(acc.get("authErrorCount", 0) or 0) + 1
            acc["lastAuthErrorAt"] = _now_iso()
            acc["lastAuthErrorMessage"] = _s(message)
            self._save()
            result = json.loads(json.dumps(acc))
            result["newlyInvalid"] = newly
            return result

    def mark_auth_success(self, user_id: str) -> dict | None:
        uid = _s(user_id)
        if not uid:
            return None
        with self._lock:
            acc = self._pool["accounts"].get(uid)
            if not acc:
                return None
            acc["authInvalid"] = False
            acc["authErrorCount"] = 0
            acc["lastAuthErrorAt"] = ""
            acc["lastAuthErrorMessage"] = ""
            acc["lastSuccessAt"] = _now_iso()
            self._save()
            return json.loads(json.dumps(acc))

    def clear_invalid_accounts(self) -> dict:
        """移除失效非全局账号, 失效全局账号仅跳过保留。"""
        with self._lock:
            removed, skipped = [], []
            for uid, acc in list(self._pool["accounts"].items()):
                if not acc.get("authInvalid"):
                    continue
                if acc.get("isGlobalDefault"):
                    skipped.append(json.loads(json.dumps(acc)))
                    continue
                removed.append(json.loads(json.dumps(acc)))
                del self._pool["accounts"][uid]
            if removed:
                self._save()
            return {"removedAccounts": removed, "skippedGlobalAccounts": skipped}


# ==================== 微信扫码登录 ====================

class LoginError(Exception):
    """扫码登录失败 (含 code: QR_EXPIRED/QR_CANCELED/QR_TIMEOUT/QR_ERROR)。"""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _build_nonce(length: int = 8) -> str:
    import random
    return "".join(str(random.randint(0, 9)) for _ in range(length))


def _build_special_encode_param() -> str:
    timestamp = int(time.time() * 1000)
    device_id = _uuid_hex()
    payload = {
        "timestamp": timestamp,
        "nonce": f":{_uuid_hex()}:{timestamp}",
        "cDeviceId": device_id,
        "deviceid": device_id,
        "cDeviceImei": device_id[:15],
        "cDeviceMac": "02:00:00:00:00:00",
        "cDevicePPI": 480,
        "cDeviceScreenWidth": 1080,
        "cDeviceScreenHeight": 2400,
        "cDeviceBrand": "OnePlus",
        "cDeviceModel": "PHK110",
        "cDeviceMem": 12 * 1024 * 1024 * 1024,
        "cDeviceCPU": "SM8650",
        "cSystemVersionCode": "34",
        "cDeviceNet": "WIFI",
        "cDeviceSP": "China Mobile",
        "cDeviceOaid": device_id,
        "deviceLevel": 3,
        "px": 0,
        "py": 0,
        "wifi_ssid": "unknown",
        "wifi_mac": "02:00:00:00:00:00",
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(
        crypto.rsa_public_encrypt(raw, _PUB_N, _PUB_E)).decode("ascii")


async def _request_json(session, url, *, method="GET", headers=None, data=None):
    async with session.request(method, url, headers=headers or {}, data=data) as resp:
        text = await resp.text()
        try:
            payload = json.loads(text)
        except Exception:
            payload = {"raw": text}
        return resp.status, payload, text


async def _fetch_sdk_ticket(session, x_log_uid: str) -> str:
    status, payload, text = await _request_json(
        session, f"{CAMP_BASE_URL}/a/getwxsdkticket",
        method="POST", headers={**_COMMON_HEADERS, "x-log-uid": x_log_uid})
    ticket = (payload.get("data") or {}).get("sdkTicket")
    if status != 200 or payload.get("returnCode") != 0 or not ticket:
        raise LoginError(f"获取登录 SDK Ticket 失败: {text[:200]}")
    return ticket


async def _fetch_qr_code(session, ticket: str) -> dict:
    nonce = _build_nonce()
    timestamp = str(int(time.time()))
    signature = _sha1(
        f"appid={APPID_WX}&noncestr={nonce}&sdk_ticket={ticket}&timestamp={timestamp}")
    params = {
        "appid": APPID_WX, "noncestr": nonce, "timestamp": timestamp,
        "scope": "snsapi_userinfo", "signature": signature,
    }
    async with session.get(WX_QR_URL, params=params) as resp:
        text = await resp.text()
        try:
            payload = json.loads(text)
        except Exception:
            payload = {}
    qr_b64 = (payload.get("qrcode") or {}).get("qrcodebase64")
    uuid_val = payload.get("uuid")
    if resp.status != 200 or payload.get("errcode") != 0 or not qr_b64 or not uuid_val:
        raise LoginError(f"获取登录二维码失败: {text[:200]}")
    return {"uuid": uuid_val, "qrcodeBase64": qr_b64}


async def _login_with_code(session, code: str, x_log_uid: str) -> dict:
    special = _build_special_encode_param()
    form = {
        "loginType": "wx", "code": code, "delOldUser": "0",
        "key1": _uuid_hex(), "lastLoginTime": "0", "lastGetRemarkTime": "0",
        "cChannelId": "10003391", "cClientVersionCode": "2057957801",
        "cClientVersionName": "10.111.0323", "cCurrentGameId": "20001",
        "cGameId": "20001", "cGzip": "1", "cIsArm64": "true",
        "cRand": str(int(time.time() * 1000)), "cSupportArm64": "true",
        "cSystem": "android", "cSystemVersionCode": "34",
        "cSystemVersionName": "14", "cpuHardware": "qcom", "gameId": "20001",
        "tinkerId": "2057957801_64_0", "specialEncodeParam": special,
    }
    headers = {
        **_COMMON_HEADERS, "x-log-uid": x_log_uid,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "cChannelId": "10003391", "cClientVersionCode": "2057957801",
        "cClientVersionName": "10.111.0323", "cCurrentGameId": "20001",
        "cGameId": "20001", "cGzip": "1", "cIsArm64": "true",
        "cRand": str(int(time.time() * 1000)), "cSupportArm64": "true",
        "cSystem": "android", "cSystemVersionCode": "34",
        "cSystemVersionName": "14", "cpuHardware": "qcom", "gameId": "20001",
        "tinkerId": "2057957801_64_0", "specialEncodeParam": special,
    }
    status, payload, text = await _request_json(
        session, f"{CAMP_BASE_URL}/user/login",
        method="POST", headers=headers, data=form)
    data = payload.get("data") or {}
    if status != 200 or payload.get("returnCode") != 0 or not data.get("userId") \
            or not data.get("token"):
        raise LoginError(f"营地登录失败: {text[:200]}")
    return payload


def build_account_from_login(payload: dict) -> dict:
    data = payload.get("data") or {}
    encode_res = _s(data.get("encodeRes"))
    user_key = _s(data.get("userKey"))
    if not user_key and encode_res:
        user_key = _s(crypto.decode_encode_res(encode_res, _PUB_N, _PUB_E).get("userKey"))
    return {
        "userId": _s(data.get("userId")),
        "token": _s(data.get("token")),
        "userKey": user_key,
        "encodeRes": encode_res,
        "accessToken": _s(data.get("accessToken")),
        "refreshToken": _s(data.get("refreshToken")),
        "appOpenid": _s(data.get("appOpenid")),
        "avatar": _s(data.get("avatar")),
        "bigAvatar": _s(data.get("bigAvatar")),
        "icon": _s(data.get("icon")),
        "nickname": _s(data.get("nickname")),
        "snsnickname": _s(data.get("snsnickname")),
        "userName": _s(data.get("userName")),
        "sex": _s(data.get("sex")),
        "expires": _s(data.get("expires")),
        "uin": _s(data.get("uin")),
        "userSig": _s(data.get("userSig")),
        "loginPlatform": "wechat",
        "lastLoginAt": _now_iso(),
    }


async def create_login_session() -> dict:
    """发起扫码会话, 返回 {xLogUid, uuid, qrcodeBase64}。"""
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        x_log_uid = _uuid_upper()
        ticket = await _fetch_sdk_ticket(session, x_log_uid)
        qr = await _fetch_qr_code(session, ticket)
        return {
            "xLogUid": x_log_uid,
            "uuid": qr["uuid"],
            "qrcodeBase64": qr["qrcodeBase64"],
            "createdAt": _now_iso(),
        }


async def wait_for_login(session_info: dict, *, timeout_s: int = 180,
                         poll_interval_s: float = 2.0, on_status=None) -> dict:
    """轮询扫码状态, 成功返回 {account, loginResponse}; 失败抛 LoginError。"""
    started = time.time()
    last_summary = ""
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        while time.time() - started < timeout_s:
            try:
                async with session.get(
                        WX_POLL_URL,
                        params={"f": "json", "uuid": session_info["uuid"]}) as resp:
                    text = await resp.text()
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = {}
            except Exception:
                await asyncio.sleep(poll_interval_s)
                continue

            status_code = payload.get("wx_errcode", payload.get("errcode"))
            auth_code = payload.get("wx_code", payload.get("code"))
            summary = json.dumps(payload, sort_keys=True)
            if summary != last_summary:
                last_summary = summary
                if on_status:
                    try:
                        res = on_status(status_code, auth_code, payload)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        pass

            if auth_code and status_code == 405:
                login_response = await _login_with_code(
                    session, auth_code, session_info["xLogUid"])
                return {
                    "loginResponse": login_response,
                    "account": build_account_from_login(login_response),
                }
            if status_code == 402:
                raise LoginError("登录二维码已过期，请重新发起", "QR_EXPIRED")
            if status_code == 403:
                raise LoginError("登录已取消，请重新发起", "QR_CANCELED")
            if status_code == 500:
                raise LoginError("登录服务异常，请稍后再试", "QR_ERROR")

            await asyncio.sleep(poll_interval_s)

    raise LoginError("等待登录二维码超时，请重新发起", "QR_TIMEOUT")
