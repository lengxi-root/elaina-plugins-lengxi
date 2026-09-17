"""王者营地 QQ OAuth 扫码登录 (纯接口, 无浏览器依赖)。"""
import asyncio, base64, hashlib, hmac, json, re, time, uuid
from urllib.parse import quote, urlencode, urlsplit
import aiohttp
from . import crypto
from .auth import _PUB_N, _PUB_E

QQ_APP_ID = "1105200115"
# QQ互联统一登录壳 (m_authorize 页面内嵌的 xlogin 参数)
PT_APP_ID = "716027609"
PT_DAID = "381"
PT_LANG = "2052"
PT_UISTYLE = "35"
S_URL = "http://connect.qq.com"
M_AUTHORIZE_URL = "https://openmobile.qq.com/oauth2.0/m_authorize?" + urlencode({
    "client_id": QQ_APP_ID, "scope": "all", "redirect_uri": "auth://tauth.qq.com/",
    "style": "qr", "response_type": "code"})
PTQRSHOW_URL = "https://ssl.ptlogin2.qq.com/ptqrshow"
PTQRLOGIN_URL = "https://ssl.ptlogin2.qq.com/ptqrlogin"
YSDK_URL = "https://ysdk.qq.com/cmd/QQCodeLogin?"
CAMP_LOGIN_URL = "https://ssl.kohsocialapp.qq.com:10001/user/login"

USER_AGENT = ("Mozilla/5.0 (Linux; Android 15; V2366GA Build/V417IR; wv) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 "
              "Safari/537.36 tencent_game_emulator")

_POLL_INTERVAL_S = 3.0
# ptqrlogin 状态: 0=成功 66=二维码有效 67=已扫码待确认 65/68=已失效
_PT_EXPIRED_CODES = (65, 68)

_PTCB_RE = re.compile(r"ptuiCB\('(-?\d+)'\s*,\s*'\d+'\s*,\s*'([^']*)'\s*,\s*'\d+'\s*,\s*'([^']*)'")
_CODE_RE = re.compile(r"[?&]code=([0-9A-Za-z_-]+)")
_AUTH_URL_RE = re.compile(r"auth://[^\"'<>\\\s]+")
_TAUTH_URL_RE = re.compile(r"tauth\.qq\.com/[^\"'<>\\\s]+")
_META_RE = re.compile(r"http-equiv=['\"]?refresh['\"]?[^>]*?url=([^\"'>\s]+)", re.I)
_JS_REDIRECT_RE = re.compile(
    r"(?:(?:window\.)?location(?:\.href)?\s*=\s*|(?:window\.)?location\.replace\(\s*|"
    r"window\.open\(\s*|<iframe[^>]+?src=['\"])['\"]?([^'\"\s>]+)", re.I)


class LoginError(Exception):
    def __init__(self, message, code=""):
        super().__init__(message); self.code = code


def _hash33(text: str) -> int:
    h = 0
    for ch in text:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


def _extract_code(target: str):
    """从 auth://tauth.qq.com/?...&code= 回调地址中提取授权码。"""
    if target and (target.startswith("auth://") or "tauth.qq.com" in target):
        m = _CODE_RE.search(target)
        if m:
            return m.group(1)
    return ""


def _url_param(url: str, name: str) -> str:
    m = re.search(rf"[?&]{name}=([^&#]+)", url or "")
    return m.group(1) if m else ""


def _decode_js_escapes(text: str) -> str:
    """腾讯页面常把 URL 写成 https\\x3A\\x2F... 转义形式, 嗅探前先还原。"""
    if "\\" not in text:
        return text
    try:
        return (text.encode("utf-8", "ignore").decode("unicode_escape")
                .encode("latin-1", "ignore").decode("utf-8", "ignore"))
    except Exception:
        return text


def _find_auth_code(text: str) -> str:
    for pattern in (_AUTH_URL_RE, _TAUTH_URL_RE):
        for m in pattern.finditer(text or ""):
            c = _CODE_RE.search(m.group(0))
            if c:
                return c.group(1)
    return ""


