"""Classify the titles the dictionary could not match.

The dictionary in topics.yml is the fast path and covers the entities we
already know about — roughly a quarter of the corpus. The rest needs reading
rather than matching: "Why is Aman Gupta's Boat Failing?" is about boAt, and
no amount of pattern work gets there.

Runs against Gemini's free tier (1500 requests/day, no card). Titles are sent
in batches so one request covers many videos, which keeps a full pass over
several thousand unmatched titles inside a single day's quota.

New topics discovered here are appended to topics.yml, so the dictionary
grows and the same title never has to be classified twice. That is the point:
the LLM is used once per unknown subject, not once per lookup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from classify import classify, load_topics  # noqa: E402
from store import Store  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = "gemini-2.0-flash"
ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{MODEL}:generateContent"
)
BATCH = 40

PROMPT = """You are labelling YouTube videos from Indian business, finance and \
geopolitics channels, so they can be grouped by subject.

For each numbered title, return the single main subject as a canonical topic.

Rules:
- Topics are ENTITY-LEVEL. "BlackRock" is one topic; do not split it into \
"BlackRock and ESG" or "BlackRock owns everything". Those are framings.
- Use the company, country, person, policy or sector the video is ABOUT.
- Ignore the channel's own name and any presenter's name.
- If a title is generic advice with no specific subject (for example \
"5 rules to become rich", "mutual fund mistakes"), return null for the topic.
- label: human-readable, e.g. "boAt", "Zoho", "Shark Tank India".
- slug: lowercase, hyphenated, e.g. "boat", "zoho", "shark-tank-india".
- category: one of company, geopolitics, macro, policy, person, sector.
- framing: two or three words for the angle taken, e.g. "collapse", \
"hidden owner", "rise of", "price war". Null if unclear.

Return ONLY a JSON array, one object per input title, in the same order:
[{"n": 1, "slug": "...", "label": "...", "category": "...", "framing": "..."}]
Use null for slug when there is no specific subject.

Titles:
"""


class QuotaExceeded(RuntimeError):
    pass


def call_gemini(api_key: str, titles: list[str], timeout: int = 90) -> list[dict]:
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    body = {
        "contents": [{"parts": [{"text": PROMPT + numbered}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        f"{ENDPOINT}?key={api_key}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise QuotaExceeded("Gemini free-tier quota exhausted") from e
        raise RuntimeError(f"Gemini HTTP {e.code}: {e.read()[:200]!r}") from e

    try:
        text = doc["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"unexpected Gemini response: {str(doc)[:200]}") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def unmatched_titles(store: Store, topics_path: str, limit: int) -> list[tuple[str, str]]:
    """Long-form videos with no dictionary topic, best performers first.

    Ordering by performance matters: a budget that runs out should have been
    spent on the videos whose subjects are most likely to be worth covering.
    """
    r = classify(store, topics_path)
    matched = {v for v, _ in r["pairs"]}
    with store.cursor() as cur:
        cur.execute(
            "select id, title from videos where duration_s >= 480 "
            "and mult is not null order by mult desc"
        )
        rows = cur.fetchall()
    return [(i, t) for i, t in rows if i not in matched][:limit]


def merge_into_registry(path: str, discovered: dict[str, dict]) -> int:
    """Append newly found topics, skipping any slug or alias already claimed."""
    existing = load_topics(path)
    have_slugs = {t["slug"] for t in existing}
    have_aliases = {a.lower() for t in existing for a in t["aliases"]}

    new = []
    for slug, t in sorted(discovered.items()):
        if slug in have_slugs:
            continue
        alias = t["label"].lower()
        if alias in have_aliases or len(alias) < 3:
            continue
        new.append(t)
        have_slugs.add(slug)
        have_aliases.add(alias)

    if not new:
        return 0
    with open(path, "a") as fh:
        fh.write(f"\n  # --- discovered by classify_llm.py, {time.strftime('%Y-%m-%d')}\n")
        for t in new:
            fh.write(
                f"  - {{slug: {t['slug']}, label: {json.dumps(t['label'])}, "
                f"category: {t['category']}, aliases: [{json.dumps(t['label'].lower())}]}}\n"
            )
    return len(new)


def main(argv=None):
    p = argparse.ArgumentParser(description="LLM-classify unmatched titles")
    p.add_argument("--dsn")
    p.add_argument("--topics", default=os.path.join(HERE, "topics.yml"))
    p.add_argument("--limit", type=int, default=400, help="titles per run")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be sent, call nothing")
    a = p.parse_args(argv)

    key = os.environ.get("GEMINI_API_KEY")
    if not key and not a.dry_run:
        print("GEMINI_API_KEY is not set. Get a free key at "
              "https://aistudio.google.com/apikey", file=sys.stderr)
        return 2

    store = Store(a.dsn)
    try:
        todo = unmatched_titles(store, a.topics, a.limit)
        print(f"{len(todo)} unmatched long-form titles, "
              f"{(len(todo) + BATCH - 1)//BATCH} requests")
        if a.dry_run:
            for _, t in todo[:8]:
                print("   ", t[:74])
            return 0

        discovered: dict[str, dict] = {}
        labelled = 0
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            try:
                out = call_gemini(key, [t for _, t in chunk])
            except QuotaExceeded:
                print("quota exhausted; stopping cleanly", file=sys.stderr)
                break
            except Exception as e:  # noqa: BLE001
                print(f"  batch {i//BATCH + 1} failed: {e}", file=sys.stderr)
                continue
            for item in out:
                slug = (item.get("slug") or "").strip()
                if not slug:
                    continue
                discovered.setdefault(slug, {
                    "slug": slug,
                    "label": item.get("label") or slug,
                    "category": item.get("category") or "company",
                })
                labelled += 1
            time.sleep(1.0)  # stay inside the free tier's rate limit

        added = merge_into_registry(a.topics, discovered)
        print(f"labelled {labelled} titles · {len(discovered)} distinct subjects · "
              f"{added} new topics appended to {os.path.basename(a.topics)}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
