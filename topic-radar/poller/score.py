"""Rank topics into a shortlist, with the reasoning attached.

The score is a gate, not a weighted feed. That comes from how topics
actually behave in the data:

  Vijay Mallya   3.1M -> 22.9M -> 31M     grew for late entrants
  Byju's         1.8M -> 10.1M            grew for late entrants
  Nokia          13.2M -> 1.2M -> 53K     decayed to nothing
  Jet Airways    one cluster, then dead   never revived

Nokia is an evergreen topic with no fresh trigger, and it decays. Jet
Airways was a trigger with no evergreen depth, and it died in one cluster.
The two that grew were deep topics that news re-lit. So proven demand and a
fresh trigger are conditions, not contributions: failing either is a
rejection, and the remaining factors only order what survives.

Trigger detection is not built yet, so `--require-trigger` is off by
default. Everything else in the gate is live, and the evergreen lane is
where the evidence says the wins are anyway.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from classify import classify  # noqa: E402
from store import Store, as_utc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# A topic anyone covered this recently is still saturated with their
# audience's attention. Decoded's own OpenAI video landed 14 days after
# Think School's with the same framing and did 936 views against a channel
# median of ~7000 — the clearest evidence in the data for this rule.
BLOCK_DAYS_ANY = 30
BLOCK_DAYS_TIER1 = 45
PENALTY_DAYS = 90

# Below this a topic is not proven enough to build on.
MIN_DEMAND_EN = 1.15
MIN_CHANNELS = 2

# Demand is judged on English coverage, so the evidence has to be English
# coverage. Counting channels across all languages while measuring demand in
# English only let a topic with one English video pass as "proven by 2
# channels" — which is how Apollo 13, on a single outlier, reached the top of
# the first shortlist this produced.
MIN_EN_VIDEOS = 2
MIN_EN_CHANNELS = 2

# Hindi is kept as weak supporting evidence only: measured correlation with
# English performance is 0.23, so it can nudge ordering but never qualify a
# topic on its own.
HINDI_WEIGHT = 0.15

# What an active trigger is worth. Deliberately large: the evidence says a
# proven topic with fresh news is the shape that wins, and a proven topic
# without one is the Nokia shape that decays. This is the difference between
# ranking by history and ranking by opportunity.
# Large enough that a fresh trigger reorders the list rather than nudging it,
# which is what the evidence argues for — Mallya and Byju's grew for late
# entrants only once news re-lit them. The exact figure is not calibrated
# against anything yet and should be tuned once picks and outcomes accumulate.
TRIGGER_WEIGHT = 1.6


def _days_since(value, now: dt.datetime) -> float | None:
    d = as_utc(value)
    return (now - d).total_seconds() / 86400 if d else None


def gather(store: Store, topics_path: str, now: dt.datetime) -> list[dict]:
    """Per-topic evidence, including who covered it and when."""
    result = classify(store, topics_path)
    by_video = {}
    for vid, slug in result["pairs"]:
        by_video.setdefault(vid, []).append(slug)

    with store.cursor() as cur:
        cur.execute(
            "select v.id, v.title, v.published_at, v.views, v.mult, "
            "c.name, c.lang, c.tier, c.is_own "
            "from videos v join channels c on c.id = v.channel_id "
            "where v.duration_s >= 480"
        )
        rows = cur.fetchall()

    ev: dict[str, list] = {}
    for vid, title, pub, views, mult, cname, lang, tier, is_own in rows:
        for slug in by_video.get(vid, []):
            ev.setdefault(slug, []).append({
                "id": vid, "title": title, "pub": pub, "views": views,
                "mult": mult, "channel": cname, "lang": lang,
                "tier": int(tier), "is_own": bool(is_own),
                "age_days": _days_since(pub, now),
            })

    out = []
    for t in result["topics"]:
        t = dict(t)
        t["evidence"] = sorted(ev.get(t["slug"], []),
                               key=lambda e: e["age_days"] or 1e9)
        out.append(t)
    return out


def decay_trend(evidence: list[dict]) -> float | None:
    """Recent performance against older performance on the same topic.

    Below 1 means later entrants did worse than earlier ones — the Nokia
    shape. Above 1 is the Mallya shape, where the topic grew.
    """
    dated = [e for e in evidence if e["mult"] and e["age_days"] is not None]
    if len(dated) < 4:
        return None
    dated.sort(key=lambda e: e["age_days"])
    half = len(dated) // 2
    recent = st.median([e["mult"] for e in dated[:half]])
    older = st.median([e["mult"] for e in dated[half:]])
    return round(recent / older, 3) if older else None


def evaluate(topic: dict, now: dt.datetime, require_trigger: bool) -> dict:
    """Apply the gate. Returns the verdict and the reasons behind it."""
    ev = topic["evidence"]
    rejects: list[str] = []
    notes: list[str] = []

    if topic["state"] in ("dead", "saturated"):
        # A topic can be dead on Hindi evidence with no English coverage at
        # all, so demand_en may be absent here.
        seen = (f"English {topic['demand_en']:.2f}x"
                if topic["demand_en"] is not None
                else f"Hindi {topic['demand_hi']:.2f}x"
                if topic["demand_hi"] is not None else "no usable demand")
        rejects.append(f"topic is {topic['state']} "
                       f"({seen} over {topic['n_videos']} videos)")

    demand_en = topic["demand_en"]
    if demand_en is None:
        rejects.append("no English coverage to judge demand from")
    elif demand_en < MIN_DEMAND_EN:
        rejects.append(f"English demand {demand_en:.2f}x is below "
                       f"{MIN_DEMAND_EN:.2f}x")

    if topic["n_channels"] < MIN_CHANNELS:
        rejects.append(f"only {topic['n_channels']} channel has covered it; "
                       f"demand is unproven")
    if topic.get("n_en", 0) < MIN_EN_VIDEOS:
        rejects.append(f"only {topic.get('n_en', 0)} English video(s); "
                       f"not enough to judge English demand")
    elif topic.get("n_channels_en", 0) < MIN_EN_CHANNELS:
        rejects.append(f"English coverage comes from a single channel")

    if any(e["is_own"] for e in ev):
        rejects.append("Decoded has already covered this")

    fresh = [e for e in ev if (e["age_days"] or 1e9) <= BLOCK_DAYS_ANY]
    if fresh:
        rejects.append(f"{fresh[0]['channel']} covered it "
                       f"{fresh[0]['age_days']:.0f} days ago")
    else:
        t1 = [e for e in ev
              if e["tier"] == 1 and (e["age_days"] or 1e9) <= BLOCK_DAYS_TIER1]
        if t1:
            rejects.append(f"{t1[0]['channel']} (benchmark channel) covered it "
                           f"{t1[0]['age_days']:.0f} days ago")

    trend = decay_trend(ev)
    if trend is not None and trend < 0.5:
        rejects.append(f"decaying — recent coverage does {trend:.2f}x "
                       f"what earlier coverage did")

    if require_trigger and not (topic.get("trigger") or {}).get("event"):
        rejects.append("no verified fresh news trigger")

    # Ordering among what survives.
    score = 0.0
    if demand_en:
        score += demand_en
        raw = topic.get("demand_en_raw")
        extra = (f" (median {raw:.2f}x over {topic.get('n_en')} videos)"
                 if raw and topic.get("n_en") else "")
        notes.append(f"{demand_en:.2f}x English demand{extra}")
    if topic["demand_hi"]:
        score += HINDI_WEIGHT * topic["demand_hi"]
    score += min(topic["n_channels"], 6) * 0.12
    notes.append(f"{topic['n_channels']} channels have covered it")

    gap = min((e["age_days"] for e in ev if e["age_days"]), default=None)
    if gap:
        # A long gap since the last video means the angle is open again.
        score += min(gap / 365, 1.5) * 0.35
        notes.append(f"last covered {gap/30:.0f} months ago")
    if trend is not None and trend > 1.2:
        score += 0.4
        notes.append(f"growing — recent coverage does {trend:.2f}x earlier")
    # Only an adjudicated trigger counts. `event` is set when a model
    # confirmed the headline is about this topic and describes something that
    # happened; without it the trigger is a keyword match and nothing more.
    trigger = topic.get("trigger")
    if trigger and not trigger.get("event"):
        trigger = None
    if trigger:
        strength = trigger.get("strength") or 0.5
        score += TRIGGER_WEIGHT * strength
        # The adjudicator's one-line summary reads better on a card than a
        # syndicated headline, which often buries the event mid-sentence.
        what = trigger.get("event") or (trigger.get("headline") or "")[:80]
        notes.append(f"fresh {trigger['kind']} trigger ({strength:.2f}): {what}")
    if topic["best_mult"]:
        notes.append(f"best performer hit {topic['best_mult']:.1f}x")

    return {**topic, "score": round(score, 3), "trend": trend,
            "rejects": rejects, "notes": notes, "eligible": not rejects}


def shortlist(store: Store, topics_path: str, limit: int = 8,
              require_trigger: bool = False) -> tuple[list[dict], list[dict]]:
    now = dt.datetime.now(dt.timezone.utc)
    triggers = store.active_triggers(now)
    topics = gather(store, topics_path, now)
    for t in topics:
        t["trigger"] = triggers.get(t["slug"])
    scored = [evaluate(t, now, require_trigger) for t in topics]
    ok = sorted([s for s in scored if s["eligible"]],
                key=lambda s: -s["score"])[:limit]
    rejected = sorted([s for s in scored if not s["eligible"]],
                      key=lambda s: -(s["demand_en"] or 0))
    return ok, rejected


def render_markdown(ok: list[dict], rejected: list[dict], health: dict) -> str:
    """The shortlist as a readable page.

    Until the web app exists this is the product: run the workflow, read the
    shortlist on the run's summary page. Evidence is included on every card
    because a ranking nobody can check is a ranking nobody should trust.
    """
    L = [f"## Topic shortlist — {dt.datetime.now(dt.timezone.utc):%d %B %Y}", ""]
    if not ok:
        L += ["No topic cleared the gate this time.", "",
              "That is a real answer, not a failure. A shortlist padded with "
              "topics that failed the rules is how a tool like this stops "
              "being read.", ""]
    else:
        L += [f"**{len(ok)} topics cleared the gate.** Every figure below is a "
              f"multiple of that channel's own median, so a small channel's hit "
              f"counts as a hit.", ""]

    for i, t in enumerate(ok, 1):
        L += [f"### {i}. {t['label']}", "",
              f"*{t['category']}* · score **{t['score']:.2f}**", ""]
        trig = t.get("trigger")
        if trig:
            when = (f"{(dt.datetime.now(dt.timezone.utc) - trig['detected_at']).days}d ago"
                    if trig.get("detected_at") else "recently")
            head = trig.get("headline") or "—"
            link = f"[{head}]({trig['url']})" if trig.get("url") else head
            L += [f"**Why now** · {trig['kind']}, {when} · {link}",
                  f"<sub>{trig.get('source') or ''}</sub>", ""]
        L += ["> " + " · ".join(t["notes"]), "",
              "| Who covered it | When | vs their median | Title |",
              "|---|---|---|---|"]
        for e in t["evidence"][:5]:
            age = f"{e['age_days']/30:.0f} mo ago" if e["age_days"] else "—"
            mult = f"{e['mult']:.2f}x" if e["mult"] else "—"
            title = e["title"].replace("|", "\\|")[:70]
            L.append(f"| {e['channel']} | {age} | {mult} | {title} |")
        L.append("")

    if rejected:
        L += ["---", "", "### Rejected, and why", "",
              "| Topic | English demand | Reason |", "|---|---|---|"]
        for t in rejected[:12]:
            d = f"{t['demand_en']:.2f}x" if t["demand_en"] else "—"
            L.append(f"| {t['label']} | {d} | {t['rejects'][0]} |")
        L.append("")

    L += ["---", "",
          "<sub>" + " · ".join(f"{k} {v:,}" for k, v in health.items()) + "</sub>", ""]
    return "\n".join(L)


def main(argv=None):
    p = argparse.ArgumentParser(description="Rank topics into a shortlist")
    p.add_argument("--dsn")
    p.add_argument("--topics", default=os.path.join(HERE, "topics.yml"))
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--require-trigger", action="store_true",
                   help="reject topics with no fresh news trigger "
                        "(trigger detection is not built yet)")
    p.add_argument("--show-rejected", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p.add_argument("--markdown", action="store_true",
                   help="render as Markdown; written to GITHUB_STEP_SUMMARY "
                        "when running in Actions so the shortlist appears on "
                        "the run page instead of buried in log lines")
    a = p.parse_args(argv)

    store = Store(a.dsn)
    try:
        ok, rejected = shortlist(store, a.topics, a.limit, a.require_trigger)
        store_health = store.health()
    finally:
        store.close()

    if a.markdown:
        out = render_markdown(ok, rejected, store_health)
        print(out)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as fh:
                fh.write(out)
        return 0

    if a.json:
        print(json.dumps({"shortlist": ok, "rejected": rejected[:20]},
                         indent=1, default=str))
        return 0

    # A shortlist of two is a valid answer. Padding it to eight with topics
    # that failed the gate is how a tool like this loses its reader.
    print(f"SHORTLIST — {len(ok)} topics clear the gate\n")
    for i, t in enumerate(ok, 1):
        print(f"{i}. {t['label']}  ({t['category']}, score {t['score']:.2f})")
        print(f"   {' · '.join(t['notes'])}")
        for e in t["evidence"][:3]:
            print(f"     {e['channel'][:20]:<21} {e['age_days']/30:>5.0f}mo ago "
                  f"{(e['mult'] or 0):>6.2f}x  {e['title'][:44]}")
        print()

    if a.show_rejected:
        print(f"\nREJECTED (top {a.show_rejected} by demand)\n")
        for t in rejected[:a.show_rejected]:
            d = f"{t['demand_en']:.2f}x" if t["demand_en"] else "  -  "
            print(f"  {t['label'][:26]:<27} {d:>7}  {t['rejects'][0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
