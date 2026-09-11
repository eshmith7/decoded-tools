/* Topic Radar — the team's view of the shortlist.
 *
 * This file renders and records. It does not decide. Every score, threshold
 * and rejection reason was computed in Python and written to the database by
 * poller/publish.py, and the browser only reads those rows back. Re-deriving
 * any of it here would put the gate in two languages, and the two would
 * disagree the first time a threshold moved.
 *
 * Without config.js filled in, the page runs on a fixture exported from the
 * development database so the layout can be worked on offline. That mode is
 * labelled on screen, because a demo that looks like live data is worse than
 * no demo.
 */

const CFG = window.TOPIC_RADAR_CONFIG || {};
const LIVE = Boolean(CFG.supabaseUrl && CFG.supabaseAnonKey);

const $ = (id) => document.getElementById(id);
const el = {
  signin: $("signin"), signinForm: $("signin-form"), signinNote: $("signin-note"),
  signinBtn: $("signin-btn"), email: $("email"),
  notmember: $("notmember"), notmemberEmail: $("notmember-email"),
  shortlist: $("shortlist"), cards: $("cards"), meta: $("meta"),
  loading: $("loading"), error: $("error"), empty: $("empty"),
  topbarRight: $("topbar-right"), who: $("who"),
};

let supabase = null;
let session = null;

/* ------------------------------------------------------------ utilities */

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/** "24 mo ago" reads faster than a date when the point is recency. */
function ago(days) {
  if (days == null) return "—";
  if (days < 45) return `${Math.round(days)}d ago`;
  const months = days / 30;
  if (months < 24) return `${Math.round(months)}mo ago`;
  return `${(months / 12).toFixed(1)}y ago`;
}

const fmtMult = (m) => (m == null ? "—" : `${Number(m).toFixed(2)}×`);

function show(node, visible = true) { node.hidden = !visible; }

function fail(message) {
  show(el.loading, false);
  el.error.textContent = message;
  show(el.error, true);
}

/* --------------------------------------------------------------- data */

/** Latest shortlist plus its candidates, newest first. */
async function loadLatest() {
  if (!LIVE) {
    const res = await fetch("fixture.json");
    if (!res.ok) throw new Error("No fixture.json — run publish.py --export");
    return res.json();
  }

  const { data: lists, error: e1 } = await supabase
    .from("shortlists")
    .select("id, mode, created_at")
    .order("created_at", { ascending: false })
    .limit(1);
  if (e1) throw e1;
  if (!lists?.length) return { shortlist: null, candidates: [] };

  const { data: rows, error: e2 } = await supabase
    .from("candidates")
    .select("id, rank, score, score_parts, reason, evidence, status, reject_note, decided_at, topics(slug, label, category, state)")
    .eq("shortlist_id", lists[0].id)
    .order("rank");
  if (e2) throw e2;

  return { shortlist: lists[0], candidates: rows ?? [] };
}

async function decide(candidate, status, note) {
  const patch = {
    status,
    reject_note: status === "rejected" ? (note || null) : null,
    decided_by: session?.user?.id ?? null,
    decided_at: new Date().toISOString(),
  };
  if (!LIVE) return patch;            // demo mode: state lives in the page only
  const { error } = await supabase
    .from("candidates").update(patch).eq("id", candidate.id);
  if (error) throw error;
  return patch;
}

/* ------------------------------------------------------------ rendering */

/* Offered as starting points, not as the answer. Each one drops editable
   text into the box, because the sentence a lead writes afterwards is the
   part worth having — a tag tells us a topic was rejected, the sentence
   tells us what the rules got wrong. */
const REASONS = [
  "Already on the plan",
  "No strong angle",
  "Too niche for us",
  "Not enough to fill 15 minutes",
  "Wrong audience",
  "Feels stale",
];

function evidenceRows(evidence) {
  if (!Array.isArray(evidence) || !evidence.length) {
    return `<tr><td colspan="4" class="vid">No coverage recorded.</td></tr>`;
  }
  return evidence.map((e) => {
    const strong = e.mult != null && Number(e.mult) >= 2;
    const link = e.video_id
      ? `<a href="https://www.youtube.com/watch?v=${esc(e.video_id)}" target="_blank" rel="noopener">${esc(e.title)}</a>`
      : esc(e.title);
    return `<tr>
      <td class="chan">${esc(e.channel)}</td>
      <td class="when">${esc(ago(e.age_days))}</td>
      <td class="mult r${strong ? " hi" : ""}">${esc(fmtMult(e.mult))}</td>
      <td class="vid">${link}</td>
    </tr>`;
  }).join("");
}

