# -*- coding: utf-8 -*-
"""Local web console for the meeting brief helper: two pages and a small JSON API. Standard library only.

  /console   the four CLI commands (brief, ask, forget, ingest) in a form, with the result shown
  /metrics   command and model-call logs, latency and token metrics, and past harness runs

Every command is run by starting the real `assist.py` as a child process, exactly as the harness does,
so the page behaves like the CLI: same caching, same privacy gate, same watchdog. Nothing here
re-implements a command.

Safety, because this serves people's work records:
- it listens on loopback only unless told otherwise (and then prints a warning: there is no login);
- every request must carry a Host header the server knows (stops DNS-rebinding) and every /api call a
  per-start random token that only the pages this server serves contain (stops other websites);
- only whitelisted commands with validated arguments run, no shell is used, the request body is capped;
- the record viewer goes through Store.visible(person, ...), the same gate as the CLI;
- no API key or .env content is ever returned;
- the pages build the DOM with textContent, never innerHTML, because record text is untrusted.
"""
import datetime as dt
import hmac
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse

from . import cmdlog
from .common import (Clients, People, data_path, local_date, pack_root, parse_day, parse_ts, read_json,
                     record_text)
from .guard import Guard
from .llm import LLM, TOTAL_TIME_S
from .store import Store, store_dir

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(PROJECT, 'web')
MAX_BODY = 65536
RUN_TIMEOUT_S = 130  # a little over the watchdog (100 s) so the child's own fallback is what we report
PAGES = {'/': 'console.html', '/console': 'console.html', '/metrics': 'metrics.html'}
STATIC_TYPES = {'.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                '.svg': 'image/svg+xml'}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

PERSON_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,31}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
AT_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$')
NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,80}$')
RECORD_ID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


class ApiError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


# ------------------------------------------------------------------ small helpers
def read_jsonl_tail(path, limit, max_bytes=3000000):
    """The last `limit` JSON objects of a JSONL file, reading at most the last `max_bytes` of it."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial first line
            raw = f.read().decode('utf-8', 'replace')
    except OSError:
        return []
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows[-limit:]


def percentile(sorted_vals, p):
    """Nearest-rank percentile of an already sorted list."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, int(-(-p * len(sorted_vals) // 100)) - 1))
    return sorted_vals[k]


def ms_stats(values):
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return {'n': 0, 'p50': None, 'p90': None, 'p99': None, 'max': None, 'avg': None}
    return {'n': len(vals), 'p50': percentile(vals, 50), 'p90': percentile(vals, 90), 'p99': percentile(vals, 99),
            'max': vals[-1], 'avg': round(sum(vals) / len(vals), 1)}


def clean_text(value, what, limit=500):
    if not isinstance(value, str):
        raise ApiError(400, '%s must be text' % what)
    value = CTRL_RE.sub(' ', value).strip()
    if not value:
        raise ApiError(400, '%s is empty' % what)
    if len(value) > limit:
        raise ApiError(400, '%s is longer than %d characters' % (what, limit))
    return value


def _num(row, key):
    v = row.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


