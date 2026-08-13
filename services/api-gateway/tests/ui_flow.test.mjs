/*
 * End-to-end UI test: the real console page, in a real DOM, driving real
 * clicks against a running platform.
 *
 *     python scripts/run_local.py          # in another terminal
 *     npm install jsdom                    # once
 *     node services/api-gateway/tests/ui_flow.test.mjs
 *
 * Not a mock of the console. jsdom parses the page that ships, executes its
 * script, and this file clicks the actual buttons. fetch is pointed at the
 * gateway, so every assertion below is about what a person sitting in front
 * of the page would see after pressing something.
 *
 * WHY THIS EXISTS. Every console bug so far reached the cluster: an empty
 * card number sent as "", a raw 401 object printed into a results panel, a
 * dropdown of opaque account IDs. None of them could have been caught by the
 * Python suites, which never load the page, and all of them were found by a
 * person clicking. This closes that gap.
 */

import fs from "node:fs";
import { JSDOM } from "jsdom";

const BASE = process.env.CONSOLE_BASE || "http://127.0.0.1:18080";
const PAGE = new URL("../app/static/index.html", import.meta.url);

let failures = 0;
let group = "";

const section = (name) => { group = name; console.log(`\n=== ${name} ===`); };
const check = (label, ok, detail = "") => {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${label}${ok ? "" : "  -- " + detail}`);
  if (!ok) failures++;
};

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/** Let the page's in-flight promises settle. */
const settle = async (ms = 350) => { await sleep(ms); };

async function loadConsole() {
  const html = fs.readFileSync(PAGE, "utf8");
  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    url: BASE + "/",
    pretendToBeVisual: true,
    // beforeParse, NOT after construction. The page calls fetch("/ready") as
    // it loads, and jsdom executes inline scripts during construction, so
    // anything injected afterwards arrives too late and the page dies on
    // "fetch is not defined" before a single button exists.
    beforeParse(window) {
      // Real network, real gateway. Node 20 has fetch; jsdom does not.
      window.fetch = (path, init) => fetch(new URL(path, BASE), init);
      // The console uses confirm() for reset and prompt() for purge. jsdom
      // throws on both unless they are provided.
      window.confirm = () => window.__confirm ?? true;
      window.prompt = () => window.__prompt ?? null;
    },
  });
  const { window } = dom;

  await settle(400);
  return { dom, window, $: (id) => window.document.getElementById(id) };
}

const text = (el) => (el ? (el.textContent || "").trim() : "");
const html = (el) => (el ? el.innerHTML : "");

// ---------------------------------------------------------------------------

async function main() {
  const { window, $ } = await loadConsole();
  const doc = window.document;

  let purchaseCid = null;
  const phoneA = "0300" + String(Date.now()).slice(-7);
  const phoneB = "0301" + String(Date.now() + 11).slice(-7);

  // ------------------------------------------------------------------------
  section("0. the page loads signed out, and says so everywhere");

  check("header says not signed in", text($("who")) === "not signed in", text($("who")));
  check("logout is hidden while signed out", $("btn-logout").hidden === true);
  check("register is offered", $("btn-register").hidden === false);
  for (const tab of ["db", "load", "trace"]) {
    check(`${tab} tab shows the sign-in banner`, $("auth-" + tab).hidden === false,
          `hidden=${$("auth-" + tab).hidden}`);
  }

  // ------------------------------------------------------------------------
  section("1. a Database button pressed signed out explains itself");

  $("btn-accounts").click();
  await settle();
  const out1 = html($("ledger-out"));
  check("it does not print a raw Authorization error",
        !/Authorization header/.test(out1), out1.slice(0, 120));
  check("it names the Wallet tab", /Wallet/.test(out1), out1.slice(0, 120));

  // ------------------------------------------------------------------------
  section("2. register wallet A");

  $("msisdn").value = phoneA;
  $("btn-register").click();
  await settle(900);

  check("signed in", /signed in/.test(text($("who"))), text($("who")));
  check("the card field was filled", $("card").value.length >= 12, $("card").value);
  check("logout is now offered", $("btn-logout").hidden === false);
  check("register is hidden while signed in", $("btn-register").hidden === true);
  const cardA = $("card").value;

  for (const tab of ["db", "load", "trace"]) {
    check(`${tab} banner cleared by the login`, $("auth-" + tab).hidden === true,
          `hidden=${$("auth-" + tab).hidden}`);
  }

  // ------------------------------------------------------------------------
  section("3. the console offers to create the missing merchant");

  // A purchase credits merchant:demo. On a fresh platform it does not exist,
  // and the console reveals a button for exactly this. Clicking it is what a
  // person would do, so the test does the same rather than reaching past the
  // UI to set up state.
  // Order-independent: the merchant may already exist from an earlier run
  // against the same stack. Asserting the note is VISIBLE first made this
  // test depend on a fresh database, which is a property of the environment
  // and not of the console.
  if ($("merchant-note").hidden === false) {
    $("btn-merchant").click();
    await settle(900);
  }
  check("the demo merchant exists, so a purchase has somewhere to credit",
        $("merchant-note").hidden === true, `hidden=${$("merchant-note").hidden}`);

  // ------------------------------------------------------------------------
  section("4. an empty wallet cannot spend, and the console says why");

  $("amt").value = "25.50";
  $("btn-pay").click();
  await settle(1200);
  const paid = html($("result"));
  check("declined for insufficient funds", /nsufficient funds/.test(paid),
        paid.slice(0, 160));

  // ------------------------------------------------------------------------
  section("5. top up");

  $("tu-amt").value = "100.00";
  $("btn-topup").click();
  await settle(1200);
  check("top-up approved", /approved/i.test(html($("result"))),
        html($("result")).slice(0, 160));

  $("btn-balance").click();
  await settle(600);
  check("balance shows 100", /100/.test(html($("balance"))), html($("balance")).slice(0, 160));

  // ------------------------------------------------------------------------
  section("6. pay");

  // No manual key rotation here on purpose. The declined attempt in step 4
  // used this key, and a cached decline is correct behaviour, so the page
  // has to mint a fresh key itself or a funded wallet still cannot spend.
  // That is exactly the bug this step caught.
  $("btn-pay").click();
  await settle(1500);
  const paid2 = html($("result"));
  check("purchase approved", /approved/i.test(paid2), paid2.slice(0, 200));
  // Captured HERE, not scraped from #result at the end. By then the panel
  // holds the transfer, and an earlier version of this test loaded whichever
  // correlation id happened to be lying around, which was a gateway-only
  // request, and then reported the trace as missing a service.
  purchaseCid = (paid2.match(/cid-[0-9a-f]+/) || [])[0];

  $("btn-balance").click();
  await settle(600);
  check("balance fell to 74.50", /74\.50/.test(html($("balance"))),
        html($("balance")).slice(0, 160));

  // The other half of the rule: an APPROVED payment keeps its key, so
  // pressing Pay again must replay the cached reply rather than charge
  // twice. Rotating on every outcome would have quietly broken this.
  $("btn-pay").click();
  await settle(1200);
  check("pressing Pay again replays instead of charging twice",
        /approved/i.test(html($("result"))), html($("result")).slice(0, 160));
  $("btn-balance").click();
  await settle(600);
  check("the balance did NOT move on the replay", /74\.50/.test(html($("balance"))),
        html($("balance")).slice(0, 160));

  // ------------------------------------------------------------------------
  section("7. the Database tab works while signed in");

  $("btn-accounts").click();
  await settle(700);
  const accounts = html($("ledger-out"));
  check("accounts render", /acc_/.test(accounts), accounts.slice(0, 160));
  check("the type column says wallet, not checking",
        /wallet/.test(accounts) && !/checking/.test(accounts), accounts.slice(0, 300));
  check("phone numbers render readably, not as raw digits",
        /\+92/.test(accounts), accounts.slice(0, 300));

  $("btn-integrity").click();
  await settle(600);
  check("the books balance", /balanc/i.test(html($("ledger-out"))),
        html($("ledger-out")).slice(0, 160));

  // ------------------------------------------------------------------------
  section("8. log out");

  $("btn-logout").click();
  await settle(300);

  check("header says not signed in", text($("who")) === "not signed in", text($("who")));
  check("the card field was cleared", $("card").value === "", $("card").value);
  check("logout is hidden again", $("btn-logout").hidden === true);
  check("register is offered again", $("btn-register").hidden === false);
  for (const tab of ["db", "load", "trace"]) {
    check(`${tab} banner is back`, $("auth-" + tab).hidden === false,
          `hidden=${$("auth-" + tab).hidden}`);
  }

  $("btn-pay").click();
  await settle(400);
  check("paying while signed out is refused, not sent",
        /Register or log in|No card in this session/.test(html($("result"))),
        html($("result")).slice(0, 160));

  // ------------------------------------------------------------------------
  section("9. log back in as the same wallet");

  $("msisdn").value = phoneA;
  $("btn-login").click();
  await settle(900);

  check("signed in again", /signed in/.test(text($("who"))), text($("who")));
  check("the card came back", $("card").value === cardA,
        `expected ${cardA}, got ${$("card").value}`);

  $("btn-balance").click();
  await settle(600);
  check("the balance survived the logout", /74\.50/.test(html($("balance"))),
        html($("balance")).slice(0, 160));

  // ------------------------------------------------------------------------
  section("10. switch to a second wallet");

  $("btn-logout").click();
  await settle(200);
  $("msisdn").value = phoneB;
  $("btn-register").click();
  await settle(1000);

  const cardB = $("card").value;
  check("wallet B has its own card", cardB && cardB !== cardA, `${cardA} vs ${cardB}`);

  $("btn-balance").click();
  await settle(600);
  check("wallet B starts empty", /0\.00/.test(html($("balance"))),
        html($("balance")).slice(0, 160));

  // Back to A, then B again, to prove the memory is per-wallet.
  $("btn-logout").click();
  await settle(200);
  $("msisdn").value = phoneA;
  $("btn-login").click();
  await settle(900);
  check("switching back restores wallet A's card", $("card").value === cardA,
        `expected ${cardA}, got ${$("card").value}`);
  $("btn-balance").click();
  await settle(600);
  check("wallet A still holds 74.50", /74\.50/.test(html($("balance"))),
        html($("balance")).slice(0, 160));

  // ------------------------------------------------------------------------
  section("11. transfer to the second wallet");

  await settle(500);
  const peer = $("peer-pick");
  check("the recipient list is populated", peer.options.length > 1,
        `${peer.options.length} options`);
  const optionText = Array.from(peer.options).map(o => o.text).join(" | ");
  check("recipients are shown as phone numbers", /\+92/.test(optionText),
        optionText.slice(0, 200));
  check("the funding account is not offered as a payee",
        !/system_funding/.test(optionText), optionText.slice(0, 200));

  $("peer").value = phoneB;
  $("t-amt").value = "10.00";
  $("btn-send").click();
  await settle(1500);
  check("transfer approved", /approved/i.test(html($("result"))),
        html($("result")).slice(0, 200));

  // ------------------------------------------------------------------------
  section("12. tabs switch and keep their state");

  // The Wallet tab is identified as "app". The banner button used to
  // target "wallet" and silently did nothing, which is how that bug hid.
  for (const tab of ["trace", "db", "load", "app"]) {
    doc.querySelector(`nav.tabs button[data-tab="${tab}"]`).click();
    await settle(120);
    const panel = doc.querySelector(`section.panel[data-tab="${tab}"]`);
    const selected = doc.querySelector(`nav.tabs button[data-tab="${tab}"]`)
      .getAttribute("aria-selected");
    check(`${tab} tab activates`, selected === "true", `aria-selected=${selected}`);
    check(`${tab} panel is shown`, panel && !panel.hidden, `hidden=${panel && panel.hidden}`);
  }

  // ------------------------------------------------------------------------
  section("13. the live trace shows every layer, grouped by request");

  // Needs a Redis for the services to write into. run_local starts an
  // embedded one; without it the tab correctly reports that tracing is off,
  // and there is nothing to assert about a feature that is not running.
  doc.querySelector('nav.tabs button[data-tab="trace"]').click();
  await settle(200);
  $("btn-refresh-traces").click();
  await settle(900);

  const listed = html($("trace-list"));
  if (!/cid-/.test(listed)) {
    console.log("  SKIP  tracing is off (no REDIS_URL), nothing to check");
  } else {
    check("recent requests are listed", true);

    const cid = purchaseCid;
    check("the purchase's correlation id was captured", !!cid, "none found");

    if (cid) {
      $("cid").value = cid;
      $("btn-load-trace").click();
      await settle(1200);

      const meta = html($("trace-meta"));
      const timeline = html($("timeline"));

      check("the outcome is stated up front", /trace-outcome/.test(meta),
            meta.slice(0, 160));
      check("it says how many services were involved",
            /across \d+ services/.test(meta), meta.slice(0, 200));
      check("hops are grouped, not one flat list", /class="hop/.test(timeline),
            timeline.slice(0, 160));

      // The point of the exercise. Before REDIS_URL reached them, six of the
      // seven tracing services wrote into a client that was never
      // configured, so the timeline stopped after the gateway.
      for (const svc of ["api-gateway", "transaction-service",
                         "iso8583-adapter", "ace-stub", "ledger-service"]) {
        check(`${svc} appears in the timeline`, timeline.includes(svc),
              "missing, so REDIS_URL is not reaching it");
      }
      check("the switch reply is visible", /0210/.test(timeline), "no MTI 0210");
      check("the PIN never reached the trace store", !/[">]1234[<"]/.test(timeline),
            "a PIN is visible in the trace");
    }
  }

  console.log(failures ? `\n${failures} UI check(s) FAILED` : "\nall UI checks passed");
  return failures ? 1 : 0;
}

main().then(c => process.exit(c)).catch(e => { console.error(e); process.exit(1); });