function card(c, index) {
  const t = c.topics || {};
  const parts = typeof c.score_parts === "string"
    ? JSON.parse(c.score_parts || "{}") : (c.score_parts || {});
  const evidence = typeof c.evidence === "string"
    ? JSON.parse(c.evidence || "[]") : (c.evidence || []);
  const notes = parts.notes || [];

  const decided = c.status === "picked" || c.status === "rejected";
  const node = document.createElement("article");
  node.className = "card" + (c.status === "picked" ? " is-picked"
    : c.status === "rejected" ? " is-rejected" : "");
  node.dataset.id = c.id;

  node.innerHTML = `
    <div class="card-head">
      <div class="rank">${String(index + 1).padStart(2, "0")}</div>
      <div class="title-wrap">
        <h2 class="title">${esc(t.label || "Untitled topic")}</h2>
        <div class="tags">
          ${t.category ? `<span class="tag">${esc(t.category)}</span>` : ""}
          ${t.state ? `<span class="tag tag-state">${esc(t.state)}</span>` : ""}
          ${parts.n_channels != null ? `<span class="tag">${esc(parts.n_channels)} channels</span>` : ""}
        </div>
      </div>
      <div class="score">
        <span class="score-v">${Number(c.score).toFixed(2)}</span>
        <span class="score-k">score</span>
      </div>
    </div>

    ${notes.length ? `<p class="why">${notes.map(esc).join(" · ")}</p>` : ""}

    <div class="ev-head"><span>Who has covered it</span></div>
    <table class="ev">
      <thead>
        <tr>
          <th>Channel</th><th>When</th><th class="r">vs their median</th>
          <th class="vid-h">Video</th>
        </tr>
      </thead>
      <tbody>${evidenceRows(evidence)}</tbody>
    </table>

    <div class="actions"></div>
  `;

  const actions = node.querySelector(".actions");
  renderActions(actions, node, c, decided);
  return node;
}

function renderActions(actions, node, c, decided) {
  actions.innerHTML = "";

  if (decided) {
    const v = document.createElement("div");
    v.className = `verdict ${c.status}`;
    v.innerHTML = c.status === "picked"
      ? `<span class="badge">Making this</span>`
      : `<span class="badge">Passed</span>` +
        (c.reject_note ? ` <span class="verdict-note">“${esc(c.reject_note)}”</span>` : "");
    actions.appendChild(v);

    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "btn btn-quiet";
    undo.textContent = "Undo";
    undo.style.marginLeft = "auto";
    undo.onclick = async () => {
      undo.disabled = true;
      try {
        await decide(c, "shown", null);
        c.status = "shown"; c.reject_note = null;
        node.className = "card";
        renderActions(actions, node, c, false);
      } catch (err) { fail(`Could not undo: ${err.message}`); }
    };
    actions.appendChild(undo);
    return;
  }

  const pick = document.createElement("button");
  pick.type = "button";
  pick.className = "btn btn-pick";
  pick.textContent = "Make this one";
  pick.onclick = async () => {
    pick.disabled = true;
    try {
      await decide(c, "picked", null);
      c.status = "picked";
      node.classList.add("is-picked");
      renderActions(actions, node, c, true);
    } catch (err) { pick.disabled = false; fail(`Could not save: ${err.message}`); }
  };

  const pass = document.createElement("button");
  pass.type = "button";
  pass.className = "btn btn-reject";
  pass.textContent = "Not this one";
  pass.onclick = () => openRejector(node, actions, c);

  actions.append(pick, pass);
}

