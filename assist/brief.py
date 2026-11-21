# -*- coding: utf-8 -*-
"""brief --person P --date D: one brief per meeting P attends on that local date.

Window: records from the day of P's last meeting with the same client that had already happened
when the brief is prepared (end of the last ingested day), up to now; from day 1 if there was
none. (The plan says "from the start of that meeting"; the kit's answer keys count the whole day
of the last meeting - e.g. the founder's billing message at 04:45 UTC before the 05:30 UTC
check-in - so the window starts at the start of that day.)

Change lines come only from: task status to done/blocked/cancelled (and unblocked), task owner
and due changes, 'Decision:' lines in meeting notes, ownership announcements, client date
requests, doc/sheet version diffs, and deliveries (optional, last). Order: ownership, decisions,
client dates and blockers first; then done/owner/due; deliveries last.
"""
import re

from . import owners, promises
from .clients import task_states
from .common import Clients, dedupe, find_dates, fmt_dm, local_date, parse_day, parse_ts, words
from .guard import Guard, build_superseded, history_from_facts, subject_words
from .store import records_of
from .llm import LLM

GENERIC_BODIES = set(['done and shared', 'updated the file', 'small fixes done', 'pushed an update for review',
                      'added screenshots', 'working on feedback', 'synced with the team', 'waiting for assets'])
STATUS_WORDS = {'todo': 'to do (not started)', 'in_progress': 'in progress', 'blocked': 'blocked', 'done': 'done',
                'cancelled': 'cancelled'}
KIND_ORDER = {'ownership': 0, 'decision': 1, 'client_date': 2, 'blocked': 3, 'done': 4, 'owner': 5, 'due': 6,
              'cancelled': 7, 'unblocked': 8, 'doc_change': 9, 'sheet_change': 10, 'delivered': 11}


def day_of(store, d):
    base = parse_day(store.meta('day1'))
    return (d - base).days + 1 if (base and d) else None


def latest_events(vis):
    latest = {}
    for r in records_of(vis, 'event'):  # time order: a later record of the same event replaces it
        if r.get('type') == 'event' and r.get('event_id'):
            latest[r['event_id']] = r
    return latest


def meetings_on(vis, person, date, zone):
    out = []
    for ev in latest_events(vis).values():
        if person not in (ev.get('attendees') or []):
            continue
        st = parse_ts(ev.get('start'))
        if st and local_date(st, zone) == date:
            out.append(ev)
    out.sort(key=lambda e: (e.get('start') or '', e.get('event_id')))
    return out


def window_start(store, vis, person, client, zone, last_day):
    best = None
    for ev in latest_events(vis).values():
        if ev.get('client') != client or person not in (ev.get('attendees') or []):
            continue
        st = parse_ts(ev.get('start'))
        d = day_of(store, local_date(st, zone)) if st else None
        if d is not None and d <= last_day and (best is None or d > best):
            best = d
    return best or 1


def blocked_reason(body):
    b = (body or '').strip().rstrip('.')
    m = re.match(r'^\s*blocked\s*[:\-–]\s*(.+)$', b, re.I)
    if m:
        return m.group(1).strip()
    if b and b.lower() not in GENERIC_BODIES and len(b) <= 100:
        return b
    return ''


def item(kind, text, sources, at, **extra):
    d = {'kind': kind, 'prio': 1 if kind in ('ownership', 'decision', 'client_date', 'blocked') else
         (3 if kind == 'delivered' else 2), 'text': text, 'sources': dedupe(sources), 'at': at or ''}
    d.update(extra)
    return d


