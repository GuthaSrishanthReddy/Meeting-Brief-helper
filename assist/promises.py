# -*- coding: utf-8 -*-
"""Promises: commitment patterns (Hinglish and English), due dates worked out in the author's
zone, deliveries that close them, and a model fallback for commitments the patterns miss.

"kal tak" = the next day, "parso tak" = the day after, "next Monday tak" = the coming Monday,
"by EOD today" = the same day, "by Thursday" = the coming Thursday. A promise closes when a
"Sharing the X as promised" message arrives or a task with the same name is done. Overdue
promises stay open.
"""
import re

from .common import local_date, norm_what, parse_day, parse_ts, parse_when, sentences
from .llm import data_block

_HI_TAIL = r'(?:d(?:u|oo)ng[ai](?:\s*/\s*d(?:u|oo)ng[ai])?|denge|dega|degi)'
_MONTH = r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*'
_DAYNAME = r'(?:mon|tues|wednes|thurs|fri|satur|sun)day'
# A bare number is not a time ("the email copy for week 2 kal tak ..."): dates need a month or an ordinal.
_WHEN = (r'(?:kal|parso|aaj(?:\s+(?:shaam|eod))?|abhi|eod|today|tomorrow|(?:next|this|coming)\s+[a-z]+|' + _DAYNAME + r'|'
         r'\d{1,2}(?:st|nd|rd|th)?\s+' + _MONTH + r'|' + _MONTH + r'\s+\d{1,2}(?:st|nd|rd|th)?|\d{1,2}(?:st|nd|rd|th)|'
         r'end\s+of\s+(?:the\s+)?(?:day|week))')
HINGLISH = re.compile(
    r'^(?P<what>.*?)\s*\b(?P<when>' + _WHEN + r')\s+tak\b(?P<mid>.*?)\b'
    r'(?P<verb>(?:[a-z]+\s+)?(?:kar\s+)?' + _HI_TAIL + r')(?![a-z])', re.I)
ENGLISH = re.compile(
    r"(?:^|[.!?;,]\s*|\b(?:hi|hello|hey)\s+\w+,?\s*)(?:i|we)?\s*(?:will|'ll|’ll|shall|am going to|are going to)\s+"
    r'(?:share|send|deliver|get|provide|submit|upload|circulate|email|mail|post|forward|have|finish|'
    r'complete|prepare|draft)\s+(?:you\s+|over\s+)?(?P<what>.+?)\s+(?P<when>by\s+(?:the\s+)?(?:eod|cob|end\s+of\s+'
    r'(?:the\s+)?(?:day|week)|tonight|today|tomorrow|tmrw|noon|next\s+[a-z]+|this\s+[a-z]+|' + _DAYNAME + r'|'
    r'\d{1,2}(?:st|nd|rd|th)?\s+' + _MONTH + r'|' + _MONTH + r'\s+\d{1,2}(?:st|nd|rd|th)?|\d{1,2}(?:st|nd|rd|th)|'
    r'\d{1,2}(?::\d{2})?\s*(?:am|pm))(?:\s+today)?|today|tomorrow|tonight|(?:next|this)\s+' + _DAYNAME + r'|'
    r'eod(?:\s+today)?)\b', re.I)
LOOSE = re.compile(
    r"\b(?:d(?:u|oo)ng[ai]|denge|dega|degi|(?:deta|deti)\s+(?:hoon|hun|hu)\b|karunga|karungi|karenge|bhejta|bhejti|"
    r"i'?ll|i will|we'?ll|we will|promise|"
    r"will\s+(?:revert|share|send|deliver|get|have|finish|complete|prepare|draft|submit|update|circulate|forward|"
    r"upload|email|mail|post))\b", re.I)
DELIVER = [
    re.compile(r'\b(?:sharing|attaching|attached|sending|sent|shared|here\s+is|here\s+are|here\'s|uploaded|'
               r'uploading|forwarding|delivering|delivered)\s+(?:you\s+)?(?:the\s+|our\s+)?(?P<what>[^.!?\n]+?)'
               r'\s+as\s+promised\b', re.I),
    re.compile(r'\bas\s+promised[,:]?\s+(?:here\s+is|here\s+are|here\'s|sharing|attaching|attached|please\s+find)'
               r'\s+(?:the\s+)?(?P<what>[^.!?\n]+)', re.I),
]
BAD_WHAT = re.compile(r'^(?:back|in touch|to you|it|this|that|them)\b', re.I)


def _clean_what(s):
    s = re.sub(r'\s+', ' ', (s or '')).strip(' ,.;:-')
    s = re.sub(r'^(?:hi|hello|hey)\s+\w+,?\s*', '', s, flags=re.I)
    s = re.sub(r'\s+(?:over\s+)?to\s+(?:you|them|him|her|the\s+client)$', '', s, flags=re.I)
    return s


