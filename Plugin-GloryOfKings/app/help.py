"""帮助 — 渲染 Gitee 原版 help.html 面板"""

import time

from core.plugin.decorators import handler
from ..lib import render

_HELP_TEXT = """王者荣耀 · 指令菜单
【账号】<qqbot-cmd-input text='王者绑定 ' show='王者绑定 营地ID' /> | <qqbot-cmd-input text='王者我的ID' /> | <qqbot-cmd-input text='王者切换 ' show='王者切换 序号' /> | <qqbot-cmd-input text='王者删除 ' show='王者删除 序号' />
【查询】<qqbot-cmd-input text='王者主页' /> | <qqbot-cmd-input text='王者战绩' /> | <qqbot-cmd-input text='王者查人 ' show='王者查人 昵称' /> | <qqbot-cmd-input text='王者谁在游戏' />
【英雄】<qqbot-cmd-input text='王者英雄列表' />（常用英雄榜） | <qqbot-cmd-input text='王者英雄攻略 ' show='王者英雄攻略 英雄名' /> | <qqbot-cmd-input text='王者英雄梯度榜' /> | <qqbot-cmd-input text='王者我的英雄列表' /> | <qqbot-cmd-input text='查战力 ' show='查战力 英雄名' /> | <qqbot-cmd-input text='查皮肤 ' show='查皮肤 英雄名' /> | <qqbot-cmd-input text='王者皮肤墙' />
【排行】<qqbot-cmd-input text='王者段位榜' /> | <qqbot-cmd-input text='王者排位排名' /> | <qqbot-cmd-input text='王者巅峰排名' /> | <qqbot-cmd-input text='王者排位趋势' /> | <qqbot-cmd-input text='王者分数趋势' /> | <qqbot-cmd-input text='王者巅峰赛数据' /> | <qqbot-cmd-input text='王者赛季页面' />
【报告】<qqbot-cmd-input text='王者日报' /> | <qqbot-cmd-input text='王者周报' /> | <qqbot-cmd-input text='王者月报' /> | <qqbot-cmd-input text='王者群日报' /> | <qqbot-cmd-input text='王者群周报' /> | <qqbot-cmd-input text='王者群月报' />
【战绩/皮肤】<qqbot-cmd-input text='王者缺皮肤 ' show='王者缺皮肤 英雄名' /> | <qqbot-cmd-input text='王者皮肤资讯' />
【推送/系统】<qqbot-cmd-input text='王者推送 开' /> | <qqbot-cmd-input text='王者推送 关' /> | <qqbot-cmd-input text='王者推送 状态' /> | <qqbot-cmd-input text='王者清理图片缓存' />
【登录/账号池】<qqbot-cmd-input text='王者wx登录' /> | <qqbot-cmd-input text='王者wx全局登录' /> | <qqbot-cmd-input text='王者QQ登录' /> | <qqbot-cmd-input text='王者QQ全局登录' /> | <qqbot-cmd-input text='王者账号池' /> | <qqbot-cmd-input text='王者清理失效' />
【帮助】<qqbot-cmd-input text='王者帮助' />（也支持 王者荣耀帮助 / 王者菜单）"""


def _get_runtime():
    from .. import get_runtime
    return get_runtime()


@handler(r'^王者(?:荣耀|农药)?(?:插件)?(?:帮助|help|菜单)$', name='王者帮助',
         desc='王者荣耀插件帮助', priority=10)
async def cmd_help(event, match):
    data = {"generatedAt": time.strftime("%Y/%m/%d %H:%M:%S")}
    # 帮助菜单内容固定, 缓存图床直链 12 小时, 命中则跳过渲染+上传
    ok = await render.send_html(event, "help.html", data, name_hint="help",
                               cache_key="help-v10", cache_ttl=12 * 3600)
    if not ok:
        await event.reply(_HELP_TEXT)
