# -*- coding: utf-8 -*-
"""ask --person P --at ISO --ask "...": answer as of that UTC moment.

Exactly one of answer / clarify / refused is filled, and matched is filled every time.
Question shapes, code first (in this order):
  which person    "Which <first name> is that?"    role and client context, else ask back
  privacy         "What did <person> say about me?" refuse when the only evidence is private to others
  leave           "Is <person> on leave on <date>?" leave facts end at the end of the last day
  task field      owner / due / status of a task, matched within the resolved client
  relative day    "Is the Friday thing done?"      the asker's tasks due that day; ask back if 2+
  function owner  "Who handles billing for <client>?" ownership facts with precedence
  own notes       "When is my <trip>?"             the asker's own records, private ones included
  promises        "What do we still owe <client>?"
  model           unusual phrasing: the model maps the question onto one of the shapes above
                  (ids only); the answer itself still comes from the records.
"""
import datetime as dt
import re

from . import owners, promises, retrieve
from .clients import ClientResolver, match_tasks, pick_task, task_states
from .common import (Clients, WEEKDAY_NUM, canon_ts, content_words, dedupe, find_dates, fmt_dm, fmt_dmy,
                     local_date, local_day_end_utc, log, parse_day, parse_ts, sentences, words)
from .guard import NOTHING, Guard, build_superseded, history_from_facts, record_text
from .llm import LLM, data_block

STATUS_WORDS = {'todo': 'to do (not started yet)', 'in_progress': 'in progress', 'blocked': 'blocked',
                'done': 'done', 'cancelled': 'cancelled'}
RX_OWNER = re.compile(r"\b(?:who(?:'s|\s+is|\s+will)?\s+(?:owns?|owning|handles?|handling|working\s+on|on|"
                      r'responsible|in\s+charge|doing|looking\s+after|manag\w*|runs?|running|the\s+owner|taking)|'
                      r'owner\s+of|whose\b|assigned\s+to|who\s+has)', re.I)
RX_DUE = re.compile(r'\b(?:due|deadline|by\s+when|due\s+date|eta)\b', re.I)
RX_STATUS = re.compile(r'\b(?:status|done|finished|complete[d]?|blocked|progress|started|ready|cancel+ed|stuck)\b', re.I)
RX_WHICH = re.compile(r'\b[Ww]hich\s+([A-Z][a-z]+)\b')
RX_SAY = re.compile(r'\b(?:say|said|says|saying|think|thinks|thought|feel|feels|wrote|write|written|mention\w*|'
                    r'opinion|complain\w*)\b[^?]*?\babout\s+(?P<obj>[^?.,]+)', re.I)
RX_LEAVE = re.compile(r'\b(?:on\s+leave|leave|ooo|out\s+of\s+(?:the\s+)?office|vacation|holiday|available|'
                      r'availability|around|back|in\s+office|away|off)\b', re.I)
RX_PROMISE = re.compile(r'\b(?:promis\w*|commit\w*|owe[sd]?|pending\s+from|still\s+(?:owe|pending)|open\s+items)\b', re.I)
RX_DAYWORD = re.compile(r"\b(today|tonight|tomorrow|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|"
                        r"sunday)(?:'s)?\b", re.I)
TRAVEL = set('travel travels travelling traveling trip trips vacation holiday holidays flight flights fly flying '
             'visit visiting abroad tour journey'.split())
LEAVE = set('leave ooo vacation holiday holidays sick away unavailable off'.split())
INTENT_WORDS = set('who whos owns own owner owning handles handle handling responsible charge manages manage '
                   'managing runs run running looks look looking after does do doing currently now point contact '
                   'poc lead leads take takes taking care client account customer team brand'.split())


# ------------------------------------------------------------------ output builders
def ans(text, sources, matched):
    return {'answer': {'text': text, 'sources': dedupe(sources)}, 'matched': matched, 'clarify': None, 'refused': None}


def clarify(question, options, matched):
    return {'answer': None, 'matched': matched, 'clarify': {'question': question, 'options': options}, 'refused': None}


def refuse():
    return {'answer': None, 'matched': [], 'clarify': None, 'refused': {'reason': 'private'}}


