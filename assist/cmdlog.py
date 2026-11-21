# -*- coding: utf-8 -*-
"""One line per command in <store>/commands.jsonl: the data behind the metrics page.

A line says which command ran, for whom, how long it took, whether it came from the cache, how it
ended (ok / fallback / watchdog) and a few numbers about the result. It never holds content: no answer
text, no brief lines, and no question or forget text (a log must not keep what a person asked the
helper to forget). A question is identified by its length and a short hash, so repeated questions can
still be told apart. Set R3_LOG_ASK_TEXT=1 to also keep the first 200 characters of questions.

Writing the log can never break a command: every failure is swallowed. The file is bounded (see
common.trim_log) and starts fresh whenever `ingest --day 1` rebuilds the store.
"""
import hashlib
import os
import json
import time

from .common import log, trim_log
from .store import store_dir

MAX_BYTES = 2000000
KEEP_LINES = 3000
FILENAME = 'commands.jsonl'


def path():
    return os.path.join(store_dir(), FILENAME)


def _usage_totals(items):
    tin = sum(int((i.get('usage') or {}).get('input_tokens') or 0) for i in items)
    tout = sum(int((i.get('usage') or {}).get('output_tokens') or 0) for i in items)
    cost = sum(float((i.get('usage') or {}).get('cost_usd') or 0.0) for i in items)
    return {'tokens_in': tin, 'tokens_out': tout, 'cost_usd': round(cost, 6)}


def result_info(cmd, out):
    """A few numbers about the result, never its content."""
    info = {}
    if cmd == 'brief' and isinstance(out, list):
        briefs = [b for b in out if isinstance(b, dict)]
        info.update(meetings=len(briefs), briefs=sum(1 for b in briefs if b.get('status') == 'brief'),
                    changes=sum(len(b.get('changed') or []) for b in briefs),
                    promises=sum(len(b.get('promises') or []) for b in briefs))
        if any('summary' in b for b in briefs):
            info['summaries'] = sum(1 for b in briefs if b.get('summary'))
        info.update(_usage_totals(briefs))
    elif cmd == 'ask' and isinstance(out, dict):
        ans = out.get('answer')
        if out.get('refused'):
            info['kind'] = 'refused'
        elif out.get('clarify'):
            info['kind'] = 'clarify'
        elif isinstance(ans, dict) and ans.get('text') == 'I have nothing on that.':
            info['kind'] = 'nothing'
        else:
            info['kind'] = 'answer'
        info['sources'] = len((ans or {}).get('sources') or []) if isinstance(ans, dict) else 0
        info.update(_usage_totals([out]))
    elif cmd == 'ingest' and isinstance(out, dict):
        for k in ('records', 'feedback', 'model_calls', 'model_calls_skipped'):
            if isinstance(out.get(k), int):
                info[k] = out[k]
        pend = out.get('model_pending')
        if isinstance(pend, dict):
            info['pending'] = sum(v for v in pend.values() if isinstance(v, int))
        if out.get('skipped'):
            info['skipped'] = True
    elif cmd == 'forget' and isinstance(out, dict):
        info['ok'] = bool(out.get('ok'))
    return info


def entry_for(a, out, status, ms, cache=None):
    entry = {'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'cmd': a.cmd,
             'via': os.environ.get('R3_SOURCE') or 'cli', 'status': status, 'ms': int(ms), 'cache': cache}
    for k in ('person', 'date', 'at', 'day'):
        v = getattr(a, k, None)
        if v is not None:
            entry[k] = v
    text = getattr(a, 'ask', None)
    if text:
        entry['ask_len'] = len(text)
        entry['ask_id'] = hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]
        if os.environ.get('R3_LOG_ASK_TEXT') == '1':
            entry['ask'] = text[:200]
    if getattr(a, 'summary', False):
        entry['summary'] = True
    entry.update(result_info(a.cmd, out))
    return entry


def record(a, out, status, ms, cache=None):
    """Append one line. Never raises."""
    try:
        p = path()
        trim_log(p, MAX_BYTES, KEEP_LINES)
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry_for(a, out, status, ms, cache), ensure_ascii=True) + '\n')
    except Exception as e:  # noqa: BLE001 - observability must never break a command
        log('command log skipped:', e)
