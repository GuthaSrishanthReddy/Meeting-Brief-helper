# -*- coding: utf-8 -*-
"""Feedback into memory: preferences, corrections, verdicts and forgets, all stored per person.

Feedback written on day N is read at `ingest --day N+1`. A forget line also arrives as its own
`forget` command after ingest, so applying it is idempotent (keyed by the normalised words).
Parsing is pattern-first; the model is asked only about lines the patterns cannot read.
"""
import datetime as dt
import hashlib
import json
import re

from .common import content_words, data_path, norm_text, parse_day, read_jsonl, words
from .llm import data_block

NAME = r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?'
MAXN = re.compile(r'\b(?:max(?:imum)?|at\s+most|no\s+more\s+than|up\s+to|only|top|limit(?:\s+it)?\s+to|'
                  r'keep\s+it\s+to|just)\s*(\d{1,2})\b', re.I)
MAXN2 = re.compile(r'\b(\d{1,2})\s+(?:changes?|items?|lines?|points?|bullets?|things?|updates?)\s+(?:max|at\s+most|tops)\b', re.I)
BLOCKERS_FIRST = re.compile(r'\bblock(?:er|ers|ed|ing)?\b.{0,40}\b(?:first|on\s+top|at\s+the\s+top|up\s+top)\b|'
                            r'\b(?:start|lead|open|begin)\s+with\s+(?:the\s+)?block', re.I)
ONLY_BLOCKERS = re.compile(r'\bonly\s+(?:show\s+|tell\s+me\s+|list\s+)?(?:the\s+)?block', re.I)
SKIP = re.compile(r'\b(?:skip|no|hide|drop|remove|exclude|without|leave\s+out|stop\s+(?:sending|showing|including))\b'
                  r'.{0,30}?\b(?P<what>newsletters?|deliver(?:y|ies|ed)|promises?|vendors?|notifications?)\b', re.I)
CORR_OWNER = re.compile(r'(?:^|\b[Nn]o\b[,.!]?\s*|\b[Aa]ctually\b,?\s*)(?P<new>' + NAME + r')\s+(?:now\s+|actually\s+)?'
                        r'(?:owns|handles|runs|manages|does|is\s+(?:now\s+)?(?:the\s+)?(?:owner|lead|in\s+charge|'
                        r'responsible)\s+(?:of|for))\s+(?:the\s+)?(?P<what>[^,.;!?]+?)(?:\s+now)?'
                        r'(?:\s*,?\s*not\s+(?P<old>' + NAME + r'))?\s*[.!]?\s*$')
CORR_DATE = re.compile(r'(?:^|\b[Nn]o\b[,.!]?\s*)(?:the\s+)?(?P<what>[A-Za-z][\w ]{1,40}?)\s+is\s+(?:now\s+)?'
                       r'(?:on|due(?:\s+on)?|moved\s+to)\s+(?P<new>[^,.;!?]+?)(?:\s*,?\s*not\s+(?P<old>[^,.;!?]+))?\s*[.!]?\s*$')
FORGET = re.compile(r"^\s*(?:please\s+)?(?:forget|drop|delete|remove|erase|ignore)\s+"
                    r"(?:what\s+i\s+(?:said|told\s+you|mentioned|wrote|shared|posted)\s+|everything\s+(?:i\s+(?:said|told\s+you)\s+)?|"
                    r"my\s+(?:note|notes|message|messages|chat)\s+)?(?:about|on|regarding|re:?)?\s*(?P<topic>.+?)\s*[.!]?\s*$", re.I)


CMD_WORDS = set('forget please skip drop remove delete erase ignore said say told note notes thing things '
                'stuff mentioned wrote'.split())


def _topic(text):
    """Topic words for matching, plus the distinctive (proper-noun) ones.

    A capital at the very start may just begin the sentence ("Weekly digest trends"), so it only
    counts when another capitalised word is present too ("Acme Print poster offers").
    """
    ws = [w for w in content_words(text) if w not in CMD_WORDS]
    caps = [(m.start(), m.group(1).lower()) for m in re.finditer(r'\b([A-Z][a-zA-Z0-9]+)', text or '')
            if m.group(1).lower() in ws]
    lead = len(text or '') - len((text or '').lstrip())
    inner = [w for pos, w in caps if pos > lead]
    distinct = []
    for pos, w in caps:
        if (pos > lead or inner) and w not in distinct:
            distinct.append(w)
    return ws, distinct


