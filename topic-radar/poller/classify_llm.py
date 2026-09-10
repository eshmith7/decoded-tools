"""Classify the titles the dictionary could not match.

The dictionary in topics.yml is the fast path and covers the entities we
already know about — roughly a quarter of the corpus. The rest needs reading
rather than matching: "Why is Aman Gupta's Boat Failing?" is about boAt, and
no amount of pattern work gets there.

Works with either OpenAI or Gemini, whichever key is present in the
environment. Titles are sent in batches so one request covers many videos,
which keeps a full pass over several thousand titles cheap and, on Gemini's
free tier, inside a single day's quota.

New topics discovered here are appended to topics.yml, so the dictionary
grows and the same title never has to be classified twice. That is the point:
the LLM is used once per unknown subject, not once per lookup.
"""

from __future__ import annotations

import argparse
import difflib
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
BATCH = 40

# A cheap, fast model is the right tool here: the task is short-label
# extraction from one line of text, not reasoning.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

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
    """Rate limit or spend cap hit. Callers stop cleanly rather than retry."""


def _post(url: str, body: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read()[:300]
        if e.code == 429:
            raise QuotaExceeded(f"rate limited or out of quota: {detail!r}") from e
        raise RuntimeError(f"HTTP {e.code}: {detail!r}") from e


def _parse_array(text: str) -> list[dict]:
    """Models occasionally wrap JSON in prose or a fenced block."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            raise
        parsed = json.loads(m.group(0))
    if isinstance(parsed, dict):
        # Some models honour a JSON-object response format by wrapping the
        # array in a single key rather than returning it bare.
        for v in parsed.values():
            if isinstance(v, list):
                return v
        return []
    return parsed


def call_openai(api_key: str, titles: list[str], timeout: int = 120) -> list[dict]:
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    doc = _post(
        "https://api.openai.com/v1/chat/completions",
        {
            "model": OPENAI_MODEL,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content":
                 "You label video titles by subject and reply with JSON only."},
                {"role": "user", "content":
                 PROMPT + numbered +
                 '\n\nReturn an object of the form {"items": [...]}.'},
            ],
        },
        {"Authorization": f"Bearer {api_key}"},
        timeout,
    )
    return _parse_array(doc["choices"][0]["message"]["content"])


def call_gemini(api_key: str, titles: list[str], timeout: int = 120) -> list[dict]:
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    doc = _post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}",
        {
            "contents": [{"parts": [{"text": PROMPT + numbered}]}],
            "generationConfig": {"temperature": 0,
                                 "responseMimeType": "application/json"},
        },
        {},
        timeout,
    )
    try:
        text = doc["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"unexpected Gemini response: {str(doc)[:200]}") from e
    return _parse_array(text)


def pick_backend() -> tuple[str, str, callable]:
    """Whichever key is configured. OpenAI wins if both are set."""
    if os.environ.get("OPENAI_API_KEY"):
        return "openai", os.environ["OPENAI_API_KEY"], call_openai
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini", os.environ["GEMINI_API_KEY"], call_gemini
    return "", "", None


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


def _near_duplicate(label: str, existing: list[str]) -> str | None:
    """Is `label` the same topic as one we already have, under another name?

    Character similarity alone misfires on short words — "Titanic" and
    "Titan" score 0.83 and are unrelated. So short labels are compared by
    whole-word containment only, and longer ones must clear a high bar.
    """
    words = set(re.split(r"[^\w]+", label))
    for other in existing:
        other_words = set(re.split(r"[^\w]+", other))
        # "Union Budget 2023" vs "Union Budget": one is the other plus detail.
        if words > other_words or other_words > words:
            return other
        if min(len(label), len(other)) < 8:
            continue  # too short for character similarity to mean anything
        if difflib.SequenceMatcher(None, label, other).ratio() >= 0.88:
            return other
    return None


# Words that must stay usable as topics no matter which channel name contains
# them. "India Today" would otherwise make "india" creator branding, and
# "Backstage with Millionaires" would do the same to "with".
PROTECTED = {
    "india", "indian", "today", "with", "business", "money", "finance",
    "news", "world", "global", "market", "economy", "china", "america",
    "school", "capital", "invest", "wealth", "first", "post", "times",
}


def self_referential_names(store: Store, threshold: float = 0.8) -> set[str]:
    """Channel names that only ever appear in their own channel's titles.

    A creator's name in their own titles is branding, not a subject. A
    company that happens to also run a channel — Zerodha, Groww, ET Money —
    gets discussed by everyone else too, and must stay eligible as a topic.
    So the rule is concentration, not mere collision.
    """
    with store.cursor() as cur:
        cur.execute("select id, name from channels")
        channels = cur.fetchall()

    out: set[str] = set()
    for cid, name in channels:
        words = [w for w in re.split(r"[^\w]+", name.lower())
                 if len(w) >= 4 and w not in PROTECTED]
        if not words:
            continue
        like = "%" + "%".join(words[:2]) + "%"
        with store.cursor() as cur:
            cur.execute(
                store.q(
                    "select channel_id = ? , count(*) from videos "
                    "where lower(title) like ? group by 1"
                ),
                (cid, like),
            )
            counts = {bool(own): n for own, n in cur.fetchall()}
        total = sum(counts.values())
        if total >= 3 and counts.get(True, 0) / total >= threshold:
            out.update(words)
    return out


def merge_into_registry(path: str, discovered: dict[str, dict],
                        channel_words: set[str]) -> tuple[int, list[str]]:
    """Append newly found topics, rejecting the ones that would do harm.

    Three filters, each earned:

    - Self-referential creator names. Creators put their own names in their
      titles, so the model proposed "Dhruv Rathee" and "Sandeep Maheshwari"
      as topics. Matching a channel name is not enough to reject on, though:
      Zerodha, Groww and ET Money are channels we track *and* real companies
      other channels make videos about. The test is whether anyone else ever
      talks about it — see `self_referential_names`.
    - Near-duplicates. "Electric Vehicles" against an existing "EV industry",
      or "IT Sector" against "Indian IT industry", splits one topic's
      evidence across two rows and understates both.
    - Ordinary English words, for the same reason classify.py rejects them.
    """
    from classify import UNSAFE_ALIASES  # noqa: PLC0415

    existing = load_topics(path)
    have_slugs = {t["slug"] for t in existing}
    have_aliases = {a.lower() for t in existing for a in t["aliases"]}
    have_labels = [t["label"].lower() for t in existing]

    new, rejected = [], []
    for slug, t in sorted(discovered.items()):
        label = t["label"].lower()
        if slug in have_slugs or label in have_aliases or len(label) < 3:
            continue
        if label in UNSAFE_ALIASES:
            rejected.append(f"{t['label']} (ordinary word)")
            continue
        words = {w for w in re.split(r"[^\w]+", label) if len(w) >= 3}
        if words and words <= channel_words:
            rejected.append(f"{t['label']} (self-referential creator name)")
            continue
        near = _near_duplicate(label, have_labels)
        if near:
            rejected.append(f"{t['label']} (near-duplicate of {near!r})")
            continue
        new.append(t)
        have_slugs.add(slug)
        have_aliases.add(label)
        have_labels.append(label)

    if not new:
        return 0, rejected
    with open(path, "a") as fh:
        fh.write(f"\n  # --- discovered by classify_llm.py, {time.strftime('%Y-%m-%d')}\n")
        for t in new:
            fh.write(
                f"  - {{slug: {t['slug']}, label: {json.dumps(t['label'])}, "
                f"category: {t['category']}, aliases: [{json.dumps(t['label'].lower())}]}}\n"
            )
    return len(new), rejected


def main(argv=None):
    p = argparse.ArgumentParser(description="LLM-classify unmatched titles")
    p.add_argument("--dsn")
    p.add_argument("--topics", default=os.path.join(HERE, "topics.yml"))
    p.add_argument("--limit", type=int, default=400, help="titles per run")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be sent, call nothing")
    a = p.parse_args(argv)

    backend, key, call = pick_backend()
    if not key and not a.dry_run:
        print("No LLM key configured. Set OPENAI_API_KEY, or GEMINI_API_KEY "
              "for the free tier at https://aistudio.google.com/apikey",
              file=sys.stderr)
        return 2
    if key:
        model = OPENAI_MODEL if backend == "openai" else GEMINI_MODEL
        print(f"backend: {backend} ({model})")

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
                out = call(key, [t for _, t in chunk])
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
            print(f"  batch {i//BATCH + 1}/{(len(todo)+BATCH-1)//BATCH}: "
                  f"{len(out)} labelled", flush=True)
            time.sleep(0.4)

        added, rejected = merge_into_registry(
            a.topics, discovered, self_referential_names(store)
        )
        print(f"labelled {labelled} titles · {len(discovered)} distinct subjects · "
              f"{added} new topics appended to {os.path.basename(a.topics)}")
        if rejected:
            print(f"rejected {len(rejected)}:")
            for r in rejected[:12]:
                print("   ", r)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
