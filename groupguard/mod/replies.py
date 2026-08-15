"""All user-visible GroupGuard replies and reply delivery live here."""

import json
import string
import time
from dataclasses import dataclass
from datetime import datetime

from .reply_templates import _get_cached_template
from .storage.audit import record_audit

_UNSET = object()


@dataclass(slots=True)
class ReplyMessage:
    """Rendered reply content plus framework-native delivery options."""

    content: str
    buttons: object = None
    small_buttons: bool = False
    msg_type: int | None = None
    at_user: bool = True

    def delivery_kwargs(self):
        kwargs = {}
        if self.buttons:
            kwargs['buttons'] = self.buttons
        if self.small_buttons:
            kwargs['button_font_size'] = 'small'
        if self.msg_type is not None:
            kwargs['msg_type'] = self.msg_type
        return kwargs


def _apply_delivery_overrides(
    message,
    *,
    buttons=_UNSET,
    small_buttons=_UNSET,
):
    if buttons is not _UNSET:
        message.buttons = buttons
    if small_buttons is not _UNSET:
        message.small_buttons = bool(small_buttons)
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


def format_spam_punishment(action, minutes=10):
    if action == 'mute':
        return f'禁言 {minutes} 分钟'
    if action == 'recall_mute':
        return f'撤回并禁言 {minutes} 分钟'
    return '仅撤回消息'


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
    return _build('category_panel', {
        'category': category, 'group_config': group_config,
    }).content


def join_review_buttons(requests):
    return _build('join_requests', {'requests': requests}).buttons


class _TemplateData(dict):
    def __missing__(self, key):
        return '{' + key + '}'


class _SafeFormatter(string.Formatter):
    """Allow only simple names; block attribute/index traversal in templates."""

    def get_field(self, field_name, args, kwargs):
        if not field_name.isascii() or not field_name.isidentifier():
            raise ValueError('template field must be a simple identifier')
        return super().get_field(field_name, args, kwargs)


_SAFE_FORMATTER = _SafeFormatter()
_FRAMEWORK_EVENT_FIELDS = (
    'content', 'raw_content', 'user_id', 'raw_user_id', 'group_id',
    'channel_id', 'guild_id', 'username', 'message_id', 'message_type',
    'event_id', 'event_type', 'timestamp', 'appid', 'image_url',
)
_FRAMEWORK_VARIABLE_ALIASES = {
    'userid': 'user_id',
    'rawuserid': 'raw_user_id',
    'groupid': 'group_id',
    'channelid': 'channel_id',
    'guildid': 'guild_id',
    'messageid': 'message_id',
    'eventid': 'event_id',
    'nickname': 'username',
    'botid': 'appid',
    'botname': 'bot_name',
    'botqq': 'bot_qq',
    'selfid': 'bot_qq',
}


def _render_value(value, variables):
    if isinstance(value, str):
        try:
            return _SAFE_FORMATTER.vformat(value, (), _TemplateData(variables))
        except (ValueError, TypeError):
            return value
    if isinstance(value, list):
        return [_render_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render_value(item, variables) for key, item in value.items()}
    return value


def _button_rows(buttons, columns=3):
    """Convert the editable flat button list into framework keyboard rows."""
    if not buttons:
        return None
    if all(isinstance(row, list) for row in buttons):
        return buttons
    return [buttons[index:index + columns] for index in range(0, len(buttons), columns)]


def _framework_variables(event):
    if event is None:
        return {}
    variables = {
        name: getattr(event, name, '') or ''
        for name in _FRAMEWORK_EVENT_FIELDS
    }
    sender = getattr(event, 'sender', None)
    variables.update(
        bot_name=getattr(sender, '_bot_name', '') or '',
        bot_qq=getattr(sender, '_bot_qq', '') or '',
    )
    return variables


def _base_variables(data, event=None):
    variables = {
        'count': 0,
        'failed': 0,
        'minutes': 0,
        'seconds': 0,
        'limit': 0,
        'target_id': '',
        'word': '',
        'px': '',
        'url': '',
        'names': '',
        'punish': '',
        'action_text': '',
        'remaining': '',
        'error': '未知错误',
    }
    variables.update(_framework_variables(event))
    if 'target_id' not in data:
        variables['target_id'] = str(
            variables.get('user_id') or variables.get('raw_user_id') or ''
        )
    variables.update({
        name: value for name, value in data.items()
        if value is None or isinstance(value, (str, int, float, bool))
    })
    if 'target_id' in data:
        variables['target_id'] = str(data.get('target_id') or '')
    variables.update({
        alias: variables.get(source, '')
        for alias, source in _FRAMEWORK_VARIABLE_ALIASES.items()
    })
    return variables