def topic_matches(entry, text):
    """Does `text` touch a stored topic (forget tombstone or suppress entry)?"""
    tw = set(words(text))
    if not tw:
        return False
    distinct = entry.get('distinct') or []
    ws = entry.get('words') or []
    if distinct and all(d in tw for d in distinct):
        return True
    if ws:
        hits = sum(1 for w in ws if w in tw)
        need = max(1, int(round(0.6 * len(ws) + 0.49)))
        return hits >= need and (not distinct or any(d in tw for d in distinct))
    return False


def parse_preference(text):
    out = {}
    m = MAXN.search(text) or MAXN2.search(text)
    if m:
        n = int(m.group(1))
        if n > 0:
            out['max_changed'] = n
    if BLOCKERS_FIRST.search(text):
        out['blockers_first'] = True
    if ONLY_BLOCKERS.search(text):
        out['only_blockers'] = True
    for m in SKIP.finditer(text):
        w = m.group('what').lower()
        if w.startswith('newsletter'):
            out['skip_newsletters'] = True
        elif w.startswith('deliver'):
            out['skip_deliveries'] = True
        elif w.startswith('promise'):
            out['skip_promises'] = True
        elif w.startswith('vendor'):
            out['skip_vendors'] = True
        elif w.startswith('notification'):
            out['skip_notifications'] = True
    if re.search(r'\bmost\s+important\s+first\b', text, re.I):
        out['order'] = 'importance'
    return out


def parse_forget(text):
    m = FORGET.match(text or '')
    topic = m.group('topic') if m else (text or '')
    ws, distinct = _topic(topic)
    return {'text': text, 'topic': topic, 'words': ws, 'distinct': distinct}


def remember_forget(store, person, text, day):
    entry = parse_forget(text)
    if not entry['words'] and not entry['distinct']:
        return False
    store.remember(person, 'forget', norm_text(text) or text, entry, day)
    store.out_clear()
    # Model results are prompt-hash keyed, so we cannot reliably identify every cache entry
    # influenced by this topic. Flush that small bounded cache to honor forget across later calls.
    store.llm_clear()
    return True


def _event_client(store, meeting):
    if not meeting:
        return None
    for r in store.versions('event', meeting):  # indexed by event id
        return r.get('client')
    return None


def _effective_at(store, day):
    """Feedback from day N (read at ingest N+1) counts from the start of day N+1."""
    base = parse_day(store.meta('day1'))
    if not base:
        return '%04d' % day
    d = base + dt.timedelta(days=day - 1)
    return d.isoformat() + 'T00:00:00Z'


def apply_line(store, ln, day, people, resolver, llm):
    person = ln.get('person')
    kind = (ln.get('type') or '').lower()
    text = ln.get('words') or ''
    if not person:
        return
    org = people.org_of(person)
    if kind == 'preference':
        prefs = parse_preference(text)
        if not prefs:
            prefs = model_preference(llm, text)
        for k, v in prefs.items():
            store.remember(person, 'pref', k, {'value': v, 'words': text}, day)
    elif kind == 'correction':
        client = _event_client(store, ln.get('meeting'))
        m = CORR_OWNER.search(text)
        if m:
            func = [w for w in content_words(m.group('what')) if w not in ('now',)]
            res = resolver.resolve(m.group('what')) if resolver else {'ids': []}
            if len(res['ids']) == 1:
                client = res['ids'][0]
                func = [w for w in func if w not in words(res['phrase'])]
            new = people.resolve(m.group('new'), org, func)
            old = people.resolve(m.group('old'), org, func) if m.group('old') else None
            if new and func:
                store.remember(person, 'correction', norm_text(text), {
                    'kind': 'owner', 'client': client, 'function': func, 'person': new, 'prev': old,
                    'at': _effective_at(store, day), 'via': 'correction', 'words': text}, day)
                return
        m = CORR_DATE.search(text)
        if m:
            store.remember(person, 'correction', norm_text(text), {
                'kind': 'date', 'client': client, 'subject': m.group('what').strip(), 'new_text': m.group('new').strip(),
                'old_text': (m.group('old') or '').strip(), 'at': _effective_at(store, day), 'via': 'correction',
                'words': text}, day)
            return
        got = model_correction(llm, text, client, people, org)
        if got:
            store.remember(person, 'correction', norm_text(text), dict(got, at=_effective_at(store, day),
                                                                       via='correction', words=text), day)
    elif kind == 'verdict':
        topic = ln.get('topic') or text
        ws, distinct = _topic(topic)
        verdict = (ln.get('verdict') or '').lower()
        store.remember(person, 'suppress' if verdict == 'ignored' else 'boost', norm_text(topic) or topic, {
            'topic': topic, 'words': ws, 'distinct': distinct, 'verdict': verdict, 'meeting': ln.get('meeting'),
            'client': _event_client(store, ln.get('meeting')), 'why': text}, day)
    elif kind == 'forget':
        remember_forget(store, person, text, day)


