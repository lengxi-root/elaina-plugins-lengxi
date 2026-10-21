"""配置校验；兼容旧版秒数配置，保留用户设置的检查间隔。"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_CONFIG = {
    "enabled": True,
    "interval_minutes": 5,
    "request_timeout_seconds": 30,
    "concurrency": 8,
    "request_retries": 2,
    "max_message_bytes": 3500,
    "send_timeout_seconds": 30,
    "send_interval_seconds": 1,
    "render_backend": "auto",
    "image_max_height": 12000,
    "diff_context_lines": 2,
    "font_path": "",
}
CONFIG_COMMENTS = {
    "enabled": "启用监控",
    "interval_minutes": "检查间隔（分钟，最低 1 分钟；每轮核对全部正文）",
    "request_timeout_seconds": "每次 HTTP 请求超时（秒，5～120）",
    "concurrency": "抓取并发数（1～32）",
    "request_retries": "临时网络错误的额外重试次数（0～3）",
    "max_message_bytes": "每段通知的 UTF-8 字节上限（512～4000）",
    "send_timeout_seconds": "每段推送超时（秒，5～120）",
    "send_interval_seconds": "连续推送的间隔（秒，0～10）",
    "render_backend": "图片后端：auto 优先 PIL、失败尝试 Playwright；pil / playwright 固定后端",
    "image_max_height": "单张差异图片最大高度（像素，2000～20000），超长自动分图",
    "diff_context_lines": "修改处保留的前后文行数（0～10），变更行完整保留",
    "font_path": "可选中文字体文件路径（ttf / ttc / otf）；留空自动查找系统中文字体",
}


@dataclass(frozen=True, slots=True)
class Settings:
    enabled: bool
    interval_seconds: float
    request_timeout_seconds: float
    concurrency: int
    request_retries: int
    max_message_bytes: int
    send_timeout_seconds: float
    send_interval_seconds: float
    render_backend: str
    image_max_height: int
    diff_context_lines: int
    font_path: str


def prepare_config(raw: dict) -> tuple[dict, Settings]:
    if not isinstance(raw, dict):
        raise ValueError("config.yaml 必须是键值配置")
    config = {**DEFAULT_CONFIG, **raw}
    if "interval_minutes" not in raw and "interval_seconds" in raw:
        try:
            config["interval_minutes"] = float(raw["interval_seconds"]) / 60
        except (TypeError, ValueError) as exc:
            raise ValueError("旧配置 interval_seconds 必须是有效数字") from exc

    def number(
        key: str, minimum: float, maximum: float | None = None, *, integer: bool = False
    ):
        value = config[key]
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"配置 {key} 必须是数字") from exc
        if (
            isinstance(value, bool)
            or not math.isfinite(parsed)
            or integer
            and not parsed.is_integer()
        ):
            raise ValueError(f"配置 {key} 必须是有效{'整数' if integer else '数字'}")
        if parsed < minimum or maximum is not None and parsed > maximum:
            raise ValueError(
                f"配置 {key} 超出范围：最低 {minimum:g}"
                + (f"，最高 {maximum:g}" if maximum is not None else "")
            )
        return int(parsed) if integer else parsed

    if not isinstance(config["enabled"], bool):
        raise ValueError("配置 enabled 必须是 true 或 false")
    if config["render_backend"] not in ("auto", "pil", "playwright"):
        raise ValueError("配置 render_backend 必须是 auto、pil 或 playwright")
    if not isinstance(config["font_path"], str):
        raise ValueError("配置 font_path 必须是字体文件路径字符串")
    interval = number("interval_minutes", 0)
    settings = Settings(
        enabled=config["enabled"],
        interval_seconds=max(60, interval * 60),
        request_timeout_seconds=number("request_timeout_seconds", 5, 120),
        concurrency=number("concurrency", 1, 32, integer=True),
        request_retries=number("request_retries", 0, 3, integer=True),
        max_message_bytes=number("max_message_bytes", 512, 4000, integer=True),
        send_timeout_seconds=number("send_timeout_seconds", 5, 120),
        send_interval_seconds=number("send_interval_seconds", 0, 10),
        render_backend=config["render_backend"],
        image_max_height=number("image_max_height", 2000, 20000, integer=True),
        diff_context_lines=number("diff_context_lines", 0, 10, integer=True),
        font_path=config["font_path"].strip(),
    )
    return config, settings
