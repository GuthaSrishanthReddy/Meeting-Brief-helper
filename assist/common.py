# -*- coding: utf-8 -*-
"""Shared helpers: pack paths, people, clients, time zones, dates and small text utilities.

Standard library only, Python 3.8 compatible. zoneinfo is used when it is available and knows
the zone; otherwise a built-in table covers common zones (fixed offsets plus the EU and US
daylight-saving rules), so Windows machines without tzdata still convert correctly.
"""
import datetime as dt
import json
import os
import re
import sys
import time

T0 = time.time()
UTC = dt.timezone.utc
ONE_DAY = dt.timedelta(days=1)

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
_MONTH_FULL = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
               'september', 'october', 'november', 'december']
MONTH_NUM = {}
for _i, _m in enumerate(_MONTH_FULL):
    MONTH_NUM[_m] = _i + 1
    MONTH_NUM[_m[:3]] = _i + 1
MONTH_NUM['sept'] = 9
WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
WEEKDAY_NUM = {}
for _i, _d in enumerate(WEEKDAYS):
    WEEKDAY_NUM[_d] = _i
    WEEKDAY_NUM[_d[:3]] = _i
WEEKDAY_NUM.update({'tues': 1, 'weds': 2, 'thur': 3, 'thurs': 3})


def log(*parts):
    """Logs go to stderr only; stdout is reserved for the JSON answer."""
    try:
        print('[assist]', *parts, file=sys.stderr)
    except Exception:
        pass


def elapsed_ms():
    return int((time.time() - T0) * 1000)


# ------------------------------------------------------------------ paths and files
def pack_root():
    env = os.environ.get('R3_PACK')
    if env and os.path.isdir(os.path.join(env, 'data')):
        return os.path.abspath(env)
    return os.getcwd()


def data_path(*parts):
    return os.path.join(pack_root(), 'data', *parts)


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_jsonl(path):
    rows = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    log('skipping a line that is not JSON in', path)
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        pass
    return rows


def trim_log(path, max_bytes, keep_lines):
    """Keep a JSONL log bounded: once it is over `max_bytes`, rewrite it with only its last `keep_lines`
    lines. Never raises."""
    try:
        if os.path.getsize(path) <= max_bytes:
            return
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()[-keep_lines:]
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        os.replace(tmp, path)
    except OSError:
        pass


# ------------------------------------------------------------------ .env
def load_dotenv():
    """Load KEY=VALUE lines from a .env file into os.environ (existing variables win).

    Looked up in $R3_DOTENV if set, else next to assist.py, then the current folder. Values are
    never logged. Returns the names of the variables that were set from the file.
    """
    explicit = os.environ.get('R3_DOTENV')
    paths = [explicit] if explicit else [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'),
        os.path.join(os.getcwd(), '.env')]
    loaded = []
    for path in paths:
        try:
            with open(path, encoding='utf-8-sig') as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.lower().startswith('export '):
                line = line[7:].lstrip()
            key, sep, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if not sep or not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]
            elif ' #' in value:
                value = value.split(' #', 1)[0].rstrip()
            if value and not os.environ.get(key):
                os.environ[key] = value
                loaded.append(key)
    return loaded


