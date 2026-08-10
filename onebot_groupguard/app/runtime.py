"""群管运行时状态。"""


class RuntimeState:
    def __init__(self):
        self.bot_id = ''
        self.sessions: dict = {}
        self.pending_comments: dict = {}
        self.msg_cache: dict = {}
        self.spam_cache: dict = {}
        self.last_cache_cleanup = 0
        self.save_task = None


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