def _join_verify_text(verify_info, empty_text='无'):
    """Render legacy verification text and the latest structured review Q&A."""
    if not isinstance(verify_info, dict):
        return empty_text
    lines = []
    verify_message = str(verify_info.get('verify_message') or '').strip()
    if verify_message:
        lines.append(verify_message)
    qa_list = verify_info.get('review_qa_list')
    if isinstance(qa_list, list):
        for index, item in enumerate(qa_list, 1):
            if not isinstance(item, dict):
                continue
            question = str(item.get('question') or '').strip()
            answer = str(item.get('answer') or '').strip()
            if question or answer:
                lines.append(f'问{index}：{question or empty_text}\n答{index}：{answer or empty_text}')
    return '\n'.join(lines) or empty_text


def _dynamic_template(key, template, data, event=None):
    variables = _base_variables(data, event)
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
        template_key = category_keys.get(data['category'], 'category_spam')
        template = _get_cached_template(template_key)
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
        rows, button_rows = [], []
        for index, item in enumerate(requests, 1):
            verify_info = item.get('verify_info')
            verify_info = verify_info if isinstance(verify_info, dict) else {}
            member_id = str(item.get('member_openid') or '')
            appid = str(variables.get('appid') or '')
            item_vars = {
                'index': index,
                'username': item.get('username') or template.get('unknown_user_text', '未知用户'),
                'avatar': (
                    f'![头像 #30px #30px]'
                    f'(https://q.qlogo.cn/qqapp/{appid}/{member_id}/640)'
                    if appid and member_id else ''
                ),
                'member_id': member_id,
                'target_id': member_id,
                'request_id': str(item.get('join_request_id') or ''),
                'verify_message': _join_verify_text(
                    verify_info, template.get('empty_text', '无')
                ),
            }
            rows.append(_render_value(item_template, {**variables, **item_vars}))
            if (template.get('button_mode') == 'join_requests'
                    and item_vars['member_id'] and item_vars['request_id']):
                button_rows.append(_render_value(
                    button_template, {**variables, **item_vars},
                ))
        variables.update(
            request_count=len(requests), request_rows='\n'.join(rows),
            next_page=_render_value(template.get('next_page_content', ''), {
                **variables, 'next_cursor': data.get('next_cursor', ''),
            }) if data.get('next_cursor') else '',
        )
        if template.get('button_mode') == 'join_requests':
            template = {**template, 'buttons': button_rows[:5]}
    elif key == 'management_stats':
        variables.update(data['stats'])
    elif key == 'audit_list':
        rows = data['rows']
        if not rows:
            key, template = 'audit_list_empty', _get_cached_template('audit_list_empty')
        else:
            labels = template.get('action_labels') or {}
            item_template = template.get('item_content', '')
            rendered_rows = []
            for item in rows:
                rendered_rows.append(_render_value(item_template, {
                    **variables,
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
                **variables,
                'name': item.get('username') or template.get('unknown_user_text', '未知用户'),
                'member_id': item.get('member_openid') or '',
                'target_id': item.get('member_openid') or '',
                'expire_at': item.get('mute_expire_at') or template.get('unknown_time_text', '未知时间'),
            }))
        overflow_count = max(0, len(members) - 10)
        variables.update(
            global_mode=labels.get(mode, labels.get('unknown', '未知')),
            member_count=len(members), member_rows=''.join(member_rows),
            overflow=_render_value(template.get('overflow_content', ''), {
                **variables, 'overflow_count': overflow_count,
            }) if overflow_count else '',
        )
    elif key == 'verify_question':
        if template.get('button_mode') == 'verify_options':
            prototype = (template.get('buttons') or [{}])[0]
            buttons = [[
                _render_value(prototype, {
                    **variables, 'option': option, 'option_index': index,
                })
                for index, option in enumerate(data['options'][:5])
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
                    **variables, 'index': index, 'word': word,
                }) for index, word in enumerate(words, 1)
            ),
        )
    elif key == 'punish_list':
        entries = data.get('entries') or []
        variables.update(
            entry_count=len(entries),
            entry_rows='\n'.join(
                _render_value(template.get('item_content', ''), {
                    **variables, **item,
                })
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
    return template, variables


def _build(key, data, event=None):
    template = _get_cached_template(
        key if key != 'category_panel' else 'category_spam'
    )
    template, variables = _dynamic_template(key, template, data, event)
    message = ReplyMessage(
        content=str(_render_value(template.get('content', ''), variables)),
        buttons=_button_rows(_render_value(template.get('buttons'), variables)),
        small_buttons=bool(template.get('small_buttons')),
        msg_type=template.get('msg_type'),
        at_user=template.get('at_user', True),
    )
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
    small_buttons=_UNSET,
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
        _build(key, data, event),
        buttons=buttons,
        small_buttons=small_buttons,
    )
    should_at_user = bool(message.at_user if at_user is _UNSET else at_user)
    if should_at_user:
        message.content = f'<@{event.user_id}> {message.content}'
    kwargs = message.delivery_kwargs()
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
                     'small_buttons': message.small_buttons,
                 })
    return result
