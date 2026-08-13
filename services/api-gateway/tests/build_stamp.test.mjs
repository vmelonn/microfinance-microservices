/*
 * The console's build stamp, and the stale-page warning it powers.
 *
 *     python scripts/run_local.py     # in another terminal
 *     node services/api-gateway/tests/build_stamp.test.mjs
 *
 * WHY THIS EXISTS. FileResponse sent an ETag and no Cache-Control, so a
 * browser could apply heuristic freshness and serve the previous build
 * without revalidating. An operator then works against a page whose buttons
 * no longer match the pod, and it looks identical to a deploy that has not
 * landed. Those two need opposite fixes: a hard reload, or waiting for a
 * rollout. The page now carries the build it was served with, /health reports
 * the build the pod has, and a mismatch says so on screen.
 */
// NOT fire when it is current. Checked as a rendered ELEMENT, because
// body.innerHTML contains the <script> source and a naive regex matches the
// template literal that produces the banner rather than the banner.
import { JSDOM } from "jsdom";
const BASE = process.env.CONSOLE_BASE || "http://127.0.0.1:18080";

async function load(html) {
  const dom = new JSDOM(html, {
    runScripts: "dangerously", url: BASE + "/", pretendToBeVisual: true,
    beforeParse(w) { w.fetch = (p,i)=>fetch(new URL(p,BASE),i);
                     w.confirm=()=>true; w.prompt=()=>null; },
  });
  await new Promise(r => setTimeout(r, 1200));
  return dom.window.document;
}

let bad = 0;
const check = (l, ok, d="") => { console.log(`  ${ok?"PASS":"FAIL"}  ${l}${ok?"":"  -- "+d}`); if(!ok) bad++; };

const live = await (await fetch(BASE + "/")).text();

const fresh = await load(live);
check("a current page shows no warning",
      fresh.getElementById("build-warning") === null,
      (fresh.getElementById("build-warning") || {}).textContent);

const stale = live.replace(/(id="build"[^>]*>)[0-9a-f]{8}</, "$1deadbeef<");
const old = await load(stale);
const warning = old.getElementById("build-warning");
check("a cached page says it is out of date", warning !== null, "no warning shown");
if (warning) {
  const txt = warning.textContent.replace(/\s+/g, " ");
  check("it names both builds", /deadbeef/.test(txt) && !/\$\{/.test(txt), txt.slice(0,150));
  check("it says how to fix it", /Ctrl\+Shift\+R/.test(txt), txt.slice(0,150));
}

const head = await fetch(BASE + "/");
check("the page is not cacheable",
      /no-store/.test(head.headers.get("cache-control") || ""),
      head.headers.get("cache-control"));

console.log(bad ? `\n${bad} FAILED` : "\nbuild-stamp checks passed");
process.exit(bad ? 1 : 0);
