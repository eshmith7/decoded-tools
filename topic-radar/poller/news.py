"""Find the fresh news event that makes an old topic worth making again.

This is the second leg of the gate. Demand alone does not carry a topic:
Nokia was covered by everyone and decayed 13.2M -> 53K because nothing new
ever happened to it, while Mallya and Byju's grew for late entrants because
news kept re-lighting them. Jet Airways is the opposite failure — a real
event with no evergreen depth behind it, one cluster of videos and then
silence. A topic worth making needs both legs.

So what counts here is narrow on purpose. A company appearing in a market
wrap is not a trigger; a collapse, a filing, a regulator action, a founder
leaving, a verdict is. Most business news is the former, and treating it as
a trigger would hand back a shortlist of whatever happened to be in the
paper — exactly the tool the evidence said not to build.

Two sources, for two different reasons:

  Wire feeds give us whatever the business press is leading on, including
  stories about topics we do not track yet.
  Google News per topic gives us coverage of the topics we *do* care about,
  which the wires may never lead with. Only topics that could actually clear
  the gate are queried, because that request budget is finite.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import concurrent.futures as cf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from classify import (  # noqa: E402
    build_matchers,
    channel_name_tokens,
    classify,
    load_topics,
)
from classify_llm import _parse_array, pick_backend  # noqa: E402
from store import Store, as_utc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Indian business press first, because the audience is Indian and the stories
# that work are Indian business stories. Global wires are included because
# India-relevant stories often break there first — the WSJ's NEOM collapse
# piece preceded Think School's by two weeks.
FEEDS = {
    "Economic Times": "https://economictimes.indiatimes.com/rssfeedstopstories.cms",
    "ET Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Business Standard": "https://www.business-standard.com/rss/home_page_top_stories.rss",
    "Mint": "https://www.livemint.com/rss/companies",
    "Mint Economy": "https://www.livemint.com/rss/economy",
    "Moneycontrol": "https://www.moneycontrol.com/rss/business.xml",
    "Moneycontrol Companies": "https://www.moneycontrol.com/rss/results.xml",
    "The Hindu BusinessLine": "https://www.thehindubusinessline.com/feeder/default.rss",
    "Business Today": "https://www.businesstoday.in/rss/latest-news",
    "Reuters Business": "https://news.google.com/rss/search?q=when:7d+site:reuters.com+business&hl=en-IN&gl=IN&ceid=IN:en",
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "CNBC Business": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
}

GOOGLE_NEWS = ("https://news.google.com/rss/search"
               "?q={q}&hl=en-IN&gl=IN&ceid=IN:en")

# How long a window stays open, by what happened. A collapse is worth making
# a video about for weeks; a routine filing is stale in days. These set
# expires_at, which is what stops a trigger propping a topic up forever.
WINDOW_DAYS = {
    "collapse": 45,
    "verdict": 30,
    "acquisition": 30,
    "exit": 30,
    "policy": 21,
    "price-war": 21,
    "filing": 14,
}
DEFAULT_WINDOW_DAYS = 14

# Phrases that mark a real event. Ordered so the strongest reading wins when
# a headline matches more than one.
EVENT_PATTERNS: list[tuple[str, float, tuple[str, ...]]] = [
    ("collapse", 0.95, (
        "collapse", "collapses", "shuts down", "shutting down", "bankrupt",
        "bankruptcy", "insolvency", "liquidation", "wound up", "winding up",
        "defaults", "default on", "bailout", "rescue package", "fire sale",
        "mass layoffs", "lays off", "laid off", "job cuts",
    )),
    ("verdict", 0.9, (
        "sebi", "supreme court", "high court", "tribunal", "nclt", "nclat",
        "cci orders", "verdict", "convicted", "acquitted", "guilty",
        "penalty", "fined", "raid", "raids", "probe", "investigation",
        "summons", "banned", "ban on", "crackdown", "notice to",
    )),
    ("acquisition", 0.85, (
        "acquires", "acquisition", "to acquire", "buys stake", "takeover",
        "merger", "merges with", "to merge", "sells stake", "divests",
        "buyout", "acquihire",
    )),
    ("exit", 0.85, (
        "resigns", "steps down", "quits", "ousted", "sacked", "fired",
        "to step down", "exits as", "hands over", "succession", "new ceo",
        "appointed ceo", "co-founder leaves",
    )),
    ("policy", 0.8, (
        "government approves", "cabinet approves", "new rules", "new norms",
        "rbi", "gst council", "budget", "tariff", "tariffs", "sanctions",
        "import duty", "export ban", "policy change", "regulation",
        "bill passed", "notification", "mandate",
    )),
    ("price-war", 0.75, (
        "price war", "slashes prices", "cuts prices", "price cut",
        "undercut", "free for", "discount war", "burns cash",
    )),
    ("filing", 0.7, (
        "files for ipo", "drhp", "ipo", "listing", "lists at", "results",
        "q1 results", "q2 results", "q3 results", "q4 results", "annual report",
        "posts loss", "posts profit", "revenue jumps", "revenue falls",
        "profit falls", "loss widens", "valuation cut", "raises funding",
    )),
]

# Headlines that are routine coverage, whatever else they contain. A market
# wrap mentioning six companies is not six triggers.
ROUTINE_PATTERNS = (
    "market wrap", "closing bell", "opening bell", "sensex", "nifty",
    "stocks to watch", "stocks to buy", "top gainers", "top losers",
    "share price target", "buy or sell", "technical view", "f&o",
    "gold rate today", "silver rate", "petrol and diesel", "rupee vs dollar today",
    "live updates", "live blog", "horoscope", "match preview",
    "here's what", "things to know before", "trade setup",
)

MAX_AGE_DAYS = 10
MIN_STRENGTH = 0.7
ADJUDICATE_BATCH = 25

ADJUDICATE_PROMPT = """You are checking whether news stories are real triggers
for making a YouTube business video, and whether each is genuinely about the
topic it was matched to.

