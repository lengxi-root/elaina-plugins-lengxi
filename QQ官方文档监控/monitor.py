"""全量正文核对、删除确认与持久化通知调度。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from .delivery import DeliveryQueue, DeliveryReport, Target, now_iso, queue_report
from .documents import (
    SITEMAP_URL,
    PageChange,
    PageSnapshot,
    build_notifications,
    compare_page,
    official_url,
    parse_page,
    parse_sitemap,
)
from .storage import load_state, save_json


@dataclass(slots=True)
class CheckResult:
    initialized: bool
    changes: list[PageChange]
    page_count: int
    fetched_count: int
    failed_urls: list[str]
    failure_details: dict[str, str] = field(default_factory=dict)
    delivery: DeliveryReport = field(default_factory=DeliveryReport)


class FetchError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class _Fetched:
    snapshot: PageSnapshot | None = None
    error: str = ""
    status_code: int | None = None


class DocsMonitor:
    def __init__(
        self,
        state_path: str | Path,
        targets: Callable[[], list[Target]],
        send: Callable[[Target, str], Awaitable],
        *,
        timeout: float = 30,
        concurrency: int = 8,
        retries: int = 2,
        max_message_bytes: int = 3500,
        send_timeout: float = 30,
        send_interval: float = 1,
        client: httpx.AsyncClient | None = None,
        prepare_images: Callable[[dict], Awaitable[list[str]]] | None = None,
    ):
        self.state_path = Path(state_path)
        self.status_path = self.state_path.with_name("status.json")
        self.timeout = timeout
        self.concurrency = concurrency
        self.retries = retries
        self.max_message_bytes = max_message_bytes
        self.delivery = DeliveryQueue(
            targets,
            send,
            timeout=send_timeout,
            interval=send_interval,
            prepare_images=prepare_images,
        )
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._closed = False
        self.status: dict = {}

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            if self._owns_client and self._client is not None:
                await self._client.aclose()
            self._client = None

    async def _persist(self, state: dict) -> None:
        # 热重载取消后台任务时，必须等正在进行的磁盘提交完成再释放检查锁。
        writing = asyncio.create_task(
            asyncio.to_thread(save_json, self.state_path, state)
        )
        try:
            await asyncio.shield(writing)
        except asyncio.CancelledError:
            await writing
            raise

    async def check_once(self) -> CheckResult:
        async with self._lock:
            if self._closed:
                raise RuntimeError("文档监控器已停止")
            started = asyncio.get_running_loop().time()
            self.status.update(
                {
                    "checking": True,
                    "started_at": now_iso(),
                    "last_error": "",
                    "failure_details": {},
                }
            )
            state = None
            result = None
            failure: Exception | None = None
            report = DeliveryReport()
            try:
                state = await asyncio.to_thread(load_state, self.state_path)
                result = await self._scan(state)
            except Exception as exc:
                failure = exc
            # 即使 sitemap/网络异常，已落盘的通知仍然单独重试。
            if state is not None:
                try:
                    report = await self.delivery.flush(
                        state, lambda: self._persist(state)
                    )
                except Exception as exc:
                    failure = failure or exc
                    report = queue_report(state)
            self.status.update(
                {
                    "checking": False,
                    "finished_at": now_iso(),
                    "elapsed_seconds": round(
                        asyncio.get_running_loop().time() - started, 2
                    ),
                    "last_error": str(failure)[:1000] if failure else "",
                    "last_success_at": state.get("checked_at", "") if state else "",
                    "sent_parts": report.sent_parts,
                    "pending_batches": report.pending_batches,
                    "pending_targets": report.pending_targets,
                    "pending_parts": report.pending_parts,
                    "delivery_errors": report.errors,
                }
            )
            if result is not None:
                result.delivery = report
                self.status.update(
                    {
                        "page_count": result.page_count,
                        "fetched_count": result.fetched_count,
                        "change_count": len(result.changes),
                    }
                )
            await asyncio.to_thread(save_json, self.status_path, self.status)
            if failure:
                raise RuntimeError(str(failure)) from failure
            assert result is not None
            return result

    async def retry_notifications(self) -> DeliveryReport:
        async with self._lock:
            if self._closed:
                raise RuntimeError("文档监控器已停止")
            state = await asyncio.to_thread(load_state, self.state_path)
            report = await self.delivery.flush(state, lambda: self._persist(state))
            self.status.update(
                {
                    "delivery_attempt_at": now_iso(),
                    "sent_parts": report.sent_parts,
                    "pending_batches": report.pending_batches,
                    "pending_targets": report.pending_targets,
                    "pending_parts": report.pending_parts,
                    "delivery_errors": report.errors,
                }
            )
            await asyncio.to_thread(save_json, self.status_path, self.status)
            return report

    async def cancel_target(self, target: Target) -> None:
        async with self._lock:
            state = await asyncio.to_thread(load_state, self.state_path)
            self.delivery.cancel_target(state, target)
            await self._persist(state)

    async def _scan(self, state: dict) -> CheckResult:
        sitemap = await self._load_sitemap()
        old_pages: dict[str, dict] = state["pages"]
        initialized = bool(state.get("initialized"))
        # lastmod 仅作诊断信息，绝不用于跳过正文抓取；缺失于 sitemap 的旧页也需验证。
        urls = sorted(set(sitemap) | set(old_pages))
        fetched = await self._fetch_pages(urls, sitemap)
        failures = {
            url: item.error
            for url, item in fetched.items()
            if item.error and (url in sitemap or item.status_code not in (404, 410))
        }
        self.status.update({"failed_pages": len(failures), "failure_details": failures})
        if not any(fetched[url].snapshot is not None for url in sitemap):
            raise RuntimeError("本轮所有在册文档均抓取失败，已保留上次基线")

        next_pages: dict[str, dict] = {}
        changes: list[PageChange] = []
        legacy_scope = state.get("legacy_scope")
        for url in urls:
            old = old_pages.get(url)
            item = fetched[url]
            if item.snapshot is not None:
                snapshot = item.snapshot
                next_pages[url] = snapshot.to_dict()
                if old and old.get("digest"):
                    change = compare_page(old, snapshot)
                    if initialized and change:
                        changes.append(change)
                elif old is not None:
                    if initialized and not old.get("pending_baseline", False):
                        changes.append(
                            PageChange(
                                "added", url, snapshot.title, after=snapshot.content
                            )
                        )
                elif initialized and not (
                    legacy_scope and not url.startswith(legacy_scope)
                ):
                    changes.append(
                        PageChange("added", url, snapshot.title, after=snapshot.content)
                    )
                continue

            if old is None:
                # 首轮失败的历史页恢复后只补基线；后续真正新增但暂时失败的页恢复后通知。
                next_pages[url] = {
                    "title": url,
                    "content": "",
                    "digest": "",
                    "lastmod": sitemap.get(url, ""),
                    "pending_baseline": not initialized
                    or bool(legacy_scope and not url.startswith(legacy_scope)),
                    "missing_checks": 0,
                }
                continue
            page = dict(old)
            confirmed_missing = url not in sitemap and item.status_code in (404, 410)
            page["missing_checks"] = (
                int(old.get("missing_checks", 0)) + 1 if confirmed_missing else 0
            )
            # 避免目录短暂缺页、风控或网络错误引发删除通知。
            if confirmed_missing and page["missing_checks"] >= 2:
                if initialized and old.get("digest"):
                    changes.append(
                        PageChange(
                            "removed",
                            url,
                            str(old.get("title") or url),
                            before=str(old.get("content", "")),
                        )
                    )
            else:
                next_pages[url] = page

        changes.sort(key=lambda change: (change.kind, change.url))
        next_state = copy.deepcopy(state)
        next_state.pop("legacy_scope", None)
        next_state.update(
            {"initialized": True, "checked_at": now_iso(), "pages": next_pages}
        )
        if changes:
            self.delivery.enqueue(
                next_state,
                build_notifications(changes, self.max_message_bytes),
                image_changes=[asdict(change) for change in changes],
            )
        # 快照与通知队列一起提交，成功落盘之后才允许发送。
        await self._persist(next_state)
        state.clear()
        state.update(next_state)
        return CheckResult(
            not initialized,
            changes,
            len(sitemap),
            len(urls),
            sorted(failures),
            failures,
        )

    async def _load_sitemap(self) -> dict[str, str]:
        pending = [SITEMAP_URL]
        seen: set[str] = set()
        pages: dict[str, str] = {}
        while pending:
            url = pending.pop()
            if url in seen:
                continue
            if len(seen) >= 32:
                raise RuntimeError("sitemap 索引超过 32 个，未使用不完整目录")
            seen.add(url)
            response = await self._request(url)
            try:
                document = parse_sitemap(response.text, url)
            except Exception as exc:
                raise RuntimeError(
                    f"无法解析 sitemap [{url}]：{exc}；响应摘要：{response.text[:300]}"
                ) from exc
            pages.update(document.pages)
            pending.extend(document.indexes)
        if not pages:
            raise RuntimeError("官方 sitemap 中没有文档页面，已保留上次基线")
        return dict(sorted(pages.items()))

    async def _request(self, url: str) -> httpx.Response:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.timeout,
                limits=httpx.Limits(
                    max_connections=self.concurrency,
                    max_keepalive_connections=self.concurrency,
                ),
                headers={"User-Agent": "ElainaBot-QQDocsMonitor/2.0"},
            )
        for attempt in range(self.retries + 1):
            try:
                response = await self._client.get(
                    url,
                    headers={
                        "Cache-Control": "no-cache, max-age=0",
                        "Pragma": "no-cache",
                    },
                    timeout=self.timeout,
                )
                if response.status_code != 200:
                    error = FetchError(
                        f"HTTP {response.status_code}；响应摘要：{response.text[:300]}",
                        response.status_code,
                    )
                    if (
                        response.status_code not in (408, 425, 429)
                        and response.status_code < 500
                    ):
                        raise error
                    if attempt == self.retries:
                        raise error
                else:
                    actual = official_url(str(response.url))
                    if actual is None or actual.rstrip("/") != url.rstrip("/"):
                        raise FetchError(f"文档被重定向至其他页面：{response.url}")
                    return response
            except httpx.RequestError as exc:
                if attempt == self.retries:
                    raise FetchError(f"{type(exc).__name__}: {exc}") from exc
            await asyncio.sleep(0.5 * (2**attempt))
        raise AssertionError("请求重试未返回结果")

    async def _fetch_pages(
        self, urls: list[str], sitemap: dict[str, str]
    ) -> dict[str, _Fetched]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def fetch(url: str) -> tuple[str, _Fetched]:
            async with semaphore:
                try:
                    response = await self._request(url)
                    return url, _Fetched(
                        snapshot=parse_page(url, sitemap.get(url, ""), response.text)
                    )
                except FetchError as exc:
                    return url, _Fetched(error=str(exc), status_code=exc.status_code)
                except Exception as exc:
                    return url, _Fetched(error=f"{type(exc).__name__}: {exc}"[:1000])

        return dict(await asyncio.gather(*(fetch(url) for url in urls)))
