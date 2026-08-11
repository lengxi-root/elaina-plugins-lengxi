"""面板兼容入口，具体实现位于 panels 子包。"""

# ruff: noqa: F401

from .panels import show_category, show_gm_panel, show_mute_panel
from .panels.categories import category_markdown as _category_md
from .panels.components import button as _btn
from .panels.components import command as _cmd
from .panels.components import row as _row
from .panels.components import toggle as _toggle
