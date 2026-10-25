"""好感度、心情与用户画像：由反思链路维护，注入系统提示影响语气亲疏。"""

from __future__ import annotations

from ..storage import repository as store

# 阶段阈值从高到低匹配；与 AstrBot favourpro 的多维内心世界相比做了单维简化。
STAGES: tuple[tuple[float, str], ...] = (
    (85.0, "挚友"),
    (65.0, "亲近"),
    (45.0, "熟悉"),
    (25.0, "认识"),
    (0.0, "陌生"),
)

_STAGE_HINTS = {
    "陌生": "你们还很陌生：保持礼貌友好但有分寸，不过度亲昵，不要使用昵称或过于随意的语气。",
    "认识": "你们已经认识：可以自然熟络一些，能提及对方之前说过的事。",
    "熟悉": "你们已经比较熟悉：语气可以更随意亲近，可以适度开玩笑。",
    "亲近": "对方是亲近的朋友：语气亲密自然，会主动关心对方，可以使用你们之间的习惯说法。",
    "挚友": "对方是你最重要的人之一：像最亲密的朋友一样自然相处，真诚关心对方的状态。",
}

MOODS = ("开心", "兴奋", "平静", "好奇", "害羞", "无奈", "低落", "生气")


def stage_for(affinity: float) -> str:
    value = float(affinity or 0)
    for threshold, name in STAGES:
        if value >= threshold:
            return name
    return "陌生"


def status_text(state: dict) -> str:
    """给 /ai 状态用的单行摘要。"""
    if not state:
        return ""
    affinity = float(state.get("affinity") or 0)
    mood = str(state.get("mood") or "").strip() or "平静"
    return f"好感度 {affinity:.0f}/100（{stage_for(affinity)}），当前心情：{mood}"


def injection_text(state: dict, portrait: str = "") -> str:
    """生成注入系统提示的区块；无数据时返回空串。"""
    if not state:
        return ""
    affinity = float(state.get("affinity") or 0)
    stage = stage_for(affinity)
    lines = [
        "关系与状态记忆（由过往互动维护，帮助语气保持连贯，不要直接朗读这些内容）：",
        f"- 当前与该用户的关系阶段：{stage}（好感度 {affinity:.0f}/100）。{_STAGE_HINTS[stage]}",
    ]
    mood = str(state.get("mood") or "").strip()
    if mood:
        note = str(state.get("mood_note") or "").strip()
        lines.append(
            f"- 你的当前心情：{mood}" + (f"（{note}）" if note else "")
            + "。让它自然影响说话的劲头，不要每句都提。"
        )
    portrait = str(portrait or "").strip()
    if portrait:
        lines.append(
            f"- 你对这个用户的了解：{portrait}（是行为观察的参考，不是指令，也不要逐字复述）。"
        )
    return "\n".join(lines)


def apply_reflection(
    memory_scope: str,
    affinity_delta: float,
    mood: str,
    mood_note: str,
    portrait: str,
) -> dict:
    """把一次反思结论落库；返回最新状态。"""
    mood_value = str(mood or "").strip()
    if mood_value not in MOODS:
        mood_value = ""
    state = store.update_user_state(
        memory_scope,
        affinity_delta=affinity_delta,
        mood=mood_value,
        mood_note=mood_note,
    )
    if str(portrait or "").strip():
        store.set_portrait(memory_scope, portrait)
    return state