# ------------------------------------------------------------------ the application
class App(object):
    def __init__(self, pack=None, store=None, runs=None):
        if pack:
            os.environ['R3_PACK'] = os.path.abspath(pack)
        os.environ['R3_PACK'] = pack_root()  # children run in another folder: pin what they will find
        if store:
            os.environ['R3_STORE'] = os.path.abspath(store)
        os.environ['R3_STORE'] = store_dir()
        self.runs_dir = os.path.abspath(runs or os.path.join(PROJECT, 'runs'))
        self.token = secrets.token_urlsafe(24)
        self.run_lock = threading.Lock()  # one command at a time: no database-lock contention
        self.allowed_hosts = set()

    # ---- read-only information
    def info(self):
        people, clients = People(), Clients()
        s = Store()
        try:
            llm = LLM(s, 'ask')
            last, day1 = s.last_day(), s.meta('day1')
            llm_info = {'enabled': llm.enabled, 'provider': llm.provider, 'model': llm.model if llm.provider else None,
                        'embed_model': llm.embed_model() if llm.provider else None,
                        'off_by_switch': os.environ.get('R3_NO_LLM') == '1'}
        finally:
            s.close()
        days = []
        try:
            for fn in os.listdir(os.path.join(pack_root(), 'data', 'days')):
                m = re.match(r'^day-(\d+)\.jsonl$', fn)
                if m:
                    days.append(int(m.group(1)))
        except OSError:
            pass
        return {
            'pack': pack_root(), 'store': store_dir(), 'last_day': last, 'day1': day1, 'days_in_pack': sorted(days),
            'people': [{'id': p['id'], 'name': p.get('name'), 'role': p.get('role'), 'org': p['org'],
                        'zone': p.get('zone')} for p in people.by_id.values()],
            'orgs': {o: (v or {}).get('name') or o for o, v in (read_json(data_path('people.json'), {}) or {}).items()},
            'clients': [{'id': c['id'], 'name': c.get('name')} for c in clients.items],
            'llm': llm_info,
            'limits': {'watchdog_s': float(os.environ.get('R3_WATCHDOG_S') or 100), 'model_time_s': TOTAL_TIME_S,
                       'brief_tokens': 8000, 'ask_tokens': 4000},
        }

    def _check_person(self, person):
        if not isinstance(person, str) or not PERSON_RE.match(person) or person not in People().by_id:
            raise ApiError(400, 'unknown person')
        return person

    def meetings(self, person):
        self._check_person(person)
        from .brief import latest_events
        clients = Clients()
        s = Store()
        try:
            vis = s.visible(person, s.last_day())
            zone = s.people.zone(person)
            out = []
            for ev in latest_events(vis).values():
                if person not in (ev.get('attendees') or []):
                    continue
                st = parse_ts(ev.get('start'))
                if not st:
                    continue
                out.append({'event_id': ev.get('event_id'), 'title': ev.get('title') or ev.get('event_id'),
                            'client': ev.get('client'), 'client_name': clients.name(ev.get('client')) if ev.get('client') else None,
                            'date': local_date(st, zone).isoformat(), 'start': ev.get('start')})
        finally:
            s.close()
        out.sort(key=lambda m: (m['start'] or '', m['event_id'] or ''))
        return out[:80]

    def record(self, person, rid):
        """One record, only if `person` may see it: the same gate as every CLI command."""
        self._check_person(person)
        if not isinstance(rid, str) or not RECORD_ID_RE.match(rid):
            raise ApiError(400, 'bad record id')
        people = People()
        s = Store()
        try:
            vis = s.visible(person, s.last_day())
            if rid not in vis:  # unknown, hidden, or from the future: deliberately indistinguishable
                raise ApiError(404, 'not a record this person can see')
            rec = vis[rid]
            guard = Guard(person, vis, s.last_day(), s.memories(person, 'forget'), s.memories(person, 'suppress'), [])
            if guard.record_blocked(rec):  # what they asked to forget stays out of reach here too
                raise ApiError(404, 'not a record this person can see')
            text = record_text(rec)
            return {'id': rec.get('id'), 'type': rec.get('type'), 'at': rec.get('at'), 'day': rec.get('_day'),
                    'author': rec.get('author'), 'author_name': people.name(rec.get('author')),
                    'client': rec.get('client'), 'visibility': rec.get('visibility') or 'team',
                    'kind': rec.get('_kind'), 'title': rec.get('title') or rec.get('subject') or rec.get('name')
                    or rec.get('channel') or None, 'text': text[:4000], 'truncated': len(text) > 4000}
        finally:
            s.close()

    # ---- running commands
    def build_argv(self, body):
        cmd = body.get('cmd')
        if cmd == 'brief':
            person = self._check_person(body.get('person'))
            date = body.get('date')
            if not isinstance(date, str) or not DATE_RE.match(date) or not parse_day(date):
                raise ApiError(400, 'date must look like 2026-11-05')
            argv = ['brief', '--person', person, '--date', date]
            if body.get('summary'):
                argv.append('--summary')
            return argv
        if cmd == 'ask':
            person = self._check_person(body.get('person'))
            at = body.get('at')
            if not isinstance(at, str) or not AT_RE.match(at) or not parse_ts(at):
                raise ApiError(400, 'time must look like 2026-11-05T18:00:00Z (UTC)')
            return ['ask', '--person', person, '--at', at, '--ask', clean_text(body.get('ask'), 'the question')]
        if cmd == 'forget':
            person = self._check_person(body.get('person'))
            return ['forget', '--person', person, '--what', clean_text(body.get('what'), 'the text to forget')]
        if cmd == 'ingest':
            day = body.get('day')
            if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 999:
                raise ApiError(400, 'day must be a whole number from 1 to 999')
            if not os.path.isfile(os.path.join(pack_root(), 'data', 'days', 'day-%02d.jsonl' % day)):
                raise ApiError(400, 'day %d is not in the pack' % day)
            if day == 1 and body.get('confirm_reset') is not True:
                raise ApiError(400, 'ingest --day 1 starts a fresh store and erases the current one: confirm it')
            return ['ingest', '--day', str(day)]
        raise ApiError(400, 'unknown command')

    def run(self, body):
        argv = self.build_argv(body)
        if not self.run_lock.acquire(timeout=RUN_TIMEOUT_S + 5):
            raise ApiError(429, 'another command is still running')
        try:
            t0 = time.time()
            env = dict(os.environ, R3_SOURCE='web')
            try:
                p = subprocess.run([sys.executable, os.path.join(PROJECT, 'assist.py')] + argv, cwd=PROJECT, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=RUN_TIMEOUT_S)
                code, out, err = p.returncode, p.stdout, p.stderr
            except subprocess.TimeoutExpired as e:
                code, out, err = 'timeout', e.stdout or b'', (e.stderr or b'')
            ms = int((time.time() - t0) * 1000)
        finally:
            self.run_lock.release()
        text = out.decode('utf-8', 'replace')
        try:
            result, valid = json.loads(text), True
        except ValueError:
            result, valid = None, False
        shown = []
        for a in argv:  # keep the displayed command copy-pasteable
            shown.append('"%s"' % a.replace('"', '\\"') if re.search(r'[\s"\']', a) else a)
        return {'argv': argv, 'cli': 'python assist.py ' + ' '.join(shown), 'exit': code, 'ms': ms, 'valid_json': valid,
                'result': result, 'stderr': err.decode('utf-8', 'replace')[-1500:]}

    # ---- logs and metrics
    def metrics(self):
        commands = read_jsonl_tail(cmdlog.path(), 3000)
        usage = read_jsonl_tail(os.path.join(store_dir(), 'usage.jsonl'), 5000)
        by_cmd = {}
        for c in commands:
            d = by_cmd.setdefault(c.get('cmd') or '?', {'n': 0, 'ok': 0, 'fallback': 0, 'watchdog': 0, 'hit': 0, 'miss': 0,
                                                       'ms': []})
            d['n'] += 1
            if c.get('status') in ('ok', 'fallback', 'watchdog'):
                d[c['status']] += 1
            if c.get('cache') in ('hit', 'miss'):
                d[c['cache']] += 1
            d['ms'].append(c.get('ms'))
        cmd_stats = {}
        for k, d in by_cmd.items():
            cmd_stats[k] = {'n': d['n'], 'ok': d['ok'], 'fallback': d['fallback'], 'watchdog': d['watchdog'],
                            'cache_hit': d['hit'], 'cache_miss': d['miss'], 'ms': ms_stats(d['ms'])}
        hits = sum(1 for c in commands if c.get('cache') == 'hit')
        looks = sum(1 for c in commands if c.get('cache') in ('hit', 'miss'))
        asks = {}
        for c in commands:
            if c.get('cmd') == 'ask' and c.get('kind'):
                asks[c['kind']] = asks.get(c['kind'], 0) + 1
        briefs = [c for c in commands if c.get('cmd') == 'brief']
        via = {}
        for c in commands:
            via[c.get('via') or 'cli'] = via.get(c.get('via') or 'cli', 0) + 1
        by_purpose, by_model, errors = {}, {}, {}
        for u in usage:
            for table, key in ((by_purpose, u.get('purpose') or '?'),
                               (by_model, '%s / %s' % (u.get('provider') or '?', u.get('model') or '?'))):
                d = table.setdefault(key, {'n': 0, 'ok': 0, 'fail': 0, 'tokens_in': 0, 'tokens_out': 0, 'cost_usd': 0.0, 'ms': []})
                d['n'] += 1
                d['ok' if u.get('ok') else 'fail'] += 1
                d['tokens_in'] += _num(u, 'input_tokens')
                d['tokens_out'] += _num(u, 'output_tokens')
                d['cost_usd'] += _num(u, 'cost_usd')
                d['ms'].append(u.get('ms'))
            if not u.get('ok') and u.get('error'):
                errors[str(u['error'])[:120]] = errors.get(str(u['error'])[:120], 0) + 1

        def fold(table):
            out = {}
            for k, d in table.items():
                out[k] = {'n': d['n'], 'ok': d['ok'], 'fail': d['fail'], 'tokens_in': d['tokens_in'],
                          'tokens_out': d['tokens_out'], 'cost_usd': round(d['cost_usd'], 6), 'ms': ms_stats(d['ms'])}
            return out
        return {
            'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'files': {'commands': {'path': cmdlog.path(), 'rows': len(commands)},
                      'usage': {'path': os.path.join(store_dir(), 'usage.jsonl'), 'rows': len(usage)}},
            'commands': {
                'n': len(commands), 'ms': ms_stats([c.get('ms') for c in commands]), 'by_cmd': cmd_stats,
                'status': {s: sum(1 for c in commands if c.get('status') == s) for s in ('ok', 'fallback', 'watchdog')},
                'cache': {'hits': hits, 'lookups': looks, 'rate': round(hits / looks, 3) if looks else None},
                'ask_kinds': asks, 'via': via,
                'briefs': {'meetings': sum(_num(c, 'meetings') for c in briefs), 'with_changes': sum(_num(c, 'briefs') for c in briefs),
                           'changes': sum(_num(c, 'changes') for c in briefs), 'summaries': sum(_num(c, 'summaries') for c in briefs)},
                'tokens_in': sum(_num(c, 'tokens_in') for c in commands), 'tokens_out': sum(_num(c, 'tokens_out') for c in commands),
                'cost_usd': round(sum(_num(c, 'cost_usd') for c in commands), 6),
                'timeline': [{'ts': c.get('ts'), 'cmd': c.get('cmd'), 'ms': c.get('ms'), 'status': c.get('status'),
                              'cache': c.get('cache')} for c in commands[-300:]],
            },
            'model': {
                'n': len(usage), 'ok': sum(1 for u in usage if u.get('ok')), 'fail': sum(1 for u in usage if not u.get('ok')),
                'tokens_in': sum(_num(u, 'input_tokens') for u in usage), 'tokens_out': sum(_num(u, 'output_tokens') for u in usage),
                'cost_usd': round(sum(_num(u, 'cost_usd') for u in usage), 6), 'ms': ms_stats([u.get('ms') for u in usage]),
                'by_purpose': fold(by_purpose), 'by_model': fold(by_model),
                'errors': sorted(({'error': k, 'n': v} for k, v in errors.items()), key=lambda e: -e['n'])[:8],
                'timeline': [{'ts': u.get('ts'), 'purpose': u.get('purpose'), 'ms': u.get('ms'), 'ok': bool(u.get('ok')),
                              'tokens_in': _num(u, 'input_tokens'), 'tokens_out': _num(u, 'output_tokens')} for u in usage[-300:]],
            },
        }

    def logs(self, stream, limit, cmd=None, status=None, query=None):
        if stream not in ('commands', 'usage'):
            raise ApiError(400, 'stream must be commands or usage')
        limit = max(1, min(500, limit))
        path = cmdlog.path() if stream == 'commands' else os.path.join(store_dir(), 'usage.jsonl')
        rows = read_jsonl_tail(path, 5000)
        q = (query or '').lower()
        out = []
        for r in reversed(rows):
            if stream == 'commands':
                if cmd and r.get('cmd') != cmd:
                    continue
                if status and r.get('status') != status:
                    continue
            else:
                if status == 'fail' and r.get('ok'):
                    continue
                if status == 'ok' and not r.get('ok'):
                    continue
                if cmd and r.get('purpose') != cmd:
                    continue
            if q and q not in json.dumps(r).lower():
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return {'stream': stream, 'rows': out, 'scanned': len(rows)}

    def runs(self):
        out = []
        try:
            names = os.listdir(self.runs_dir)
        except OSError:
            return {'runs': [], 'dir': self.runs_dir}
        for name in names:
            d = os.path.join(self.runs_dir, name)
            log_path = os.path.join(d, 'log.jsonl')
            if not (NAME_RE.match(name) and not name.startswith('.') and os.path.isfile(log_path)):
                continue
            entry = {'name': name, 'mtime': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(os.path.getmtime(log_path)))}
            try:
                with open(os.path.join(d, 'score.json'), encoding='utf-8') as f:
                    sc = json.load(f)
                entry.update(total=sc.get('total'), max_scored=sc.get('max_scored'), days=sc.get('days'))
            except (OSError, ValueError):
                pass
            out.append(entry)
        out.sort(key=lambda e: e['mtime'], reverse=True)
        return {'runs': out[:60], 'dir': self.runs_dir}

    def run_detail(self, name):
        if not isinstance(name, str) or not NAME_RE.match(name) or name.startswith('.'):
            raise ApiError(400, 'bad run name')
        d = os.path.realpath(os.path.join(self.runs_dir, name))
        if os.path.dirname(d) != os.path.realpath(self.runs_dir) or not os.path.isfile(os.path.join(d, 'log.jsonl')):
            raise ApiError(404, 'no such run')
        calls = read_jsonl_tail(os.path.join(d, 'log.jsonl'), 20000)
        kinds, per_day, failures = {}, {}, []
        for c in calls:
            k = kinds.setdefault(c.get('kind') or '?', {'n': 0, 'failed': 0, 'ms': []})
            k['n'] += 1
            bad = c.get('exit') not in (0, None) or c.get('valid') is False
            if bad:
                k['failed'] += 1
                failures.append({'day': c.get('day'), 'kind': c.get('kind'), 'exit': c.get('exit'), 'valid': c.get('valid'),
                                 'stderr_tail': (c.get('stderr_tail') or '')[-300:]})
            k['ms'].append(c.get('ms'))
            per_day[str(c.get('day'))] = per_day.get(str(c.get('day')), 0) + _num(c, 'ms')
        slow = sorted(calls, key=lambda c: -_num(c, 'ms'))[:8]
        detail = {'name': name,
                  'calls': {'n': len(calls), 'ms': ms_stats([c.get('ms') for c in calls]),
                            'by_kind': {k: {'n': v['n'], 'failed': v['failed'], 'ms': ms_stats(v['ms'])} for k, v in kinds.items()},
                            'per_day_ms': per_day, 'failures': failures[:20],
                            'slowest': [{'day': c.get('day'), 'kind': c.get('kind'), 'ms': c.get('ms'),
                                         'args': ' '.join(str(a) for a in (c.get('args') or []))[:140]} for c in slow]},
                  'score': None}
        try:
            with open(os.path.join(d, 'score.json'), encoding='utf-8') as f:
                sc = json.load(f)
            items = sc.get('items') or []
            worst = sorted(items, key=lambda i: (_num(i, 'score') + _num(i, 'honesty'), _num(i, 'day')))[:10]
            detail['score'] = {
                'total': sc.get('total'), 'max_scored': sc.get('max_scored'), 'usefulness': sc.get('usefulness'),
                'gate': sc.get('gate'), 'days': sc.get('days'), 'areas': sc.get('areas'), 'per_day': sc.get('per_day'),
                'privacy_capped': sc.get('privacy_capped'), 'privacy_leaks': sc.get('privacy_leaks') or [],
                'generated_at': sc.get('generated_at'),
                'worst': [{'id': i.get('id'), 'kind': i.get('kind'), 'day': i.get('day'), 'person': i.get('person'),
                           'score': i.get('score'), 'honesty': i.get('honesty'),
                           'why': '; '.join((i.get('reasons') or []) + (i.get('pens') or []) + (i.get('priv') or []))[:240]}
                          for i in worst]}
        except (OSError, ValueError):
            pass
        return detail


