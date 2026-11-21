# -*- coding: utf-8 -*-
"""ingest --day N: read day N and yesterday's feedback, classify records, extract facts.

Day 1 starts a fresh store. Only day N's file is read, never a later day. Every fact is keyed by
its source record id, so the visibility gate in store.visible() applies to it at query time:

  decision      'Decision:' lines in meeting notes
  promise       commitments in staff chats and mails (patterns first, model fallback)
  delivery      'Sharing the X as promised'
  ownership     announcements in staff messages; '<Function> contact: Name' doc lines
  leave         "I'm on leave from 9 Nov till the 12th"
  date_request  'we need to move the launch to 14 Nov'
  doc_line      a labelled doc line when it is new or changed (unchanged lines keep their first date)
  doc_change    a labelled doc line whose value changed between versions
  sheet_change  a sheet cell whose value changed between versions

Task changes are not copied: they are replayed from the visible task records at query time.
"""
import collections
import datetime as dt
import os
import re

from . import feedback, owners, promises
from .clients import ClientResolver, abbreviations
from .common import (Clients, canon_ts, content_words, data_path, find_dates, local_date, log, parse_ts,
                     read_jsonl, sentences, words)
from .llm import LLM
from .store import CITABLE_KINDS

DECISION = re.compile(r'^\s*(?:decision|decided|agreed|resolution)\s*[:\-–]\s*(.+?)\s*$', re.I)
LABEL_LINE = re.compile(u"^\\s*([A-Za-z][A-Za-z0-9 &/()'’+\\-]{0,48}?)\\s*:\\s*(.+?)\\s*$")
CELL = re.compile(r'^([A-Z]+)(\d+)$')
REQ_ACTIVE = re.compile(
    r'\b(?:move|moving|shift|shifting|push|pushing|postpone|postponing|reschedule|rescheduling|delay|delaying|'
    r'prepone|bring\s+forward|pull\s+in|change|changing)\s+(?:the\s+|our\s+|your\s+)?(?P<what>[A-Za-z][A-Za-z0-9 \-]{1,40}?)'
    r'\s+(?:date\s+)?(?:to|till|until|for|on)\s+(?P<when>[^.;!?\n]{2,40})', re.I)
REQ_PASSIVE = re.compile(
    r'(?P<what>[A-Za-z][A-Za-z0-9 \-]{1,60}?)\s+(?:is\s+|has\s+been\s+|was\s+|got\s+|will\s+be\s+)?(?:moved|shifted|'
    r'pushed|postponed|rescheduled|delayed|preponed|brought\s+forward)\s+(?:to|till|until|for|on)\s+(?P<when>[^.;!?\n]{2,40})',
    re.I)
SUBJECT_DROP = set('need needs want wants like please can could would we us our have has date dates new'.split())


def classify(r, people):
    t = r.get('type')
    author = r.get('author') or ''
    if t == 'notification' or author == 'system':
        return 'notification'
    if author.startswith('ext:'):
        return 'client_external' if r.get('client') else 'external_untagged'
    if author in people.by_id:
        return 'staff'
    return 'staff' if re.match(r'^[a-z]\d+$', author) else 'unknown'


def update_day_dates(store, day, rows, people):
    """Remember which calendar date day 1 is, from the local dates of this day's records."""
    if store.meta('day1'):
        return
    counts = collections.Counter()
    for r in rows:
        ts = parse_ts(r.get('at'))
        if not ts:
            continue
        a = r.get('author')
        zone = people.zone(a) if a in people.by_id else people.default_zone(r.get('org'))
        counts[local_date(ts, zone)] += 1
    if counts:
        d = counts.most_common(1)[0][0]
        store.set_meta('day1', (d - dt.timedelta(days=day - 1)).isoformat())


def decisions(body):
    out = []
    for line in (body or '').split('\n'):
        m = DECISION.match(line)
        if m:
            t = m.group(1).strip().rstrip('.')
            if t:
                out.append(t[0].upper() + t[1:])
    return out


