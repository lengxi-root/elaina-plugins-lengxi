"""群管分类菜单内容。"""

from .components import command, row


def category_markdown(category, group_config):
    features = group_config['features']
    back = command('返回菜单', '群管菜单')
    if category == '用户处理':
        return '\n'.join([
            row(
                command('发言撤回', '发言撤回 @用户', enter=False),
                command('撤回30分', '发言撤回 30 @用户', enter=False),
                command('取消撤回', '取消撤回 @用户', enter=False),
            ),
            row(
                command('撤回此人', '撤回最近 @用户', enter=False),
                command('撤回最近', '撤回最近'),
                command('处罚列表', '处罚列表'),
            ),
            row(
                command('针对(永久)', '针对 @用户', enter=False),
                command('通过验证', '通过验证 @用户', enter=False),
            ),
            back,
        ])
    if category == '群管理':
        return '\n'.join([
            row(
                command('禁言菜单', '禁言菜单'),
                command('禁言列表', '禁言列表'),
                command('入群申请', '入群申请'),
            ),
            row(
                command('通过申请', '通过入群 成员ID 申请ID', enter=False),
                command('拒绝申请', '拒绝入群 成员ID 申请ID 理由', enter=False),
                command('拒绝并拉黑', '拒绝并拉黑 成员ID 申请ID', enter=False),
            ),
            back,
        ])
    if category == '违禁词':
        enabled = features['forbidden_words']
        return '\n'.join([
            row(
                command(
                    '违禁词' + ('关闭' if enabled else '开启'),
                    '违禁词关闭' if enabled else '违禁词开启',
                ),
                command('违禁词列表', '违禁词列表'),
            ),
            row(
                command('加违禁词', '违禁词添加 ', enter=False),
                command('删违禁词', '违禁词删除 ', enter=False),
                command('清空违禁词', '清空违禁词'),
            ),
            back,
        ])
    if category == '消息过滤':
        join_verify = features['join_verify']
        block_links = features['block_links']
        block_cards = features['block_cards']
        block_forward = features['block_forward']
        return '\n'.join([
            row(
                command(
                    '入群验证' + ('关' if join_verify else '开'),
                    '入群验证关闭' if join_verify else '入群验证开启',
                ),
                command(
                    '禁发链接' + ('关' if block_links else '开'),
                    '禁发链接关闭' if block_links else '禁发链接开启',
                ),
            ),
            row(
                command(
                    '禁发卡片' + ('关' if block_cards else '开'),
                    '禁发卡片关闭' if block_cards else '禁发卡片开启',
                ),
                command(
                    '禁止转发' + ('关' if block_forward else '开'),
                    '禁止转发关闭' if block_forward else '禁止转发开启',
                ),
            ),
            back,
        ])
    return '\n'.join([
        row(command('开启刷屏', '开启刷屏检测'), command('关闭刷屏', '关闭刷屏检测')),
        row(
            command('刷屏限制', '设置刷屏限制 15', enter=False),
            command('刷屏处罚', '设置刷屏处罚 10', enter=False),
        ),
        back,
    ])
