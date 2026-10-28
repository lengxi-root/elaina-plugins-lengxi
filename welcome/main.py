"""群成员入群欢迎插件入口。"""

__plugin_meta__ = {
    "name": "群成员入群欢迎",
    "author": "ElainaBot",
    "description": "群成员入群自动推送, 召回与欢迎可同时开启, 支持黑白名单与合并欢迎, 含 Web 面板",
    "version": "2.1.3",
    "github": "https://github.com/ElainaCore/Elaina-plugins",
    "license": "MIT",
}

from .handlers import welcome as _implementation