def labelled_lines(body):
    out = collections.OrderedDict()
    for line in (body or '').split('\n'):
        if len(line) > 160:
            continue
        m = LABEL_LINE.match(line)
        if not m:
            continue
        label, value = m.group(1).strip(), m.group(2).strip().rstrip('.')
        if len(label.split()) > 6 or not value:
            continue
        out.setdefault(label.lower(), (label, value))
    return out


def previous_version(store, r, id_field):
    best = None
    for p in store.versions(r.get('type'), r.get(id_field), r.get('org')):  # indexed by doc/sheet id
        if p['id'] == r['id']:
            continue
        try:
            if int(p.get('version') or 0) >= int(r.get('version') or 0):
                continue
        except (TypeError, ValueError):
            continue
        if best is None or (int(p.get('version') or 0), p['at']) > (int(best.get('version') or 0), best['at']):
            best = p
    return best


def subject_of(what, people, clients):
    drop = set(SUBJECT_DROP)
    for p in people.by_id.values():
        drop.update(words((p.get('name') or '').split()[0] if p.get('name') else ''))
    for c in clients.items:
        drop.update(words(c.get('name')))
    ws = [w for w in content_words(what) if w not in drop]
    return ' '.join(ws[-3:])


def date_requests(r, people, clients):
    text = r.get('body') or ''
    ts = parse_ts(r.get('at'))
    if not ts or not text:
        return []
    author = r.get('author') or ''
    zone = people.zone(author) if author in people.by_id else people.default_zone(r.get('org'))
    ref = local_date(ts, zone)
    out = []
    for sent in sentences(text):
        if owners.is_question(sent):
            continue
        for rx in (REQ_ACTIVE, REQ_PASSIVE):
            m = rx.search(sent)
            if not m:
                continue
            ds = find_dates(m.group('when'), ref)
            subj = subject_of(m.group('what'), people, clients)
            if ds and subj:
                out.append({'subject': subj, 'new': ds[0]['date'].isoformat(), 'sentence': sent[:300],
                            'external': author.startswith('ext:'), 'author': author})
                break
    return out


def doc_facts(store, r, day, at, people):
    prev = previous_version(store, r, 'doc_id')
    cur = labelled_lines(r.get('body'))
    old = labelled_lines(prev.get('body')) if prev else {}
    org, author, client = r.get('org'), r.get('author'), r.get('client')
    for key, (label, value) in cur.items():
        if key in old and old[key][1] == value:
            continue  # unchanged between versions: the fact keeps its first version's date
        store.add_fact('doc_line', r['id'], day, at, client, {
            'doc_id': r.get('doc_id'), 'title': r.get('title'), 'label': label, 'value': value,
            'version': r.get('version')})
        if key in old:
            store.add_fact('doc_change', r['id'], day, at, client, {
                'doc_id': r.get('doc_id'), 'title': r.get('title'), 'label': label, 'old': old[key][1],
                'new': value, 'old_rid': prev['id']})
        own = owners.from_doc_line(label, value, people, org, author)
        if own and client:
            store.add_fact('ownership', r['id'], day, at, client, own)


def sheet_facts(store, r, day, at):
    prev = previous_version(store, r, 'sheet_id')
    if not prev:
        return
    cur = r.get('cells') if isinstance(r.get('cells'), dict) else {}
    old = prev.get('cells') if isinstance(prev.get('cells'), dict) else {}
    for cell in sorted(set(cur) | set(old)):
        a, b = old.get(cell), cur.get(cell)
        if a == b:
            continue
        m = CELL.match(cell)
        row_label = col_label = None
        if m:
            col, row = m.group(1), m.group(2)
            if col != 'A':
                row_label = cur.get('A' + row) or old.get('A' + row)
            if row != '1':
                col_label = cur.get(col + '1') or old.get(col + '1')
        store.add_fact('sheet_change', r['id'], day, at, r.get('client'), {
            'sheet_id': r.get('sheet_id'), 'name': r.get('name') or r.get('body'), 'cell': cell,
            'row_label': row_label, 'col_label': col_label, 'old': a, 'new': b, 'old_rid': prev['id']})