# ------------------------------------------------------------------ timestamps
def parse_ts(s):
    """ISO string -> aware UTC datetime (or None)."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip()
    if t[-1:] in ('Z', 'z'):
        t = t[:-1] + '+00:00'
    d = None
    try:
        d = dt.datetime.fromisoformat(t)
    except ValueError:
        for fmt, n in (('%Y-%m-%dT%H:%M:%S', 19), ('%Y-%m-%dT%H:%M', 16), ('%Y-%m-%d', 10)):
            try:
                d = dt.datetime.strptime(t[:n], fmt)
                break
            except ValueError:
                continue
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def iso(d):
    return d.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def canon_ts(s):
    d = parse_ts(s)
    return iso(d) if d else (s or '')


def parse_day(s):
    try:
        return dt.datetime.strptime(str(s)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def fmt_dm(d):
    return '%d %s' % (d.day, MONTHS[d.month - 1])


def fmt_dmy(d):
    return '%d %s %d' % (d.day, MONTHS[d.month - 1], d.year)


# ------------------------------------------------------------------ time zones
try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python 3.8
    ZoneInfo = None

_FIXED = {
    'UTC': 0, 'Etc/UTC': 0, 'GMT': 0, 'Etc/GMT': 0, 'Asia/Kolkata': 330, 'Asia/Calcutta': 330,
    'Asia/Colombo': 330, 'Asia/Kathmandu': 345, 'Asia/Dhaka': 360, 'Asia/Karachi': 300,
    'Asia/Dubai': 240, 'Asia/Muscat': 240, 'Asia/Riyadh': 180, 'Asia/Qatar': 180,
    'Asia/Singapore': 480, 'Asia/Kuala_Lumpur': 480, 'Asia/Shanghai': 480, 'Asia/Hong_Kong': 480,
    'Asia/Manila': 480, 'Asia/Jakarta': 420, 'Asia/Bangkok': 420, 'Asia/Ho_Chi_Minh': 420,
    'Asia/Tokyo': 540, 'Asia/Seoul': 540, 'Africa/Nairobi': 180, 'Africa/Lagos': 60,
    'Africa/Johannesburg': 120, 'Africa/Cairo': 120, 'America/Sao_Paulo': -180,
}
_EU = {
    'Europe/London': 0, 'Europe/Dublin': 0, 'Europe/Lisbon': 0, 'Europe/Paris': 60,
    'Europe/Berlin': 60, 'Europe/Madrid': 60, 'Europe/Rome': 60, 'Europe/Amsterdam': 60,
    'Europe/Brussels': 60, 'Europe/Stockholm': 60, 'Europe/Oslo': 60, 'Europe/Copenhagen': 60,
    'Europe/Zurich': 60, 'Europe/Vienna': 60, 'Europe/Warsaw': 60, 'Europe/Prague': 60,
    'Europe/Athens': 120, 'Europe/Helsinki': 120, 'Europe/Kiev': 120, 'Europe/Kyiv': 120,
    'Europe/Bucharest': 120,
}
_US = {
    'America/New_York': -300, 'America/Toronto': -300, 'America/Chicago': -360,
    'America/Denver': -420, 'America/Los_Angeles': -480, 'America/Vancouver': -480,
}
_zi_cache = {}
_warned = set()


def _nth_weekday(year, month, weekday, n):
    """n-th (1-based) given weekday of a month; n=-1 is the last one."""
    if n > 0:
        d = dt.date(year, month, 1)
        d += dt.timedelta(days=(weekday - d.weekday()) % 7)
        return d + dt.timedelta(days=7 * (n - 1))
    nxt = dt.date(year + (1 if month == 12 else 0), month % 12 + 1, 1)
    d = nxt - ONE_DAY
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def utc_offset(zone, when):
    zone = zone or 'UTC'
    if ZoneInfo is not None:
        zi = _zi_cache.get(zone, 0)
        if zi == 0:
            try:
                zi = ZoneInfo(zone)
            except Exception:
                zi = None
            _zi_cache[zone] = zi
        if zi is not None:
            off = when.astimezone(zi).utcoffset()
            if off is not None:
                return off
    if zone in _FIXED:
        return dt.timedelta(minutes=_FIXED[zone])
    if zone in _EU:
        y = when.year
        start = dt.datetime.combine(_nth_weekday(y, 3, 6, -1), dt.time(1), UTC)
        end = dt.datetime.combine(_nth_weekday(y, 10, 6, -1), dt.time(1), UTC)
        return dt.timedelta(minutes=_EU[zone] + (60 if start <= when < end else 0))
    if zone in _US:
        base = _US[zone]
        y = when.year
        start = dt.datetime.combine(_nth_weekday(y, 3, 6, 2), dt.time(2), UTC) - dt.timedelta(minutes=base)
        end = dt.datetime.combine(_nth_weekday(y, 11, 6, 1), dt.time(2), UTC) - dt.timedelta(minutes=base + 60)
        return dt.timedelta(minutes=base + (60 if start <= when < end else 0))
    if zone not in _warned:
        _warned.add(zone)
        log('unknown time zone, treating it as UTC:', zone)
    return dt.timedelta(0)


def to_local(when, zone):
    """Aware UTC datetime -> naive wall-clock datetime in `zone`."""
    return (when + utc_offset(zone, when)).replace(tzinfo=None)


def local_date(when, zone):
    return to_local(when, zone).date()


def local_day_end_utc(day, zone):
    """UTC instant when local date `day` ends (00:00 of the next local day) in `zone`."""
    guess = dt.datetime.combine(day + ONE_DAY, dt.time(0), UTC)
    t = guess - utc_offset(zone, guess)
    return guess - utc_offset(zone, t)


# ------------------------------------------------------------------ date phrases
_MON = (r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|'
        r'sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)')
_ORD = r'(?:st|nd|rd|th)?'
_RANGE = r'(?:\s*(?:-|–|—|to)\s*(\d{1,2})' + _ORD + r')?'
RE_ISO_DATE = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b')
RE_DM = re.compile(r'(?<![\w-])(\d{1,2})' + _ORD + _RANGE + r'\s+(?:of\s+)?(' + _MON + r')\b\.?(?:,?\s+(\d{4}))?', re.I)
RE_MD = re.compile(r'\b(' + _MON + r')\.?\s+(\d{1,2})' + _ORD + _RANGE + r'(?:,?\s+(\d{4}))?(?!\w)', re.I)
RE_ORDINAL = re.compile(r'\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b', re.I)
RE_WEEKDAY = re.compile(r'\b(next|this|coming)?\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday|'
                        r'mon|tues|tue|weds|wed|thurs|thur|thu|fri|sat|sun)\b', re.I)


def _month_of(s):
    s = (s or '').lower().rstrip('.')
    return MONTH_NUM.get(s) or MONTH_NUM.get(s[:4]) or MONTH_NUM.get(s[:3])


def _date_near(month, day, ref, year=None):
    """Build a date; with no year given, pick the year that puts it nearest to `ref`."""
    try:
        if year:
            return dt.date(int(year), month, day)
        cand = dt.date(ref.year, month, day)
    except ValueError:
        return None
    if (ref - cand).days > 183:
        try:
            cand = dt.date(ref.year + 1, month, day)
        except ValueError:
            return None
    elif (cand - ref).days > 183:
        try:
            cand = dt.date(ref.year - 1, month, day)
        except ValueError:
            return None
    return cand


def find_dates(text, ref):
    """Calendar dates written in `text`. Returns dicts {date, end, span, text}, in text order.

    Understands 2026-11-14, 14 Nov, 14th November 2026, Nov 14, and ranges such as 20-24 Nov.
    """
    text = text or ''
    found, taken = [], []

    def free(span):
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    for m in RE_ISO_DATE.finditer(text):
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        found.append({'date': d, 'end': d, 'span': m.span(), 'text': m.group(0)})
        taken.append(m.span())
    for m in RE_DM.finditer(text):
        if not free(m.span()):
            continue
        mon = _month_of(m.group(3))
        d1 = _date_near(mon, int(m.group(1)), ref, m.group(4)) if mon else None
        if not d1:
            continue
        d2 = _date_near(mon, int(m.group(2)), ref, m.group(4)) if m.group(2) else d1
        found.append({'date': d1, 'end': d2 or d1, 'span': m.span(), 'text': m.group(0)})
        taken.append(m.span())
    for m in RE_MD.finditer(text):
        if not free(m.span()):
            continue
        mon = _month_of(m.group(1))
        d1 = _date_near(mon, int(m.group(2)), ref, m.group(4)) if mon else None
        if not d1:
            continue
        d2 = _date_near(mon, int(m.group(3)), ref, m.group(4)) if m.group(3) else d1
        found.append({'date': d1, 'end': d2 or d1, 'span': m.span(), 'text': m.group(0)})
        taken.append(m.span())
    found.sort(key=lambda x: x['span'][0])
    return found


def parse_when(phrase, ref):
    """A deadline/when phrase -> date, relative to the local date `ref` of the speaker.

    'kal' / 'tomorrow' = next day, 'parso' = day after, 'EOD' / 'today' / 'aaj' = same day,
    'next Monday' = the first Monday after today, 'by Thursday' = the coming Thursday
    (today if it is Thursday), 'the 12th' = the 12th of this month (next month if passed).
    """
    p = (phrase or '').lower().strip()
    if not p or ref is None:
        return None
    ds = find_dates(phrase, ref)
    if ds:
        return ds[0]['date']
    if re.search(r'\b(?:day after tomorrow|parso|parson)\b', p):
        return ref + 2 * ONE_DAY
    if re.search(r'\b(?:kal|tomorrow|tmrw|tmr|tomo)\b', p):
        return ref + ONE_DAY
    m = RE_WEEKDAY.search(p)
    if m:
        wd = WEEKDAY_NUM.get(m.group(2).lower())
        if wd is not None:
            ahead = (wd - ref.weekday()) % 7
            if m.group(1) and m.group(1).lower() == 'next' and ahead == 0:
                ahead = 7
            return ref + dt.timedelta(days=ahead)
    if re.search(r'\b(?:eod|cob|end of (?:the )?day|today|tonight|aaj|abhi|this evening|shaam|noon)\b', p) or \
            re.search(r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b', p):
        return ref
    if re.search(r'\b(?:end of (?:the )?week|eow|this week)\b', p):
        return ref + dt.timedelta(days=(4 - ref.weekday()) % 7)
    if re.search(r'\bnext week\b', p):
        monday = ref + dt.timedelta(days=(7 - ref.weekday()) or 7)
        return monday + dt.timedelta(days=4)
    m = RE_ORDINAL.search(p)
    if m:
        day = int(m.group(1))
        for k in range(0, 3):
            month = (ref.month - 1 + k) % 12 + 1
            year = ref.year + (ref.month - 1 + k) // 12
            try:
                cand = dt.date(year, month, day)
            except ValueError:
                continue
            if cand >= ref - dt.timedelta(days=3):
                return cand
    return None


# ------------------------------------------------------------------ text
_APOS = re.compile(u"['’]")
_NON = re.compile(r'[^a-z0-9]+')

STOP = set('''a an the and or but if of to for in on at by with from into onto over under about as
is are was were be been being am do does did doing have has had having it its this that these those
there here what which who whom whose when where why how whats whos i me my mine we us our ours you
your yours he him his she her hers they them their theirs can could would should will shall may
might must not no yes please pls thanks thank ok okay hi hello hey dear team regards now just also so
than then too very any all some each every more most other such own same only quite really still yet
up down out off again once let lets get got via per re fwd fw'''.split())


def norm_text(s):
    s = _APOS.sub('', (s or '').lower())
    return _NON.sub(' ', s).strip()


def words(s):
    return norm_text(s).split()


def content_words(s):
    return [w for w in words(s) if w not in STOP and len(w) > 1]


def norm_what(s):
    """Normalise a deliverable phrase: 'the UTM sheet.' -> 'utm sheet'."""
    w = words(s)
    while w and w[0] in ('the', 'a', 'an', 'our', 'my', 'your', 'their', 'his', 'her', 'its'):
        w = w[1:]
    return ' '.join(w)


def sentences(text):
    parts = re.split(r'(?<=[.!?])\s+|\n+', text or '')
    return [p.strip() for p in parts if p and p.strip()]


def record_text(r):
    """All the readable text of a record: body, title, subject, name and sheet cell values."""
    if not r:
        return ''
    parts = [r.get('body') or '', r.get('title') or '', r.get('subject') or '', r.get('name') or '']
    cells = r.get('cells')
    if isinstance(cells, dict):
        parts.extend(str(v) for v in cells.values())
    return '\n'.join(p for p in parts if p)


def dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def has_word(text, word):
    return re.search(r'(?<![A-Za-z0-9])' + re.escape(word) + r'(?![A-Za-z0-9])', text or '', re.I) is not None


# ------------------------------------------------------------------ people
TIER_FOUNDER = 4
TIER_MANAGER = 3
TIER_STAFF = 2
TIER_INTERN = 1

FUNCTION_ROLE_HINTS = {
    'billing': ('accounts', 'billing', 'finance'),
    'invoice': ('accounts', 'billing', 'finance'),
    'invoices': ('accounts', 'billing', 'finance'),
    'invoicing': ('accounts', 'billing', 'finance'),
    'payments': ('accounts', 'billing', 'finance'),
    'accounts': ('accounts', 'billing', 'finance'),
    'finance': ('accounts', 'finance'),
    'audits': ('accounts', 'finance'),
    'design': ('design',),
    'designs': ('design',),
    'creative': ('design',),
    'banner': ('design',),
    'banners': ('design',),
    'wireframes': ('design', 'dev'),
    'mockups': ('design',),
    'figma': ('design',),
    'dev': ('dev', 'developer', 'engineer'),
    'development': ('dev', 'developer', 'engineer'),
    'engineering': ('dev', 'developer', 'engineer'),
    'code': ('dev', 'developer'),
    'api': ('dev', 'developer'),
    'qa': ('qa',),
    'testing': ('qa',),
    'uat': ('qa',),
    'content': ('content', 'writer', 'copywriter'),
    'copy': ('content', 'writer', 'copywriter'),
    'copywriting': ('content', 'copywriter'),
    'social': ('social',),
    'analytics': ('data', 'analyst'),
    'data': ('data', 'analyst'),
    'reporting': ('data', 'analyst', 'operations'),
    'sales': ('sales',),
    'hiring': ('hr',),
    'hr': ('hr',),
    'operations': ('ops', 'operations'),
    'ops': ('ops', 'operations'),
}


class People(object):
    """people.json, re-read on every call (new people can appear on hidden days)."""

    def __init__(self, path=None):
        raw = read_json(path or data_path('people.json'), {}) or {}
        self.by_id = {}
        self.orgs = {}
        if isinstance(raw, dict):
            for org, block in raw.items():
                plist = block.get('people') if isinstance(block, dict) else None
                for p in plist or []:
                    if isinstance(p, dict) and p.get('id'):
                        q = dict(p)
                        q['org'] = org
                        self.by_id[q['id']] = q
                        self.orgs.setdefault(org, []).append(q['id'])
        self._org_zone = {}
        for org, ids in self.orgs.items():
            counts = {}
            for i in ids:
                z = self.by_id[i].get('zone')
                if z:
                    counts[z] = counts.get(z, 0) + 1
            self._org_zone[org] = max(counts, key=counts.get) if counts else 'UTC'

    def get(self, pid):
        return self.by_id.get(pid) or {}

    def org_of(self, pid):
        p = self.by_id.get(pid)
        if p:
            return p['org']
        m = re.match(r'[A-Za-z]+', pid or '')
        if not m or (pid or '').startswith('ext:') or pid == 'system':
            return None
        prefix = m.group(0)
        for org, ids in self.orgs.items():
            for i in ids:
                mm = re.match(r'[A-Za-z]+', i)
                if mm and mm.group(0) == prefix:
                    return org
        return None

    def default_zone(self, org=None):
        if org and org in self._org_zone:
            return self._org_zone[org]
        if self._org_zone:
            return list(self._org_zone.values())[0]
        return 'UTC'

    def zone(self, pid, org=None):
        z = self.get(pid).get('zone')
        return z or self.default_zone(org or self.org_of(pid))

    def name(self, pid):
        p = self.by_id.get(pid)
        if p and p.get('name'):
            return p['name']
        if (pid or '').startswith('ext:'):
            local = pid[4:].split('@')[0]
            return ' '.join(x.capitalize() for x in re.split(r'[._-]+', local) if x) or pid
        return pid or ''

    def first(self, pid):
        n = self.name(pid)
        return n.split()[0] if n else pid

    def role(self, pid):
        return self.get(pid).get('role') or ''

    def tier(self, pid):
        if not pid or pid.startswith('ext:') or pid == 'system':
            return 0
        role = self.role(pid).lower()
        if re.search(r'\b(founder|co-founder|ceo|coo|cto|cfo|chief|director|president|vp|head|partner)\b', role):
            return TIER_FOUNDER
        if re.search(r'\b(manager|lead)\b', role):
            return TIER_MANAGER
        if re.search(r'\b(intern|trainee|apprentice)\b', role):
            return TIER_INTERN
        return TIER_STAFF

    def candidates(self, name, org=None):
        """Person ids whose full name, first name or last name matches `name`."""
        n = norm_text(name)
        if not n:
            return []
        people = [p for p in self.by_id.values() if org is None or p['org'] == org]
        full = [p['id'] for p in people if norm_text(p.get('name')) == n]
        if full:
            return full
        toks = n.split()
        if len(toks) == 1:
            first = [p['id'] for p in people if (norm_text(p.get('name')).split() or [''])[0] == toks[0]]
            if first:
                return first
            return [p['id'] for p in people if (norm_text(p.get('name')).split() or [''])[-1] == toks[0]]
        return [p['id'] for p in people if norm_text(p.get('name')).startswith(n)]

    def role_relevance(self, pid, hint_words):
        p = self.get(pid)
        hay = ' '.join([p.get('role') or '', p.get('team') or '']).lower()
        score = 0
        for w in hint_words or ():
            for h in FUNCTION_ROLE_HINTS.get(w, ()):
                if h in hay:
                    score += 1
                    break
        return score

    def resolve(self, name, org=None, hint_words=None):
        """Best person id for a name, using role relevance to break ties (two people, one first name)."""
        cands = self.candidates(name, org)
        if len(cands) <= 1:
            return cands[0] if cands else None
        scored = sorted(((self.role_relevance(c, hint_words), c) for c in cands), reverse=True)
        if scored[0][0] > scored[1][0]:
            return scored[0][1]
        return None

    def mentions(self, text, org=None):
        """[(name_as_written, [candidate ids])] for people named in text (full names first)."""
        out, used = [], []
        people = [p for p in self.by_id.values() if org is None or p['org'] == org]
        for p in sorted(people, key=lambda q: -len(q.get('name') or '')):
            nm = p.get('name') or ''
            if not nm:
                continue
            m = re.search(r'(?<![A-Za-z])' + re.escape(nm) + r'(?![A-Za-z])', text or '', re.I)
            if m and all(m.end() <= a or m.start() >= b for a, b in used):
                out.append((m.group(0), [p['id']]))
                used.append(m.span())
        firsts = {}
        for p in people:
            f = (p.get('name') or '').split()
            if f:
                firsts.setdefault(f[0].lower(), []).append(p['id'])
        for m in re.finditer(r'(?<![A-Za-z])([A-Z][a-z]+)(?![A-Za-z])', text or ''):
            if not all(m.end() <= a or m.start() >= b for a, b in used):
                continue
            ids = firsts.get(m.group(1).lower())
            if ids:
                out.append((m.group(1), list(ids)))
                used.append(m.span())
        return out


class Clients(object):
    def __init__(self, path=None):
        raw = read_json(path or data_path('clients.json'), []) or []
        self.items = [c for c in raw if isinstance(c, dict) and c.get('id')]
        self.by_id = {c['id']: c for c in self.items}

    def name(self, cid):
        return (self.by_id.get(cid) or {}).get('name') or cid or ''
