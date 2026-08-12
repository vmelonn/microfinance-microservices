/*
 * Behavioural test for the console's signed-out handling.
 *
 *     node services/api-gateway/tests/console_auth.test.mjs
 *
 * Extracts the real signedOut / explain401 / refreshAuthBanners functions
 * from the shipped page and runs them against a DOM stub, so it tests what
 * ships rather than a copy. Deliberately NOT in the CI matrix, which is
 * Python only; the static counterpart that does run there is
 * tests/e2e/test_console_guards.py.
 *
 * The case worth keeping: a 401 means two different things. No token at all,
 * or a token that expired, since the gateway issues them with a one hour
 * life. An operator who left the tab open overnight hits the second, and
 * telling them "not signed in" when they plainly are is its own small lie.
 */
// Exercise the real signedOut / explain401 / refreshAuthBanners logic from
// the shipped console, with a DOM stub. No browser, no framework.
import fs from "node:fs";

const page = fs.readFileSync(new URL("../app/static/index.html", import.meta.url), "utf8");
const js = page.match(/<script>([\s\S]*?)<\/script>/)[1];

// Pull out just the three functions under test plus the state they touch.
const slice = (name) => {
  const i = js.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`${name} not found`);
  let depth = 0, started = false;
  for (let j = i; j < js.length; j++) {
    if (js[j] === "{") { depth++; started = true; }
    else if (js[j] === "}") { depth--; if (started && depth === 0) return js.slice(i, j + 1); }
  }
  throw new Error(`unterminated ${name}`);
};

const els = {};
const mk = () => ({ innerHTML: "", hidden: false, textContent: "" });
["auth-db", "auth-load", "ledger-out", "who", "session-kv"].forEach(id => els[id] = mk());

const harness = `
  const S = { token: null, msisdn: null, userId: null, card: null };
  const $ = id => els[id];
  const esc = s => String(s ?? "");
  function renderSession() {
    $("who").textContent = S.token ? "signed in" : "not signed in";
    refreshAuthBanners();
  }
  ${slice("signedOut")}
  ${slice("explain401")}
  ${slice("refreshAuthBanners")}
  return { S, signedOut, explain401, refreshAuthBanners, renderSession };
`;
const api = new Function("els", harness)(els);

let failures = 0;
const check = (label, cond, detail = "") => {
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "  -- " + detail}`);
  if (!cond) failures++;
};

// 1. signed out, pressing a Database button
api.S.token = null;
const blocked = api.signedOut(els["ledger-out"]);
check("signed out blocks the call", blocked === true, String(blocked));
check("it says where to sign in", /Wallet/.test(els["ledger-out"].innerHTML),
      els["ledger-out"].innerHTML);
check("no raw Authorization error is shown",
      !/Authorization header/.test(els["ledger-out"].innerHTML));
check("the Database banner is visible", els["auth-db"].hidden === false);
check("the Load test banner is visible", els["auth-load"].hidden === false);

// 2. signed in
api.S.token = "jwt";
api.refreshAuthBanners();
check("banner hides once signed in", els["auth-db"].hidden === true);
check("signed in does not block", api.signedOut(els["ledger-out"]) === false);

// 3. an EXPIRED token: 401 arrives while S.token is still set
api.S.token = "expired-jwt";
els["ledger-out"].innerHTML = "";
api.explain401(els["ledger-out"]);
check("expiry is named as expiry", /expired/i.test(els["ledger-out"].innerHTML),
      els["ledger-out"].innerHTML);
check("the dead token is cleared", api.S.token === null, String(api.S.token));
check("the banner comes back after expiry", els["auth-db"].hidden === false);

// 4. never signed in at all, 401 from a background call
api.S.token = null;
els["ledger-out"].innerHTML = "";
api.explain401(els["ledger-out"]);
check("no token is named as not signed in",
      /Not signed in/.test(els["ledger-out"].innerHTML) &&
      !/expired/i.test(els["ledger-out"].innerHTML),
      els["ledger-out"].innerHTML);

console.log(failures ? `\n${failures} FAILED` : "\nall console auth checks passed");
process.exit(failures ? 1 : 0);