function openRejector(node, actions, c) {
  if (node.querySelector(".rejector")) return;

  const box = document.createElement("div");
  box.className = "rejector";
  box.innerHTML = `
    <p><b>What's wrong with it?</b>
       <span>One line is plenty. This is what sharpens the rules — every
       reason written here is read back when the scoring is tuned.</span></p>
    <div class="chips">
      ${REASONS.map((r) => `<button type="button" class="chip">${esc(r)}</button>`).join("")}
    </div>
    <textarea placeholder="e.g. Asif already shot something close to this in July"></textarea>
    <div class="rejector-actions">
      <button type="button" class="btn btn-primary save">Save and pass</button>
      <button type="button" class="btn btn-quiet cancel">Cancel</button>
    </div>
  `;
  node.insertBefore(box, actions);

  const area = box.querySelector("textarea");
  box.querySelectorAll(".chip").forEach((chip) => {
    chip.onclick = () => {
      // Prefill rather than submit: the chip is a running start, and the
      // lead can keep typing after it.
      area.value = area.value ? `${area.value.trim()}. ${chip.textContent}` : chip.textContent;
      area.focus();
      area.setSelectionRange(area.value.length, area.value.length);
    };
  });
  area.focus();

  box.querySelector(".cancel").onclick = () => box.remove();
  box.querySelector(".save").onclick = async () => {
    const save = box.querySelector(".save");
    save.disabled = true;
    try {
      const note = area.value.trim();
      await decide(c, "rejected", note);
      c.status = "rejected"; c.reject_note = note;
      box.remove();
      node.classList.add("is-rejected");
      renderActions(actions, node, c, true);
    } catch (err) { save.disabled = false; fail(`Could not save: ${err.message}`); }
  };
}

function renderMeta(shortlist, candidates) {
  const when = shortlist?.created_at
    ? new Date(shortlist.created_at).toLocaleDateString(undefined,
        { day: "numeric", month: "long", year: "numeric" })
    : "—";
  const open = candidates.filter((c) => c.status === "shown").length;
  el.meta.innerHTML = `
    ${LIVE ? "" : `<span class="demo">Demo data</span>`}
    <span>Shortlist of <strong>${esc(when)}</strong></span>
    <span><strong>${candidates.length}</strong> cleared the gate</span>
    <span><strong>${open}</strong> still to decide</span>
    ${shortlist?.mode ? `<span>${esc(shortlist.mode)}</span>` : ""}
  `;
}

async function render() {
  show(el.loading, true);
  show(el.error, false);
  try {
    const { shortlist, candidates } = await loadLatest();
    show(el.loading, false);
    show(el.shortlist, true);
    renderMeta(shortlist, candidates);
    el.cards.innerHTML = "";
    candidates.forEach((c, i) => el.cards.appendChild(card(c, i)));
    show(el.empty, candidates.length === 0);
  } catch (err) {
    fail(`Could not load the shortlist: ${err.message}`);
  }
}

/* ----------------------------------------------------------------- auth */

async function membershipOk() {
  const { data, error } = await supabase.from("team_members").select("email").limit(1);
  // A member reads at least their own row; a stranger's policy returns none.
  return !error && Array.isArray(data) && data.length > 0;
}

async function start() {
  if (!LIVE) {
    show(el.loading, false);
    await render();
    return;
  }

  const { createClient } = await import(
    "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm");
  supabase = createClient(CFG.supabaseUrl, CFG.supabaseAnonKey);

  const { data } = await supabase.auth.getSession();
  session = data.session;

  supabase.auth.onAuthStateChange((_event, s) => {
    if (s?.user?.id !== session?.user?.id) { session = s; route(); }
  });

  await route();
}

async function route() {
  show(el.signin, false);
  show(el.notmember, false);
  show(el.shortlist, false);
  show(el.topbarRight, false);

  if (!session) {
    show(el.loading, false);
    show(el.signin, true);
    return;
  }

  show(el.loading, true);
  if (!(await membershipOk())) {
    show(el.loading, false);
    el.notmemberEmail.textContent = session.user.email ?? "this account";
    show(el.notmember, true);
    return;
  }

  el.who.textContent = session.user.email ?? "";
  show(el.topbarRight, true);
  await render();
}

/* ---------------------------------------------------------------- wiring */

el.signinForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = el.email.value.trim();
  if (!email) return;
  el.signinBtn.disabled = true;
  el.signinNote.className = "gate-note";
  el.signinNote.textContent = "Sending…";
  try {
    const { error } = await supabase.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: window.location.origin + window.location.pathname },
    });
    if (error) throw error;
    el.signinNote.className = "gate-note ok";
    el.signinNote.textContent = `Link sent to ${email}. Open it on this device.`;
  } catch (err) {
    el.signinNote.className = "gate-note bad";
    el.signinNote.textContent = err.message;
  } finally {
    el.signinBtn.disabled = false;
  }
});

$("signout")?.addEventListener("click", async () => {
  await supabase?.auth.signOut();
  session = null;
  route();
});
$("notmember-signout")?.addEventListener("click", async () => {
  await supabase?.auth.signOut();
  session = null;
  route();
});
$("refresh")?.addEventListener("click", render);

start();
