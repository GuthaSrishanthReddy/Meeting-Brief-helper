# -*- coding: utf-8 -*-
"""Client cards, client-name resolution, task state replay and the task title index.

Client names, code first: the client id, the full name, its first word, a prefix ("Cobalt"),
initials or a learned abbreviation ("NB", "CL"), or name plus a word ("NB Retail"). Then client
cards built from what a person can see (external contacts' domains, meeting and doc titles, the
zones of the staff on each client) resolve descriptive nicknames such as "the <city> account".
A small model is the last resort; unresolved names are asked back, never guessed.
"""
import collections
import difflib
import re

from .common import STOP, content_words, norm_text, words
from .llm import data_block
from .store import Visible, records_of

ACRONYM_STOP = set('''api uat sso tms qa pr eod eta faq sow cta ui ux seo kpi roi crm erp hr it pm am
asap fyi ooo wfh mvp poc sla qbr usd inr gst pdf csv url aws gcp nda rfp rfq ceo cto coo cfo vp ok uk
us usa uae eu nyc la sf id dm cc bcc tbd tba eow cob utm cms cdn ios sms otp kyc'''.split())

# Place words -> country code; country code -> evidence that ties a client to that country.
GEO = {}
for _code, _places in {
    'gb': 'uk britain british england english london manchester birmingham leeds liverpool bristol '
          'edinburgh glasgow scotland wales cardiff belfast',
    'in': 'india indian mumbai bombay delhi bangalore bengaluru pune chennai madras hyderabad kolkata '
          'calcutta ahmedabad jaipur gurgaon gurugram noida kochi indore lucknow surat',
    'us': 'us usa america american york nyc boston chicago seattle austin texas california '
          'francisco angeles miami atlanta denver',
    'ca': 'canada canadian toronto vancouver montreal',
    'ae': 'uae dubai emirates abu dhabi sharjah',
    'sg': 'singapore',
    'au': 'australia australian sydney melbourne brisbane perth',
    'de': 'germany german berlin munich hamburg frankfurt',
    'fr': 'france french paris lyon',
    'nl': 'netherlands dutch amsterdam rotterdam',
    'ie': 'ireland irish dublin',
    'jp': 'japan japanese tokyo osaka',
}.items():
    for _p in _places.split():
        GEO[_p] = _code
COUNTRIES = {
    'gb': {'tlds': ('.uk',), 'words': ('uk', 'britain', 'british', 'england', 'london'), 'zones': ('Europe/London',)},
    'in': {'tlds': ('.in',), 'words': ('india', 'indian', 'mumbai', 'delhi', 'bangalore', 'bengaluru'),
           'zones': ('Asia/Kolkata', 'Asia/Calcutta')},
    'us': {'tlds': ('.us',), 'words': ('us', 'usa', 'america', 'american', 'nyc'),
           'zones': ('America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles')},
    'ca': {'tlds': ('.ca',), 'words': ('canada', 'toronto'), 'zones': ('America/Toronto', 'America/Vancouver')},
    'ae': {'tlds': ('.ae',), 'words': ('uae', 'dubai'), 'zones': ('Asia/Dubai',)},
    'sg': {'tlds': ('.sg',), 'words': ('singapore',), 'zones': ('Asia/Singapore',)},
    'au': {'tlds': ('.au',), 'words': ('australia', 'sydney'), 'zones': ('Australia/Sydney', 'Australia/Melbourne')},
    'de': {'tlds': ('.de',), 'words': ('germany', 'berlin'), 'zones': ('Europe/Berlin',)},
    'fr': {'tlds': ('.fr',), 'words': ('france', 'paris'), 'zones': ('Europe/Paris',)},
    'nl': {'tlds': ('.nl',), 'words': ('netherlands', 'amsterdam'), 'zones': ('Europe/Amsterdam',)},
    'ie': {'tlds': ('.ie',), 'words': ('ireland', 'dublin'), 'zones': ('Europe/Dublin',)},
    'jp': {'tlds': ('.jp',), 'words': ('japan', 'tokyo'), 'zones': ('Asia/Tokyo',)},
}
NICK_NOUNS = r'(?:client|account|customer|brand|retailer|company|folks|people|guys|team|office|one|project)'
GEO_PHRASES = [
    re.compile(r'\b(?:the\s+|our\s+)?((?:[A-Z][A-Za-z]+\s+){0,1}[A-Z][A-Za-z]+)(?:-based)?\s+' + NICK_NOUNS + r'\b'),
    re.compile(r'\b' + NICK_NOUNS + r'\s+(?:in|from|based\s+in|out\s+of)\s+((?:[A-Z][A-Za-z]+\s+){0,1}[A-Z][A-Za-z]+)'),
]


