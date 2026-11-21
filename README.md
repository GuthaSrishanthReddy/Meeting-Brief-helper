# Meeting Brief Helper (BuildSprint R3)

Before each client meeting, it writes a short brief of what changed since the person last met that
client. It also answers short questions ("Who owns billing for NB?"). Everything is cited to record
ids. People only see what they may see.

One command, standard library only:

```
python3 assist.py ingest --day N
python3 assist.py brief  --person P --date YYYY-MM-DD
python3 assist.py ask    --person P --at ISO --ask "question"
python3 assist.py forget --person P --what "words"
```

Only JSON goes to stdout and logs go to stderr. The exit code is always 0, even on bad input.

## Install

- Python 3.8 or newer. Nothing to install: `requirements.txt` is empty on purpose.
- Data is found through `$R3_PACK` (the folder that holds `data/`), else the current folder.
- The store is kept in `$R3_STORE`, else `./.assist_store/`. It holds `assist.db` (SQLite), the
  model usage log `usage.jsonl`, and the output and prompt caches. `ingest --day 1` starts it fresh.

Run the kit's harness and scorer from this folder:

```
export R3_PACK=/path/to/pack
python3 $R3_PACK/tools/run_days.py --cmd "python3 assist.py" --days 1-5 --out runs/mine
python3 $R3_PACK/tools/score.py --run runs/mine --days 1-5
python3 -m unittest discover -s tests -v
```

## Model and API

| | |
|---|---|
| Provider | Google Gemini API, REST `generateContent` |
| Network access needed | `generativelanguage.googleapis.com` only |
| Model | `gemini-3.5-flash-lite` (override with `R3_MODEL`). `gemini-3.8-flash` was tried first but returned HTTP 503 and a timeout on test calls, so it is not the default |
| Key | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), from the environment or a `.env` file next to `assist.py` (copy `.env.example`; real environment variables win; `.env` is git-ignored) |
| Settings | JSON output with a response schema, `thinkingLevel: low` |
| Code-only switch | `R3_NO_LLM=1` |

The plan asks for temperature 0. It is sent, but Gemini 3.6 Flash and newer ignore it.
Determinism comes from a prompt-hash cache instead: an identical prompt is answered from the cache
and costs no tokens.

Code does the picking; the model only reads the few records code has already chosen. It is used
for:

- commitments the patterns flag but cannot parse
- senior-role messages that hint at an ownership change
- preferences and corrections in plain words
- client nicknames the client cards cannot settle
- unusual questions, where it maps the question to a shape and ids; the answer still comes from
  the records

Record text goes in as quoted JSON inside `<data>` tags, and the instructions say it is data. If
there is no key, or a call fails or times out (25 s), the code-only path answers.

## How tokens are counted

- `usage.input_tokens` is the API's `usageMetadata.promptTokenCount`.
- `usage.output_tokens` is `candidatesTokenCount + thoughtsTokenCount`.
- `cost_usd` uses Gemini's paid-tier prices ($0.30 / $2.50 per 1M tokens for `gemini-3.5-flash-lite`).
- If one brief or answer makes several calls, they are added up.
- Calls made during `ingest` are logged to `.assist_store/usage.jsonl`, one line per call with
  purpose, tokens, cost and time.
- Before a call, a characters/4 estimate is used only to skip a call that would break the budget
  (8,000 input tokens per brief, 4,000 per question). It is never reported as usage.
- No call means 0 tokens are reported. A cache hit is also 0.

**This run:** no Gemini key was configured where it was built and scored, so every call took the
code-only path. Every output reports 0 tokens, $0 and its real time in ms, and `usage.jsonl` is
empty. With a key set, the same files show the real numbers.

## Scores for days 1 to 5 (`tools/score.py`)

| Run | Briefs /20 | Answers /15 | Honesty /15 | Memory /15 | Privacy /10 | Cost /10 | Total |
|---|---|---|---|---|---|---|---|
| Dummy baseline | 2.9 | 0.9 | 3.2 | 0.0 | 2.2 | 2.2 | 11.3 / 85 |
| This program (code-only) | 20.0 | 15.0 | 15.0 | 15.0 | 10.0 | 10.0 | **85.0 / 85** |

"Getting better" needs 6 or more days, so it applies only on the organisers' 15-day replay. The
design note (10 points) is in [DESIGN.md](DESIGN.md).

A full-day leak sweep on the kit found no leaks and no invalid output: 33 people (Kitepoint and
Brightwave), days 1 to 5, a brief and 14 kinds of question each, 2,480 calls. The slowest call took 5.8 s.

## The 3 worst outputs, and why they failed

After the fixes below, every scored item on days 1 to 5 is at 1.0. These are the three worst
outputs the program produced while it was being built.

1. **q05-02, "Send the NB deck to Arjun. Which Arjun is that?"** It answered "I have nothing on
   that" and scored 0. The which-person pattern was case-sensitive, so it missed the capital in
   "Which". Fixed: it now answers Arjun Mehta (p11), the billing executive who owns Northbeam
   billing, and cites the founder's message.
2. **Promise "the email copy for week 2"** was shortened to "the email copy for week". The Hinglish
   time pattern took the bare "2" as a date. It still scored, because the due date was right by
   luck, but the text was wrong. Fixed: a date now needs a month or an ordinal ("14 Nov", "the
   12th").
3. **`question_to_ask`: "Is everything still on track for Launch on 14 Nov?"** This was clumsy
   template wording, with the subject capitalised mid-sentence. It is now "…for the launch on 14
   Nov?". The questions are still templates built from the top blocker or date change; it earns no
   points but is checked for honesty.

## Where this departs from the plan, and why

- **Brief window.** The plan says the window runs "from the start of P's last meeting with the same
  client". The answer keys count the whole day of that meeting. The founder's 04:45 UTC billing
  message (before the 05:30 UTC check-in) and the 06:20 UTC "Ops report template is done" (before
  the 10:30 UTC call) are both in the keys. So the window starts at the start of that meeting's day.
- **Task state.** The plan says "read its latest record". In the data, comment records, and the
  other fields of change records, carry stale snapshots: UAT test plan shows `todo` after it was
  marked done. So state is replayed from `created` plus owner, due and status changes only.
- **Model provider.** The plan names no provider. Gemini Flash was chosen on request.

## Layout

```
assist.py            entry: ingest | brief | ask | forget; output cache; JSON-only, exit 0
assist/
  store.py           SQLite schema; visible(person, cutoff) lives here and only here
  ingest.py          classify, day mapping, decisions, client date requests, doc/sheet diffs
  promises.py        Hinglish/English commitment patterns, due dates, deliveries, model fallback
  owners.py          ownership statements, leave facts and expiry, precedence
  clients.py         client cards, nickname resolution, task replay and title matching
  feedback.py        preference, correction, verdict, forget (per person, idempotent)
  brief.py           meetings, window, change lines, ranking, prefs, promises, nothing_new
  ask.py             question shapes, clarify, refuse
  guard.py           citation allow-list, stale / tombstone / suppression / invented-status checks
  common.py          paths, people, clients, time zones (zoneinfo or built-in rules), dates, text
  llm.py             Gemini wrapper: budget, usage, prompt-hash cache, fallback
tests/test_assist.py made-up pack: private and restricted records, zero leaks for every person,
                     forget, leave expiry, max N, blockers first, two Priyas, injection, other org,
                     a new person mid-run, unknown zone, the JSON contract, the model wrapper
```