def apply_day(store, day, people, resolver, llm):
    """Read feedback/day-(N-1).jsonl at ingest of day N (never earlier)."""
    if day <= 1:
        return 0
    n = 0
    for ln in read_jsonl(data_path('feedback', 'day-%02d.jsonl' % (day - 1))):
        k = 'fb:' + hashlib.sha1(json.dumps(ln, sort_keys=True).encode('utf-8')).hexdigest()
        if store.seen(k):
            continue
        try:
            apply_line(store, ln, ln.get('day') or day - 1, people, resolver, llm)
        except Exception as e:  # noqa: BLE001 - one bad line must not stop ingest
            from .common import log
            log('feedback line skipped:', e)
        store.mark_seen(k)
        n += 1
    return n


# ------------------------------------------------------------------ model fallbacks
PREF_SCHEMA = {
    'type': 'object',
    'properties': {'max_changed': {'type': 'integer', 'description': '0 if not stated'},
                   'blockers_first': {'type': 'boolean'}, 'only_blockers': {'type': 'boolean'},
                   'skip': {'type': 'array', 'items': {'type': 'string',
                                                      'enum': ['newsletters', 'deliveries', 'promises', 'vendors']}}},
    'required': ['max_changed', 'blockers_first', 'only_blockers', 'skip'],
}
CORR_SCHEMA = {
    'type': 'object',
    'properties': {'kind': {'type': 'string', 'enum': ['owner', 'date', 'other']},
                   'function_or_subject': {'type': 'string'}, 'new_value': {'type': 'string'},
                   'old_value': {'type': 'string'}},
    'required': ['kind', 'function_or_subject', 'new_value', 'old_value'],
}


def model_preference(llm, text):
    if not llm or not llm.enabled:
        return {}
    res = llm.json_call('preference', 'How does this person want their meeting briefs? ' + data_block({'words': text}),
                        PREF_SCHEMA, max_output=120)
    out = {}
    if res:
        if isinstance(res.get('max_changed'), int) and res['max_changed'] > 0:
            out['max_changed'] = res['max_changed']
        if res.get('blockers_first'):
            out['blockers_first'] = True
        if res.get('only_blockers'):
            out['only_blockers'] = True
        for s in res.get('skip') or []:
            out['skip_' + s] = True
    return out


def model_correction(llm, text, client, people, org):
    if not llm or not llm.enabled:
        return None
    res = llm.json_call('correction', 'A person corrected a fact in their meeting brief. What is the corrected '
                        'fact? For an owner correction give the function and the new and old owner names. '
                        + data_block({'words': text}), CORR_SCHEMA, max_output=120)
    if not res:
        return None
    if res.get('kind') == 'owner':
        func = content_words(res.get('function_or_subject'))
        new = people.resolve(res.get('new_value') or '', org, func)
        if new and func:
            return {'kind': 'owner', 'client': client, 'function': func, 'person': new,
                    'prev': people.resolve(res.get('old_value') or '', org, func)}
    if res.get('kind') == 'date' and res.get('new_value'):
        return {'kind': 'date', 'client': client, 'subject': res.get('function_or_subject') or '',
                'new_text': res.get('new_value'), 'old_text': res.get('old_value') or ''}
    return None
