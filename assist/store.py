# -*- coding: utf-8 -*-
"""SQLite store.

visible(person, cutoff) lives here and only here. At query time every record, and every fact
derived from a record, is read through it: facts are filtered by whether their source record is
in the person's visible set, so nothing derived can bypass the gate.

Built to stay fast as the data grows:
- the small columns live in `records`, the full JSON in `record_raw`, so a scan never drags every
  body along;
- visible() returns a lightweight Visible map: membership, kind, type, author and client are in
  memory, and a record's full JSON is read only when someone asks for it (or in batches, through
  of_type());
- document/sheet versions and events are found through the indexed `ref` column, not by scanning;
- facts are read per kind from an index, not all at once.
"""
import json
import os
import sqlite3

try:  # Python 3.10+
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

from .common import People, log, record_text

SEARCH_TEXT_CHARS = 6000  # indexed per record: enough for any message, and the head of a long document
MAX_EMBEDDING_CACHE_ENTRIES = 1500

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY, day INTEGER, seq INTEGER, org TEXT, type TEXT, at TEXT, author TEXT,
    client TEXT, visibility TEXT, readers TEXT, kind TEXT, ref TEXT);
CREATE INDEX IF NOT EXISTS records_gate ON records (org, day, at);
CREATE INDEX IF NOT EXISTS records_ref ON records (type, ref);
CREATE TABLE IF NOT EXISTS record_raw (id TEXT PRIMARY KEY, raw TEXT);
CREATE TABLE IF NOT EXISTS facts (
    fid INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, rid TEXT, day INTEGER, at TEXT,
    client TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS facts_kind_at ON facts (kind, at);
CREATE INDEX IF NOT EXISTS facts_rid ON facts (rid);
CREATE TABLE IF NOT EXISTS memory (
    person TEXT, kind TEXT, key TEXT, data TEXT, day INTEGER, PRIMARY KEY (person, kind, key));
CREATE TABLE IF NOT EXISTS seen (k TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS outputs (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS llm_cache (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS embeddings (k TEXT PRIMARY KEY, v TEXT);
'''
TABLES = ['meta', 'records', 'record_raw', 'facts', 'memory', 'seen', 'outputs', 'llm_cache', 'search_idx',
          'embeddings']

# Record kinds that may ever be cited: staff messages/docs/tasks and client-tagged external
# messages. Notifications and untagged external mail (newsletters, vendors, injections) never.
CITABLE_KINDS = ('staff', 'client_external')

# SQLite keeps both caches across CLI processes. Bound them so running the assistant indefinitely
# does not turn every historical brief/model interpretation into permanent local cache state.
MAX_OUTPUT_CACHE_ENTRIES = 512
MAX_LLM_CACHE_ENTRIES = 512


def store_dir():
    d = os.environ.get('R3_STORE') or os.path.join(os.getcwd(), '.assist_store')
    os.makedirs(d, exist_ok=True)
    return d


def row_allowed(visibility, author, readers_json, person):
    """THE gate for one record: may `person` see it? (Company and time cutoff are applied by the
    queries that call this.) Used by visible(), hidden_from() and search(), and nowhere else."""
    vis = (visibility or 'team').lower()
    if vis == 'team':
        return True
    if vis == 'private':
        return author == person
    try:
        readers = json.loads(readers_json or '[]') or []
    except ValueError:
        readers = []
    if vis == 'restricted':
        return person in readers
    return author == person or person in readers  # unknown visibility: be strict


class Visible(Mapping):
    """The records one person may see up to a cutoff, in time order, as a read-only {id: record} map.

    Only light columns are held in memory. `vis[rid]` reads that record's JSON on first use;
    of_type() reads many in batches. Everything that worked with the old dict still works.
    """

    def __init__(self, store, rows):
        self._store = store
        self._order = [r[0] for r in rows]
        self._meta = {r[0]: r for r in rows}  # (id, day, seq, kind, at, type, author, client)
        self._cache = {}

    def __len__(self):
        return len(self._order)

    def __iter__(self):
        return iter(self._order)

    def __contains__(self, rid):
        return rid in self._meta

    def __getitem__(self, rid):
        rec = self._cache.get(rid)
        if rec is not None:
            return rec
        if rid not in self._meta:
            raise KeyError(rid)
        self._load([rid])
        return self._cache[rid]

    def _load(self, ids):
        missing = [i for i in ids if i not in self._cache]
        for start in range(0, len(missing), 400):
            chunk = missing[start:start + 400]
            q = 'SELECT id, raw FROM record_raw WHERE id IN (%s)' % ','.join('?' * len(chunk))
            for row in self._store.db.execute(q, chunk):
                m = self._meta[row['id']]
                rec = json.loads(row['raw'])
                rec['_day'], rec['_seq'], rec['_kind'], rec['at'] = m[1], m[2], m[3], m[4]
                self._cache[row['id']] = rec

    def light(self, rtypes=None, kinds=None, author=None, client=None):
        """Yield the in-memory column tuples (id, day, seq, kind, at, type, author, client) that
        match, in time order, without reading any record JSON."""
        for rid in self._order:
            m = self._meta[rid]
            if rtypes and m[5] not in rtypes:
                continue
            if kinds and m[3] not in kinds:
                continue
            if author is not None and m[6] != author:
                continue
            if client is not None and m[7] != client:
                continue
            yield m

    def of_type(self, *rtypes, **kw):
        """Full records of the given types (and optional kind/author/client filters), time order."""
        kinds = kw.get('kinds')
        ids = [m[0] for m in self.light(rtypes or None, kinds, kw.get('author'), kw.get('client'))]
        self._load(ids)
        return [self._cache[i] for i in ids]


def records_of(vis, *rtypes):
    """Full records of the given types from a Visible (batched) or from a plain dict (tests)."""
    if isinstance(vis, Visible):
        return vis.of_type(*rtypes)
    return [r for r in vis.values() if r.get('type') in rtypes]


class Store(object):
    def __init__(self, people=None):
        self.dir = store_dir()
        self.path = os.path.join(self.dir, 'assist.db')
        self.usage_path = os.path.join(self.dir, 'usage.jsonl')
        self.db = sqlite3.connect(self.path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self._drop_if_old_schema()
        self.db.executescript(SCHEMA)
        self.fts = self._init_fts()
        self.people = people if people is not None else People()
        self._fact_cache = {}
        self.model_spent = 0.0  # seconds this command has spent waiting on the model (see llm.py)

    def _init_fts(self):
        """Full-text index over citable records (BM25 ranking, stemming). False if SQLite lacks FTS5."""
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_idx USING fts5(text, tokenize='porter unicode61')")
            return True
        except sqlite3.OperationalError as e:
            log('full-text search unavailable (%s): the scan fallback is used' % e)
            return False

    def _drop_if_old_schema(self):
        """A store made by an older version (no `ref` column) is rebuilt: `ingest --day 1` refills it."""
        cols = [r['name'] for r in self.db.execute('PRAGMA table_info(records)')]
        if cols and 'ref' not in cols:
            log('old store schema found: rebuilding it (re-run ingest from day 1)')
            for t in TABLES:
                self.db.execute('DROP TABLE IF EXISTS %s' % t)

    def commit(self):
        self.db.commit()

    def close(self):
        try:
            self.db.commit()
            self.db.close()
        except Exception:
            pass

    def reset(self):
        for t in TABLES:
            self.db.execute('DROP TABLE IF EXISTS %s' % t)
        self.db.executescript(SCHEMA)
        self.fts = self._init_fts()
        try:
            os.remove(self.usage_path)
        except OSError:
            pass
        self._fact_cache = {}

    # ------------------------------------------------------------- meta
    def meta(self, k, default=None):
        row = self.db.execute('SELECT v FROM meta WHERE k=?', (k,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row['v'])
        except ValueError:
            return default

    def set_meta(self, k, v):
        self.db.execute('INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)', (k, json.dumps(v)))

    def last_day(self):
        try:
            return int(self.meta('last_day', 0) or 0)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------- records (ingest side)
    def add_record(self, r, day, seq, kind, at):
        readers = r.get('readers') if isinstance(r.get('readers'), list) else []
        ref = r.get('doc_id') or r.get('sheet_id') or r.get('event_id')
        if self.fts:  # a re-ingested record must not leave its old text behind in the index
            old = self.db.execute('SELECT rowid FROM records WHERE id=?', (r['id'],)).fetchone()
            if old:
                self.db.execute('DELETE FROM search_idx WHERE rowid=?', (old[0],))
        cur = self.db.execute(
            'INSERT OR REPLACE INTO records (id, day, seq, org, type, at, author, client, visibility,'
            ' readers, kind, ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (r['id'], day, seq, r.get('org'), r.get('type'), at, r.get('author'), r.get('client'),
             (r.get('visibility') or 'team').lower(), json.dumps(readers), kind, ref))
        self.db.execute('INSERT OR REPLACE INTO record_raw (id, raw) VALUES (?, ?)', (r['id'], json.dumps(r)))
        if self.fts and kind in CITABLE_KINDS:  # only what may ever be cited is searchable
            text = record_text(r)[:SEARCH_TEXT_CHARS]
            if text:
                self.db.execute('INSERT INTO search_idx (rowid, text) VALUES (?, ?)', (cur.lastrowid, text))

    def _full(self, row, raw):
        rec = json.loads(raw)
        rec['_day'], rec['_seq'], rec['_kind'], rec['at'] = row['day'], row['seq'], row['kind'], row['at']
        return rec

    def record(self, rid):
        """One stored record by id, for ingest-time work (retrying model passes). Not a query path."""
        row = self.db.execute('SELECT r.day, r.seq, r.kind, r.at, w.raw FROM records r JOIN record_raw w '
                              'ON w.id = r.id WHERE r.id=?', (rid,)).fetchone()
        return self._full(row, row['raw']) if row else None

    def versions(self, rtype, ref, org=None):
        """Every stored version of one document/sheet (or every record of one event), oldest first.
        An indexed lookup by ref: ingest-time diffing, not a query path."""
        if not ref:
            return []
        q = ('SELECT r.day, r.seq, r.kind, r.at, w.raw FROM records r JOIN record_raw w ON w.id = r.id '
             'WHERE r.type=? AND r.ref=?')
        args = [rtype, ref]
        if org:
            q += ' AND r.org=?'
            args.append(org)
        return [self._full(row, row['raw']) for row in self.db.execute(q + ' ORDER BY r.at, r.day, r.seq', args)]

    # ------------------------------------------------------------- THE visibility gate
    def _scope(self, person, cutoff_day, cutoff_at, alias=''):
        org = self.people.org_of(person)
        if not org:
            return None, None, None
        where, args = '{0}org=? AND {0}day<=?'.format(alias), [org, int(cutoff_day)]
        if cutoff_at:
            where += ' AND {0}at<=?'.format(alias)
            args.append(cutoff_at)
        return org, where, args

    def visible(self, person, cutoff_day, cutoff_at=None):
        """Records `person` may see, up to the cutoff, as an ordered {id: record} map (a Visible).

        Same org only; team records, private records the person wrote, restricted records that
        list the person in `readers`; only days already ingested (<= cutoff_day) and, for asks,
        nothing after the moment of the question (cutoff_at, ISO UTC).
        """
        org, where, args = self._scope(person, cutoff_day, cutoff_at)
        if org is None:
            return Visible(self, [])
        cols = 'id, day, seq, kind, at, type, author, client'
        rows = [tuple(r) for r in self.db.execute(
            "SELECT %s FROM records WHERE %s AND visibility='team'" % (cols, where), args)]
        for r in self.db.execute(
                "SELECT %s, visibility, readers FROM records WHERE %s AND visibility!='team'" % (cols, where), args):
            if row_allowed(r['visibility'], r['author'], r['readers'], person):
                rows.append(tuple(r)[:8])
        rows.sort(key=lambda m: (m[4], m[1], m[2]))
        return Visible(self, rows)

    def search(self, person, cutoff_day, cutoff_at, terms, limit=40):
        """Record ids matching any of `terms`, best BM25 match first, that `person` may see.

        Only citable records are indexed. The company and time cutoff are applied in SQL, other
        people's private records are excluded in SQL, and the rest go through row_allowed(), the same
        gate visible() uses. Returns [] when there are no terms or no full-text index; callers then
        use their scan fallback (check `self.fts`).
        """
        terms = [t for t in dict.fromkeys(terms or []) if t]
        org, where, args = self._scope(person, cutoff_day, cutoff_at, alias='r.')
        if not self.fts or org is None or not terms:
            return []
        match = ' OR '.join('"%s"' % t.replace('"', '') for t in terms)
        q = ("SELECT r.id, r.visibility, r.author, r.readers FROM search_idx JOIN records r "
             "ON r.rowid = search_idx.rowid WHERE search_idx MATCH ? AND %s "
             "AND (r.visibility='team' OR r.author=? OR r.visibility NOT IN ('team','private')) "
             "ORDER BY bm25(search_idx), r.rowid LIMIT ?" % where)
        out = []
        try:
            rows = self.db.execute(q, [match] + args + [person, int(limit) * 3]).fetchall()
        except sqlite3.OperationalError as e:  # e.g. a query FTS5 cannot parse
            log('search failed (%s)' % e)
            return []
        for row in rows:
            if row_allowed(row['visibility'], row['author'], row['readers'], person):
                out.append(row['id'])
                if len(out) >= limit:
                    break
        return out

    def hidden_from(self, person, cutoff_day, cutoff_at=None):
        """Own-org records that exist but `person` may NOT see (private to others, restricted).

        Used only to decide whether to refuse; their content is never returned to the asker.
        """
        org, where, args = self._scope(person, cutoff_day, cutoff_at, alias='r.')
        if org is None:
            return []
        out = []
        for row in self.db.execute(
                "SELECT r.id, r.day, r.seq, r.kind, r.at, r.author, r.visibility, r.readers, w.raw FROM records r "
                "JOIN record_raw w ON w.id = r.id WHERE %s AND r.visibility!='team'" % where, args):
            if not row_allowed(row['visibility'], row['author'], row['readers'], person):
                out.append(self._full(row, row['raw']))
        return out

    # ------------------------------------------------------------- facts
    def add_fact(self, kind, rid, day, at, client, data):
        self.db.execute('INSERT INTO facts (kind, rid, day, at, client, data) VALUES (?,?,?,?,?,?)',
                        (kind, rid, day, at, client, json.dumps(data)))
        self._fact_cache = {}

    def facts(self, kinds, vis):
        """Facts of the given kinds whose source record is in the visible map `vis`."""
        key = tuple(sorted(kinds or ()))
        rows = self._fact_cache.get(key)
        if rows is None:
            if key:
                q = 'SELECT * FROM facts WHERE kind IN (%s) ORDER BY at, fid' % ','.join('?' * len(key))
                rows = self.db.execute(q, key).fetchall()
            else:
                rows = self.db.execute('SELECT * FROM facts ORDER BY at, fid').fetchall()
            self._fact_cache[key] = rows
        out = []
        for row in rows:
            if row['rid'] not in vis:
                continue
            try:
                d = json.loads(row['data'] or '{}')
            except ValueError:
                continue
            d['_fid'], d['_kind'] = row['fid'], row['kind']
            d['rid'], d['day'], d['at'], d['client'] = row['rid'], row['day'], row['at'], row['client']
            out.append(d)
        return out

    # ------------------------------------------------------------- per-person memory
    def remember(self, person, kind, key, data, day):
        self.db.execute('INSERT OR REPLACE INTO memory (person, kind, key, data, day) VALUES (?,?,?,?,?)',
                        (person, kind, key, json.dumps(data), day))

    def memories(self, person, kind):
        out = []
        for row in self.db.execute('SELECT key, data, day FROM memory WHERE person=? AND kind=? ORDER BY day, key',
                                   (person, kind)):
            try:
                d = json.loads(row['data'] or '{}')
            except ValueError:
                d = {}
            d['_key'], d['_day'] = row['key'], row['day']
            out.append(d)
        return out

    def prefs(self, person):
        out = {}
        for m in self.memories(person, 'pref'):
            out[m['_key']] = m.get('value')
        return out

    # ------------------------------------------------------------- idempotency + caches
    def seen(self, k):
        return self.db.execute('SELECT 1 FROM seen WHERE k=?', (k,)).fetchone() is not None

    def mark_seen(self, k):
        self.db.execute('INSERT OR IGNORE INTO seen (k) VALUES (?)', (k,))

    def out_get(self, k):
        row = self.db.execute('SELECT v FROM outputs WHERE k=?', (k,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row['v'])
        except ValueError:
            return None

    def out_put(self, k, v):
        self.db.execute('INSERT OR REPLACE INTO outputs (k, v) VALUES (?, ?)', (k, json.dumps(v)))
        self.db.execute(
            'DELETE FROM outputs WHERE rowid NOT IN '
            '(SELECT rowid FROM outputs ORDER BY rowid DESC LIMIT ?)',
            (MAX_OUTPUT_CACHE_ENTRIES,))

    def out_clear(self):
        self.db.execute('DELETE FROM outputs')

    def llm_get(self, k):
        row = self.db.execute('SELECT v FROM llm_cache WHERE k=?', (k,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row['v'])
        except ValueError:
            return None

    def llm_put(self, k, v):
        self.db.execute('INSERT OR REPLACE INTO llm_cache (k, v) VALUES (?, ?)', (k, json.dumps(v)))
        self.db.execute(
            'DELETE FROM llm_cache WHERE rowid NOT IN '
            '(SELECT rowid FROM llm_cache ORDER BY rowid DESC LIMIT ?)',
            (MAX_LLM_CACHE_ENTRIES,))

    def llm_clear(self):
        """Drop cached model interpretations, for example when a person asks to forget a topic."""
        self.db.execute('DELETE FROM llm_cache')
        self.db.execute('DELETE FROM embeddings')  # vectors are derived from record text: forget them too

    def emb_get(self, k):
        row = self.db.execute('SELECT v FROM embeddings WHERE k=?', (k,)).fetchone()
        return row['v'] if row else None

    def emb_put(self, k, v):
        self.db.execute('INSERT OR REPLACE INTO embeddings (k, v) VALUES (?, ?)', (k, v))
        self.db.execute(
            'DELETE FROM embeddings WHERE rowid NOT IN (SELECT rowid FROM embeddings ORDER BY rowid DESC LIMIT ?)',
            (MAX_EMBEDDING_CACHE_ENTRIES,))

    def log_usage(self, entry):
        try:
            with open(self.usage_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry) + '\n')
        except OSError as e:
            log('could not write usage log:', e)
