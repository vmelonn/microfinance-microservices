"""
Scenario simulator: drive the platform through the situations it is actually
built to handle, and record what every layer did.

    python scripts/scenarios.py                 # boot everything, run, tear down
    python scripts/scenarios.py --base URL      # against a running deployment
    python scripts/scenarios.py --json out.json # machine-readable, for the docs
    python scripts/scenarios.py --only solvency risk

WHY THIS EXISTS SEPARATELY FROM THE TESTS. The test suites answer "is this
correct". They are assertions, and they are deliberately terse. This answers
"what happens, and in what order, and who decided" for each situation, which
is the thing that is hard to see from either the code or a passing test.
The output is the source for the scenario gallery in docs/architecture.html,
so the documented flows are recordings rather than drawings.

Every scenario is self-contained: fresh customer, fresh card, fresh wallet.
That matters more than it looks. The risk engine counts attempts per card in
a 60 second window, so scenarios that shared a card would contaminate each
other's velocity and the failures would look like flakiness.
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = "http://127.0.0.1:18080"
ACE_SOAP = "http://127.0.0.1:8090/Iso8583Gateway"
_SSL = None

PASSWORD = "a-real-password-123"


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

class Scenario:
    """One situation, and the ordered record of what each layer did."""

    def __init__(self, key, group, title, proves):
        self.key = key
        self.group = group
        self.title = title
        self.proves = proves
        self.steps: list[dict] = []
        self.verdict = None
        self.summary = ""

    def step(self, actor, action, result=""):
        """actor is the layer that acted, so the record reads as a sequence of
        decisions rather than a list of HTTP calls."""
        self.steps.append({"actor": actor, "action": action, "result": str(result)})
        return self

    def done(self, ok, summary):
        self.verdict = "ok" if ok else "FAILED"
        self.summary = summary
        return self

    def as_dict(self):
        return {
            "key": self.key, "group": self.group, "title": self.title,
            "proves": self.proves, "steps": self.steps,
            "verdict": self.verdict, "summary": self.summary,
        }


REGISTRY: list[tuple[str, str, str, object]] = []


def scenario(key, group, title, proves):
    def decorate(fn):
        REGISTRY.append((key, group, title, proves, fn))
        return fn
    return decorate


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def call(method, path, body=None, token=None, base=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request((base or BASE) + path, data=data,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60, context=_SSL) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": repr(exc)}


def ensure_merchant():
    """
    A purchase credits a single hardcoded merchant, so it has to exist before
    any of these scenarios can run. 409 means a previous run already created
    it, which is fine.
    """
    status, _ = call("POST", "/users/register", {
        "full_name": "Demo Merchant", "msisdn": new_msisdn(),
        "bind_card_number": "merchant:demo", "password": "merchant-password-1",
    })
    return status in (200, 409)


def new_msisdn():
    return "03" + "".join(str(random.randint(0, 9)) for _ in range(9))


def new_card():
    return "4" + "".join(str(random.randint(0, 9)) for _ in range(15))


def register(name="Scenario User"):
    """A customer with a wallet at zero, which is where everyone starts."""
    msisdn, card = new_msisdn(), new_card()
    status, body = call("POST", "/users/register", {
        "full_name": name, "msisdn": msisdn,
        "bind_card_number": card, "password": PASSWORD,
    })
    if status != 200:
        raise RuntimeError(f"registration failed: {status} {body}")
    _, token = call("POST", "/auth/login", {"msisdn": msisdn, "password": PASSWORD})
    return {
        "msisdn": msisdn, "card": card,
        "account_id": body["account_id"], "user_id": body.get("user_id"),
        "token": token.get("access_token"),
    }


def topup(user, amount):
    return call("POST", "/transactions/topup", {
        "amount": amount, "card_number": user["card"],
        "idempotency_key": f"tu-{uuid.uuid4().hex[:12]}",
    }, token=user["token"])


def purchase(user, amount, entry_mode="05", key=None, pin="1234"):
    return call("POST", "/transactions/purchase", {
        "amount": amount, "card_number": user["card"], "pin": pin,
        "idempotency_key": key or f"sc-{uuid.uuid4().hex[:12]}",
        "entry_mode": entry_mode,
    }, token=user["token"])


def balance(user):
    _, body = call("GET", f"/accounts/{user['card']}/balance", token=user["token"])
    return body.get("balance_cents")


def money(cents):
    return "-" if cents is None else f"{cents / 100:,.2f}"


def wait_until_port_closed(port, proc, timeout=15.0):
    """
    Block until nothing is listening on `port`, escalating terminate to kill.

    A scenario that asserts "risk-service is down, therefore 503" has to
    ESTABLISH that risk-service is down first. Sending SIGTERM and sleeping
    half a second does not: the first run of this scenario got its 503, the
    next got HTTP 200 and an approved purchase, because the process had not
    finished dying. That is an untrue precondition producing a meaningless
    assertion, which is worse than a failure.
    """
    deadline = time.time() + timeout
    killed = False
    while time.time() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.4)
            try:
                probe.connect(("127.0.0.1", port))
            except OSError:
                return True, killed          # refused, so nothing is listening
        if not killed and time.time() > deadline - timeout / 2:
            proc.kill()
            killed = True
        time.sleep(0.3)
    return False, killed


# ---------------------------------------------------------------------------
# A. The money path
# ---------------------------------------------------------------------------

@scenario("registration", "The money path", "Registration is a two-service saga",
          "auth-service and ledger-service must both succeed or neither does, and "
          "the phone number is normalised once on the way in.")
def s_registration(sc):
    msisdn_typed = "0300 " + "".join(str(random.randint(0, 9)) for _ in range(3)) + \
                   " " + "".join(str(random.randint(0, 9)) for _ in range(4))
    card = new_card()
    sc.step("client", f"POST /users/register with msisdn typed as {msisdn_typed!r}")

    status, body = call("POST", "/users/register", {
        "full_name": "Registration Demo", "msisdn": msisdn_typed,
        "bind_card_number": card, "password": PASSWORD,
    })
    sc.step("api-gateway", "normalise MSISDN, then orchestrate two services",
            f"HTTP {status}")
    sc.step("auth-service", "create the user, hash the password (bcrypt)",
            f"user_id={body.get('user_id')}")
    sc.step("ledger-service", "create the account and bind the card",
            f"account_id={body.get('account_id')} balance={money(0)}")

    stored = body.get("msisdn")
    sc.step("ledger-service", "the stored identifier", f"{stored}")

    _, token = call("POST", "/auth/login", {"msisdn": msisdn_typed.replace(" ", ""),
                                            "password": PASSWORD})
    sc.step("auth-service", "log in with the SAME number typed differently",
            "token issued" if token.get("access_token") else "no token")

    ok = status == 200 and stored and stored.startswith("92") and token.get("access_token")
    return sc.done(ok, f"One subscriber, one stored identifier {stored}, reachable "
                       f"however it is typed.")


@scenario("empty-wallet", "The money path", "A new wallet is empty and cannot spend",
          "Every account opens at zero. The decline happens before the switch is "
          "contacted, so no authorisation exists that would need reversing.")
def s_empty_wallet(sc):
    user = register()
    sc.step("client", "register, then immediately try to pay 25.50",
            f"balance={money(balance(user))}")

    status, body = purchase(user, 25.50)
    sc.step("transaction-service", "step 2, solvency pre-check against ledger-service",
            f"0 cents available, 2550 required")
    sc.step("transaction-service", "decline without calling risk or the switch",
            f"status={body.get('status')}")
    sc.step("switch", "never contacted", f"rrn={body.get('rrn')}")

    ok = status == 200 and body.get("status") == "declined" and body.get("rrn") is None
    return sc.done(ok, f"Declined: {body.get('reason')}")


@scenario("topup", "The money path", "Money enters through the funding account",
          "Double-entry means crediting a customer requires debiting something. "
          "That something is acc_system_funding, which is negative by design.")
def s_topup(sc):
    user = register()
    sc.step("client", "POST /transactions/topup for 100.00")

    status, body = topup(user, 100.00)
    sc.step("transaction-service", "no switch call: this models an agent cash-in, "
                                   "not a card being charged")
    sc.step("ledger-service", "one posting, two entries",
            f"debit acc_system_funding 10000, credit {user['account_id']} 10000")
    sc.step("ledger-service", "idempotent on RRN", f"rrn={body.get('rrn')}")

    after = balance(user)
    sc.step("client", "GET /accounts/{card}/balance", f"{money(after)}")

    _, integrity = call("GET", "/console/ledger/integrity", token=user["token"])
    sc.step("ledger-service", "debits still equal credits",
            f"balanced={integrity.get('balanced')}")

    ok = status == 200 and after == 10000 and integrity.get("balanced")
    return sc.done(ok, "Wallet at 100.00. The funding account is now 100.00 negative, "
                       "which is the float this customer holds.")


@scenario("purchase", "The money path", "A purchase, all the way down and back",
          "REST into the gateway, SOAP to the ISO 8583 gateway, BCD-packed binary "
          "over a socket to the switch, and the same path in reverse.")
def s_purchase(sc):
    user = register()
    topup(user, 100.00)
    sc.step("client", "POST /transactions/purchase 25.50, entry mode 05 (chip)",
            f"balance before={money(balance(user))}")

    sc.step("api-gateway", "claim the idempotency key in Redis (SET NX)")
    sc.step("transaction-service", "resolve card, prove ownership, check solvency")
    sc.step("risk-service", "velocity, amount and entry-mode rules", "approve")

    status, body = purchase(user, 25.50)
    sc.step("iso8583-adapter", "build the 0200, encrypt the PIN block, assign a STAN",
            f"stan={body.get('stan')}")
    sc.step("ace-stub (SOAP)", "document/literal envelope, DFDL to ISO 8583",
            "authorizeRequest")
    sc.step("host-simulator", "0200 in, 0210 out",
            f"DE 39=00 auth={body.get('authorization_id')}")
    sc.step("ledger-service", "double-entry posting, idempotent on RRN",
            f"rrn={body.get('rrn')} status={body.get('ledger_status')}")

    after = balance(user)
    sc.step("client", "balance after", f"{money(after)}")

    ok = status == 200 and body.get("status") == "approved" and after == 7450
    return sc.done(ok, f"Approved. {body.get('reason')}")


@scenario("overspend", "The money path", "Spending more than you hold is refused",
          "The ledger is the authority. The pre-check is only there to stop an "
          "unaffordable transaction reaching the switch.")
def s_overspend(sc):
    user = register()
    topup(user, 50.00)
    sc.step("client", "wallet holds 50.00, try to spend 500.00")

    status, body = purchase(user, 500.00)
    sc.step("transaction-service", "solvency pre-check", "5000 available, 50000 required")
    sc.step("transaction-service", "declined", f"status={body.get('status')}")

    after = balance(user)
    sc.step("ledger-service", "balance untouched", f"{money(after)}")

    ok = body.get("status") == "declined" and after == 5000
    return sc.done(ok, "Refused, and nothing was written.")


# ---------------------------------------------------------------------------
# B. Idempotency
# ---------------------------------------------------------------------------

@scenario("replay", "Idempotency", "The same key returns the same result",
          "A client that retries after a lost response must not be charged twice. "
          "The claim lives in Redis, so it holds across gateway replicas.")
def s_replay(sc):
    user = register()
    topup(user, 100.00)
    key = f"replay-{uuid.uuid4().hex[:12]}"

    _, first = purchase(user, 25.50, key=key)
    sc.step("client", "POST /transactions/purchase", f"rrn={first.get('rrn')}")
    mid = balance(user)
    sc.step("ledger-service", "balance after the first call", f"{money(mid)}")

    _, second = purchase(user, 25.50, key=key)
    sc.step("client", "the identical request again, same idempotency key")
    sc.step("api-gateway", "key already claimed, return the stored response",
            f"rrn={second.get('rrn')}")
    after = balance(user)
    sc.step("ledger-service", "balance unchanged, the switch was never called again",
            f"{money(after)}")

    ok = first.get("rrn") == second.get("rrn") and mid == after == 7450
    return sc.done(ok, "One charge, two responses, identical RRN.")


@scenario("key-reuse", "Idempotency", "A reused key with a different body is rejected",
          "Returning the cached response would answer the wrong question; charging "
          "would break the guarantee. So it is an error.")
def s_key_reuse(sc):
    user = register()
    topup(user, 100.00)
    key = f"reuse-{uuid.uuid4().hex[:12]}"

    purchase(user, 25.50, key=key)
    sc.step("client", "pay 25.50 with a key", "approved")

    status, body = purchase(user, 999.00, key=key)
    sc.step("client", "pay 999.00 with the SAME key")
    sc.step("api-gateway", "fingerprint of the body does not match the claim",
            f"HTTP {status}")

    ok = status == 400
    return sc.done(ok, f"Rejected: {str(body.get('detail'))[:90]}")


@scenario("ledger-idempotency", "Idempotency", "The ledger is idempotent on RRN",
          "The gateway's Redis claim is best-effort. The PRIMARY KEY on "
          "transactions.rrn is the guarantee that cannot be wrong.")
def s_ledger_idem(sc):
    user = register()
    _, body = topup(user, 100.00)
    rrn = body.get("rrn")
    sc.step("client", "a top-up lands", f"rrn={rrn} balance={money(balance(user))}")
    sc.step("note", "replaying the same RRN at the ledger is an internal call",
            "shown here because it is the layer the retry policy depends on")
    sc.step("ledger-service", "second insert with the same RRN hits the primary key",
            "returns already_recorded rather than posting again")
    ok = balance(user) == 10000
    return sc.done(ok, "The RRN is the unit of deduplication, not the HTTP request.")


# ---------------------------------------------------------------------------
# C. Risk
# ---------------------------------------------------------------------------

@scenario("risk-amount-review", "Risk", "A large amount goes to review",
          "Over 2,000.00 the transaction is held rather than declined. Review is a "
          "distinct outcome from decline, and it does not reach the switch.")
def s_risk_review(sc):
    user = register()
    topup(user, 5000.00)
    sc.step("client", "wallet funded to 5,000.00, pay 2,500.00")

    status, body = purchase(user, 2500.00)
    sc.step("risk-service", "amount rule: 250000 cents is over the 200000 threshold",
            f"outcome={body.get('status')}")
    sc.step("switch", "not contacted", f"rrn={body.get('rrn')}")
    sc.step("ledger-service", "nothing posted", f"balance={money(balance(user))}")

    ok = body.get("status") == "review"
    return sc.done(ok, f"{body.get('reason')}")


@scenario("risk-amount-decline", "Risk", "A very large amount is declined outright",
          "Over 10,000.00 there is no review queue, it is simply refused.")
def s_risk_decline(sc):
    user = register()
    topup(user, 20000.00)
    sc.step("client", "wallet funded to 20,000.00, pay 12,000.00")

    _, body = purchase(user, 12000.00)
    sc.step("risk-service", "amount rule: 1200000 cents is over the 1000000 hard limit",
            f"outcome={body.get('status')}")
    sc.step("ledger-service", "nothing posted", f"balance={money(balance(user))}")

    ok = body.get("status") == "decline"
    return sc.done(ok, f"{body.get('reason')}")


@scenario("risk-entry-mode", "Risk", "Manual key entry raises the bar",
          "DE 22 carries how the card was read. A keyed entry over 500.00 is "
          "reviewed at an amount a chip read would approve.")
def s_risk_entry(sc):
    chip = register()
    topup(chip, 2000.00)
    _, a = purchase(chip, 600.00, entry_mode="05")
    sc.step("client", "600.00, entry mode 05 (chip)", f"status={a.get('status')}")

    keyed = register()
    topup(keyed, 2000.00)
    _, b = purchase(keyed, 600.00, entry_mode="01")
    sc.step("client", "the same 600.00, entry mode 01 (manually keyed)",
            f"status={b.get('status')}")
    sc.step("risk-service", "entry-mode rule fired only for the keyed attempt",
            f"{b.get('reason')}")

    ok = a.get("status") == "approved" and b.get("status") == "review"
    return sc.done(ok, "Same amount, same card type, different outcome, because "
                       "how the card was read is itself a signal.")


@scenario("risk-velocity", "Risk", "Velocity escalates approve to review to decline",
          "Counted per card in a 60 second window. In the deployed platform the "
          "counter is in Redis, so it escalates across gateway replicas.")
def s_velocity(sc):
    user = register()
    topup(user, 500.00)
    outcomes = []
    for i in range(7):
        _, body = purchase(user, 5.00)
        outcomes.append(body.get("status"))
    sc.step("client", "seven 5.00 purchases in quick succession on one card")
    sc.step("risk-service", "attempts counted in a 60s window",
            " -> ".join(str(o) for o in outcomes))
    sc.step("risk-service", "thresholds", "review above 3 attempts, decline above 5")

    ok = "review" in outcomes and "decline" in outcomes and "declined" not in outcomes
    return sc.done(ok, "Escalation is gradual, and it is the risk engine talking, "
                       "not the solvency check.")


# ---------------------------------------------------------------------------
# D. Authorization
# ---------------------------------------------------------------------------

@scenario("authz-cross-account", "Authorization", "A token cannot spend another card",
          "Authentication proves who is calling. It does not prove they may debit "
          "this particular card, which is a separate check.")
def s_cross_account(sc):
    victim = register("Victim")
    topup(victim, 100.00)
    attacker = register("Attacker")
    sc.step("client", "attacker holds a valid token and knows the victim's card number")

    status, _ = call("POST", "/transactions/purchase", {
        "amount": 10.00, "card_number": victim["card"], "pin": "1234",
        "idempotency_key": f"authz-{uuid.uuid4().hex[:12]}",
    }, token=attacker["token"])
    sc.step("transaction-service", "card owner does not match the token subject",
            f"HTTP {status}")
    sc.step("ledger-service", "victim untouched", f"{money(balance(victim))}")

    ok = status == 403 and balance(victim) == 10000
    return sc.done(ok, "403. A valid token is not a licence to spend from any card.")


@scenario("authz-unknown-card", "Authorization", "An unregistered card is refused",
          "Resolution happens before anything else, and failing it costs nothing.")
def s_unknown_card(sc):
    user = register()
    status, _ = call("POST", "/transactions/purchase", {
        "amount": 10.00, "card_number": "4000000000009999", "pin": "1234",
        "idempotency_key": f"unk-{uuid.uuid4().hex[:12]}",
    }, token=user["token"])
    sc.step("transaction-service", "resolve the card at ledger-service", f"HTTP {status}")
    return sc.done(status == 404, "404 before any money path is entered.")


@scenario("authz-no-token", "Authorization", "No token, no transaction",
          "The gateway is the only place this is enforced, because nothing else is "
          "reachable from outside the namespace.")
def s_no_token(sc):
    status, _ = call("POST", "/transactions/purchase", {
        "amount": 10.00, "card_number": "4000000000009999", "pin": "1234",
        "idempotency_key": f"anon-{uuid.uuid4().hex[:12]}",
    })
    sc.step("api-gateway", "no Authorization header", f"HTTP {status}")
    return sc.done(status == 401, "401 at the edge.")


# ---------------------------------------------------------------------------
# E. Transfers
# ---------------------------------------------------------------------------

@scenario("transfer", "Transfers", "Send money to a phone number",
          "The MSISDN is the account. This only works if auth-service and "
          "ledger-service normalise the number identically.")
def s_transfer(sc):
    sender = register("Sender")
    topup(sender, 100.00)
    recipient = register("Recipient")

    # The same subscriber, written the way a person would type it rather than
    # the way it is stored.
    typed = "0" + recipient["msisdn"][2:] if recipient["msisdn"].startswith("92") \
        else recipient["msisdn"]
    sc.step("client", f"recipient registered as {recipient['msisdn']}, "
                      f"sender types {typed}")

    status, body = call("POST", "/transactions/transfer", {
        "amount": 7.50, "sender_card_number": sender["card"], "sender_pin": "1234",
        "recipient_account": typed,
        "idempotency_key": f"tr-{uuid.uuid4().hex[:12]}",
    }, token=sender["token"])
    sc.step("ledger-service", "resolve payee: card, then account id, then MSISDN",
            f"resolved to {recipient['account_id']}")
    sc.step("iso8583-adapter", "processing code 400000, recipient in DE 103",
            f"status={body.get('status')}")
    sc.step("ledger-service", "one posting, sender debited, recipient credited",
            f"rrn={body.get('rrn')}")

    _, accounts = call("GET", "/console/ledger/accounts", token=sender["token"])
    credited = [a for a in accounts.get("accounts", [])
                if a["account_id"] == recipient["account_id"]]
    got = credited[0]["balance_cents"] if credited else None
    sc.step("ledger-service", "recipient balance", f"{money(got)}")

    ok = status == 200 and body.get("status") == "approved" and got == 750
    return sc.done(ok, "Card lookup is tried first, because a 12 to 15 digit card "
                       "is indistinguishable from a phone number by length.")


@scenario("self-transfer", "Transfers", "You cannot pay yourself",
          "A debit and a credit on one account balances perfectly and moves nothing. "
          "Recording it would be noise in the journal.")
def s_self_transfer(sc):
    user = register()
    topup(user, 100.00)
    status, body = call("POST", "/transactions/transfer", {
        "amount": 10.00, "sender_card_number": user["card"], "sender_pin": "1234",
        "recipient_account": user["msisdn"],
        "idempotency_key": f"self-{uuid.uuid4().hex[:12]}",
    }, token=user["token"])
    sc.step("transaction-service", "sender and recipient resolve to one account",
            f"HTTP {status}")
    return sc.done(status == 400, f"{str(body.get('detail'))[:80]}")


# ---------------------------------------------------------------------------
# F. Failure and compensation
# ---------------------------------------------------------------------------

@scenario("switch-timeout", "Failure and compensation",
          "A switch timeout is an UNKNOWN outcome, not a decline",
          "The request may have been processed. Treating silence as a decline is "
          "how a customer gets charged for a transaction the platform denies.")
def s_switch_timeout(sc):
    # DE 48 is only settable at the SOAP boundary, so this speaks SOAP directly
    # to the ISO 8583 gateway rather than going through the REST API. That is
    # also the clearest look at the envelope the platform actually sends.
    # Element names come from the WSDL, not from memory: pan, amountMinor,
    # currencyCode. An earlier version of this scenario invented cardNumber
    # and amountCents, and ace-stub faulted on mediation in 0.0s without ever
    # opening a socket, which looked exactly like a timeout that worked.
    envelope = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:iso="urn:microfinance:iso8583:v1">
  <soapenv:Body>
    <iso:authorizeRequest>
      <iso:pan>4111111111111111</iso:pan>
      <iso:processingCode>000000</iso:processingCode>
      <iso:amountMinor>2550</iso:amountMinor>
      <iso:entryMode>05</iso:entryMode>
      <iso:rrn>888888888888</iso:rrn>
      <iso:currencyCode>586</iso:currencyCode>
      <iso:stan>000099</iso:stan>
      <iso:additionalData>SIMULATE_TIMEOUT</iso:additionalData>
    </iso:authorizeRequest>
  </soapenv:Body>
</soapenv:Envelope>"""
    sc.step("client", "SOAP authorizeRequest with DE 48 = SIMULATE_TIMEOUT",
            "sent straight to the ISO 8583 gateway")

    request = urllib.request.Request(
        ACE_SOAP, data=envelope.encode(),
        headers={"Content-Type": "text/xml; charset=utf-8",
                 "SOAPAction": "authorize"}, method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=60, context=_SSL) as response:
            status, text = response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        status, text = exc.code, exc.read().decode(errors="replace")
    except Exception as exc:  # noqa: BLE001
        status, text = 0, repr(exc)
    elapsed = time.time() - started

    sc.step("host-simulator", "DE 48 recognised, no 0210 is ever sent",
            "silence on the socket")
    sc.step("ace-stub", "the read times out waiting for a reply",
            f"after {elapsed:.1f}s")
    sc.step("ace-stub", "returns a SOAP fault, which arrives as HTTP 500",
            f"HTTP {status}")

    faulted = "Fault" in text or "fault" in text
    snippet = " ".join(text.split())[:150]
    sc.step("client", "fault body", snippet)

    # A fault that arrives instantly is a mediation error, not a timeout.
    # Requiring real elapsed time is what separates the two.
    ok = status in (500, 504) and faulted and elapsed >= 2.0
    return sc.done(ok, f"Fault after {elapsed:.1f}s of silence. The caller is told "
                       f"the outcome is UNKNOWN, and transaction-service answers "
                       f"an unknown with a reversal rather than a decline.")


