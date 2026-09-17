"""战绩归档 — 日报/周报/群报的数据层 (移植自 battleArchive.js)。"""

import json
import math
import os
import time

ARCHIVE_KEEP_DAYS = 35

# 落库只留报告真正会用到的字段 (完整列表项 60+ 字段 ≈ 1.5KB/场)
KEEP_FIELDS = (
    "gameSeq", "dtEventTime", "gameresult", "heroId", "gradeGame",
    "mvpcnt", "losemvp", "mapName", "usedTime",
    "killcnt", "deadcnt", "assistcnt",
    "roleJobName", "roleJob", "stars",
    "oldMasterMatchScore", "newMasterMatchScore", "desc",
)
_INT_FIELDS = {"dtEventTime", "gameresult", "heroId", "mvpcnt", "losemvp",
               "usedTime", "killcnt", "deadcnt", "assistcnt", "roleJob",
               "stars", "oldMasterMatchScore", "newMasterMatchScore"}


def _int(value) -> int:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0
    return int(num) if math.isfinite(num) else 0


def _cutoff_sec() -> int:
    return int(time.time()) - ARCHIVE_KEEP_DAYS * 86400


class BattleArchive:
    """整库内存缓存 + JSON 落盘。只有本模块写这个文件, 单进程内内存即权威副本。"""

    def __init__(self, path: str):
        self._path = path
        self._cache = None

    def _load_all(self) -> dict:
        if self._cache is not None:
            return self._cache
        try:
            with open(self._path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            self._cache = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            self._cache = {}
        except Exception:
            # 归档是 35 天攒出来的, 坏了先挪走留证再按空库继续,
            try:
                os.replace(self._path, self._path + ".corrupt")
            except OSError:
                pass
            self._cache = {}
        return self._cache

    def _save_all(self, data: dict) -> None:
        self._cache = data or {}
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._cache, fp, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self._path)
        except Exception:
            pass

    # -------------------- 查询 --------------------

    def load_archive(self, camp_id) -> list:
        entry = self._load_all().get(str(camp_id or "")) or {}
        return entry.get("battles") or []

    def archive_range(self, camp_id) -> dict:
        battles = self.load_archive(camp_id)
        if not battles:
            return {"earliest": 0, "latest": 0, "count": 0}
        return {
            "earliest": _int(battles[-1].get("dtEventTime")),
            "latest": _int(battles[0].get("dtEventTime")),
            "count": len(battles),
        }

    def get_watermark(self, camp_id) -> int:
        entry = self._load_all().get(str(camp_id or "")) or {}
        return _int(entry.get("oldestFetched"))

    # -------------------- 写入 --------------------

    def set_watermark(self, camp_id, reached_sec: int) -> None:
        key = str(camp_id or "")
        reached = _int(reached_sec)
        if not key or reached <= 0:
            return
        all_ = self._load_all()
        entry = all_.get(key) or {"battles": []}
        current = _int(entry.get("oldestFetched"))
        nxt = min(current, reached) if current > 0 else reached
        if nxt == current:
            return
        entry["oldestFetched"] = max(nxt, _cutoff_sec())
        all_[key] = entry
        self._save_all(all_)

    def archive_battles(self, camp_id, items: list) -> int:
        """按 gameSeq 幂等合并 (轮询反复拉同一批), 返回本次新增场数"""
        key = str(camp_id or "")
        if not key or not isinstance(items, list) or not items:
            return 0
        all_ = self._load_all()
        existed = (all_.get(key) or {}).get("battles") or []

        by_seq = {}
        for item in existed:
            seq = str(item.get("gameSeq") or "")
            if seq:
                by_seq[seq] = item
        before = len(by_seq)
        for raw in items:
            seq = str(raw.get("gameSeq") or "")
            if not seq or seq in by_seq:
                continue
            slim = {}
            for field in KEEP_FIELDS:
                value = raw.get(field)
                if value is None:
                    continue
                slim[field] = _int(value) if field in _INT_FIELDS else str(value)
            by_seq[seq] = slim
        added = len(by_seq) - before
        if not added:
            return 0

        cutoff = _cutoff_sec()
        battles = [x for x in by_seq.values() if _int(x.get("dtEventTime")) >= cutoff]
        battles.sort(key=lambda x: -_int(x.get("dtEventTime")))
        prev_mark = _int((all_.get(key) or {}).get("oldestFetched"))
        entry = {"updatedAt": int(time.time() * 1000), "battles": battles}
        if prev_mark > 0:
            entry["oldestFetched"] = max(prev_mark, cutoff)
        all_[key] = entry
        self._save_all(all_)
        return added

    # -------------------- 采集 --------------------

    async def collect_battles(self, api, camp_id, requester_qq: str, from_sec: int,
                              max_pages: int = 12, to_sec: int = 0) -> dict:
        """取 [from_sec, to_sec] 的战绩: 实拉第一页保证库是新的, 不够才翻页补。"""
        key = str(camp_id or "")
        from_sec = _int(from_sec)
        to_sec = _int(to_sec)
        # 落库前库里最新一场: 第一页要一直翻到接上它, 中间才没有空洞
        head_before = _int((self.load_archive(key) or [{}])[0].get("dtEventTime"))

        last_time = 0
        reached = 0
        fetched = 0
        truncated = False

        for page in range(max_pages):
            try:
                res = await api.get_more_battle_list(key, requester_qq=requester_qq,
                                                     last_time=last_time)
            except Exception:
                break
            if not isinstance(res, dict) or _int(res.get("returnCode")) != 0:
                break
            data = res.get("data") or {}
            items = data.get("list") or []
            if not items:
                break

            fetched += 1
            self.archive_battles(key, items)
            reached = _int(items[-1].get("dtEventTime"))
            if reached <= from_sec:
                break
            # 水位说更早的翻过了, 且这页接上了原库的头 —— 没有空洞, 收工
            watermark = self.get_watermark(key)
            if watermark > 0 and watermark <= from_sec and head_before > 0 and reached <= head_before:
                reached = watermark
                break
            if not data.get("hasMore") or not data.get("lastTime"):
                # 接口说没有更多历史, 水位直接推到区间起点, 免得下次白翻
                reached = from_sec
                break
            last_time = _int(data.get("lastTime")) or 0
            if page == max_pages - 1:
                truncated = True

        if reached > 0:
            self.set_watermark(key, reached)

        final_mark = self.get_watermark(key)
        in_range = [x for x in self.load_archive(key)
                    if _int(x.get("dtEventTime")) >= from_sec
                    and (to_sec <= 0 or _int(x.get("dtEventTime")) <= to_sec)]
        return {
            "battles": in_range,
            "covered_from": max(final_mark, from_sec) if final_mark > 0 else from_sec,
            "truncated": truncated,
            "fetched": fetched,
        }


_CSV_COLUMNS = ("对局时间", "模式", "结果", "英雄", "击杀", "死亡", "助攻", "KDA",
                "评分", "MVP", "时长(分)", "段位", "星数", "巅峰分变化", "评价")


