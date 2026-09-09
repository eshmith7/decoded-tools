"""Resolve and verify channels before they enter the registry.

Handle resolution alone is unsafe: YouTube handles get squatted, so
@DrVivekBindra resolved to a 127-subscriber impostor and @CARahulMalodia to
one with 2. Anything entering the registry has to clear a subscriber floor
and actually have a catalogue, or the baselines it feeds are garbage.
"""

from __future__ import annotations

import json
import re
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import youtube as yt  # noqa: E402

MIN_SUBS = 50_000
MIN_VIDEOS = 10


def _parse_count(text: str | None) -> int | None:
    if not text:
        return None
    m = re.match(r"([\d.]+)\s*([KMB]?)", text.strip())
    if not m:
        return None
    n = float(m.group(1))
    return int(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])


def channel_about(channel_id: str) -> dict:
    """Name, subscriber count and video count for a channel.

    Read from the innertube browse header rather than the rendered page: the
    /channel/<id> HTML carries subscriber counts for *recommended* channels
    too, so scraping it attributes someone else's numbers to this channel.
    """
    doc = yt.with_retry(
        yt._post_innertube,
        "browse",
        {
            "context": yt.INNERTUBE_CTX,
            "browseId": channel_id,
            "params": yt.TAB_VIDEOS,
        },
    )
    name = (doc.get("metadata", {}).get("channelMetadataRenderer", {}).get("title")
            or doc.get("microformat", {}).get("microformatDataRenderer", {}).get("title"))
    header = json.dumps(doc.get("header", {}))
    subs = re.search(r'"content":\s*"([\d.]+[KMB]?) subscribers"', header)
    vids = re.search(r'"content":\s*"([\d.,]+[KMB]?) videos?"', header)
    return {
        "id": channel_id,
        "name": name,
        "handle": (doc.get("metadata", {})
                   .get("channelMetadataRenderer", {})
                   .get("vanityChannelUrl", "").rsplit("/", 1)[-1] or None),
        "subs": _parse_count(subs.group(1)) if subs else None,
        "videos": _parse_count(vids.group(1).replace(",", "")) if vids else None,
    }


def find_channel(name: str) -> list[dict]:
    """Locate a channel by searching for its videos and reading the owners.

    More reliable than guessing handles, because it finds the account that
    actually publishes the content rather than whoever holds the name.
    """
    hits: dict[str, int] = {}
    for v in yt.search(f"{name} case study"):
        if v.channel_id:
            hits[v.channel_id] = hits.get(v.channel_id, 0) + 1
    out = []
    for cid, n in sorted(hits.items(), key=lambda kv: -kv[1])[:4]:
        try:
            info = channel_about(cid)
        except Exception:
            continue
        info["hits"] = n
        out.append(info)
    return out


# Words that should show up in the titles of a channel we would trust as topic
# evidence. Deliberately broad — this is a smell test, not a classifier.
RELEVANT = {
    "business", "company", "economy", "economic", "market", "markets", "money",
    "finance", "financial", "stock", "stocks", "invest", "investing", "crore",
    "billion", "million", "trillion", "revenue", "profit", "loss", "brand",
    "startup", "founder", "ceo", "industry", "strategy", "case", "study",
    "collapse", "downfall", "failed", "failure", "rise", "empire", "bankrupt",
    "acquisition", "merger", "ipo", "tax", "gst", "policy", "government",
    "geopolitics", "war", "trade", "tariff", "china", "india", "america",
    "bank", "banking", "debt", "crisis", "scam", "fraud", "price", "pricing",
    # Added after the first registry audit: legitimate explainer channels
    # (Company Man, RealLifeLore, BritMonkey) were scraping past the threshold
    # at 0.15-0.17 because they phrase the same subject differently.
    "decline", "declined", "bankruptcy", "sold", "sales", "retail", "airline",
    "monopoly", "billionaire", "entrepreneur", "corporate", "factory", "oil",
    "energy", "supply", "trillion", "shutdown", "layoffs", "jobs", "growth",
    "richest", "worth", "cost", "expensive", "cheap", "deal", "buyout",
    "regulation", "antitrust", "sanctions", "tariffs", "inflation", "recession",
}

MIN_RELEVANT_SHARE = 0.15


def content_check(channel_id: str, sample: int = 40) -> tuple[bool, float, list[str]]:
    """Does this channel actually publish the kind of thing we score on?

    A subscriber floor rejects squatters but not a channel that is large and
    simply about something else. @TheCompanyMan resolves to a 393k-subscriber
    hip-hop commentary channel that shares its name with the business
    explainer we wanted, and passed every size check.
    """
    vids = yt.channel_videos(channel_id, max_pages=2)[:sample]
    if not vids:
        return False, 0.0, []
    hits = 0
    for v in vids:
        words = {w.strip(".,:|?!\"'()").lower() for w in v.title.split()}
        if words & RELEVANT:
            hits += 1
    share = hits / len(vids)
    return share >= MIN_RELEVANT_SHARE, share, [v.title for v in vids[:3]]


def verify(channel_id: str, skip_content: bool = False) -> tuple[bool, dict, str]:
    info = channel_about(channel_id)
    if info["subs"] is None:
        return False, info, "no subscriber count on page"
    if info["subs"] < MIN_SUBS:
        return False, info, f"only {info['subs']:,} subscribers"
    if info["videos"] is not None and info["videos"] < MIN_VIDEOS:
        return False, info, f"only {info['videos']} videos"
    if not skip_content:
        ok, share, sample = content_check(channel_id)
        info["relevant_share"] = round(share, 2)
        info["sample"] = sample
        if not ok:
            return False, info, (
                f"only {share:.0%} of titles look on-topic — e.g. {sample[:2]}"
            )
    return True, info, "ok"


if __name__ == "__main__":
    for cid in sys.argv[1:]:
        ok, info, why = verify(cid)
        print(f"{'OK ' if ok else 'REJECT'} {cid}  {info['name']!r:<34} "
              f"subs={info['subs']}  videos={info['videos']}  {why}")