def task_items(states, client, start_day, people):
    out = []
    for st in states.values():
        if st.get('client') != client:
            continue
        evs = [e for e in st['events'] if (e.get('day') or 0) >= start_day]
        title = st.get('title') or st['task_id']
        for field in ('status', 'owner', 'due'):
            fe = [e for e in evs if e['field'] == field]
            if not fe:
                continue
            before, after, last = fe[0]['from'], fe[-1]['to'], fe[-1]
            if before == after:
                continue
            if field == 'status':
                if after == 'done':
                    out.append(item('done', '%s is done' % title, [last['rid']], last['at'], task=st['task_id'], title=title))
                elif after == 'blocked':
                    why = blocked_reason(last.get('body'))
                    out.append(item('blocked', '%s is blocked%s' % (title, ' (%s)' % why if why else ''),
                                    [last['rid']], last['at'], task=st['task_id'], title=title))
                elif after == 'cancelled':
                    out.append(item('cancelled', '%s is cancelled' % title, [last['rid']], last['at'],
                                    task=st['task_id'], title=title))
                elif before == 'blocked' and after in ('todo', 'in_progress'):
                    out.append(item('unblocked', '%s is unblocked (now %s)' % (title, STATUS_WORDS.get(after, after)),
                                    [last['rid']], last['at'], task=st['task_id'], title=title))
            elif field == 'owner' and after:
                was = ' (was %s)' % people.name(before) if before else ''
                out.append(item('owner', '%s now owned by %s%s' % (title, people.name(after), was), [last['rid']],
                                last['at'], task=st['task_id'], title=title))
            elif field == 'due':
                d0, d1 = parse_day(before), parse_day(after)
                if d1:
                    text = ('%s due date moved from %s to %s' % (title, fmt_dm(d0), fmt_dm(d1)) if d0 else
                            '%s due date moved to %s' % (title, fmt_dm(d1)))
                    out.append(item('due', text, [last['rid']], last['at'], task=st['task_id'], title=title,
                                    new=d1.isoformat()))
    return out


def previous_owner(own_facts, fact, client):
    earlier = [f for f in own_facts if f['at'] < fact['at'] and f['_fid'] != fact['_fid']]
    when = parse_ts(fact['at'])
    prev = owners.resolve(earlier, client, fact.get('function'), when) if when else None
    return prev.get('person') if prev else None


def build(store, person, date_str):
    people, clients = store.people, Clients()
    last = store.last_day()
    date = parse_day(date_str)
    if not last or not date:
        return []
    llm = LLM(store, 'brief', budget=8000)
    vis = store.visible(person, last)
    zone = people.zone(person)
    meetings = meetings_on(vis, person, date, zone)
    if not meetings:
        return []
    states = task_states(vis)
    prefs = store.prefs(person)
    forgets = store.memories(person, 'forget')
    suppress = store.memories(person, 'suppress')
    boosts = store.memories(person, 'boost')
    facts = {k: store.facts([k], vis) for k in ('decision', 'ownership', 'date_request', 'doc_line', 'doc_change',
                                                 'sheet_change', 'delivery', 'promise')}
    own_hist, date_hist = history_from_facts(facts['ownership'], facts['date_request'], facts['doc_line'],
                                             owners.func_match, find_dates)
    guard = Guard(person, vis, last, forgets, suppress, build_superseded(people, states, own_hist, date_hist))

    out = []
    for ev in meetings:
        client = ev.get('client')
        b = {'meeting_id': ev.get('event_id'), 'status': 'nothing_new', 'changed': [], 'promises': [],
             'open_these': [], 'question_to_ask': None}
        if client:
            start_day = window_start(store, vis, person, client, zone, last)
            items = collect(client, start_day, states, facts, people, clients, guard)
            items = rank(items, prefs, boosts)
            if items:
                b['status'] = 'brief'
                b['changed'] = [{'text': it['text'], 'sources': it['sources']} for it in items]
                if not prefs.get('skip_promises'):
                    b['promises'] = open_promises(facts, states, client, guard)
                b['open_these'] = [{'record': it['sources'][0], 'why': it['text']} for it in items[:3] if it['sources']]
                b['question_to_ask'] = question_for(items)
        clean = guard.brief(b)
        clean['usage'] = llm.usage(0)
        out.append(clean)
    return out


