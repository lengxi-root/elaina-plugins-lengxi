"""按原始顺序导入并注册群管指令。"""

# ruff: noqa: F401

from .admin import cmd_auth, cmd_clear_cache, cmd_refresh_group_state, cmd_verify_pass
from .common import api_error as _api_error
from .forbidden import (
    cmd_fw_add,
    cmd_fw_clear,
    cmd_fw_del,
    cmd_fw_list,
    cmd_global_fw_add,
    cmd_global_fw_apply_groups,
    cmd_global_fw_del,
    cmd_global_fw_list,
)
from .join import (
    cmd_approve_join,
    cmd_decline_join,
    cmd_join_requests,
)
from .join import (
    ensure_join_reviewer as _ensure_join_reviewer,
)
from .join import (
    join_review_buttons as _join_review_buttons,
)
from .menu import cmd_category, cmd_gm_off, cmd_gm_on, cmd_show_panel
from .menu import make_toggle as _make_toggle
from .mute import (
    cmd_mute_member,
    cmd_mute_menu,
    cmd_mute_status,
    cmd_unmute_member,
)
from .mute import (
    ensure_mute_operator as _ensure_mute_operator,
)
from .mute import (
    parse_member as _parse_member,
)
from .mute import (
    parse_members_and_minutes as _parse_members_and_minutes,
)
from .punishments import (
    cmd_cancel_recall,
    cmd_punish_list,
    cmd_speak_recall,
    cmd_target,
)
from .recall import cmd_recall_recent
from .recall import recall_batch as _recall_batch
from .remote import cmd_bind_groupguard
from .spam import cmd_spam_limit, cmd_spam_off, cmd_spam_on, cmd_spam_punish
from .spam import punish_text as _punish_text
from .statistics import cmd_management_log, cmd_management_stats

__all__ = [name for name in globals() if name.startswith("cmd_")] + [
    "_api_error",
    "_ensure_join_reviewer",
    "_ensure_mute_operator",
    "_join_review_buttons",
    "_make_toggle",
    "_parse_member",
    "_parse_members_and_minutes",
    "_punish_text",
    "_recall_batch",
]
