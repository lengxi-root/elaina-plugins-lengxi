"""All user-visible GroupGuard replies and reply delivery live here."""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime

from .reply_templates import get_reply_template
from .storage.audit import record_audit

_UNSET = object()
_DIRECT_SEND_OPTION_KEYS = {
    'media', 'msg_type', 'auto_delete_time', 'skip_suffix',
    'message_reference', 'message_reference_id', 'reference_message_id',
}
_TUPLE_OPTION_KEYS = _DIRECT_SEND_OPTION_KEYS | {
    'small_buttons', 'button_font_size', 'button_style', 'send_kwargs',
}


@dataclass(slots=True)
class ReplyMessage:
    """Rendered reply content plus framework-native delivery options."""

    content: str
    buttons: object = None
    prompt_buttons: object = None
    button_font_size: str | None = None
    button_style: dict | None = None
    send_kwargs: dict = field(default_factory=dict)

    def delivery_kwargs(self):
        kwargs = dict(self.send_kwargs)
        if self.buttons:
            kwargs['buttons'] = self.buttons
        if self.prompt_buttons:
            kwargs['prompt_buttons'] = self.prompt_buttons
        if self.button_font_size:
            kwargs['button_font_size'] = self.button_font_size
        if self.button_style:
            kwargs['button_style'] = self.button_style
        return kwargs


def reply_message(
    content,
    buttons=None,
    *,
    prompt_buttons=None,
    small_buttons=False,
    button_font_size=None,
    button_style=None,
    **send_kwargs,
):
    """Build a reply template with buttons and framework delivery options."""
    return ReplyMessage(
        content=str(content or ''),
        buttons=buttons,
        prompt_buttons=prompt_buttons,
        button_font_size=button_font_size or ('small' if small_buttons else None),
        button_style=button_style,
        send_kwargs=send_kwargs,
    )


def _normalize_reply(value):
    """Normalize supported template shapes into one sendable reply object."""
    if isinstance(value, ReplyMessage):
        return value
    if isinstance(value, str):
        return ReplyMessage(value)
    if isinstance(value, dict):
        content = value.get('content', value.get('text', value.get('markdown', '')))
        raw_send_kwargs = value.get('send_kwargs') or {}
        if not isinstance(raw_send_kwargs, dict):
            raise TypeError('reply send_kwargs must be a dict')
        send_kwargs = dict(raw_send_kwargs)
        send_kwargs.update({
            key: value[key] for key in _DIRECT_SEND_OPTION_KEYS if key in value
        })
        return reply_message(
            content,
            value.get('buttons'),
            prompt_buttons=value.get('prompt_buttons'),
            small_buttons=bool(value.get('small_buttons')),
            button_font_size=value.get('button_font_size'),
            button_style=value.get('button_style'),
            **send_kwargs,
        )
    if isinstance(value, (tuple, list)):
        if not 1 <= len(value) <= 4:
            raise TypeError('reply tuple must contain 1 to 4 items')
        content = value[0]
        buttons = value[1] if len(value) > 1 else None
        third = value[2] if len(value) > 2 else None
        if (len(value) == 3 and isinstance(third, dict)
                and _TUPLE_OPTION_KEYS.intersection(third)):
            prompt_buttons, options = None, third
        else:
            prompt_buttons = third
            options = value[3] if len(value) > 3 else {}
        if not isinstance(options, dict):
            raise TypeError('reply tuple delivery options must be a dict')
        options = dict(options)
        nested_send_kwargs = options.pop('send_kwargs', None)
        if nested_send_kwargs:
            if not isinstance(nested_send_kwargs, dict):
                raise TypeError('reply send_kwargs must be a dict')
            options = {**nested_send_kwargs, **options}
        return reply_message(
            content,
            buttons,
            prompt_buttons=prompt_buttons,
            **options,
        )
    raise TypeError(f'unsupported groupguard reply type: {type(value).__name__}')