# A described client in lower case ("the stationery company"): not resolvable by code, so the
# model gets the client cards (and may answer "unknown").
DESCRIBED = re.compile(r'\b(?:the|our|that|this)\s+((?:[a-z][a-z\-]+\s+){0,3}[a-z][a-z\-]+)\s+'
                       r'(?:client|company|account|brand|retailer|folks|customer|people|business|firm)\b', re.I)


def _initials(name):
    return ''.join(w[0] for w in re.findall(r'[A-Za-z0-9]+', name)).lower()


def _subseq_abbrev(abbr, name):
    a = abbr.lower()
    n = re.sub(r'[^a-z0-9]', '', (name or '').lower())
    if len(a) < 2 or not n or a[0] != n[0]:
        return False
    i = 1
    for ch in a[1:]:
        j = n.find(ch, i)
        if j < 0:
            return False
        i = j + 1
    return True


def abbreviations(body, client_name):
    """Upper-case short forms in `body` that abbreviate `client_name` ("NB" for Northbeam Retail)."""
    out = set()
    for tok in re.findall(r'\b[A-Z][A-Z0-9]{1,4}\b', body or ''):
        low = tok.lower()
        if low not in ACRONYM_STOP and _subseq_abbrev(tok, client_name):
            out.add(low)
    return out


