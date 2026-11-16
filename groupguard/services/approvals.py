"""合并同一份入群申请的审批请求和通知。"""

from collections import OrderedDict
from dataclasses import dataclass

from .utils import api_pair

_MAX_REQUESTS = 2048
DUPLICATE_REVIEW = "GROUPGUARD_DUPLICATE_REVIEW"


@dataclass(slots=True)
class _Request:
    reviewing: bool = False
    reviewed: bool = False
    notifying: bool = False
    notified: bool = False


_requests: OrderedDict[tuple[str, str, str, str], _Request] = OrderedDict()


def _request(event, member_id, request_id):
    key = (str(event.appid), str(event.group_id), member_id, request_id)
    entry = _requests.setdefault(key, _Request())
    _requests.move_to_end(key)
    if len(_requests) > _MAX_REQUESTS:
        for old_key, old in _requests.items():
            if old is not entry and not old.reviewing and not old.notifying:
                del _requests[old_key]
                break
    return entry


def mark_reviewed(event, member_id, request_id):
    _request(event, member_id, request_id).reviewed = True


async def notify_once(event, member_id, request_id, send):
    entry = _request(event, member_id, request_id)
    if entry.reviewed or entry.notifying or entry.notified:
        return
    entry.notifying = True
    try:
        entry.notified = bool(await send())
    finally:
        entry.notifying = False


async def review(event, member_id, request_id, operation, *, reason="", blacklisted=False):
    entry = _request(event, member_id, request_id)
    if entry.reviewed:
        return False, {"code": DUPLICATE_REVIEW, "message": "该申请已处理，请刷新入群申请列表"}
    if entry.reviewing:
        return False, {"code": DUPLICATE_REVIEW, "message": "该申请正在处理"}
    entry.reviewing = True
    try:
        ok, response = await api_pair(
            event.sender.review_group_join_request(
                event.group_id,
                member_id,
                operation,
                join_request_id=request_id,
                reject_reason=reason if operation == "decline" else "",
                add_to_member_blacklist=blacklisted,
            )
        )
        if ok:
            entry.reviewed = True
        return bool(ok), response
    finally:
        entry.reviewing = False