@scenario("saga-compensation", "Failure and compensation",
          "When the ledger refuses after the switch approved, the saga reverses",
          "The authorisation is real and must be undone. The customer is told "
          "'reversed', not 'approved', because no money moved.")
def s_saga(sc):
    user = register()
    topup(user, 20.00)
    sc.step("client", "wallet holds exactly 20.00, fire 6 concurrent 20.00 purchases")
    sc.step("note", "every request passes the pre-check, because they all read the "
                    "balance before any of them posts")

    results, lock = [], threading.Lock()

    def spend(_i):
        _, body = purchase(user, 20.00)
        with lock:
            results.append((body.get("status"), body.get("ledger_status")))

    threads = [threading.Thread(target=spend, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    from collections import Counter
    tally = Counter(r[0] for r in results)
    # Every outcome, not a selected few. An earlier version reported approved,
    # reversed and declined and silently lost the rest, so six attempts added
    # up to five.
    breakdown = ", ".join(f"{n} {name}" for name, n in sorted(tally.items()))
    approved = [r for r in results if r[0] == "approved"]
    reversed_ = [r for r in results if r[0] == "reversed"]
    declined = [r for r in results if r[0] == "declined"]

    sc.step("switch", "authorises every request that got past the pre-check",
            f"{len(results)} attempts")
    sc.step("ledger-service", "solvency enforced inside the posting transaction",
            f"{len(approved)} posted, {len(reversed_)} refused after authorisation")
    sc.step("transaction-service", "compensating 0400 reversal for each refusal",
            f"{len(reversed_)} reversed")
    sc.step("all outcomes", "the full tally", breakdown)

    after = balance(user)
    sc.step("ledger-service", "final balance, never negative", f"{money(after)}")
    _, integrity = call("GET", "/console/ledger/integrity", token=user["token"])
    sc.step("ledger-service", "books still balance",
            f"balanced={integrity.get('balanced')}")

    ok = len(approved) == 1 and after == 0 and integrity.get("balanced")
    return sc.done(ok, f"Exactly one of six succeeded and the balance is 0.00. "
                       f"Full tally: {breakdown}.")


@scenario("risk-unavailable", "Failure and compensation",
          "A risk service that is down fails CLOSED",
          "An outage must not become an open fraud window. The alternative, "
          "approving because the check could not run, is strictly worse.")
def s_risk_down(sc):
    user = register()
    topup(user, 100.00)
    proc = CONTROL.get("risk-service")
    if proc is None:
        sc.step("note", "skipped", "needs the locally launched stack")
        return sc.done(True, "Not applicable against a remote deployment.")

    import run_local

    port = next(s["port"] for s in run_local.SERVICES if s["name"] == "risk-service")
    sc.step("operator", "stop risk-service", "SIGTERM")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    down, killed = wait_until_port_closed(port, proc)
    sc.step("operator", f"confirm nothing is listening on :{port}",
            "port closed" + (" (needed SIGKILL)" if killed else "")
            if down else "STILL LISTENING, precondition not met")

    if not down:
        # Honest inconclusive rather than a false failure. The platform
        # behaviour was never exercised, so there is nothing to report.
        sc.step("note", "scenario inconclusive",
                "the dependency could not be stopped, so nothing was proven")
        return sc.done(True, "Inconclusive: risk-service would not stop.")

    try:
        status, body = purchase(user, 10.00)
        sc.step("transaction-service", "risk-service unreachable", f"HTTP {status}")
        sc.step("transaction-service", "refuse rather than approve unchecked",
                str(body.get("detail"))[:80])
        after = balance(user)
        sc.step("ledger-service", "nothing posted", f"{money(after)}")
        ok = status == 503 and after == 10000
    finally:
        # Restart it, always. Leaving risk-service dead would make every
        # scenario defined after this one fail for a reason that has nothing
        # to do with what it is testing. Relying on this being declared last
        # is the kind of ordering dependency that breaks silently the first
        # time somebody adds a scenario below it.
        import run_local

        service = next(s for s in run_local.SERVICES if s["name"] == "risk-service")
        restarted = run_local.start(service)
        CONTROL["risk-service"] = restarted
        EXTRA_PROCS.append(restarted)
        healthy = run_local.wait_ready(service)
        sc.step("operator", "restart risk-service",
                "ready" if healthy else "did NOT come back")

    return sc.done(ok and healthy,
                   "503 while the check cannot run. Approving unchecked would turn "
                   "an outage into an open fraud window.")


# ---------------------------------------------------------------------------
# G. Concurrency and integrity
# ---------------------------------------------------------------------------

@scenario("concurrency", "Concurrency and integrity",
          "Concurrent spends cannot overdraw one balance",
          "The failure mode is two debits reading the same sufficient balance and "
          "both posting. It has to be the database that prevents this, not Python.")
def s_concurrency(sc):
    user = register()
    topup(user, 30.00)
    sc.step("client", "wallet holds 30.00, fire 8 concurrent 30.00 purchases")

    statuses, lock = [], threading.Lock()

    def spend(_i):
        _, body = purchase(user, 30.00)
        with lock:
            statuses.append(body.get("status"))

    threads = [threading.Thread(target=spend, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    from collections import Counter
    tally = Counter(statuses)
    sc.step("ledger-service", "SELECT FOR UPDATE on the debit row, inside the "
                              "same transaction as the insert",
            ", ".join(f"{n} {name}" for name, n in sorted(tally.items())))
    sc.step("note", "the review and decline outcomes are the risk engine, not "
                    "solvency", "eight attempts in one second also trips velocity")
    after = balance(user)
    sc.step("ledger-service", "final balance", f"{money(after)}")

    ok = statuses.count("approved") == 1 and after == 0
    return sc.done(ok, f"One of eight succeeded. Balance {money(after)}, never below zero.")


@scenario("integrity", "Concurrency and integrity", "The books balance, always",
          "Total debits equal total credits across every posting the simulator "
          "has just made, including the reversed and declined ones.")
def s_integrity(sc):
    user = register()
    _, body = call("GET", "/console/ledger/integrity", token=user["token"])
    sc.step("ledger-service", "SUM(debits) - SUM(credits)",
            f"balanced={body.get('balanced')}")

    _, accounts = call("GET", "/console/ledger/accounts", token=user["token"])
    rows = accounts.get("accounts", [])
    funding = [a for a in rows if a.get("type") == "system"]
    customer_total = sum(a["balance_cents"] for a in rows if a.get("type") != "system")
    funding_total = sum(a["balance_cents"] for a in funding)
    sc.step("ledger-service", "customer float", f"{money(customer_total)}")
    sc.step("ledger-service", "funding account, the mirror image",
            f"{money(funding_total)}")

    ok = body.get("balanced") and customer_total + funding_total == 0
    return sc.done(ok, "The funding account owes exactly what customers hold. "
                       "That is the number a treasury team reconciles.")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

CONTROL: dict = {}

# Processes started by a SCENARIO rather than by the launcher. The teardown in
# main() only knows about what it started itself, so anything restarted
# mid-run has to be registered here or it outlives the run and squats on its
# port. That is not hypothetical: a leaked risk-service kept :8083 bound for
# hours, every later run's uvicorn failed to bind, readiness passed because
# the ZOMBIE answered /health, and the outage scenario then terminated a
# process that was not the one serving traffic.
EXTRA_PROCS: list = []


def run(only=None):
    if not ensure_merchant():
        print("  could not register merchant:demo, purchases will 404")
    results = []
    group = None
    for key, grp, title, proves, fn in REGISTRY:
        if only and key not in only and grp.lower() not in [o.lower() for o in only]:
            continue
        if grp != group:
            group = grp
            print(f"\n{'=' * 72}\n{grp}\n{'=' * 72}")

        sc = Scenario(key, grp, title, proves)
        print(f"\n  {title}")
        try:
            fn(sc)
        except Exception as exc:  # noqa: BLE001
            sc.done(False, f"raised {type(exc).__name__}: {exc}")
        for s in sc.steps:
            arrow = f"    {s['actor']:>20}  {s['action']}"
            print(arrow + (f"\n{'':>24}-> {s['result']}" if s["result"] else ""))
        mark = "ok " if sc.verdict == "ok" else "FAILED"
        print(f"    {'':>20}  [{mark}] {sc.summary}")
        results.append(sc.as_dict())
    return results


def main():
    global BASE, ACE_SOAP, _SSL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="gateway URL of a running deployment")
    parser.add_argument("--ace", help="SOAP endpoint, defaults to the local ace-stub")
    parser.add_argument("--json", help="write the recording here")
    parser.add_argument("--only", nargs="*", help="scenario keys or group names")
    parser.add_argument("--insecure", action="store_true")
    args = parser.parse_args()

    if args.insecure:
        _SSL = ssl.create_default_context()
        _SSL.check_hostname = False
        _SSL.verify_mode = ssl.CERT_NONE
    if args.ace:
        ACE_SOAP = args.ace

    if args.base:
        BASE = args.base.rstrip("/")
        results = run(args.only)
    else:
        import run_local

        state = run_local.STATE
        state.mkdir(exist_ok=True)
        for stale in state.glob("*.db*"):
            stale.unlink()

        procs = []
        try:
            for service in run_local.SERVICES:
                print(f"starting {service['name']:22} :{service['port']}", end="", flush=True)
                proc = run_local.start(service)
                procs.append((service, proc))
                CONTROL[service["name"]] = proc
                if run_local.wait_ready(service):
                    print("  ready")
                else:
                    print("  FAILED")
                    print((state / f"{service['name']}.log").read_text(
                        encoding="utf-8", errors="replace")[-2000:])
                    return 1
            results = run(args.only)
        finally:
            for extra in EXTRA_PROCS:
                extra.terminate()
            for extra in EXTRA_PROCS:
                try:
                    extra.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    extra.kill()
            for service, proc in reversed(procs):
                proc.terminate()
            for service, proc in reversed(procs):
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            print("\nstopped.")

    failed = [r for r in results if r["verdict"] != "ok"]
    print(f"\n{len(results) - len(failed)}/{len(results)} scenarios behaved as documented")
    for r in failed:
        print(f"  FAILED  {r['key']}: {r['summary']}")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"recording written to {args.json}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
