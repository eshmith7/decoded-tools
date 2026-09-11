# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`topic-radar` finds topics worth making a Decoded with be10X video about, by
measuring what has already worked on 49 comparable YouTube channels. It is a
measuring instrument, not a search tool: the numbers that matter can only be
derived from a record kept over time.

`decoded-tools` is a monorepo; `topic-radar/` is the first tool in it.

## Commands

```bash
cd topic-radar
pip install -r requirements.txt

# First-time local setup. Every script defaults to $DATABASE_URL, so always
# pass --dsn locally or you will hit production.
sqlite3 local.db < db/schema_sqlite.sql
DSN="sqlite:///$PWD/local.db"

python3 poller/poll.py        --dsn "$DSN" --verbose   # RSS poll: videos + snapshots
python3 poller/backfill.py    --dsn "$DSN" --tier 1    # catalogues + baselines (slow)
python3 poller/classify.py    --dsn "$DSN"             # dictionary topic matching
python3 poller/classify_llm.py --dsn "$DSN" --limit 400 # LLM pass over the rest
python3 poller/news.py        --dsn "$DSN"             # news -> triggers
python3 poller/score.py       --dsn "$DSN" --markdown  # the shortlist
python3 poller/publish.py     --dsn "$DSN"             # store it for the app

python3 tests/test_pg_types.py    # dialect guards; no database needed
python3 tests/lint_aliases.py     # alias safety; needs a system word list
node app/render.test.mjs          # app rendering helpers

cd app && python3 -m http.server 8000   # app on the committed fixture
```

`classify_llm.py` and `news.py` need `GEMINI_API_KEY` or `OPENAI_API_KEY`
(OpenAI wins if both are set). Gemini's free tier is 1500 requests/day and is
easy to exhaust while iterating.

`--dry-run` on `poll.py` writes data but does not advance `last_polled`.
`--only` and `--tier` on `backfill.py` narrow a run to a few channels.

## Architecture

Five stages, each a separate entry point writing to a shared Postgres
(Supabase in production, SQLite locally). Nothing runs on a developer's
machine in production; GitHub Actions drives everything.

```
YouTube RSS ─poll.py──────► videos + snapshots ─┐
YouTube browse ─backfill.py─► durations + baselines ─┤
                                                     ├─► score.py ─► publish.py ─► app/
news RSS ─news.py───────────► news_items + triggers ─┤
titles ─classify(_llm).py───► topics + video_topics ─┘
```

**Data sources are unauthenticated.** `poller/youtube.py` uses channel RSS
(exact publish timestamps and live counts, last 15 uploads), the innertube
browse endpoint (catalogues with durations), and innertube search. The
official Data API is a worse fit: ~100 searches a day at 100 units each.

**Snapshots are the compounding asset.** A view count is a running total, so
history before we start watching is unrecoverable. `snapshots` records views
per video per poll, and velocity is the delta *between consecutive snapshots*,
never since publish — an average since publish lags and hides a topic that has
already peaked. Only videos under 30 days old are snapshotted, which keeps the
table inside Supabase's 500MB free tier.

**Everything is relative to a channel's own baseline.** `mult` is a multiple of
that channel's median long-form views. Raw views only measure channel size:
Decoded's best video is 6.5x its own median at 6K views. The 8-minute cut
(`MIN_LONGFORM_S`) is what keeps shorts and news clips out of the median.

**Topics are entity-level.** "BlackRock" is one topic; framings hang off it.
`poller/topics.yml` is curated and `classify_llm.py` appends what it discovers.
Lexical extraction was tried twice and cannot do this — ranking title tokens by
frequency surfaces creator names above every real entity, and matching bare
capitalised words invents topics like "north" and "pros".

**Scoring is a gate, not a ranked feed** (`poller/score.py`). Failing a
condition is a rejection, not a deduction. This comes from how topics behave:
Nokia (evergreen, no trigger) decayed 13.2M → 53K; Jet Airways (trigger, no
evergreen depth) clustered once and died; Mallya and Byju's were deep topics
that news re-lit and grew for late entrants. So a winner needs proven demand
*and* a fresh trigger.

