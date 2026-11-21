# -*- coding: utf-8 -*-
"""Found by running the project the way an outside evaluator would, with hostile input:

- `--help` printed argparse's text on stdout, ahead of the JSON (stdout is for one JSON document only);
- on Python 3.8 a "+0530" offset was silently dropped, moving a question's time by hours;
- `ingest --day 999` (no such day) marked the day as ingested, which pushed last_day past real time
  and emptied every later brief; with a wrong R3_PACK, `ingest --day 1` wiped the good store first.

No model is involved (R3_NO_LLM) and nothing touches the network.
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from assist.common import canon_ts, parse_ts  # noqa: E402
from test_assist import write_pack  # noqa: E402

ASSIST = os.path.join(ROOT, 'assist.py')


def utc(*a):
    return dt.datetime(*a, tzinfo=dt.timezone.utc)


class ParseTimestampTests(unittest.TestCase):
    def test_every_offset_form_is_honoured(self):
        noon = utc(2026, 11, 5, 12, 30)
        for s in ('2026-11-05T18:00:00+05:30', '2026-11-05T18:00:00+0530', '2026-11-05T18:00+05:30', '2026-11-05 18:00:00+05:30',
                  '2026-11-05T07:00:00-05:30', '2026-11-05T07:00:00-0530', '2026-11-05T12:30:00Z', '2026-11-05T12:30:00z',
                  '2026-11-05T12:30:00+00:00', '2026-11-05T12:30:00+0000', '2026-11-05T12:30:00 UTC', '2026-11-05 12:30 GMT'):
            self.assertEqual(parse_ts(s), noon, s)
        self.assertEqual(parse_ts('2026-11-05T18:00:00+05'), utc(2026, 11, 5, 13, 0))
        self.assertEqual(parse_ts('2026-11-05T18:00:00-0800'), utc(2026, 11, 6, 2, 0))

    def test_no_offset_means_utc_and_fractions_of_any_length_work(self):
        for s in ('2026-11-05T18:00:00', '2026-11-05T18:00', '2026-11-05 18:00', '2026-11-05T18:00:00Z', '2026-11-05T18:00:00.000Z'):
            self.assertEqual(parse_ts(s), utc(2026, 11, 5, 18, 0), s)
        self.assertEqual(parse_ts('2026-11-05'), utc(2026, 11, 5))
        self.assertEqual(parse_ts('2026-11-05T18:00:00.5Z'), utc(2026, 11, 5, 18, 0, 0, 500000))
        self.assertEqual(parse_ts('2026-11-05T18:00:00.123456789Z'), utc(2026, 11, 5, 18, 0, 0, 123456))
        self.assertEqual(parse_ts('  2026-11-05T18:00:00Z  '), utc(2026, 11, 5, 18, 0))

    def test_what_is_not_a_time_is_none(self):
        for s in (None, '', '   ', 'tomorrow', '1730829600', '2026/11/05 18:00', '2026-13-45T00:00:00Z', '2026-02-30', 5, [], {},
                  '2026-11-05T18:00:00+25:00', 'Nov 5 2026 6pm'):
            self.assertIsNone(parse_ts(s), repr(s))

    def test_an_offset_is_never_dropped_even_when_the_rest_is_odd(self):
        # trailing text after a plain time is read leniently, but text that hides an offset is not guessed at
        self.assertEqual(parse_ts('2026-11-05T18:00:00 sharp'), utc(2026, 11, 5, 18, 0))
        self.assertIsNone(parse_ts('2026-11-05T18:00:00 +05:30 sharp'))

    def test_canon_ts_moves_to_utc(self):
        self.assertEqual(canon_ts('2026-11-05T18:00:00+0530'), '2026-11-05T12:30:00Z')
        self.assertEqual(canon_ts('not a time'), 'not a time')


class CliBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='r3cli_')
        cls.pack = os.path.join(cls.root, 'pack')
        write_pack(cls.pack)
        cls.env0 = {k: v for k, v in os.environ.items() if not k.upper().startswith('R3_')
                    and k.upper() not in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_KEY', 'OPENAI_API_KEY')}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix='store_', dir=self.root)
        self.env = dict(self.env0, R3_PACK=self.pack, R3_STORE=self.store, R3_NO_LLM='1',
                        R3_DOTENV=os.path.join(self.root, 'no.env'))

    def cli(self, *args, **kw):
        env = dict(self.env, **kw.get('env', {}))
        p = subprocess.run([sys.executable, ASSIST] + list(args), cwd=self.root, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=120)
        out = p.stdout.decode('utf-8')
        self.assertEqual(p.returncode, 0, p.stderr.decode('utf-8', 'replace'))
        self.assertEqual(len(out.strip().splitlines()), 1, 'exactly one JSON document on stdout: %r' % out[:200])
        return json.loads(out), p.stderr.decode('utf-8', 'replace')

    def ingest(self, *days):
        for d in days:
            self.cli('ingest', '--day', str(d))


class HelpGoesToStderrTests(CliBase):
    def test_help_never_reaches_stdout(self):
        for args in (['--help'], ['-h'], ['brief', '--help'], ['ask', '-h'], ['ingest', '--help'], ['forget', '--help']):
            out, err = self.cli(*args)  # cli() already insists on exit 0 and a single JSON line on stdout
            self.assertIsInstance(out, (dict, list), args)
            self.assertIn('usage', err.lower(), args)  # the help is still there, on stderr, for a person

    def test_bad_arguments_are_still_one_json_document(self):
        for args in ([], ['frobnicate'], ['ingest'], ['ingest', '--day', 'x'], ['brief', '--person', 'a01'],
                     ['ask', '--person', 'a01', '--at', '2027-03-22T10:00:00Z', '--ask', 'q', '--summary']):
            out, _ = self.cli(*args)
            self.assertIsInstance(out, (dict, list))


class IngestOnlyRealDaysTests(CliBase):
    def last_day_and_days(self):
        sys.path.insert(0, ROOT)
        from assist.store import Store
        saved = os.environ.get('R3_STORE')
        os.environ['R3_STORE'] = self.store
        try:
            s = Store()
            try:
                return s.last_day(), list(s.meta('days', []) or [])
            finally:
                s.close()
        finally:
            if saved is None:
                os.environ.pop('R3_STORE', None)
            else:
                os.environ['R3_STORE'] = saved

    BRIEF = ('brief', '--person', 'a03', '--date', '2027-03-25')

    @staticmethod
    def core(o):
        if isinstance(o, dict):
            return dict((k, IngestOnlyRealDaysTests.core(v)) for k, v in o.items() if k != 'usage')
        if isinstance(o, list):
            return [IngestOnlyRealDaysTests.core(x) for x in o]
        return o

    def test_a_day_that_does_not_exist_changes_nothing(self):
        self.ingest(1, 2, 3)
        before = self.core(self.cli(*self.BRIEF)[0])
        self.assertTrue(before and before[0]['status'] == 'brief', 'a real brief, so the comparison means something')
        state = self.last_day_and_days()
        for day in (999, 5, 17, 0, -2):
            out, err = self.cli('ingest', '--day', str(day))
            self.assertFalse(out['ok'], day)
            self.assertIn('nothing was changed', out['error'])
            self.assertEqual(out['day'], day)
            self.assertIn('no data for day', err)
        self.assertEqual(self.last_day_and_days(), state)
        self.assertEqual(self.core(self.cli(*self.BRIEF)[0]), before)
        self.ingest(4)  # a real next day is still welcome later: nothing was poisoned (no day-04 file here: a quiet day)
        self.assertEqual(self.last_day_and_days()[0], 4)

    def test_a_quiet_day_is_accepted_only_as_the_next_day(self):
        self.ingest(1, 2, 3)
        out, _ = self.cli('ingest', '--day', '4')  # no day-04 file in the pack: a quiet day, and it is the next one
        self.assertTrue(out['ok'])
        self.assertEqual(out['records'], 0)
        self.assertEqual(self.last_day_and_days(), (4, [1, 2, 3, 4]))
        out, _ = self.cli('ingest', '--day', '4')
        self.assertEqual(out.get('skipped'), 'already ingested')
        out, _ = self.cli('ingest', '--day', '6')  # a gap: not accepted
        self.assertFalse(out['ok'])
        out, _ = self.cli('ingest', '--day', '5')
        self.assertTrue(out['ok'])
        self.assertEqual(self.last_day_and_days(), (5, [1, 2, 3, 4, 5]))

    def test_a_wrong_pack_cannot_wipe_a_good_store(self):
        self.ingest(1, 2, 3)
        before = self.core(self.cli(*self.BRIEF)[0])
        state = self.last_day_and_days()
        nowhere = os.path.join(self.root, 'not-a-pack')
        os.makedirs(nowhere)
        out, _ = self.cli('ingest', '--day', '1', env={'R3_PACK': nowhere})
        self.assertFalse(out['ok'])
        self.assertEqual(self.last_day_and_days(), state)
        self.assertEqual(self.core(self.cli(*self.BRIEF)[0]), before)

    def test_day_one_still_starts_fresh_when_there_is_data(self):
        self.ingest(1, 2, 3)
        out, _ = self.cli('ingest', '--day', '1')
        self.assertTrue(out['ok'])
        self.assertEqual(self.last_day_and_days(), (1, [1]))

    def test_ingesting_a_real_day_out_of_order_still_works(self):
        self.ingest(1, 3)  # day 2 skipped on purpose; both files exist
        self.assertEqual(self.last_day_and_days(), (3, [1, 3]))
        self.ingest(2)
        self.assertEqual(self.last_day_and_days(), (3, [1, 2, 3]))


class AskNeedsAReadableTimeTests(CliBase):
    """An unreadable --at used to become "now" with no time cutoff at all, so a question could be answered
    from records written after the moment it was supposedly asked. It now answers nothing."""
    Q = 'Who owns billing for Acme?'

    def ask(self, at):
        out, _ = self.cli('ask', '--person', 'a02', '--at', at, '--ask', self.Q)
        return out

    def test_a_readable_time_still_gets_the_real_answer(self):
        self.ingest(1, 2, 3)
        out = self.ask('2027-03-24T18:00:00Z')
        self.assertNotEqual(out['answer']['text'], 'I have nothing on that.')
        self.assertTrue(out['answer']['sources'])
        for at in ('2027-03-24T18:00:00+0000', '2027-03-24 18:00', '2027-03-24T23:30:00+05:30'):
            self.assertEqual(self.ask(at)['answer']['text'], out['answer']['text'], at)

    def test_an_unreadable_time_answers_nothing_and_reads_nothing(self):
        self.ingest(1, 2, 3)
        for at in ('garbage', '', 'tomorrow', '2027-13-45T00:00:00Z', '1711303200', '2027/03/24 18:00', '2027-03-24T18:00:00+25:00'):
            out = self.ask(at)
            self.assertEqual(out['answer'], {'text': 'I have nothing on that.', 'sources': []}, repr(at))
            self.assertEqual(out['matched'], [], repr(at))
            self.assertIsNone(out['clarify'])
            self.assertIsNone(out['refused'])
            self.assertEqual(out['usage']['input_tokens'], 0)
            self.assertEqual(out['usage']['output_tokens'], 0)

    def test_the_time_cutoff_still_holds_for_a_readable_time(self):
        self.ingest(1, 2, 3)
        # before the announcement (day 2, 04:00 UTC) the answer cannot cite it
        early = self.ask('2027-03-22T20:00:00Z')
        late = self.ask('2027-03-24T18:00:00Z')
        self.assertNotEqual(early['answer'], late['answer'])
        self.assertNotIn('xann1', early['answer']['sources'])


if __name__ == '__main__':
    unittest.main()
