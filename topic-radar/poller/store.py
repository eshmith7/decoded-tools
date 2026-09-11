"""Storage adapter.

Production is Postgres (Supabase). Local development uses a SQLite file so the
whole pipeline can be run and tested without credentials — chosen deliberately
so that "does the poller work" never depends on a network service being up.

Both backends speak the same handful of statements; anything Postgres-specific
(vector, enum, generated columns) is confined to db/schema.sql and is not used
by the poller.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

DEFAULT_SQLITE = os.path.join(os.path.dirname(__file__), "..", "local.db")


def as_utc(value) -> dt.datetime | None:
    """Normalise a timestamp read back from either backend.

    SQLite hands back the ISO string that was written; Postgres hands back a
    real datetime. Every consumer that forgot this has been a production-only
    crash, because the development path is SQLite and never sees the typed
    form. Anything reading a timestamp out of the database goes through here.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def _iso(value) -> str:
    """Timestamps cross the storage boundary as ISO strings.

    Python 3.12 dropped sqlite3's implicit datetime adapter, and Postgres
    casts an ISO string to timestamptz without complaint, so normalising here
    keeps both backends on the same path.
    """
    return value.isoformat() if isinstance(value, dt.datetime) else value


@dataclass
class Channel:
    id: str
    name: str
    lang: str
    tier: int
    is_own: bool = False
    poll_minutes: int = 30
    median_views: int | None = None
    median_recent: int | None = None
    last_polled: dt.datetime | None = None


