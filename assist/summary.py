# -*- coding: utf-8 -*-
"""Optional per-meeting summary for `brief --summary`.

The summary is written from one thing only: the lines the guard has already approved for that
meeting (`changed`, `promises`, `open_these`). The model never sees a record, a record id it was not
given, or anything the person cannot see, because nothing it receives comes from anywhere but a
finished, guard-checked brief.

What comes back must pass, or the summary is None and the normal brief is untouched:
  - valid JSON with a text and a list of source ids;
  - every id is one of the ids of the lines that were sent (and still citable);
  - the guard's checks: forgotten and suppressed topics, stale values, invented status words;
  - a lexical grounding check: no number and no capitalised name that the approved lines do not contain.
    This is a conservative heuristic, not a proof: it can reject a good paraphrase, which is the safe
    direction, and it cannot catch a wrong claim made purely of words the lines already contain.
"""
import re

from .common import fmt_dmy, parse_day, words
from .llm import data_block

MAX_CHANGED = 12   # the brief is already ranked, so these are its most important lines
MAX_PROMISES = 8
MAX_OPEN = 3
MAX_SUMMARY_CHARS = 600
MONTHS = set('january february march april may june july august september october november december '
             'jan feb mar apr jun jul aug sep sept oct nov dec'.split())

# Ordinary words that may begin a sentence of a summary without being facts of their own.
OPENERS = set('the a an this that these those there it its they their we our also and but however overall '
              'meanwhile next today tomorrow yesterday one two three first second third finally please several '
              'some all both most key main important note in on at for with as after before since while '
              'currently now still so then once only just here'.split())

SCHEMA = {
    'type': 'object',
    'properties': {'summary': {'type': 'string'}, 'source_ids': {'type': 'array', 'items': {'type': 'string'}}},
    'required': ['summary', 'source_ids'],
}


def approved_items(clean, people):
    """The only material the model may see: the approved lines of one finished brief, each with the
    ids of its sources."""
    items = []
    for c in (clean.get('changed') or [])[:MAX_CHANGED]:
        items.append({'kind': 'change', 'text': c['text'], 'sources': list(c['sources'])})
    for p in (clean.get('promises') or [])[:MAX_PROMISES]:
        due = parse_day(p.get('due'))
        text = '%s promised %s%s' % (people.name(p.get('owner')), p.get('what'),
                                     (' by %s' % fmt_dmy(due)) if due else '')
        items.append({'kind': 'promise', 'text': text, 'sources': list(p['sources'])})
    seen = set(i['text'] for i in items)
    for o in (clean.get('open_these') or [])[:MAX_OPEN]:
        if o.get('why') and o['why'] not in seen:  # usually the same line as a change: no need to repeat it
            items.append({'kind': 'open', 'text': o['why'], 'sources': [o['record']]})
    return items


def _numbers(text):
    return set(re.findall(r'\d+', text or ''))


def _capitalised(text):
    """[(word, starts_a_sentence)] for every capitalised word, without a trailing 's."""
    out = []
    for m in re.finditer(r"[A-Z][a-z]+(?:'s)?", text or ''):
        before = text[:m.start()].rstrip()
        w = m.group(0)
        out.append((w[:-2] if w.endswith("'s") else w, not before or before[-1] in '.!?:;\n"('))
    return out


def grounded(text, items):
    """True if every number and every capitalised word in `text` also occurs in the approved lines.

    Month names are always allowed (so "14 Nov" may be written "November 14"), and a word that merely
    starts a sentence may also be an ordinary opener such as "The" or "Overall". A name at the start of
    a sentence is NOT waved through: "Rahul Verma confirmed it" is rejected if Rahul is not in the lines.
    """
    corpus = ' '.join(i['text'] for i in items)
    known = set(words(corpus))
    if not _numbers(text) <= _numbers(corpus):
        return False
    for w, initial in _capitalised(text):
        lw = w.lower()
        if lw not in known and lw not in MONTHS and not (initial and lw in OPENERS):
            return False
    return True


def prompt_for(items):
    return ('Write a concise summary of this meeting brief: at most three short sentences in plain words. '
            'Use ONLY the items below. Do not add any name, date, number, status or reason that the items do '
            'not state, do not count the items, and do not repeat an old value without its new one. In '
            'source_ids list the ids of the records you relied on, using only ids that appear in the items. '
            'If the items do not support a summary, return an empty summary and no ids.\n'
            + data_block({'items': items}))


def summarize(llm, guard, clean, people):
    """-> {'text': str, 'sources': [ids]} or None. Never raises, never widens what the brief shows.

    None when there is nothing to summarise, the model is off / over its token or time budget /
    unreachable, it answers invalid JSON, or its answer fails any check above.
    """
    if clean.get('status') != 'brief' or not llm.enabled:
        return None
    items = approved_items(clean, people)
    if not items:
        return None
    allowed = set(s for i in items for s in i['sources'])
    try:
        res = llm.json_call('summary', prompt_for(items), SCHEMA, max_output=250)
    except Exception:  # noqa: BLE001 - the normal brief must survive anything the model side does
        return None
    if not isinstance(res, dict):
        return None
    got = guard.summary(res.get('summary'), res.get('source_ids'), allowed)
    if not got or len(got['text']) > MAX_SUMMARY_CHARS or not grounded(got['text'], items):
        return None
    return got