**English and Hindi demand are stored and scored separately.** Measured
correlation across 34 topics covered in both is r=0.23. Decoded publishes in
English, so English evidence decides and Hindi is weak support. Pooling them
would actively mislead.

## Things that have broken, and the rules that came from them

**Postgres and SQLite disagree, and local runs cannot see it.** Two bugs
reached production this way. Both are covered by `tests/test_pg_types.py`; add
to it whenever you touch a DB type.
- Postgres has `round(numeric, int)` but no `round(double precision, int)`.
  Cast to `numeric`.
- SQLite returns timestamps as ISO strings, Postgres as `datetime`. Every
  value read back goes through `store.as_utc()`. The only `fromisoformat` that
  belongs outside `store.py` is the one parsing RSS XML.

**Write in batches.** The database is in another region and one statement is
one round trip. Unbatched writes took a poll to 6m44s and killed the backfill
at its 55-minute timeout. Use `executemany` via `store.upsert_videos` /
`add_snapshots` / `upsert_triggers`.

**`print(..., flush=True)` for progress.** Python block-buffers stdout when it
is not a terminal, so a cancelled Actions run logs nothing at all.

**Aliases that are everyday words poison a topic's figures.** `boat` matched
"22 migrants rescued from disabled boat near Tripoli". `UNSAFE_ALIASES` in
`classify.py` rejects them at load; `tests/lint_aliases.py` checks the whole
file. Word boundaries already handle substrings, so short acronyms (GST, UPI,
IPO) are fine — being an ordinary word is the hazard, not being short.

**Channel identity is verified, never assumed.** `@DrVivekBindra`,
`@CARahulMalodia` and `@sandeepmaheshwari` resolve to squatters with
double-digit subscriber counts, and `@TheCompanyMan` resolves to a
393k-subscriber hip-hop channel sharing the name with the business explainer —
it passed both a size and a name check. `verify_channels.py` therefore also
samples titles for topical fit. Run it before adding anyone to `channels.yml`.

**Adjudication fails closed.** A trigger must carry the adjudicator's one-line
summary of what happened (`event`). Without it, it is a keyword match and
scoring ignores it. When the LLM quota ran out, failing open marked all 45
candidates confirmed and let a feature story through as a policy trigger.

**Creator names are not topics.** Channels put their own names in their titles.
`classify_llm.py` rejects a proposed topic whose name appears almost only in
its own channel's videos — but concentration, not mere collision, because
Zerodha and Groww are channels we track *and* real companies.

## Deployment

GitHub Actions on cron, reading `DATABASE_URL` (and the LLM key) from repository
secrets. `topic-radar/deploy/` holds canonical copies of the workflow files and
documents them; `app/README.md` documents the Cloudflare Pages setup.

| Workflow | Schedule |
|---|---|
| `poll` | every 30 min, 01:00–19:30 UTC |
| `news` | every 3 hours |
| `shortlist` | daily, plus on demand |
| `backfill` | weekly (Sunday) |
| `classify` | weekly (Sunday) |

GitHub throttles cron heavily — roughly one run per 4.6 hours against a
30-minute request. This costs nothing: the fastest-posting channel fills its
15-item RSS window in 26 hours.

**Connect via Supabase's pooler, not `db.<ref>.supabase.co`** — the direct host
is IPv6-only on new projects and Actions runners are IPv4. The pooler runs
pgbouncer in transaction mode, so `store.py` opens connections with
`prepare_threshold=None`; without it psycopg3's automatic prepare fails after
five statements.

`db/schema.sql` is idempotent and is pasted into the Supabase SQL editor by
hand, so the database password never has to be shared. `db/schema_sqlite.sql`
mirrors it for local development and must be kept in step manually.

## Writing code here

Match the existing style: module docstrings explain *why* the thing exists and
what it is for, and comments record the evidence behind a decision rather than
restating the code. Commit messages do the same — they carry the reasoning and
the measurement, not just the change.

The scoring rules live in Python and are not reimplemented in JavaScript. The
app reads rows that `publish.py` wrote; forking the gate into a second language
guarantees the two drift.
