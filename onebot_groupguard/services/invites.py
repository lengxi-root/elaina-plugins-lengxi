"""通过 Elaina_QQBot 的统一发包接口逐个邀请来源群成员。"""

import asyncio
import re

from core.plugins import get_api
from core.runtime.embedded.packet import packet_response_bytes

from .runtime import get_runtime, start_background
from .utils import log

MAX_USERS = 200
INVITE_INTERVAL = 2
_INVITE_COMMAND = "OidbSvcTrpcTcp.0x758_1"


def identity(value, label: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[0-9]{1,12}", text) or not 0 < int(text) <= 0xFFFFFFFF:
        raise ValueError(f"{label}必须是有效的 QQ 号或群号")
    return str(int(text))


def parse_users(text) -> tuple[list[str], int]:
    if not isinstance(text, str):
        raise ValueError("QQ 列表必须为文本，每行一个 QQ 号")
    users, seen, duplicates = [], set(), 0
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        user_id = identity(line, f"第 {number} 行 QQ 号")
        if user_id in seen:
            duplicates += 1
            continue
        seen.add(user_id)
        users.append(user_id)
    if not users:
        raise ValueError("请输入 QQ 列表，每行一个 QQ 号")
    if len(users) > MAX_USERS:
        raise ValueError(f"每次最多邀请 {MAX_USERS} 个不同的 QQ 号")
    return users, duplicates


def _varint(value: int) -> bytes:
    output = bytearray()
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def _integer(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _bytes(number: int, value: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def build_invite_packet(source_id: str, target_id: str, uid: str) -> bytes:
    # Lagrange GroupInviteService / OidbSvcTrpcTcp0x758_1:
    # body.1=目标群，body.2={1:成员 UID, 2:来源群}，body.10=0。
    member = _bytes(1, uid.encode()) + _integer(2, int(source_id))
    body = _integer(1, int(target_id)) + _bytes(2, member) + _integer(10, 0)
    return _integer(1, 0x758) + _integer(2, 1) + _bytes(4, body) + _integer(12, 0)


def _fields(data: bytes) -> dict:
    fields, offset = {}, 0

    def read_varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                break
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("邀请响应 PB 数据损坏")

    while offset < len(data):
        tag = read_varint()
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("邀请响应 PB 字段无效")
        if wire == 0:
            value = read_varint()
        elif wire in (1, 2, 5):
            size = read_varint() if wire == 2 else (8 if wire == 1 else 4)
            if offset + size > len(data):
                raise ValueError("邀请响应 PB 数据不完整")
            value = data[offset : offset + size]
            offset += size
        else:
            raise ValueError("邀请响应 PB 编码不支持")
        fields[number] = value
    return fields


def check_invite_response(data) -> None:
    fields = _fields(packet_response_bytes(data))
    if fields.get(1) != 0x758 or fields.get(2) != 1:
        raise ValueError("邀请接口未返回有效的 QQ 邀请回执")
    code = fields.get(3, 0)
    if code:
        detail = fields.get(5, b"")
        message = (
            detail.decode("utf-8", errors="replace")
            if isinstance(detail, bytes)
            else ""
        )
        raise ValueError(f"QQ 拒绝邀请 ({code})" + (f"：{message}" if message else ""))


async def _api(action: str, params: dict, self_id: str):
    response = await get_api().call_api(action, params, self_id=self_id)
    if (
        not isinstance(response, dict)
        or response.get("status") != "ok"
        or str(response.get("retcode")) != "0"
    ):
        detail = (
            response.get("message") or response.get("wording")
            if isinstance(response, dict)
            else "未返回有效响应"
        )
        raise RuntimeError(f"{action} 失败：{detail or 'OneBot 接口调用失败'}")
    return response.get("data")


async def list_groups(self_id: str) -> list:
    groups = await _api("get_group_list", {"no_cache": True}, self_id)
    if not isinstance(groups, list):
        raise RuntimeError("获取群列表未返回有效数据")
    return [item for item in groups if isinstance(item, dict)]


async def _members(group_id: str, self_id: str) -> dict:
    members = await _api(
        "get_group_member_list", {"group_id": int(group_id), "no_cache": True}, self_id
    )
    if not isinstance(members, list):
        raise RuntimeError(f"群 {group_id} 成员列表无效，已停止邀请")
    return {
        str(item.get("user_id")): item
        for item in members
        if isinstance(item, dict) and item.get("user_id")
    }


def start_invites(body: dict) -> dict:
    self_id = identity(body.get("self_id"), "机器人账号")
    source_id = identity(body.get("source_group_id"), "来源群")
    target_id = identity(body.get("target_group_id"), "目标群")
    if source_id == target_id:
        raise ValueError("来源群和目标群不能相同")
    users, duplicates = parse_users(body.get("users"))
    if self_id not in get_api().connected_self_ids():
        raise ValueError("所选机器人不在线，请刷新账号列表")
    runtime = get_runtime()
    previous = getattr(runtime, "invite_job", None)
    if previous and previous["status"] == "running":
        raise RuntimeError("已有批量邀请任务正在执行，请等待完成")
    job = {
        "self_id": self_id,
        "source_group_id": source_id,
        "target_group_id": target_id,
        "status": "running",
        "total": len(users),
        "duplicates": duplicates,
        "results": [],
        "message": "正在检查来源群和目标群",
    }
    runtime.invite_job = job
    task = start_background(_run_invites(job, users))

    def on_done(completed):
        # 任务可能在首次获得执行机会之前就因插件卸载而取消。
        if completed.cancelled() and job["status"] == "running":
            job.update(status="cancelled", message="插件卸载，邀请任务已停止")

    task.add_done_callback(on_done)
    return job


async def _run_invites(job: dict, users: list[str]) -> None:
    self_id = job["self_id"]
    source_id, target_id = job["source_group_id"], job["target_group_id"]
    try:
        group_ids = {str(g.get("group_id")) for g in await list_groups(self_id)}
        if source_id not in group_ids or target_id not in group_ids:
            raise ValueError("所选机器人必须同时加入来源群和目标群")
        source = await _members(source_id, self_id)
        target = await _members(target_id, self_id)
        invited_before = False
        for user_id in users:
            result = {"user_id": user_id, "status": "skipped", "message": ""}
            if user_id in target:
                result["message"] = "已在目标群"
            elif user_id not in source:
                result["message"] = "不在来源群"
            else:
                if invited_before:
                    await asyncio.sleep(INVITE_INTERVAL)
                invited_before = True
                job["message"] = f"正在邀请 {user_id}"
                try:
                    member = source[user_id]
                    uid = str(member.get("uid") or member.get("user_uid") or "")
                    if not uid.startswith("u_"):
                        info = await _api(
                            "get_stranger_info", {"user_id": int(user_id)}, self_id
                        )
                        uid = (
                            str(info.get("uid") or "") if isinstance(info, dict) else ""
                        )
                    if not uid.startswith("u_"):
                        raise ValueError("协议端未提供成员 UID，无法发起邀请")
                    packet = build_invite_packet(source_id, target_id, uid)
                    response = await _api(
                        "send_packet",
                        {"cmd": _INVITE_COMMAND, "data": packet.hex(), "rsp": True},
                        self_id,
                    )
                    check_invite_response(response)
                    result.update(
                        status="submitted", message="邀请已提交，入群结果以 QQ 为准"
                    )
                except Exception as exc:  # noqa: BLE001
                    result.update(status="failed", message=str(exc))
                    log.warning(f"批量邀请失败 {user_id}: {exc}")
            job["results"].append(result)
        job.update(status="completed", message="批量邀请处理完成")
    except asyncio.CancelledError:
        job.update(
            status="cancelled",
            message="插件卸载，邀请任务已停止；未完成的邀请请核实后重试",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        job.update(status="failed", message=str(exc))
        log.warning(f"批量邀请任务失败: {exc}")
