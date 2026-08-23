import asyncio
import importlib.util
import json
import logging
import pathlib
import sys
import types


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_webpanel():
    package = '_ai_dev_test'
    app_package = f'{package}.app'
    root_module = _module(package)
    root_module.__path__ = [str(ROOT / 'ai_dev')]
    app_module = _module(app_package)
    app_module.__path__ = [str(ROOT / 'ai_dev' / 'app')]

    core = _module('core')
    core.__path__ = []
    core_base = _module('core.base')
    core_base.__path__ = []
    _module(
        'core.base.logger',
        PLUGIN='plugin',
        get_logger=lambda *_args, **_kwargs: logging.getLogger('ai-dev-test'),
    )
    core_plugin = _module('core.plugin')
    core_plugin.__path__ = []
    _module('core.plugin.web_pages', register_route=lambda *_args, **_kwargs: None)

    _module(f'{app_package}.aiconfig', enabled=lambda: True)
    _module(f'{app_package}.agent')
    _module(f'{app_package}.central')

    name = f'{app_package}.webpanel'
    spec = importlib.util.spec_from_file_location(name, ROOT / 'ai_dev' / 'app' / 'webpanel.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Store:
    def __init__(self):
        self.events = []

    def ensure_session(self, session_id):
        return {'id': session_id or 'created-session'}

    def add_event(self, event_type, data, session_id=''):
        self.events.append({'type': event_type, 'data': data, 'session_id': session_id})


def _payload(response):
    return json.loads(response.body.decode('utf-8'))


def test_chat_runs_in_background_and_is_idempotent(monkeypatch):
    async def scenario():
        webpanel = _load_webpanel()
        jobs = {}
        store = _Store()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def run_agent(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {'ok': True, 'message': 'done', 'iterations': 3}

        body = {
            'session_id': 'session-1',
            'request_id': 'request-1',
            'message': 'build it',
            'model': '',
            'images': [],
            'mode': 'dev',
        }

        async def read_json(_request):
            return body

        monkeypatch.setattr(webpanel, '_store', lambda: store)
        monkeypatch.setattr(webpanel, '_jobs', lambda: jobs)
        monkeypatch.setattr(webpanel, '_json', read_json)
        monkeypatch.setattr(webpanel.agentmod, 'run_agent', run_agent, raising=False)

        first = await webpanel._post_chat(object())
        assert first.status == 202
        assert _payload(first)['status'] == 'running'
        await asyncio.wait_for(started.wait(), timeout=1)

        duplicate = await webpanel._post_chat(object())
        assert duplicate.status == 202
        assert calls == 1

        body['request_id'] = 'request-2'
        conflict = await webpanel._post_chat(object())
        assert conflict.status == 409
        assert calls == 1

        release.set()
        await asyncio.wait_for(jobs['session-1']['task'], timeout=1)
        assert jobs['session-1']['status'] == 'completed'
        assert jobs['session-1']['result'] == {
            'success': True,
            'message': 'done',
            'reasoning': '',
            'iterations': 3,
        }

    asyncio.run(scenario())


def test_background_failure_is_available_to_reconnected_client(monkeypatch):
    async def scenario():
        webpanel = _load_webpanel()
        jobs = {}
        store = _Store()

        async def run_agent(*_args, **_kwargs):
            raise RuntimeError('upstream failed')

        async def read_json(_request):
            return {
                'session_id': 'session-2', 'request_id': 'request-2',
                'message': 'check it', 'images': [], 'mode': 'analyze',
            }

        monkeypatch.setattr(webpanel, '_store', lambda: store)
        monkeypatch.setattr(webpanel, '_jobs', lambda: jobs)
        monkeypatch.setattr(webpanel, '_json', read_json)
        monkeypatch.setattr(webpanel.agentmod, 'run_agent', run_agent, raising=False)

        accepted = await webpanel._post_chat(object())
        assert accepted.status == 202
        await asyncio.wait_for(jobs['session-2']['task'], timeout=1)

        view = webpanel._job_view('session-2')
        assert view['status'] == 'failed'
        assert view['result']['success'] is False
        assert 'RuntimeError: upstream failed' in view['result']['message']

    asyncio.run(scenario())