def _apply_delivery_overrides(
    message,
    *,
    buttons=_UNSET,
    prompt_buttons=_UNSET,
    small_buttons=_UNSET,
    button_font_size=_UNSET,
    button_style=_UNSET,
    send_kwargs=None,
):
    if buttons is not _UNSET:
        message.buttons = buttons
    if prompt_buttons is not _UNSET:
        message.prompt_buttons = prompt_buttons
    if small_buttons is not _UNSET:
        message.button_font_size = 'small' if small_buttons else None
    if button_font_size is not _UNSET:
        message.button_font_size = button_font_size
    if button_style is not _UNSET:
        message.button_style = button_style
    if send_kwargs:
        message.send_kwargs.update(send_kwargs)
    return message


def _command(text, command_text, enter=True):
    del enter
    return f'<qqbot-cmd-input text="{command_text}" show="{text}" />'


def _row(*items):
    return ' | '.join(items)


def _button(text, data, enter=True, **extra):
    item = {'text': text, 'data': data, 'type': 2, 'tips': '当前客户端不支持'}
    if enter:
        item['enter'] = True
    item.update(extra)
    return item


def _toggle(label, enabled, command_prefix):
    command_text = f"{command_prefix}{'关闭' if enabled else '开启'}"
    return ('✅' if enabled else '❌') + _command(label, command_text)


def format_remaining(expire):
    if expire == 0:
        return '永久'
    remain = expire - time.time()
    if remain <= 0:
        return '已过期'
    if remain >= 86400:
        return f'{remain / 86400:.1f}天'
    if remain >= 3600:
        return f'{remain / 3600:.1f}小时'
    if remain >= 60:
        return f'{remain / 60:.0f}分钟'
    return f'{remain:.0f}秒'


def format_spam_punishment(minutes):
    if minutes == 0:
        return '不处罚 (只撤回刷屏消息)'
    if minutes < 0:
        return '永久发言撤回'
    return f'发言撤回 {minutes} 分钟'


def api_error(data):
    if isinstance(data, dict):
        return str(data.get('message') or data.get('msg')
                   or json.dumps(data, ensure_ascii=False))
    return str(data or '未知错误')


def command(text, command_text, enter=True):
    return _command(text, command_text, enter)


def row(*items):
    return _row(*items)


def button(text, data, enter=True, **extra):
    return _button(text, data, enter, **extra)


def toggle(label, enabled, command_prefix):
    return _toggle(label, enabled, command_prefix)


def category_markdown(category, group_config):
    return _normalize_reply(_build(
        'category_panel',
        {'category': category, 'group_config': group_config},
    )).content


def join_review_buttons(requests):
    return _normalize_reply(_build('join_requests', {'requests': requests})).buttons


class _TemplateData(dict):
    def __missing__(self, key):
        return '{' + key + '}'


def _render_value(value, variables):
    if isinstance(value, str):
        try:
            return value.format_map(_TemplateData(variables))
        except (ValueError, TypeError):
            return value
    if isinstance(value, list):
        return [_render_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render_value(item, variables) for key, item in value.items()}
    return value


def _base_variables(data):
    variables = {
        'count': 0,
        'failed': 0,
        'minutes': 0,
        'limit': 0,
        'target_id': '',
        'word': '',
        'px': '',
        'url': '',
        'names': '',
        'punish': '',
        'remaining': '',
        'error': '未知错误',
    }
    variables.update(data)
    return variables


