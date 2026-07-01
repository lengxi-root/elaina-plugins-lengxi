"""统一发送: MessageChain -> OneBot v11 消息段 -> 框架 API。"""

from __future__ import annotations

from . import state
from .components import (
    AtAll,
    BaseMessageComponent,
    Image,
    MessageChain,
    MessageEventResult,
    Node,
    Nodes,
    Plain,
    Record,
)

log = state.log


# ---- UMO (统一会话标识): 与 AstrBot aiocqhttp 对齐 ----
#   群:  aiocqhttp:GroupMessage:<group_id>
#   私聊: aiocqhttp:FriendMessage:<user_id>

def make_umo(event) -> str:
    gid = getattr(event, "group_id", None)
    if gid:
        return f"aiocqhttp:GroupMessage:{gid}"
    uid = getattr(event, "user_id", "") or ""
    return f"aiocqhttp:FriendMessage:{uid}"


def parse_umo(umo: str):
    """返回 (scene, target_id); scene in {'group','c2c'}。失败返回 (None, None)。"""
    if not umo or not isinstance(umo, str):
        return None, None
    parts = umo.split(":")
    if len(parts) >= 3:
        scene = "group" if "group" in parts[1].lower() else "c2c"
        return scene, parts[-1]
    if len(parts) == 1:  # 纯数字会话, 当群处理
        return "group", parts[0]
    return None, None


def _chain_items(chain):
    if isinstance(chain, MessageChain):
        return chain.chain
    if isinstance(chain, BaseMessageComponent):
        return [chain]
    return chain if isinstance(chain, list) else []


def _is_forward(chain) -> bool:
    items = _chain_items(chain)
    return any(isinstance(c, (Node, Nodes)) for c in items)


def _needs_base64(c) -> bool:
    """本地文件 / 字节需转 base64 后再发, 否则远端 OneBot 读不到会丢弃该段。"""
    if not isinstance(c, (Image, Record)):
        return False
    if getattr(c, "bytes_", None) is not None:
        return True
    f = c.file
    if isinstance(f, bytes):
        return True
    if isinstance(f, str):
        return not f.startswith(("http://", "https://", "base64://"))
    return False


async def _component_to_segment(c) -> dict | None:
    """组件 -> OneBot 消息段; 图片/语音的本地文件与字节转 base64 保证可送达。"""
    if _needs_base64(c):
        try:
            b64 = await c.convert_to_base64()
            seg_type = "image" if isinstance(c, Image) else "record"
            return {"type": seg_type, "data": {"file": f"base64://{b64}"}}
        except Exception as e:
            log.warning(f"[astrbot基座] {type(c).__name__} 转 base64 失败, 回退原始文件: {e}")
    return c.toDict()


async def _node_to_dict(node: Node) -> dict:
    content_segs = []
    for c in _chain_items(node.content) if not isinstance(node.content, str) else [Plain(node.content)]:
        try:
            seg = await _component_to_segment(c)
            if seg:
                content_segs.append(seg)
        except Exception:
            pass
    return {
        "type": "node",
        "data": {
            "name": node.name or "匿名",
            "uin": str(node.uin or "10000"),
            "content": content_segs,
        },
    }


async def _collect_nodes(chain) -> list[dict]:
    nodes: list[dict] = []
    for c in _chain_items(chain):
        if isinstance(c, Nodes):
            for n in c.nodes:
                if isinstance(n, Node):
                    nodes.append(await _node_to_dict(n))
        elif isinstance(c, Node):
            nodes.append(await _node_to_dict(c))
    return nodes


async def chain_to_segments(chain) -> list[dict]:
    """把消息链转成 OneBot v11 消息段列表 (跳过合并转发节点)。"""
    segments: list[dict] = []
    for c in _chain_items(chain):
        if isinstance(c, (Node, Nodes)):
            continue
        if isinstance(c, Plain) and not c.text:
            continue
        try:
            seg = await _component_to_segment(c)
        except Exception as e:
            log.warning(f"[astrbot基座] 组件转换失败 {type(c).__name__}: {e}")
            continue
        if not seg:
            continue
        segments.append(seg)
        if isinstance(c, AtAll) or (seg.get("type") == "at"):
            segments.append({"type": "text", "data": {"text": " "}})
    return segments


async def _send_forward(api, *, group_id=None, user_id=None, nodes: list[dict]):
    try:
        if group_id:
            return await api.send_group_forward_msg(group_id, nodes)
        if user_id:
            return await api.send_private_forward_msg(user_id, nodes)
    except Exception as e:
        log.warning(f"[astrbot基座] 合并转发发送失败: {e}")
    return None


async def send_chain(chain, *, event=None, group_id=None, user_id=None, self_id=None):
    """发送一条 MessageChain。优先回复触发事件, 否则按 group_id/user_id 主动发。"""
    if chain is None:
        return None
    api = state.get_api(self_id)
    if api is None:
        return None

    if event is not None:
        group_id = getattr(event, "group_id", None) or None
        user_id = getattr(event, "user_id", None)
        self_id = self_id or str(getattr(event, "self_id", "") or "")

    if _is_forward(chain):
        nodes = await _collect_nodes(chain)
        if nodes:
            r = await _send_forward(api, group_id=group_id, user_id=user_id, nodes=nodes)
            if r is not None:
                return r
        # 转发失败则回退为普通消息继续发送其余组件

    segments = await chain_to_segments(chain)
    if not segments:
        return None
    try:
        if group_id:
            return await api.send_group_msg(int(group_id), segments)
        if user_id:
            return await api.send_private_msg(int(user_id), segments)
    except Exception as e:
        log.warning(f"[astrbot基座] 消息发送失败: {e}")
    return None


async def send_result(event, result):
    """处理插件被动 yield / return 的单个结果。"""
    if result is None:
        return
    if isinstance(result, (MessageEventResult, MessageChain)):
        await send_chain(result, event=event)
    elif isinstance(result, BaseMessageComponent):
        await send_chain([result], event=event)
    elif isinstance(result, list):
        await send_chain(result, event=event)
    elif isinstance(result, str) and result:
        await send_chain(MessageChain().message(result), event=event)
