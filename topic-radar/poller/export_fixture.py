"""Dump the latest stored shortlist as JSON for the app to run on offline.

The app is developed without access to production, so it needs something
real to render. A fixture exported from the development database keeps the
layout honest — genuine topic names, genuine evidence, genuine lengths of
title — in a way invented placeholder data never does.

The app labels this mode on screen. A demo that looks like live data is
worse than no demo at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store  # noqa: E402

DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "app", "fixture.json")


def export(store: Store) -> dict:
    with store.cursor() as cur:
        cur.execute(
            "select id, mode, created_at from shortlists "
            "order by created_at desc limit 1"
        )
        row = cur.fetchone()
        if not row:
            return {"shortlist": None, "candidates": []}
        sid, mode, created = row

        cur.execute(
            store.q(
                "select c.id, c.rank, c.score, c.score_parts, c.reason, "
                "c.evidence, c.status, c.reject_note, c.decided_at, "
                "t.slug, t.label, t.category, t.state "
                "from candidates c join topics t on t.id = c.topic_id "
                "where c.shortlist_id = ? order by c.rank"
            ),
            (sid,),
        )
        candidates = []
        for (cid, rank, score, parts, reason, evidence, status,
             note, decided, slug, label, category, state) in cur.fetchall():
            candidates.append({
                "id": cid, "rank": rank, "score": float(score),
                "score_parts": json.loads(parts) if isinstance(parts, str) else parts,
                "reason": reason,
                "evidence": json.loads(evidence) if isinstance(evidence, str) else evidence,
                "status": status, "reject_note": note,
                "decided_at": str(decided) if decided else None,
                # Matches the shape Supabase returns for the joined select.
                "topics": {"slug": slug, "label": label,
                           "category": category, "state": state},
            })

    return {
        "shortlist": {"id": sid, "mode": mode, "created_at": str(created)},
        "candidates": candidates,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Export the latest shortlist as JSON")
    p.add_argument("--dsn")
    p.add_argument("--out", default=DEFAULT_OUT)
    a = p.parse_args(argv)

    store = Store(a.dsn)
    try:
        payload = export(store)
    finally:
        store.close()

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(payload, fh, indent=1)
    n = len(payload["candidates"])
    print(f"wrote {n} candidates to {os.path.relpath(a.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