def collect(client, start_day, states, facts, people, clients, guard):
    in_win = lambda f: (f.get('day') or 0) >= start_day and f.get('client') == client  # noqa: E731
    items = task_items(states, client, start_day, people)
    cname = clients.name(client)

    # decisions in meeting notes; also cite the matching cancelled task and drop its own line
    for f in facts['decision']:
        if not in_win(f):
            continue
        srcs = [f['rid']]
        dwords = set(words(f['text']))
        for it in list(items):
            if it['kind'] == 'cancelled':
                tw = subject_words(it.get('title'))
                if tw and tw <= dwords:
                    srcs.extend(it['sources'])
                    items.remove(it)
        items.append(item('decision', f['text'], srcs, f['at']))

    # ownership announcements (function owners such as billing)
    for f in facts['ownership']:
        if f.get('via') != 'announcement' or not in_win(f):
            continue
        prev = f.get('prev') or previous_owner(facts['ownership'], f, client)
        if prev == f['person']:
            continue
        func = ' '.join(f.get('function') or [])
        text = '%s now owns %s for %s%s' % (people.name(f['person']), func, cname,
                                           ' (was %s)' % people.name(prev) if prev else '')
        items.append(item('ownership', text, [f['rid']], f['at']))

    # client date requests, merged with doc lines that confirm the same new date
    used_doc = set()
    for rq in facts['date_request']:
        if not in_win(rq):
            continue
        new = parse_day(rq.get('new'))
        subj = rq.get('subject') or ''
        sw = set(words(subj))
        old = None
        for dl in facts['doc_line']:
            if dl['at'] < rq['at'] and dl.get('client') == client and sw & set(words(dl.get('label'))):
                ds = find_dates(dl.get('value'), new)
                if ds and ds[0]['date'] != new:
                    old = ds[0]['date']
        srcs = [rq['rid']]
        for dc in facts['doc_change']:
            if dc.get('client') == client and sw & set(words(dc.get('label'))) and (dc.get('day') or 0) >= start_day:
                ds = find_dates(dc.get('new'), new)
                if ds and ds[0]['date'] == new:
                    srcs.append(dc['rid'])
                    used_doc.add(dc['_fid'])
        label = subj[:1].upper() + subj[1:]
        text = ('%s moved from %s to %s' % (label, fmt_dm(old), fmt_dm(new)) if old else '%s moved to %s' % (label, fmt_dm(new)))
        if rq.get('external'):
            text += ' (client request)'
        if any(i['kind'] == 'client_date' and i['text'].split(' moved')[0].lower() == label.lower() for i in items):
            continue
        items.append(item('client_date', text, srcs, rq['at'], new=new.isoformat() if new else None))

    # other doc diffs
    for dc in facts['doc_change']:
        if dc['_fid'] in used_doc or not in_win(dc):
            continue
        old_v, new_v, label = dc.get('old') or '', dc.get('new') or '', dc.get('label') or ''
        if dc.get('old_rid') not in guard.vis:
            old_v = ''
        ref = parse_day((dc.get('at') or '')[:10])
        od, nd = find_dates(old_v, ref) if old_v else [], find_dates(new_v, ref)
        if nd and (not old_v or od):
            text = ('%s moved from %s to %s' % (label, fmt_dm(od[0]['date']), fmt_dm(nd[0]['date'])) if od else
                    '%s moved to %s' % (label, fmt_dm(nd[0]['date'])))
            if any(i['kind'] == 'client_date' and i['text'].split(' moved')[0].lower() in label.lower() for i in items):
                for i in items:
                    if i['kind'] == 'client_date' and i['text'].split(' moved')[0].lower() in label.lower():
                        i['sources'] = dedupe(i['sources'] + [dc['rid']])
                continue
            items.append(item('client_date', text, [dc['rid']], dc['at'], new=nd[0]['date'].isoformat()))
            continue
        olist = [x.strip() for x in re.split(r',|\+|\band\b', old_v) if x.strip()]
        nlist = [x.strip() for x in re.split(r',|\+|\band\b', new_v) if x.strip()]
        title = dc.get('title') or 'Doc'
        if old_v and len(olist) > 1 or len(nlist) > 1:
            added = [x for x in nlist if x.lower() not in [y.lower() for y in olist]]
            removed = [x for x in olist if x.lower() not in [y.lower() for y in nlist]]
            for x in added:
                items.append(item('doc_change', '%s added to %s (%s)' % (x, label, title), [dc['rid']], dc['at']))
            for x in removed:
                items.append(item('doc_change', '%s removed from %s (%s)' % (x, label, title), [dc['rid']], dc['at']))
            if added or removed:
                continue
        text = ('%s: %s changed from %s to %s' % (title, label, old_v, new_v) if old_v else
                '%s: %s is now %s' % (title, label, new_v))
        items.append(item('doc_change', text, [dc['rid']], dc['at']))

    for sc in facts['sheet_change']:
        if not in_win(sc):
            continue
        what = sc.get('row_label') or sc.get('cell')
        if sc.get('col_label'):
            what = '%s %s' % (what, sc['col_label'])
        old_v = sc.get('old') if sc.get('old_rid') in guard.vis else None
        text = ('%s: %s changed from %s to %s' % (sc.get('name'), what, old_v, sc.get('new')) if old_v not in (None, '')
                else '%s: %s is now %s' % (sc.get('name'), what, sc.get('new')))
        items.append(item('sheet_change', text, [sc['rid']], sc['at']))

    for f in facts['delivery']:
        if in_win(f):
            items.append(item('delivered', '%s delivered the %s' % (people.first(f.get('by')), f.get('what')),
                              [f['rid']], f['at']))

    keep = []
    for it in items:
        if not it['sources']:
            continue
        if any(guard.record_blocked(guard.vis[s]) for s in it['sources'] if s in guard.vis):
            it['sources'] = [s for s in it['sources'] if s in guard.vis and not guard.record_blocked(guard.vis[s])]
            if not it['sources']:
                continue
        keep.append(it)
    return keep


