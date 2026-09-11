# The app

What the team opens to see the shortlist and pick a topic. Plain HTML, CSS
and one JavaScript file — no build step, no framework, nothing to compile.
Free to host and free to run.

It does not score anything. Every number it shows was computed in Python by
`poller/score.py` and written to the database by `poller/publish.py`; the app
reads those rows and records a decision. Keeping the rules in one language is
deliberate — two copies of the gate would disagree the first time a threshold
moved.

## Seeing it before deploying anything

```bash
cd app
python3 -m http.server 8000
```

Open <http://localhost:8000>. With no Supabase details configured it runs on
`fixture.json`, a real shortlist exported from the development database, and
labels itself **Demo data** on screen so nobody mistakes it for live.

To refresh that fixture from your own local database:

```bash
cd topic-radar
python3 poller/publish.py --dsn "sqlite:///$PWD/local.db"
python3 poller/export_fixture.py --dsn "sqlite:///$PWD/local.db"
```

---

# Deploying it

Four steps. Nothing here costs money.

## 1. Turn on the team list

The app signs people in with an emailed link, and **a link is not an
invitation** — anyone who types an email address into the login box gets a
valid session. So access is decided by a list you control, not by being
signed in. Everything is invisible to anyone not on it.

1. Open <https://supabase.com/dashboard> → your project
2. Left sidebar → **SQL Editor** → **+ New query**
3. Paste all of `topic-radar/db/schema.sql` and press **Run**

You have run this file before; it is safe to run again, and the bottom
section that creates the team list is new.

## 2. Add your people

Same SQL Editor, new query. One row per person, using the email address they
will sign in with:

```sql
insert into team_members (email, name, role) values
  ('asif@houseofedtech.in',    'Asif',    'lead'),
  ('tanya@houseofedtech.in',   'Tanya',   'lead'),
  ('prajwal@houseofedtech.in', 'Prajwal', 'lead')
on conflict (email) do nothing;
```

Press **Run**. Removing someone later is one line:

```sql
delete from team_members where email = 'someone@houseofedtech.in';
```

Their existing session keeps working but every query returns nothing.

## 3. Point the app at your project

1. Supabase → **Project Settings** (gear, bottom left) → **Data API**
2. Copy the **Project URL**
3. Copy the **anon public** key — the one labelled `anon`, **not**
   `service_role`
4. Open `app/config.js` and paste both in:

```js
window.TOPIC_RADAR_CONFIG = {
  supabaseUrl: "https://zdhujpsphjcyxhocdjys.supabase.co",
  supabaseAnonKey: "eyJhbGciOi...",
};
```

Both are safe in public code. The anon key is built to ship to browsers; on
its own it grants nothing, because every table is behind the policies from
step 1.

> **The one that is not safe** is the `service_role` key. It ignores those
> policies completely. It belongs only in GitHub secrets, where the pollers
> read it. If it ever lands in this file, rotate it.

Commit the change:

```bash
git add app/config.js && git commit -m "Point the app at Supabase" && git push
```

## 4. Put it online

1. Go to <https://dash.cloudflare.com> and sign up if you have not
2. Left sidebar → **Workers & Pages** → **Create** → **Pages** tab →
   **Connect to Git**
3. Authorise GitHub and choose **eshmith7/decoded-tools**
4. On the build settings screen:
   - **Framework preset**: `None`
   - **Build command**: leave empty
   - **Build output directory**: `app`
5. **Save and Deploy**

A minute later you get a URL like `decoded-tools.pages.dev`. Every push to
`main` redeploys automatically.

*Cloudflare rather than Vercel because Vercel's free Hobby tier is licensed
for non-commercial use only, and Be10x is a business.*

## 5. Let Supabase accept the login links

Supabase will not send people back to a site it does not recognise.

1. Supabase → **Authentication** → **URL Configuration**
2. **Site URL**: your Pages URL, e.g. `https://decoded-tools.pages.dev`
3. **Redirect URLs**: add both
   - `https://decoded-tools.pages.dev`
   - `http://localhost:8000` *(so local development still works)*
4. **Save**

Now open the site, enter an email that is in `team_members`, and the link
will arrive.

---

## Where the shortlist comes from

The app never computes one. The `shortlist` workflow does, daily at 02:40 UTC
and whenever you run it by hand, and stores it for the app to read.

To produce a fresh one right now:
**Actions → shortlist → Run workflow**

The app's **Refresh** button re-reads the newest stored shortlist; it does not
trigger a new computation. That separation is on purpose — triggering a
workflow from a browser needs a GitHub token, and a token in a public web page
is a token anyone can use.

## What the team can and cannot do

| | |
|---|---|
| Read the shortlist, topics and evidence | yes, if on the team list |
| Mark a topic picked or passed, with a reason | yes |
| Change a score, its evidence or its reasoning | no — those columns are not granted |
| See anything at all while not on the list | no |

The last two are enforced by the database, not by the page, so they hold even
if someone calls the API directly.

## Checking it still works

```bash
node app/render.test.mjs
```

Runs the rendering helpers over the real fixture: that every stored candidate
produces a complete card, that ages and multiples format correctly, that
channel and video titles are escaped before reaching the page, and that no
credentials have been committed to `config.js`.