Keyword matching produced these candidates. It is high recall and low
precision — it matched "Mahindra" against "Kotak Mahindra Bank's head of
commercial banking quits", which is a story about Kotak, not about Mahindra.

For each numbered candidate decide two things.

1. is_about: is the headline genuinely ABOUT the named topic — the subject of
the story, not a company mentioned in passing, and not a different company
that happens to share a word in its name?

2. is_event: does the headline describe something that HAPPENED — a collapse,
a filing, a regulator action or court ruling, an acquisition, a founder or
chief executive leaving, a policy change, a price war? Routine coverage is not
an event: market wraps, share-price commentary, analyst opinion, a feature
about how well something is doing, a preview of a meeting.

3. event: name what happened in under six words, as a completed action with an
actor — "SEBI settled with Adani Ports", "Byju's filed for insolvency". If you
cannot write one because nothing specific happened, is_event is false. These
are NOT events, however important the subject:
  "UPI's success creates new responsibilities"      — a state of affairs
  "Why India's IT sector is struggling"             — an explanation
  "Everything to know about the new tax regime"     — a summary
  "Adani shares rise 4%"                            — a price move

Also return:
- kind: one of collapse, verdict, acquisition, exit, policy, price-war, filing
- strength: 0.0 to 1.0, how strong a reason this is to make a video now. A
national-scale collapse or a regulator acting against a major company is near
1.0. A quarterly result or a mid-level executive leaving is near 0.3.

