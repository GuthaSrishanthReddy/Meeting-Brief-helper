# -*- coding: utf-8 -*-
"""The per-command model-time cap, and what happens to work the model could not reach.

Mocked transports only: no model provider is called.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import assist.llm as llm_mod  # noqa: E402
from assist import ingest  # noqa: E402
from assist.llm import LLM  # noqa: E402
from assist.store import Store  # noqa: E402
from test_assist import write_pack  # noqa: E402

KEYS = ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_KEY', 'OPENAI_API_KEY', 'R3_PROVIDER', 'R3_NO_LLM',
        'R3_LLM_TOTAL', 'R3_LLM_DEADLINE', 'R3_LLM_TIMEOUT')
OK = {'candidates': [{'content': {'parts': [{'text': '{"ok": true}'}]}}],
      'usageMetadata': {'promptTokenCount': 5, 'candidatesTokenCount': 1}}


class Base(unittest.TestCase):
    def setUp(self):
        self.old = dict(os.environ)
        self.tmp = tempfile.mkdtemp(prefix='r3cap_')
        for k in KEYS:
            os.environ.pop(k, None)
        os.environ['R3_STORE'] = os.path.join(self.tmp, 'store')
        os.environ['R3_DOTENV'] = os.path.join(self.tmp, 'no-such.env')
        os.environ['GEMINI_API_KEY'] = 'g-test'
        self.store = Store()
        self.sleep = mock.patch.object(llm_mod.time, 'sleep', lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self.old)


class TimeCapTests(Base):
    def test_defaults_per_command(self):
        self.assertEqual(LLM(self.store, 'ingest').total_time, 80.0)
        self.assertEqual(LLM(self.store, 'ask').total_time, 45.0)
        self.assertEqual(LLM(self.store, 'brief').total_time, 45.0)
        os.environ['R3_LLM_TOTAL'] = '12'
        self.assertEqual(LLM(self.store, 'ask').total_time, 12.0)
        os.environ['R3_LLM_TOTAL'] = 'banana'
        self.assertEqual(LLM(self.store, 'ask').total_time, 45.0, 'a bad value falls back to the default')

    def test_exhausted_budget_skips_the_call(self):
        os.environ['R3_LLM_TOTAL'] = '5'
        llm = LLM(self.store, 'ask', budget=4000)
        self.store.model_spent = 4.5  # 0.5 s left: under the 2 s minimum worth starting a call for
        self.assertTrue(llm.exhausted)
        llm._post = lambda body: self.fail('no request once the time budget is used up')
        self.assertIsNone(llm.json_call('t', 'p <data>[]</data>', {'type': 'object'}))
        self.assertEqual(llm.skipped, 1)

    def test_budget_is_shared_by_every_llm_object_in_the_command(self):
        os.environ['R3_LLM_TOTAL'] = '5'
        a, b = LLM(self.store, 'ask'), LLM(self.store, 'ask')
        self.store.model_spent = 4.0
        self.assertAlmostEqual(a.time_left(), 1.0)
        self.assertAlmostEqual(b.time_left(), 1.0)

    def test_attempt_timeout_shrinks_to_what_is_left_and_spend_is_recorded(self):
        os.environ['R3_LLM_TOTAL'] = '10'
        llm = LLM(self.store, 'ask')
        seen = []

        class Resp(object):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(OK).encode('utf-8')

        def fake_urlopen(req, timeout=None):
            seen.append(timeout)
            return Resp()
        self.store.model_spent = 6.0  # 4 s left, attempts normally get 25 s
        with mock.patch.object(llm_mod.urllib.request, 'urlopen', fake_urlopen):
            self.assertEqual(llm.json_call('t', 'p <data>[]</data>', {'type': 'object'}), {'ok': True})
        self.assertAlmostEqual(seen[0], 4.0, places=1)
        self.assertGreater(self.store.model_spent, 6.0 - 1e-9, 'the call time is added to the spend')

    def test_cache_hits_are_free_even_when_the_budget_is_gone(self):
        os.environ['R3_LLM_TOTAL'] = '5'
        llm = LLM(self.store, 'ask')
        llm._post = lambda body: OK
        self.assertEqual(llm.json_call('t', 'same prompt <data>[]</data>', {'type': 'object'}), {'ok': True})
        self.store.model_spent = 99.0
        llm._post = lambda body: self.fail('served from the cache, no request')
        self.assertEqual(llm.json_call('t', 'same prompt <data>[]</data>', {'type': 'object'}), {'ok': True})
        self.assertEqual(llm.skipped, 0)

    def test_retry_window_never_passes_the_budget(self):
        os.environ['R3_LLM_TOTAL'] = '2.5'
        llm = LLM(self.store, 'ask')
        calls = []

        def down(body):
            calls.append(1)
            raise urllib.error.URLError('down')
        llm._post = down
        self.assertIsNone(llm.json_call('t', 'p <data>[]</data>', {'type': 'object'}))
        self.assertLess(len(calls), 3, 'the 3 s retry delay does not fit in what is left, so it stops early')


class BacklogTests(Base):
    """Ingest keeps what the model could not reach, and finishes it on the next day."""

    def setUp(self):
        Base.setUp(self)
        self.pack = os.path.join(self.tmp, 'pack')
        write_pack(self.pack)
        os.environ['R3_PACK'] = self.pack
        self.store.close()
        self.store = Store()  # the store reads people.json when created, so it must follow R3_PACK
        day1 =os.path.join(self.pack, 'data', 'days', 'day-01.jsonl')
        extra = [
            {'id': 'xcand1', 'org': 'org_a', 'type': 'chat', 'at': '2027-03-22T06:00:00Z', 'author': 'a03',
             'client': 'acme', 'visibility': 'team', 'channel': '#acme',
             'body': 'main final deck kal subah tak bhej deta hoon'},
            {'id': 'xown1', 'org': 'org_a', 'type': 'chat', 'at': '2027-03-22T04:00:00Z', 'author': 'a01',
             'client': 'acme', 'visibility': 'team', 'channel': '#accounts',
             'body': 'Acme invoicing: ownership moves to Priya Shah.'}]
        with open(day1, 'a', encoding='utf-8') as f:
            for r in extra:
                f.write(json.dumps(r) + '\n')

    @staticmethod
    def working(self_, body):
        text = body['contents'][0]['parts'][0]['text']
        ids = re.findall(r'"id": "([^"]+)"', text)
        if 'commits to deliver' in text:
            items = [{'id': i, 'is_promise': i == 'xcand1', 'what': 'the final deck', 'due': '2027-03-23'} for i in ids]
        else:
            items = [{'id': i, 'is_assignment': i == 'xown1', 'client': 'acme', 'function': 'billing',
                      'new_owner': 'Priya Shah', 'previous_owner': 'Asha Verma'} for i in ids]
        return {'candidates': [{'content': {'parts': [{'text': json.dumps({'items': items})}]}}],
                'usageMetadata': {'promptTokenCount': 50, 'candidatesTokenCount': 20}}

    @staticmethod
    def down(self_, body):
        raise urllib.error.URLError('provider down')

    def facts(self, kind):
        return [(r['rid'], json.loads(r['data']), r['day'])
                for r in self.store.db.execute('SELECT rid, data, day FROM facts WHERE kind=?', (kind,))]

    def test_unreached_work_is_kept_and_finished_next_day(self):
        with mock.patch.object(LLM, '_post', self.down):
            out = ingest.run(self.store, 1)
        self.assertEqual(out['model_pending'], {'promises': 1, 'owners': 1})
        model_facts = [f for f in self.facts('promise') if f[1].get('via') == 'model']
        self.assertEqual(model_facts, [], 'nothing was invented while the model was down')
        pend = self.store.meta('pending_model')
        self.assertEqual([c['id'] for c in pend['promises']], ['xcand1'])
        self.assertEqual([c['id'] for c in pend['owners']], ['xown1'])

        with mock.patch.object(LLM, '_post', self.working):
            out = ingest.run(self.store, 2)
        self.assertEqual(out['model_pending'], {'promises': 0, 'owners': 0})
        promise = [f for f in self.facts('promise') if f[0] == 'xcand1']
        self.assertEqual(len(promise), 1)
        self.assertEqual(promise[0][1]['owner'], 'a03')
        self.assertEqual(promise[0][1]['due'], '2027-03-23')
        self.assertEqual(promise[0][2], 1, 'the fact keeps the day of its source record')
        own = [f for f in self.facts('ownership') if f[0] == 'xown1']
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0][1]['person'], 'a05')

    def test_a_candidate_the_model_never_answers_is_dropped_after_three_days(self):
        with mock.patch.object(LLM, '_post', self.down):
            ingest.run(self.store, 1)
            ingest.run(self.store, 2)
            out = ingest.run(self.store, 3)
        self.assertEqual(out['model_pending'], {'promises': 0, 'owners': 0}, 'backlog is bounded by MAX_TRIES')

    def test_time_budget_zero_keeps_everything_for_later(self):
        os.environ['R3_LLM_TOTAL'] = '0'
        with mock.patch.object(LLM, '_post', lambda s, b: self.fail('no request with a zero budget')):
            out = ingest.run(self.store, 1)
        self.assertEqual(out['model_pending'], {'promises': 1, 'owners': 1})
        self.assertGreaterEqual(out['model_calls_skipped'], 2)

    def test_no_backlog_without_a_model(self):
        os.environ.pop('GEMINI_API_KEY', None)
        out = ingest.run(self.store, 1)
        self.assertEqual(out['model_pending'], {'promises': 0, 'owners': 0}, 'no model: nothing to retry later')
        self.assertIsNone(self.store.meta('pending_model'))


if __name__ == '__main__':
    unittest.main()