class ClientResolver(object):
    def __init__(self, clients, people, vis, llm=None, abbrevs=None):
        self.clients = clients
        self.people = people
        self.vis = vis if vis is not None else {}
        self.llm = llm
        self.abbrevs = abbrevs  # 'abbrev' facts visible to this person; None = scan the records instead
        self._cards = None
        self._learned = None

    # ------------------------------------------------------------ cards
    def _count_address(self, card, a):
        if not isinstance(a, str):
            return
        if a.startswith('ext:') and '@' in a:
            card['domains'][a.split('@', 1)[1].lower()] += 1
        elif a in self.people.by_id:
            card['zones'][self.people.zone(a)] += 1

    def _add_titles(self, card, r):
        for fld in ('title', 'name', 'subject'):
            t = r.get(fld)
            if t and r.get('type') in ('event', 'doc', 'sheet') and t not in card['titles']:
                card['titles'].append(t)

    def cards(self):
        if self._cards is not None:
            return self._cards
        cards = {}
        for c in self.clients.items:
            cards[c['id']] = {'name': c.get('name') or c['id'], 'domains': collections.Counter(),
                              'titles': [], 'zones': collections.Counter()}
        if isinstance(self.vis, Visible):
            # Authors come straight from the in-memory columns; only the few record types that carry
            # attendees, recipients or titles are read in full.
            for m in self.vis.light():
                if m[7] in cards and m[3] != 'notification':
                    self._count_address(cards[m[7]], m[6])
            for r in self.vis.of_type('event', 'mail', 'doc', 'sheet'):
                cid = r.get('client')
                if cid not in cards or r.get('_kind') == 'notification':
                    continue
                for a in list(r.get('to') or []) + list(r.get('attendees') or []):
                    self._count_address(cards[cid], a)
                self._add_titles(cards[cid], r)
        else:
            for r in self.vis.values():
                cid = r.get('client')
                if cid not in cards or r.get('_kind') == 'notification':
                    continue
                for a in [r.get('author')] + list(r.get('to') or []) + list(r.get('attendees') or []):
                    self._count_address(cards[cid], a)
                self._add_titles(cards[cid], r)
        self._cards = cards
        return cards

    def learned_abbrevs(self):
        """Upper-case short forms people actually use in records tagged with a client."""
        if self._learned is not None:
            return self._learned
        out = collections.defaultdict(set)
        if self.abbrevs is not None:  # found once at ingest, kept as facts of their source records
            for f in self.abbrevs:
                if f.get('client') in self.clients.by_id and f.get('tok'):
                    out[f['client']].add(f['tok'])
        else:
            for r in self.vis.values():
                cid = r.get('client')
                if cid not in self.clients.by_id or r.get('_kind') not in ('staff', 'client_external'):
                    continue
                out[cid].update(abbreviations(r.get('body'), self.clients.name(cid)))
        self._learned = out
        return out

    def card_text(self, cid):
        c = self.cards().get(cid) or {}
        doms = ', '.join(d for d, _ in c.get('domains', collections.Counter()).most_common(3))
        zones = ', '.join(z for z, _ in c.get('zones', collections.Counter()).most_common(3))
        return {'id': cid, 'name': c.get('name'), 'contact_domains': doms, 'staff_zones': zones,
                'titles': c.get('titles', [])[:4]}

    # ------------------------------------------------------------ resolution
    def resolve(self, text):
        """-> {'ids': [client ids], 'phrase': str, 'method': str}; ids empty if unresolved."""
        text = text or ''
        hits = {}  # cid -> (strength, phrase, method)

        def add(cid, strength, phrase, method):
            if cid not in hits or hits[cid][0] < strength:
                hits[cid] = (strength, phrase, method)

        learned = self.learned_abbrevs()
        for c in self.clients.items:
            cid, name = c['id'], c.get('name') or c['id']
            name_words = re.findall(r'[A-Za-z0-9]+', name)
            first = name_words[0] if name_words else cid
            for phrase in (name, cid, first):
                m = re.search(r'(?<![A-Za-z0-9])' + re.escape(phrase) + r'(?![A-Za-z0-9])', text, re.I)
                if m:
                    add(cid, 3, m.group(0), 'name')
                    break
            for m in re.finditer(r'[A-Za-z][A-Za-z0-9]*', text):
                tok = m.group(0)
                low = tok.lower()
                if len(tok) >= 4 and first.lower().startswith(low) and low != first.lower():
                    add(cid, 2, tok, 'prefix')
                if 2 <= len(tok) <= 5 and low not in ACRONYM_STOP:
                    if low in learned.get(cid, ()):
                        add(cid, 2, tok, 'abbreviation')
                    elif tok.isupper() and (low == _initials(name) or _subseq_abbrev(tok, name)):
                        add(cid, 2, tok, 'initials')
                if len(name_words) > 1 and len(tok) >= 4 and \
                        low.rstrip('s') in [w.lower().rstrip('s') for w in name_words[1:]]:
                    add(cid, 1, tok, 'second word')  # "food company" -> Alderpoint Foods
        geo = self._resolve_geo(text)
        if geo:
            for cid, phrase in geo:
                add(cid, 2, phrase, 'client card')
        if not hits:
            return {'ids': [], 'phrase': '', 'method': ''}
        top = max(h[0] for h in hits.values())
        best = [(cid, h) for cid, h in hits.items() if h[0] == top]
        if len(best) == 1:
            cid, h = best[0]
            return {'ids': [cid], 'phrase': h[1], 'method': h[2]}
        return {'ids': [cid for cid, _ in best], 'phrase': best[0][1][1], 'method': 'ambiguous'}

    def nickname_phrases(self, text):
        out = []
        for rx in GEO_PHRASES + [DESCRIBED]:
            for m in rx.finditer(text or ''):
                out.append((m.group(0), m.group(1)))
        return out

    def _resolve_geo(self, text):
        found = []
        for phrase, place in self.nickname_phrases(text):
            code = None
            for w in reversed(words(place)):
                if w in GEO:
                    code = GEO[w]
                    break
            if not code:
                continue
            scores = self._geo_scores(code)
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            if ranked[0][1] >= 2 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
                found.append((ranked[0][0], phrase.strip()))
        return found

    def _geo_scores(self, code):
        country = COUNTRIES.get(code)
        if not country:
            return {}
        cards = self.cards()
        all_zone_sets = [set(c['zones']) for c in cards.values() if c['zones']]
        common = set.intersection(*all_zone_sets) if all_zone_sets else set()
        scores = {}
        for cid, c in cards.items():
            s = 0
            if any(d.endswith(t) for d in c['domains'] for t in country['tlds']):
                s += 3
            if any(re.search(r'\b' + re.escape(w) + r'\b', t, re.I) for t in c['titles'] for w in country['words']):
                s += 2
            if any(z in country['zones'] and z not in common for z in c['zones']):
                s += 1
            if s:
                scores[cid] = s
        return scores

    def resolve_with_model(self, text):
        """Last resort for a descriptive nickname: three client cards and the phrase."""
        if not self.llm or not self.llm.enabled or not self.nickname_phrases(text):
            return None
        cards = [self.card_text(c['id']) for c in self.clients.items]
        prompt = ('Which client does the phrase refer to? Use only the client cards. Answer '
                  '"unknown" if the cards do not settle it.\nphrase: %s\n%s' % (text, data_block(cards)))
        schema = {'type': 'object', 'properties': {'client_id': {'type': 'string'}}, 'required': ['client_id']}
        res = self.llm.json_call('nickname', prompt, schema, max_output=60)
        cid = (res or {}).get('client_id')
        return cid if cid in self.clients.by_id else None


