"""基座冒烟测试: 出厂 apps/ 为空, 临时装入 tests/fixtures/hello_astr 验证收发闭环。

用法 (装进 ElainaBot-Onebot 后, 从框架根运行):
    python plugins/astr基座/tests/functional_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness as h  # noqa: E402


def _segs(sent):
    kinds = []
    for s in sent:
        msg = s["params"].get("message", [])
        if isinstance(msg, list):
            kinds += [seg.get("type") for seg in msg if isinstance(seg, dict)]
        elif isinstance(msg, str):
            kinds.append("text")
    return kinds


# (用例名, 指令, 期望(None=出站非空 / 子串 / ("seg",类型)))
CASES = [
    ("command hello", "hello", "你好"),
    ("command add 参数", "add 3 5", "3+5=8"),
    ("command echo -> At+文本", "echo 测试文本", "测试文本"),
    ("regex 匹配", "喵喵喵", "喵喵喵"),
]


async def main():
    installed = h.install_fixtures(["hello_astr"])
    try:
        mgr = await h.load_base()
        import importlib
        state = importlib.import_module(f"plugins.{h.BASE_DIR}.runtime.state")
        print(f"handlers={mgr.handler_count} plugins={len(state.PLUGIN_SPECS)}")
        print("instances ok:",
              sum(1 for s in state.PLUGIN_SPECS if s.instance), "/", len(state.PLUGIN_SPECS))
        print("-" * 60)

        ok = 0
        for name, text, expect in CASES:
            try:
                sent = await h.feed(mgr, text)
            except Exception as e:
                print(f"[ERR ] {name:22} 指令异常: {e}")
                continue
            got = h.texts(sent)
            kinds = _segs(sent)
            if expect is None:
                passed = bool(sent)
            elif isinstance(expect, tuple) and expect[0] == "seg":
                passed = expect[1] in kinds
            else:
                passed = expect in got
            ok += passed
            flag = "PASS" if passed else "FAIL"
            print(f"[{flag}] {name:22} segs={kinds} {got[:40]!r}")
        print("-" * 60)
        print(f"{ok}/{len(CASES)} cases passed")
        return ok, len(CASES)
    finally:
        h.uninstall_fixtures(installed)


if __name__ == "__main__":
    asyncio.run(main())