ABBREV_FACTS_PER_TOKEN = 20  # one short form needs a few sources, not one fact per message


def run(store, day):
    path = data_path('days', 'day-%02d.jsonl' % day)
    if day != 1 and day in set(store.meta('days', []) or []):
        return {'ok': True, 'day': day, 'skipped': 'already ingested'}
    # A day with no data file is a quiet day only when it is the very next one. Anything else (a typo such
    # as --day 999, a wrong R3_PACK) must change nothing: marking it ingested would push last_day past real
    # time, make every later meeting look as if it had already happened, and (for day 1) wipe a good store.
    if day < 1 or (not os.path.isfile(path) and not (day > 1 and day == store.last_day() + 1)):
        log('ingest: no data for day %d (looked for %s); nothing was changed' % (day, path))
        return {'ok': False, 'day': day, 'error': 'no data for day %d; nothing was changed' % day}
    if day == 1:
        store.reset()
    done = set(store.meta('days', []) or [])
    if day in done:
        return {'ok': True, 'day': day, 'skipped': 'already ingested'}
    people, clients = store.people, Clients()
    resolver = ClientResolver(clients, people, {})
    llm = LLM(store, 'ingest', budget=None)
    rows = [r for r in read_jsonl(path) if r.get('id')]
    kinds = {}
    for seq, r in enumerate(rows):
        kinds[r['id']] = classify(r, people)
        store.add_record(r, day, seq, kinds[r['id']], canon_ts(r.get('at')))
    update_day_dates(store, day, rows, people)
    # Feedback first: preferences, corrections and forgets are the person's explicit instructions, so
    # they get the model's time before the background passes below.
    fb = feedback.apply_day(store, day, people, resolver, llm)

    promise_cands, owner_cands = [], []
    abbrev_seen = {}
    ordered = sorted(enumerate(rows), key=lambda x: (canon_ts(x[1].get('at')), x[0]))
    for _, r in ordered:
        kind = kinds[r['id']]
        if kind not in CITABLE_KINDS:
            continue  # notifications, newsletters, vendor mail, injections: never facts
        rid, t, at = r['id'], r.get('type'), canon_ts(r.get('at'))
        client, body, author, org = r.get('client'), r.get('body') or '', r.get('author') or '', r.get('org')
        try:
            if client:
                for tok in abbreviations(body, clients.name(client)):  # "NB" for Northbeam Retail, learned once
                    if abbrev_seen.get((client, tok), 0) < ABBREV_FACTS_PER_TOKEN:
                        abbrev_seen[(client, tok)] = abbrev_seen.get((client, tok), 0) + 1
                        store.add_fact('abbrev', rid, day, at, client, {'tok': tok})
            if t == 'note':
                for text in decisions(body):
                    store.add_fact('decision', rid, day, at, client, {'text': text, 'event_id': r.get('event_id')})
            if t in ('chat', 'mail'):
                if kind == 'staff':
                    found = promises.extract(body, at, people.zone(author))
                    for p in found:
                        store.add_fact('promise', rid, day, at, client, dict(p, owner=author, via='pattern'))
                    if not found and promises.looks_like_commitment(body):
                        ts = parse_ts(at)
                        ld = local_date(ts, people.zone(author)) if ts else None
                        promise_cands.append({'id': rid, 'text': body[:400], 'local_date': ld.isoformat() if ld else '',
                                              'weekday': ld.strftime('%A') if ld else ''})
                    what = promises.extract_delivery(body)
                    if what:
                        store.add_fact('delivery', rid, day, at, client, {'what': what, 'by': author})
                    stmts = owners.statements(body, client, author, people, resolver, at, org)
                    for s in stmts:
                        store.add_fact('ownership', rid, day, at, s['client'], s)
                    if not stmts and people.tier(author) >= 3 and owners.OWNER_HINT.search(body) and \
                            not owners.is_question(body):
                        owner_cands.append({'id': rid, 'text': body[:400], 'client_tag': client or ''})
                    for lv in owners.leaves(body, author, people, at, org):
                        store.add_fact('leave', rid, day, at, client, lv)
                for rq in date_requests(r, people, clients):
                    store.add_fact('date_request', rid, day, at, client, rq)
            if t == 'doc':
                doc_facts(store, r, day, at, people)
            if t == 'sheet':
                sheet_facts(store, r, day, at)
        except Exception as e:  # noqa: BLE001 - one odd record must not stop the day
            log('record %s skipped during extraction: %s' % (rid, e))

    pending = model_passes(store, llm, people, clients, {r['id']: r for r in rows}, day, promise_cands, owner_cands)
    store.set_meta('days', sorted(done | {day}))
    store.set_meta('last_day', max([day] + list(done)))
    store.out_clear()
    store.commit()
    return {'ok': True, 'day': day, 'records': len(rows), 'feedback': fb,
            'model_calls': llm.calls, 'model_input_tokens': llm.input_tokens,
            'model_calls_skipped': llm.skipped, 'model_pending': pending}


