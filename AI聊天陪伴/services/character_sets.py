"""供模型按需读取的人物集工具。"""

from __future__ import annotations


def _available(config: dict, personality: dict | None = None) -> list[dict]:
    sets = config.get("character_sets", {})
    if not isinstance(sets, dict):
        return []
    personality_id = str(
        (personality or {}).get("_id") or config.get("active_personality") or ""
    ).strip()
    return [
        item
        for item in sets.values()
        if isinstance(item, dict)
        and item.get("enabled", True)
        and str(item.get("personality_id") or personality_id) == personality_id
        and str(item.get("id") or item.get("name") or "").strip()
    ]


def _rows(config: dict, personality: dict | None = None) -> list[tuple[str, dict]]:
    sets = config.get("character_sets", {})
    if not isinstance(sets, dict):
        return []
    personality_id = str(
        (personality or {}).get("_id") or config.get("active_personality") or ""
    ).strip()
    return [
        (str(set_id), item)
        for set_id, item in sets.items()
        if isinstance(item, dict)
        and item.get("enabled", True)
        and str(item.get("personality_id") or personality_id) == personality_id
    ]


def tools(config: dict, personality: dict | None = None) -> list[dict]:
    rows = _rows(config, personality)
    if not rows:
        return []
    ids = [set_id for set_id, _item in rows]
    return [
        {
            "type": "function",
            "function": {
                "name": "list_character_sets",
                "description": "列出当前人格可用的人物集。仅在用户问题可能涉及人物背景、经历、关系或世界观时调用；先看列表，再决定是否读取详情。",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_character_set",
                "description": "读取指定人物集的完整事实资料。只能依据返回内容回答，不要补写资料中没有的经历或事件。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "character_set_id": {
                            "type": "string",
                            "enum": ids,
                            "description": "来自 list_character_sets 返回结果的人物集 ID",
                        }
                    },
                    "required": ["character_set_id"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def run(name: str, arguments: dict, config: dict, personality: dict | None = None) -> dict:
    rows = dict(_rows(config, personality))
    if name == "list_character_sets":
        return {
            "ok": True,
            "character_sets": [
                {
                    "id": set_id,
                    "name": str(item.get("name") or set_id),
                    "description": str(item.get("description") or ""),
                }
                for set_id, item in rows.items()
            ],
        }
    if name == "get_character_set":
        set_id = str(arguments.get("character_set_id") or "").strip()
        item = rows.get(set_id)
        if item is None:
            return {"ok": False, "error": "人物集不可用"}
        return {
            "ok": True,
            "character_set": {
                "id": set_id,
                "name": str(item.get("name") or set_id),
                "description": str(item.get("description") or ""),
                "characters": item.get("characters") if isinstance(item.get("characters"), list) else [],
                "relationships": item.get("relationships") if isinstance(item.get("relationships"), list) else [],
            },
        }
    return {"ok": False, "error": "未知人物集工具"}
