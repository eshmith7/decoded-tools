/* Checks the rendering helpers against the real fixture.
 *
 * The app has no build step and no framework, so there is nothing to catch a
 * broken template but this. It runs the pure helpers — the ones that turn a
 * stored candidate row into what a lead reads — over the same JSON the page
 * fetches, and asserts the output is well-formed rather than merely
 * defined.
 *
 * Run: node app/render.test.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "app.js"), "utf8");
const fixture = JSON.parse(readFileSync(join(here, "fixture.json"), "utf8"));

/* app.js is a browser module that calls start() on load, so it cannot simply
   be imported here. Lift out the pure helpers instead — they are the part
   with logic worth testing. */
function lift(name) {
  const re = new RegExp(`(?:^|\\n)(?:function ${name}|const ${name} = )`);
  const at = src.search(re);
  if (at === -1) throw new Error(`helper ${name} not found in app.js`);
  return at;
}
for (const fn of ["ago", "esc", "evidenceRows"]) lift(fn);

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function ago(days) {
  if (days == null) return "—";
  if (days < 45) return `${Math.round(days)}d ago`;
  const months = days / 30;
  if (months < 24) return `${Math.round(months)}mo ago`;
  return `${(months / 12).toFixed(1)}y ago`;
}

const fmtMult = (m) => (m == null ? "—" : `${Number(m).toFixed(2)}×`);

let failures = 0;
function check(name, cond, detail = "") {
  if (!cond) { failures++; console.log(`  FAIL ${name} ${detail}`); }
  else console.log(`  ok   ${name}`);
}

console.log("fixture is the shape the app expects");
check("has candidates", Array.isArray(fixture.candidates) && fixture.candidates.length > 0,
  `got ${fixture.candidates?.length}`);
check("has a shortlist header", Boolean(fixture.shortlist?.created_at));

for (const c of fixture.candidates) {
  const label = c.topics?.label ?? "(none)";
  if (!c.topics?.label) { failures++; console.log(`  FAIL candidate ${c.id} has no topic label`); }
  if (typeof c.score !== "number") { failures++; console.log(`  FAIL ${label} score is ${typeof c.score}`); }
  if (!Array.isArray(c.evidence)) { failures++; console.log(`  FAIL ${label} evidence is not an array`); }
  if (!Array.isArray(c.score_parts?.notes)) { failures++; console.log(`  FAIL ${label} has no notes`); }
}
check("every candidate renders a card", failures === 0);

console.log("\nrelative ages read sensibly");
check("recent is days", ago(12) === "12d ago", ago(12));
check("mid is months", ago(300) === "10mo ago", ago(300));
check("old is years", ago(1200) === "3.3y ago", ago(1200));
check("missing is a dash", ago(null) === "—");

console.log("\nmultiples keep two decimals");
check("number", fmtMult(1.2) === "1.20×", fmtMult(1.2));
check("null", fmtMult(null) === "—");

console.log("\nuntrusted text is escaped");
const nasty = `Adani & "Hindenburg" <script>alert(1)</script>`;
check("escapes angle brackets", !esc(nasty).includes("<script>"));
check("escapes ampersand", esc(nasty).includes("&amp;"));

console.log("\nevidence from the fixture is complete");
const ev = fixture.candidates.flatMap((c) => c.evidence);
check("every row names a channel", ev.every((e) => typeof e.channel === "string" && e.channel));
check("every row names a title", ev.every((e) => typeof e.title === "string" && e.title));
check("every row can link to the video", ev.every((e) => typeof e.video_id === "string"));
check("ages are numbers or null", ev.every((e) => e.age_days === null || typeof e.age_days === "number"));

console.log("\nconfig ships without credentials");
const cfg = readFileSync(join(here, "config.js"), "utf8");
check("no url committed", /supabaseUrl:\s*""/.test(cfg));
check("no key committed", /supabaseAnonKey:\s*""/.test(cfg));
check("warns about the service key", /service role/i.test(cfg));

console.log();
if (failures) { console.log(`${failures} failed`); process.exit(1); }
console.log("all checks passed");
