# -*- coding: utf-8 -*-
"""Retrieval fallback for vague free-text questions. Runs only after every rule-based flow failed.

  1. lexical   SQLite FTS5 (BM25, stemming) over the records that may ever be cited, with the
               person's visibility gate applied inside the query (Store.search). Without FTS5 a scan
               over the visible records does the same job, slower.
  2. dense     the top few candidates plus the question are embedded and the two rankings are fused
               (reciprocal rank fusion). Only candidates are embedded, never the whole corpus, and the
               call shares the command's time cap and token budget. No key, no budget or any failure:
               the lexical order stands.
  3. answer    the model reads the best few snippets and answers from them only, naming its sources;
               the sources must be among the snippets. With no model, an extractive answer is given
               only when one sentence covers most of the question's words.

Nothing here can widen what a person may see: candidates come from Store.search / the Visible map,
and each one is checked again by the guard (citable, not forgotten, not suppressed).
"""
import array
import base64
import hashlib
import math

from .common import content_words, fmt_dm, local_date, parse_ts, record_text, sentences, words
from .llm import data_block
from .store import CITABLE_KINDS

CANDIDATE_TYPES = ('chat', 'mail', 'note', 'doc', 'sheet')  # tasks and events have their own flows
K_LEXICAL = 40
K_RERANK = 16
RRF_K = 60
EMBED_TEXT_CHARS = 240
ANSWER_SNIPPETS = 4
ANSWER_SNIPPET_CHARS = 500
MAX_TERMS = 12
NEVER_ECHO = ('at risk', 'approved by client', 'client approved', 'approved by the client')
QUESTION_FILLER = set('say said says tell told know knew news update updates anything something thing things '
                      'about around mention mentioned remind reminded help need want find look check show give '
                      'like much many get got going'.split())


def stem(w):
    w = w.lower()
    for suf in ('ing', 'ed', 'es', 's'):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def query_terms(ctx):
    """The question's content words, minus the client's own name and question filler."""
    drop = set(QUESTION_FILLER)
    if ctx.client['phrase']:
        drop.update(words(ctx.client['phrase']))
    for c in ctx.clients.items:
        drop.update(words(c.get('name')))
        drop.add(c['id'].lower())
    out = []
    for w in content_words(ctx.q):
        if w not in drop and w not in out:
            out.append(w)
    return out[:MAX_TERMS]


def focus(text, terms, limit):
    """The sentences that mention the terms (else the start of the text), cut to `limit` characters."""
    want = set(stem(t) for t in terms)
    picked = [s for s in sentences(text) if want & set(stem(w) for w in words(s))]
    out = ' '.join(picked) if picked else ' '.join((text or '').split())
    return out[:limit]


# ------------------------------------------------------------------ 1. lexical
def lexical(ctx, terms):
    """Visible, citable candidate records for the terms, best first."""
    ids = ctx.store.search(ctx.person, ctx.last, ctx.at_iso, terms, K_LEXICAL) if ctx.store.fts else []
    if ctx.store.fts:
        recs = [ctx.vis[i] for i in ids if i in ctx.vis]
    else:  # no FTS5 in this SQLite: score the visible records in Python
        want = set(stem(t) for t in terms)
        scored = []
        for n, r in enumerate(ctx.vis.of_type(*CANDIDATE_TYPES, kinds=CITABLE_KINDS)):
            hits = len(want & set(stem(w) for w in words(record_text(r))))
            if hits:
                scored.append((-hits, n, r))
        scored.sort(key=lambda x: (x[0], x[1]))
        recs = [r for _, _, r in scored[:K_LEXICAL]]
    return [r for r in recs if r.get('type') in CANDIDATE_TYPES and ctx.guard.citable(r['id'])]


# ------------------------------------------------------------------ 2. dense
def _encode(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return base64.b64encode(array.array('f', [x / norm for x in vec]).tobytes()).decode('ascii')


def _decode(s):
    arr = array.array('f')
    arr.frombytes(base64.b64decode(s))
    return arr


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))  # both are unit length


