# -*- coding: utf-8 -*-
"""The guard: every citation and every line is re-checked before the JSON prints.

- citation allow-list: records the person may see (own org, team / own private / listed reader),
  from a day already ingested and not after the question's moment, that are not notifications,
  newsletters or vendor mail, and that are not forgotten or suppressed for this person
- stale check: a line that holds a superseded value (an old owner, an old date) about the same
  subject must also hold the current value, or it is dropped
- tombstones and suppressions: a line touching something the person asked to forget, or a topic
  they marked as not relevant, is dropped
- no invented status: words like "AT RISK" or "approved by client" survive only if a cited record
  says so
"""
import re

from .common import dedupe, fmt_dm, parse_day, record_text, words  # noqa: F401 - record_text re-exported
from .feedback import topic_matches
from .store import CITABLE_KINDS

RISKY = ['at risk', 'approved by client', 'client approved', 'approved by the client', 'red flag', 'escalated',
         'on hold', 'churn']
NOTHING = 'I have nothing on that.'


def value_in(value, text):
    """Same matching the scorer uses: short values must stand as a whole word."""
    if not value or not text:
        return False
    if len(value) <= 3:
        return re.search(r'(?<![A-Za-z0-9])' + re.escape(value) + r'(?![A-Za-z0-9])', text, re.I) is not None
    return value.lower() in text.lower()


def date_forms(d):
    return [fmt_dm(d), d.isoformat(), '%s %d' % (fmt_dm(d).split()[1], d.day)]


def name_forms(people, pid):
    n = people.name(pid)
    out = [n] if n else []
    if n and ' ' in n:
        out.append(n.split()[0])
    return out


def subject_words(text):
    return set(w for w in words(text) if len(w) >= 4)


class Guard(object):
    def __init__(self, person, vis, cutoff_day, forgets, suppress, superseded):
        self.person = person
        self.vis = vis
        self.cutoff_day = int(cutoff_day or 0)
        self.forgets = forgets or []
        self.suppress = suppress or []
        self.superseded = superseded or []

    # ------------------------------------------------------------ records and citations
    def record_blocked(self, r):
        """A record this person must not be shown: something they asked to forget, or a topic
        they marked as not relevant."""
        text = record_text(r)
        if r.get('author') == self.person and any(topic_matches(f, text) for f in self.forgets):
            return True
        return any(topic_matches(s, text) for s in self.suppress)

    def citable(self, rid):
        r = self.vis.get(rid)
        if r is None or r.get('_kind') not in CITABLE_KINDS:
            return False
        if int(r.get('_day') or 0) > self.cutoff_day:
            return False
        return not self.record_blocked(r)

    def sources(self, ids):
        return [i for i in dedupe(ids or []) if self.citable(i)]

    # ------------------------------------------------------------ lines
    def is_stale(self, text):
        tw = set(words(text))
        for s in self.superseded:
            if not (s['subject'] & tw):
                continue
            if any(value_in(o, text) for o in s['old']) and not any(value_in(n, text) for n in s['new']):
                return True
        return False

    def line_ok(self, text, cited=()):
        if not text:
            return False
        if any(topic_matches(f, text) for f in self.forgets):
            return False
        if any(topic_matches(s, text) for s in self.suppress):
            return False
        low = text.lower()
        for phrase in RISKY:
            if phrase in low and not any(phrase in record_text(self.vis.get(i)).lower() for i in cited):
                return False
        return not self.is_stale(text)

    # ------------------------------------------------------------ whole outputs
    def brief(self, b):
        changed = []
        for c in b.get('changed') or []:
            src = self.sources(c.get('sources'))
            if src and self.line_ok(c.get('text'), src):
                changed.append({'text': c['text'], 'sources': src})
        out = {'meeting_id': b['meeting_id']}
        if not changed:
            out.update(status='nothing_new', changed=[], promises=[], open_these=[], question_to_ask=None)
            return out
        proms = []
        for p in b.get('promises') or []:
            src = self.sources(p.get('sources'))
            if src and self.line_ok(p.get('what'), src):
                proms.append({'owner': p['owner'], 'what': p['what'], 'due': p['due'], 'sources': src})
        cited = set(s for c in changed for s in c['sources'])
        opens = []
        for o in b.get('open_these') or []:
            if o.get('record') in cited and o['record'] not in [x['record'] for x in opens] and \
                    self.line_ok(o.get('why'), [o['record']]):
                opens.append({'record': o['record'], 'why': o['why']})
        q = b.get('question_to_ask')
        if isinstance(q, dict):
            qs = [s for s in q.get('sources') or [] if s in cited]
            q = {'text': q['text'], 'sources': qs} if qs and self.line_ok(q.get('text'), qs) else None
        else:
            q = None
        out.update(status='brief', changed=changed, promises=proms, open_these=opens[:3], question_to_ask=q)
        return out

    def summary(self, text, ids, allowed):
        """A model-written meeting summary through the same checks as every other line, or None.

        `allowed` is the set of source ids of the lines the model was shown for this meeting. Every cited
        id must be in it, and still citable (visible, a citable kind, not forgotten or suppressed). The
        text must have no forgotten or suppressed topic, no stale value without its current one, and no
        status word that no cited record says. Anything else is rejected whole, never trimmed to fit.
        """
        if not isinstance(text, str) or not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            return None
        text = ' '.join(text.split())
        ids = dedupe(ids)
        if not text or not ids or any(i not in allowed for i in ids):
            return None
        ok = self.sources(ids)
        if ok != ids or not self.line_ok(text, ok):
            return None
        return {'text': text, 'sources': ok}

    def answer(self, a):
        matched = []
        for m in a.get('matched') or []:
            if isinstance(m, dict) and m.get('id'):
                matched.append({'phrase': m.get('phrase') or '', 'id': m['id'], 'sources': self.sources(m.get('sources'))})
        ans = a.get('answer')
        if isinstance(ans, dict):
            src = self.sources(ans.get('sources'))
            text = ans.get('text') or ''
            if text != NOTHING and not self.line_ok(text, src):
                return {'answer': {'text': NOTHING, 'sources': []}, 'matched': [], 'clarify': None, 'refused': None}
            ans = {'text': text, 'sources': src}
        cl = a.get('clarify')
        if isinstance(cl, dict):
            opts = [o for o in cl.get('options') or [] if isinstance(o, dict) and self.line_ok(o.get('text') or o.get('id'))]
            cl = {'question': cl.get('question') or 'Which one do you mean?', 'options': opts} if opts else None
            if cl is None and ans is None:
                ans = {'text': NOTHING, 'sources': []}
        rf = a.get('refused')
        if rf:
            return {'answer': None, 'matched': [], 'clarify': None, 'refused': {'reason': 'private'}}
        return {'answer': ans, 'matched': matched, 'clarify': cl, 'refused': None}