Return ONLY a JSON array, one object per candidate, in the same order:
[{"n": 1, "is_about": true, "is_event": true, "event": "Byju's filed for
insolvency", "kind": "collapse", "strength": 0.9}]

Candidates:
"""


def _get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _tag(block: str, *names: str) -> str | None:
    for n in names:
        m = re.search(rf"<{n}[^>]*>(.*?)</{n}>", block, re.S | re.I)
        if m:
            return m.group(1)
    return None


def _clean_url(raw: str | None) -> str | None:
    """A link is not prose. `_clean` strips urls, which is right for a summary
    and fatal for the link element itself."""
    if raw is None:
        return None
    raw = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", raw, flags=re.S)
    return html.unescape(raw).strip() or None


def _clean(text: str | None) -> str | None:
    """RSS bodies carry CDATA, escaped markup, and tracking links.

    Unescape before stripping tags, not after. Several feeds escape their
    markup, so stripping first leaves `&lt;a href="https://news.google.com/
    rss/articles/..."&gt;` intact — and the word "google" inside that
    tracking url matched the Google topic against a story about Volkswagen.
    Urls are dropped for the same reason: a link is not what a story is
    about.
    """
    if text is None:
        return None
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.S)
    for _ in range(2):
        text = html.unescape(text)
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip() or None


_DATE_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%d %b %Y %H:%M:%S %z",
)


def _parse_date(raw: str | None, now: dt.datetime) -> dt.datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            d = dt.datetime.strptime(raw, fmt)
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    return as_utc(raw)


def parse_feed(xml: str, source: str, now: dt.datetime) -> list[dict]:
    """Handle both RSS <item> and Atom <entry>; feeds here use both."""
    blocks = re.findall(r"<item[\s>].*?</item>", xml, re.S | re.I)
    blocks += re.findall(r"<entry[\s>].*?</entry>", xml, re.S | re.I)
    out = []
    for b in blocks:
        title = _clean(_tag(b, "title"))
        link = _clean_url(_tag(b, "link"))
        if not link:
            m = re.search(r'<link[^>]*href="([^"]+)"', b, re.I)
            link = m.group(1) if m else None
        if not (title and link):
            continue
        published = _parse_date(
            _clean(_tag(b, "pubDate", "published", "updated", "dc:date")), now
        )
        if published is None:
            continue
        out.append({
            "source": source,
            "url": link,
            "title": title,
            "summary": (_clean(_tag(b, "description", "summary")) or "")[:600] or None,
            "published_at": published,
            "fetched_at": now,
        })
    return out


def fetch_feed(item: tuple[str, str], now: dt.datetime) -> list[dict]:
    source, url = item
    try:
        return parse_feed(_get(url).decode("utf-8", "replace"), source, now)
    except Exception as e:  # noqa: BLE001
        print(f"  {source}: {type(e).__name__}: {e}", flush=True)
        return []


def classify_event(text: str) -> tuple[str | None, float]:
    """What kind of event a headline describes, and how sure we are.

    Returns (None, 0) for routine coverage, which is most of it.
    """
    low = text.lower()
    if any(p in low for p in ROUTINE_PATTERNS):
        return None, 0.0
    for kind, strength, phrases in EVENT_PATTERNS:
        if any(p in low for p in phrases):
            return kind, strength
    return None, 0.0


def _call_json(backend: str, key: str, prompt: str) -> list[dict]:
    """One prompt, a JSON array back, on whichever backend is configured."""
    import classify_llm as L  # noqa: PLC0415

    if backend == "openai":
        doc = L._post(
            "https://api.openai.com/v1/chat/completions",
            {"model": L.OPENAI_MODEL, "temperature": 0,
             "response_format": {"type": "json_object"},
             "messages": [
                 {"role": "system", "content": "You reply with JSON only."},
                 {"role": "user",
                  "content": prompt + chr(10) + chr(10) +
                             'Return {"items": [...]}.'},
             ]},
            {"Authorization": f"Bearer {key}"}, 120,
        )
        return _parse_array(doc["choices"][0]["message"]["content"])

    doc = L._post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{L.GEMINI_MODEL}:generateContent?key={key}",
        {"contents": [{"parts": [{"text": prompt}]}],
         "generationConfig": {"temperature": 0,
                              "responseMimeType": "application/json"}},
        {}, 120,
    )
    return _parse_array(doc["candidates"][0]["content"]["parts"][0]["text"])


def adjudicate(candidates: list[dict], verbose: bool = True) -> list[dict]:
    """Confirm the keyword stage's guesses, or drop them.

    The keyword stage is deliberately loose so it does not miss events. That
    leaves two errors only a reader can catch: a headline matched to a company
    it merely mentions, and a headline carrying an event word without
    describing an event. Both were in the first run's output — a Volkswagen
    layoff story filed under Google, and "UPI's success in becoming integral to
    commerce" counted as a policy event.

    Without a key the candidates pass through unchanged. The keyword stage
    alone is worse, but a missing key should degrade the shortlist rather than
    empty it.
    """
    backend, key, _ = pick_backend()
    if not key or not candidates:
        if verbose:
            print("no LLM key set — keeping keyword matches unadjudicated",
                  flush=True)
        return candidates

    kept: list[dict] = []
    batches = (len(candidates) + ADJUDICATE_BATCH - 1) // ADJUDICATE_BATCH
    for i in range(0, len(candidates), ADJUDICATE_BATCH):
        chunk = candidates[i:i + ADJUDICATE_BATCH]
        lines = "\n".join(
            f"{j + 1}. topic: {c['topic_label']} | headline: {c['note']}"
            for j, c in enumerate(chunk)
        )
        verdicts = None
        # 503 "high demand" is common and temporary. Letting one blip through
        # unadjudicated puts the precision errors this stage exists to catch
        # straight into the shortlist, so it is worth a couple of retries.
        for attempt in range(3):
            try:
                verdicts = _call_json(backend, key, ADJUDICATE_PROMPT + lines)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    print(f"  batch {i // ADJUDICATE_BATCH + 1}/{batches} "
                          f"failed ({type(e).__name__}), keeping "
                          f"unadjudicated", flush=True)
                else:
                    time.sleep(2 * (attempt + 1))
        # Keep the row — the headline is still evidence — but leave `event`
        # unset. A trigger with no named event has not been adjudicated, and
        # scoring refuses to let those satisfy the gate. Failing open here
        # turned the precision stage into a no-op the moment the free tier
        # ran out, and "UPI's success creates new responsibilities" sailed
        # through as a policy trigger.
        if verdicts is None:
            kept += chunk
            continue
        by_n = {v.get("n"): v for v in verdicts if isinstance(v, dict)}
        for j, c in enumerate(chunk):
            v = by_n.get(j + 1)
            if v is None:
                kept.append(c)
                continue
            if not (v.get("is_about") and v.get("is_event")):
                continue
            # The model has to name what happened, as a completed action. A
            # verdict that cannot produce one is describing a state of
            # affairs, which is how "UPI's success creates new
            # responsibilities" was once confirmed as a policy trigger.
            event = (v.get("event") or "").strip()
            if len(event.split()) < 2:
                continue
            c = dict(c)
            c["event"] = event
            c["kind"] = v.get("kind") or c["kind"]
            if v.get("strength") is not None:
                # Keep the age decay the keyword stage already applied.
                c["strength"] = round(
                    float(v["strength"]) * c.get("decay", 1.0), 2)
            c["expires_at"] = c["published_at"] + dt.timedelta(
                days=WINDOW_DAYS.get(c["kind"], DEFAULT_WINDOW_DAYS))
            kept.append(c)
    verified = sum(1 for c in kept if c.get("event"))
    if verbose:
        print(f"adjudication ({backend}): {len(candidates)} candidates -> "
              f"{len(kept)} kept, {verified} verified", flush=True)
    if verified < len(kept):
        print(f"WARNING: {len(kept) - verified} trigger(s) could not be "
              f"adjudicated and are stored unverified. They will not satisfy "
              f"--require-trigger or add to any score.", flush=True)
    return kept


def scoreable_topics(store: Store, topics_path: str) -> set[str]:
    """Topics whose demand could survive the gate, so worth watching for news.

    Querying Google News for all 400-odd topics would spend most of the
    budget on subjects that are dead or saturated and would be rejected
    regardless of what happened to them this week.
    """
    result = classify(store, topics_path)
    return {
        t["slug"] for t in result["topics"]
        if t["state"] in ("proven", "unproven")
        and (t.get("demand_en") or 0) >= 1.0
        and t["n_channels"] >= 2
    }


def google_news_for(topics: list[dict], now: dt.datetime,
                    workers: int = 6) -> list[dict]:
    def one(t):
        q = urllib.parse.quote_plus(f'"{t["label"]}" when:7d')
        try:
            xml = _get(GOOGLE_NEWS.format(q=q)).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return []
        return parse_feed(xml, f"Google News: {t['label']}", now)

    out: list[dict] = []
    with cf.ThreadPoolExecutor(workers) as ex:
        for rows in ex.map(one, topics):
            out += rows
    return out


def dedupe(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for r in sorted(rows, key=lambda r: r["published_at"], reverse=True):
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        out.append(r)
    return out


# Google News renders its titles as "Headline - Publisher". The publisher is
# not part of the story, and matching against it filed a missing-aircraft
# report under Meta because the syndicating site was facebook.com.
_PUBLISHER_SUFFIX = re.compile(r"\s+-\s+[^-]{2,40}$")


def match_text(row: dict) -> str:
    """The part of a headline that is actually the headline."""
    title = row["title"]
    if row.get("source", "").lower().startswith("google news"):
        title = _PUBLISHER_SUFFIX.sub("", title)
    return title


def match_triggers(rows: list[dict], matchers, now: dt.datetime) -> list[dict]:
    """Attach stories to topics, keeping only the ones that are real events."""
    triggers = []
    for r in rows:
        # Match on the headline alone. A summary names every company the
        # story mentions in passing, and treating those as the subject
        # attributed a Volkswagen layoff story to Google.
        text = match_text(r)
        kind, strength = classify_event(text)
        if not kind or strength < MIN_STRENGTH:
            continue
        for topic, rx in matchers:
            if not rx.search(text.lower()):
                continue
            window = WINDOW_DAYS.get(kind, DEFAULT_WINDOW_DAYS)
            decay = max(0.5, 1 - (now - r["published_at"]).days / 20)
            triggers.append({
                "topic_slug": topic["slug"],
                "topic_label": topic["label"],
                "url": r["url"],
                "kind": kind,
                # A story loses force as it ages, so an event from nine days
                # ago should not count as strongly as one from this morning.
                "decay": round(decay, 3),
                "strength": round(strength * decay, 2),
                "detected_at": now,
                "expires_at": r["published_at"] + dt.timedelta(days=window),
                "note": r["title"][:300],
                "published_at": r["published_at"],
            })
    return triggers


def run(store: Store, topics_path: str, use_google: bool = True,
        max_topics: int = 60) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=MAX_AGE_DAYS)

    topics = load_topics(topics_path)
    with store.cursor() as cur:
        store.upsert_topics(cur, topics)

    print(f"fetching {len(FEEDS)} wire feeds", flush=True)
    rows: list[dict] = []
    with cf.ThreadPoolExecutor(8) as ex:
        for got in ex.map(lambda kv: fetch_feed(kv, now), FEEDS.items()):
            rows += got
    print(f"  {len(rows)} stories from wires", flush=True)

    if use_google:
        watch = scoreable_topics(store, topics_path)
        by_slug = {t["slug"]: t for t in topics}
        picked = [by_slug[s] for s in sorted(watch) if s in by_slug][:max_topics]
        print(f"querying Google News for {len(picked)} watchable topics",
              flush=True)
        got = google_news_for(picked, now)
        print(f"  {len(got)} stories", flush=True)
        rows += got

    rows = [r for r in dedupe(rows) if r["published_at"] >= cutoff]
    print(f"{len(rows)} distinct stories in the last {MAX_AGE_DAYS} days",
          flush=True)

    stop = channel_name_tokens(store)
    matchers = build_matchers(topics, stop)
    candidates = match_triggers(rows, matchers, now)
    print(f"{len(candidates)} keyword candidates", flush=True)
    triggers = adjudicate(candidates)

    keep_urls = {t["url"] for t in triggers}
    with store.cursor() as cur:
        store.upsert_news(cur, [r for r in rows if r["url"] in keep_urls])
        store.upsert_triggers(cur, triggers)

    return {"stories": len(rows), "triggers": triggers,
            "topics_hit": len({t["topic_slug"] for t in triggers})}


def main(argv=None):
    p = argparse.ArgumentParser(description="Find news triggers for topics")
    p.add_argument("--dsn")
    p.add_argument("--topics", default=os.path.join(HERE, "topics.yml"))
    p.add_argument("--no-google", action="store_true",
                   help="wire feeds only")
    p.add_argument("--max-topics", type=int, default=60,
                   help="how many topics to query Google News for")
    a = p.parse_args(argv)

    store = Store(a.dsn)
    try:

        store.require_schema()
        r = run(store, a.topics, use_google=not a.no_google,
                max_topics=a.max_topics)
        health = store.health()
    finally:
        store.close()

    print(f"\n{len(r['triggers'])} triggers across {r['topics_hit']} topics\n",
          flush=True)
    seen = set()
    for t in sorted(r["triggers"], key=lambda t: -t["strength"]):
        if t["topic_slug"] in seen:
            continue
        seen.add(t["topic_slug"])
        print(f"  {t['strength']:.2f}  {t['kind']:<12} {t['topic_label'][:24]:<25} "
              f"{t['note'][:58]}", flush=True)
    print("\ndatabase: " + " · ".join(f"{k}={v:,}" for k, v in health.items()),
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