def _special_encode_param():
    ts = int(time.time() * 1000)
    raw = json.dumps({"timestamp": ts, "nonce": f":{uuid.uuid4().hex}:{ts}"}, separators=(",", ":")).encode()
    return base64.b64encode(crypto.rsa_public_encrypt(raw, _PUB_N, _PUB_E)).decode()


async def _exchange_code(code):
    ts = str(int(time.time()))
    body = json.dumps({"appID": QQ_APP_ID, "loginCode": code}, separators=(",", ":"))
    sign = f"POST\n/cmd/QQCodeLogin\njson\nysdk\n{ts}\n{body}"
    digest = base64.b64encode(hmac.new(b"yyb@cloud_game:CQ8FA#", sign.encode(), hashlib.sha256).digest()).decode()
    headers = {"Content-Type": "json", "Auth-Secret-ID": "ysdk", "Auth-Secret-Digest": digest, "Auth-Request-Time": ts}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(YSDK_URL, headers=headers, data=body) as resp:
            result = await resp.json(content_type=None)
    data = result.get("data") or {}
    if resp.status != 200 or result.get("code") != 0 or data.get("ret") != 0:
        raise LoginError(result.get("errmsg") or data.get("errmsg") or f"YSDK 请求失败 ({resp.status})", "YSDK_ERROR")
    return {"accessToken": str(data.get("accessToken") or ""), "openId": str(data.get("openID") or ""), "payToken": str(data.get("payToken") or ""), "refreshToken": str(data.get("refreshToken") or ""), "expiresIn": int(data.get("expiresIn") or 0)}


async def _camp_login(tokens):
    form = {"delOldUser": "0", "key1": uuid.uuid4().hex, "lastLoginTime": "0", "lastGetRemarkTime": "0", "cChannelId": "10003391", "cClientVersionCode": "2057971306", "cClientVersionName": "10.114.0826", "cCurrentGameId": "20001", "cGameId": "20001", "cGzip": "1", "cIsArm64": "true", "cRand": str(int(time.time()*1000)), "cSupportArm64": "true", "cSystem": "android", "cSystemVersionCode": "35", "cSystemVersionName": "15", "cpuHardware": "qcom", "gameId": "20001", "tinkerId": "2057971306_64_0", "specialEncodeParam": _special_encode_param(), "loginType": "openSdk", "accessToken": tokens["accessToken"], "openId": tokens["openId"], "payToken": tokens["payToken"]}
    headers = {"Content-Encrypt": "", "Accept-Encrypt": "", "NOENCRYPT": "1", "X-Client-Proto": "https", "User-Agent": "okhttp/4.9.1", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "x-log-uid": str(uuid.uuid4()).upper()}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(CAMP_LOGIN_URL, headers=headers, data=form) as resp:
            result = await resp.json(content_type=None)
    data = result.get("data") or {}
    if resp.status != 200 or result.get("returnCode") != 0 or not data.get("userId") or not data.get("token"):
        raise LoginError(result.get("returnMsg") or f"营地登录失败 ({resp.status})", "CAMP_ERROR")
    decoded = crypto.decode_encode_res(str(data.get("encodeRes") or ""), _PUB_N, _PUB_E)
    keys = ("userId","token","encodeRes","appOpenid","avatar","bigAvatar","icon","nickname","snsnickname","userName","sex","expires","uin","userSig","realRegisterTime")
    account = {k: str(data.get(k) or "") for k in keys}
    account.update(userKey=str(data.get("userKey") or decoded.get("userKey") or ""), accessToken=tokens["accessToken"], refreshToken=tokens["refreshToken"], loginPlatform="qq", lastLoginAt=time.strftime("%Y-%m-%dT%H:%M:%S"))
    return account