def _dynamic_template(key, template, data):
    variables = _base_variables(data)
    if key == 'main_panel':
        config = data['group_config']
        features = config['features']
        spam_enabled = bool(data['spam_config']['enabled'])
        states = {
            'group': bool(config['enabled']),
            'notify': bool(config['notify']),
            **{name: bool(features[name]) for name in (
                'join_verify', 'block_links', 'block_cards',
                'block_forward', 'forbidden_words',
            )},
            'spam': spam_enabled,
        }
        for name, enabled in states.items():
            variables[f'{name}_mark'] = '✅' if enabled else '❌'
            prefix = '群管' if name == 'group' else name
            command_prefixes = {
                'notify': '撤回提醒', 'join_verify': '入群验证',
                'block_links': '禁发链接', 'block_cards': '禁发卡片',
                'block_forward': '禁止转发', 'forbidden_words': '违禁词',
            }
            prefix = command_prefixes.get(name, prefix)
            if name == 'spam':
                variables[f'{name}_command'] = ('关闭' if enabled else '开启') + '刷屏检测'
            else:
                variables[f'{name}_command'] = prefix + ('关闭' if enabled else '开启')
    elif key == 'category_panel':
        category_keys = {
            '用户处理': 'category_user', '群管理': 'category_group',
            '违禁词': 'category_forbidden', '消息过滤': 'category_filter',
            '刷屏检测': 'category_spam',
        }
        key = category_keys.get(data['category'], 'category_spam')
        template = get_reply_template(key)
        features = data['group_config']['features']
        for name in ('join_verify', 'block_links', 'block_cards', 'block_forward'):
            enabled = bool(features[name])
            variables[f'{name}_switch_state'] = '关闭' if enabled else '开启'
            variables[f'{name}_switch_short'] = '关' if enabled else '开'
        variables['forbidden_switch_state'] = (
            '关闭' if features['forbidden_words'] else '开启'
        )
    elif key == 'join_requests':
        requests = data['requests']
        item_template = template.get('item_content', '')
        button_template = template.get('buttons') or []
        rows, buttons = [], []
        for index, item in enumerate(requests, 1):
            verify_info = item.get('verify_info') or {}
            item_vars = {
                'index': index,
                'username': item.get('username') or template.get('unknown_user_text', '未知用户'),
                'member_id': str(item.get('member_openid') or ''),
                'request_id': str(item.get('join_request_id') or ''),
                'verify_message': verify_info.get('verify_message') or template.get('empty_text', '无'),
            }
            rows.append(_render_value(item_template, item_vars))
            if (template.get('button_mode') == 'join_requests'
                    and item_vars['member_id'] and item_vars['request_id']):
                buttons.extend(_render_value(button_template, item_vars))
        variables.update(
            request_count=len(requests), request_rows='\n'.join(rows),
            next_page=_render_value(template.get('next_page_content', ''), {
                'next_cursor': data.get('next_cursor', ''),
            }) if data.get('next_cursor') else '',
        )
        if template.get('button_mode') == 'join_requests':
            template = {**template, 'buttons': buttons}
    elif key == 'management_stats':
        variables.update(data['stats'])
    elif key == 'audit_list':
        rows = data['rows']
        if not rows:
            key, template = 'audit_list_empty', get_reply_template('audit_list_empty')
        else:
            labels = template.get('action_labels') or {}
            item_template = template.get('item_content', '')
            rendered_rows = []
            for item in rows:
                rendered_rows.append(_render_value(item_template, {
                    'time': datetime.fromtimestamp(item['time']).strftime('%m-%d %H:%M:%S'),
                    'action_label': labels.get(item['action'], item['action']),
                    'state': template.get('success_text', '成功') if item['success'] else template.get('failure_text', '失败'),
                    'affected_count': item['affected_count'],
                    'trace_short': item['trace_id'][:8],
                }))
            variables.update(audit_count=len(rows), audit_rows='\n'.join(rendered_rows))
    elif key == 'mute_list':
        setting = data['setting']
        members = setting.get('members') or []
        labels = template.get('mode_labels') or {}
        mode = (setting.get('global_rule') or {}).get('mode')
        member_rows = []
        for item in members[:10]:
            member_rows.append(_render_value(template.get('item_content', ''), {
                'name': item.get('username') or template.get('unknown_user_text', '未知用户'),
                'member_id': item.get('member_openid') or '',
                'expire_at': item.get('mute_expire_at') or template.get('unknown_time_text', '未知时间'),
            }))
        overflow_count = max(0, len(members) - 10)
        variables.update(
            global_mode=labels.get(mode, labels.get('unknown', '未知')),
            member_count=len(members), member_rows=''.join(member_rows),
            overflow=_render_value(template.get('overflow_content', ''), {
                'overflow_count': overflow_count,
            }) if overflow_count else '',
        )
    elif key == 'verify_question':
        if template.get('button_mode') == 'verify_options':
            prototype = ((template.get('buttons') or [[]])[0] or [{}])[0]
            buttons = [[
                _render_value(prototype, {
                    **variables, 'option': option, 'option_index': index,
                })
                for index, option in enumerate(data['options'])
            ]]
            template = {**template, 'buttons': buttons}
    elif key == 'group_state':
        state = data.get('state') or {}
        yes, no = template.get('true_text', '是'), template.get('false_text', '否')
        variables.update({
            name: yes if state.get(name) else no
            for name in ('is_admin', 'is_full_access', 'allow_proactive_msg')
        })
    elif key == 'forbidden_list_text':
        words = data.get('words') or []
        variables.update(
            word_count=len(words),
            word_rows='\n'.join(
                _render_value(template.get('item_content', ''), {
                    'index': index, 'word': word,
                }) for index, word in enumerate(words, 1)
            ),
        )
    elif key == 'punish_list':
        entries = data.get('entries') or []
        variables.update(
            entry_count=len(entries),
            entry_rows='\n'.join(
                _render_value(template.get('item_content', ''), item)
                for item in entries
            ),
        )
    elif key == 'recall_done':
        variables['scope_text'] = template.get('scope_text', '') if data.get('user_scope') else ''
        variables['failed_text'] = _render_value(
            template.get('failed_content', ''), variables
        ) if data.get('failed') else ''
    elif key == 'join_declined':
        variables['decision_text'] = (
            template.get('blacklisted_text', '已拒绝并拉黑该申请人') if data.get('blacklisted')
            else template.get('decision_text', '已拒绝该入群申请')
        )
    elif key == 'verify_wrong_muted':
        variables['retry_text'] = _render_value(
            template.get('retry_content', ''), variables
        ) if data.get('retry_count', 0) >= 3 else ''
    return key, template, variables