def rerank(ctx, recs, terms):
    """Fuse the lexical order with embedding similarity over the top candidates. Returns the new
    order, or the input order unchanged when embeddings are unavailable."""
    llm = ctx.llm
    top = recs[:K_RERANK]
    if not llm.enabled or len(top) < 2:
        return recs
    texts = [focus(record_text(r), terms, EMBED_TEXT_CHARS) for r in top]
    model = llm.embed_model()

    def key(item):  # the same text embeds differently as a query and as a document
        return hashlib.sha256(('%s|%s|%s|%s' % (llm.provider, model, item[1], item[0])).encode('utf-8')).hexdigest()
    need = [(ctx.q, 'query')] + [(t, 'document') for t in texts]
    vecs = {}
    for item in need:
        cached = ctx.store.emb_get(key(item))
        if cached:
            vecs[item] = _decode(cached)
    missing = [item for item in dict.fromkeys(need) if item not in vecs]
    if missing:
        got = llm.embed([t for t, _ in missing], kinds=[k for _, k in missing])
        if not got:
            return recs
        for item, v in zip(missing, got):
            enc = _encode(v)
            ctx.store.emb_put(key(item), enc)
            vecs[item] = _decode(enc)
    q = vecs[(ctx.q, 'query')]
    dense = sorted(range(len(top)), key=lambda i: (-_cos(q, vecs[(texts[i], 'document')]), i))
    dense_rank = {i: n for n, i in enumerate(dense)}
    fused = sorted(range(len(top)), key=lambda i: (-(1.0 / (RRF_K + i) + 1.0 / (RRF_K + dense_rank[i])), i))
    return [top[i] for i in fused] + recs[K_RERANK:]


def candidates(ctx, terms):
    recs = lexical(ctx, terms)
    return rerank(ctx, recs, terms) if recs else []


# ------------------------------------------------------------------ 3. answer
ANSWER_SCHEMA = {
    'type': 'object',
    'properties': {'answerable': {'type': 'boolean'}, 'answer': {'type': 'string'},
                   'source_ids': {'type': 'array', 'items': {'type': 'string'}}},
    'required': ['answerable', 'answer', 'source_ids'],
}


def grounded(ctx, recs, terms):
    """The model answers from the best snippets only. None unless it names real sources among them."""
    if not ctx.llm.enabled or ctx.llm.exhausted:
        return None
    shown = recs[:ANSWER_SNIPPETS]
    items = []
    for r in shown:
        when = parse_ts(r.get('at'))
        items.append({'id': r['id'], 'author': ctx.people.name(r.get('author')),
                      'date': fmt_dm(local_date(when, ctx.zone)) if when else '',
                      'text': focus(record_text(r), terms, ANSWER_SNIPPET_CHARS)})
    prompt = ('Answer the question using ONLY the records below. If they do not answer it, set answerable '
              'to false. Otherwise write one or two plain sentences, keep names and dates as written in the '
              'records, and list the ids of the records you used.\n'
              + data_block({'question': ctx.q, 'records': items}))
    res = ctx.llm.json_call('search-answer', prompt, ANSWER_SCHEMA, max_output=300)
    if not res or not res.get('answerable'):
        return None
    ids = [i for i in res.get('source_ids') or [] if i in {r['id'] for r in shown}]
    text = ' '.join(str(res.get('answer') or '').split())[:400]
    if not ids or not text:
        return None
    return text, ids


def extractive(ctx, recs, terms):
    """One sentence that covers most of the question's words, quoted with its author and date."""
    want = [stem(t) for t in terms]
    need = max(2, int(math.ceil(0.6 * len(want))))
    best = None
    for rank, r in enumerate(recs[:ANSWER_SNIPPETS]):
        for s in sentences(record_text(r)):
            have = set(stem(w) for w in words(s))
            cover = sum(1 for t in want if t in have)
            if cover >= need and (best is None or (cover, -rank) > (best[0], -best[1])):
                best = (cover, rank, r, s)
    if not best:
        return None
    r, s = best[2], best[3]
    when = parse_ts(r.get('at'))
    text = 'On %s, %s wrote: "%s"' % (fmt_dm(local_date(when, ctx.zone)) if when else '?',
                                      ctx.people.name(r.get('author')), s[:300])
    return text, [r['id']]


def answer(ctx):
    """-> (text, [source ids], 'model'|'extract') or None."""
    terms = query_terms(ctx)
    if not terms:
        return None
    recs = candidates(ctx, terms)
    if not recs:
        return None
    got = grounded(ctx, recs, terms)
    method = 'model'
    if not got:
        got, method = extractive(ctx, recs, terms), 'extract'
    if not got:
        return None
    # A retrieved message may itself say "X is AT RISK" (or be an injected order to say so). Whatever
    # its source, such a status claim is never repeated: it would be a status no task record states.
    if any(p in got[0].lower() for p in NEVER_ECHO):
        return None
    return got[0], got[1], method
