"""Persist a computed shortlist so the web app has rows to read.

The scoring runs entirely in memory: `score.py` reads videos, channels and
topics.yml, applies the gate, and prints the result. Nothing was ever written
to the `shortlists` and `candidates` tables the schema has carried from the
start.

That gap is what this closes, and it is deliberately the only way the app
gets its data. The alternative — having the browser query videos and apply
the rules in JavaScript — would mean two copies of the gate, in two
languages, drifting apart the first time a threshold changes. The rules stay
in Python; the app reads what Python decided.

Each run writes one `shortlists` row and one `candidates` row per topic that
cleared the gate, carrying the evidence and the reasoning as JSON. The app
then only ever reads those rows and writes back a decision.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from score import shortlist  # noqa: E402
from store import Store  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _json(value) -> str:
    """jsonb on Postgres, text on SQLite — a JSON string satisfies both."""
    return json.dumps(value, default=str)


def upsert_topics(store: Store, topics: list[dict]) -> dict[str, str]:
    """Make sure every topic on the shortlist exists as a row, keyed by slug.

    `candidates.topic_id` is a foreign key, so the topics have to be real
    rows before any candidate can reference them. Slug is the natural key:
    topics.yml owns it, and it survives relabelling.
    """
    ids: dict[str, str] = {}
    with store.cursor() as cur:
        for t in topics:
            cur.execute(store.q("select id from topics where slug = ?"),
                        (t["slug"],))
            row = cur.fetchone()
            if row:
                ids[t["slug"]] = str(row[0])
                cur.execute(
                    store.q(
                        "update topics set label = ?, category = ?, "
                        "n_videos = ?, n_channels = ?, demand_en = ?, "
                        "demand_hi = ?, best_mult = ?, state = ? where id = ?"
                    ),
                    (t["label"], t["category"], t["n_videos"], t["n_channels"],
                     t["demand_en"], t["demand_hi"], t["best_mult"],
                     t["state"], row[0]),
                )
                continue
            new_id = str(uuid.uuid4())
            cur.execute(
                store.q(
                    "insert into topics (id, slug, label, category, n_videos, "
                    "n_channels, demand_en, demand_hi, best_mult, state) "
                    "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (new_id, t["slug"], t["label"], t["category"], t["n_videos"],
                 t["n_channels"], t["demand_en"], t["demand_hi"],
                 t["best_mult"], t["state"]),
            )
            ids[t["slug"]] = new_id
    return ids


def publish(store: Store, ok: list[dict], mode: str = "decoded",
            requested_by: str | None = None) -> tuple[str, int]:
    """Write one shortlist and its candidates. Returns (shortlist id, count)."""
    topic_ids = upsert_topics(store, ok)
    shortlist_id = str(uuid.uuid4())
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    with store.cursor() as cur:
        cur.execute(
            store.q(
                "insert into shortlists (id, mode, requested_by, created_at) "
                "values (?, ?, ?, ?)"
            ),
            (shortlist_id, mode, requested_by, now),
        )
        rows = []
        for rank, t in enumerate(ok, 1):
            # Only the fields the card actually shows. Storing the whole
            # evidence list would carry every video we matched, most of which
            # nobody reads.
            evidence = [
                {
                    "channel": e["channel"],
                    "title": e["title"],
                    "age_days": e["age_days"],
                    "mult": float(e["mult"]) if e["mult"] is not None else None,
                    "views": e["views"],
                    "lang": e["lang"],
                    "tier": e["tier"],
                    "video_id": e["id"],
                }
                for e in t["evidence"][:6]
            ]
            parts = {
                "notes": t["notes"],
                "demand_en": t["demand_en"],
                "demand_hi": t["demand_hi"],
                "demand_en_raw": t.get("demand_en_raw"),
                "n_en": t.get("n_en"),
                "n_videos": t["n_videos"],
                "n_channels": t["n_channels"],
                "best_mult": t["best_mult"],
                "trend": t.get("trend"),
                "state": t["state"],
            }
            rows.append((
                str(uuid.uuid4()), shortlist_id, topic_ids[t["slug"]], rank,
                float(t["score"]), _json(parts), t["notes"][0] if t["notes"] else None,
                _json(evidence), "shown",
            ))
        cur.executemany(
            store.q(
                "insert into candidates (id, shortlist_id, topic_id, rank, "
                "score, score_parts, reason, evidence, status) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            rows,
        )
    return shortlist_id, len(rows)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Compute a shortlist and store it for the app to read")
    p.add_argument("--dsn")
    p.add_argument("--topics", default=os.path.join(HERE, "topics.yml"))
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--mode", default="decoded", choices=["decoded", "ai_seekho"])
    p.add_argument("--require-trigger", action="store_true")
    a = p.parse_args(argv)

    store = Store(a.dsn)
    try:
        ok, _ = shortlist(store, a.topics, a.limit, a.require_trigger)
        if not ok:
            # Publishing an empty shortlist would show the team a blank page
            # with no way to tell it apart from a failure.
            print("No topic cleared the gate — nothing published.")
            return 0
        sid, n = publish(store, ok, mode=a.mode)
    finally:
        store.close()

    print(f"published shortlist {sid} with {n} candidates")
    for i, t in enumerate(ok, 1):
        print(f"  {i}. {t['label']}  ({t['score']:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
