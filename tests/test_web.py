# -*- coding: utf-8 -*-
"""The web console (assist/web.py, webapp.py, web/*) and the per-command log (assist/cmdlog.py).

No model provider is called: the commands the console starts run with R3_NO_LLM=1, and the one test that
needs a configured key blocks urllib. Standard library only:

    python -m unittest tests.test_web -v
"""
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from assist import cmdlog, web  # noqa: E402
from assist.common import trim_log  # noqa: E402
from assist.store import Store  # noqa: E402
from test_assist import write_pack  # noqa: E402

ASSIST = os.path.join(ROOT, 'assist.py')
KEY_NAMES = ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_KEY', 'OPENAI_API_KEY')


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('R3_') and k.upper() not in KEY_NAMES}
    env.update(extra)
    return env


def write_jsonl(path, rows):
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r)) + '\n')


class EnvGuard(object):
    """Mixin: App() writes R3_PACK / R3_STORE into os.environ, so put the environment back afterwards."""

    def snapshot_env(self):
        self._env = dict(os.environ)

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)


# ------------------------------------------------------------------ HTTP layer and commands
class ConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env = dict(os.environ)
        cls.root = tempfile.mkdtemp(prefix='r3web_')
        cls.pack = os.path.join(cls.root, 'pack')
        cls.store = os.path.join(cls.root, 'store')
        cls.runs = os.path.join(cls.root, 'runs')
        write_pack(cls.pack)
        for k in list(os.environ):
            if k.upper().startswith('R3_') or k.upper() in KEY_NAMES:
                del os.environ[k]
        os.environ['R3_NO_LLM'] = '1'
        os.environ['R3_DOTENV'] = os.path.join(cls.root, 'no.env')
        cls.cli_env = clean_env(R3_PACK=cls.pack, R3_STORE=cls.store, R3_NO_LLM='1', R3_DOTENV=os.path.join(cls.root, 'no.env'))
        for d in (1, 2, 3):
            cls.cli(['ingest', '--day', str(d)])
        cls.app = web.App(pack=cls.pack, store=cls.store, runs=cls.runs)
        cls.srv = web.serve(cls.app, '127.0.0.1', 0)
        cls.port = cls.srv.server_address[1]
        cls.host = '127.0.0.1:%d' % cls.port
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.environ.clear()
        os.environ.update(cls._env)
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def cli(cls, args):
        p = subprocess.run([sys.executable, ASSIST] + args, cwd=cls.root, env=cls.cli_env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=120)
        assert p.returncode == 0, p.stderr.decode('utf-8', 'replace')
        return json.loads(p.stdout.decode('utf-8'))

    # ---- plumbing
    def req(self, method, path, body=None, token=True, host=None, headers=None):
        hdrs = {'Host': host or self.host}
        if token is True:
            hdrs['X-R3-Token'] = self.app.token
        elif token:
            hdrs['X-R3-Token'] = token
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            hdrs['Content-Type'] = 'application/json'
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=120)
        try:
            conn.request(method, path, body=body.encode('utf-8') if isinstance(body, str) else body, headers=hdrs)
            r = conn.getresponse()
            raw = r.read()
            return r.status, dict((k.lower(), v) for k, v in r.getheaders()), raw
        finally:
            conn.close()

    def get_json(self, path, **kw):
        status, _, raw = self.req('GET', path, **kw)
        return status, json.loads(raw.decode('utf-8'))

    def post_run(self, body, **kw):
        status, _, raw = self.req('POST', '/api/run', body=body, **kw)
        return status, json.loads(raw.decode('utf-8'))

    # ---- pages, token, Host
    def test_pages_carry_this_servers_token_and_a_csp(self):
        for path in ('/', '/console', '/metrics'):
            status, h, raw = self.req('GET', path, token=False)
            text = raw.decode('utf-8')
            self.assertEqual(status, 200, path)
            self.assertIn('text/html', h['content-type'])
            self.assertNotIn('{{TOKEN}}', text)
            self.assertIn('<meta name="r3-token" content="%s">' % self.app.token, text)
            self.assertIn("script-src 'self'", h['content-security-policy'])
            self.assertEqual(h['cache-control'], 'no-store')
            self.assertEqual(h['x-frame-options'], 'DENY')

    def test_every_new_start_has_its_own_token(self):
        other = web.App(pack=self.pack, store=self.store, runs=self.runs)
        self.assertNotEqual(other.token, self.app.token)
        self.assertGreaterEqual(len(self.app.token), 24)

    def test_an_unknown_host_header_is_refused_everywhere(self):
        for path in ('/console', '/static/app.css', '/api/info'):
            status, _, _ = self.req('GET', path, host='evil.example:%d' % self.port)
            self.assertEqual(status, 403, path)
        status, _, _ = self.req('GET', '/console', host='evil.example')
        self.assertEqual(status, 403)
        status, _, _ = self.req('GET', '/console', host='localhost:%d' % self.port, token=False)
        self.assertEqual(status, 200)

    def test_api_needs_the_token(self):
        for tok in (False, 'nope', self.app.token[:-1]):
            status, body = self.get_json('/api/info', token=tok)
            self.assertEqual(status, 403)
            self.assertNotIn('pack', body)
        status, _ = self.get_json('/api/info')
        self.assertEqual(status, 200)

    def test_only_whitelisted_static_files_are_served(self):
        for name, ctype in (('app.css', 'text/css'), ('common.js', 'text/javascript'), ('console.js', 'text/javascript'),
                            ('metrics.js', 'text/javascript')):
            status, h, raw = self.req('GET', '/static/' + name, token=False)
            self.assertEqual(status, 200, name)
            self.assertIn(ctype, h['content-type'])
            self.assertTrue(raw)
        for bad in ('/static/console.html', '/static/../assist.py', '/static/..%2Fassist.py', '/static/%2e%2e%2fassist.py',
                    '/static/..\\assist.py', '/static/', '/static/nothing.js', '/static/.env', '/static/app.css/x'):
            status, _, raw = self.req('GET', bad, token=False)
            self.assertEqual(status, 404, bad)
            self.assertNotIn(b'import ', raw)

    def test_other_methods_and_unknown_paths(self):
        self.assertEqual(self.req('PUT', '/api/run', body='{}')[0], 405)
        self.assertEqual(self.req('DELETE', '/api/info')[0], 405)
        self.assertEqual(self.req('GET', '/nowhere', token=False)[0], 404)
        self.assertEqual(self.req('GET', '/api/nothing')[0], 404)
        self.assertEqual(self.req('POST', '/api/info', body='{}')[0], 404)
        self.assertEqual(self.req('POST', '/console', body='{}', token=False)[0], 404)

    # ---- POST safety
    def test_post_origin_must_match_the_host(self):
        body = {'cmd': 'nope'}
        status, out = self.post_run(body, headers={'Origin': 'http://evil.example'})
        self.assertEqual(status, 403)
        self.assertIn('cross-origin', out['error'])
        status, out = self.post_run(body, headers={'Origin': 'http://127.0.0.1:1'})
        self.assertEqual(status, 403)
        status, out = self.post_run(body, headers={'Origin': 'http://' + self.host})
        self.assertEqual(status, 400)  # got past the origin check, then failed validation
        status, out = self.post_run(body, token=False)
        self.assertEqual(status, 403)

    def test_post_body_limits_and_shapes(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        conn.putrequest('POST', '/api/run', skip_host=True)
        conn.putheader('Host', self.host)
        conn.putheader('X-R3-Token', self.app.token)
        conn.putheader('Content-Length', str(web.MAX_BODY + 1))
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual(r.status, 413)
        conn.close()
        status, _, raw = self.req('POST', '/api/run', body='not json')
        self.assertEqual(status, 400)
        status, _, raw = self.req('POST', '/api/run', body='[1, 2]')
        self.assertEqual(status, 400)
        status, _, raw = self.req('POST', '/api/run', body='', headers={'Content-Length': 'abc'})
        self.assertEqual(status, 400)

    # ---- read-only API
    def test_info_describes_the_pack_and_the_model_without_secrets(self):
        status, info = self.get_json('/api/info')
        self.assertEqual(status, 200)
        self.assertEqual(info['last_day'], 3)
        self.assertEqual(info['days_in_pack'], [1, 2, 3])
        ids = [p['id'] for p in info['people']]
        self.assertIn('a02', ids)
        self.assertIn('b01', ids)
        self.assertFalse(info['llm']['enabled'])
        self.assertTrue(info['llm']['off_by_switch'])
        self.assertEqual(info['limits']['brief_tokens'], 8000)

    def test_info_never_returns_a_key_or_dotenv_content(self):
        env_file = os.path.join(self.root, 'secret.env')
        with open(env_file, 'w', encoding='utf-8') as f:
            f.write('OPENAI_KEY=sentinel-openai-0123456789\nSOME_OTHER=sentinel-other-0123456789\n')
        saved = dict(os.environ)
        try:
            os.environ.pop('R3_NO_LLM', None)
            os.environ['R3_DOTENV'] = env_file
            os.environ['GEMINI_API_KEY'] = 'sentinel-gemini-0123456789'
            with mock.patch('urllib.request.urlopen', side_effect=AssertionError('no network in this test')):
                status, _, raw = self.req('GET', '/api/info')
        finally:
            os.environ.clear()
            os.environ.update(saved)
        text = raw.decode('utf-8')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(text)['llm']['enabled'])
        for secret in ('sentinel-gemini', 'sentinel-openai', 'sentinel-other', 'secret.env'):
            self.assertNotIn(secret, text)

    def test_meetings_listing(self):
        status, data = self.get_json('/api/meetings?person=a02')
        self.assertEqual(status, 200)
        by_id = dict((m['event_id'], m) for m in data['meetings'])
        self.assertIn('ev-ac-02', by_id)
        self.assertEqual(by_id['ev-ac-02']['date'], '2027-03-23')
        self.assertEqual(by_id['ev-ac-02']['client_name'], 'Acme Corp')
        status, data = self.get_json('/api/meetings?person=b01')
        self.assertEqual(status, 200)
        self.assertEqual(data['meetings'], [])
        for bad in ('', 'zz99', '../x', 'a02%3Bls', 'a02%20b'):  # (';' encoded: Python 3.8.5 splits queries on it)
            self.assertEqual(self.get_json('/api/meetings?person=' + bad)[0], 400, bad)
        self.assertEqual(self.get_json('/api/meetings')[0], 400)

    def test_record_viewer_uses_the_same_gate_as_the_cli(self):
        def code(person, rid):
            return self.get_json('/api/record?person=%s&id=%s' % (person, rid))

        status, rec = code('a02', 'xev02')  # team record, in the person's org
        self.assertEqual(status, 200)
        self.assertEqual(rec['type'], 'event')
        self.assertEqual(rec['author'], 'a02')
        self.assertEqual(rec['author_name'], 'Ravi Menon')
        status, rec = code('a03', 'xev02')
        self.assertEqual(status, 200)
        # someone else's private note, a restricted note outside the readers, another company's record, a forgotten one
        unknown = code('a03', 'no-such-record')
        self.assertEqual(unknown[0], 404)
        denied = [code('a03', 'xpriv1'), code('a01', 'xpriv1'), code('a03', 'xres1'), code('b01', 'xev02'),
                  code('a02', 'xbw1'), code('a02', 'xpriv1')]
        for status, body in denied:
            self.assertEqual(status, 404)
            self.assertEqual(body, unknown[1])
        self.assertEqual(code('a02', 'no-such-record')[0], 404)
        for who in ('a01', 'a02'):  # the readers of the restricted note
            self.assertEqual(code(who, 'xres1')[0], 200, who)
        for bad in ('../x', 'a/b', 'x' * 65, '', '%00'):
            self.assertEqual(code('a02', bad)[0], 400, bad)
        self.assertEqual(self.get_json('/api/record?person=zz99&id=xev02')[0], 400)

    def test_denied_and_unknown_records_look_identical(self):
        a = self.get_json('/api/record?person=a03&id=xpriv1')
        b = self.get_json('/api/record?person=a03&id=nonexistent')
        self.assertEqual(a, b)

    # ---- running commands
    def test_validation_rejects_bad_arguments(self):
        good_ask = {'cmd': 'ask', 'person': 'a02', 'at': '2027-03-24T18:00:00Z', 'ask': 'Who owns billing for Acme?'}
        bad = [
            {}, {'cmd': 'shell'}, {'cmd': 'brief'}, {'cmd': ['brief']},
            {'cmd': 'brief', 'person': 'zz99', 'date': '2027-03-23'},
            {'cmd': 'brief', 'person': 'a02;ls', 'date': '2027-03-23'},
            {'cmd': 'brief', 'person': 'a02', 'date': '2027-13-45'},
            {'cmd': 'brief', 'person': 'a02', 'date': '2027-3-1'},
            {'cmd': 'brief', 'person': 'a02', 'date': 20270323},
            dict(good_ask, at='2027-03-24 18:00'), dict(good_ask, at='2027-03-24T18:00:00'), dict(good_ask, at='2027-02-30T10:00:00Z'),
            dict(good_ask, ask=''), dict(good_ask, ask='   '), dict(good_ask, ask='x' * 501), dict(good_ask, ask=['a']),
            {'cmd': 'forget', 'person': 'a02'}, {'cmd': 'forget', 'person': 'a02', 'what': ''},
            {'cmd': 'forget', 'person': 'nobody', 'what': 'x'},
            {'cmd': 'ingest'}, {'cmd': 'ingest', 'day': 0}, {'cmd': 'ingest', 'day': -1}, {'cmd': 'ingest', 'day': 1000},
            {'cmd': 'ingest', 'day': True}, {'cmd': 'ingest', 'day': '2'}, {'cmd': 'ingest', 'day': 2.5},
            {'cmd': 'ingest', 'day': 99},  # whole number, but not in the pack
        ]
        for body in bad:
            status, out = self.post_run(body)
            self.assertEqual(status, 400, body)
            self.assertIn('error', out)

    def test_ingest_day_1_needs_a_confirmation_and_changes_nothing_without_it(self):
        before = self.get_json('/api/info')[1]['last_day']
        for extra in ({}, {'confirm_reset': False}, {'confirm_reset': 'yes'}, {'confirm_reset': 1}):
            status, out = self.post_run(dict({'cmd': 'ingest', 'day': 1}, **extra))
            self.assertEqual(status, 400, extra)
            self.assertIn('confirm', out['error'])
        self.assertEqual(self.get_json('/api/info')[1]['last_day'], before)
        self.assertEqual(self.app.build_argv({'cmd': 'ingest', 'day': 1, 'confirm_reset': True}), ['ingest', '--day', '1'])

    def test_build_argv_is_a_list_with_no_shell_in_it(self):
        nasty = '"; echo pwned #`id` $(id) | rm -rf / & \x00\x07 "'
        argv = self.app.build_argv({'cmd': 'ask', 'person': 'a02', 'at': '2027-03-24T18:00:00Z', 'ask': nasty})
        self.assertEqual(argv[:5], ['ask', '--person', 'a02', '--at', '2027-03-24T18:00:00Z'])
        self.assertEqual(argv[5], '--ask')
        self.assertIn('echo pwned', argv[6])  # kept as plain text, as one argument
        self.assertNotIn('\x00', argv[6])
        self.assertNotIn('\x07', argv[6])
        self.assertEqual(self.app.build_argv({'cmd': 'brief', 'person': 'a02', 'date': '2027-03-23', 'summary': True})[-1], '--summary')
        self.assertNotIn('--summary', self.app.build_argv({'cmd': 'brief', 'person': 'a02', 'date': '2027-03-23'}))

    def test_a_brief_through_the_web_equals_the_cli(self):
        status, out = self.post_run({'cmd': 'brief', 'person': 'a02', 'date': '2027-03-23'})
        self.assertEqual(status, 200)
        self.assertEqual(out['exit'], 0)
        self.assertTrue(out['valid_json'])
        self.assertEqual(out['cli'], 'python assist.py brief --person a02 --date 2027-03-23')
        direct = self.cli(['brief', '--person', 'a02', '--date', '2027-03-23'])
        self.assertEqual(out['result'], direct)
        self.assertTrue(direct, 'the made-up pack has a meeting that day, so this is not a vacuous comparison')

    def test_summary_flag_with_the_model_off_keeps_the_normal_brief(self):
        status, plain = self.post_run({'cmd': 'brief', 'person': 'a02', 'date': '2027-03-23'})
        status, summ = self.post_run({'cmd': 'brief', 'person': 'a02', 'date': '2027-03-23', 'summary': True})
        self.assertEqual(status, 200)
        self.assertTrue(summ['cli'].endswith('--summary'))
        self.assertTrue(summ['result'])
        for b in summ['result']:
            self.assertIn('summary', b)
            self.assertIsNone(b['summary'])
        def core(briefs):  # 'usage' carries this call's own timing, so it differs between any two runs
            return [dict((k, v) for k, v in b.items() if k not in ('summary', 'usage')) for b in briefs]
        self.assertEqual(core(summ['result']), core(plain['result']))
        self.assertEqual(summ['result'][0]['usage']['input_tokens'], 0)

    def test_an_ask_through_the_web_is_logged_without_the_question(self):
        marker = 'marker-qzx-7731'
        q = 'Who owns billing for Acme? (%s)' % marker
        status, out = self.post_run({'cmd': 'ask', 'person': 'a02', 'at': '2027-03-24T18:00:00Z', 'ask': q})
        self.assertEqual(status, 200)
        self.assertEqual(out['exit'], 0)
        self.assertTrue(out['valid_json'])
        self.assertIsInstance(out['result'], dict)
        self.assertIn('"%s"' % q, out['cli'])
        with open(cmdlog.path(), encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn(marker, raw)
        last = json.loads(raw.strip().splitlines()[-1])
        self.assertEqual(last['cmd'], 'ask')
        self.assertEqual(last['via'], 'web')
        self.assertEqual(last['status'], 'ok')
        self.assertEqual(last['person'], 'a02')
        self.assertEqual(last['ask_len'], len(q))
        self.assertRegex(last['ask_id'], r'^[0-9a-f]{8}$')
        self.assertNotIn('ask', last)

    def test_forget_through_the_web_logs_no_text(self):
        marker = 'ppqzz5521'  # matches no real record, so the forget cannot disturb the other tests
        status, out = self.post_run({'cmd': 'forget', 'person': 'a03', 'what': marker})
        self.assertEqual(status, 200)
        self.assertEqual(out['exit'], 0)
        with open(cmdlog.path(), encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn(marker, raw)
        self.assertEqual(json.loads(raw.strip().splitlines()[-1])['cmd'], 'forget')

    def test_a_second_command_while_one_runs_is_refused_not_queued_forever(self):
        self.app.run_lock.acquire()
        try:
            with mock.patch.object(web, 'RUN_TIMEOUT_S', -4.9):
                with self.assertRaises(web.ApiError) as cm:
                    self.app.run({'cmd': 'brief', 'person': 'a02', 'date': '2027-03-23'})
            self.assertEqual(cm.exception.status, 429)
        finally:
            self.app.run_lock.release()

    # ---- metrics and logs over HTTP
    def test_metrics_and_logs_endpoints_answer(self):
        self.post_run({'cmd': 'brief', 'person': 'a02', 'date': '2027-03-23'})
        status, m = self.get_json('/api/metrics')
        self.assertEqual(status, 200)
        self.assertGreaterEqual(m['commands']['n'], 4)  # three ingests plus whatever ran above
        self.assertIn('ingest', m['commands']['by_cmd'])
        status, logs = self.get_json('/api/logs?stream=commands&limit=3')
        self.assertEqual(status, 200)
        self.assertLessEqual(len(logs['rows']), 3)
        self.assertEqual(self.get_json('/api/logs?stream=other')[0], 400)
        self.assertEqual(self.get_json('/api/logs?limit=abc')[0], 400)

    def test_runs_endpoints_reject_path_tricks(self):
        for bad in ('..', '../x', '..%2F..%2Fx', 'a%20b', '', 'x' * 81, '..%5Cx'):
            self.assertEqual(self.get_json('/api/run-detail?name=' + bad)[0], 400, bad)
        self.assertEqual(self.get_json('/api/run-detail?name=nope')[0], 404)
        self.assertEqual(self.get_json('/api/run-detail')[0], 400)
        status, data = self.get_json('/api/runs')
        self.assertEqual(status, 200)
        self.assertEqual(data['runs'], [])


# ------------------------------------------------------------------ metrics maths, on made-up logs
class MetricsTests(EnvGuard, unittest.TestCase):
    def setUp(self):
        self.snapshot_env()
        self.root = tempfile.mkdtemp(prefix='r3met_')
        self.pack = os.path.join(self.root, 'pack')
        self.store = os.path.join(self.root, 'store')
        self.runs = os.path.join(self.root, 'runs')
        os.makedirs(self.store)
        write_pack(self.pack)
        self.app = web.App(pack=self.pack, store=self.store, runs=self.runs)

    def tearDown(self):
        self.restore_env()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_percentiles_and_stats(self):
        self.assertIsNone(web.percentile([], 50))
        self.assertEqual(web.percentile([5], 99), 5)
        self.assertEqual(web.percentile([100, 200, 300, 400, 1000], 50), 300)
        self.assertEqual(web.percentile([100, 200, 300, 400, 1000], 90), 1000)
        s = web.ms_stats([300, None, 'x', 100, 200])
        self.assertEqual((s['n'], s['p50'], s['max']), (3, 200, 300))
        self.assertEqual(web.ms_stats([])['n'], 0)
        self.assertIsNone(web.ms_stats([None])['max'])

    def test_metrics_from_made_up_logs(self):
        write_jsonl(os.path.join(self.store, 'commands.jsonl'), [
            {'ts': '2027-01-01T00:00:01Z', 'cmd': 'ask', 'via': 'web', 'status': 'ok', 'ms': 100, 'cache': 'miss', 'kind': 'answer',
             'tokens_in': 10, 'tokens_out': 2, 'cost_usd': 0.001},
            {'ts': '2027-01-01T00:00:02Z', 'cmd': 'ask', 'via': 'cli', 'status': 'ok', 'ms': 200, 'cache': 'hit', 'kind': 'clarify'},
            {'ts': '2027-01-01T00:00:03Z', 'cmd': 'ask', 'via': 'cli', 'status': 'watchdog', 'ms': 100000, 'cache': None, 'kind': 'nothing'},
            {'ts': '2027-01-01T00:00:04Z', 'cmd': 'brief', 'via': 'cli', 'status': 'fallback', 'ms': 50, 'cache': 'miss'},
            {'ts': '2027-01-01T00:00:05Z', 'cmd': 'brief', 'via': 'cli', 'status': 'ok', 'ms': 400, 'cache': 'hit', 'meetings': 2,
             'briefs': 1, 'changes': 3, 'summaries': 1},
            'this line is not json', '[1, 2]', '',
        ])
        write_jsonl(os.path.join(self.store, 'usage.jsonl'), [
            {'purpose': 'promises', 'provider': 'gemini', 'model': 'm1', 'ok': True, 'ms': 800, 'input_tokens': 100, 'output_tokens': 20,
             'cost_usd': 0.002},
            {'purpose': 'promises', 'provider': 'gemini', 'model': 'm1', 'ok': False, 'ms': 25000, 'input_tokens': 0, 'output_tokens': 0,
             'cost_usd': 0.0, 'error': 'HTTP 503'},
            {'purpose': 'summary', 'provider': 'openai', 'model': 'm2', 'ok': True, 'ms': 1200, 'input_tokens': 50, 'output_tokens': 10,
             'cost_usd': 0.001},
            {'purpose': 'summary', 'provider': 'openai', 'model': 'm2', 'ok': False, 'ms': 10, 'error': 'HTTP 503'},
        ])
        m = self.app.metrics()
        c = m['commands']
        self.assertEqual(c['n'], 5)  # the three bad lines are skipped
        self.assertEqual(c['status'], {'ok': 3, 'fallback': 1, 'watchdog': 1})
        self.assertEqual(c['cache'], {'hits': 2, 'lookups': 4, 'rate': 0.5})
        self.assertEqual(c['ask_kinds'], {'answer': 1, 'clarify': 1, 'nothing': 1})
        self.assertEqual(c['via'], {'web': 1, 'cli': 4})
        self.assertEqual(c['by_cmd']['ask']['n'], 3)
        self.assertEqual(c['by_cmd']['ask']['watchdog'], 1)
        self.assertEqual(c['by_cmd']['ask']['ms']['max'], 100000)
        self.assertEqual(c['briefs'], {'meetings': 2, 'with_changes': 1, 'changes': 3, 'summaries': 1})
        self.assertEqual((c['tokens_in'], c['tokens_out']), (10, 2))
        self.assertEqual(len(c['timeline']), 5)
        md = m['model']
        self.assertEqual((md['n'], md['ok'], md['fail']), (4, 2, 2))
        self.assertEqual((md['tokens_in'], md['tokens_out']), (150, 30))
        self.assertAlmostEqual(md['cost_usd'], 0.003, places=6)
        self.assertEqual(md['by_purpose']['promises']['fail'], 1)
        self.assertEqual(md['by_purpose']['summary']['tokens_in'], 50)
        self.assertEqual(sorted(md['by_model']), ['gemini / m1', 'openai / m2'])
        self.assertEqual(md['errors'], [{'error': 'HTTP 503', 'n': 2}])

    def test_metrics_on_an_empty_store_do_not_divide_by_zero(self):
        m = self.app.metrics()
        self.assertEqual(m['commands']['n'], 0)
        self.assertIsNone(m['commands']['cache']['rate'])
        self.assertIsNone(m['commands']['ms']['p50'])
        self.assertEqual(m['model']['n'], 0)
        self.assertEqual(self.app.logs('commands', 10)['rows'], [])

    def test_log_filters(self):
        write_jsonl(os.path.join(self.store, 'commands.jsonl'), [
            {'cmd': 'ask', 'status': 'ok', 'person': 'a01', 'ms': 1}, {'cmd': 'brief', 'status': 'ok', 'person': 'a02', 'ms': 2},
            {'cmd': 'ask', 'status': 'watchdog', 'person': 'a03', 'ms': 3}, {'cmd': 'ask', 'status': 'ok', 'person': 'a04', 'ms': 4},
        ])
        write_jsonl(os.path.join(self.store, 'usage.jsonl'), [
            {'purpose': 'summary', 'ok': True, 'ms': 1}, {'purpose': 'promises', 'ok': False, 'ms': 2, 'error': 'boom'},
            {'purpose': 'promises', 'ok': True, 'ms': 3}])
        rows = self.app.logs('commands', 100)['rows']
        self.assertEqual([r['person'] for r in rows], ['a04', 'a03', 'a02', 'a01'], 'newest first')
        self.assertEqual(len(self.app.logs('commands', 2)['rows']), 2)
        self.assertEqual(len(self.app.logs('commands', 0)['rows']), 1, 'the limit is clamped to at least one row')
        self.assertEqual([r['person'] for r in self.app.logs('commands', 100, cmd='ask')['rows']], ['a04', 'a03', 'a01'])
        self.assertEqual([r['person'] for r in self.app.logs('commands', 100, status='watchdog')['rows']], ['a03'])
        self.assertEqual([r['person'] for r in self.app.logs('commands', 100, query='A02')['rows']], ['a02'])
        self.assertEqual(len(self.app.logs('usage', 100, status='fail')['rows']), 1)
        self.assertEqual(len(self.app.logs('usage', 100, status='ok')['rows']), 2)
        self.assertEqual(len(self.app.logs('usage', 100, cmd='promises')['rows']), 2)
        with self.assertRaises(web.ApiError):
            self.app.logs('secrets', 10)

    def test_jsonl_tail_reads_only_the_end_of_a_big_file(self):
        p = os.path.join(self.root, 'big.jsonl')
        write_jsonl(p, [{'i': i, 'pad': 'x' * 100} for i in range(5000)])
        rows = web.read_jsonl_tail(p, 10, max_bytes=20000)
        self.assertEqual([r['i'] for r in rows], list(range(4990, 5000)))
        self.assertEqual(web.read_jsonl_tail(os.path.join(self.root, 'missing.jsonl'), 10), [])

    # ---- harness runs
    def make_run(self, name='r1'):
        d = os.path.join(self.runs, name)
        os.makedirs(d)
        write_jsonl(os.path.join(d, 'log.jsonl'), [
            {'day': 1, 'kind': 'ingest', 'args': ['ingest', '--day', '1'], 'exit': 0, 'valid': True, 'ms': 900},
            {'day': 1, 'kind': 'ask', 'args': ['ask', '--person', 'p02', '--ask', 'Who owns billing?'], 'exit': 0, 'valid': True, 'ms': 300},
            {'day': 2, 'kind': 'ask', 'args': ['ask'], 'exit': 1, 'valid': False, 'ms': 5000, 'stderr_tail': 'Traceback boom'},
        ])
        with open(os.path.join(d, 'score.json'), 'w', encoding='utf-8') as f:
            json.dump({'total': 80, 'max_scored': 85, 'gate': 1, 'usefulness': 0.9, 'days': 2,
                       'areas': {'Honesty': {'got': 15, 'max': 15}}, 'per_day': {'1': 0.9},
                       'items': [{'id': 'q-good', 'kind': 'ask', 'day': 1, 'person': 'p02', 'score': 1.0, 'honesty': 1.0},
                                 {'id': 'q-bad', 'kind': 'ask', 'day': 2, 'person': 'p03', 'score': 0.25, 'honesty': 0.0,
                                  'reasons': ['missed the owner'], 'pens': ['made something up']}]}, f)
        return d

    def test_runs_listing_and_detail(self):
        self.make_run('r1')
        os.makedirs(os.path.join(self.runs, 'has space'))
        write_jsonl(os.path.join(self.runs, 'has space', 'log.jsonl'), [{'ms': 1}])
        os.makedirs(os.path.join(self.runs, 'no-log'))
        listing = self.app.runs()
        self.assertEqual([r['name'] for r in listing['runs']], ['r1'])
        self.assertEqual(listing['runs'][0]['total'], 80)
        d = self.app.run_detail('r1')
        self.assertEqual(d['calls']['n'], 3)
        self.assertEqual(d['calls']['by_kind']['ask']['failed'], 1)
        self.assertEqual(d['calls']['failures'][0]['stderr_tail'], 'Traceback boom')
        self.assertEqual(d['calls']['slowest'][0]['ms'], 5000)
        self.assertEqual(d['calls']['per_day_ms'], {'1': 1200, '2': 5000})
        self.assertEqual(d['score']['worst'][0]['id'], 'q-bad')
        self.assertIn('missed the owner', d['score']['worst'][0]['why'])
        self.assertIn('made something up', d['score']['worst'][0]['why'])
        for bad in ('..', '../r1', 'r1/..', 'has space', '', None, 5):
            with self.assertRaises(web.ApiError) as cm:
                self.app.run_detail(bad)
            self.assertEqual(cm.exception.status, 400, bad)
        with self.assertRaises(web.ApiError) as cm:
            self.app.run_detail('nope')
        self.assertEqual(cm.exception.status, 404)

    def test_a_run_without_a_score_still_shows_its_calls(self):
        d = self.make_run('r2')
        os.remove(os.path.join(d, 'score.json'))
        detail = self.app.run_detail('r2')
        self.assertIsNone(detail['score'])
        self.assertEqual(detail['calls']['n'], 3)


# ------------------------------------------------------------------ the command log
class CommandLogTests(EnvGuard, unittest.TestCase):
    def setUp(self):
        self.snapshot_env()
        self.root = tempfile.mkdtemp(prefix='r3log_')
        os.environ['R3_STORE'] = os.path.join(self.root, 'store')
        os.environ.pop('R3_SOURCE', None)
        os.environ.pop('R3_LOG_ASK_TEXT', None)

    def tearDown(self):
        self.restore_env()
        shutil.rmtree(self.root, ignore_errors=True)

    def args(self, **kw):
        base = dict(cmd='ask', person='a02', date=None, at='2027-03-24T18:00:00Z', day=None, ask=None, what=None, summary=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_an_ask_entry_has_numbers_and_an_id_but_no_text(self):
        out = {'answer': {'text': 'Priya owns billing.', 'sources': ['r1', 'r2']}, 'matched': [],
               'usage': {'input_tokens': 40, 'output_tokens': 5, 'cost_usd': 0.0004}}
        e = cmdlog.entry_for(self.args(ask='Who owns billing for Acme?'), out, 'ok', 123.9, 'miss')
        self.assertEqual((e['cmd'], e['via'], e['status'], e['ms'], e['cache']), ('ask', 'cli', 'ok', 123, 'miss'))
        self.assertEqual((e['kind'], e['sources'], e['tokens_in'], e['tokens_out']), ('answer', 2, 40, 5))
        self.assertEqual(e['ask_len'], len('Who owns billing for Acme?'))
        self.assertRegex(e['ask_id'], r'^[0-9a-f]{8}$')
        blob = json.dumps(e)
        for leaked in ('Who owns', 'Priya', 'billing'):
            self.assertNotIn(leaked, blob)
        self.assertNotIn('ask', e)
        self.assertEqual(cmdlog.entry_for(self.args(ask='same'), out, 'ok', 1)['ask_id'],
                         cmdlog.entry_for(self.args(ask='same'), out, 'ok', 2)['ask_id'])

    def test_ask_kinds(self):
        a = self.args(ask='q')
        kind = lambda out: cmdlog.entry_for(a, out, 'ok', 1)['kind']  # noqa: E731
        self.assertEqual(kind({'answer': {'text': 'I have nothing on that.', 'sources': []}}), 'nothing')
        self.assertEqual(kind({'clarify': {'question': 'Which Priya?'}, 'answer': None}), 'clarify')
        self.assertEqual(kind({'refused': True}), 'refused')
        self.assertEqual(kind({'answer': {'text': 'x', 'sources': ['r1']}}), 'answer')

    def test_ask_text_is_kept_only_when_asked_for_and_cut_short(self):
        os.environ['R3_LOG_ASK_TEXT'] = '1'
        e = cmdlog.entry_for(self.args(ask='q' * 500), {'answer': None}, 'ok', 1)
        self.assertEqual(e['ask'], 'q' * 200)

    def test_brief_forget_and_ingest_entries(self):
        out = [{'status': 'brief', 'changed': [1, 2], 'promises': [1], 'summary': {'text': 't', 'sources': ['r']}},
               {'status': 'nothing_new', 'changed': [], 'promises': [], 'summary': None}]
        e = cmdlog.entry_for(self.args(cmd='brief', date='2027-03-23', at=None, summary=True), out, 'ok', 5)
        self.assertEqual((e['meetings'], e['briefs'], e['changes'], e['promises'], e['summaries']), (2, 1, 2, 1, 1))
        self.assertTrue(e['summary'])
        e = cmdlog.entry_for(self.args(cmd='brief', date='2027-03-23', at=None), [{'status': 'brief', 'changed': []}], 'ok', 5)
        self.assertNotIn('summaries', e)
        self.assertNotIn('summary', e)
        e = cmdlog.entry_for(self.args(cmd='forget', what='my secret trip', at=None), {'ok': True}, 'ok', 5)
        self.assertNotIn('secret', json.dumps(e))
        self.assertTrue(e['ok'])
        e = cmdlog.entry_for(self.args(cmd='ingest', day=3, person=None, at=None),
                             {'records': 40, 'feedback': 2, 'model_calls': 3, 'model_pending': {'promises': 2, 'x': 1}}, 'ok', 5)
        self.assertEqual((e['day'], e['records'], e['pending']), (3, 40, 3))
        self.assertNotIn('person', e)

    def test_the_source_comes_from_the_environment(self):
        os.environ['R3_SOURCE'] = 'web'
        self.assertEqual(cmdlog.entry_for(self.args(), {'answer': None}, 'ok', 1)['via'], 'web')

    def test_record_appends_lines_and_never_raises(self):
        cmdlog.record(self.args(ask='q'), {'answer': None}, 'ok', 5, 'miss')
        cmdlog.record(self.args(ask='q'), {'answer': None}, 'watchdog', 6)
        with open(cmdlog.path(), encoding='utf-8') as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual([r['status'] for r in rows], ['ok', 'watchdog'])
        # an unwritable location, a weird result and a broken args object must all be swallowed
        os.environ['R3_STORE'] = os.path.join(self.root, 'a-file')
        with open(os.environ['R3_STORE'], 'w') as f:
            f.write('not a folder')
        with mock.patch.object(cmdlog, 'log') as said:  # the skip message goes to stderr; keep the test output clean
            cmdlog.record(self.args(), {'answer': None}, 'ok', 1)
            os.environ['R3_STORE'] = os.path.join(self.root, 'store')
            cmdlog.record(object(), object(), 'ok', 'not a number')
            cmdlog.record(self.args(), {'usage': {'input_tokens': 'many'}}, 'ok', 1)
        self.assertEqual(said.call_count, 3)

    def test_trim_log_keeps_the_last_lines_and_only_when_over_the_limit(self):
        p = os.path.join(self.root, 'x.jsonl')
        write_jsonl(p, [{'i': i} for i in range(100)])
        size = os.path.getsize(p)
        trim_log(p, size, 10)  # not over the limit: untouched
        with open(p) as f:
            self.assertEqual(len(f.readlines()), 100)
        trim_log(p, size - 1, 10)
        with open(p) as f:
            rows = [json.loads(line)['i'] for line in f]
        self.assertEqual(rows, list(range(90, 100)))
        trim_log(os.path.join(self.root, 'absent.jsonl'), 1, 1)  # missing file: no error

    def test_the_log_is_bounded_while_commands_keep_appending(self):
        os.makedirs(os.environ['R3_STORE'])
        write_jsonl(cmdlog.path(), [{'i': i, 'pad': 'x' * 50} for i in range(60)])
        with mock.patch.object(cmdlog, 'MAX_BYTES', 1000), mock.patch.object(cmdlog, 'KEEP_LINES', 5):
            cmdlog.record(self.args(ask='q'), {'answer': None}, 'ok', 1)
        with open(cmdlog.path(), encoding='utf-8') as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[-1]['cmd'], 'ask')

    def test_rebuilding_the_store_starts_the_logs_afresh(self):
        s = Store()
        try:
            write_jsonl(s.commands_path, [{'cmd': 'ask'}])
            write_jsonl(s.usage_path, [{'purpose': 'x'}])
            s.reset()
            self.assertFalse(os.path.exists(s.commands_path))
            self.assertFalse(os.path.exists(s.usage_path))
        finally:
            s.close()


# ------------------------------------------------------------------ the pages themselves
class PageSourceTests(unittest.TestCase):
    """Cheap guards on the front-end files: record text is untrusted, and the CSP forbids inline script."""
    WEB = os.path.join(ROOT, 'web')

    def read(self, name):
        with open(os.path.join(self.WEB, name), encoding='utf-8') as f:
            return f.read()

    def test_scripts_never_parse_markup_or_run_strings(self):
        for name in ('common.js', 'console.js', 'metrics.js'):
            src = self.read(name)
            for pat in (r'\.innerHTML', r'outerHTML', r'insertAdjacentHTML', r'document\.write', r'\beval\s*\(',
                        r'new\s+Function\s*\(', r'setTimeout\s*\(\s*[\'"]', r'setInterval\s*\(\s*[\'"]'):
                self.assertIsNone(re.search(pat, src), '%s uses %s' % (name, pat))

    def test_pages_have_no_inline_script_or_handlers_and_carry_the_token_slot(self):
        for name in ('console.html', 'metrics.html'):
            src = self.read(name)
            self.assertIn('content="{{TOKEN}}"', src)
            self.assertIsNone(re.search(r'<script(?![^>]*\bsrc=)', src), name + ' has an inline script')
            self.assertIsNone(re.search(r'\son[a-z]+\s*=', src), name + ' has an inline event handler')
            self.assertNotIn('javascript:', src)
            for ref in re.findall(r'(?:src|href)="(/static/[^"]+)"', src):
                self.assertTrue(os.path.isfile(os.path.join(self.WEB, ref[len('/static/'):])), ref)

    def test_every_element_id_the_scripts_look_up_exists(self):
        for page, script in (('console.html', 'console.js'), ('metrics.html', 'metrics.js')):
            html, js = self.read(page), self.read(script)
            ids = set(re.findall(r'\bid="([^"]+)"', html))
            wanted = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", js)) | set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", js))
            self.assertEqual(sorted(wanted - ids), [], '%s looks up ids %s does not have' % (script, page))


class WebappScriptTests(unittest.TestCase):
    def test_a_pack_without_data_is_refused_before_anything_starts(self):
        root = tempfile.mkdtemp(prefix='r3wa_')
        try:
            p = subprocess.run([sys.executable, os.path.join(ROOT, 'webapp.py'), '--pack', root, '--port', '0'],
                               cwd=root, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('no data/', p.stderr.decode('utf-8', 'replace'))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_serve_allows_loopback_names_and_a_specific_bind_address_only(self):
        app = web.App.__new__(web.App)
        app.allowed_hosts = set()
        with mock.patch.object(web, 'Server') as server:
            server.return_value.server_address = ('0.0.0.0', 4321)
            web.serve(app, '0.0.0.0', 0)
            self.assertEqual(app.allowed_hosts, {'127.0.0.1:4321', 'localhost:4321', '[::1]:4321'})
            web.serve(app, '192.168.1.20', 0)
            self.assertIn('192.168.1.20:4321', app.allowed_hosts)
            self.assertIn('localhost:4321', app.allowed_hosts)
            self.assertNotIn('0.0.0.0:4321', app.allowed_hosts)


if __name__ == '__main__':
    unittest.main()
