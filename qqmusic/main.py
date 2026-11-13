"""QQ 音乐大型插件入口。"""

__plugin_meta__ = {
    'name': '音乐点歌',
    'author': '冷曦、24801816',
    'description': 'QQ音乐点歌、个人扫码登录和会员账号池轮询',
    'version': '1.0.7',
    'license': 'MIT',
}

from .app import login as _login  # noqa: F401
from .app import music as _music  # noqa: F401
from .app import runtime as _runtime  # noqa: F401
