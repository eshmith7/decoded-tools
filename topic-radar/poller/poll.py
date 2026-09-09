"""Poll channel RSS feeds, record videos and snapshots.

Runs on a schedule (GitHub Actions). Two things happen per channel:

  1. New uploads are written to `videos`.
  2. Every video young enough for velocity to be meaningful gets a row in
     `snapshots`, carrying views/hour since the *previous* snapshot.

Snapshot history is the one asset here that cannot be backfilled. Views are a
running total: a video's history before we start watching is unrecoverable, so
the poller is the first thing built and the first thing deployed.

Storage discipline matters on the free tier. Snapshotting every known video
every 30 minutes would be ~175M rows a year, far past Supabase's 500MB. Only
videos under SNAPSHOT_MAX_AGE_DAYS are tracked at full resolution, which is
~100 videos at a time across the panel and about 90MB a year.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import statistics as st
import sys
import concurrent.futures as cf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import youtube as yt  # noqa: E402
from store import Channel, Store  # noqa: E402

SNAPSHOT_MAX_AGE_DAYS = 30
BASELINE_RECENT_N = 20
WORKERS = 8

log = logging.getLogger("poll")


def load_registry(path: str) -> list[dict]:
    import yaml  # noqa: PLC0415

    with open(path) as fh:
        return yaml.safe_load(fh)["channels"]


def _baseline(store: Store, channel_id: str) -> tuple[int | None, int | None, int]:
    """Median long-form views, all-time and recent.

    Every performance figure in the product is expressed against this rather
    than raw views. Raw views only measure channel size: Decoded's Rameshwaram
    video is 6.5x its own median at 6K views, which a raw ranking would bury.
    """
    allv = store.longform_views(channel_id)
    recent = store.longform_views(channel_id, limit=BASELINE_RECENT_N)
    return (
        int(st.median(allv)) if allv else None,
        int(st.median(recent)) if recent else None,
        len(allv),
    )


def poll_channel(ch: Channel) -> tuple[Channel, list[yt.Video] | None, str]:
    try:
        return ch, yt.with_retry(yt.rss_feed, ch.id), "ok"
    except Exception as e:  # noqa: BLE001
        return ch, None, f"{type(e).__name__}: {e}"


def run(store: Store, registry_path: str, dry_run: bool = False) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=SNAPSHOT_MAX_AGE_DAYS)

    store.upsert_channels(load_registry(registry_path))
    due = store.channels_due(now)
    log.info("%d channels due", len(due))

    stats = {"channels": 0, "failed": 0, "new_videos": 0,
             "snapshots": 0, "accelerating": []}

    with cf.ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(poll_channel, due))

    for ch, videos, status in results:
        if videos is None:
            stats["failed"] += 1
            log.warning("%s: %s", ch.name, status)
            continue

        known = store.known_video_ids(ch.id)
        median = ch.median_recent or ch.median_views

        with store.cursor() as cur:
            for v in videos:
                if v.published_at is None:
                    continue
                is_new = v.id not in known
                mult = (v.views / median) if (v.views and median) else None
                store.upsert_video(cur, {
                    "id": v.id,
                    "channel_id": ch.id,
                    "title": v.title,
                    "description": v.description,
                    "published_at": v.published_at.isoformat(),
                    "duration_s": v.duration_s,
                    "views": v.views,
                    "likes": v.likes,
                    "mult": round(mult, 3) if mult else None,
                    "now": now.isoformat(),
                })
                if is_new:
                    stats["new_videos"] += 1
                    log.info("new: %s — %s", ch.name, v.title[:60])

                if v.views is not None and v.published_at >= cutoff:
                    vph = store.add_snapshot(cur, v.id, now, v.views, v.likes)
                    stats["snapshots"] += 1
                    # Only long-form counts: shorts distort every baseline.
                    if vph and v.is_longform and median:
                        age_h = max((now - v.published_at).total_seconds() / 3600, 1)
                        avg_vph = v.views / age_h
                        # Rising *now* rather than merely large overall.
                        if avg_vph > 0 and vph > avg_vph * 1.2 and v.views > median * 0.2:
                            stats["accelerating"].append({
                                "channel": ch.name, "title": v.title,
                                "views": v.views, "age_h": round(age_h, 1),
                                "vph_now": vph, "vph_avg": round(avg_vph, 1),
                                "mult": round(v.views / median, 2),
                            })

        # RSS gives no duration, so a channel's baseline is only meaningful once
        # a catalogue crawl has filled durations in. Skip until then.
        mv, mr, n = _baseline(store, ch.id)
        if n:
            store.set_baseline(ch.id, mv, mr, n)
        if not dry_run:
            store.mark_polled(ch.id, now.isoformat())
        stats["channels"] += 1

    return stats


def main(argv=None):
    p = argparse.ArgumentParser(description="Poll channel RSS feeds")
    p.add_argument("--registry", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "channels.yml"))
    p.add_argument("--dsn", default=None, help="DATABASE_URL override")
    p.add_argument("--dry-run", action="store_true",
                   help="write data but do not advance last_polled")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if a.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    store = Store(a.dsn)
    try:
        s = run(store, a.registry, dry_run=a.dry_run)
    finally:
        store.close()

    print(f"polled {s['channels']} channels ({s['failed']} failed) · "
          f"{s['new_videos']} new videos · {s['snapshots']} snapshots")
    if s["accelerating"]:
        print(f"\naccelerating ({len(s['accelerating'])}):")
        for a_ in sorted(s["accelerating"], key=lambda x: -x["mult"]):
            print(f"  {a_['mult']:>5.2f}x own median · {a_['age_h']:>5.0f}h · "
                  f"{a_['vph_now']:>9,.0f} v/h now vs {a_['vph_avg']:>9,.0f} avg · "
                  f"{a_['channel']} — {a_['title'][:48]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
