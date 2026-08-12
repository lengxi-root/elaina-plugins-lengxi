"""入群验证事件 — 新成员入群出题 + 按钮回调判题"""

from core.plugin.decorators import handler

from ..mod import db, state
from ..mod.verify import handle_verify_answer, send_verify


@handler(r'', name='入群验证触发', desc='新成员入群时发送验证题', event_types=['GROUP_MEMBER_ADD'])
async def on_member_add(event, match):
    gid = event.group_id
    if not gid:
        return
    gc = db.get_group_cfg(gid)
    if not gc['enabled'] or not gc['features']['join_verify']:
        return
    member_id = event.user_id or event.member_openid
    if not member_id:
        return
    state.clear_member(gid, member_id)
    state.unverified.setdefault(gid, set()).add(member_id)
    await send_verify(event, gid, member_id, retry_count=0)


@handler(r'', name='入群验证状态清理', desc='成员离群时释放验证状态', event_types=['GROUP_MEMBER_REMOVE'])
async def on_member_remove(event, match):
    member_id = event.user_id or event.member_openid
    if event.group_id and member_id:
        state.clear_member(event.group_id, member_id)


@handler(r'^verify\|', name='验证答案回调', desc='处理入群验证按钮点击', event_types=['INTERACTION_CREATE'])
async def on_verify_click(event, match):
    event.set_callback_code(0)
    parts = (event.content or '').split('|')
    if len(parts) not in (3, 4, 5):
        return
    gid = event.group_id
    if not gid or parts[1] != gid:
        return
    member_id = event.user_id or event.member_openid
    if not member_id:
        return
    try:
        chosen = int(parts[-1])
    except (TypeError, ValueError):
        return
    if len(parts) == 5:
        if parts[2] != str(member_id):
            return
        verify_id = parts[3]
    else:
        verify_id = parts[2] if len(parts) == 4 else None
    await handle_verify_answer(event, gid, member_id, chosen, verify_id=verify_id)
