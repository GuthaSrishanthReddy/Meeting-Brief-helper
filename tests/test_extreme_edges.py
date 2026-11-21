"""Boundary tests for privacy, retention, provider selection, and model caching.

These tests use synthetic records and mocked transports. They never call a model provider.
"""
import json
import os
import tempfile
import unittest
import urllib.error

from assist.common import People, canon_ts
from assist.feedback import remember_forget
from assist.guard import Guard
from assist.llm import LLM
from assist.store import Store


class StoreEdgeFixture:
    def setUp(self):
        self.old_env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory(prefix='r3edges_')
        self.store_dir = os.path.join(self.tmp.name, 'store')
        os.environ['R3_STORE'] = self.store_dir
        os.environ['R3_DOTENV'] = os.path.join(self.tmp.name, 'no-such.env')
        people_path = os.path.join(self.tmp.name, 'people.json')
        with open(people_path, 'w', encoding='utf-8') as f:
            json.dump({
                'org_a': {'name': 'A', 'people': [
                    {'id': 'a01', 'name': 'A One', 'role': 'manager', 'zone': 'UTC'},
                    {'id': 'a02', 'name': 'A Two', 'role': 'designer', 'zone': 'UTC'},
                ]},
                'org_b': {'name': 'B', 'people': [
                    {'id': 'b01', 'name': 'B One', 'role': 'manager', 'zone': 'UTC'},
                ]},
            }, f)
        self.people = People(people_path)
        self.store = Store(self.people)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self.old_env)

    def add_record(self, rid, author, visibility='team', readers=None, day=1,
                   at='2027-03-22T09:00:00Z', org='org_a'):
        record = {
            'id': rid, 'org': org, 'type': 'chat', 'at': at, 'author': author,
            'client': 'client_a', 'visibility': visibility, 'readers': readers or [],
            'body': 'body ' + rid,
        }
        self.store.add_record(record, day, 0, 'staff', canon_ts(at))

    def check_visibility_time_org_and_derived_fact_boundaries(self):
        self.add_record('team-at-cutoff', 'a01', at='2027-03-22T10:00:00Z')
        self.add_record('team-after-cutoff', 'a01', at='2027-03-22T10:00:01Z')
        self.add_record('private-other', 'a01', visibility='private')
        self.add_record('private-self', 'a02', visibility='private')
        self.add_record('restricted-self', 'a01', visibility='restricted', readers=['a02'])
        self.add_record('restricted-other', 'a01', visibility='restricted', readers=['a01'])
        self.add_record('unknown-other', 'a01', visibility='unknown')
        self.add_record('unknown-self', 'a02', visibility='unknown')
        self.add_record('next-day', 'a01', day=2, at='2027-03-22T09:30:00Z')
        self.add_record('other-org', 'b01', org='org_b')
        self.store.add_fact('decision', 'team-at-cutoff', 1, '2027-03-22T10:00:00Z', 'client_a',
                            {'text': 'visible fact'})
        self.store.add_fact('decision', 'private-other', 1, '2027-03-22T09:00:00Z', 'client_a',
                            {'text': 'private fact'})

        visible = self.store.visible('a02', 1, '2027-03-22T10:00:00Z')
        self.assertEqual(set(visible), {'team-at-cutoff', 'private-self', 'restricted-self', 'unknown-self'})
        facts = self.store.facts(['decision'], visible)
        self.assertEqual([f['rid'] for f in facts], ['team-at-cutoff'],
                         'facts must inherit visibility from their source record')
        guard = Guard('a02', visible, 1, [], [], [])
        self.assertEqual(guard.sources(['team-at-cutoff', 'private-other']), ['team-at-cutoff'])


class StoreEdgeTests(StoreEdgeFixture, unittest.TestCase):
    def test_visibility_time_org_and_derived_fact_boundaries(self):
        self.check_visibility_time_org_and_derived_fact_boundaries()


