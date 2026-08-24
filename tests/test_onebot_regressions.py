import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parents[1]
FRAMEWORK_ROOT = PLUGIN_ROOT.parent / 'ElainaBot-Onebot1'
sys.path[:0] = [str(PLUGIN_ROOT), str(FRAMEWORK_ROOT)]

from onebot_aicat.services.packet import send_packet  # noqa: E402
from onebot_play.services.message import extract_at_users  # noqa: E402


class MentionRegressionTests(unittest.TestCase):
    def test_internal_qq_mention_name_is_available_as_text(self):
        event = type(
            'Event',
            (),
            {
                'message': [
                    {
                        'type': 'at',
                        'data': {'qq': '123456789', 'name': '汐雨'},
                    }
                ]
            },
        )()
        self.assertEqual(
            extract_at_users(event),
            [{'qq': '123456789', 'text': '汐雨'}],
        )


class PacketRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_decode_failure_is_not_reported_as_success(self):
        class Event:
            async def call_api(self, action, params):
                self.action = action
                self.params = params
                return {
                    'status': 'failed',
                    'retcode': 1,
                    'data': None,
                    'message': 'request body decode failed',
                }

        event = Event()
        result = await send_packet(event, 'MessageSvc.PbSendMsg', {1: 1})
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'request body decode failed')
        self.assertEqual(event.action, 'send_packet')
        self.assertTrue(event.params['data'])


if __name__ == '__main__':
    unittest.main()
