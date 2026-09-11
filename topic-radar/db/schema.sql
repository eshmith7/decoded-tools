-- Topic Radar — schema
-- Postgres / Supabase. Designed to stay inside the 500MB free tier:
-- snapshots are only taken for videos still young enough for velocity to
-- mean anything, and are rolled up to daily resolution after a week.

create extension if not exists pgcrypto;
create extension if not exists vector;

-- ---------------------------------------------------------------- channels

do $$ begin
  create type channel_lang as enum ('en', 'hi', 'mixed');
exception when duplicate_object then null;
end $$;

create table if not exists channels (
  id            text primary key,               -- YouTube UCxxxx
  handle        text,
  name          text not null,
  lang          channel_lang not null default 'en',
  tier          smallint not null default 2,    -- 1 = direct benchmark, 2 = peer, 3 = wide net
  is_own        boolean not null default false, -- true for Decoded itself
  -- Poll cadence is per channel: Zee Business posts a dozen a day and would
  -- push items out of its 15-item RSS window; Think School posts twice a week.
  poll_minutes  int not null default 30,
  -- Rolling baseline, recomputed nightly. Every performance number in the
  -- product is relative to this, never to raw views.
  median_views  bigint,
  median_recent bigint,                         -- last 20 long-form uploads
  video_count   int not null default 0,
  last_polled   timestamptz,
  added_at      timestamptz not null default now(),
  active        boolean not null default true
);

create index if not exists channels_active_poll on channels (active, last_polled);

-- ------------------------------------------------------------------ videos

create table if not exists videos (
  id            text primary key,               -- YouTube video id
  channel_id    text not null references channels(id) on delete cascade,
  title         text not null,
  description   text,
  published_at  timestamptz not null,           -- exact, from RSS
  duration_s    int,
  is_short      boolean generated always as (duration_s is not null and duration_s < 480) stored,
  -- latest observed values, denormalised so the app never has to touch snapshots
  views         bigint,
  likes         bigint,
  comments      bigint,
  -- performance relative to the channel's own baseline at time of measurement
  mult          numeric(8,3),
  first_seen    timestamptz not null default now(),
  last_seen     timestamptz not null default now()
);

create index if not exists videos_channel_pub on videos (channel_id, published_at desc);
create index if not exists videos_pub on videos (published_at desc);
create index if not exists videos_mult on videos (mult desc nulls last);

-- --------------------------------------------------------------- snapshots
-- The compounding asset. One poll gives views-since-publish; two give
-- acceleration, which is the only way to tell a topic still climbing from
-- one that has already peaked.

create table if not exists snapshots (
  video_id   text not null references videos(id) on delete cascade,
  taken_at   timestamptz not null,
  views      bigint not null,
  likes      bigint,
  -- views/hour since the previous snapshot, not since publish
  delta_vph  numeric(12,2),
  primary key (video_id, taken_at)
);

-- Only videos under this age get high-resolution snapshots.
create index if not exists snapshots_recent on snapshots (taken_at desc);

-- ------------------------------------------------------------------ topics
-- Entity-level, per the product decision: "BlackRock" is one topic. Framings
-- and angles hang off it rather than splitting it.

create table if not exists topics (
  id            uuid primary key default gen_random_uuid(),
  slug          text unique not null,
  label         text not null,
  category      text,                           -- company | geopolitics | macro | policy | person | sector
  aliases       text[] not null default '{}',   -- match patterns, lowercase
  embedding     vector(768),
  -- demand evidence, recomputed nightly from video_topics
  n_videos      int not null default 0,
  n_channels    int not null default 0,
  demand_en     numeric(8,3),                   -- median mult across English coverage
  demand_hi     numeric(8,3),                   -- ... and Hindi. Kept apart on purpose:
  -- measured correlation between the two is only r=0.23, so pooling them
  -- and ranking would actively mislead an English channel.
  best_mult     numeric(8,3),
  last_covered  timestamptz,                    -- most recent video by anyone
  peak_covered  timestamptz,                    -- when the best-performing video landed
  -- lifecycle, derived: 'proven' | 'saturated' | 'decaying' | 'dead' | 'unproven'
  state         text,
  created_at    timestamptz not null default now()
);

create index if not exists topics_demand on topics (demand_en desc nulls last);
create index if not exists topics_state on topics (state);

create table if not exists video_topics (
  video_id   text not null references videos(id) on delete cascade,
  topic_id   uuid not null references topics(id) on delete cascade,
  -- how the video framed it, e.g. 'collapse', 'hidden-owner', 'rise-of'
  framing    text,
  confidence numeric(3,2),
  source     text not null default 'dict',      -- dict | llm | manual
  primary key (video_id, topic_id)
);

create index if not exists video_topics_topic on video_topics (topic_id);

-- ---------------------------------------------------------------- triggers
-- Fresh news on a topic. The evidence says a proven evergreen topic with no
-- trigger decays (Nokia: 13.2M -> 53K), and a trigger with no evergreen
-- depth clusters once and dies (Jet Airways). Winners need both.

create table if not exists news_items (
  id           uuid primary key default gen_random_uuid(),
  source       text not null,
  url          text unique not null,
  title        text not null,
  summary      text,
  published_at timestamptz not null,
  fetched_at   timestamptz not null default now(),
  embedding    vector(768)
);

