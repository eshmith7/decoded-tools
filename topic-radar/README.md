# Topic Radar

Finds topics worth making a Decoded video on, ranked by evidence rather than
instinct, and hands the lead a shortlist with the working shown.

## Why this shape

The brief was originally a latest-news topic finder: read the news on demand,
shortlist what is fresh. Looking at the actual performance data changed the
design, in four steps.

**Think School was not winning on speed.** Its biggest videos are evergreen —
India vs China military strategy (14M), OPEC (5.1M), Marlboro's marketing
(4.4M), the UK economy (4M). None had a news peg. Its genuinely news-pegged
videos sit at 300–450K against a channel median of 810K. Being fast on news is
not what built that channel, so matching its turnaround was the wrong target.

**"Already covered" is a demand signal, not a disqualifier.** BlackRock had
been done by at least six channels, one at 4.4M, before Think School published
its own in August 2026 — and got 1.8M anyway. Same pattern on NEOM and Air
India. Prior coverage is evidence that an audience wants the topic.

**But coverage alone does not carry it.** Topics move in three shapes:

| Topic | Shape |
|---|---|
| Vijay Mallya | 3.1M → 22.9M → 31M / 15.3M — grew for late entrants |
| Byju's | 1.8M → 10.1M — grew for late entrants |
| Nokia | 13.2M → 1.2M → 194K → 53K — decayed to nothing |
| Jet Airways | one cluster at the news event, then silence |

Nokia is evergreen with no trigger and decays. Jet Airways was a trigger with
no evergreen depth and died in one cluster. Mallya and Byju's were deep
evergreen stories that fresh news re-lit, and late entrants ate.

**So the winner is an evergreen topic with proven demand, re-triggered by
fresh news.** Not evergreen alone, not news alone. That makes scoring a gate
rather than a weighted feed: missing proven demand or missing a fresh trigger
is a rejection, not a low rank.

This also re-explains Decoded's weakest video. Its OpenAI piece landed two
weeks after Think School's, with nearly the same title and the same number in
it, and did 936 views. OpenAI has no deep evergreen demand history — the
company is too new — and the angle was already taken. Both legs missing.

## What the data says about language

Decoded publishes in English, but Hindi channels are worth mining as demand
evidence. How much they are worth is measurable, and the answer is: less than
you would hope. Across 34 topics covered in both languages, the correlation
between a topic's Hindi and English performance is **r = 0.23**.

| Strong in Hindi, weak in English | Strong in English, weak in Hindi |
|---|---|
| Japan 3.5x → 1.1x | Saudi/NEOM 1.1x → 1.6x |
| Zomato 2.5x → 0.9x | Bangladesh 0.7x → 1.4x |
| Tata 2.4x → 1.1x | Germany 0.8x → 1.3x |

So the two are scored separately and English evidence is weighted higher.
Pooling them and ranking would actively mislead. (Directional, not final —
several topics have small samples, and language is tagged per channel rather
than per video.)

Two negative signals fell out of the same pass and are worth keeping: Zomato
has 36 English videos averaging 0.87x — saturated. Meta/Facebook (0.33x) and
crypto (0.59x) are dead.

## Architecture

Everything runs on free tiers.

| Layer | Choice | Why |
|---|---|---|
| Database + auth | Supabase | 500MB Postgres, 50K MAU covers a 20+ person team |
| Pollers | GitHub Actions cron | 2000 min/month; the pattern Pratik's aggregator already proves |
| App | Cloudflare Pages + Workers | free and, unlike Vercel Hobby, licensed for commercial use |
| Classification | Gemini Flash free tier | 1500 req/day, with a dictionary handling the common cases |

### Data sources

No API key anywhere. The official YouTube Data API is a worse fit — 10K units
a day, and search costs 100 units a call, so about 100 searches a day.

- **Channel RSS** — `feeds/videos.xml?channel_id=…`. Exact publish timestamps
  and live view/like counts, last 15 uploads. The freshest thing YouTube
  exposes and the only place with a real timestamp instead of "4 days ago".
- **Innertube browse** — full back catalogues, with durations.
- **Innertube search** — who else covered a topic, when, how it performed.

### Snapshots are the asset

One poll gives views-since-publish. Two give acceleration, which is the only
way to tell a topic still climbing from one that has peaked. Views are a
running total, so history before we start watching is unrecoverable — which is
why the poller was built and deployed first.

Storage is the constraint on a 500MB tier: snapshotting everything every 30
minutes would be ~175M rows a year. Only videos under 30 days old are tracked
at full resolution — about 100 at a time across the panel, roughly 90MB a year.

