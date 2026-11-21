# -*- coding: utf-8 -*-
"""Ownership facts, authority and precedence; leave facts with expiry.

Precedence for facts that disagree (plan section 9):
1. Forgotten (for that person) and expired facts never enter.
2. A doc line that did not change between versions keeps its first version's date (ingest only
   records a doc line when it is new or changed).
3. Questions are not claims ("X owns billing for the client, right?" asserts nothing).
4. Higher roles win on ownership: founder > managers/leads > staff > interns. The briefed or
   asking person's own correction counts as theirs (it wins for that person).
5. Between equals, the newer statement wins.
"""
import re

from .common import (ONE_DAY, TIER_STAFF, content_words, local_date, local_day_end_utc, parse_day,
                     parse_ts, parse_when, sentences, words)
from .llm import data_block

NAME = r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?'
_END = r'(?=\s*(?:[.;!?,]|$|\s+(?:from|starting|effective|as\s+of|with\s+effect|until|till)\b))'
ANNOUNCE = [
    re.compile(r'(?P<new>' + NAME + r')\s+(?:will\s+|is\s+going\s+to\s+)?(?:take|takes|is\s+taking|has\s+taken|took)\s+'
               r'over\s+(?:the\s+)?(?P<what>[^.;!?]+?)(?:\s+from\s+(?P<old>' + NAME + r'))?' + _END),
    re.compile(r'(?P<new>' + NAME + r')\s+(?:now\s+|will\s+now\s+|will\s+)?(?:owns|handles|runs|manages|covers|'
               r'looks\s+after|is\s+(?:now\s+)?(?:the\s+)?(?:owner|lead|point\s+of\s+contact|poc|contact)\s+'
               r'(?:of|for)|is\s+(?:now\s+)?(?:responsible\s+for|in\s+charge\s+of))\s+(?:the\s+)?(?P<what>[^.;!?]+?)'
               r'(?:\s+(?:now|going\s+forward|from\s+now\s+on|from\s+today|from\s+this\s+week|from\s+next\s+week))?'
               r'(?:\s*,?\s*not\s+(?P<old>' + NAME + r'))?' + _END),
    re.compile(r'(?:hand(?:ing|ed)?\s+over|moving|reassign(?:ing|ed)?|transferring)\s+(?:the\s+)?(?P<what>[^.;!?]+?)'
               r'\s+(?:from\s+(?P<old>' + NAME + r')\s+)?to\s+(?P<new>' + NAME + r')' + _END),
]
UNTIL = re.compile(r'\b(?:until|till|through)\s+(?P<d>[^.;!?,]+)', re.I)
DOC_OWNER_LABEL = re.compile(r'^(?P<func>.+?)\s+(?:contact|owner|lead|poc|point of contact|manager|handled by)$', re.I)
FUNC_STOP = set('work stuff things side part piece account accounts client clients project'.split())

LEAVE_SELF = re.compile(r"\b(?:i'?m|i\s+am|i\s+will\s+be|i'?ll\s+be)\s+(?:on\s+(?:leave|vacation|holiday|pto|"
                        r"sick\s+leave|annual\s+leave)|ooo|out\s+of\s+(?:the\s+)?office|off|away)\b", re.I)
LEAVE_OTHER = re.compile(r'\b(?P<name>' + NAME + r")\s+(?:is|will\s+be|'s)\s+(?:on\s+(?:leave|vacation|holiday|pto|"
                         r'sick\s+leave)|ooo|out\s+of\s+(?:the\s+)?office|off|away)\b')
L_FROM = re.compile(r'\bfrom\s+(?P<d>[^.;!?]+?)(?=\s+(?:till|until|to|through)\b|[.;!?,]|$)', re.I)
L_UNTIL = re.compile(r'\b(?:till|until|to|through)\s+(?:the\s+)?(?P<d>[^.;!?,]+)', re.I)
L_BACK = re.compile(r'\bback\s+(?:on|from|by)\s+(?P<d>[^.;!?,]+)', re.I)


