"""Attach videos to canonical topics, then score each topic's demand.

Matching is dictionary-based and deliberately conservative. It is the fast
path for the entities we already know; `classify_llm.py` handles the rest.

Two guards matter here:

  Creator names. Channels put their own name in their titles, so "dhruv",
  "rathee", "abhi" and "niyu" are among the most frequent capitalised tokens
  in the corpus. They are derived from the registry and excluded rather than
  hand-listed, so adding a channel cannot silently create a phantom topic.

  Word boundaries. An alias that is also an ordinary word matches everywhere.
  "ola" inside "Motorola" or "Ebola" would quietly inflate that topic's
  demand, so every alias is matched on boundaries and single-word aliases
  shorter than four characters are rejected outright.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store, as_utc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
# Aliases that are also ordinary English words match nearly every title and
# quietly inflate a topic's demand. Length is not the risk — word boundaries
# already stop "ola" matching inside "Motorola", and real acronyms like GST,
# UPI and IPO are only three letters. Being an everyday word is the risk.
UNSAFE_ALIASES = {
    "the line", "line", "apple", "meta", "gold", "oil", "budget", "chip",
    "orange", "shell", "target", "square", "corner", "post",
    "next", "time", "money", "market", "brand", "group", "bank", "share",
}

# Thresholds for topic state. Derived from the observed shapes: Nokia decayed
# 13.2M -> 53K with no trigger; Zomato sits at 0.87x across 36 English videos;
# Mallya and Byju's grew for late entrants.
DEAD_MULT = 0.6
SATURATED_MULT = 0.9
SATURATED_MIN_VIDEOS = 8
PROVEN_MULT = 1.2


def channel_name_tokens(store: Store) -> set[str]:
    """Words that are channel names, not topics."""
    with store.cursor() as cur:
        cur.execute("select name from channels")
        names = [r[0] for r in cur.fetchall()]
    out: set[str] = set()
    for n in names:
        for w in re.split(r"[^\w]+", n.lower()):
            if len(w) >= 3:
                out.add(w)
    return out


def load_topics(path: str) -> list[dict]:
    import yaml  # noqa: PLC0415

    with open(path) as fh:
        topics = yaml.safe_load(fh)["topics"]
    seen: dict[str, str] = {}
    for t in topics:
        bad = [a for a in t["aliases"] if a.lower() in UNSAFE_ALIASES]
        if bad:
            raise ValueError(
                f"topic {t['slug']}: aliases {bad} are ordinary English words "
                f"and would match unrelated titles; qualify them "
                f"(e.g. 'apple' -> 'apple inc')"
            )
        for a in t["aliases"]:
            if a.lower() in seen and seen[a.lower()] != t["slug"]:
                raise ValueError(
                    f"alias {a!r} is claimed by both {seen[a.lower()]!r} and "
                    f"{t['slug']!r}; a video would be attributed to both"
                )
            seen[a.lower()] = t["slug"]
    return topics


def build_matchers(topics: list[dict], stop: set[str]) -> list[tuple[dict, re.Pattern]]:
    out = []
    for t in topics:
        parts = []
        for a in t["aliases"]:
            if " " not in a and a.lower() in stop:
                continue  # collides with a channel name
            parts.append(re.escape(a.lower()).replace(r"\ ", r"\s+"))
        if not parts:
            continue
        out.append((t, re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)")))
    return out


def classify(store: Store, topics_path: str, verbose: bool = False) -> dict:
    topics = load_topics(topics_path)
    stop = channel_name_tokens(store)
    matchers = build_matchers(topics, stop)

    with store.cursor() as cur:
        cur.execute(
            "select v.id, lower(v.title), v.channel_id, v.mult, v.views, "
            "v.published_at, c.lang "
            "from videos v join channels c on c.id = v.channel_id "
            "where v.duration_s >= 480"
        )
        rows = cur.fetchall()

    hits: dict[str, list] = defaultdict(list)
    matched_videos = set()
    pairs = []
    for vid, title, ch, mult, views, pub, lang in rows:
        for t, rx in matchers:
            if rx.search(title):
                hits[t["slug"]].append(
                    {"ch": ch, "mult": mult, "views": views,
                     "pub": pub, "lang": lang}
                )
                pairs.append((vid, t["slug"]))
                matched_videos.add(vid)

    now = dt.datetime.now(dt.timezone.utc)
    summary = []
    for t in topics:
        rs = hits.get(t["slug"], [])
        if not rs:
            continue
        mults = [r["mult"] for r in rs if r["mult"] is not None]
        en = [r["mult"] for r in rs if r["mult"] is not None and r["lang"] == "en"]
        hi = [r["mult"] for r in rs if r["mult"] is not None and r["lang"] == "hi"]
        # Normalised first: Postgres returns datetimes and SQLite strings,
        # and sorting a mix of the two raises.
        pubs = sorted(p for p in (as_utc(r["pub"]) for r in rs) if p)
        best = max(rs, key=lambda r: r["mult"] or 0) if mults else None
        summary.append({
            "slug": t["slug"], "label": t["label"], "category": t["category"],
            "n_videos": len(rs), "n_channels": len({r["ch"] for r in rs}),
            "n_en": len(en), "n_hi": len(hi),
            "n_channels_en": len({r["ch"] for r in rs if r["lang"] == "en"}),
            "demand_en": shrink(en), "demand_hi": shrink(hi),
            "demand_en_raw": round(st.median(en), 3) if en else None,
            "best_mult": round(max(mults), 3) if mults else None,
            "last_covered": pubs[-1].isoformat() if pubs else None,
            "peak_covered": (as_utc(best["pub"]).isoformat()
                             if best and as_utc(best["pub"]) else None),
            "state": topic_state(len(rs), en, hi, mults),
        })

    return {"topics": summary, "pairs": pairs,
            "matched": len(matched_videos), "total": len(rows)}


SHRINK_K = 3.0


def shrink(mults: list[float]) -> float | None:
    """Median pulled toward 1.0 in proportion to how little evidence there is.

    A raw median over one or two videos is not a demand estimate, it is an
    anecdote. Before this, "Apollo 13" topped the shortlist on a single
    ColdFusion video at 22.39x — one freak result on a topic no business
    channel should build on.

    Shrinking toward the no-information value of 1.0 costs almost nothing
    once a topic has real coverage (at n=12 the estimate keeps 80% of the
    observed median) while refusing to let n=1 shout.
    """
    if not mults:
        return None
    n = len(mults)
    est = (n * st.median(mults) + SHRINK_K * 1.0) / (n + SHRINK_K)
    return round(est, 3)


def topic_state(n: int, en: list, hi: list, mults: list) -> str:
    """Where a topic sits in its lifecycle.

    English is the deciding evidence — Decoded publishes in English, and the
    measured correlation between Hindi and English performance is only 0.23,
    so a topic thriving in Hindi says little about how it will do here.
    """
    if not mults or n < 2:
        return "unproven"
    primary = en if en else mults
    med = st.median(primary)
    if med < DEAD_MULT and n >= 4:
        return "dead"
    if n >= SATURATED_MIN_VIDEOS and med < SATURATED_MULT:
        return "saturated"
    if med >= PROVEN_MULT:
        return "proven"
    return "unproven"


def main(argv=None):
    p = argparse.ArgumentParser(description="Attach videos to topics")
    p.add_argument("--dsn")
    p.add_argument("--topics", default=os.path.join(HERE, "topics.yml"))
    p.add_argument("--show", type=int, default=30)
    a = p.parse_args(argv)

    store = Store(a.dsn)
    try:
        r = classify(store, a.topics)
    finally:
        store.close()

    print(f"matched {r['matched']}/{r['total']} long-form videos "
          f"({r['matched']/max(r['total'],1):.0%}) to "
          f"{len(r['topics'])} topics\n")
    rows = [t for t in r["topics"] if t["demand_en"] is not None]
    rows.sort(key=lambda t: -(t["demand_en"] or 0))
    print(f"{'topic':<26}{'vids':>5}{'chs':>4}{'EN':>7}{'HI':>7}{'best':>7}  state")
    for t in rows[:a.show]:
        hi = f"{t['demand_hi']:.2f}" if t["demand_hi"] is not None else "  -"
        print(f"{t['label'][:25]:<26}{t['n_videos']:>5}{t['n_channels']:>4}"
              f"{t['demand_en']:>7.2f}{hi:>7}{t['best_mult']:>7.2f}  {t['state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