class QQLoginSession:
    """无浏览器 QQ OAuth 扫码会话: 同一个 Cookie 会话里完成出码/轮询/授权。"""

    def __init__(self):
        self.qrcode = b''
        self.closed = False
        self._scanned = False
        self._http = None
        self._xlogin_url = ""
        self._pt_openlogin_data = ""

    async def _init(self):
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                                           headers={"User-Agent": USER_AGENT})
        try:
            # 1. 授权页壳 → xlogin 地址 (带本次会话的 h5sig)
            async with self._http.get(M_AUTHORIZE_URL) as resp:
                html = await resp.text(errors="ignore")
            m = re.search(r'var src = "([^"]+)"', html)
            if not m:
                raise LoginError("获取 QQ 授权页失败, 请稍后重试", "OAUTH_PAGE_ERROR")
            self._xlogin_url = m.group(1).encode().decode("unicode_escape")
            # 官方 JS: pt_openlogin_data = xlogin 的 query + "&pt_flex=1"
            self._pt_openlogin_data = quote(urlsplit(self._xlogin_url).query + "&pt_flex=1", safe="")
            # 2. 登录页 (落地 pt_login_sig 等 cookie)
            async with self._http.get(self._xlogin_url, headers={"Referer": M_AUTHORIZE_URL}) as resp:
                await resp.read()
            # 3. 出码
            await self._fetch_qrcode()
        except Exception:
            await self.close()
            raise

    async def _fetch_qrcode(self):
        params = (f"s=8&e=0&appid={PT_APP_ID}&type=0&t={time.time():.3f}"
                  f"&u1={quote(S_URL, safe='')}&daid={PT_DAID}&pt_3rd_aid={QQ_APP_ID}")
        async with self._http.get(PTQRSHOW_URL + "?" + params,
                                  headers={"Referer": self._xlogin_url}) as resp:
            data = await resp.read()
        if resp.status != 200 or data[:4] != b"\x89PNG":
            raise LoginError("获取 QQ 登录二维码失败, 请稍后重试", "QR_ERROR")
        self.qrcode = data

    def _cookie(self, name: str) -> str:
        for cookie in self._http.cookie_jar:
            if cookie.key == name:
                return cookie.value or ""
        return ""

    def _poll_url(self) -> str:
        qrsig = self._cookie("qrsig")
        if not qrsig:
            raise LoginError("QQ 登录会话异常, 请重新发起", "QR_SESSION_ERROR")
        return PTQRLOGIN_URL + "?" + (
            f"u1={quote(S_URL, safe='')}&from_ui=1&type=1&ptlang={PT_LANG}"
            f"&ptqrtoken={_hash33(qrsig)}&daid={PT_DAID}&aid={PT_APP_ID}"
            f"&pt_3rd_aid={QQ_APP_ID}&pt_openlogin_data={self._pt_openlogin_data}"
            f"&device=2&ptopt=1&pt_uistyle={PT_UISTYLE}&jsver=20142&r={time.time():.6f}")

    async def wait_for_code(self, timeout_s=180, on_status=None) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.closed:
                raise LoginError("登录会话已结束", "QR_CANCELED")
            try:
                async with self._http.get(self._poll_url(),
                                          headers={"Referer": self._xlogin_url}) as resp:
                    text = await resp.text(errors="ignore")
                status, url, msg = self._parse_ptcb(text)
            except LoginError:
                raise
            except Exception:
                await asyncio.sleep(_POLL_INTERVAL_S)
                continue

            if status == 0 and url:
                return await self._hunt_code(url)
            if status in _PT_EXPIRED_CODES:
                raise LoginError("登录二维码已失效，请重新发起", "QR_EXPIRED")
            if status == 67 and not self._scanned:
                self._scanned = True
                if on_status:
                    try:
                        res = on_status("scanned")
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        pass
            if status not in (66, 67, 0):
                raise LoginError(f"QQ 扫码登录失败: {msg or status}", "PTLOGIN_ERROR")
            await asyncio.sleep(_POLL_INTERVAL_S)
        raise LoginError("登录二维码等待超时，请重新发起", "QR_TIMEOUT")

    @staticmethod
    def _parse_ptcb(text: str):
        m = _PTCB_RE.search(text)
        if not m:
            return None, "", (text or "")[:120]
        return int(m.group(1)), m.group(2), m.group(3)

    async def _hunt_code(self, check_sig_url: str) -> str:
        """登录成功后沿跳转链捕获 auth:// 回调里的 code, 失败则多重兜底。"""
        trace = []
        h5sig = _url_param(self._xlogin_url, "h5sig")
        candidates = [check_sig_url]
        # 授权码可能绑定在出码那次页面加载的 h5sig 上, 优先原样重放
        if h5sig:
            candidates.append(M_AUTHORIZE_URL + "&h5sig=" + quote(h5sig, safe=""))
        candidates.append(M_AUTHORIZE_URL)
        for start in candidates:
            code = _extract_code(start)
            if code:
                return code
            try:
                code, hops = await self._walk_redirects(start)
            except Exception as exc:
                trace.append(f"exc:{type(exc).__name__}")
                continue
            if code:
                return code
            trace.append(hops)
        # 兜底: 登录态已建立时重放一次轮询, 部分流程会直接给出授权跳转
        try:
            async with self._http.get(self._poll_url(),
                                      headers={"Referer": self._xlogin_url}) as resp:
                text = await resp.text(errors="ignore")
            status, url, _msg = self._parse_ptcb(text)
            if status == 0 and url:
                code, hops = await self._walk_redirects(url)
                if code:
                    return code
                trace.append("repoll:" + hops)
        except Exception as exc:
            trace.append(f"repoll-exc:{type(exc).__name__}")
        raise LoginError("QQ 授权未返回 code [" + "; ".join(t[:64] for t in trace[-4:]) + "]",
                         "OAUTH_CODE_ERROR")

    async def _walk_redirects(self, url: str):
        """手动跟随 30x/JS/meta 跳转, 返回 (code, 链路追踪)。"""
        hops = []
        for _ in range(12):
            if not url or not url.startswith(("http://", "https://")):
                return "", " ".join(hops + ["stop"])
            try:
                async with self._http.get(url, allow_redirects=False,
                                          headers={"Referer": self._xlogin_url}) as resp:
                    status = resp.status
                    location = resp.headers.get("Location", "")
                    body = "" if location else await resp.text(errors="ignore")
            except Exception as exc:
                return "", " ".join(hops + [f"!{type(exc).__name__}"])
            hop_host = urlsplit(location or url).netloc or "?"
            hops.append(f"{status}>{hop_host[:36]}")
            code = _extract_code(location)
            if code:
                return code, " ".join(hops)
            plain = _decode_js_escapes(body or "")
            code = _find_auth_code(plain)
            if code:
                return code, " ".join(hops)
            nxt = location or _js_redirect(plain) or _meta_refresh(plain)
            nxt = (nxt or "").replace("&amp;", "&")
            if not nxt or nxt == url:
                if "code=" in plain or "tauth" in plain:
                    hops.append("sniff:" + plain[max(0, plain.find("code=") - 40):][:90])
                return "", " ".join(hops)
            url = nxt
        return "", " ".join(hops[:6]) + " max"

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                pass


def _js_redirect(body: str) -> str:
    m = _JS_REDIRECT_RE.search(body or "")
    return m.group(1) if m else ""


def _meta_refresh(body: str) -> str:
    m = _META_RE.search(body or "")
    return m.group(1) if m else ""


async def create_login_session() -> QQLoginSession:
    """创建纯接口扫码会话, 返回带二维码 (PNG bytes) 的会话对象。"""
    session = QQLoginSession()
    try:
        await session._init()
    except LoginError:
        raise
    except Exception as exc:
        raise LoginError(f"获取 QQ 登录二维码失败: {exc}", "QR_ERROR") from exc
    return session


async def wait_for_login(session: QQLoginSession, timeout_s=180, on_status=None):
    try:
        code = await session.wait_for_code(timeout_s, on_status=on_status)
        tokens = await _exchange_code(code)
        return {'tokens': tokens, 'account': await _camp_login(tokens)}
    finally:
        await session.close()
