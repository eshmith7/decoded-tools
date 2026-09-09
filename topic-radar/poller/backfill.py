"""Crawl full channel catalogues to fill in what RSS cannot.

RSS is the freshest feed but carries only the last ~15 uploads and no
duration. Duration is not cosmetic: the 8-minute cut is what separates a real
case study from a short or a news clip, and every channel baseline is the
median of its long-form videos. Without it, `mult` is null everywhere and the
whole scoring layer has nothing to stand on.

So this runs weekly, walks each channel's Videos tab, and fills duration and
view counts for the back catalogue. View counts from here are coarse — the
browse endpoint rounds to "1.2M" — which is fine for a median but not for
velocity. Velocity always comes from RSS, which reports exact counts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import youtube as yt  # noqa: E402
from poll import BASELINE_RECENT_N, load_registry  # noqa: E402
from store import Store  # noqa: E402

log = logging.getLogger("backfill")


def estimate_published(age_days: float | None,
                       now: dt.datetime) -> dt.datetime | None:
    """Browse results give "4 days ago"; RSS overwrites this with the real
    timestamp when the video is recent enough to matter."""
    if age_days is None:
        return None
    return now - dt.timedelta(days=age_days)


def backfill_channel(store: Store, channel_id: str, name: str,
                     max_pages: int, now: dt.datetime) -> dict:
    vids = yt.channel_videos(channel_id, max_pages=max_pages)
    known = store.known_video_ids(channel_id)
    added = updated = 0

    with store.cursor() as cur:
        for v in vids:
            published = estimate_published(yt.parse_age_days(v.age_text), now)
            if published is None and v.id not in known:
                continue
            row = {
                "id": v.id,
                "channel_id": channel_id,
                "title": v.title,
                "description": None,
                "published_at": (published or now).isoformat(),
                "duration_s": v.duration_s,
                "views": v.views,
                "likes": None,
                "mult": None,
                "now": now.isoformat(),
            }
            if v.id in known:
                # Don't clobber a real RSS timestamp with an estimate, and
                # don't overwrite exact RSS views with a rounded browse figure.
                cur.execute(
                    store.q(
                        "update videos set duration_s = coalesce(?, duration_s), "
                        "views = coalesce(views, ?), last_seen = ? where id = ?"
                    ),
                    (v.duration_s, v.views, now.isoformat(), v.id),
                )
                updated += 1
            else:
                store.upsert_video(cur, row)
                added += 1

    allv = store.longform_views(channel_id)
    recent = store.longform_views(channel_id, limit=BASELINE_RECENT_N)
    mv = int(st.median(allv)) if allv else None
    mr = int(st.median(recent)) if recent else None
    store.set_baseline(channel_id, mv, mr, len(allv))

    return {"channel": name, "fetched": len(vids), "added": added,
            "updated": updated, "longform": len(allv), "median": mv}


def recompute_mults(store: Store) -> int:
    """Restate every video against its channel's current baseline."""
    with store.cursor() as cur:
        cur.execute(
            """
            update videos set mult = round(
              cast(views as real) / (
                select coalesce(c.median_recent, c.median_views)
                from channels c where c.id = videos.channel_id
              ), 3)
            where views is not null and exists (
              select 1 from channels c where c.id = videos.channel_id
                and coalesce(c.median_recent, c.median_views) > 0
            )
            """
        )
        return cur.rowcount


def main(argv=None):
    p = argparse.ArgumentParser(description="Crawl full channel catalogues")
    p.add_argument("--registry", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "channels.yml"))
    p.add_argument("--dsn", default=None)
    p.add_argument("--max-pages", type=int, default=40)
    p.add_argument("--only", nargs="*", help="channel ids or names to limit to")
    p.add_argument("--tier", type=int, help="only this tier")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = dt.datetime.now(dt.timezone.utc)
    store = Store(a.dsn)
    try:
        registry = load_registry(a.registry)
        store.upsert_channels(registry)
        rows = [
            c for c in registry
            if (not a.only or c["id"] in a.only or c["name"] in a.only)
            and (a.tier is None or int(c.get("tier", 2)) == a.tier)
        ]
        print(f"backfilling {len(rows)} channels\n")
        print(f"{'channel':<30}{'fetched':>8}{'added':>7}{'longform':>9}{'median':>12}")
        for c in rows:
            try:
                r = backfill_channel(store, c["id"], c["name"], a.max_pages, now)
            except Exception as e:  # noqa: BLE001
                print(f"{c['name'][:29]:<30}   FAILED  {type(e).__name__}: {e}")
                continue
            print(f"{r['channel'][:29]:<30}{r['fetched']:>8}{r['added']:>7}"
                  f"{r['longform']:>9}{(r['median'] or 0):>12,}")
        n = recompute_mults(store)
        print(f"\nrecomputed mult for {n} videos")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