def extract(text, at, zone):
    """-> [{'what', 'due', 'phrase'}] for commitments written by the author of `text`."""
    ts = parse_ts(at)
    if not ts or not text:
        return []
    ref = local_date(ts, zone)
    out = []
    for sent in sentences(text):
        if sent.rstrip().endswith('?') or re.search(r'\bas\s+promised\b', sent, re.I):
            continue
        m = HINGLISH.search(sent)
        if m:
            what = _clean_what(m.group('what')) or _clean_what(m.group('mid'))
            due = parse_when(m.group('when'), ref)
            if what and due and not BAD_WHAT.search(what):
                out.append({'what': what, 'due': due.isoformat(), 'phrase': m.group(0).strip()})
                continue
        m = ENGLISH.search(sent)
        if m:
            what = _clean_what(m.group('what'))
            due = parse_when(m.group('when'), ref)
            if what and due and not BAD_WHAT.search(what):
                out.append({'what': what, 'due': due.isoformat(), 'phrase': m.group(0).strip()})
    return out


def looks_like_commitment(text):
    if not text or re.search(r'\bas\s+promised\b', text, re.I) or text.strip().endswith('?'):
        return False
    return LOOSE.search(text) is not None


def extract_delivery(text):
    for rx in DELIVER:
        m = rx.search(text or '')
        if m:
            what = _clean_what(m.group('what'))
            if what:
                return what
    return None


def same_item(a, b):
    na, nb = norm_what(a), norm_what(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    sa, sb = set(na.split()), set(nb.split())
    if len(sa & sb) / float(len(sa | sb)) >= 0.75:
        return True
    return (na in nb or nb in na) and min(len(sa), len(sb)) >= 2


def ledger(promise_facts, delivery_facts, task_states):
    """Promises with their status. A delivery closes the oldest matching open promise."""
    used = set()
    done_tasks = []
    for st in task_states.values():
        for ev in st.get('events', []):
            if ev['field'] == 'status' and ev['to'] == 'done':
                done_tasks.append((st.get('title') or '', ev['at'], ev['rid']))
    out = []
    for p in sorted(promise_facts, key=lambda f: (f['at'], f['_fid'])):
        closed_by = None
        for d in delivery_facts:
            if d['_fid'] in used or d['at'] < p['at']:
                continue
            if p.get('client') and d.get('client') and p['client'] != d['client']:
                continue
            if same_item(p.get('what'), d.get('what')):
                closed_by = d['rid']
                used.add(d['_fid'])
                break
        if not closed_by:
            for title, at, rid in done_tasks:
                if at >= p['at'] and same_item(p.get('what'), title):
                    closed_by = rid
                    break
        q = dict(p)
        q['closed_by'] = closed_by
        out.append(q)
    return out


# ------------------------------------------------------------------ model fallback (ingest)
SCHEMA = {
    'type': 'object',
    'properties': {'items': {'type': 'array', 'items': {
        'type': 'object',
        'properties': {'id': {'type': 'string'}, 'is_promise': {'type': 'boolean'},
                       'what': {'type': 'string'}, 'due': {'type': 'string', 'description': 'YYYY-MM-DD or empty'}},
        'required': ['id', 'is_promise', 'what', 'due']}}},
    'required': ['items'],
}


def model_pass(llm, candidates):
    """candidates: [{'id','text','local_date','weekday'}] the patterns could not parse.

    Returns {id: {'what', 'due'}} for the ones the model judges to be a personal commitment.
    """
    if not llm or not llm.enabled or not candidates:
        return {}
    out = {}
    for i in range(0, len(candidates), 20):
        batch = candidates[i:i + 20]
        prompt = ('For each message decide if the author personally commits to deliver something by a time. '
                  'Messages may be Hinglish ("kal tak bhej dunga" = I will send it by tomorrow). If so, give '
                  'the deliverable as written and the due date (YYYY-MM-DD) counted from the message\'s local '
                  'date: tomorrow/kal = +1 day, parso = +2, EOD/today = same day, "next <weekday>" = the first '
                  'such weekday after the message date. Otherwise is_promise=false.\n' + data_block(batch))
        res = llm.json_call('promises', prompt, SCHEMA, max_output=900)
        for it in (res or {}).get('items') or []:
            if not isinstance(it, dict) or not it.get('is_promise'):
                continue
            due = parse_day(it.get('due'))
            what = _clean_what(it.get('what'))
            if it.get('id') and due and what:
                out[it['id']] = {'what': what, 'due': due.isoformat()}
    return out
