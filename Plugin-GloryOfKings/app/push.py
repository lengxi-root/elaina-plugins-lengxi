"""战绩订阅推送 — 群内订阅营地ID, 有新对局自动推送 (渲染战绩列表图)"""

import asyncio
import time

from ..lib.handlers import handler
from ..lib import render
from ..lib import requester, data as D
from ..lib.api import AuthFailure

_POLL_INTERVAL = 600   # 轮询周期 (秒)
_INITIAL_DELAY = 15    # 启动后首次检查延迟 (秒), 给连接/加载留出稳定期
_API_INTERVAL = 2      # 两次请求最小间隔 (秒)
_MAX_NEW = 10          # 单次最多渲染的新对局数
# 错峰窗口: 把所有营地ID的检查均匀分散在轮询周期内 (留 10% 余量),
_SPREAD_WINDOW = _POLL_INTERVAL * 0.9

# 谁在游戏订阅: 每 5 分钟查一遍本群账号的在线状态, 只在"开始游戏"那一下播报
_PLAY_ALL = "*"            # play_subs.camp_id 用 '*' 表示盯本群全部订阅账号
_PLAY_INTERVAL = 300       # 检查周期 (秒)
_PLAY_MAX_CAMPS = 25       # 单群单轮最多查几个账号
_PLAY_API_INTERVAL = 1.2   # 两次资料请求之间的间隔 (秒)

_PLAY_USAGE = ("用法:\n"
               "订阅谁在游戏 —— 盯本群全部账号\n"
               "订阅谁在游戏 123456789 —— 只盯指定营地ID (4 位以内当绑定序号)\n"
               "订阅谁在游戏 关 [营地ID] —— 取消\n"
               "订阅谁在游戏 状态 —— 看本群订阅")

_GROUP_ONLY = "该功能仅限群聊使用，请在群内发送哦~"
_NO_UNSUB_PERM = "该订阅由其他成员创建，你暂无本群管理权限，无权操作该命令。"
_REFRESH_BUTTONS = [[{
    "text": "刷新群信息",
    "type": 2,
    "data": "刷新群信息",
    "enter": True,
    "admin": True,
    "style": 1,
}]]


async def _has_proactive_message(event):
    try:
        from .. import get_runtime
        runtime = get_runtime()
        return bool(runtime and runtime.is_full_access(str(event.group_id)))
    except Exception:
        return False


async def _reply_proactive_required(event):
    return await event.reply(
        f"<@{event.user_id}> 订阅推送需要本群开启主动消息。\n"
        "请群主先在机器人群设置中开启‘主动在群聊内发言’，"
        "开启后由群主或群管理员点击下方‘刷新群信息’重新检测。",
        buttons=_REFRESH_BUTTONS,
    )


def _can_unsub(event, subscriber: str) -> bool:
    """订阅人本人、群管理员或群主可取消订阅"""
    if not subscriber or str(event.user_id) == str(subscriber):
        return True
    return (getattr(event, "member_role", "") or "") in ("admin", "owner")


def _get_runtime():
    from .. import get_runtime
    return get_runtime()


def _battle_id(item: dict) -> str:
    return str(item.get("gameSeq") or item.get("gametime") or "")


# ==================== 订阅指令 ====================

@handler(r'^王者推送\s*(开|关|状态|on|off)?\s*(.*)$', name='王者推送',
         desc='群内订阅王者战绩推送 (开/关/状态)')
async def cmd_push(event, match):
    rt = _get_runtime()
    if not rt:
        return
    if not getattr(event, "is_group", False) or not getattr(event, "group_id", None):
        return await event.reply(f"<@{event.user_id}> {_GROUP_ONLY}")

    action = (match.group(1) or "状态").lower()
    arg = (match.group(2) or "").strip()
    gid = str(event.group_id)
    qq = str(event.user_id)

    if action in ("状态", ""):
        subs = rt.db.get_group_subs(gid)
        if not subs:
            return await event.reply(f"<@{event.user_id}> 本群暂无战绩推送订阅\n"
                                     "发送 <qqbot-cmd-input text='王者推送 开' /> 订阅当前账号")
        lines = [f"{i}. {s['camp_id']}" + (f" {s['role_name']}" if s.get("role_name") else "")
                 for i, s in enumerate(subs, 1)]
        return await event.reply(f"<@{event.user_id}> 本群战绩推送订阅 ({len(subs)}):\n"
                                 + "\n".join(lines))

    camp_id = arg if arg.isdigit() else rt.db.get_current(qq)
    if not camp_id:
        return await event.reply(f"<@{event.user_id}> 请先绑定营地ID, 或指定要订阅的营地ID")

    if action in ("开", "on"):
        if not await _has_proactive_message(event):
            return await _reply_proactive_required(event)
        binds = {b["camp_id"]: b.get("role_name", "") for b in rt.db.list_bindings(qq)}
        appid = getattr(event, "appid", "") or ""
        added = rt.db.add_sub(gid, camp_id, binds.get(camp_id, ""), subscriber=qq, appid=str(appid))
        if not added:
            return await event.reply(f"<@{event.user_id}> 该营地ID已在本群订阅")
        return await event.reply(
            f"<@{event.user_id}> 已订阅营地ID {camp_id} 的战绩推送\n有新对局会自动推送到本群")

    if action in ("关", "off"):
        sub = next((s for s in rt.db.get_group_subs(gid)
                    if str(s.get("camp_id")) == str(camp_id)), None)
        if not sub:
            return await event.reply(f"<@{event.user_id}> 本群未订阅该营地ID")
        if not _can_unsub(event, str(sub.get("subscriber") or "")):
            return await event.reply(f"<@{event.user_id}> {_NO_UNSUB_PERM}")
        rt.db.remove_sub(gid, camp_id)
        return await event.reply(f"<@{event.user_id}> 已取消营地ID {camp_id} 的订阅")


