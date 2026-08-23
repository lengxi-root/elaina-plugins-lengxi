import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / 'app' / 'safety.py'
SPEC = importlib.util.spec_from_file_location('ai_companion_safety', MODULE_PATH)
safety = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safety)


class SafeOutputTests(unittest.TestCase):
    def test_disabled_content_safety_skips_all_configured_checks(self):
        result, hit = safety.safe_output(
            '敏感词 192.168.1.1', ['敏感词'], '已拦截', enabled=False,
        )
        self.assertEqual((result, hit), ('敏感词 192.168.1.1', ''))

if __name__ == '__main__':
    unittest.main()