def rank(items, prefs, boosts):
    from .feedback import topic_matches
    if prefs.get('skip_deliveries'):
        items = [i for i in items if i['kind'] != 'delivered']
    for i in items:
        i['boost'] = 1 if any(topic_matches(b, i['text']) for b in boosts) else 0
    items.sort(key=lambda i: (i['prio'], -i['boost'], i['at'], KIND_ORDER.get(i['kind'], 99)))
    if prefs.get('only_blockers') and any(i['kind'] == 'blocked' for i in items):
        items = [i for i in items if i['kind'] == 'blocked']
    if prefs.get('blockers_first'):
        items.sort(key=lambda i: 0 if i['kind'] == 'blocked' else 1)
    n = prefs.get('max_changed')
    try:
        n = int(n) if n else None
    except (TypeError, ValueError):
        n = None
    if n and n > 0:
        items = items[:n]
    return items


def open_promises(facts, states, client, guard):
    led = promises.ledger(facts['promise'], facts['delivery'], states)
    out = []
    for p in sorted(led, key=lambda x: (x.get('due') or '', x['at'])):
        if p.get('closed_by') or p.get('client') != client:
            continue
        r = guard.vis.get(p['rid'])
        if r is None or guard.record_blocked(r):
            continue
        out.append({'owner': p.get('owner'), 'what': p.get('what'), 'due': p.get('due'), 'sources': [p['rid']]})
    return out


def _inline(name):
    """'Launch' -> 'launch' inside a sentence; leave acronyms such as 'TMS data pipeline' alone."""
    return name[:1].lower() + name[1:] if len(name) > 1 and name[1:2].islower() else name


def question_for(items):
    """One question from the top blocker or date change, citing the same source."""
    for it in items:
        if it['kind'] == 'blocked' and it.get('title'):
            return {'text': 'What do we need to unblock the %s?' % _inline(it['title']), 'sources': it['sources'][:1]}
        if it['kind'] == 'client_date' and it.get('new'):
            subj = it['text'].split(' moved')[0]
            return {'text': 'Is everything still on track for the %s on %s?' % (_inline(subj), fmt_dm(parse_day(it['new']))),
                    'sources': it['sources'][:1]}
        if it['kind'] == 'due' and it.get('new') and it.get('title'):
            return {'text': 'Is %s still on track for %s?' % (_inline(it['title']), fmt_dm(parse_day(it['new']))),
                    'sources': it['sources'][:1]}
    return None
