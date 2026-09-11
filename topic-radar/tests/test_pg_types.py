"""Guard against the dialect differences that keep breaking production.

Development runs on SQLite and deployment on Postgres, and the two disagree
about what comes back out of a query. Twice now that has shipped a bug that
every local run passed:

  * `round(double precision, int)` does not exist in Postgres, so the
    backfill's final statement failed after a successful twelve-minute crawl.
  * SQLite returns timestamps as ISO strings and Postgres as datetime
    objects, so the scorer crashed calling fromisoformat on a datetime.

These tests exercise the Postgres-shaped values without needing a Postgres.
Run with: python3 tests/test_pg_types.py
"""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "poller"))

from store import as_utc, news_uuid, topic_uuid, trigger_uuid  # noqa: E402
import news  # noqa: E402
import score  # noqa: E402

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")


def main():
    aware = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)

    print("as_utc accepts what either backend returns")
    check("postgres datetime passes through", as_utc(aware), aware)
    check("naive datetime gets UTC", as_utc(dt.datetime(2026, 9, 1, 12, 0)), aware)
    check("sqlite ISO string parses", as_utc("2026-09-01T12:00:00+00:00"), aware)
    check("naive ISO string gets UTC", as_utc("2026-09-01T12:00:00"), aware)
    check("None stays None", as_utc(None), None)
    check("junk is not fatal", as_utc("not a date"), None)

    print("\n_days_since works on both shapes")
    now = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.timezone.utc)
    check("from datetime", score._days_since(aware, now), 10.0)
    check("from string", score._days_since("2026-09-01T12:00:00+00:00", now), 10.0)
    check("from None", score._days_since(None, now), None)

    print("\nevaluate survives a topic with no English demand")
    topic = {
        "slug": "x", "label": "X", "category": "company", "state": "dead",
        "n_videos": 9, "n_channels": 3, "n_en": 0, "n_channels_en": 0,
        "demand_en": None, "demand_hi": 0.4, "demand_en_raw": None,
        "best_mult": 1.0, "evidence": [],
    }
    verdict = score.evaluate(topic, now, require_trigger=False)
    check("rejected, not crashed", verdict["eligible"], False)
    check("reason names Hindi evidence",
          "Hindi" in verdict["rejects"][0], True)

    print("\nids are stable across runs and backends")
    # Postgres defaults these to gen_random_uuid() and SQLite has no
    # equivalent, so they are derived from the thing they name instead.
    # If they were not stable, every run would duplicate every row.
    check("topic id is deterministic",
          topic_uuid("blackrock"), topic_uuid("blackrock"))
    check("different slugs differ",
          topic_uuid("blackrock") != topic_uuid("adani"), True)
    check("news id is deterministic",
          news_uuid("https://x.com/a"), news_uuid("https://x.com/a"))
    check("trigger id keys on topic and story",
          trigger_uuid("adani", "u") != trigger_uuid("tata", "u"), True)

    print("\nnews cleaning does not leak tracking urls into the text")
    # A Google News summary carries an escaped <a href> whose url contains
    # the word "google"; matching on that filed a Volkswagen layoff story
    # under the Google topic.
    escaped = ('&lt;a href="https://news.google.com/rss/articles/CBMi0AF"&gt;'
               'Volkswagen cuts jobs&lt;/a&gt;')
    check("escaped markup is stripped", news._clean(escaped),
          "Volkswagen cuts jobs")
    check("links survive their own cleaner",
          news._clean_url("https://example.com/a?b=1"),
          "https://example.com/a?b=1")

    print("\nroutine coverage is not an event")
    check("market wrap rejected",
          news.classify_event("Sensex closes higher; stocks to watch")[0], None)
    check("regulator action accepted",
          news.classify_event("SEBI fines Adani Group Rs 500 crore")[0],
          "verdict")
    check("founder exit accepted",
          news.classify_event("Byju Raveendran steps down as CEO")[0], "exit")

    print("\nan unadjudicated trigger cannot satisfy the gate")
    base = {
        "slug": "t", "label": "T", "category": "company", "state": "proven",
        "n_videos": 9, "n_channels": 4, "n_en": 5, "n_channels_en": 3,
        "demand_en": 1.8, "demand_hi": None, "demand_en_raw": 1.9,
        "best_mult": 3.0, "evidence": [],
    }
    unverified = dict(base, trigger={"kind": "policy", "strength": 0.9})
    v = score.evaluate(unverified, now, require_trigger=True)
    check("keyword-only trigger is rejected", v["eligible"], False)
    check("and adds nothing to the score",
          v["score"], score.evaluate(dict(base), now, False)["score"])

    verified = dict(base, trigger={"kind": "policy", "strength": 0.9,
                                   "event": "SEBI settled with Adani Ports"})
    v2 = score.evaluate(verified, now, require_trigger=True)
    check("adjudicated trigger passes", v2["eligible"], True)
    check("and raises the score", v2["score"] > v["score"], True)

    print("\nSQL avoids round(double precision, int)")
    import backfill  # noqa: PLC0415
    import inspect  # noqa: PLC0415
    src = inspect.getsource(backfill.recompute_mults)
    check("casts to numeric", "as numeric" in src, True)
    check("does not cast to real", "as real)" in src, False)

    print()
    if FAILS:
        for f in FAILS:
            print("FAILED:", f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