def nothing(matched=None):
    return {'answer': {'text': NOTHING, 'sources': []}, 'matched': matched or [], 'clarify': None, 'refused': None}


# ------------------------------------------------------------------ context
class Ctx(object):
    def __init__(self, store, person, at_iso, question):
        self.store = store
        self.person = person
        self.q = (question or '').strip()
        self.people = store.people
        self.clients = Clients()
        self.org = self.people.org_of(person)
        self.at_iso = canon_ts(at_iso) if parse_ts(at_iso) else None
        self.at = parse_ts(at_iso) or dt.datetime.now(dt.timezone.utc)
        self.last = store.last_day()
        self.vis = store.visible(person, self.last, self.at_iso)
        self.zone = self.people.zone(person)
        self.today = local_date(self.at, self.zone)
        self.llm = LLM(store, 'ask', budget=4000)
        self.resolver = ClientResolver(self.clients, self.people, self.vis, self.llm,
                                       abbrevs=store.facts(['abbrev'], self.vis))
        self.states = task_states(self.vis)
        self.forgets = store.memories(person, 'forget')
        self.suppress = store.memories(person, 'suppress')
        self.corrections = [c for c in store.memories(person, 'correction') if c.get('kind') == 'owner']
        self.own_facts = store.facts(['ownership'], self.vis)
        own_hist, date_hist = history_from_facts(self.own_facts, store.facts(['date_request'], self.vis),
                                                 store.facts(['doc_line'], self.vis), owners.func_match, find_dates)
        self.guard = Guard(person, self.vis, self.last, self.forgets, self.suppress,
                           build_superseded(self.people, self.states, own_hist, date_hist))
        self.client = self.resolver.resolve(self.q)
        if not self.client['ids']:
            cid = self.resolver.resolve_with_model(self.q)
            if cid:
                phrase = self.resolver.nickname_phrases(self.q)[0][0]
                self.client = {'ids': [cid], 'phrase': phrase, 'method': 'model'}

    def one_client(self):
        ids = self.client['ids']
        return ids[0] if len(ids) == 1 else None

    def client_matched(self):
        cid = self.one_client()
        return [{'phrase': self.client['phrase'], 'id': cid, 'sources': []}] if cid else []

    def blocked(self, r):
        return self.guard.record_blocked(r)


def detect_field(q):
    if RX_OWNER.search(q):
        return 'owner'
    if RX_DUE.search(q):
        return 'due'
    if RX_STATUS.search(q):
        return 'status'
    if re.search(r'\bwhen\b', q, re.I):
        return 'due'
    return None


def phrase_for(q, title):
    m = re.search(re.escape(title), q, re.I)
    return m.group(0) if m else title


# ------------------------------------------------------------------ flows
def task_answer(ctx, st, field):
    title, tid = st.get('title') or st['task_id'], st['task_id']
    src = st['src'].get(field) or st['src'].get('status')
    matched = [{'phrase': phrase_for(ctx.q, title), 'id': tid, 'sources': [src]}] + ctx.client_matched()
    note = ''
    if field in ('owner', 'due') and st.get('status') in ('done', 'cancelled'):
        evs = [e for e in st['events'] if e['field'] == 'status']
        when = local_date(parse_ts(evs[-1]['at']), ctx.zone) if evs and parse_ts(evs[-1]['at']) else None
        note = ' It is %s%s.' % (st['status'], ' (since %s)' % fmt_dm(when) if when else '')
    if field == 'owner':
        pid = st.get('owner')
        if not pid:
            return ans('%s has no owner on record.%s' % (title, note), [src], matched)
        matched.append({'phrase': ctx.people.name(pid), 'id': pid, 'sources': [src]})
        return ans('%s (%s) owns %s.%s' % (ctx.people.name(pid), pid, title, note), [src], matched)
    if field == 'due':
        d = parse_day(st.get('due'))
        if not d:
            return ans('%s has no due date on record.%s' % (title, note), [src], matched)
        return ans('%s is due %s (%s).%s' % (title, fmt_dmy(d), d.isoformat(), note), [src], matched)
    status = st.get('status')
    extra = ''
    if status == 'blocked':
        evs = [e for e in st['events'] if e['field'] == 'status']
        body = (evs[-1]['body'] if evs else '').strip().rstrip('.')
        m = re.match(r'^\s*blocked\s*[:\-–]\s*(.+)$', body, re.I)
        if m:
            extra = ' (%s)' % m.group(1)
    return ans('%s is %s%s.' % (title, STATUS_WORDS.get(status, status or 'unknown'), extra), [src], matched)