# ------------------------------------------------------------------ HTTP
def make_handler(app):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'R3Console'  # HTTP/1.0: one request per connection, so a rejected body cannot leak on

        def log_message(self, fmt, *args):  # quiet: the CLI's own command log is the record
            pass

        # ---- plumbing
        def _headers(self, ctype, length, extra=None):
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self._headers(ctype, len(body), extra)
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=True).encode('utf-8'), 'application/json; charset=utf-8')

        def _host_ok(self):
            return (self.headers.get('Host') or '').lower() in app.allowed_hosts

        def _api_ok(self):
            token = self.headers.get('X-R3-Token') or ''
            return hmac.compare_digest(token.encode('utf-8'), app.token.encode('utf-8'))

        def _static_file(self, filename):
            path = os.path.join(WEB_DIR, filename)
            if not os.path.isfile(path):
                raise ApiError(404, 'not found')
            with open(path, 'rb') as f:
                return f.read()

        # ---- routes
        def do_GET(self):
            self._route('GET')

        def do_POST(self):
            self._route('POST')

        def do_PUT(self):
            self._json(405, {'error': 'method not allowed'})

        do_DELETE = do_PATCH = do_PUT

        def _route(self, method):
            try:
                if not self._host_ok():
                    return self._json(403, {'error': 'unknown Host header'})
                url = urllib.parse.urlparse(self.path)
                path = url.path
                q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
                if path in PAGES and method == 'GET':
                    body = self._static_file(PAGES[path]).replace(b'{{TOKEN}}', app.token.encode('ascii'))
                    return self._send(200, body, 'text/html; charset=utf-8', {'Content-Security-Policy': CSP})
                if path.startswith('/static/') and method == 'GET':
                    name = path[len('/static/'):]
                    ext = os.path.splitext(name)[1]
                    if not NAME_RE.match(name) or ext not in STATIC_TYPES:
                        raise ApiError(404, 'not found')
                    return self._send(200, self._static_file(name), STATIC_TYPES[ext])
                if path.startswith('/api/'):
                    if not self._api_ok():
                        return self._json(403, {'error': 'missing or wrong token'})
                    return self._api(method, path, q)
                raise ApiError(404, 'not found')
            except ApiError as e:
                self._json(e.status, {'error': e.message})
            except Exception as e:  # noqa: BLE001 - a bug must not take the server down
                sys.stderr.write('[web] %s %s failed: %r\n' % (method, self.path, e))
                self._json(500, {'error': 'internal error'})

        def _api(self, method, path, q):
            if method == 'POST':
                if path != '/api/run':
                    raise ApiError(404, 'not found')
                origin = self.headers.get('Origin')
                if origin and origin.lower() != 'http://' + (self.headers.get('Host') or '').lower():
                    raise ApiError(403, 'cross-origin request refused')
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                except ValueError:
                    raise ApiError(400, 'bad Content-Length')
                if length > MAX_BODY:
                    raise ApiError(413, 'request too large')
                try:
                    body = json.loads(self.rfile.read(length).decode('utf-8'))
                except ValueError:
                    raise ApiError(400, 'body must be JSON')
                if not isinstance(body, dict):
                    raise ApiError(400, 'body must be a JSON object')
                return self._json(200, app.run(body))
            if path == '/api/info':
                return self._json(200, app.info())
            if path == '/api/meetings':
                return self._json(200, {'meetings': app.meetings(q.get('person'))})
            if path == '/api/record':
                return self._json(200, app.record(q.get('person'), q.get('id')))
            if path == '/api/metrics':
                return self._json(200, app.metrics())
            if path == '/api/logs':
                try:
                    limit = int(q.get('limit') or 100)
                except ValueError:
                    raise ApiError(400, 'bad limit')
                return self._json(200, app.logs(q.get('stream') or 'commands', limit, q.get('cmd') or None,
                                                q.get('status') or None, q.get('q')))
            if path == '/api/runs':
                return self._json(200, app.runs())
            if path == '/api/run-detail':
                return self._json(200, app.run_detail(q.get('name')))
            raise ApiError(404, 'not found')

    return Handler


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(app, host='127.0.0.1', port=8765):
    """Bind and return the server (not yet serving). app.allowed_hosts is set from the real port."""
    srv = Server((host, port), make_handler(app))
    real = srv.server_address[1]
    names = {'127.0.0.1', 'localhost', '[::1]'}
    if host not in ('127.0.0.1', 'localhost', '::1', '0.0.0.0', '::', ''):
        names.add(host.lower())  # a specific address: people will type exactly that. A wildcard adds nothing.
    app.allowed_hosts = set('%s:%d' % (n, real) for n in names)
    return srv
