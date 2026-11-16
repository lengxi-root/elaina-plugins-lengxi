"""群管操作使用 QQ 群成员 OpenID。"""


def member_openid(event):
    return str(getattr(event, "raw_user_id", "") or getattr(event, "user_id", "") or "")


def target_mentions(event):
    """返回命令中的目标艾特，跳过用于唤起命令的机器人艾特。"""
    return (
        item
        for item in (getattr(event, "mentions", None) or ())
        if isinstance(item, dict) and not item.get("is_you")
    )


def mentioned_members(event):
    for item in target_mentions(event):
        if item.get("bot") or item.get("scope") == "all":
            continue
        openid = item.get("member_openid") or item.get("id")
        if isinstance(openid, str) and openid.strip():
            yield openid.strip(), str(item.get("member_role") or "")