def is_question(sentence):
    s = sentence.strip()
    return s.endswith('?') or re.search(r'\b(?:right|correct|isn\'t it|no)\s*\?', s, re.I) is not None


def function_words(text, client_resolver=None):
    """Words naming the function (e.g. 'billing') once client names are taken out."""
    drop = set()
    if client_resolver is not None:
        res = client_resolver.resolve(text)
        if res['ids'] and res['phrase']:
            drop.update(words(res['phrase']))
        for c in client_resolver.clients.items:
            drop.update(words(c.get('name')))
            drop.add(c['id'].lower())
    return [w for w in content_words(text) if w not in drop and w not in FUNC_STOP]


def statements(text, record_client, author, people, client_resolver, at, org):
    """Ownership announcements in a staff message -> [fact dicts]."""
    out = []
    for sent in sentences(text):
        if is_question(sent):
            continue
        for rx in ANNOUNCE:
            m = rx.search(sent)
            if not m:
                continue
            what = m.group('what')
            res = client_resolver.resolve(what) if client_resolver else {'ids': []}
            client = res['ids'][0] if len(res['ids']) == 1 else record_client
            func = function_words(what, client_resolver)
            if not func or not client:
                continue
            new = people.resolve(m.group('new'), org, func)
            if not new:
                continue
            old = people.resolve(m.group('old'), org, func) if m.group('old') else None
            fact = {'client': client, 'function': func, 'person': new, 'prev': old, 'author': author,
                    'tier': people.tier(author), 'via': 'announcement', 'sentence': sent[:300]}
            u = UNTIL.search(sent)
            if u:
                ts = parse_ts(at)
                ref = local_date(ts, people.zone(author)) if ts else None
                d = parse_when(u.group('d'), ref) if ref else None
                if d:
                    fact['until'] = d.isoformat()
                    fact['zone'] = people.zone(author)
            out.append(fact)
            break
    return out


def from_doc_line(label, value, people, org, author):
    """'Billing contact: <Full Name> (team)' -> ownership fact data (or None)."""
    m = DOC_OWNER_LABEL.match(label.strip())
    if not m:
        return None
    func = [w for w in content_words(m.group('func')) if w not in FUNC_STOP]
    nm = re.match(r'\s*(' + NAME + r')', value or '')
    if not func or not nm:
        return None
    pid = people.resolve(nm.group(1), org, func)
    if not pid:
        return None
    return {'function': func, 'person': pid, 'prev': None, 'author': author,
            'tier': people.tier(author) or TIER_STAFF, 'via': 'doc', 'sentence': '%s: %s' % (label, value)}


def leaves(text, author, people, at, org):
    """'I'm on leave from 9 Nov till the 12th. Back on 13 Nov.' -> [{'person','start','end','zone'}]."""
    ts = parse_ts(at)
    if not ts:
        return []
    out = []
    for sent in sentences(text):
        if is_question(sent):
            continue
        person = None
        if LEAVE_SELF.search(sent):
            person = author
        else:
            m = LEAVE_OTHER.search(sent)
            if m:
                person = people.resolve(m.group('name'), org)
        if not person:
            continue
        zone = people.zone(person)
        ref = local_date(ts, people.zone(author))
        start = end = None
        mf = L_FROM.search(sent)
        if mf:
            start = parse_when(mf.group('d'), ref)
        mu = L_UNTIL.search(sent)
        if mu:
            end = parse_when(mu.group('d'), start or ref)
        mb = L_BACK.search(text)
        if mb:
            back = parse_when(mb.group('d'), start or ref)
            if back and not end:
                end = back - ONE_DAY
        if not start:
            start = ref
        if end and end < start:
            end = None
        out.append({'person': person, 'start': start.isoformat(), 'end': end.isoformat() if end else None,
                    'zone': zone, 'sentence': sent[:300]})
    return out


