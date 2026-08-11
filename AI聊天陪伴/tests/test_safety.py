import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / 'app' / 'safety.py'
SPEC = importlib.util.spec_from_file_location('ai_companion_safety', MODULE_PATH)
safety = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safety)


class VisibleOutputTests(unittest.TestCase):
    def test_removes_think_block(self):
        self.assertEqual(
            safety.visible_output('<think>内部推理</think>最终答复'),
            '最终答复',
        )

    def test_removes_multiline_reasoning_block(self):
        self.assertEqual(
            safety.visible_output('前文<reasoning>第一步\n第二步</reasoning>后文'),
            '前文后文',
        )

    def test_drops_unclosed_reasoning_tail(self):
        self.assertEqual(
            safety.visible_output('最终答复<thinking>未闭合的内部推理'),
            '最终答复',
        )

    def test_safe_output_filters_reasoning_before_blocklist(self):
        result, hit = safety.safe_output(
            '<analysis>仅内部出现敏感词</analysis>正常答复',
            ['敏感词'],
            '已拦截',
        )
        self.assertEqual((result, hit), ('正常答复', ''))

    def test_removes_internal_tool_protocol(self):
        value = (
            '<tool_music query="aaa" selection="1">'
            '{"query":"aaa","selection":1}'
            '</tool_music>正常答复'
        )
        self.assertEqual(safety.visible_output(value), '正常答复')


if __name__ == '__main__':
    unittest.main()
