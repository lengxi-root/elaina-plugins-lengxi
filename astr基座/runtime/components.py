"""消息组件 + MessageChain (仅实现astr插件用到的接口)。"""

from __future__ import annotations


class _Component:
    type = "component"

    def __init__(self, *_, **__):
        pass


BaseMessageComponent = _Component
ComponentType = _Component


class Plain(_Component):
    type = "Plain"

    def __init__(self, text: str = "", **_):
        self.text = str(text) if text is not None else ""

    def __repr__(self):
        return f"Plain({self.text!r})"


class Image(_Component):
    type = "Image"

    def __init__(self, file: str = "", url: str = "", path: str = "", **_):
        self.file = file or url or path or ""
        self.url = url or (file if str(file).startswith(("http://", "https://")) else "")
        self.path = path or ("" if self.url else file)

    @classmethod
    def fromURL(cls, url: str):
        return cls(file=url, url=url)

    @classmethod
    def fromFileSystem(cls, path: str):
        return cls(file=path, path=path)

    @classmethod
    def fromBytes(cls, data: bytes):
        obj = cls()
        obj.bytes_ = data
        obj.file = data
        return obj

    def __repr__(self):
        return f"Image(file={self.file!r})"


class At(_Component):
    type = "At"

    def __init__(self, qq=None, name: str = "", **_):
        self.qq = qq
        self.name = name or ""


class AtAll(_Component):
    type = "AtAll"

    def __init__(self, **_):
        pass


class Face(_Component):
    type = "Face"

    def __init__(self, id=None, **_):
        self.id = id


class Record(_Component):
    type = "Record"

    def __init__(self, file: str = "", url: str = "", path: str = "", **_):
        self.file = file or url or path or ""

    @classmethod
    def fromFileSystem(cls, path: str = "", **kw):
        return cls(file=path, path=path, **kw)

    @classmethod
    def fromURL(cls, url: str = "", **kw):
        return cls(file=url, url=url, **kw)


class Video(_Component):
    type = "Video"

    def __init__(self, file: str = "", url: str = "", path: str = "", cover: str = "", **_):
        self.file = file or url or path or ""
        self.url = url
        self.path = path
        self.cover = cover

    @classmethod
    def fromFileSystem(cls, path: str = "", **kw):
        return cls(file=path, path=path, **kw)

    @classmethod
    def fromURL(cls, url: str = "", **kw):
        return cls(file=url, url=url, **kw)


class File(_Component):
    type = "File"

    def __init__(self, name: str = "", file: str = "", url: str = "", file_: str = "", **_):
        self.name = name
        self.file = file or file_ or url or ""
        self.file_ = self.file
        self.url = url


class Poke(_Component):
    type = "Poke"

    def __init__(self, qq=None, id=None, type: str = "poke", **_):
        self.qq = qq
        self.id = id
        self.poke_type = type


class Json(_Component):
    type = "Json"

    def __init__(self, data=None, **_):
        self.data = data


class Forward(_Component):
    type = "Forward"

    def __init__(self, id=None, **_):
        self.id = id


class Unknown(_Component):
    type = "Unknown"

    def __init__(self, text: str = "", **_):
        self.text = text


class Reply(_Component):
    type = "Reply"

    def __init__(self, id=None, **_):
        self.id = id


class Node(_Component):
    """合并转发的单个节点。"""

    type = "Node"

    def __init__(self, content=None, uin=None, name: str = "", **_):
        self.content = content or []
        self.uin = uin
        self.name = name or ""


class Nodes(_Component):
    """合并转发的节点列表 (astrbot 用于多条转发消息)。"""

    type = "Nodes"

    def __init__(self, nodes=None, **_):
        self.nodes = list(nodes) if nodes else []


Text = Plain


class MessageChain:
    """AstrBot 消息链 (链式构造)。"""

    def __init__(self, chain=None):
        self.chain: list = list(chain) if chain else []

    def message(self, text: str):
        self.chain.append(Plain(text))
        return self

    def text(self, text: str):
        self.chain.append(Plain(text))
        return self

    def file_image(self, file):
        if isinstance(file, bytes):
            self.chain.append(Image.fromBytes(file))
        elif isinstance(file, str) and file.startswith(("http://", "https://")):
            self.chain.append(Image.fromURL(file))
        else:
            self.chain.append(Image.fromFileSystem(file))
        return self

    def image(self, file):
        return self.file_image(file)

    def url_image(self, url):
        self.chain.append(Image.fromURL(url))
        return self

    def at(self, qq=None, name: str = ""):
        self.chain.append(At(qq=qq, name=name))
        return self

    def at_all(self):
        self.chain.append(AtAll())
        return self

    def append(self, component):
        self.chain.append(component)
        return self

    def extend(self, components):
        self.chain.extend(components)
        return self


class MessageEventResult:
    """事件被动结果 (plain_result / image_result / chain_result 的返回)。"""

    def __init__(self, chain=None):
        if isinstance(chain, MessageChain):
            self.chain = chain.chain
        else:
            self.chain = list(chain) if chain else []

    def message(self, text):
        self.chain.append(Plain(text))
        return self