def task_flow(ctx, field):
    ids = ctx.client['ids'] or None
    kind, res = pick_task(match_tasks(ctx.q, ctx.states, ctx.clients, ids))
    if kind == 'one':
        return task_answer(ctx, res, field or 'status')
    if kind == 'many':
        opts = [{'id': st['task_id'], 'text': st['title']} for st in res[:6]]
        return clarify('Which one do you mean: %s?' % ' or '.join(o['text'] for o in opts), opts, ctx.client_matched())
    return None


def target_day(word, today):
    w = word.lower()
    if w in ('today', 'tonight'):
        return today
    if w == 'tomorrow':
        return today + dt.timedelta(days=1)
    if w == 'yesterday':
        return today - dt.timedelta(days=1)
    wd = WEEKDAY_NUM.get(w)
    if wd is None:
        return None
    ahead = (wd - today.weekday()) % 7
    if ahead > 4:
        ahead -= 7
    return today + dt.timedelta(days=ahead)


def day_flow(ctx, field):
    m = RX_DAYWORD.search(ctx.q)
    if not m:
        return None
    day = target_day(m.group(1), ctx.today)
    if not day:
        return None
    ids = ctx.client['ids']
    pool = [st for st in ctx.states.values() if st.get('due') == day.isoformat() and (not ids or st.get('client') in ids)]
    mine = [st for st in pool if st.get('owner') == ctx.person]
    cands = mine or pool
    if len(cands) == 1:
        return task_answer(ctx, cands[0], field or 'status')
    if len(cands) > 1:
        opts = [{'id': st['task_id'], 'text': st['title']} for st in cands[:6]]
        return clarify('Which %s (%s) item do you mean?' % (day.strftime('%A'), fmt_dm(day)), opts, ctx.client_matched())
    return None


DATE_STOP = set('when date dates due day the what whats which start starts starting begin begins happen '
                'happening planned scheduled new now set going'.split())


def date_flow(ctx):
    """Dates that are not task due dates: client date requests, dated doc lines, dated decisions.

    A client's own request (or a decision agreed in a client meeting) is authoritative for their
    dates; otherwise the newest statement wins. Unchanged doc lines keep their first date.
    """
    drop = set(DATE_STOP) | INTENT_WORDS
    if ctx.client['phrase']:
        drop.update(words(ctx.client['phrase']))
    for c in ctx.clients.items:
        drop.update(words(c.get('name')))
        drop.add(c['id'].lower())
    qw = set(w for w in content_words(ctx.q) if w not in drop)
    if not qw:
        return None
    ids = ctx.client['ids']
    cands = []  # (authority, at, date, rid, label, kind)
    for kind, key, auth in (('date_request', 'subject', 2), ('decision', 'text', 2), ('doc_line', 'label', 1)):
        for f in ctx.store.facts([kind], ctx.vis):
            if ids and f.get('client') not in ids:
                continue
            if not set(words(f.get(key))) & qw or ctx.blocked(ctx.vis[f['rid']]):
                continue
            ref = parse_day((f.get('at') or '')[:10]) or ctx.today
            if kind == 'date_request':
                d = parse_day(f.get('new'))
            else:
                ds = find_dates(f.get('text') if kind == 'decision' else f.get('value'), ref)
                d = ds[0]['date'] if ds else None
            if d:
                cands.append((auth if kind != 'date_request' or f.get('external') else 1, f['at'], d, f['rid'],
                              f.get(key), kind))
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1]))
    auth, at, d, rid, label, kind = cands[-1]
    olds = [c[2] for c in sorted(cands, key=lambda c: c[1]) if c[2] != d and c[1] <= at]
    srcs = dedupe([rid] + [c[3] for c in cands if c[2] == d])[:3]
    when = local_date(parse_ts(at), ctx.zone) if parse_ts(at) else None
    if kind == 'decision':
        text = 'Decision on %s: "%s" (%s, %s).' % (fmt_dm(when) if when else '?', label, fmt_dmy(d), d.isoformat())
    else:
        moved = ', moved from %s' % fmt_dm(olds[-1]) if olds else ''
        text = '%s: %s (%s)%s.' % (label[:1].upper() + label[1:], fmt_dmy(d), d.isoformat(), moved)
    return ans(text, srcs, ctx.client_matched())


