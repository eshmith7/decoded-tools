/* Supabase connection for the deployed app.
 *
 * Both values below are safe in public code. The anon key is designed to ship
 * to browsers: on its own it grants nothing, because every table is behind
 * Row Level Security and every policy checks membership of team_members. The
 * key that must never appear here is the *service role* key, which bypasses
 * RLS entirely — that one belongs only in GitHub secrets, where the pollers
 * read it.
 *
 * Left blank, the app runs on app/fixture.json and says so on screen.
 * Filling these in is step 3 of app/README.md.
 */

window.TOPIC_RADAR_CONFIG = {
  supabaseUrl: "",
  supabaseAnonKey: "",
};
