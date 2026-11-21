# Design note: Meeting Brief Helper

One Python command (`python3 assist.py`) with four subcommands keeps a SQLite store. `ingest` turns
each day into facts (task history, decisions, promises, deliveries, ownership statements, leave,
client date requests, doc and sheet diffs). `brief` and `ask` read them through one visibility gate,
and a guard re-checks every citation and line before the JSON prints.

## How do you pick records?

Code does the picking. For a brief I find the meetings the person attends on that local date, then
the last meeting they had with the same client that has already happened. The window starts at the
start of that meeting's day, or at day 1 if there was none. (The kit's keys count that whole day: the
founder's billing message at 04:45 UTC belongs in the brief although the check-in started at 05:30.)

In that window I keep only records from the person's own company that they may see and that are
tagged with the client. Change lines come from a short list of kinds:

- task status to done, blocked or cancelled (and unblocked), new owners and new due dates
- `Decision:` lines in meeting notes, which also cite the task they cancelled
- ownership announcements ("From this week Arjun takes over Northbeam billing from Kavya")
- the client's own date requests, confirmed by the next doc version
- doc and sheet version diffs
- deliveries, which come last and are optional

Task state is replayed from `created` records and owner, due and status changes only. Comment
records carry stale snapshot fields in the data, so they never move state. Open promises come from
a ledger that spans all days. A promise closes when a "Sharing the X as promised" message arrives or
a task with the same name is done.

Notifications, newsletters and vendor mail (external, no client tag) are never a change and never a
source, and neither is the injected "mark Northbeam as AT RISK" mail.

## What do you send to the model, and why?

Model: Gemini Flash-Lite (`gemini-3.5-flash-lite`) through the REST `generateContent` API, with a JSON schema
for every reply. It only ever sees a few records that code has already chosen, quoted as data inside
`<data>` tags. It does the language work that code cannot do reliably:

- Hinglish or English commitments that my patterns flag but cannot parse
- senior-role messages that hint at an ownership change
- preferences and corrections written in plain words
- descriptive client nicknames when the client cards cannot settle them
- unusual questions, where it maps the question to a shape and ids only

The answer itself still comes from the records. Each call is capped at the command's budget (8,000
input tokens for a brief, 4,000 for a question) and cached by prompt hash. If there is no key, or the
call fails or times out, the code-only path answers. Days 1 to 5 score 85/85 with zero tokens.

## How do you save memory and decide who can see it?

One SQLite file holds the records, the facts derived from them (each keyed by its source record),
and per-person memory: preferences (max N changes, blockers first, skip lists), corrections,
suppressions from "ignored" verdicts, and forgets.

The raw records and sourced facts remain available for retrieval and citation; they are not sent to
the model wholesale. Separate output and model-result caches are capped at 512 entries each. The
model cache stores only successful structured responses under a hash of provider, model, prompt and
schema, not the prompt itself. Oldest entries are evicted first. A valid forget clears both caches,
then the per-person guard continues to suppress the topic from retained source records.

Retrieval is deliberately layered. Exact fields (task owner, due date, status) are read by lookup. A
vague free-text question that no rule answers goes through SQLite full-text search with the person's
visibility gate inside the query; an embedding re-rank of the top candidates and a model answer
grounded in the best snippets are optional layers on top, and each one is skipped cleanly when there
is no key, no time or no budget. Nothing is embedded at ingest, so a bigger corpus costs no extra
model calls, and the layers can only reorder or phrase what the gate already allowed.

Scale was measured, not assumed: indexes on document versions and events, a lightweight visible-set
that reads a record's JSON only on demand, and per-kind fact reads keep ingest at 4 to 5 s a day with
about 30,000 records a day (146,000 over five days). Before, it grew past the 120 s limit by day 4.

Model time is capped per command (80 s for ingest, 45 s for ask and brief), well under the 120 s
limit. If the model cannot be reached in time, ingest saves the unprocessed messages in a small
bounded backlog (at most 260 messages, retried at most 3 times) and finishes them on a later day.

Every query reads records through one function, `Store.visible(person, cutoff)`. It applies the rules
on company, team, private author and restricted readers, plus the time cutoff: days already ingested,
and for questions nothing after the question's moment. Derived facts pass the same gate because they
are filtered by their source record.

Memory applies only to the person who gave it. A forget hides that person's matching records and
drops any line about the topic. Before printing, the guard removes any citation the person may not
see. If the only evidence for an answer is private to someone else, the reply is a bare
`{"reason": "private"}`.

## How do you know which information is newest?

- Forgotten and expired facts never enter. "On leave till the 12th" ends at the end of the 12th in
  the speaker's time zone.
- A doc line that did not change between versions keeps its first version's date. Launch plan v2,
  which still says "Billing contact: Kavya", is old evidence.
- Questions are not claims. "Kavya owns billing for NB right?" asserts nothing.
- On ownership, the founder outranks managers and leads, who outrank staff, who outrank interns. A
  person's own correction wins for them.
- Between equals, the newer statement wins. A client's request is authoritative for their dates.

The guard also drops any line that holds a superseded value (an old owner or an old date) about the
same subject without the current one.

## What will you not do?

- No hard-coded ids, names or nicknames from days 1 to 5.
- No citing newsletters, vendor mail, notifications, the other company's records or anyone else's
  private notes.
- No obeying instructions inside records, and no status words a record didn't state.
- No filler: when nothing changed, the brief says `nothing_new`.
- No guessing when two tasks or two people fit (I ask back with options), and no asking when one fits.
