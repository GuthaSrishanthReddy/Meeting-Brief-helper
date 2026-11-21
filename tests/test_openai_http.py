# -*- coding: utf-8 -*-
"""The OpenAI path, over real HTTP, against a strict local fake of OpenAI's API.

No OpenAI key exists on the machine that built this, so the real API has never been called. This fake
enforces the documented rules instead (Bearer auth, strict structured-output schema rules, parameter
values, the JSON-mode rule) and can refuse models and features or fail with 503, so what is tested
here is everything except the one thing only a real key can show: that OpenAI itself accepts the
request. Nothing here reaches the internet.
"""
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import assist.llm as llm_mod  # noqa: E402
from assist import ask, feedback, owners, promises, retrieve  # noqa: E402
from assist.llm import LLM, _env  # noqa: E402
from assist.store import Store  # noqa: E402
from test_assist import write_pack  # noqa: E402

CHAT_KEYS = {'model', 'messages', 'response_format', 'max_completion_tokens', 'reasoning_effort', 'temperature',
             'max_tokens', 'n', 'stop', 'seed'}
EFFORTS = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}
SCHEMA_TYPES = {'string', 'number', 'integer', 'boolean', 'object', 'array', 'null'}


def strict_problems(node, path='root'):
    """OpenAI strict mode: every object closed and fully required, only supported types."""
    out = []
    if isinstance(node, dict):
        t = node.get('type')
        if isinstance(t, str) and t not in SCHEMA_TYPES:
            out.append('%s: unsupported type %r' % (path, t))
        if t == 'object':
            props = node.get('properties')
            if not isinstance(props, dict):
                out.append('%s: object without properties' % path)
            else:
                if node.get('additionalProperties') is not False:
                    out.append('%s: additionalProperties must be false' % path)
                if sorted(node.get('required') or []) != sorted(props):
                    out.append('%s: every property must be required' % path)
                for k, v in props.items():
                    out += strict_problems(v, '%s.%s' % (path, k))
        if t == 'array':
            out += strict_problems(node.get('items') or {}, path + '[]')
    return out


def sample(node):
    t = node.get('type')
    if 'enum' in node:
        return node['enum'][0]
    if t == 'object':
        return {k: sample(v) for k, v in node.get('properties', {}).items()}
    if t == 'array':
        return []
    return {'string': '', 'boolean': False, 'integer': 0, 'number': 0}.get(t)


