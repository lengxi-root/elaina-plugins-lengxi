"""持久化通知队列：按目标、按分段记录进度，失败不阻断文档采集。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Target:
    kind: str
    appid: str
    target_id: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.appid}:{self.target_id}"

    def progress(self) -> dict:
        return {
            "kind": self.kind,
            "appid": self.appid,
            "target_id": self.target_id,
            "next_part": 0,
            "attempts": 0,
            "error": "",
        }


@dataclass(slots=True)
class DeliveryReport:
    sent_parts: int = 0
    pending_batches: int = 0
    pending_targets: int = 0
    pending_parts: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def queue_report(state: dict) -> DeliveryReport:
    report = DeliveryReport(pending_batches=len(state["outbox"]))
    for batch in state["outbox"]:
        image_pending = bool(batch.get("image_pending"))
        if image_pending and batch.get("image_error"):
            report.errors[f"image:{batch['id']}"] = batch["image_error"]
        if batch.get("unassigned"):
            report.errors[f"batch:{batch['id']}"] = "暂无主人或订阅群，等待通知目标"
        for key, target in batch["targets"].items():
            remaining = len(batch["messages"]) - target["next_part"] + image_pending
            if remaining:
                report.pending_targets += 1
                report.pending_parts += remaining
                if target.get("error"):
                    report.errors[key] = target["error"]
    return report


def send_error(result) -> str:
    if isinstance(result, (tuple, list)) and result and result[0] is True:
        return ""
    detail = (
        result[1] if isinstance(result, (tuple, list)) and len(result) > 1 else result
    )
    if isinstance(detail, dict):
        return f"code={detail.get('code', '?')}: {detail.get('message', detail)}"[:1000]
    return f"发送失败：{detail!s}"[:1000]


class DeliveryQueue:
    def __init__(
        self,
        targets: Callable[[], list[Target]],
        send: Callable[[Target, str], Awaitable],
        *,
        timeout: float = 30,
        interval: float = 1,
        prepare_images: Callable[[dict], Awaitable[list[str]]] | None = None,
    ):
        self.targets = targets
        self.send = send
        self.timeout = timeout
        self.interval = interval
        self.prepare_images = prepare_images
        self._last_send_at: float | None = None

    def enqueue(
        self,
        state: dict,
        messages: list[str],
        *,
        image_changes: list[dict] | None = None,
    ) -> None:
        targets = {target.key: target.progress() for target in self.targets()}
        state["outbox"].append(
            {
                "id": uuid4().hex,
                "created_at": now_iso(),
                "messages": messages,
                "targets": targets,
                "unassigned": not targets,
                **(
                    {"image_changes": image_changes, "image_pending": True}
                    if image_changes
                    else {}
                ),
            }
        )

    async def flush(
        self, state: dict, persist: Callable[[], Awaitable[None]]
    ) -> DeliveryReport:
        sent = 0
        blocked: set[str] = set()
        for batch in list(state["outbox"]):
            if batch.get("unassigned"):
                targets = {target.key: target.progress() for target in self.targets()}
                if not targets:
                    continue
                batch["targets"] = targets
                batch["unassigned"] = False
                await persist()
            if batch.get("image_pending"):
                try:
                    if self.prepare_images is None:
                        raise RuntimeError("差异图片服务尚未配置")
                    images = await self.prepare_images(batch)
                    if not images or not all(
                        isinstance(image, str) and image for image in images
                    ):
                        raise RuntimeError("渲染或上传未返回图片消息")
                except Exception as exc:
                    batch["image_error"] = f"{type(exc).__name__}: {exc}"[:1000]
                else:
                    batch["messages"].extend(images)
                    batch["image_pending"] = False
                    batch.pop("image_error", None)
                    batch.pop("image_changes", None)
                    batch.pop("image_uploads", None)
                # 链接先发、图片失败后补发；原始差异在上传成功前一直保留。
                await persist()
            for key, progress in batch["targets"].items():
                if key in blocked:
                    continue
                target = Target(
                    progress["kind"], progress["appid"], progress["target_id"]
                )
                while progress["next_part"] < len(batch["messages"]):
                    if self._last_send_at is not None:
                        delay = self.interval - (
                            asyncio.get_running_loop().time() - self._last_send_at
                        )
                        if delay > 0:
                            await asyncio.sleep(delay)
                    progress["attempts"] += 1
                    progress["last_attempt_at"] = now_iso()
                    try:
                        result = await asyncio.wait_for(
                            self.send(target, batch["messages"][progress["next_part"]]),
                            self.timeout,
                        )
                        error = send_error(result)
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"[:1000]
                    self._last_send_at = asyncio.get_running_loop().time()
                    progress["error"] = error
                    if error:
                        blocked.add(key)
                    else:
                        progress["next_part"] += 1
                        sent += 1
                    # 每段成功后立即落盘；崩溃重启只补发尚未确认的分段。
                    await persist()
                    if error:
                        break
                if batch.get("image_pending"):
                    blocked.add(key)
            if not batch.get("image_pending") and all(
                target["next_part"] >= len(batch["messages"])
                for target in batch["targets"].values()
            ):
                state["outbox"].remove(batch)
                await persist()
        report = queue_report(state)
        report.sent_parts = sent
        return report

    @staticmethod
    def cancel_target(state: dict, target: Target) -> None:
        for batch in list(state["outbox"]):
            batch["targets"].pop(target.key, None)
            if not batch.get("unassigned") and (
                not batch["targets"]
                or not batch.get("image_pending")
                and all(
                    progress["next_part"] >= len(batch["messages"])
                    for progress in batch["targets"].values()
                )
            ):
                state["outbox"].remove(batch)
