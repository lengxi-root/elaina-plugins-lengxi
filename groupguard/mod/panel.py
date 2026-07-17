"""群管控制面板 — 主菜单 + 分类菜单 (qqbot-cmd-input 蓝字点击)"""

from . import db


def _cmd(text, command, enter=True):
    """蓝字点击指令标签 (点击后填入输入框)"""
    return f'<qqbot-cmd-input text="{command}" show="{text}" />'


def _row(*items):
    return ' | '.join(items)


def _btn(text, data, enter=True):
    b = {'text': text, 'data': data, 'type': 2, 'tips': '当前客户端不支持'}
    if enter:
        b['enter'] = True
    return b


def _toggle(label, on, cmd_prefix):
    """开关项: ✅/❌ 在前, 点击蓝字切换开关"""
    return ('✅' if on else '❌') + _cmd(label, f"{cmd_prefix}{'关闭' if on else '开启'}")


def _category_md(category, gc):
    feat = gc['features']
    back = _cmd('返回菜单', '群管菜单')
    if category == '用户处理':
        return '\n'.join([
            _row(
                _cmd('发言撤回', '发言撤回 @用户', enter=False),
                _cmd('撤回30分', '发言撤回 30 @用户', enter=False),
                _cmd('取消撤回', '取消撤回 @用户', enter=False),
            ),
            _row(
                _cmd('撤回此人', '撤回最近 @用户', enter=False),
                _cmd('撤回最近', '撤回最近'),
                _cmd('处罚列表', '处罚列表'),
            ),
            _row(
                _cmd('针对(永久)', '针对 @用户', enter=False),
                _cmd('通过验证', '通过验证 @用户', enter=False),
            ),
            back,
        ])
    if category == '违禁词':
        fw_on = feat['forbidden_words']
        return '\n'.join([
            _row(
                _cmd('违禁词' + ('关闭' if fw_on else '开启'), '违禁词关闭' if fw_on else '违禁词开启'),
                _cmd('违禁词列表', '违禁词列表'),
            ),
            _row(
                _cmd('加违禁词', '违禁词添加 ', enter=False),
                _cmd('删违禁词', '违禁词删除 ', enter=False),
                _cmd('清空违禁词', '清空违禁词'),
            ),
            back,
        ])
    if category == '消息过滤':
        jv_on = feat['join_verify']
        bl_on = feat['block_links']
        bc_on = feat['block_cards']
        bf_on = feat['block_forward']
        return '\n'.join([
            _row(
                _cmd('入群验证' + ('关' if jv_on else '开'), '入群验证关闭' if jv_on else '入群验证开启'),
                _cmd('禁发链接' + ('关' if bl_on else '开'), '禁发链接关闭' if bl_on else '禁发链接开启'),
            ),
            _row(
                _cmd('禁发卡片' + ('关' if bc_on else '开'), '禁发卡片关闭' if bc_on else '禁发卡片开启'),
                _cmd('禁止转发' + ('关' if bf_on else '开'), '禁止转发关闭' if bf_on else '禁止转发开启'),
            ),
            back,
        ])
    # 刷屏检测
    return '\n'.join([
        _row(_cmd('开启刷屏', '开启刷屏检测'), _cmd('关闭刷屏', '关闭刷屏检测')),
        _row(
            _cmd('刷屏限制', '设置刷屏限制 15', enter=False),
            _cmd('刷屏处罚', '设置刷屏处罚 10', enter=False),
        ),
        back,
    ])


async def show_gm_panel(event):
    gid = event.group_id
    gc = db.get_group_cfg(gid)
    spam = db.get_spam_config(gid)

    feat = gc['features']
    spam_on = spam['enabled'] == 1
    text = (
        f"<@{event.user_id}>\n"
        f"🛡️ 群管菜单 · 点击蓝字切换开关\n"
        + _row(_toggle('群管功能', gc['enabled'], '群管'),
               _toggle('撤回提醒', gc['notify'], '撤回提醒')) + '\n'
        + _row(_toggle('入群验证', feat['join_verify'], '入群验证'),
               _toggle('禁发链接', feat['block_links'], '禁发链接')) + '\n'
        + _row(_toggle('禁发卡片', feat['block_cards'], '禁发卡片'),
               _toggle('禁止转发', feat['block_forward'], '禁止转发')) + '\n'
        + _row(_toggle('违禁词', feat['forbidden_words'], '违禁词'),
               ('✅' if spam_on else '❌')
               + _cmd('刷屏检测', '关闭刷屏检测' if spam_on else '开启刷屏检测'))
    )
    buttons = [
        [
            _btn('用户处理', '群管 用户处理'),
            _btn('违禁词管理', '群管 违禁词'),
            _btn('刷屏设置', '群管 刷屏检测'),
        ],
        [
            _btn('群管授权', '群管授权'),
            _btn('清除缓存', '清除缓存'),
        ],
    ]
    await event.reply(text, buttons=buttons)


async def show_category(event, category):
    gc = db.get_group_cfg(event.group_id)
    await event.reply(f'<@{event.user_id}>\n🛡️ {category}\n\n' + _category_md(category, gc))
