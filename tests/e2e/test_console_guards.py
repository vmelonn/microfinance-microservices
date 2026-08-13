"""
Every console button that calls the API must handle being signed out.

The console is served by api-gateway and its whole /console router sits
behind current_user, so a Database or Load test button pressed without a
session gets a 401. Before this guard existed, that surfaced as the gateway's
own error object printed into the results panel:

    {"detail":"Missing or malformed Authorization header. Expected: Bearer <token>"}

Accurate, addressed to an API client, and useless to the person looking at
the button they just pressed.

This is a STATIC check on the shipped page rather than a browser test,
because it protects against the thing that actually goes wrong: somebody
adds a console button later and forgets the guard. It needs no Node, no
headless browser, and it runs in the Python suite that already exists.

The behavioural counterpart lives in
services/api-gateway/tests/console_auth.test.mjs, which exercises the real
functions against a DOM stub. Run it with `node` when changing that logic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONSOLE = (Path(__file__).resolve().parents[2]
           / "services" / "api-gateway" / "app" / "static" / "index.html")

# Handlers that fire on page load, before anyone could have signed in. They
# degrade quietly on purpose: turning a background load into a visible error
# would be noise, not information.
BACKGROUND_LOADERS = {"loadPeers", "loadTraceList", "schema-examples"}


@pytest.fixture(scope="module")
def script() -> str:
    html = CONSOLE.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*?)</script>", html, re.S)
    assert match, "the console page has no <script> block"
    return match.group(1)


def _handlers(script: str) -> dict[str, str]:
    """Every `$("btn-x").onclick = ...` body, keyed by element id."""
    found = {}
    pattern = re.compile(r'\$\("([\w-]+)"\)\.onclick\s*=\s*async\s*\(\)\s*=>\s*\{', re.S)
    for m in pattern.finditer(script):
        start = m.end() - 1
        depth = 0
        for i in range(start, len(script)):
            if script[i] == "{":
                depth += 1
            elif script[i] == "}":
                depth -= 1
                if depth == 0:
                    found[m.group(1)] = script[start:i + 1]
                    break
    return found


def test_the_page_defines_the_signed_out_helpers(script):
    for helper in ("function signedOut(", "function explain401(",
                   "function refreshAuthBanners("):
        assert helper in script, f"{helper} is missing from the console"


def test_every_console_button_checks_for_a_session(script):
    """
    The regression this exists for: a new Database button that calls
    /console/... and prints a raw 401 when pressed signed out.
    """
    unguarded = []
    for element, body in _handlers(script).items():
        if "/console/" not in body:
            continue
        if "signedOut(" not in body:
            unguarded.append(element)

    assert not unguarded, (
        f"these console buttons call the API without checking for a session "
        f"first: {unguarded}. Add `if (signedOut($(\"<output-el>\"))) return;` "
        f"as the first line, before any confirm() dialog."
    )


def test_every_console_button_explains_a_401(script):
    """
    Signed out is not the only way to get a 401. Tokens expire after an hour,
    so an operator who left the tab open overnight has a token that no longer
    works, and `signedOut` alone would let that through to a raw error body.
    """
    unhandled = []
    for element, body in _handlers(script).items():
        if "/console/" not in body:
            continue
        if "explain401(" not in body:
            unhandled.append(element)

    assert not unhandled, (
        f"these console buttons do not handle an expired session: {unhandled}. "
        f"Add `if (r.status === 401) return explain401($(\"<output-el>\"));` "
        f"after the api() call."
    )


def test_the_reset_button_checks_before_it_asks_for_confirmation(script):
    """
    Asking someone to confirm an irreversible action and THEN refusing it on
    a technicality is worse than not asking. The session check has to come
    first.
    """
    body = _handlers(script)["btn-reset"]
    assert "signedOut(" in body and "confirm(" in body
    assert body.index("signedOut(") < body.index("confirm("), (
        "btn-reset asks for confirmation before checking the session"
    )


def test_the_tabs_that_need_a_session_say_so(script):
    html = CONSOLE.read_text(encoding="utf-8")
    for tab in ("db", "load"):
        assert f'id="auth-{tab}"' in html, (
            f"the {tab} tab has no sign-in banner, so a signed-out operator "
            f"gets no warning until something fails"
        )
    assert "goto-wallet" in html, "the banner offers no way to get to the login"


def test_the_banner_is_hidden_by_default(script):
    """
    It starts hidden and is revealed by refreshAuthBanners once the session
    state is known. Shipping it visible would flash a warning at someone who
    is signed in.
    """
    html = CONSOLE.read_text(encoding="utf-8")
    for tab in ("db", "load"):
        marker = f'id="auth-{tab}"'
        element = html[html.index(marker):html.index(marker) + 260]
        assert "hidden" in element, f"the {tab} banner is not hidden initially"


# ---------------------------------------------------------------------------
# The card field
#
# Only Register fills it. The login response carries no card and no endpoint
# exposes one, so signing in as an existing user legitimately leaves it
# blank. Every money button reads that one input, and an empty one used to
# reach the API and come back as a raw pydantic array or a bare 404.
# ---------------------------------------------------------------------------

MONEY_BUTTONS = ["btn-pay", "btn-topup", "btn-balance", "btn-send"]


def test_no_money_button_sends_the_card_field_unchecked(script):
    """
    The regression: reading $("card").value directly inside a request body
    instead of going through requireCard.
    """
    assert "function requireCard(" in script, "requireCard is missing"

    offenders = []
    for element, body in _handlers(script).items():
        if "api(" not in body:
            continue
        # A READ, not a write. btn-register assigns to this field, which is
        # exactly how it gets populated, and matching that was a false
        # positive on the one handler doing the right thing. The lookahead
        # skips `= x` while still catching `== x` and `=== x`.
        reads_raw = re.search(r'\$\("card"\)\.value(?!\s*=[^=])', body)
        if reads_raw and "requireCard(" not in body:
            offenders.append(element)

    assert not offenders, (
        f"these buttons read the card field without requireCard: {offenders}. "
        f"An empty field reaches the API and returns a validation array or a "
        f"404 about a route, neither of which tells the operator anything."
    )


@pytest.mark.parametrize("element", MONEY_BUTTONS)
def test_each_money_button_guards_the_card(script, element):
    body = _handlers(script)[element]
    assert "requireCard(" in body, f"{element} does not guard the card field"


def test_the_guard_runs_before_the_request(script):
    """A guard after the call would still send the empty value."""
    for element in MONEY_BUTTONS:
        body = _handlers(script)[element]
        assert body.index("requireCard(") < body.index("api("), (
            f"{element} calls the API before guarding the card field"
        )


def test_the_card_field_says_what_fills_it(script):
    html = CONSOLE.read_text(encoding="utf-8")
    field = html[html.index('<label for="card">'):]
    field = field[:field.index("</div>") + 6]
    assert "Register" in field, (
        "the card field does not say that Register is what fills it, which is "
        "the whole reason it is empty after a plain login"
    )


# ---------------------------------------------------------------------------
# The hidden attribute has to actually hide
#
# .signin-note sets display:flex. An AUTHOR rule beats the browser's built-in
# [hidden] { display: none }, which is user-agent origin, so setting
# el.hidden = true changed the property and left the banner on screen while
# the header read "signed in 03423222111".
#
# Checked here rather than in the jsdom suite because jsdom honours the
# hidden attribute regardless of the cascade, so a browser-shaped test cannot
# see this bug. It is the same reason CSS frameworks all ship this rule.
# ---------------------------------------------------------------------------

def _stylesheet(html: str) -> str:
    """The CSS, with comments removed.

    Stripping comments is not fussiness: the first draft of the test below
    matched the `[hidden] { display: none }` inside the comment EXPLAINING
    the rule, and passed while the rule itself was absent.
    """
    css = re.search(r"<style>(.*?)</style>", html, re.S)
    assert css, "the console page has no <style> block"
    return re.sub(r"/\*.*?\*/", "", css.group(1), flags=re.S)


def test_the_page_forces_hidden_to_win(script):
    css = _stylesheet(CONSOLE.read_text(encoding="utf-8"))

    rule = re.search(r"\[hidden\]\s*\{([^}]*)\}", css)
    assert rule, (
        "no [hidden] rule. Any component that sets `display` silently defeats "
        "the hidden attribute, because author styles beat the user agent's "
        "[hidden] { display: none }."
    )
    body = rule.group(1).replace(" ", "")
    assert "display:none" in body, rule.group(0)
    assert "!important" in body, (
        "without !important the rule loses to a later or more specific "
        "display declaration, which is exactly the bug it exists to prevent"
    )


def test_components_that_set_display_are_covered(script):
    """
    Every class the console hides with the attribute AND styles with a
    display. Each one would be invisible-in-name-only without the rule above.
    """
    html = CONSOLE.read_text(encoding="utf-8")
    css = _stylesheet(html)
    for cls in ["signin-note"]:
        block = re.search(rf"\.{cls}\s*\{{([^}}]*)\}}", css)
        assert block, f".{cls} is not styled at all"
        assert "display" in block.group(1), (
            f".{cls} no longer sets display; if that is deliberate the "
            f"[hidden] rule is still correct, but this list is now stale"
        )
    assert re.search(r"\[hidden\]\s*\{", css), "the [hidden] rule is gone"