class FakeOpenAI(http.server.BaseHTTPRequestHandler):
    server_version = 'FakeOpenAI'

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, code, message, err_code=None):
        self._send(code, {'error': {'message': message, 'type': 'invalid_request_error', 'code': err_code}})

    def do_POST(self):
        cfg = self.server.cfg
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode('utf-8'))
        cfg['seen'].append((self.path, body))
        cfg['auth'].append(self.headers.get('Authorization'))
        if not (self.headers.get('Authorization') or '').startswith('Bearer sk-'):
            return self._error(401, 'Incorrect API key provided')
        if cfg['fail_first'] > 0:
            cfg['fail_first'] -= 1
            return self._error(503, 'The server is overloaded')
        if self.path == '/v1/chat/completions':
            return self._chat(body, cfg)
        if self.path == '/v1/embeddings':
            return self._embed(body, cfg)
        self._error(404, 'Unknown URL %s' % self.path)

    def _chat(self, b, cfg):
        extra = set(b) - CHAT_KEYS
        if extra:
            return self._error(400, 'Unrecognized request arguments: %s' % sorted(extra))
        if b.get('model') not in cfg['models']:
            return self._error(404, 'The model `%s` does not exist or you do not have access' % b.get('model'),
                               'model_not_found')
        msgs = b.get('messages')
        if not isinstance(msgs, list) or not msgs:
            return self._error(400, 'messages is required')
        for m in msgs:
            if m.get('role') not in ('developer', 'system', 'user', 'assistant') or not isinstance(m.get('content'), str):
                return self._error(400, 'bad message')
            if m['role'] == 'developer' and 'developer_role' in cfg['reject']:
                return self._error(400, "Invalid value: 'developer'")
        if 'reasoning_effort' in b:
            if 'reasoning_effort' in cfg['reject'] or b['reasoning_effort'] not in EFFORTS:
                return self._error(400, "Unrecognized request argument supplied: reasoning_effort")
        if not isinstance(b.get('max_completion_tokens', 1), int):
            return self._error(400, 'max_completion_tokens must be an integer')
        rf, schema = b.get('response_format'), None
        if rf is not None:
            if 'response_format' in cfg['reject']:
                return self._error(400, 'response_format is not supported with this model')
            t = rf.get('type')
            if t == 'json_schema':
                if 'json_schema' in cfg['reject']:
                    return self._error(400, 'json_schema is not supported with this model')
                js = rf.get('json_schema') or {}
                if not re.match(r'^[A-Za-z0-9_-]{1,64}$', js.get('name') or ''):
                    return self._error(400, 'Invalid json_schema name')
                schema = js.get('schema') or {}
                if schema.get('type') != 'object':
                    return self._error(400, 'The root schema must be an object')
                if js.get('strict'):
                    problems = strict_problems(schema)
                    if problems:
                        return self._error(400, "Invalid schema for response_format 'reply': " + '; '.join(problems))
            elif t == 'json_object':
                if 'json' not in ' '.join(m['content'] for m in msgs).lower():
                    return self._error(400, "'messages' must contain the word 'json' in some form")
            elif t != 'text':
                return self._error(400, 'Invalid response_format type')
        text = json.dumps(cfg['reply'](schema) if cfg.get('reply') else (sample(schema) if schema else {}))
        ptoks = len(json.dumps(msgs)) // 4
        self._send(200, {'id': 'chatcmpl-fake', 'object': 'chat.completion', 'model': b['model'],
                         'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}],
                         'usage': {'prompt_tokens': ptoks, 'completion_tokens': 12, 'total_tokens': ptoks + 12}})

    def _embed(self, b, cfg):
        if b.get('model') not in cfg['embed_models']:
            return self._error(404, 'The model `%s` does not exist' % b.get('model'), 'model_not_found')
        inp = b.get('input')
        if not isinstance(inp, list) or not 1 <= len(inp) <= 2048 or not all(isinstance(t, str) and t for t in inp):
            return self._error(400, "'input' must be a non-empty array of non-empty strings (at most 2048)")
        if 'dimensions' in b and ('dimensions' in cfg['reject'] or not isinstance(b['dimensions'], int) or b['dimensions'] < 1):
            return self._error(400, 'dimensions is not supported by this model')
        dims = b.get('dimensions') or 1536
        data = []
        for i, t in enumerate(inp):
            h = hashlib.sha256(t.encode('utf-8')).digest()
            data.append({'object': 'embedding', 'index': i, 'embedding': [(h[j % 32] - 127.5) / 128.0 for j in range(dims)]})
        data.reverse()  # real APIs may not return them in order: `index` says where each belongs
        n = sum(len(t) for t in inp) // 4 + 1
        self._send(200, {'object': 'list', 'data': data, 'model': b['model'], 'usage': {'prompt_tokens': n, 'total_tokens': n}})


class Base(unittest.TestCase):
    def setUp(self):
        self.old = dict(os.environ)
        self.tmp = tempfile.mkdtemp(prefix='r3oai_')
        for k in list(os.environ):
            if k.upper() in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_KEY', 'OPENAI_API_KEY', 'R3_PROVIDER',
                             'R3_NO_LLM', 'R3_OPENAI_MODEL', 'R3_EMBED_MODEL', 'R3_LLM_TOTAL'):
                os.environ.pop(k, None)
        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeOpenAI)
        self.srv.daemon_threads = True
        self.srv.cfg = {'seen': [], 'auth': [], 'models': {'gpt-6-luna', 'gpt-5.4-mini', 'gpt-4o-mini'}, 'embed_models': {
            'text-embedding-3-small'}, 'reject': set(), 'fail_first': 0, 'reply': None}
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d/v1' % self.srv.server_address[1]
        os.environ.update(R3_STORE=os.path.join(self.tmp, 'store'), R3_DOTENV=os.path.join(self.tmp, 'no.env'),
                          OPENAI_KEY='sk-testkey', R3_OPENAI_BASE=self.base)
        self.sleep = mock.patch.object(llm_mod.time, 'sleep', lambda s: None)
        self.sleep.start()
        self.store = Store()

    def tearDown(self):
        self.sleep.stop()
        self.srv.shutdown()
        self.srv.server_close()
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self.old)

    @property
    def seen(self):
        return self.srv.cfg['seen']