### Everything is relative to a channel's own baseline

Raw views only measure channel size. Decoded's Rameshwaram Cafe video is 6.5x
its own median at 6K views; a raw ranking buries it. So every performance
figure is a multiple of that channel's median long-form views, and the
8-minute duration cut is what keeps news clips and shorts out of the median.

## Layout

```
db/schema.sql          Postgres (Supabase) schema
db/schema_sqlite.sql   local dev mirror
poller/youtube.py      RSS + innertube client, no API key
poller/channels.yml    the 49-channel registry, tiered and language-tagged
poller/verify_channels.py  subscriber-floor check before a channel is trusted
poller/store.py        storage adapter: Postgres in prod, SQLite locally
poller/poll.py         30-minute RSS poll; writes videos + snapshots
poller/backfill.py     weekly catalogue crawl; fills durations and baselines
poller/news.py         news feeds -> triggers; the "why now" leg of the gate
poller/score.py        the gate; ranks surviving topics into a shortlist
.github/workflows/     the two cron jobs
```

## Running locally

```bash
pip install -r requirements.txt
sqlite3 local.db < db/schema_sqlite.sql
python poller/poll.py --dsn "sqlite:///$PWD/local.db" --verbose
python poller/backfill.py --dsn "sqlite:///$PWD/local.db" --tier 1
```

Against Supabase, set `DATABASE_URL` and drop the `--dsn` flag.

## A note on the channel registry

Handles are not trustworthy. `@DrVivekBindra` resolves to a 127-subscriber
account, `@CARahulMalodia` to one with 2, `@sandeepmaheshwari` to 166 —
squatters hold the names. Every id in `channels.yml` was verified against a
subscriber floor, and `verify_channels.py` finds the real channel by searching
for who actually publishes the content. Run it before adding anyone.

## Deploying

### 1. Apply the schema

In the Supabase dashboard, open **SQL Editor**, paste the whole of
`db/schema.sql`, and run it. It is idempotent, so re-running is safe.

Doing it here rather than from a script is deliberate: it means the database
password never has to be shared or pasted anywhere.

### 2. Get a pooler connection string

**Use the connection pooler, not the direct host.** Supabase's direct endpoint
(`db.<ref>.supabase.co:5432`) is IPv6-only on new projects and GitHub Actions
runners are IPv4, so a direct URL fails to connect with no useful error.

In the dashboard: **Project Settings → Database → Connection string →
Connection pooling**. It looks like

```
postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
```

The pooler runs pgbouncer in transaction mode, which is why `store.py` opens
connections with `prepare_threshold=None` — see the comment there.

### 3. Store it as a repository secret

**Settings → Secrets and variables → Actions → New repository secret**, named
`DATABASE_URL`. Secrets are not readable from workflow logs or by forks, and
never appear in the repository.

### 4. First run

Trigger **backfill** manually first (Actions → backfill → Run workflow) so
channel baselines exist, then **poll**. After that both run on their crons:
poll every 30 minutes, backfill weekly.

## The trigger layer

Demand says a topic can work. A trigger says it is worth making *now*. The
evidence for needing both is in the shapes above: Nokia had demand and no
trigger and decayed to nothing; Jet Airways had a trigger and no evergreen
depth and died in one cluster.

Stories come from twelve wire feeds (Indian business press, plus global wires
because India-relevant stories often break there first) and from Google News
queried per topic, so we hear about the topics we care about rather than only
what the wires lead with. Only topics that could actually clear the gate are
queried — the request budget should not be spent on subjects that are dead or
saturated.

What counts as a trigger is narrow on purpose, in two stages:

1. **Keywords**, tuned for recall. Event phrases (`collapse`, `SEBI`,
   `acquires`, `steps down`, `files for IPO`) against routine-coverage
   patterns (`market wrap`, `stocks to watch`, `share price target`).
2. **A reading pass**, for precision. The keyword stage alone produced a
   Volkswagen layoff story filed under Google — the word came from a tracking
   url in the summary — and matched "Mahindra" against "Kotak Mahindra Bank's
   head of commercial banking quits". An LLM confirms both that the headline
   is genuinely *about* the topic and that it describes something that
   happened, then sets `kind` and `strength`. On a recent run it cut 31
   candidates to 21 and removed exactly those errors.

Strength decays with the story's age, and `expires_at` closes the window by
kind: 45 days for a collapse, 14 for a filing. Without that, one old event
would prop a topic up forever.

Run `score.py --require-trigger` to see only topics with both legs.
