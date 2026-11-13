"""个人扫码会话、会员分池、账号轮换与确定失效后的清理。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from pathlib import Path

from .client import (
    QQMusicAPIError,
    QQMusicError,
    QQMusicService,
    _as_int,
    is_rate_limited,
)

AUTH_EXPIRED = {1000, 104400, 104401}
POOLS = ('vip', 'normal', 'pending')


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode('utf-8')).hexdigest()


def _expired(exc):
    return isinstance(exc, QQMusicAPIError) and exc.code in AUTH_EXPIRED


class AccountManager:
    def __init__(self, context):
        self.ctx = context
        self.guest = QQMusicService(context)
        self.config = {}
        self.pools = {name: {} for name in POOLS}
        self.owners = {}
        self.clients = {}
        self.sessions = {}
        self.locks = {}
        self.cursors = {}
        self.search_cursors = {}
        self.check_errors = set()
        self.task = None
        self._started = False

    @staticmethod
    def identity(event):
        bot = str(getattr(event, 'appid', '') or '')
        user = str(getattr(event, 'user_id', '') or '')
        if not user:
            raise QQMusicError('消息缺少用户标识，无法创建个人登录会话')
        # 框架 user_id 是统一用户标识，raw_user_id 仅用于平台消息路由。
        return bot, _key(bot, user)

    def _path(self):
        return Path(self.ctx.get_data_path('accounts.json'))

    def _save(self):
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.json.tmp')
        temp.write_text(json.dumps({
            'version': 2, 'pools': self.pools, 'owners': self.owners,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)

    def _find(self, account_id):
        for pool, entries in self.pools.items():
            if account_id in entries:
                return pool, entries[account_id]
        return '', None

    def _client(self, account_id, record):
        client = self.clients.get(account_id)
        if client is None:
            def persist(credential):
                _, current = self._find(account_id)
                if current is None or self.clients.get(account_id) is not client:
                    raise QQMusicError('账号已移除，已丢弃旧刷新结果')
                current['credential'] = credential
                current['checked_at'] = 0
                self._save()

            client = QQMusicService(self.ctx, config=self.config,
                                    credential=record['credential'], on_save=persist)
            self.clients[account_id] = client
        return client

    async def start(self):
        if self._started:
            return
        await self.guest.start()
        self.config = self.guest.config
        path = self._path()
        if path.exists():
            # 文件损坏时停止加载，避免静默覆盖所有用户的凭据。
            state = json.loads(path.read_text(encoding='utf-8'))
            if state.get('version') != 2 or not isinstance(state.get('pools'), dict):
                raise QQMusicError('账号存储格式无效，请检查 accounts.json')
            seen = set()
            for pool in POOLS:
                entries = state['pools'].get(pool, {})
                if not isinstance(entries, dict):
                    raise QQMusicError('账号池格式无效，请检查 accounts.json')
                for account_id, record in entries.items():
                    if (account_id in seen or not isinstance(record, dict)
                            or not isinstance(record.get('credential'), dict)
                            or not record['credential'].get('cookie')):
                        raise QQMusicError('账号记录格式无效，请检查 accounts.json')
                    seen.add(account_id)
                    # 重启后重新核验，既有分池信息保留。
                    record['checked_at'] = 0
                self.pools[pool] = entries
            owners = state.get('owners', {})
            if not isinstance(owners, dict):
                raise QQMusicError('账号归属格式无效，请检查 accounts.json')
            self.owners = {owner: account for owner, account in owners.items() if account in seen}
        legacy = Path(self.ctx.get_data_path('credential.json'))
        if legacy.exists():
            credential = json.loads(legacy.read_text(encoding='utf-8'))
            if not isinstance(credential, dict) or not credential.get('cookie'):
                raise QQMusicError('旧版凭据格式无效，请检查 credential.json')
            account_id = _key('', str(credential.get('musicid') or credential.get('uin')))
            if not self._find(account_id)[1]:
                self.pools['pending'][account_id] = {
                    'bot': '', 'credential': credential, 'checked_at': 0, 'legacy': True,
                }
            self._save()
            legacy.unlink()
        self._started = True
        self.task = asyncio.create_task(self._check_loop(), name='qqmusic-account-check')

    async def stop(self):
        task, self.task = self.task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        clients = [self.guest, *self.clients.values(), *self.sessions.values()]
        await asyncio.gather(*(client.stop() for client in clients))
        self.sessions.clear()
        self.clients.clear()
        self.search_cursors.clear()
        self._started = False

    def _bind_legacy(self, bot):
        """旧版共享账号无法还原归属，只绑定首次使用它的机器人。"""
        for pool in POOLS:
            for old_id, record in list(self.pools[pool].items()):
                if not record.get('legacy') or record.get('bot'):
                    continue
                musicid = str(record['credential'].get('musicid') or record['credential'].get('uin'))
                account_id = _key(bot, musicid)
                del self.pools[pool][old_id]
                if self._find(account_id)[1] is None:
                    record['bot'] = bot
                    self.pools[pool][account_id] = record
                self._save()

    def _accept_login(self, bot, owner, credential):
        musicid = str(credential.get('musicid') or credential.get('uin'))
        account_id = _key(bot, musicid)
        pool, record = self._find(account_id)
        if record:
            del self.pools[pool][account_id]
        self.pools['pending'][account_id] = {
            'bot': bot, 'credential': credential, 'checked_at': 0,
        }
        if account_id in self.clients:
            self.clients[account_id].credential = credential
            self.clients[account_id].refresh_error = ''
        self.owners[owner] = account_id
        self.check_errors.discard(account_id)
        self._save()
        return account_id

    async def begin_login(self, event, *, on_complete=None):
        bot, owner = self.identity(event)
        client = self.sessions.get(owner)
        if client is None:
            client = QQMusicService(self.ctx, config=self.config,
                                   on_save=lambda credential: self._accept_login(bot, owner, credential))
            self.sessions[owner] = client

        async def completed(qr):
            if qr.status == 'logged_in':
                account_id = self.owners.get(owner)
                if account_id:
                    try:
                        await self.check_account(account_id, force=True)
                    except Exception as exc:  # noqa: BLE001 - 分类失败不阻止登录结果通知
                        self.check_errors.add(account_id)
                        self.ctx.log.warning('QQ音乐登录后分类暂时失败（%s）', type(exc).__name__)
                    if owner not in self.owners:
                        qr.status = 'failed'
                # 扫码会话不继续保留第二份账号凭据。
                client.credential = {}
                client.login_cookies.clear()
            if on_complete:
                await on_complete(qr)

        return await client.begin_login(on_complete=completed)

    async def cancel_login(self, event, qr):
        _, owner = self.identity(event)
        client = self.sessions.get(owner)
        # 二维码发送失败的旧请求不得取消刚刚创建的新会话。
        if client and client.qr is qr:
            await client.cancel_login()

    async def _remove(self, account_id, credential):
        pool, record = self._find(account_id)
        if record is None or record['credential'] is not credential:
            return
        # 删除前修正轮询位置，避免移除首个失效账号后跳过下一个账号。
        bot = record.get('bot', '')
        ids = self._search_ids(bot)
        if account_id in ids:
            cursor = self.search_cursors.get(bot, 0) % len(ids)
            self.search_cursors[bot] = cursor - int(ids.index(account_id) < cursor)
        del self.pools[pool][account_id]
        removed_owners = [owner for owner, value in self.owners.items() if value == account_id]
        self.owners = {owner: value for owner, value in self.owners.items() if value != account_id}
        for owner in removed_owners:
            session = self.sessions.get(owner)
            # 保留正在进行的新扫码，清理已结束扫码会话中的旧凭据与状态。
            if session and (not session.qr or session.qr.status not in ('waiting', 'scanned', 'confirming')):
                session.credential = {}
                session.login_cookies.clear()
                session.refresh_error = ''
                if session.qr and session.qr.status == 'logged_in':
                    session.qr.status = 'invalid'
                    session.qr.message = '账号已失效，请重新扫码登录'
        self.check_errors.discard(account_id)
        self._save()
        client = self.clients.pop(account_id, None)
        if client:
            client.credential = {}
            client.login_cookies.clear()
            client.refresh_error = ''
            await client.stop()
        self.ctx.log.info('QQ音乐已移除确认失效的账号记录')

    def _interval(self):
        return max(30, min(_as_int(self.config.get('account_check_interval'), 300), 3600))

    async def check_account(self, account_id, *, force=False):
        async with self.locks.setdefault(account_id, asyncio.Lock()):
            pool, record = self._find(account_id)
            if record is None:
                return False
            if not force and time.time() - record.get('checked_at', 0) < self._interval():
                return pool != 'pending'
            client = self._client(account_id, record)
            if self.config.get('auto_refresh', True) and client.refresh_due():
                await client.try_refresh()
            credential = client.credential
            try:
                await client.check_login()
                membership = await client.membership()
            except QQMusicError as exc:
                if client.credential is not credential:
                    return False
                self.check_errors.add(account_id)
                if _expired(exc):
                    # 明确失效立即删除，不因存在刷新凭据而继续保留失效状态。
                    await self._remove(account_id, credential)
                return False
            if client.credential is not credential:
                return False
            current_pool, current = self._find(account_id)
            if current is None or current['credential'] is not credential:
                return False
            target = 'vip' if membership['vip'] else 'normal'
            if current_pool != target:
                del self.pools[current_pool][account_id]
                self.pools[target][account_id] = current
            current['membership'] = membership
            current['checked_at'] = int(time.time())
            self.check_errors.discard(account_id)
            self._save()
            return True

    def account_counts(self, event):
        bot, _ = self.identity(event)
        self._bind_legacy(bot)
        counts = {
            tier: sum(1 for record in entries.values()
                      if record.get('bot') == bot and record.get('credential', {}).get('cookie'))
            for tier, entries in self.pools.items()
        }
        counts['total'] = sum(counts.values())
        return counts

    async def status(self, event):
        _, owner = self.identity(event)
        account_id = self.owners.get(owner)
        if account_id:
            await self.check_account(account_id, force=True)
        account_id = self.owners.get(owner)
        pool, record = self._find(account_id)
        client = self._client(account_id, record) if record else self.guest
        status = client.status()
        session = self.sessions.get(owner)
        if session:
            qr_status = session.status()
            for name in ('login_status', 'login_message', 'login_started'):
                status[name] = qr_status[name]
        status['pool'] = pool
        status['check_error'] = account_id in self.check_errors
        return status

    def _rotation(self, bot, tier):
        ids = [account for account, record in self.pools[tier].items() if record.get('bot') == bot]
        if not ids:
            return []
        key = (bot, tier)
        start = self.cursors.get(key, 0) % len(ids)
        self.cursors[key] = (start + 1) % len(ids)
        return ids[start:] + ids[:start]

    def _search_ids(self, bot):
        return [account for tier in ('normal', 'vip', 'pending')
                for account, record in self.pools[tier].items() if record.get('bot') == bot]

    def _next_search_account(self, bot, used):
        # 搜索不依赖会员级别，所有已保存登录态统一轮换。
        ids = self._search_ids(bot)
        if not ids:
            return None
        start = self.search_cursors.get(bot, 0) % len(ids)
        for offset in range(len(ids)):
            index = (start + offset) % len(ids)
            account = ids[index]
            if account not in used:
                # 等待网络前推进，避免并发请求都选中同一账号。
                self.search_cursors[bot] = (index + 1) % len(ids)
                return account
        return None

    async def search(self, event, keyword, limit=8):
        bot, _ = self.identity(event)
        self._bind_legacy(bot)
        used = set()
        last_error = None
        for switch in range(3):
            account = self._next_search_account(bot, used)
            if account is None:
                if not used:
                    return await self.guest.search(keyword, limit)
                break
            used.add(account)
            if switch:
                self.ctx.log.info('QQ音乐搜索切换账号（%s/2）', switch)
            async with self.locks.setdefault(account, asyncio.Lock()):
                _, record = self._find(account)
                if record is None:
                    continue
                client = self._client(account, record)
                await client.ensure_fresh()
                credential = client.credential
                try:
                    return await client.search(keyword, limit)
                except QQMusicError as exc:
                    last_error = exc
            if _expired(last_error):
                await self._remove(account, credential)
            elif not is_rate_limited(last_error):
                raise last_error
            # 超频只换账号，不删除凭据；本次不会重复使用已尝试的账号。
        if last_error:
            raise last_error
        raise QQMusicError('当前暂无可用的 QQ 音乐登录账号')

    async def audio_url(self, event, track, preferred=8):
        bot, _ = self.identity(event)
        last_error = None
        self._bind_legacy(bot)
        # 新登录/迁移的账号要先核验，不能将未知会员默认归为普通账号。
        for account_id, record in list(self.pools['pending'].items()):
            if record.get('bot') == bot:
                await self.check_account(account_id)
        raw = track.get('raw')
        raw = raw if isinstance(raw, dict) else {}
        pay = raw.get('pay')
        paid = isinstance(pay, dict) and _as_int(pay.get('pay_play')) > 0
        # 按歌曲的播放权限选账号，不根据所选音质把普通歌曲提前分给绿钻。
        tiers = ('vip',) if paid else ('normal', 'vip')
        # 实际进入某个池时才推进该池的序号，避免备用池的账号被跳过。
        for tier in tiers:
            for account_id in self._rotation(bot, tier):
                if not await self.check_account(account_id):
                    continue
                async with self.locks[account_id]:
                    current_tier, record = self._find(account_id)
                    if record is None or current_tier != tier:
                        continue
                    client = self._client(account_id, record)
                    try:
                        result = await client.audio_url(track, preferred, _retry_login=False)
                        if result and result[0]:
                            return result
                        raise QQMusicError('没有拿到可播放地址')
                    except QQMusicError as exc:
                        last_error = exc
                # vkey 空地址或版权拒绝不能证明失效，另向登录接口确认。
                if _expired(last_error) and await self.check_account(account_id, force=True):
                    async with self.locks[account_id]:
                        current_tier, record = self._find(account_id)
                        if record and current_tier == tier:
                            try:
                                result = await self._client(account_id, record).audio_url(track, preferred, _retry_login=False)
                                if result and result[0]:
                                    return result
                                last_error = QQMusicError('没有拿到可播放地址')
                            except QQMusicError as exc:
                                last_error = exc
                if tier == 'normal':
                    # 普通账号本次获取失败，直接进入绿钻池兜底。
                    break
        if paid:
            if last_error:
                raise last_error
            raise QQMusicError('没有拿到可播放地址：当前没有可用的绿钻账号')
        try:
            return await self.guest.audio_url(track, preferred, _retry_login=False)
        except QQMusicError as exc:
            if last_error:
                raise last_error from exc
            raise

    async def _check_loop(self):
        while True:
            for pool in POOLS:
                for account_id in list(self.pools[pool]):
                    _, record = self._find(account_id)
                    if record and record.get('legacy') and not record.get('bot'):
                        continue
                    try:
                        await self.check_account(account_id)
                    except Exception as exc:  # noqa: BLE001 - 边界仅记录异常类型，防止泄露请求票据
                        # 日志不记录含 Cookie/播放签名的异常原文。
                        self.ctx.log.warning('QQ音乐账号核验暂时失败（%s）', type(exc).__name__)
            for owner, session in list(self.sessions.items()):
                if not session.login_lock.locked() and (not session.login_task or session.login_task.done()) and (
                        not session.qr or time.time() - session.qr.created_at > 3600):
                    del self.sessions[owner]
                    await session.stop()
            active = {account for pool in self.pools.values() for account in pool}
            self.locks = {account: lock for account, lock in self.locks.items()
                          if account in active or lock.locked()}
            await asyncio.sleep(self._interval())
