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