class Store:
    """Thin wrapper. `qmark` keeps the two paramstyles apart."""

    def __init__(self, dsn: str | None = None):
        dsn = dsn or os.environ.get("DATABASE_URL") or f"sqlite:///{DEFAULT_SQLITE}"
        self.is_pg = dsn.startswith(("postgres://", "postgresql://"))
        if self.is_pg:
            import psycopg  # noqa: PLC0415

            # Supabase's direct host (db.<ref>.supabase.co) is IPv6-only on new
            # projects, and GitHub Actions runners are IPv4 — so production
            # connects through the pooler host instead. The pooler runs
            # pgbouncer in transaction mode, which cannot hold server-side
            # prepared statements across statements, and psycopg3 creates them
            # automatically after five executions. prepare_threshold=None turns
            # that off; without it the poller works for a few statements and
            # then fails on "prepared statement does not exist".
            self.conn = psycopg.connect(
                dsn, autocommit=False, prepare_threshold=None
            )
            self.ph = "%s"
        else:
            path = dsn.replace("sqlite:///", "")
            self.conn = sqlite3.connect(path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("pragma journal_mode=wal")
            self.conn.execute("pragma foreign_keys=on")
            self.ph = "?"

    def q(self, sql: str) -> str:
        """Rewrite ? placeholders to the backend's style."""
        return sql.replace("?", self.ph) if self.is_pg else sql

    @contextmanager
    def cursor(self):
        cur = self.conn.cursor()
        try:
            yield cur
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    def executescript(self, sql: str):
        if self.is_pg:
            with self.cursor() as cur:
                cur.execute(sql)
        else:
            self.conn.executescript(sql)
            self.conn.commit()

    # ------------------------------------------------------------- channels

    def upsert_channels(self, rows: list[dict]):
        sql = self.q(
            """
            insert into channels (id, handle, name, lang, tier, is_own, poll_minutes)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict (id) do update set
              handle = excluded.handle,
              name = excluded.name,
              lang = excluded.lang,
              tier = excluded.tier,
              is_own = excluded.is_own,
              poll_minutes = excluded.poll_minutes
            """
        )
        with self.cursor() as cur:
            for r in rows:
                cur.execute(
                    sql,
                    (
                        r["id"], r.get("handle"), r["name"], r.get("lang", "en"),
                        int(r.get("tier", 2)), bool(r.get("is_own", False)),
                        int(r.get("poll_minutes", 30)),
                    ),
                )

    def channels_due(self, now: dt.datetime | None = None) -> list[Channel]:
        """Channels whose poll interval has elapsed."""
        now = now or dt.datetime.now(dt.timezone.utc)
        with self.cursor() as cur:
            cur.execute(
                self.q(
                    "select id, name, lang, tier, is_own, poll_minutes, "
                    "median_views, median_recent, last_polled "
                    "from channels where active = "
                    + ("true" if self.is_pg else "1")
                )
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(r) if not self.is_pg else dict(zip(
                ["id", "name", "lang", "tier", "is_own", "poll_minutes",
                 "median_views", "median_recent", "last_polled"], r))
            last = as_utc(d["last_polled"])
            due = last is None or (now - last).total_seconds() >= d["poll_minutes"] * 60
            if due:
                out.append(Channel(
                    id=d["id"], name=d["name"], lang=d["lang"], tier=int(d["tier"]),
                    is_own=bool(d["is_own"]), poll_minutes=int(d["poll_minutes"]),
                    median_views=d["median_views"], median_recent=d["median_recent"],
                    last_polled=last))
        return out

    def mark_polled(self, channel_id: str, when):
        with self.cursor() as cur:
            cur.execute(
                self.q("update channels set last_polled = ? where id = ?"),
                (_iso(when), channel_id),
            )

    def set_baseline(self, channel_id: str, median_views: int | None,
                     median_recent: int | None, video_count: int):
        with self.cursor() as cur:
            cur.execute(
                self.q(
                    "update channels set median_views = ?, median_recent = ?, "
                    "video_count = ? where id = ?"
                ),
                (median_views, median_recent, video_count, channel_id),
            )

    # --------------------------------------------------------------- videos

    VIDEO_UPSERT = """
                insert into videos (id, channel_id, title, description, published_at,
                                    duration_s, views, likes, mult, first_seen, last_seen)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict (id) do update set
                  title = excluded.title,
                  description = coalesce(excluded.description, videos.description),
                  duration_s = coalesce(excluded.duration_s, videos.duration_s),
                  views = coalesce(excluded.views, videos.views),
                  likes = coalesce(excluded.likes, videos.likes),
                  mult = coalesce(excluded.mult, videos.mult),
                  last_seen = excluded.last_seen
                """

    @staticmethod
    def _video_params(v: dict) -> tuple:
        return (
            v["id"], v["channel_id"], v["title"], v.get("description"),
            v["published_at"], v.get("duration_s"), v.get("views"),
            v.get("likes"), v.get("mult"), v["now"], v["now"],
        )

    def upsert_videos(self, cur, rows: list[dict]):
        """Batch upsert.

        One statement per video costs a network round trip each, and the
        database is in another region: the first production poll wrote ~1200
        rows one at a time and took 6m44s, almost all of it latency.
        """
        if rows:
            cur.executemany(self.q(self.VIDEO_UPSERT),
                            [self._video_params(v) for v in rows])

    def last_snapshots(self, cur, video_ids: list[str]) -> dict:
        """Most recent snapshot per video, in one query instead of N."""
        if not video_ids:
            return {}
        marks = ",".join(["?"] * len(video_ids))
        cur.execute(
            self.q(
                f"select s.video_id, s.taken_at, s.views from snapshots s "
                f"join (select video_id, max(taken_at) as m from snapshots "
                f"      where video_id in ({marks}) group by video_id) t "
                f"  on t.video_id = s.video_id and t.m = s.taken_at"
            ),
            tuple(video_ids),
        )
        out = {}
        for vid, taken_at, views in cur.fetchall():
            out[vid] = (as_utc(taken_at), int(views))
        return out

    def add_snapshots(self, cur, rows: list[tuple], prev: dict) -> list[tuple]:
        """Batch insert snapshots, computing views/hour against `prev`.

        Velocity is measured between consecutive snapshots rather than since
        publish: an average since publish lags, and hides a topic that has
        already peaked.
        """
        params, deltas = [], []
        for video_id, taken_at, views, likes in rows:
            delta_vph = None
            if video_id in prev:
                prev_at, prev_views = prev[video_id]
                hours = (taken_at - prev_at).total_seconds() / 3600
                if hours > 0:
                    delta_vph = round((views - prev_views) / hours, 2)
            params.append((video_id, _iso(taken_at), views, likes, delta_vph))
            deltas.append((video_id, delta_vph))
        if params:
            cur.executemany(
                self.q(
                    "insert into snapshots (video_id, taken_at, views, likes, "
                    "delta_vph) values (?, ?, ?, ?, ?) "
                    "on conflict (video_id, taken_at) do nothing"
                ),
                params,
            )
        return deltas

    def upsert_video(self, cur, v: dict):
        cur.execute(
            self.q(
                """
                insert into videos (id, channel_id, title, description, published_at,
                                    duration_s, views, likes, mult, first_seen, last_seen)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict (id) do update set
                  title = excluded.title,
                  description = coalesce(excluded.description, videos.description),
                  duration_s = coalesce(excluded.duration_s, videos.duration_s),
                  views = coalesce(excluded.views, videos.views),
                  likes = coalesce(excluded.likes, videos.likes),
                  mult = coalesce(excluded.mult, videos.mult),
                  last_seen = excluded.last_seen
                """
            ),
            (
                v["id"], v["channel_id"], v["title"], v.get("description"),
                v["published_at"], v.get("duration_s"), v.get("views"),
                v.get("likes"), v.get("mult"), v["now"], v["now"],
            ),
        )

    def known_video_ids(self, channel_id: str) -> set[str]:
        with self.cursor() as cur:
            cur.execute(
                self.q("select id from videos where channel_id = ?"), (channel_id,)
            )
            return {r[0] for r in cur.fetchall()}

    def longform_views(self, channel_id: str, limit: int | None = None) -> list[int]:
        sql = (
            "select views from videos where channel_id = ? and views is not null "
            "and duration_s >= 480 order by published_at desc"
        )
        if limit:
            sql += f" limit {int(limit)}"
        with self.cursor() as cur:
            cur.execute(self.q(sql), (channel_id,))
            return [int(r[0]) for r in cur.fetchall()]

    # ------------------------------------------------------------ snapshots

    def last_snapshot(self, cur, video_id: str):
        cur.execute(
            self.q(
                "select taken_at, views from snapshots where video_id = ? "
                "order by taken_at desc limit 1"
            ),
            (video_id,),
        )
        return cur.fetchone()

    def add_snapshot(self, cur, video_id: str, taken_at: dt.datetime,
                     views: int, likes: int | None):
        """Records a point and the true views/hour since the previous point.

        Velocity since *publish* is a lagging average and hides a topic that
        has already peaked; velocity between consecutive snapshots does not.
        """
        prev = self.last_snapshot(cur, video_id)
        delta_vph = None
        if prev:
            prev_at, prev_views = as_utc(prev[0]), int(prev[1])
            hours = (taken_at - prev_at).total_seconds() / 3600
            if hours > 0:
                delta_vph = round((views - prev_views) / hours, 2)
        cur.execute(
            self.q(
                "insert into snapshots (video_id, taken_at, views, likes, delta_vph) "
                "values (?, ?, ?, ?, ?) on conflict (video_id, taken_at) do nothing"
            ),
            (video_id, _iso(taken_at), views, likes, delta_vph),
        )
        return delta_vph

    def health(self) -> dict:
        """Row counts and coverage, printed at the end of every run.

        The database is reachable only from the runner, so these numbers in
        the job log are the only view anyone has of whether data is actually
        landing. Cheap enough to run every time.
        """
        out = {}
        with self.cursor() as cur:
            for name, sql in (
                ("channels", "select count(*) from channels"),
                ("videos", "select count(*) from videos"),
                ("longform", "select count(*) from videos where duration_s >= 480"),
                ("with_duration", "select count(*) from videos where duration_s is not null"),
                ("with_baseline", "select count(*) from channels where median_recent is not null"),
                ("snapshots", "select count(*) from snapshots"),
                ("with_velocity", "select count(*) from snapshots where delta_vph is not null"),
            ):
                cur.execute(sql)
                out[name] = cur.fetchone()[0]
        return out

    def close(self):
        self.conn.close()