def supporting(ctx, cid, func, pid):
    out = []
    for f in reversed(ctx.own_facts):
        if f.get('person') == pid and (not cid or f.get('client') == cid) and owners.func_match(f.get('function'), func):
            out.append(f['rid'])
    return out[:2]


def function_flow(ctx):
    drop = set(INTENT_WORDS)
    if ctx.client['phrase']:
        drop.update(words(ctx.client['phrase']))
    for c in ctx.clients.items:
        drop.update(words(c.get('name')))
        drop.add(c['id'].lower())
    func = [w for w in content_words(ctx.q) if w not in drop]
    if not func:
        return None
    cids = ctx.client['ids'] or [c['id'] for c in ctx.clients.items]
    winners = {}
    for cid in cids:
        f = owners.resolve(ctx.own_facts, cid, func, ctx.at, ctx.corrections)
        if f:
            winners[cid] = f
    if not winners:
        return None
    if len(ctx.client['ids']) != 1 and len(set(f['person'] for f in winners.values())) > 1:
        opts = [{'id': cid, 'text': ctx.clients.name(cid)} for cid in winners]
        return clarify('Which client do you mean?', opts, [])
    cid = ctx.one_client() or list(winners)[0]
    f = winners.get(cid)
    if not f:
        return None
    pid = f['person']
    fn = ' '.join(f.get('function') or func)
    sources = [f['rid']] if f.get('rid') else supporting(ctx, cid, f.get('function') or func, pid)
    verb = 'handles' if re.search(r'\bhandl', ctx.q, re.I) else 'owns'
    matched = ctx.client_matched() or [{'phrase': ctx.clients.name(cid), 'id': cid, 'sources': []}]
    matched.append({'phrase': ctx.people.name(pid), 'id': pid, 'sources': sources})
    return ans('%s (%s) %s %s for %s.' % (ctx.people.name(pid), pid, verb, fn, ctx.clients.name(cid)), sources, matched)


def which_flow(ctx, first):
    cands = ctx.people.candidates(first, ctx.org)
    if len(cands) < 2:
        return None
    cid = ctx.one_client()
    qwords = content_words(ctx.q)
    scores, reasons, srcs = {}, {}, {}
    for pid in cands:
        s = 0.0
        for f in ctx.own_facts:
            if f.get('person') == pid and (cid is None or f.get('client') == cid):
                s += 3
                reasons.setdefault(pid, 'who owns %s for %s' % (' '.join(f.get('function') or []),
                                                               ctx.clients.name(f.get('client'))))
                srcs.setdefault(pid, []).append(f['rid'])
        for st in ctx.states.values():
            if st.get('owner') == pid and (cid is None or st.get('client') == cid):
                s += 1
        n = sum(1 for m in ctx.vis.light(kinds=('staff',), author=pid) if cid is None or m[7] == cid)
        s += min(n, 4) * 0.25
        s += ctx.people.role_relevance(pid, qwords)
        scores[pid] = s
    ranked = sorted(cands, key=lambda p: (-scores[p], p))
    if scores[ranked[0]] > scores[ranked[1]]:
        pid = ranked[0]
        sources = dedupe(srcs.get(pid) or [])[:2]
        others = ', '.join('%s (%s)' % (ctx.people.name(o), o) for o in ranked[1:])
        text = 'That is %s (%s), %s%s; not %s.' % (ctx.people.name(pid), pid, ctx.people.role(pid),
                                                   ', ' + reasons[pid] if pid in reasons else '', others)
        return ans(text, sources, [{'phrase': first, 'id': pid, 'sources': sources}] + ctx.client_matched())
    opts = [{'id': p, 'text': '%s (%s)' % (ctx.people.name(p), ctx.people.role(p))} for p in cands]
    return clarify('Which %s do you mean?' % first, opts, ctx.client_matched())