class RequestValidityTests(Base):
    def test_every_schema_the_program_uses_is_valid_strict_structured_output(self):
        nick = {'type': 'object', 'properties': {'client_id': {'type': 'string'}}, 'required': ['client_id']}
        schemas = {'promises': promises.SCHEMA, 'ownership': owners.SCHEMA, 'preference': feedback.PREF_SCHEMA,
                   'correction': feedback.CORR_SCHEMA, 'intent': ask.INTENT_SCHEMA, 'answer': retrieve.ANSWER_SCHEMA,
                   'nickname': nick}
        for name, schema in schemas.items():
            llm = LLM(self.store, 'ask', budget=None)
            self.seen[:] = []
            res = llm.json_call(name, 'Reply in JSON. <data>[]</data>', schema)
            self.assertIsNotNone(res, '%s: the fake rejected the request: %s' % (name, self.seen[-1][1].get('response_format')))
            self.assertEqual(len(self.seen), 1, '%s needed a retry, so the first request was invalid' % name)
            self.assertIn('json_schema', self.seen[0][1]['response_format']['type'])

    def test_the_request_has_the_documented_shape(self):
        llm = LLM(self.store, 'ask', budget=4000)
        llm.json_call('t', 'hello <data>[]</data>', {'type': 'object', 'properties': {'a': {'type': 'string'}}})
        path, b = self.seen[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(b['model'], 'gpt-6-luna')
        self.assertEqual([m['role'] for m in b['messages']], ['developer', 'user'])
        self.assertIsInstance(b['max_completion_tokens'], int)
        self.assertNotIn('temperature', b)
        self.assertNotIn('max_tokens', b)
        self.assertEqual(b['reasoning_effort'], 'low')

    def test_usage_comes_from_the_response(self):
        llm = LLM(self.store, 'ask', budget=4000)
        llm.json_call('t', 'hello <data>[]</data>', {'type': 'object', 'properties': {'a': {'type': 'string'}}})
        self.assertGreater(llm.input_tokens, 0)
        self.assertEqual(llm.output_tokens, 12)
        self.assertGreater(llm.cost, 0)


class DegradationTests(Base):
    SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}

    def test_a_model_the_key_cannot_use_falls_back_along_the_chain_and_is_remembered(self):
        self.srv.cfg['models'] = {'gpt-4o-mini'}
        llm = LLM(self.store, 'ask', budget=4000)
        self.assertIsNotNone(llm.json_call('t', 'hi <data>[]</data>', self.SCHEMA))
        self.assertEqual([b['model'] for _, b in self.seen], ['gpt-6-luna', 'gpt-5.4-mini', 'gpt-4o-mini'])
        self.store.commit()
        self.seen[:] = []
        again = LLM(self.store, 'ask', budget=4000)
        self.assertEqual(again.model, 'gpt-4o-mini')
        again.json_call('t2', 'another <data>[]</data>', self.SCHEMA)
        self.assertEqual([b['model'] for _, b in self.seen], ['gpt-4o-mini'], 'no more probing once a model works')

    def test_a_model_without_reasoning_effort_or_strict_schemas_still_answers(self):
        self.srv.cfg['reject'] = {'reasoning_effort', 'json_schema'}
        llm = LLM(self.store, 'ask', budget=4000)
        self.assertIsNotNone(llm.json_call('t', 'Reply in JSON <data>[]</data>', self.SCHEMA))
        self.assertEqual(self.seen[-1][1]['response_format'], {'type': 'json_object'})
        self.assertNotIn('reasoning_effort', self.seen[-1][1])

    def test_the_plainest_request_is_the_last_resort(self):
        self.srv.cfg['reject'] = {'reasoning_effort', 'json_schema', 'response_format', 'developer_role'}
        llm = LLM(self.store, 'ask', budget=4000)
        self.srv.cfg['reply'] = lambda schema: {'ok': True}
        self.assertEqual(llm.json_call('t', 'Reply in JSON <data>[]</data>', self.SCHEMA), {'ok': True})
        last = self.seen[-1][1]
        self.assertNotIn('response_format', last)
        self.assertEqual([m['role'] for m in last['messages']], ['system', 'user'])
        self.assertEqual(len(self.seen), 3, 'full, then plain JSON mode, then bare')

    def test_overload_is_retried_then_succeeds(self):
        self.srv.cfg['fail_first'] = 2
        llm = LLM(self.store, 'ask', budget=4000)
        self.assertIsNotNone(llm.json_call('t', 'hi <data>[]</data>', self.SCHEMA))
        self.assertEqual(len(self.seen), 3)

    def test_a_bad_key_gives_none_and_never_raises(self):
        os.environ['OPENAI_KEY'] = 'not-a-key'
        llm = LLM(self.store, 'ask', budget=4000)
        self.assertIsNone(llm.json_call('t', 'hi <data>[]</data>', self.SCHEMA))
        self.assertEqual(len(self.seen), 1, 'an authentication error is not retried')

    def test_embeddings_request_is_valid_and_vectors_keep_input_order(self):
        llm = LLM(self.store, 'ask', budget=4000)
        vecs = llm.embed(['alpha', 'beta', 'gamma'], kinds=['query', 'document', 'document'])
        path, b = self.seen[0]
        self.assertEqual((path, b['model'], b['dimensions']), ('/v1/embeddings', 'text-embedding-3-small', 256))
        self.assertEqual(len(vecs), 3)
        self.assertEqual(len(vecs[0]), 256)
        h = hashlib.sha256(b'alpha').digest()
        self.assertAlmostEqual(vecs[0][0], (h[0] - 127.5) / 128.0, msg='the first vector belongs to the first text')
        self.assertGreater(llm.input_tokens, 0)

    def test_embeddings_without_dimensions_support_still_work(self):
        self.srv.cfg['reject'] = {'dimensions'}
        llm = LLM(self.store, 'ask', budget=4000)
        vecs = llm.embed(['alpha', 'beta'])
        self.assertEqual(len(vecs[0]), 1536)
        self.assertEqual(len(self.seen), 2)

    def test_an_unavailable_embedding_model_gives_none(self):
        self.srv.cfg['embed_models'] = set()
        self.assertIsNone(LLM(self.store, 'ask', budget=4000).embed(['alpha']))