# ------------------------------------------------------------------ model passes (with a backlog)
PENDING_CAP = {'promises': 200, 'owners': 60}  # bounds the saved backlog, newest kept
MAX_TRIES = 3  # a candidate the model could not answer is retried on this many later days, then dropped


def _merge(old, new, cap):
    seen, out = set(), []
    for c in list(old) + list(new):
        if c['id'] not in seen:
            seen.add(c['id'])
            out.append(c)
    return out[-cap:]


def model_passes(store, llm, people, clients, by_id, day, promise_cands, owner_cands):
    """Language fallbacks: only messages the patterns flagged but could not read.

    The model's time is capped per command (see llm.py). Whatever it cannot reach in time is saved
    and retried at the next ingest, so a slow provider delays these facts instead of losing them.
    Ownership goes first (few messages, high value), then promises.
    """
    if not llm.enabled:
        return {'promises': 0, 'owners': 0}  # nothing to retry later without a model
    saved = store.meta('pending_model', {}) or {}
    tries = dict(saved.get('tries') or {})
    prom = _merge(saved.get('promises') or [], promise_cands, PENDING_CAP['promises'])
    own = _merge(saved.get('owners') or [], owner_cands, PENDING_CAP['owners'])

    def rec_of(rid):
        r = by_id.get(rid)
        return (r, day) if r is not None else ((store.record(rid) or None), None)

    found, left_o = owners.model_pass(llm, own, [c['id'] for c in clients.items])
    for rid, it in found.items():
        r, d = rec_of(rid)
        if r is None:
            continue
        org = r.get('org')
        func = [w for w in content_words(it.get('function')) if w]
        cid = it.get('client') if it.get('client') in clients.by_id else r.get('client')
        new = people.resolve(it.get('new_owner') or '', org, func)
        if cid and func and new:
            store.add_fact('ownership', rid, d or r.get('_day') or day, canon_ts(r.get('at')), cid, {
                'client': cid, 'function': func, 'person': new,
                'prev': people.resolve(it.get('previous_owner') or '', org, func), 'author': r.get('author'),
                'tier': people.tier(r.get('author')), 'via': 'announcement', 'sentence': (r.get('body') or '')[:300]})
    found, left_p = promises.model_pass(llm, prom)
    for rid, got in found.items():
        r, d = rec_of(rid)
        if r is None:
            continue
        store.add_fact('promise', rid, d or r.get('_day') or day, canon_ts(r.get('at')), r.get('client'),
                       {'what': got['what'], 'due': got['due'], 'owner': r.get('author'), 'via': 'model'})

    keep = {'promises': [], 'owners': []}
    new_tries = {}
    for name, left in (('owners', left_o), ('promises', left_p)):
        for c in left:
            n = int(tries.get(c['id'], 0)) + 1
            if n < MAX_TRIES:
                keep[name].append(c)
                new_tries[c['id']] = n
    store.set_meta('pending_model', {'promises': keep['promises'], 'owners': keep['owners'], 'tries': new_tries})
    return {'promises': len(keep['promises']), 'owners': len(keep['owners'])}
