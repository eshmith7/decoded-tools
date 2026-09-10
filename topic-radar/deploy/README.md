# Scheduled jobs

These two files belong at `.github/workflows/` in the repository root. They
are kept here as well because GitHub refuses a push that creates or changes
anything under `.github/workflows/` unless the credential carries the
`workflow` scope, and the token available while building this did not.

To install them, either:

- add the `workflow` scope to the token at <https://github.com/settings/tokens>
  and copy both files to `.github/workflows/`, or
- create them through GitHub's web editor: **Add file → Create new file**,
  path `.github/workflows/poll.yml`, paste, commit. Repeat for `backfill.yml`.

Nothing runs on a schedule until they are in place.

| File | Schedule | Purpose |
|---|---|---|
| `poll.yml` | every 30 min, 01:00–19:30 UTC | RSS poll; records videos and snapshots |
| `backfill.yml` | weekly, Sunday 02:20 UTC | catalogue crawl; fills durations and baselines |
| `classify.yml` | weekly, Sunday 03:30 UTC | LLM pass over unmatched titles |

`poll.yml` and `backfill.yml` read the `DATABASE_URL` repository secret;
`classify.yml` also reads `GEMINI_API_KEY`.