def _target(ctx, name, ids):
    if len(ids) == 1:
        return ids[0]
    return ctx.people.resolve(name, ctx.org, content_words(ctx.q))


def _topic(ctx, say_match):
    """-> (topic words, how many must appear in a record for it to count as evidence)."""
    if say_match:
        obj = say_match.group('obj').strip().lower()
        if re.match(r'^(?:me|myself|my\b)', obj):
            return set(words(ctx.people.name(ctx.person))), 1
        ws = set(content_words(obj))
        return ws, max(1, int(round(0.6 * len(ws) + 0.49)))
    qw = set(words(ctx.q))
    if qw & TRAVEL:
        return set(TRAVEL), 1
    if qw & LEAVE:
        return set(LEAVE), 1
    return set(), 1


def best_sentence(text, topic):
    best, score = '', -1
    for s in sentences(text):
        sc = len(set(words(s)) & set(topic))
        if sc > score:
            best, score = s, sc
    return best


STATEMENT_TYPES = ('chat', 'mail', 'note', 'doc')


def privacy_flow(ctx, targets, say_match):
    """Someone else's statements or personal plans. Visible evidence is answered with its source;
    evidence that exists only in records the asker may not see gets a bare refusal."""
    topic, need = _topic(ctx, say_match)
    if not topic:
        return None
    for name, ids in targets:
        pid = _target(ctx, name, ids)
        if not pid or pid == ctx.person:
            continue
        first = ctx.people.first(pid).lower()

        def score(r):
            if r.get('type') not in STATEMENT_TYPES:
                return 0
            tw = set(words(record_text(r)))
            if r.get('author') != pid and first not in tw:
                return 0
            hits = len(tw & topic)
            return hits if hits >= need else 0

        best, best_score = None, 0
        for r in ctx.vis.of_type(*STATEMENT_TYPES, kinds=('staff',)):
            if ctx.blocked(r):
                continue
            s = score(r)
            if s and s >= best_score:
                best, best_score = r, s
        if best:
            when = parse_ts(best['at'])
            sent = best_sentence(record_text(best), topic)
            text = 'On %s, %s wrote: "%s"' % (fmt_dm(local_date(when, ctx.zone)), ctx.people.name(best.get('author')), sent)
            return ans(text, [best['id']], [{'phrase': name, 'id': pid, 'sources': [best['id']]}])
        if any(score(r) for r in ctx.store.hidden_from(ctx.person, ctx.last, ctx.at_iso)):
            return refuse()  # the evidence exists but is not the asker's to see: say nothing of it
        return nothing([{'phrase': name, 'id': pid, 'sources': []}])
    return None


def leave_flow(ctx, targets):
    facts = ctx.store.facts(['leave'], ctx.vis)
    for name, ids in targets:
        pid = _target(ctx, name, ids)
        mine = [f for f in facts if f.get('person') == pid and not ctx.blocked(ctx.vis[f['rid']])]
        if not pid or not mine:
            continue
        f = mine[-1]
        start, end = parse_day(f.get('start')), parse_day(f.get('end'))
        span = ('from %s to %s' % (fmt_dm(start), fmt_dm(end))) if end else ('from %s' % fmt_dm(start))
        back = (' (back on %s)' % fmt_dm(end + dt.timedelta(days=1))) if end else ''
        who = '%s (%s)' % (ctx.people.name(pid), pid)
        ds = find_dates(ctx.q, ctx.today)
        if ds:
            d = ds[0]['date']
            on = (not start or start <= d) and (not end or d <= end)
            text = '%s is %son leave on %s: the leave runs %s%s.' % (who, '' if on else 'not ', fmt_dm(d), span, back)
        elif owners.leave_active(f, ctx.at):
            text = '%s is on leave %s%s.' % (who, span, back)
        elif end and ctx.at >= local_day_end_utc(end, f.get('zone') or ctx.zone):
            text = '%s is not on leave any more: the leave %s has ended%s.' % (who, span, back)
        else:
            text = '%s is not on leave now; leave is planned %s%s.' % (who, span, back)
        return ans(text, [f['rid']], [{'phrase': name, 'id': pid, 'sources': [f['rid']]}])
    return None


