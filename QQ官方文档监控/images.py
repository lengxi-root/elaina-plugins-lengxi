"""批次图片生成与图床上传；同批图片供所有目标复用。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from urllib.parse import urlsplit

from .rendering import DiffRenderer


class ChangeImages:
    def __init__(self, renderer: DiffRenderer, get_module: Callable, get_bot: Callable):
        self.renderer = renderer
        self.get_module = get_module
        self.get_bot = get_bot

    async def prepare(self, batch: dict) -> list[str]:
        hosting = self.get_module("image_hosting")
        if hosting is None:
            raise RuntimeError(
                "请安装并启用 image_hosting 图床模块，链接正常发送，差异图片将自动重试"
            )
        images = await self.renderer.render(batch["image_changes"], batch["created_at"])
        bot = next(
            (
                bot
                for target in batch["targets"].values()
                if (bot := self.get_bot(target["appid"])) is not None
            ),
            None,
        )
        kwargs = {}
        if bot is not None:
            kwargs = {"token_manager": bot.token_manager, "sender": bot.sender}
        uploaded = batch.setdefault("image_uploads", {})
        messages = []
        for index, (data, width, height) in enumerate(images, 1):
            digest = hashlib.sha256(data).hexdigest()
            url = uploaded.get(digest)
            if not url:
                url = await asyncio.wait_for(
                    hosting.upload_any(
                        data, f"qq_docs_{batch['id']}_{index}.png", **kwargs
                    ),
                    timeout=60,
                )
                if (
                    not isinstance(url, str)
                    or urlsplit(url).scheme not in ("http", "https")
                    or not urlsplit(url).netloc
                ):
                    raise RuntimeError(
                        "全部已启用图床上传失败，请检查 image_hosting 模块配置或日志"
                    )
                # 多张图片中途失败后，重试复用已经上传成功的直链。
                uploaded[digest] = url
            messages.append(
                f"QQ 开放平台文档变更（图片 {index}/{len(images)}）\n![文档变更 #{width}px #{height}px]({url})"
            )
        return messages