# ------------------------------------------------------------------ tasks
def task_states(vis):
    """Replay task records in time order. Only `created` and owner/due/status changes move state.

    Comment records (and the snapshot fields of change records for other fields) can be stale in
    the data, so they never overwrite state.
    """
    states = collections.OrderedDict()
    for r in records_of(vis, 'task'):
        if not r.get('task_id'):
            continue
        tid = r['task_id']
        ch = r.get('change') if isinstance(r.get('change'), dict) else {}
        field = ch.get('field')
        st = states.get(tid)
        if st is None:
            st = {'task_id': tid, 'title': r.get('title') or tid, 'client': r.get('client'),
                  'owner': r.get('owner'), 'due': r.get('due'), 'status': r.get('status'),
                  'src': {'owner': r['id'], 'due': r['id'], 'status': r['id']}, 'events': [],
                  'first_at': r.get('at'), 'first_day': r.get('_day')}
            if field in ('owner', 'due', 'status') and ch.get('from') is not None:
                st[field] = ch.get('from')
            states[tid] = st
        if field == 'created':
            for k in ('owner', 'due', 'status'):
                if r.get(k) is not None:
                    st[k] = r.get(k)
                    st['src'][k] = r['id']
            if r.get('title'):
                st['title'] = r['title']
            if r.get('client'):
                st['client'] = r['client']
        elif field in ('owner', 'due', 'status'):
            old = st.get(field)
            new = ch.get('to') if ch.get('to') is not None else r.get(field)
            if old is None and ch.get('from') is not None:
                old = ch.get('from')
            st[field] = new
            st['src'][field] = r['id']
            st['events'].append({'field': field, 'from': old, 'to': new, 'rid': r['id'], 'at': r.get('at'),
                                 'day': r.get('_day'), 'body': r.get('body') or '', 'author': r.get('author')})
        if not st.get('client') and r.get('client'):
            st['client'] = r.get('client')
    return states


def _title_tokens(title, clients):
    stop = set(STOP)
    for c in clients.items:
        stop.update(words(c.get('name')))
        stop.add(c['id'].lower())
    return [w for w in words(title) if w not in stop]


def match_tasks(text, states, clients, client_ids=None):
    """Score every task title against the question. -> [(score, state)] best first."""
    q_words = words(text)
    q_set = set(q_words)
    q_norm = ' ' + norm_text(text) + ' '
    out = []
    for st in states.values():
        if client_ids and st.get('client') not in client_ids:
            continue
        toks = _title_tokens(st.get('title'), clients)
        if not toks:
            continue
        got = 0.0
        for t in toks:
            if t in q_set:
                got += 1
            elif re.match(r'^v\d+$', t):
                if any(re.match(r'^v\d+$', q) for q in q_set):
                    got -= 1  # a different version number in the question
            elif len(t) >= 5 and any(len(q) >= 5 and difflib.SequenceMatcher(None, t, q).ratio() >= 0.85 for q in q_set):
                got += 0.8
        score = got / len(toks)
        if (' ' + ' '.join(toks) + ' ') in q_norm or (' ' + norm_text(st.get('title')) + ' ') in q_norm:
            score += 0.05
        if score > 0:
            out.append((round(score, 3), st))
    out.sort(key=lambda x: (-x[0], x[1]['task_id']))
    return out


def pick_task(matches, threshold=0.6, margin=0.15):
    """-> ('one', state) | ('many', [states]) | ('none', None)."""
    good = [(s, st) for s, st in matches if s >= threshold]
    if not good:
        return 'none', None
    if len(good) == 1 or good[0][0] - good[1][0] >= margin:
        return 'one', good[0][1]
    top = good[0][0]
    return 'many', [st for s, st in good if top - s < margin]