class CacheEdgeTests(StoreEdgeFixture, unittest.TestCase):
    def test_caches_have_fixed_entry_limits(self):
        from assist.store import MAX_LLM_CACHE_ENTRIES, MAX_OUTPUT_CACHE_ENTRIES

        for i in range(MAX_OUTPUT_CACHE_ENTRIES + 5):
            self.store.out_put('out-%d' % i, {'n': i})
        count = self.store.db.execute('SELECT COUNT(*) FROM outputs').fetchone()[0]
        self.assertEqual(count, MAX_OUTPUT_CACHE_ENTRIES)
        self.assertIsNone(self.store.out_get('out-0'))
        self.assertEqual(self.store.out_get('out-%d' % (MAX_OUTPUT_CACHE_ENTRIES + 4)),
                         {'n': MAX_OUTPUT_CACHE_ENTRIES + 4})

        for i in range(MAX_LLM_CACHE_ENTRIES + 5):
            self.store.llm_put('llm-%d' % i, {'result': {'n': i}})
        count = self.store.db.execute('SELECT COUNT(*) FROM llm_cache').fetchone()[0]
        self.assertEqual(count, MAX_LLM_CACHE_ENTRIES)
        self.assertIsNone(self.store.llm_get('llm-0'))

    def test_forget_clears_cached_outputs_and_model_results_per_person(self):
        self.store.out_put('old-output', {'answer': 'Dubai trip'})
        self.store.llm_put('old-prompt-hash', {'result': {'what': 'Dubai trip'}})
        self.assertTrue(remember_forget(self.store, 'a02', 'Forget what I said about the Dubai trip.', 4))
        self.assertIsNone(self.store.out_get('old-output'))
        self.assertIsNone(self.store.llm_get('old-prompt-hash'))
        self.assertTrue(self.store.memories('a02', 'forget'))
        self.assertEqual(self.store.memories('a01', 'forget'), [], 'forget memory belongs to one person')


