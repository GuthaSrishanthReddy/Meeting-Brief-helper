#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Meeting brief helper (BuildSprint R3): one command, four subcommands.

  python3 assist.py ingest --day N
  python3 assist.py brief  --person P --date YYYY-MM-DD
  python3 assist.py ask    --person P --at ISO --ask "question"
  python3 assist.py forget --person P --what "words"

Only JSON goes to stdout, logs go to stderr, and the exit code is always 0. Data is found through
$R3_PACK, else the current folder. The store lives in $R3_STORE, else ./.assist_store/.
"""
import argparse
import hashlib
import json
import os
import sys
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
        if a.cmd == 'brief':
            from assist import brief
            out = brief.build(store, a.person, a.date)
            ms = elapsed_ms()
            for b in out:
                b['usage']['ms'] = ms
        else:
            from assist import ask
            out, llm = ask.run(store, a.person, a.at, a.ask)
            out['usage'] = llm.usage(elapsed_ms())
        store.out_put(key, out)
        store.commit()
        return out
    finally:
        store.close()


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
    try:
        out = dispatch(a)
    except Exception:  # noqa: BLE001 - never crash, never print non-JSON
        log(traceback.format_exc())
        out = fallback(a.cmd, 'internal error (see stderr)')
    emit(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