class KeyLookupTests(unittest.TestCase):
    def test_variable_names_are_matched_ignoring_case(self):
        with mock.patch.dict(os.environ, {'openai_key': 'sk-lower'}):
            self.assertEqual(_env('OPENAI_KEY'), 'sk-lower')
        if os.name != 'nt':  # Windows already folds variable names, so two spellings cannot coexist there
            with mock.patch.dict(os.environ, {'OPENAI_KEY': 'sk-upper', 'openai_key': 'sk-lower'}):
                self.assertEqual(_env('OPENAI_KEY'), 'sk-upper', 'an exact match wins')
        self.assertIsNone(_env('R3_DEFINITELY_NOT_SET_XYZ'))


class EndToEndTests(Base):
    """The real commands in child processes, with the key coming from a lower-case line in .env."""

    def test_commands_run_over_http_with_a_lowercase_dotenv_key(self):
        pack = os.path.join(self.tmp, 'pack')
        write_pack(pack)
        dotenv = os.path.join(self.tmp, 'my.env')
        with open(dotenv, 'w', encoding='utf-8') as f:
            f.write('# evaluator style, lower case, spaces, quotes\nopenai_key = "sk-from-dotenv"\n')
        env = {k: v for k, v in os.environ.items() if k.upper() not in ('OPENAI_KEY', 'OPENAI_API_KEY')}
        env.update(R3_PACK=pack, R3_DOTENV=dotenv, R3_OPENAI_BASE=self.base)

        def run(*args):
            p = subprocess.run([sys.executable, os.path.join(ROOT, 'assist.py')] + list(args), cwd=self.tmp, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr.decode('utf-8', 'replace'))
            return json.loads(p.stdout.decode('utf-8'))
        for day in (1, 2):
            self.assertTrue(run('ingest', '--day', str(day))['ok'])
        out = run('ask', '--person', 'a02', '--at', '2027-03-23T18:00:00Z', '--ask',
                  'which warehouse confirmed the stock shipping?')
        self.assertIn('usage', out)
        self.assertIsNotNone(out.get('answer') or out.get('clarify') or out.get('refused'))
        brief = run('brief', '--person', 'a02', '--date', '2027-03-25')
        self.assertTrue(brief and brief[0]['meeting_id'])
        # The model really was called over HTTP, with the key from the lower-case .env line.
        self.assertTrue(self.seen, 'no request reached the fake OpenAI server')
        self.assertTrue(all(a == 'Bearer sk-from-dotenv' for a in self.srv.cfg['auth']), self.srv.cfg['auth'])
        self.assertTrue(any(p == '/v1/chat/completions' for p, _ in self.seen))
        self.assertGreater(out['usage']['input_tokens'], 0, 'usage reports the tokens the API said it used')


if __name__ == '__main__':
    unittest.main()
