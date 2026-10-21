"""原子持久化、旧快照迁移和群订阅存储。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .documents import (
    LEGACY_DOC_PREFIX,
    content_digest,
    links_only_legacy_message,
    normalize_content,
    normalize_title,
)

STATE_VERSION = 2


def load_json(path: str | Path) -> dict:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"数据文件损坏，请先修复或从备份恢复：{path.name}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"数据文件必须是 JSON 对象：{path.name}")
    return data


def save_json(path: str | Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_state(path: str | Path) -> dict:
    data = load_json(path)
    if not data and not Path(path).exists():
        return {
            "version": STATE_VERSION,
            "initialized": False,
            "pages": {},
            "outbox": [],
        }
    if data.get("version") not in (1, STATE_VERSION):
        raise ValueError("不支持的快照版本，保留原文件，停止检查以免覆盖基线")
    pages = data.get("pages")
    if not isinstance(pages, dict) or not all(
        isinstance(url, str) and isinstance(page, dict) for url, page in pages.items()
    ):
        raise ValueError("文档快照 pages 格式损坏，停止检查以免覆盖基线")
    if not isinstance(data.get("initialized"), bool):
        raise ValueError("文档快照 initialized 格式损坏，停止检查以免覆盖基线")
    if data["version"] == 1:
        # 旧版仅监控 api-v2。扩展范围时只为原来未覆盖的历史页建立基线。
        data["legacy_scope"] = LEGACY_DOC_PREFIX
        for page in pages.values():
            page["title"] = normalize_title(str(page.get("title", "")))
            page["content"] = normalize_content(str(page.get("content", "")))
            if page.get("digest"):
                page["digest"] = content_digest(page["title"], page["content"])
            else:
                page["pending_baseline"] = True
        data["version"] = STATE_VERSION
        data["outbox"] = []
    if not isinstance(data.get("outbox"), list):
        raise ValueError("文档推送队列格式损坏，停止检查以免丢失通知")
    for batch in data["outbox"]:
        if (
            not isinstance(batch, dict)
            or not isinstance(batch.get("id"), str)
            or not isinstance(batch.get("messages"), list)
            or not batch["messages"]
            or not all(
                isinstance(message, str) and message for message in batch["messages"]
            )
            or not isinstance(batch.get("targets"), dict)
        ):
            raise ValueError("文档推送批次格式损坏，停止检查以免丢失通知")
        if "image_pending" not in batch:
            batch["messages"] = [
                links_only_legacy_message(message) for message in batch["messages"]
            ]
        if "image_pending" in batch and type(batch["image_pending"]) is not bool:
            raise ValueError("文档图片推送状态损坏，停止检查以免丢失通知")
        if batch.get("image_pending"):
            changes = batch.get("image_changes")
            if (
                not isinstance(changes, list)
                or not changes
                or not all(
                    isinstance(change, dict)
                    and change.get("kind") in ("added", "modified", "removed")
                    and all(
                        isinstance(change.get(key), str)
                        for key in ("url", "title", "before", "after")
                    )
                    for change in changes
                )
            ):
                raise ValueError("文档图片差异数据损坏，停止检查以免丢失通知")
            uploaded = batch.get("image_uploads", {})
            if not isinstance(uploaded, dict) or not all(
                isinstance(key, str) and isinstance(url, str)
                for key, url in uploaded.items()
            ):
                raise ValueError("文档图片上传缓存损坏，停止检查以免丢失通知")
        for key, target in batch["targets"].items():
            if (
                not isinstance(target, dict)
                or target.get("kind") not in ("owner", "group")
                or not isinstance(target.get("appid"), str)
                or not isinstance(target.get("target_id"), str)
                or type(target.get("next_part")) is not int
                or not 0 <= target["next_part"] <= len(batch["messages"])
                or key != f"{target['kind']}:{target['appid']}:{target['target_id']}"
            ):
                raise ValueError("文档推送进度格式损坏，停止检查以免重复发送")
    return data


class SubscriptionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, list[str]]:
        apps = load_json(self.path).get("apps", {})
        if not isinstance(apps, dict):
            raise ValueError("群订阅 apps 格式损坏")
        result = {}
        for appid, groups in apps.items():
            if not isinstance(groups, list):
                raise ValueError(f"机器人 {appid} 的群订阅必须是列表")
            if not all(
                isinstance(group, (str, int)) and not isinstance(group, bool)
                for group in groups
            ):
                raise ValueError(f"机器人 {appid} 的群订阅含无效群标识")
            clean = sorted(
                {str(group).strip() for group in groups if str(group).strip()}
            )
            if str(appid).strip() and clean:
                result[str(appid).strip()] = clean
        return result

    def set(self, appid: str, group_id: str, enabled: bool) -> bool:
        apps = self.load()
        groups = set(apps.get(appid, []))
        existed = group_id in groups
        if enabled:
            groups.add(group_id)
        else:
            groups.discard(group_id)
        if groups:
            apps[appid] = sorted(groups)
        else:
            apps.pop(appid, None)
        save_json(self.path, {"version": 1, "apps": apps})
        return existed
