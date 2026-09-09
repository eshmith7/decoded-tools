"""YouTube data access without an API key.

Three feeds, all unauthenticated:

  rss_feed()        exact publish timestamps + live view/like counts, last 15
                    uploads. The freshest thing YouTube exposes, and the only
                    place that gives a real timestamp rather than "4 days ago".
  channel_videos()  full back catalogue via the internal browse endpoint.
  search()          who else covered a topic, when, and how it performed.

The official Data API v3 is a worse fit: 10k units/day and search costs 100
units a call, which is about 100 searches a day. These have no such ceiling.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Public web client key. Not a secret — it ships in every youtube.com page.
INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
INNERTUBE_CTX = {
    "client": {
        "clientName": "WEB",
        "clientVersion": "2.20260904.01.00",
        "hl": "en",
        "gl": "IN",
    }
}

# browse params for a channel's Videos tab, newest first
TAB_VIDEOS = urllib.parse.unquote("EgZ2aWRlb3PyBgQKAjoA")

MIN_LONGFORM_S = 480  # 8 minutes; below this it's a short or a clip


class TransientError(RuntimeError):
    """Worth retrying: rate limit, timeout, malformed response."""


@dataclass
class Video:
    id: str
    title: str
    channel_id: str | None = None
    published_at: dt.datetime | None = None
    duration_s: int | None = None
    views: int | None = None
    likes: int | None = None
    description: str | None = None
    # only present on search/browse results, where YouTube gives "4 days ago"
    age_text: str | None = None

    @property
    def is_longform(self) -> bool:
        return self.duration_s is not None and self.duration_s >= MIN_LONGFORM_S


def _get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (429, 500, 502, 503, 504):
            raise TransientError(f"HTTP {e.code} for {url}") from e
        raise
    except (urllib.error.URLError, TimeoutError) as e:
        raise TransientError(str(e)) from e


def _post_innertube(endpoint: str, payload: dict, timeout: int = 25) -> dict:
    req = urllib.request.Request(
        f"https://www.youtube.com/youtubei/v1/{endpoint}?key={INNERTUBE_KEY}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (429, 500, 502, 503, 504):
            raise TransientError(f"HTTP {e.code} on {endpoint}") from e
        raise
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise TransientError(str(e)) from e


def with_retry(fn, *args, attempts: int = 3, base: float = 1.5, **kwargs):
    last = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except TransientError as e:
            last = e
            if i < attempts - 1:
                time.sleep(base * (2**i))
    raise last  # type: ignore[misc]


# --------------------------------------------------------------------- RSS

_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)


def _tag(entry: str, name: str) -> str | None:
    m = re.search(rf"<{name}>(.*?)</{name}>", entry, re.S)
    return m.group(1) if m else None


def _unescape(s: str | None) -> str | None:
    if s is None:
        return None
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(a, b)
    return s


def rss_feed(channel_id: str) -> list[Video]:
    """Last ~15 uploads with exact timestamps and live counts.

    Note the 15-item cap: a channel that uploads more often than we poll will
    push items out of the window before we ever see them, which is why poll
    cadence is configured per channel.
    """
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    raw = _get(url).decode("utf-8", "replace")
    out: list[Video] = []
    for entry in _ENTRY_RE.findall(raw):
        vid = _tag(entry, "yt:videoId")
        title = _unescape(_tag(entry, "title"))
        pub = _tag(entry, "published")
        if not (vid and title and pub):
            continue
        views = re.search(r'views="(\d+)"', entry)
        likes = re.search(r'starRating count="(\d+)"', entry)
        out.append(
            Video(
                id=vid,
                title=title,
                channel_id=_tag(entry, "yt:channelId") or channel_id,
                published_at=dt.datetime.fromisoformat(pub),
                views=int(views.group(1)) if views else None,
                likes=int(likes.group(1)) if likes else None,
                description=_unescape(_tag(entry, "media:description")),
            )
        )
    return out


# ---------------------------------------------------------------- innertube


def _walk(obj, key: str, acc: list):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                acc.append(v)
            _walk(v, key, acc)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, key, acc)
    return acc


def _continuation(doc: dict) -> str | None:
    # Take the token from a continuationItemRenderer specifically. Grabbing any
    # "token" in the response picks up unrelated ones and silently stops
    # pagination after the first page.
    for cir in _walk(doc, "continuationItemRenderer", []):
        toks = _walk(cir, "token", [])
        if toks:
            return toks[0]
    return None


def parse_duration(text: str | None) -> int | None:
    if not text:
        return None
    try:
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return None


def parse_views(text: str | None) -> int | None:
    """'1.2M views' / '7,912 views' -> int."""
    if not text:
        return None
    s = text.replace("views", "").replace(",", "").strip()
    try:
        if s.endswith("M"):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith("K"):
            return int(float(s[:-1]) * 1_000)
        return int(float(s))
    except ValueError:
        return None


_AGE_UNITS = {
    "hour": 1 / 24, "hours": 1 / 24, "day": 1, "days": 1,
    "week": 7, "weeks": 7, "month": 30, "months": 30,
    "year": 365, "years": 365,
}


def parse_age_days(text: str | None) -> float | None:
    """'4 days ago' -> 4.0. Coarse by nature — prefer RSS timestamps."""
    if not text:
        return None
    m = re.match(r"(\d+)\s+(\w+)", text)
    if not m:
        return None
    unit = _AGE_UNITS.get(m.group(2))
    return int(m.group(1)) * unit if unit else None


def _parse_lockup(lockup: dict) -> Video | None:
    vid = lockup.get("contentId")
    meta = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = meta.get("title", {}).get("content")
    if not (vid and title):
        return None
    texts = [
        t.get("content")
        for t in _walk(meta.get("metadata", {}), "text", [])
        if isinstance(t, dict) and t.get("content")
    ]
    views = age = None
    for t in texts:
        if "view" in t:
            views = parse_views(t)
        elif "ago" in t:
            age = t
    duration = None
    for badge in _walk(lockup.get("contentImage", {}), "thumbnailBadgeViewModel", []):
        if str(badge.get("badgeStyle", "")).startswith("THUMBNAIL_OVERLAY_BADGE"):
            duration = parse_duration(badge.get("text"))
    return Video(id=vid, title=title, views=views, age_text=age, duration_s=duration)


def channel_videos(channel_id: str, max_pages: int = 40,
                   pause: float = 0.25) -> list[Video]:
    """Full back catalogue, newest first."""
    payload = {"context": INNERTUBE_CTX, "browseId": channel_id, "params": TAB_VIDEOS}
    seen: set[str] = set()
    out: list[Video] = []
    for _ in range(max_pages):
        doc = with_retry(_post_innertube, "browse", payload)
        for lockup in _walk(doc, "lockupViewModel", []):
            v = _parse_lockup(lockup)
            if v and v.id not in seen:
                seen.add(v.id)
                v.channel_id = channel_id
                out.append(v)
        token = _continuation(doc)
        if not token:
            break
        payload = {"context": INNERTUBE_CTX, "continuation": token}
        time.sleep(pause)
    return out


def search(query: str, max_pages: int = 1, pause: float = 0.4) -> list[Video]:
    """Demand archaeology: who covered this topic, when, how it did."""
    payload = {"context": INNERTUBE_CTX, "query": query}
    seen: set[str] = set()
    out: list[Video] = []
    for _ in range(max_pages):
        doc = with_retry(_post_innertube, "search", payload)
        for vr in _walk(doc, "videoRenderer", []):
            vid = vr.get("videoId")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            title = vr.get("title", {})
            text = title.get("simpleText") or "".join(
                r.get("text", "") for r in title.get("runs", [])
            )
            owner = vr.get("ownerText", {}).get("runs", [{}])[0]
            out.append(
                Video(
                    id=vid,
                    title=text,
                    channel_id=owner.get("navigationEndpoint", {})
                    .get("browseEndpoint", {})
                    .get("browseId"),
                    views=parse_views(vr.get("viewCountText", {}).get("simpleText")),
                    age_text=vr.get("publishedTimeText", {}).get("simpleText"),
                    duration_s=parse_duration(vr.get("lengthText", {}).get("simpleText")),
                )
            )
        token = _continuation(doc)
        if not token:
            break
        payload = {"context": INNERTUBE_CTX, "continuation": token}
        time.sleep(pause)
    return out


def resolve_handle(handle: str) -> str | None:
    """'@ThinkSchool' -> 'UCKZoz...'."""
    handle = handle.lstrip("@")
    raw = _get(f"https://www.youtube.com/@{handle}").decode("utf-8", "replace")
    m = re.search(r'externalId":"(UC[A-Za-z0-9_-]+)', raw)
    return m.group(1) if m else None
