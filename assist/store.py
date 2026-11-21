# -*- coding: utf-8 -*-
"""SQLite store.

visible(person, cutoff) lives here and only here. At query time every record, and every fact
derived from a record, is read through it: facts are filtered by whether their source record is
in the person's visible set, so nothing derived can bypass the gate.
"""
import collections
import json
import os
import sqlite3

from .common import People, log

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY, day INTEGER, seq INTEGER, org TEXT, type TEXT, at TEXT, author TEXT,
    client TEXT, visibility TEXT, readers TEXT, kind TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS records_org_day ON records (org, day);
CREATE TABLE IF NOT EXISTS facts (
    fid INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, rid TEXT, day INTEGER, at TEXT,
    client TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS facts_kind ON facts (kind);
CREATE TABLE IF NOT EXISTS memory (
    person TEXT, kind TEXT, key TEXT, data TEXT, day INTEGER, PRIMARY KEY (person, kind, key));
CREATE TABLE IF NOT EXISTS seen (k TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS outputs (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS llm_cache (k TEXT PRIMARY KEY, v TEXT);
'''
TABLES = ['meta', 'records', 'facts', 'memory', 'seen', 'outputs', 'llm_cache']

# Record kinds that may ever be cited: staff messages/docs/tasks and client-tagged external
# messages. Notifications and untagged external mail (newsletters, vendors, injections) never.
CITABLE_KINDS = ('staff', 'client_external')


def store_dir():
    d = os.environ.get('R3_STORE') or os.path.join(os.getcwd(), '.assist_store')
    os.makedirs(d, exist_ok=True)
    return d


class Store(object):
    def __init__(self, people=None):
        self.dir = store_dir()
        self.path = os.path.join(self.dir, 'assist.db')
        self.usage_path = os.path.join(self.dir, 'usage.jsonl')
        self.db = sqlite3.connect(self.path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.people = people if people is not None else People()
        self._fact_rows = None

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
        try:
            os.remove(self.usage_path)
        except OSError:
            pass
        self._fact_rows = None

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
        self.db.execute(
            'INSERT OR REPLACE INTO records (id, day, seq, org, type, at, author, client, visibility,'
            ' readers, kind, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (r['id'], day, seq, r.get('org'), r.get('type'), at, r.get('author'), r.get('client'),
             (r.get('visibility') or 'team'), json.dumps(readers), kind, json.dumps(r)))

    def ingest_rows(self, where='1=1', args=()):
        """Raw rows for ingest-time diffing (previous doc versions). Not used at query time."""
        out = []
        for row in self.db.execute('SELECT * FROM records WHERE %s ORDER BY at, day, seq' % where, args):
            rec = json.loads(row['raw'])
            rec['_day'], rec['_seq'], rec['_kind'], rec['at'] = row['day'], row['seq'], row['kind'], row['at']
            out.append(rec)
        return out

    # ------------------------------------------------------------- THE visibility gate
    def visible(self, person, cutoff_day, cutoff_at=None):
        """Records `person` may see, up to the cutoff, as an ordered {id: record} map.

        Same org only; team records, private records the person wrote, restricted records that
        list the person in `readers`; only days already ingested (<= cutoff_day) and, for asks,
        nothing after the moment of the question (cutoff_at, ISO UTC).
        """
        out = collections.OrderedDict()
        org = self.people.org_of(person)
        if not org:
            return out
        q = 'SELECT * FROM records WHERE org=? AND day<=?'
        args = [org, int(cutoff_day)]
        if cutoff_at:
            q += ' AND at<=?'
            args.append(cutoff_at)
        q += ' ORDER BY at, day, seq'
        for row in self.db.execute(q, args):
            vis = (row['visibility'] or 'team').lower()
            author = row['author']
            try:
                readers = json.loads(row['readers'] or '[]') or []
            except ValueError:
                readers = []
            if vis == 'team':
                ok = True
            elif vis == 'private':
                ok = author == person
            elif vis == 'restricted':
                ok = person in readers
            else:  # unknown visibility: be strict
                ok = author == person or person in readers
            if not ok:
                continue
            rec = json.loads(row['raw'])
            rec['_day'], rec['_seq'], rec['_kind'], rec['at'] = row['day'], row['seq'], row['kind'], row['at']
            out[rec['id']] = rec
        return out

    def hidden_from(self, person, cutoff_day, cutoff_at=None):
        """Own-org records that exist but `person` may NOT see (private to others, restricted).

        Used only to decide whether to refuse; their content is never returned to the asker.
        """
        org = self.people.org_of(person)
        if not org:
            return []
        q = 'SELECT * FROM records WHERE org=? AND day<=?'
        args = [org, int(cutoff_day)]
        if cutoff_at:
            q += ' AND at<=?'
            args.append(cutoff_at)
        vis_ids = set(self.visible(person, cutoff_day, cutoff_at))
        out = []
        for row in self.db.execute(q, args):
            if row['id'] in vis_ids:
                continue
            rec = json.loads(row['raw'])
            rec['_day'], rec['_kind'], rec['at'] = row['day'], row['kind'], row['at']
            out.append(rec)
        return out

    # ------------------------------------------------------------- facts
    def add_fact(self, kind, rid, day, at, client, data):
        self.db.execute('INSERT INTO facts (kind, rid, day, at, client, data) VALUES (?,?,?,?,?,?)',
                        (kind, rid, day, at, client, json.dumps(data)))
        self._fact_rows = None

    def facts(self, kinds, vis):
        """Facts of the given kinds whose source record is in the visible map `vis`."""
        if self._fact_rows is None:
            self._fact_rows = self.db.execute('SELECT * FROM facts ORDER BY at, fid').fetchall()
        out = []
        for row in self._fact_rows:
            if kinds and row['kind'] not in kinds:
                continue
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

    def log_usage(self, entry):
        try:
            with open(self.usage_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry) + '\n')
        except OSError as e:
            log('could not write usage log:', e)