create index if not exists news_published on news_items (published_at desc);

create table if not exists triggers (
  id           uuid primary key default gen_random_uuid(),
  topic_id     uuid not null references topics(id) on delete cascade,
  news_id      uuid references news_items(id) on delete set null,
  kind         text,          -- filing | collapse | acquisition | policy | exit | price-war | verdict
  -- What happened, in plain words: "SEBI settled with Adani Ports". The
  -- adjudicator has to produce one before a trigger is confirmed, because a
  -- headline it cannot summarise as a completed action is describing a state
  -- of affairs rather than an event. Reads better on a card than the raw
  -- headline, which is kept in `note` as the source.
  event        text,
  strength     numeric(3,2),
  detected_at  timestamptz not null default now(),
  expires_at   timestamptz,   -- how long the window stays open
  note         text
);

create index if not exists triggers_topic on triggers (topic_id, detected_at desc);

-- ------------------------------------------------------------ shortlists
-- What a lead actually sees, and — crucially — what they picked, so the
-- scoring can be graded against real outcomes later.

create table if not exists shortlists (
  id           uuid primary key default gen_random_uuid(),
  mode         text not null default 'decoded',  -- decoded | ai_seekho
  requested_by uuid,                             -- auth.users
  created_at   timestamptz not null default now()
);

create table if not exists candidates (
  id           uuid primary key default gen_random_uuid(),
  shortlist_id uuid not null references shortlists(id) on delete cascade,
  topic_id     uuid not null references topics(id),
  rank         int not null,
  score        numeric(6,3) not null,
  -- the score broken into its parts, so a lead can see *why* it ranked here
  score_parts  jsonb not null default '{}',
  angle        text,          -- the question the video would answer
  hook         text,          -- suggested working title
  reason       text,          -- one line on why it ranked
  evidence     jsonb not null default '{}',  -- the who-covered-it-when table
  trigger_id   uuid references triggers(id),
  status       text not null default 'shown',  -- shown | picked | rejected
  reject_note  text,          -- why, in the lead's words. This is the training signal.
  decided_by   uuid,
  decided_at   timestamptz
);

create index if not exists candidates_shortlist on candidates (shortlist_id, rank);
create index if not exists candidates_status on candidates (status);

-- Closes the loop: once a picked topic ships, compare what actually happened
-- against what we predicted. This is the feedback the original plan lacked.
create table if not exists outcomes (
  candidate_id uuid primary key references candidates(id) on delete cascade,
  video_id     text references videos(id),
  published_at timestamptz,
  views_30d    bigint,
  mult_30d     numeric(8,3),
  predicted    numeric(8,3),
  note         text
);

-- ------------------------------------------------------------ app access
-- Everything below exists because the web app signs people in with a magic
-- link, and a magic link is not an invitation: anyone who types an email
-- address into the login box gets a valid session. Being signed in therefore
-- proves nothing, and access has to be decided by membership instead.
--
-- Add a teammate by inserting their email here. Remove them by deleting the
-- row — their session keeps working but every query returns nothing.

create table if not exists team_members (
  email      text primary key,
  name       text,
  role       text not null default 'lead',   -- lead | viewer
  added_at   timestamptz not null default now()
);

-- The check every policy hangs off. Matching on the JWT's email rather than
-- its user id means a teammate can be authorised before they have ever
-- signed in, which is what makes "add the row, send them the link" work.
create or replace function is_team_member() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from team_members
    where lower(email) = lower(coalesce(auth.jwt() ->> 'email', ''))
  );
$$;

alter table team_members enable row level security;
alter table shortlists   enable row level security;
alter table candidates   enable row level security;
alter table topics       enable row level security;

-- Policies are additive in Postgres, so a table with RLS on and no policy
-- denies everything. The pollers connect as the service role, which bypasses
-- RLS entirely and is unaffected by all of this.
drop policy if exists team_reads_itself on team_members;
create policy team_reads_itself on team_members
  for select to authenticated using (is_team_member());

drop policy if exists team_reads_shortlists on shortlists;
create policy team_reads_shortlists on shortlists
  for select to authenticated using (is_team_member());

drop policy if exists team_reads_topics on topics;
create policy team_reads_topics on topics
  for select to authenticated using (is_team_member());

drop policy if exists team_reads_candidates on candidates;
create policy team_reads_candidates on candidates
  for select to authenticated using (is_team_member());

-- The one write the app is allowed to make.
drop policy if exists team_decides_candidates on candidates;
create policy team_decides_candidates on candidates
  for update to authenticated using (is_team_member()) with check (is_team_member());

-- A policy says which rows; a grant says which columns. Without the column
-- list a lead could rewrite a candidate's score or evidence from the browser
-- and the stored reasoning would no longer be what the scoring produced.
revoke all on candidates from authenticated;
grant select on candidates to authenticated;
grant update (status, reject_note, decided_by, decided_at) on candidates to authenticated;

revoke all on shortlists, topics, team_members from authenticated;
grant select on shortlists, topics, team_members to authenticated;

-- Nothing is readable without signing in.
revoke all on candidates, shortlists, topics, team_members from anon;