def leave_active(fact, when):
    """Is the leave in force at UTC moment `when`? It ends at the end of `end` in the person's zone."""
    start = parse_day(fact.get('start'))
    end = parse_day(fact.get('end'))
    zone = fact.get('zone') or 'UTC'
    if start and local_date(when, zone) < start:
        return False
    if end and when >= local_day_end_utc(end, zone):
        return False
    return True


def expired(fact, when):
    u = parse_day(fact.get('until'))
    return bool(u and when >= local_day_end_utc(u, fact.get('zone') or 'UTC'))


def func_match(a, b):
    sa, sb = set(a or []), set(b or [])
    if not sa or not sb:
        return False
    canon = {'invoice': 'billing', 'invoices': 'billing', 'invoicing': 'billing', 'payments': 'billing',
             'payment': 'billing', 'bills': 'billing', 'bill': 'billing', 'designs': 'design',
             'developer': 'dev', 'development': 'dev', 'engineering': 'dev', 'testing': 'qa'}
    ca = set(canon.get(w, w) for w in sa)
    cb = set(canon.get(w, w) for w in sb)
    return bool(ca & cb)


def resolve(facts, client, func, when, corrections=None):
    """Winning ownership fact for (client, function) at UTC moment `when`, or None.

    facts: visible ownership facts. corrections: the person's own corrections (win for them).
    """
    cands = []
    for f in facts:
        if client and f.get('client') and f['client'] != client:
            continue
        if not func_match(f.get('function'), func):
            continue
        if expired(f, when):
            continue
        cands.append((int(f.get('tier') or 0), f.get('at') or '', f))
    for c in corrections or []:
        if client and c.get('client') and c['client'] != client:
            continue
        if func_match(c.get('function'), func):
            cands.append((99, c.get('at') or '', c))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]))
    return cands[-1][2]


# ------------------------------------------------------------------ model fallback (ingest)
SCHEMA = {
    'type': 'object',
    'properties': {'items': {'type': 'array', 'items': {
        'type': 'object',
        'properties': {'id': {'type': 'string'}, 'is_assignment': {'type': 'boolean'},
                       'client': {'type': 'string'}, 'function': {'type': 'string'},
                       'new_owner': {'type': 'string'}, 'previous_owner': {'type': 'string'}},
        'required': ['id', 'is_assignment', 'client', 'function', 'new_owner', 'previous_owner']}}},
    'required': ['items'],
}
OWNER_HINT = re.compile(r'\b(?:take|takes|taking|took)\s+over\b|\bown(?:s|er|ership)?\b|\bhandl(?:e|es|ing)\b|'
                        r'\bresponsible\b|\bin\s+charge\b|\bpoint\s+of\s+contact\b|\bhand(?:ing)?\s*over\b|'
                        r'\breassign', re.I)


def model_pass(llm, candidates, client_ids):
    """Senior-role messages that hint at an ownership change but did not parse.

    Returns ({id: assignment}, left): the assignments found, and the candidates the model could not
    answer for (time budget used up, provider down), so the caller can retry them later.
    """
    if not llm or not llm.enabled or not candidates:
        return {}, []
    out, left = {}, []
    for i in range(0, len(candidates), 15):
        batch = candidates[i:i + 15]
        prompt = ('Some workplace messages may announce that a person now owns a function for a client (for '
                  'example billing). Questions are not announcements. For each message return is_assignment, '
                  'the client id (one of %s, or empty), the function in one or two words, and the new and '
                  'previous owner names exactly as written.\n%s' % (', '.join(client_ids), data_block(batch)))
        res = llm.json_call('ownership', prompt, SCHEMA, max_output=600)
        if res is None:
            left.extend(batch)
            continue
        for it in res.get('items') or []:
            if isinstance(it, dict) and it.get('is_assignment') and it.get('id'):
                out[it['id']] = it
    return out, left
