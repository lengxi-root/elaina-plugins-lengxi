"""群管面板通用组件。"""


def command(text, command_text, enter=True):
    """生成点击后填入输入框的蓝字指令标签。"""
    return f'<qqbot-cmd-input text="{command_text}" show="{text}" />'


def row(*items):
    return ' | '.join(items)


def button(text, data, enter=True):
    item = {'text': text, 'data': data, 'type': 2, 'tips': '当前客户端不支持'}
    if enter:
        item['enter'] = True
    return item


def toggle(label, enabled, command_prefix):
    """生成带状态标识的功能开关。"""
    command_text = f"{command_prefix}{'关闭' if enabled else '开启'}"
    return ('✅' if enabled else '❌') + command(label, command_text)