class LLMEdgeTests(StoreEdgeFixture, unittest.TestCase):
    def clear_provider_env(self):
        for key in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_KEY', 'OPENAI_API_KEY',
                    'R3_PROVIDER', 'R3_OPENAI_MODEL', 'R3_OPENAI_BASE', 'R3_LLM_BASE', 'R3_NO_LLM'):
            os.environ.pop(key, None)

    @staticmethod
    def response():
        return {'choices': [{'message': {'content': '{"ok": true}'}}],
                'usage': {'prompt_tokens': 8, 'completion_tokens': 2}}

    def test_explicit_provider_without_its_key_does_not_switch_provider(self):
        self.clear_provider_env()
        os.environ['R3_PROVIDER'] = 'gemini'
        os.environ['OPENAI_KEY'] = 'sk-test'
        llm = LLM(self.store, 'ask', budget=4000)
        self.assertIsNone(llm.provider)
        self.assertFalse(llm.enabled)
        llm._post = lambda body: self.fail('disabled provider must never send a request')
        self.assertIsNone(llm.json_call('ask', 'question', {'type': 'object'}))

        self.clear_provider_env()
        os.environ['R3_PROVIDER'] = 'openai'
        os.environ['GEMINI_API_KEY'] = 'g-test'
        forced_openai = LLM(self.store, 'ask', budget=4000)
        self.assertIsNone(forced_openai.provider)
        self.assertFalse(forced_openai.enabled)

    def test_no_llm_switch_overrides_valid_keys(self):
        self.clear_provider_env()
        os.environ['OPENAI_KEY'] = 'sk-test'
        os.environ['R3_NO_LLM'] = '1'
        llm = LLM(self.store, 'ask', budget=4000)
        self.assertEqual(llm.provider, 'openai')
        self.assertFalse(llm.enabled)
        llm._post = lambda body: self.fail('R3_NO_LLM must prevent network requests')
        self.assertIsNone(llm.json_call('ask', 'question', {'type': 'object'}))

    def test_openai_fallback_model_is_cached_and_remembered(self):
        self.clear_provider_env()
        os.environ['OPENAI_KEY'] = 'sk-test'
        llm = LLM(self.store, 'ask', budget=4000)
        tried = []

        def fake_post(body):
            tried.append(body['model'])
            if body['model'] == 'gpt-6-luna':
                raise urllib.error.HTTPError('mock', 404, 'not found', {}, None)
            return self.response()

        llm._post = fake_post
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
        prompt = 'same prompt for cache test'
        self.assertEqual(llm.json_call('ask', prompt, schema), {'ok': True})
        self.assertEqual(tried, ['gpt-6-luna', 'gpt-5.4-mini'])
        self.store.commit()

        # Same instance and a new command instance both reuse the successful model/result.
        self.assertEqual(llm.json_call('ask', prompt, schema), {'ok': True})
        second = LLM(self.store, 'ask', budget=4000)
        self.assertEqual(second.model, 'gpt-5.4-mini')
        self.assertEqual(second.json_call('ask', prompt, schema), {'ok': True})
        self.assertEqual(tried, ['gpt-6-luna', 'gpt-5.4-mini'])
        cache_rows = self.store.db.execute('SELECT k, v FROM llm_cache').fetchall()
        self.assertTrue(cache_rows)
        self.assertNotIn(prompt, json.dumps([tuple(row) for row in cache_rows]),
                         'the prompt text is not persisted in the model cache')

    def test_openai_400_retries_without_optional_fields(self):
        self.clear_provider_env()
        os.environ['OPENAI_KEY'] = 'sk-test'
        llm = LLM(self.store, 'ask', budget=4000)
        sent = []

        def fake_post(body):
            sent.append(json.loads(json.dumps(body)))
            if len(sent) == 1:
                raise urllib.error.HTTPError('mock', 400, 'unsupported schema option', {}, None)
            return self.response()

        llm._post = fake_post
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
        self.assertEqual(llm.json_call('ask', 'question', schema), {'ok': True})
        self.assertEqual(len(sent), 2)
        self.assertIn('reasoning_effort', sent[0])
        self.assertNotIn('reasoning_effort', sent[1])
        self.assertEqual(sent[1]['response_format'], {'type': 'json_object'})

    def test_openai_alternate_model_can_retry_rejected_schema(self):
        self.clear_provider_env()
        os.environ['OPENAI_KEY'] = 'sk-test'
        llm = LLM(self.store, 'ask', budget=4000)
        sent = []

        def fake_post(body):
            sent.append((body['model'], (body.get('response_format') or {}).get('type')))
            if body['model'] == 'gpt-6-luna':
                raise urllib.error.HTTPError('mock', 404, 'model not found', {}, None)
            if body['response_format']['type'] == 'json_schema':
                raise urllib.error.HTTPError('mock', 400, 'schema unsupported', {}, None)
            return self.response()

        llm._post = fake_post
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
        self.assertEqual(llm.json_call('ask', 'question', schema), {'ok': True})
        self.assertEqual(sent, [('gpt-6-luna', 'json_schema'),
                                ('gpt-5.4-mini', 'json_schema'),
                                ('gpt-5.4-mini', 'json_object')])

    def test_retryable_statuses_and_network_timeouts(self):
        import socket
        import assist.llm as llm_mod

        self.clear_provider_env()
        os.environ['GEMINI_API_KEY'] = 'g-test'
        real_sleep = llm_mod.time.sleep
        llm_mod.time.sleep = lambda seconds: None
        try:
            failures = [
                urllib.error.HTTPError('mock', code, 'retry', {}, None)
                for code in (429, 500, 502, 503, 504)
            ] + [urllib.error.URLError('offline'), socket.timeout('timed out')]
            for index, failure in enumerate(failures):
                llm = LLM(self.store, 'ask', budget=4000)
                calls = []
                ok = {'candidates': [{'content': {'parts': [{'text': '{"ok": true}'}]}}],
                      'usageMetadata': {'promptTokenCount': 4, 'candidatesTokenCount': 1}}

                def post(body, failure=failure, calls=calls, ok=ok):
                    calls.append(1)
                    if len(calls) <= 2:
                        raise failure
                    return ok

                llm._post = post
                self.assertEqual(llm.json_call('retry', 'retry-%d' % index, {'type': 'object'}), {'ok': True})
                self.assertEqual(len(calls), 3, repr(failure))

            llm = LLM(self.store, 'ask', budget=4000)
            calls = []
            llm._post = lambda body: (calls.append(1) or (_ for _ in ()).throw(
                urllib.error.HTTPError('mock', 401, 'unauthorized', {}, None)))
            self.assertIsNone(llm.json_call('unauthorized', 'no retry', {'type': 'object'}))
            self.assertEqual(len(calls), 1, 'authentication errors are not retried')
        finally:
            llm_mod.time.sleep = real_sleep

    def test_invalid_model_json_is_not_cached(self):
        self.clear_provider_env()
        os.environ['OPENAI_KEY'] = 'sk-test'
        llm = LLM(self.store, 'ask', budget=4000)
        calls = []
        llm._post = lambda body: (calls.append(1) or {
            'choices': [{'message': {'content': '{not json'}}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 1},
        })
        self.assertIsNone(llm.json_call('ask', 'malformed response prompt', {'type': 'object'}))
        self.assertIsNone(llm.json_call('ask', 'malformed response prompt', {'type': 'object'}))
        self.assertEqual(len(calls), 2, 'invalid model JSON must not poison the prompt cache')


if __name__ == '__main__':
    unittest.main()
