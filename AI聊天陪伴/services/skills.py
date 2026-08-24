"""安全发现本地技能文件，并供大语言模型按需加载。"""

from __future__ import annotations

import os
import re

SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "读取一个已启用的本地技能说明。仅在当前任务与技能描述匹配时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "可用技能目录中给出的技能 ID",
                },
            },
            "required": ["skill_id"],
        },
    },
}

_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills"
)


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    values = {}
    for line in text[3:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"name", "description"}:
            values[key.strip()] = value.strip().strip("\"'")
    return values


def discover() -> list[dict[str, str]]:
    if not os.path.isdir(_ROOT):
        return []
    result = []
    for skill_id in sorted(os.listdir(_ROOT)):
        if not _ID_RE.fullmatch(skill_id):
            continue
        path = os.path.join(_ROOT, skill_id, "SKILL.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as file:
                text = file.read(32768)
        except OSError:
            continue
        meta = _frontmatter(text)
        result.append(
            {
                "id": skill_id,
                "name": meta.get("name") or skill_id,
                "description": meta.get("description") or "本地 LLM 技能",
            }
        )
    return result


def enabled_catalog(enabled_ids: list[str]) -> list[dict[str, str]]:
    allowed = {str(item) for item in enabled_ids}
    return [item for item in discover() if item["id"] in allowed]


def catalog_prompt(enabled_ids: list[str]) -> str:
    catalog = enabled_catalog(enabled_ids)
    if not catalog:
        return ""
    lines = ["可用 Skills：需要时先调用 load_skill 读取说明，不要猜测技能内容。"]
    lines.extend(f"- {item['id']}: {item['description']}" for item in catalog)
    return "\n".join(lines)


def load_skill(skill_id: str, enabled_ids: list[str]) -> dict:
    skill_id = str(skill_id or "").strip()
    if not _ID_RE.fullmatch(skill_id) or skill_id not in {
        str(item) for item in enabled_ids
    }:
        return {"ok": False, "error": "技能不存在或未启用"}
    path = os.path.abspath(os.path.join(_ROOT, skill_id, "SKILL.md"))
    root = os.path.abspath(_ROOT) + os.sep
    if not path.startswith(root) or not os.path.isfile(path):
        return {"ok": False, "error": "技能文件不存在"}
    try:
        with open(path, encoding="utf-8") as file:
            content = file.read(20000)
    except OSError as error:
        return {"ok": False, "error": str(error)}
    return {"ok": True, "skill_id": skill_id, "content": content}


def create_skill(skill_id: str, name: str, description: str, content: str) -> dict:
    """创建本地 Markdown 技能，同时阻止路径穿越。"""
    skill_id = str(skill_id or "").strip()
    name = str(name or "").strip()
    description = str(description or "").strip()
    content = str(content or "").strip()
    if not _ID_RE.fullmatch(skill_id):
        raise ValueError("Skill ID 只能包含字母、数字、下划线和短横线")
    if not name or "\n" in name or "\r" in name:
        raise ValueError("Skill 名称不能为空且不能换行")
    if not description or "\n" in description or "\r" in description:
        raise ValueError("Skill 描述不能为空且不能换行")
    if not content:
        raise ValueError("Skill 内容不能为空")
    if len(name) > 120 or len(description) > 500 or len(content) > 20000:
        raise ValueError("Skill 内容过长")
    root = os.path.abspath(_ROOT)
    path = os.path.abspath(os.path.join(root, skill_id))
    if os.path.dirname(path) != root or os.path.exists(path):
        raise ValueError("Skill 已存在或路径无效")
    os.makedirs(path, exist_ok=False)
    target = os.path.join(path, "SKILL.md")
    temporary = target + ".tmp"
    text = f"---\nname: {name}\ndescription: {description}\n---\n\n{content}\n"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        os.replace(temporary, target)
    except Exception:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
            os.rmdir(path)
        except OSError:
            pass
        raise
    return {"id": skill_id, "name": name, "description": description, "enabled": False}