def _play_camp_arg(rt, qq: str, arg: str) -> tuple[str, str]:
    """解析谁在游戏的营地ID参数: 5 位以上当营地ID, 4 位以内当绑定序号。返回 (camp_id, err)"""
    if len(arg) >= 5:
        return arg, ""
    binds = rt.db.list_bindings(qq)
    idx = int(arg)
    if not 1 <= idx <= len(binds):
        return "", f"你没有第 {idx} 个绑定的营地ID, 发送 王者我的ID 查看列表"
    return str(binds[idx - 1]["camp_id"]), ""


@handler(r'^订阅谁在游戏\s*(开|关|状态|on|off)?\s*(\S*)$', name='订阅谁在游戏',
         desc='订阅谁在游戏: 每5分钟检查本群账号, 有人开始游戏就播报')
async def cmd_play_sub(event, match):
    rt = _get_runtime()
    if not rt:
        return
    if not getattr(event, "is_group", False) or not getattr(event, "group_id", None):
        return await event.reply(f"<@{event.user_id}> {_GROUP_ONLY}")

    action = (match.group(1) or "开").lower()
    arg = (match.group(2) or "").strip()
    gid, qq = str(event.group_id), str(event.user_id)
    minutes = _PLAY_INTERVAL // 60

    if action == "状态":
        rows = rt.db.get_play_subs(gid)
        if not rows:
            return await event.reply(f"<@{event.user_id}> 本群还没有订阅谁在游戏\n{_PLAY_USAGE}")
        lines = []
        for i, s in enumerate(rows, 1):
            if str(s.get("camp_id")) == _PLAY_ALL:
                lines.append(f"{i}. 本群全部账号")
            else:
                name = s.get("role_name") or ""
                lines.append(f"{i}. {s['camp_id']}" + (f" {name}" if name else ""))
        return await event.reply(
            f"<@{event.user_id}> 本群谁在游戏订阅 ({len(rows)} 条, 每 {minutes} 分钟检查一次):\n"
            + "\n".join(lines))

    if action in ("关", "off"):
        if arg and arg.isdigit():
            camp_id, err = _play_camp_arg(rt, qq, arg)
            if err:
                return await event.reply(f"<@{event.user_id}> {err}")
            if not rt.db.remove_play_sub(gid, camp_id):
                return await event.reply(f"<@{event.user_id}> 本群没有订阅营地ID {camp_id}")
            return await event.reply(f"<@{event.user_id}> 已取消营地ID {camp_id} 的谁在游戏订阅")
        removed = rt.db.remove_play_subs(gid)
        if not removed:
            return await event.reply(f"<@{event.user_id}> 本群还没有订阅谁在游戏")
        return await event.reply(f"<@{event.user_id}> 已取消本群 {removed} 条谁在游戏订阅")

    # 开
    if not await _has_proactive_message(event):
        return await _reply_proactive_required(event)
    if arg:
        if not arg.isdigit():
            return await event.reply(f"<@{event.user_id}> 营地ID要写数字\n{_PLAY_USAGE}")
        camp_id, err = _play_camp_arg(rt, qq, arg)
        if err:
            return await event.reply(f"<@{event.user_id}> {err}")
        role_name = next((b.get("role_name") or "" for b in rt.db.list_bindings(qq)
                          if str(b["camp_id"]) == str(camp_id)), "")
    else:
        camp_id, role_name = _PLAY_ALL, ""
    appid = str(getattr(event, "appid", "") or "")
    if not rt.db.add_play_sub(gid, camp_id, role_name, subscriber=qq, appid=appid):
        return await event.reply(f"<@{event.user_id}> 已经订阅过了")
    scope = "本群全部账号" if camp_id == _PLAY_ALL else f"营地ID {camp_id}"
    return await event.reply(
        f"<@{event.user_id}> 已订阅 {scope} 的谁在游戏提醒\n"
        f"每 {minutes} 分钟检查一次, 有人开始游戏就播报 (只在进游戏那一下提醒)")