def history_from_facts(own_facts, date_requests, doc_lines, func_match, find_dates):
    """(ownership history, date history) inputs for build_superseded, from visible facts."""
    own_hist = []
    for f in own_facts:
        prevs = [g['person'] for g in own_facts
                 if g.get('client') == f.get('client') and func_match(g.get('function'), f.get('function'))
                 and g['at'] <= f['at'] and g['person'] != f['person']]
        if f.get('prev'):
            prevs.append(f['prev'])
        if prevs:
            own_hist.append((' '.join(f.get('function') or []), prevs, f['person']))
    date_hist = []
    for rq in date_requests:
        new = parse_day(rq.get('new'))
        olds = []
        for dl in doc_lines:
            if dl['at'] < rq['at'] and dl.get('client') == rq.get('client') and \
                    set(words(dl.get('label'))) & set(words(rq.get('subject'))):
                olds.extend(d['date'] for d in find_dates(dl.get('value'), new)[:1])
        if olds and new:
            date_hist.append((rq.get('subject') or '', olds, new))
    return own_hist, date_hist


def build_superseded(people, task_states, own_facts=(), date_changes=()):
    """Old values and the current values that replace them, per subject, for the stale check."""
    out = []
    for st in task_states.values():
        subj = subject_words(st.get('title'))
        if not subj:
            continue
        owners_seen = [e['from'] for e in st['events'] if e['field'] == 'owner' and e.get('from')]
        cur = st.get('owner')
        olds = []
        for o in owners_seen:
            if o and o != cur:
                olds.extend(name_forms(people, o))
        if olds and cur:
            out.append({'subject': subj, 'old': dedupe(olds), 'new': name_forms(people, cur)})
        dues = [parse_day(e['from']) for e in st['events'] if e['field'] == 'due' and e.get('from')]
        curd = parse_day(st.get('due'))
        old_forms = []
        for d in dues:
            if d and curd and d != curd:
                old_forms.append(fmt_dm(d))
        if old_forms and curd:
            out.append({'subject': subj, 'old': dedupe(old_forms), 'new': date_forms(curd)})
    for subj_text, old_pids, cur_pid in own_facts:
        olds = []
        for o in old_pids:
            if o and o != cur_pid:
                olds.extend(name_forms(people, o))
        if olds and cur_pid:
            out.append({'subject': subject_words(subj_text) or set(words(subj_text)), 'old': dedupe(olds),
                        'new': name_forms(people, cur_pid)})
    for subj_text, old_dates, new_date in date_changes:
        olds = [fmt_dm(d) for d in old_dates if d and d != new_date]
        if olds and new_date:
            out.append({'subject': subject_words(subj_text) or set(words(subj_text)), 'old': dedupe(olds),
                        'new': date_forms(new_date)})
    return out
