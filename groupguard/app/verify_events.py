"""入群策略与验证事件。"""

from core.plugin.decorators import handler

from ..mod import db, state
from ..mod.replies import api_error
from ..mod.storage.audit import record_received, record_result
from ..mod.utils import api_pair
from ..mod.verify import handle_verify_answer, send_verify


def _join_request_details(event, mode, request_id):
    qa_list = getattr(event, 'review_qa_list', None)
    qa_list = qa_list if isinstance(qa_list, list) else []
    normalized_qa = []
    for item in qa_list[:20]:
        if not isinstance(item, dict):
            continue
        normalized_qa.append({
            'question': str(item.get('question') or '')[:1000],
            'answer': str(item.get('answer') or '')[:2000],
        })
    return {
        'mode': mode,
        'request_id': request_id,
        'operator': 'join_policy',
        'username': str(getattr(event, 'username', '') or ''),
        'apply_at': str(getattr(event, 'apply_at', '') or ''),
        'apply_source': str(getattr(event, 'apply_source', '') or ''),
        'verify_method': str(getattr(event, 'verify_method', '') or ''),
        'review_qa_list': normalized_qa,
    }


@handler(r'', name='入群申请策略', desc='按当前群策略自动审批入群申请',
         event_types=['GROUP_JOIN_REQUEST'])
async def on_join_request(event, match):
    group_id = str(event.group_id or '')
    member_id = str(event.user_id or '')
    request_id = str(event.join_request_id or '')
    if not group_id or not member_id:
        return
    config = db.get_group_cfg(group_id)
    policy = config.get('join_policy') or {}
    mode = policy.get('mode', 'manual')
    if not config['enabled'] or mode == 'manual':
        return

    decline = mode in ('auto_decline', 'auto_blacklist')
    blacklisted = mode == 'auto_blacklist'
    action = (
        'blacklist_join'
        if blacklisted
        else ('decline_join' if decline else 'approve_join')
    )
    if not request_id:
        details = _join_request_details(event, mode, '')
        details['reason'] = 'request_id_missing'
        record_received(event, action, source='automatic', details=details)
        record_result(
            event, action, False, target_id=member_id,
            details={**details, 'error': 'join_request_id_missing'},
            source='automatic',
        )
        return
    reason = str(policy.get('reject_reason') or '不符合入群要求')
    details = _join_request_details(event, mode, request_id)
    if decline:
        details['reason'] = reason
    record_received(event, action, source='automatic', details=details)
    success, response = await api_pair(event.sender.review_group_join_request(
        group_id,
        member_id,
        'decline' if decline else 'approve',
        join_request_id=request_id,
        reject_reason=reason if decline else '',
        add_to_member_blacklist=blacklisted,
    ))
    success = bool(success)
    error = '' if success else api_error(response)
    result_details = {**details, 'error': error}
    record_result(
        event, action, success, affected_count=1 if success else 0,
        target_id=member_id, details=result_details, source='automatic',
    )


@handler(r'', name='入群验证触发', desc='新成员入群时发送验证题', event_types=['GROUP_MEMBER_ADD'])
async def on_member_add(event, match):
    gid = event.group_id
    if not gid:
        return
    gc = db.get_group_cfg(gid)
    if not gc['enabled'] or not gc['features']['join_verify']:
        return
    member_id = event.user_id
    if not member_id:
        return
    state.clear_member(gid, member_id)
    state.unverified.setdefault(gid, set()).add(member_id)
    await send_verify(event, gid, member_id, retry_count=0)


@handler(r'', name='入群验证状态清理', desc='成员离群时释放验证状态', event_types=['GROUP_MEMBER_REMOVE'])
async def on_member_remove(event, match):
    member_id = event.user_id
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
    member_id = event.user_id
    if not member_id:
        return
    gc = db.get_group_cfg(gid)
    if not gc['enabled'] or not gc['features']['join_verify']:
        state.clear_member(gid, member_id)
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
