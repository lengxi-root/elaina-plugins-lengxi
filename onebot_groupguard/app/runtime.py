"""易失的运行时状态 (验证会话/防撤回缓存/刷屏缓存等), 挂到 Application 上跨热重载保持。"""


class RuntimeState:
    def __init__(self):
        self.bot_id = ''
        self.sessions: dict = {}          # key(群:QQ) -> 验证会话 (含 asyncio task)
        self.pending_comments: dict = {}  # key(群:QQ) -> 入群验证信息
        self.msg_cache: dict = {}         # message_id -> {userId, groupId, raw, segments, time}
        self.spam_cache: dict = {}        # key(群:QQ) -> [时间戳(ms)]
        self.save_task = None             # 活跃统计定时保存任务


_RT_ATTR = '_groupguard_rt'


def get_runtime() -> RuntimeState:
    from core.application import get_app
    app = get_app()
    if app is None:
        return RuntimeState()
    rt = getattr(app, _RT_ATTR, None)
    if rt is None:
        rt = RuntimeState()
        setattr(app, _RT_ATTR, rt)
    return rt