# ==================== 调度器 ====================

class PushScheduler:
    """战绩推送 + 谁在游戏提醒 调度器 — main.py on_load 时启动"""

    __slots__ = ("_rt", "_task", "_play_task", "_stop")

    def __init__(self, runtime):
        self._rt = runtime
        self._task: asyncio.Task | None = None
        self._play_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self):
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        self._play_task = asyncio.create_task(self._play_loop())

    async def stop(self):
        self._stop.set()
        for attr in ("_task", "_play_task"):
            task = getattr(self, attr)
            if not task:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            setattr(self, attr, None)

    async def _loop(self):
        # 启动后短暂延迟后立即做首次检查, 不再等一个完整轮询周期
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=_INITIAL_DELAY)
            return
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            return

        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._check_all()
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            # 检查已分散占用大半个周期, 从下轮等待中扣除, 保持整体节奏稳定
            delay = max(1.0, _POLL_INTERVAL - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return

    async def _check_all(self):
        camp_groups = self._rt.db.get_camp_groups()
        if not camp_groups:
            return
        # 错峰: 检查间隔 = 错峰窗口 / 账号数, 使各账号的检查均匀分布在整个周期内
        spacing = max(_API_INTERVAL, _SPREAD_WINDOW / len(camp_groups))
        for camp_id, groups in camp_groups.items():
            if self._stop.is_set():
                return
            # 用这个营地订阅者的登录态去拉 (没订阅者或没登录态就回落全局)
            subscriber = next((s for *_, s in groups if s), "")
            try:
                with requester.scoped(subscriber):
                    await self._check_camp(camp_id, groups)
            except Exception:
                pass
            await asyncio.sleep(spacing)

    @staticmethod
    def _collect_new(lst: list, last_id: str) -> list:
        out = []
        for item in lst:
            if _battle_id(item) == last_id:
                break
            out.append(item)
        return out

    async def _check_camp(self, camp_id: str, groups: list):
        try:
            resp = await self._rt.api.get_more_battle_list(camp_id)
        except AuthFailure:
            return
        except Exception:
            return
        battle_list = resp.get("data") or {}
        lst = battle_list.get("list") or []
        if not lst:
            return
        # 顺手归档, 喂日报/周报/群报: 轮询反复拉第一页, 靠归档按 gameSeq 幂等去重攒库
        try:
            self._rt.archive.archive_battles(camp_id, lst)
        except Exception:
            pass
        newest_id = _battle_id(lst[0])
        if not newest_id:
            return

        for group_id, role_name, last_id, appid, *_ in groups:
            if self._stop.is_set():
                return
            # 与订阅入口保持一致：仅向已开启主动消息且仍在群内的群推送。
            if not self._rt.is_full_access(group_id):
                continue
            if not last_id:
                self._rt.db.set_sub_last_battle(group_id, camp_id, newest_id)
                continue
            if last_id == newest_id:
                continue
            new_battles = self._collect_new(lst, last_id)
            if not new_battles:
                self._rt.db.set_sub_last_battle(group_id, camp_id, newest_id)
                continue
            ok = await self._push(group_id, camp_id, role_name, new_battles,
                                  battle_list, appid)
            if ok:
                self._rt.db.set_sub_last_battle(group_id, camp_id, newest_id)

    async def _push(self, group_id: str, camp_id: str, role_name: str,
                    new_battles: list, battle_list: dict, appid: str = "") -> bool:
        """单局推当局详情图; 多局合成一条消息、一张列表图 (不逐局推)。"""
        sender = self._rt.get_sender_for(appid)
        if not sender:
            return False
        name = role_name or camp_id
        buttons = [[{"text": "📜 查看战绩", "data": f"王者战绩 {camp_id}",
                     "enter": True, "style": 1}]]

        if len(new_battles) == 1:
            detail = await self._fetch_detail(camp_id, new_battles[0])
            if detail:
                try:
                    return await render.send_html_to_group(
                        sender, group_id, "QueryGameRecordDetails.html", detail,
                        caption=f"王者荣耀战绩更新\n{name} 当局战绩详情",
                        buttons=buttons, name_hint="push-detail")
                except Exception:
                    pass
            # 详情取不到就退回列表图, 别让这次推送整条丢掉

        subset = dict(battle_list)
        subset["list"] = new_battles[:_MAX_NEW]
        ldata = D.build_battle_list_data(subset)
        caption = (f"王者荣耀战绩更新\n{name} 有 {len(new_battles)} 场新对局"
                   if len(new_battles) > 1 else f"王者荣耀战绩更新\n{name} 有新对局")
        try:
            return await render.send_html_to_group(
                sender, group_id, "QueryGameRecordList.html", ldata,
                caption=caption, buttons=buttons, name_hint="push")
        except Exception:
            return False

    async def _fetch_detail(self, camp_id: str, battle: dict):
        """拉单局详情并转成模板数据; 任一步失败返回 None。"""
        try:
            target_role_id = D.parse_target_role_id(battle)
            resp = await self._rt.api.get_battle_detail(
                camp_id, battle.get("battleType"), battle.get("gameSvrId"),
                battle.get("relaySvrId"), target_role_id, battle.get("gameSeq"))
            detail, err = D.build_detail_data((resp or {}).get("data") or {})
            return detail if not err else None
        except Exception:
            return None

    # ==================== 谁在游戏提醒 ====================

    async def _play_loop(self):
        """每 _PLAY_INTERVAL 秒查一遍订阅账号的在线状态。"""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=_INITIAL_DELAY)
            return
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            return

        while not self._stop.is_set():
            try:
                await self._check_play_all()
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_PLAY_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return

    async def _check_play_all(self):
        rows = self._rt.db.get_all_play_subs()
        if not rows:
            return
        by_group: dict = {}
        for row in rows:
            gid = str(row.get("group_id") or "")
            if gid:
                by_group.setdefault(gid, []).append(row)
        for gid, subs in by_group.items():
            if self._stop.is_set():
                return
            try:
                await self._check_play_group(gid, subs)
            except Exception:
                pass

    def _play_targets(self, gid: str, subs: list) -> dict:
        """本群要盯的账号 {营地ID: 展示名}: 指定ID + ('*' 行 → 本群全部订阅账号)。"""
        group_subs = {str(s.get("camp_id")): str(s.get("role_name") or "")
                      for s in self._rt.db.get_group_subs(gid)}
        names = {str(b.get("camp_id")): str(b.get("role_name") or "")
                 for b in self._rt.db.get_all_bindings()}
        targets: dict = {}
        watch_all = False
        for sub in subs:
            camp = str(sub.get("camp_id") or "")
            if camp == _PLAY_ALL:
                watch_all = True
            elif camp:
                targets.setdefault(camp, str(sub.get("role_name") or "")
                                   or group_subs.get(camp, "") or names.get(camp, ""))
        if watch_all:
            for camp, name in group_subs.items():
                if camp:
                    targets.setdefault(camp, name or names.get(camp, ""))
        return targets

    async def _check_play_group(self, gid: str, subs: list):
        """查本群订阅账号的在线状态; 从"不在游戏"变成"游戏中"就播报一次。"""
        targets = self._play_targets(gid, subs)
        if not targets or not self._rt.is_full_access(gid):
            return
        appid = next((str(s.get("appid") or "") for s in subs if s.get("appid")), "")
        subscriber = next((str(s.get("subscriber") or "") for s in subs if s.get("subscriber")), "")
        sender = self._rt.get_sender_for(appid)
        if not sender:
            return

        for camp, name in list(targets.items())[:_PLAY_MAX_CAMPS]:
            if self._stop.is_set():
                return
            state = None
            try:
                with requester.scoped(subscriber):
                    state = D.online_state(await self._rt.api.get_profile(camp))
            except Exception:
                state = None
            if state is not None:
                prev = self._rt.db.get_play_state(gid, camp)
                # 只在"进游戏"那一下播报; 首次观测 (-1) 只记基线, 免得刚订阅就刷一屏
                if state == 2 and prev in (0, 1):
                    await self._push_play(sender, gid, name or camp, camp)
                self._rt.db.set_play_state(gid, camp, state)
            await asyncio.sleep(_PLAY_API_INTERVAL)

    async def _push_play(self, sender, group_id: str, name: str, camp_id: str):
        """播报「谁开始游戏了」— 文本 + 查看战绩按钮。"""
        buttons = [[{"text": "📜 查看战绩", "data": f"王者战绩 {camp_id}",
                     "enter": True, "style": 1}]]
        try:
            ok, _, _ = await sender.send_to_group(
                group_id, f"🎮 {name} 开始游戏了\n营地ID: {camp_id}", buttons=buttons)
            return bool(ok)
        except Exception:
            return False