def own_flow(ctx):
    m = re.search(r'\bmy\s+([^?]+)', ctx.q, re.I)
    phrase = (m.group(1) if m else ctx.q).strip().rstrip('?.! ')
    topic = [w for w in content_words(phrase) if w not in ('due', 'date', 'when', 'status', 'done')]
    if not topic:
        return None
    best, best_score = None, 0
    for r in ctx.vis.of_type('chat', 'mail', 'note', 'doc', kinds=('staff',), author=ctx.person):
        if ctx.blocked(r):
            continue
        tw = set(words(record_text(r)))
        score = sum(1 for t in topic if t in tw)
        if score > best_score or (score and score == best_score and r['at'] > best['at']):
            best, best_score = r, score
    if not best or best_score < max(1, (len(topic) + 1) // 2):
        return None
    when = local_date(parse_ts(best['at']), ctx.zone)
    sent = best_sentence(record_text(best), topic)
    lead = ''
    ds = find_dates(sent, when)
    if ds:
        d = ds[0]
        if d['end'] != d['date'] and d['end'].month == d['date'].month:
            span = '%d to %s' % (d['date'].day, fmt_dm(d['end']))
        elif d['end'] != d['date']:
            span = '%s to %s' % (fmt_dm(d['date']), fmt_dm(d['end']))
        else:
            span = fmt_dm(d['date'])
        lead = 'Your %s is planned for %s. ' % (phrase, span)
    return ans('%sFrom your note on %s: "%s"' % (lead, fmt_dm(when), sent), [best['id']], [])


def promise_flow(ctx, targets):
    led = promises.ledger(ctx.store.facts(['promise'], ctx.vis), ctx.store.facts(['delivery'], ctx.vis), ctx.states)
    open_ = [p for p in led if not p.get('closed_by') and not ctx.blocked(ctx.vis[p['rid']])]
    ids = ctx.client['ids']
    if ids:
        open_ = [p for p in open_ if p.get('client') in ids]
    named = [(n, _target(ctx, n, i)) for n, i in targets]
    named = [(n, w) for n, w in named if w]
    who = [w for _, w in named]
    if not who and re.search(r'\b(?:i|my|me)\b', ctx.q, re.I):
        who = [ctx.person]
    if who:
        open_ = [p for p in open_ if p.get('owner') in who]
    matched = ctx.client_matched() + [{'phrase': n, 'id': w, 'sources': []} for n, w in named]
    if not open_:
        return ans('There are no open promises on record for that.', [], matched)
    parts = []
    for p in sorted(open_, key=lambda x: (x.get('due') or '', x['at']))[:8]:
        d = parse_day(p.get('due'))
        parts.append('%s (%s): %s, due %s (%s)' % (ctx.people.name(p.get('owner')), p.get('owner'), p.get('what'),
                                                   fmt_dmy(d) if d else '?', p.get('due')))
    return ans('Open promises: ' + '; '.join(parts) + '.', [p['rid'] for p in open_[:8]], matched)


INTENT_SCHEMA = {
    'type': 'object',
    'properties': {
        'intent': {'type': 'string', 'enum': ['task_owner', 'task_due', 'task_status', 'function_owner',
                                              'own_note', 'promises', 'unknown']},
        'task_id': {'type': 'string'}, 'client_id': {'type': 'string'}, 'function': {'type': 'string'}},
    'required': ['intent', 'task_id', 'client_id', 'function'],
}


def model_flow(ctx):
    """Unusual phrasing: the model only maps the question to a shape and ids."""
    if not ctx.llm.enabled:
        return None
    tasks = [{'id': st['task_id'], 'title': st['title'], 'client': st.get('client')} for st in ctx.states.values()][:80]
    cl = [{'id': c['id'], 'name': c.get('name')} for c in ctx.clients.items]
    prompt = ('Map the question to one intent and the ids it refers to. Use only ids from the lists; leave a '
              'field empty if it does not apply.\n' + data_block({'question': ctx.q, 'tasks': tasks, 'clients': cl}))
    res = ctx.llm.json_call('ask-intent', prompt, INTENT_SCHEMA, max_output=120) or {}
    intent = res.get('intent')
    st = ctx.states.get(res.get('task_id') or '')
    if res.get('client_id') in ctx.clients.by_id and not ctx.client['ids']:
        ctx.client = {'ids': [res['client_id']], 'phrase': ctx.clients.name(res['client_id']), 'method': 'model'}
    if st and intent in ('task_owner', 'task_due', 'task_status'):
        return task_answer(ctx, st, {'task_owner': 'owner', 'task_due': 'due', 'task_status': 'status'}[intent])
    if intent == 'function_owner':
        return function_flow(ctx)
    if intent == 'own_note':
        return own_flow(ctx)
    if intent == 'promises':
        return promise_flow(ctx, [])
    return None


def search_flow(ctx):
    """Last resort for a vague question: retrieve the few best records (gated, citable) and answer from
    them, or say nothing. See retrieve.py."""
    got = retrieve.answer(ctx)
    if not got:
        return None
    text, sources, _ = got
    return ans(text, sources, ctx.client_matched())


# ------------------------------------------------------------------ dispatcher
def answer(ctx):
    q = ctx.q
    if not q:
        return nothing()
    targets = [(n, ids) for n, ids in ctx.people.mentions(q, ctx.org) if ids != [ctx.person]]
    m = RX_WHICH.search(q)
    if m:
        r = which_flow(ctx, m.group(1))
        if r:
            return r
    field = detect_field(q)
    say = RX_SAY.search(q)
    qw = set(words(q))
    if targets and (say or qw & TRAVEL or qw & LEAVE or RX_LEAVE.search(q)):
        unclear = [(n, ids) for n, ids in targets if len(ids) > 1 and not _target(ctx, n, ids)]
        if unclear and len(unclear) == len(targets):  # two people share the name: ask, don't guess
            n, ids = unclear[0]
            opts = [{'id': p, 'text': '%s (%s)' % (ctx.people.name(p), ctx.people.role(p))} for p in ids]
            return clarify('Which %s do you mean?' % n, opts, ctx.client_matched())
    if targets and (say or qw & TRAVEL or qw & LEAVE):
        if not say and qw & LEAVE:
            r = leave_flow(ctx, targets)
            if r:
                return r
        r = privacy_flow(ctx, targets, say)
        if r:
            return r
    if targets and RX_LEAVE.search(q):
        r = leave_flow(ctx, targets)
        if r:
            return r
    for flow in (lambda: task_flow(ctx, field), lambda: day_flow(ctx, field)):
        r = flow()
        if r:
            return r
    if field == 'due' or re.search(r'\bdate\b', q, re.I):
        r = date_flow(ctx)
        if r:
            return r
    if field == 'owner' or re.search(r'\b(?:handles?|in\s+charge|responsible)\b', q, re.I):
        r = function_flow(ctx)
        if r:
            return r
    if re.search(r'\bmy\b', q, re.I):
        r = own_flow(ctx)
        if r:
            return r
    if RX_PROMISE.search(q):
        r = promise_flow(ctx, targets)
        if r:
            return r
    r = model_flow(ctx)
    if r:
        return r
    r = search_flow(ctx)
    if r:
        return r
    if not ctx.client['ids'] and ctx.resolver.nickname_phrases(q) and field:
        opts = [{'id': c['id'], 'text': c.get('name') or c['id']} for c in ctx.clients.items]
        return clarify('Which client do you mean?', opts, [])
    return nothing(ctx.client_matched())


def run(store, person, at_iso, question):
    if not parse_ts(at_iso):
        # No moment for the question means no honest "nothing later than the question" cutoff, and the
        # whole time cutoff would silently switch off. Answer nothing, and read nothing.
        log('ask: %r is not a time the question can be placed at; answering nothing' % (at_iso,))
        return nothing(), LLM(store, 'ask', budget=4000)
    ctx = Ctx(store, person, at_iso, question)
    out = ctx.guard.answer(answer(ctx))
    return out, ctx.llm
