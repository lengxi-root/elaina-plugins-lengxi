import unittest
from datetime import datetime

from onebot_amsghook.policy import (
    EXTERNAL_CALLER,
    caller_name,
    group_target,
    normalize_config,
    transform_message,
)
from onebot_red_packet.policy import normalize_settings, rejection_reason


class MessageHookPolicyTests(unittest.TestCase):
    def test_legacy_rule_fields_are_normalized(self):
        config = normalize_config({
            'rules': [{
                'name': 'demo',
                'ownerOnly': True,
                'blockedGroups': ['1', '1', '2'],
                'blockedUsers': '3,4',
                'replaceText': 'a=b',
            }],
        })
        rule = config['rules'][0]
        self.assertTrue(rule['owner_only'])
        self.assertEqual(rule['blocked_groups'], ['1', '2'])
        self.assertEqual(rule['blocked_users'], ['3', '4'])
        self.assertEqual(rule['replace_text'], 'a=b')

    def test_message_transform_does_not_mutate_input(self):
        message = [
            {'type': 'text', 'data': {'text': 'hello'}},
            {'type': 'image', 'data': {'file': 'a.png'}},
        ]
        result = transform_message(message, replace_spec='hello=你好', suffix='!')
        self.assertEqual(result[0]['data']['text'], '你好!')
        self.assertEqual(message[0]['data']['text'], 'hello')

    def test_external_caller_and_group_target(self):
        self.assertEqual(caller_name(''), EXTERNAL_CALLER)
        self.assertEqual(
            group_target('send_msg', {'message_type': 'group', 'group_id': 123}),
            '123',
        )


class RedPacketPolicyTests(unittest.TestCase):
    def test_delay_range_is_ordered_and_bounded(self):
        settings = normalize_settings({'delay_min_ms': 800, 'delay_max_ms': 100})
        self.assertEqual(settings['delay_min_ms'], 100)
        self.assertEqual(settings['delay_max_ms'], 800)

    def test_exclusive_packet_and_stop_window(self):
        settings = normalize_settings({
            'stop_by_time': True,
            'stop_start_time': '23:00',
            'stop_end_time': '06:00',
        })
        packet = {'red_packet_type': 3, 'exclusive_uin': '10001'}
        self.assertEqual(
            rejection_reason(
                settings, packet, '10001', now=datetime(2026, 8, 24, 1, 0),
            ),
            '处于停止时段',
        )
        settings['stop_by_time'] = False
        self.assertEqual(rejection_reason(settings, packet, '20002'), '专属红包目标不是当前账号')

    def test_notify_only_still_obeys_user_whitelist(self):
        settings = normalize_settings({
            'notify_only': True,
            'group_mode': 'whitelist',
            'whitelist_users': '123,456',
        })
        packet = {'sender_id': '999', 'wishing': '恭喜发财'}
        self.assertEqual(rejection_reason(settings, packet, '10001'), '用户不在白名单')
        packet['sender_id'] = '123'
        self.assertEqual(rejection_reason(settings, packet, '10001'), '仅通知模式')


if __name__ == '__main__':
    unittest.main()
