#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Meeting brief helper (BuildSprint R3): one command, four subcommands.

  python3 assist.py ingest --day N
  python3 assist.py brief  --person P --date YYYY-MM-DD [--summary]
  python3 assist.py ask    --person P --at ISO --ask "question"
  python3 assist.py forget --person P --what "words"

Only JSON goes to stdout, logs go to stderr, and the exit code is always 0. Data is found through
$R3_PACK, else the current folder. The store lives in $R3_STORE, else ./.assist_store/. A watchdog
(R3_WATCHDOG_S, default 100 s) prints the safe fallback JSON if a command is somehow stuck, so the
harness's 120 s kill never leaves a call without valid JSON.
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from assist.common import elapsed_ms, log  # noqa: E402

ZERO_USAGE = {'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0.0, 'ms': 0}


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + '\n')
    sys.stdout.flush()


def glue_values(argv):
    """Free-text values could start with '-' and confuse argparse, so pass them as --ask=..."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] in ('--ask', '--what') and i + 1 < len(argv):
            out.append(argv[i] + '=' + argv[i + 1])
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


def parse(argv):
    ap = argparse.ArgumentParser(prog='assist.py', description='Meeting brief helper')
    sub = ap.add_subparsers(dest='cmd')
    s = sub.add_parser('ingest')
    s.add_argument('--day', type=int, required=True)
    s = sub.add_parser('brief')
    s.add_argument('--person', required=True)
    s.add_argument('--date', required=True)
    s.add_argument('--summary', action='store_true',
                   help='also write a short model summary per meeting, from its approved lines only')
    s = sub.add_parser('ask')
    s.add_argument('--person', required=True)
    s.add_argument('--at', required=True)
    s.add_argument('--ask', required=True)
    s = sub.add_parser('forget')
    s.add_argument('--person', required=True)
    s.add_argument('--what', required=True)
    return ap.parse_args(argv)


def fallback(cmd, error):
    if cmd == 'brief':
        return []
    if cmd == 'ask':
        return {'answer': {'text': 'I have nothing on that.', 'sources': []}, 'matched': [], 'clarify': None,
                'refused': None, 'usage': dict(ZERO_USAGE, ms=elapsed_ms())}
    return {'ok': False, 'error': error}


def cache_key(a, last_day):
    parts = [a.cmd, a.person, getattr(a, 'date', None), getattr(a, 'at', None), getattr(a, 'ask', None), last_day]
    if getattr(a, 'summary', False):
        parts.append('summary')  # a summarised brief and a normal one never share a cache entry
    return hashlib.sha1(json.dumps(parts).encode('utf-8')).hexdigest()


def dispatch(a):
    from assist.store import Store
    store = Store()
    try:
        if a.cmd == 'ingest':
            from assist import ingest
            return ingest.run(store, a.day)
        if a.cmd == 'forget':
            from assist import feedback
            feedback.remember_forget(store, a.person, a.what, store.last_day())
            store.commit()
            return {'ok': True}
        key = cache_key(a, store.last_day())
        cached = store.out_get(key)
        if cached is not None:  # the same brief or question asked twice returns the same JSON
            return cached
        cacheable = True
        if a.cmd == 'brief':
            from assist import brief
            out = brief.build(store, a.person, a.date, summary=a.summary)
            ms = elapsed_ms()
            for b in out:
                b['usage']['ms'] = ms
            if a.summary:  # a summary lost to an API error must not be frozen into the cache as null
                cacheable = all(b.get('summary') for b in out if b.get('status') == 'brief')
        else:
            from assist import ask
            out, llm = ask.run(store, a.person, a.at, a.ask)
            out['usage'] = llm.usage(elapsed_ms())
        if cacheable:
            store.out_put(key, out)
        store.commit()
        return out
    finally:
        store.close()


WATCHDOG_S = 100.0  # the harness kills a command at 120 s; answer with valid JSON before that
_state = {'done': False}
_out_lock = threading.Lock()


def start_watchdog(cmd):
    """If the command has not printed its result after R3_WATCHDOG_S seconds (default 100; 0 = off),
    whatever it is stuck on (a locked database, a hung network call), print the safe fallback JSON and
    exit 0 instead of being killed with no output. Committed data is safe: an interrupted SQLite
    transaction is rolled back the next time the store is opened."""
    try:
        limit = float(os.environ.get('R3_WATCHDOG_S') or WATCHDOG_S)
    except ValueError:
        limit = WATCHDOG_S
    if limit <= 0:
        return None

    def fire():
        with _out_lock:
            if _state['done']:
                return
            _state['done'] = True
            log('watchdog: no result after %.0f s, answering with the safe fallback' % limit)
            emit(fallback(cmd, 'timed out after %.0f s' % limit))
            try:
                sys.stderr.flush()  # os._exit skips the normal flush, and piped stderr is block-buffered on 3.8
            except Exception:  # noqa: BLE001
                pass
            os._exit(0)
    timer = threading.Timer(limit, fire)
    timer.daemon = True
    timer.start()
    return timer


def finish(obj):
    """Print the one result, unless the watchdog already printed the fallback."""
    with _out_lock:
        if _state['done']:
            return
        _state['done'] = True
        emit(obj)


def main():
    argv = glue_values(sys.argv[1:])
    cmd = argv[0] if argv else ''
    try:
        a = parse(argv)
    except SystemExit:
        emit(fallback(cmd, 'bad arguments'))
        return 0
    if not a.cmd:
        emit(fallback('', 'no command'))
        return 0
    start_watchdog(a.cmd)
    try:
        out = dispatch(a)
    except Exception:  # noqa: BLE001 - never crash, never print non-JSON
        log(traceback.format_exc())
        out = fallback(a.cmd, 'internal error (see stderr)')
    finish(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
