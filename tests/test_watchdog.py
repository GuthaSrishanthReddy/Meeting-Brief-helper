# -*- coding: utf-8 -*-
"""The watchdog: a command that cannot finish still prints valid JSON and exits 0 before the harness's
120 s kill. The stuck state is made for real: the test holds an exclusive lock on the store's SQLite
file, so the child process blocks inside Store() exactly as it would behind another process's lock.
No model is involved (R3_NO_LLM) and nothing touches the network."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from test_assist import write_pack  # noqa: E402

ASSIST = os.path.join(ROOT, 'assist.py')


class WatchdogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='r3wd_')
        cls.pack = os.path.join(cls.root, 'pack')
        write_pack(cls.pack)
        cls.store = os.path.join(cls.root, 'store')
        cls.env = {k: v for k, v in os.environ.items() if not k.upper().startswith('R3_')
                   and k.upper() not in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_KEY', 'OPENAI_API_KEY')}
        cls.env.update(R3_PACK=cls.pack, R3_STORE=cls.store, R3_NO_LLM='1',
                       R3_DOTENV=os.path.join(cls.root, 'no.env'))
        for d in (1, 2, 3):
            cls.run_cli(['ingest', '--day', str(d)])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def run_cli(cls, args, env=None, timeout=120):
        t0 = time.time()
        p = subprocess.run([sys.executable, ASSIST] + args, cwd=cls.root, env=env or cls.env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        out = p.stdout.decode('utf-8')
        return p.returncode, out, p.stderr.decode('utf-8', 'replace'), time.time() - t0

    def locked(self):
        """A second connection holding an exclusive lock: every other process now waits."""
        db = sqlite3.connect(os.path.join(self.store, 'assist.db'), timeout=1)
        db.execute('BEGIN EXCLUSIVE')
        return db

    def stuck(self, args, limit='3'):
        db = self.locked()
        try:
            code, out, err, secs = self.run_cli(args, env=dict(self.env, R3_WATCHDOG_S=limit))
        finally:
            db.rollback()
            db.close()
        return code, out, err, secs

    def test_a_stuck_brief_prints_an_empty_list_and_exits_zero_in_time(self):
        code, out, err, secs = self.stuck(['brief', '--person', 'a03', '--date', '2027-03-25'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])
        self.assertEqual(len(out.strip().splitlines()), 1, 'exactly one JSON document, nothing else on stdout')
        self.assertLess(secs, 20, 'it answered at the watchdog limit, not after the 60 s database wait')
        self.assertIn('watchdog', err)

    def test_a_stuck_ask_prints_the_safe_answer_with_usage(self):
        code, out, err, secs = self.stuck(['ask', '--person', 'a02', '--at', '2027-03-24T18:00:00Z', '--ask',
                                           'Who handles billing for Acme?'])
        self.assertEqual(code, 0)
        o = json.loads(out)
        self.assertEqual(o['answer']['text'], 'I have nothing on that.')
        self.assertIsNone(o['clarify'])
        self.assertIsNone(o['refused'])
        self.assertEqual(o['usage']['input_tokens'], 0, 'usage is present and zero')
        self.assertLess(secs, 20)

    def test_stuck_ingest_and_forget_say_so_in_json(self):
        for args in (['ingest', '--day', '3'], ['forget', '--person', 'a02', '--what', 'the offsite']):
            code, out, err, secs = self.stuck(args)
            self.assertEqual(code, 0, args)
            o = json.loads(out)
            self.assertIs(o['ok'], False)
            self.assertIn('timed out', o['error'])
            self.assertLess(secs, 20)

    def test_the_store_is_intact_after_a_watchdog_exit(self):
        self.stuck(['brief', '--person', 'a03', '--date', '2027-03-25'])
        code, out, err, secs = self.run_cli(['brief', '--person', 'a03', '--date', '2027-03-25'])
        self.assertEqual(code, 0)
        b = json.loads(out)
        self.assertTrue(b and b[0]['status'] == 'brief' and b[0]['changed'], 'the real brief is still there')
        code, out, err, secs = self.run_cli(['ask', '--person', 'a02', '--at', '2027-03-24T18:00:01Z', '--ask',
                                             'Who handles billing for Acme?'])
        self.assertIn('Priya Shah', json.loads(out)['answer']['text'])

    def test_a_normal_command_prints_one_result_and_exits_promptly(self):
        code, out, err, secs = self.run_cli(['brief', '--person', 'a03', '--date', '2027-03-25'],
                                            env=dict(self.env, R3_WATCHDOG_S='60'))
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertTrue(json.loads(out)[0]['changed'])
        self.assertLess(secs, 30, 'the armed timer does not keep the process alive')
        self.assertNotIn('watchdog', err)

    def test_the_watchdog_can_be_switched_off_and_bad_values_fall_back_to_the_default(self):
        for value in ('0', 'banana'):
            code, out, err, secs = self.run_cli(['brief', '--person', 'a03', '--date', '2027-03-25'],
                                                env=dict(self.env, R3_WATCHDOG_S=value))
            self.assertEqual(code, 0, value)
            self.assertTrue(json.loads(out)[0]['changed'], value)


if __name__ == '__main__':
    unittest.main()
