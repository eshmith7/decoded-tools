-- Local development mirror of db/schema.sql.
-- Same tables and columns, minus the Postgres-only pieces (uuid defaults,
-- pgvector, enums). Kept in step with the real schema by hand; the poller only
-- touches columns that exist in both.

create table if not exists channels (
  id            text primary key,
  handle        text,
  name          text not null,
  lang          text not null default 'en',
  tier          integer not null default 2,
  is_own        integer not null default 0,
  poll_minutes  integer not null default 30,
  median_views  integer,
  median_recent integer,
  video_count   integer not null default 0,
  last_polled   text,
  added_at      text not null default (datetime('now')),
  active        integer not null default 1
);

create table if not exists videos (
  id           text primary key,
  channel_id   text not null references channels(id) on delete cascade,
  title        text not null,
  description  text,
  published_at text not null,
  duration_s   integer,
  views        integer,
  likes        integer,
  comments     integer,
  mult         real,
  first_seen   text not null,
  last_seen    text not null
);
create index if not exists videos_channel_pub on videos (channel_id, published_at desc);
create index if not exists videos_mult on videos (mult desc);

create table if not exists snapshots (
  video_id  text not null references videos(id) on delete cascade,
  taken_at  text not null,
  views     integer not null,
  likes     integer,
  delta_vph real,
  primary key (video_id, taken_at)
);
create index if not exists snapshots_recent on snapshots (taken_at desc);

create table if not exists topics (
  id           text primary key,
  slug         text unique not null,
  label        text not null,
  category     text,
  aliases      text not null default '[]',
  n_videos     integer not null default 0,
  n_channels   integer not null default 0,
  demand_en    real,
  demand_hi    real,
  best_mult    real,
  last_covered text,
  peak_covered text,
  state        text,
  created_at   text not null default (datetime('now'))
);

create table if not exists video_topics (
  video_id   text not null references videos(id) on delete cascade,
  topic_id   text not null references topics(id) on delete cascade,
  framing    text,
  confidence real,
  source     text not null default 'dict',
  primary key (video_id, topic_id)
);
create index if not exists video_topics_topic on video_topics (topic_id);

-- What a lead actually sees, and what they picked. Mirrors db/schema.sql.
-- Ids are generated in Python rather than by the database, so one code path
-- serves both backends.

create table if not exists shortlists (
  id           text primary key,
  mode         text not null default 'decoded',
  requested_by text,
  created_at   text not null default (datetime('now'))
);

create table if not exists candidates (
  id           text primary key,
  shortlist_id text not null references shortlists(id) on delete cascade,
  topic_id     text not null references topics(id),
  rank         integer not null,
  score        real not null,
  score_parts  text not null default '{}',
  angle        text,
  hook         text,
  reason       text,
  evidence     text not null default '{}',
  trigger_id   text,
  status       text not null default 'shown',
  reject_note  text,
  decided_by   text,
  decided_at   text
);

create index if not exists candidates_shortlist on candidates (shortlist_id, rank);
create index if not exists candidates_status on candidates (status);

create table if not exists outcomes (
  candidate_id text primary key references candidates(id) on delete cascade,
  video_id     text references videos(id),
  published_at text,
  views_30d    integer,
  mult_30d     real,
  predicted    real,
  note         text
);

create table if not exists news_items (
  id           text primary key,
  source       text not null,
  url          text unique not null,
  title        text not null,
  summary      text,
  published_at text not null,
  fetched_at   text not null
);
create index if not exists news_published on news_items (published_at desc);

create table if not exists triggers (
  id          text primary key,
  topic_id    text not null references topics(id) on delete cascade,
  news_id     text references news_items(id) on delete set null,
  kind        text,
  event       text,
  strength    real,
  detected_at text not null,
  expires_at  text,
  note        text
);
create index if not exists triggers_topic on triggers (topic_id, detected_at desc);