def _build(key, data):
    template = get_reply_template(key if key != 'category_panel' else 'category_spam')
    key, template, variables = _dynamic_template(key, template, data)
    message = _normalize_reply({
        'content': _render_value(template.get('content', ''), variables),
        'buttons': _render_value(template.get('buttons'), variables),
        'prompt_buttons': _render_value(template.get('prompt_buttons'), variables),
        'button_font_size': template.get('button_font_size') or None,
        'button_style': _render_value(template.get('button_style'), variables),
        'send_kwargs': _render_value(template.get('send_kwargs'), variables),
    })
    message.send_kwargs['_template_at_user'] = template.get('at_user')
    if message.buttons is None and data.get('buttons'):
        message.buttons = data['buttons']
    return message


async def respond(
    event,
    key,
    *,
    at_user=_UNSET,
    audit_action=None,
    buttons=_UNSET,
    prompt_buttons=_UNSET,
    small_buttons=_UNSET,
    button_font_size=_UNSET,
    button_style=_UNSET,
    send_kwargs=None,
    **data,
):
    """Render, send and audit every GroupGuard user-visible reply."""
    from .storage.audit import current_action, record_received
    if not current_action(event):
        record_received(
            event,
            current_action(event, audit_action or key),
            source=getattr(event, '_groupguard_trace_source', 'command'),
            details={
                'event_type': str(getattr(event, 'event_type', '') or ''),
                'content_length': len(str(getattr(event, 'content', '') or '')),
            },
        )
    message = _apply_delivery_overrides(
        _normalize_reply(_build(key, data)),
        buttons=buttons,
        prompt_buttons=prompt_buttons,
        small_buttons=small_buttons,
        button_font_size=button_font_size,
        button_style=button_style,
        send_kwargs=send_kwargs,
    )
    template_at_user = message.send_kwargs.pop('_template_at_user', None)
    should_at_user = bool(
        template_at_user if at_user is _UNSET else at_user
    )
    if should_at_user:
        message.content = f'<@{event.user_id}> {message.content}'
    kwargs = message.delivery_kwargs()
    from .storage.audit import current_action
    action = audit_action or current_action(event, f'reply:{key}')
    try:
        result = await event.reply(message.content, **kwargs)
    except Exception as exc:
        record_audit(event, action, 'reply', success=False,
                     details={'reply_key': key, 'error': type(exc).__name__})
        raise
    record_audit(event, action, 'reply', success=True,
                 details={
                     'reply_key': key,
                     'length': len(message.content),
                     'buttons': bool(message.buttons),
                     'prompt_buttons': bool(message.prompt_buttons),
                     'button_font_size': message.button_font_size or '',
                 })
    return result
