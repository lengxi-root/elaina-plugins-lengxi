"""批量邀请单项测试：用模拟协议端验证发包和任务结果，不访问 QQ。"""

import asyncio
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


PLUGIN_DIR = Path(__file__).resolve().parents[1]
BOT, SOURCE, TARGET = "10001", "20001", "30001"
# OIDB 0x758_1 的成功回执；省略的 protobuf ErrorCode 默认为 0。
INVITE_OK = "08d80e10012200"


class InviteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = types.SimpleNamespace()
        self.api = Mock()
        self.api.connected_self_ids.return_value = [BOT]
        self.api.bot_accounts.return_value = [{"self_id": BOT, "connected": True}]
        self.api.call_api = AsyncMock(side_effect=self.reply)
        plugins = types.ModuleType("core.plugins")
        plugins.PLUGIN = "groupguard"
        plugins.get_logger = Mock(return_value=Mock())
        plugins.get_api = lambda: self.api
        plugins.get_app = lambda: self.app
        plugins.register_route = Mock()
        packet = types.ModuleType("core.runtime.embedded.packet")
        packet.packet_response_bytes = lambda data: bytes.fromhex(data)
        package = types.ModuleType("groupguard_under_test")
        package.__path__ = [str(PLUGIN_DIR)]
        self.modules = patch.dict(
            sys.modules,
            {
                "core.plugins": plugins,
                "core.runtime.embedded.packet": packet,
                "groupguard_under_test": package,
            },
        )
        self.modules.start()
        self.invites = importlib.import_module("groupguard_under_test.services.invites")
        self.runtime = importlib.import_module("groupguard_under_test.services.runtime")
        self.source_members = [
            {"user_id": 40001, "uid": "u_first"},
            {"user_id": 40002, "uid": "u_second"},
            {"user_id": 40003},
        ]
        self.target_members = [{"user_id": 40002}]
        self.groups = [{"group_id": int(SOURCE)}, {"group_id": int(TARGET)}]

    async def asyncTearDown(self):
        try:
            await self.runtime.stop_background()
        finally:
            self.modules.stop()

    def reply(self, action, params, *, self_id):
        self.assertEqual(self_id, BOT)
        if action == "get_group_list":
            data = self.groups
        elif action == "get_group_member_list":
            self.assertTrue(params["no_cache"])
            data = (
                self.source_members
                if params["group_id"] == int(SOURCE)
                else self.target_members
            )
        elif action == "get_stranger_info":
            data = {"user_id": params["user_id"], "uid": "u_third"}
        elif action == "send_packet":
            data = INVITE_OK
        else:
            self.fail(f"意外的 API: {action}")
        return {"status": "ok", "retcode": 0, "data": data}

    def start(self, users="40001\n40002\n40004\n40001", **overrides):
        return self.invites.start_invites(
            {
                "self_id": BOT,
                "source_group_id": SOURCE,
                "target_group_id": TARGET,
                "users": users,
                **overrides,
            }
        )

    async def finish(self):
        await asyncio.gather(*self.runtime.get_runtime().background_tasks)

    def test_list_validation_is_atomic_and_deduplicates(self):
        self.assertEqual(
            self.invites.parse_users(" 40001 \r\n\n040001\n40002"),
            (["40001", "40002"], 1),
        )
        for text in (
            "40001\n40002,40003",
            "40001\nQQ40002",
            "４０００１",
            "0",
            "4294967296",
            "",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.start(text)
        with self.assertRaisesRegex(ValueError, "最多"):
            self.start("\n".join(str(n) for n in range(50000, 50201)))
        self.api.call_api.assert_not_called()

    def test_packet_matches_protocol_fixture_and_checks_qq_retcode(self):
        # 目标群=30001，来源群=20001，UID=u_first（独立的协议十六进制样本）。
        expected = "08d80e1001221508b1ea01120d0a07755f666972737410a19c0150006000"
        self.assertEqual(
            self.invites.build_invite_packet(SOURCE, TARGET, "u_first").hex(), expected
        )
        self.invites.check_invite_response(INVITE_OK)
        # 外层 OneBot 成功不能掩盖 QQ OIDB 的权限拒绝。
        with self.assertRaisesRegex(ValueError, "QQ 拒绝邀请.*5"):
            self.invites.check_invite_response("08d80e100118052a0664656e696564")
        for invalid in ("", "00", "0801", "08d80e100122ff", "08d80e10022200"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.invites.check_invite_response(invalid)

    async def test_routes_each_call_and_skips_nonmembers_and_duplicates(self):
        job = self.start()
        await self.finish()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["duplicates"], 1)
        self.assertEqual(
            [r["status"] for r in job["results"]], ["submitted", "skipped", "skipped"]
        )
        calls = [
            c for c in self.api.call_api.call_args_list if c.args[0] == "send_packet"
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1]["cmd"], "OidbSvcTrpcTcp.0x758_1")
        self.assertTrue(calls[0].args[1]["rsp"])

    async def test_preflight_failure_sends_no_invites(self):
        self.groups = [{"group_id": int(SOURCE)}]
        job = self.start()
        await self.finish()
        self.assertEqual(job["status"], "failed")
        self.assertIn("同时加入", job["message"])
        self.assertFalse(
            any(c.args[0] == "send_packet" for c in self.api.call_api.call_args_list)
        )

    async def test_failure_does_not_stop_later_users_and_resolves_missing_uid(self):
        async def reply(action, params, *, self_id):
            if action == "send_packet" and not getattr(self, "sent_once", False):
                self.sent_once = True
                return {
                    "status": "failed",
                    "retcode": 1405,
                    "data": None,
                    "message": "邀请被拒绝",
                }
            return self.reply(action, params, self_id=self_id)

        self.api.call_api.side_effect = reply
        with patch.object(
            self.invites.asyncio, "sleep", new_callable=AsyncMock
        ) as sleep:
            job = self.start("40001\n40003")
            await self.finish()
        sleep.assert_awaited_once_with(2)
        self.assertEqual([r["status"] for r in job["results"]], ["failed", "submitted"])
        self.assertIn("邀请被拒绝", job["results"][0]["message"])
        self.assertTrue(
            any(
                c.args[0] == "get_stranger_info"
                for c in self.api.call_api.call_args_list
            )
        )

    async def test_rejects_ambiguous_response_and_missing_uid(self):
        for response in (
            None,
            {"status": "ok", "retcode": 1, "data": INVITE_OK},
            {"status": "ok", "retcode": 0, "data": None},
        ):
            self.api.call_api.side_effect = None
            self.api.call_api.return_value = response
            with (
                self.subTest(response=response),
                self.assertRaises((RuntimeError, TypeError)),
            ):
                self.invites.check_invite_response(
                    await self.invites._api("send_packet", {}, BOT)
                )

        def reply(action, params, *, self_id):
            if action == "get_stranger_info":
                return {"status": "ok", "retcode": 0, "data": {"user_id": 40003}}
            return self.reply(action, params, self_id=self_id)

        self.api.call_api.side_effect = reply
        self.api.call_api.reset_mock()
        job = self.start("40003")
        await self.finish()
        self.assertEqual(job["results"][0]["status"], "failed")
        self.assertFalse(
            any(c.args[0] == "send_packet" for c in self.api.call_api.call_args_list)
        )

    async def test_rejects_same_group_offline_bot_and_duplicate_running_job(self):
        with self.assertRaisesRegex(ValueError, "不能相同"):
            self.start(target_group_id=SOURCE)
        with self.assertRaisesRegex(ValueError, "不在线"):
            self.start(self_id="10002")
        self.start()
        with self.assertRaisesRegex(RuntimeError, "正在执行"):
            self.start()
        await self.finish()

    async def test_unload_cancels_running_job(self):
        queued = self.start()
        await self.runtime.stop_background()
        self.assertEqual(queued["status"], "cancelled")
        waiting = asyncio.Event()

        async def reply(action, params, *, self_id):
            waiting.set()
            await asyncio.Event().wait()

        self.api.call_api.side_effect = reply
        job = self.start()
        await waiting.wait()
        await self.runtime.stop_background()
        self.assertEqual(job["status"], "cancelled")

    async def test_http_routes_reject_bad_input_and_recover_legacy_runtime(self):
        # 路由依赖存储/日志模块，用替身避免加载本机群管配置。
        logbuf = types.ModuleType("groupguard_under_test.services.logbuf")
        store = types.ModuleType("groupguard_under_test.storage.repository")
        with patch.dict(sys.modules, {logbuf.__name__: logbuf, store.__name__: store}):
            routes = importlib.import_module("groupguard_under_test.web.routes")
        request = types.SimpleNamespace(
            json=AsyncMock(return_value=[]), content_length=2
        )
        self.assertEqual((await routes._start_invites(request)).status, 400)
        request.content_length = 65537
        self.assertEqual((await routes._start_invites(request)).status, 400)
        legacy = self.runtime.RuntimeState()
        del legacy.invite_job
        self.app._groupguard_rt = legacy
        response = await routes._get_invites(request)
        self.assertIsNone(json.loads(response.text)["job"])


if __name__ == "__main__":
    unittest.main()
