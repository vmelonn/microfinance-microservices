"""
End-to-end smoke test against a running platform.

    docker compose up --build
    python scripts/smoke_test.py

Drives the full path from outside, exactly as a mobile client would:

    REST -> api-gateway -> auth / risk / ledger -> iso8583-adapter
         -> SOAP -> ace-stub -> ISO 8583 over TCP -> host-simulator
         -> back up the same way

Uses only the standard library, so it runs with no install.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

# Defaults target docker-compose, which publishes the two gateway replicas on
# separate host ports. Against OpenShift there is ONE Route in front of both
# replicas, so --base is passed and the two "replicas" below become the same
# address, which weakens the cross-replica checks rather than breaking
# them: the Route load-balances, so repeated calls still land on different
# pods, just not deterministically. Compose remains the place that proves it
# conclusively.
GATEWAY_1 = "http://localhost:8080"
GATEWAY_2 = "http://localhost:8081"   # the second replica

# Set by --insecure. An OpenShift Route frequently terminates TLS with a cert
# signed by the cluster's own CA, which no CI runner trusts by default, and
# the failure is CERTIFICATE_VERIFY_FAILED, which says nothing about it being
# a trust problem rather than the service being down. Opt-in only, so nobody
# turns verification off without meaning to.
_SSL_CONTEXT = None

# Set by --base. Only changes the ADVICE printed on failure: "docker compose
# up" is actively misleading when the target is an OpenShift Route.
TARGETING_ROUTE = False

PASS, FAIL = "  PASS", "  FAIL"
failures = []


def call(base, method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=45, context=_SSL_CONTEXT) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc)}


def check(label, condition, detail=""):
    print(f"{PASS if condition else FAIL}  {label}" + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def register(base):
    msisdn = "03" + str(uuid.uuid4().int)[:9]
    card = "4" + str(uuid.uuid4().int)[:15]
    password = "a-real-password-123"

    status, body = call(base, "POST", "/users/register", {
        "full_name": "Smoke Test", "msisdn": msisdn, "bind_card_number": card, "password": password,
    })
    if status != 200:
        print(f"{FAIL}  registration failed: {status} {body}")
        sys.exit(1)

    status, token_body = call(base, "POST", "/auth/login", {"msisdn": msisdn, "password": password})
    if status != 200:
        print(f"{FAIL}  login failed: {status} {token_body}")
        sys.exit(1)

    return token_body["access_token"], card, body["account_id"]


def wait_for_gateway(base, timeout=120):
    """
    Poll /health until the gateway answers, or give up and say why.

    WHY A WAIT AND NOT A SINGLE CALL. A deployment reporting readyReplicas ==
    replicas is not the same as the Route serving traffic. The router has to
    observe the new endpoints and drop the terminating ones, and that lags the
    Deployment status by a few seconds. Failing on the first attempt turns
    that ordinary lag into a red pipeline, which is exactly what happened:
    the rollout gate passed and the very next command got nothing.

    Every distinct failure is reported once rather than every poll, so the log
    shows the sequence of states instead of forty identical lines.
    """
    deadline = time.time() + timeout
    seen, last = set(), None

    while time.time() < deadline:
        status, body = call(base, "GET", "/health")
        if status == 200:
            return True, status, body
        signature = (status, str(body)[:120])
        if signature not in seen:
            seen.add(signature)
            remaining = int(deadline - time.time())
            print(f"        waiting for the gateway: {_explain(status, body)} "
                  f"({remaining}s left)")
        last = (status, body)
        time.sleep(3)

    return False, last[0] if last else 0, last[1] if last else {}


def _explain(status, body):
    """Turn a status into the thing that is actually wrong."""
    if status == 0:
        return (f"cannot connect. DNS, TLS or the host is wrong: "
                f"{str(body.get('error', body))[:100]}")
    if status == 503:
        return "503 from the router: the Route exists but has no ready endpoints behind it"
    if status in (502, 504):
        return f"{status} from the router: an endpoint exists but is not answering"
    if status == 404:
        return "404: reached a router, but nothing is routed for this host"
    return f"HTTP {status}: {str(body)[:100]}"


def main():
    print("\n=== 0. the platform is up ===")
    up, status, body = wait_for_gateway(GATEWAY_1)
    if not up:
        print(f"{FAIL}  api-gateway never answered on {GATEWAY_1}")
        print(f"        {_explain(status, body)}")
        if TARGETING_ROUTE:
            print("        Check:  oc get pods -o wide")
            print("                oc get endpoints api-gateway")
            print("                oc logs deploy/api-gateway --tail=50")
        else:
            print("        Run: docker compose up --build")
        sys.exit(1)
    check("api-gateway healthy", True)

    status, ready = call(GATEWAY_1, "GET", "/ready")
    check("api-gateway ready", status == 200, json.dumps(ready))

    print("\n=== 1. register a merchant, so purchases have somewhere to credit ===")
    merchant_msisdn = "03" + str(uuid.uuid4().int)[:9]
    status, _ = call(GATEWAY_1, "POST", "/users/register", {
        "full_name": "Demo Merchant", "msisdn": merchant_msisdn,
        "bind_card_number": "merchant:demo", "password": "merchant-password-1",
    })
    check("merchant registered", status in (200, 409), "409 is fine, already exists")

    print("\n=== 2. register a cardholder and log in ===")
    token, card, account_id = register(GATEWAY_1)
    check("registered and authenticated", bool(token), "")
    print(f"        card={card[:6]}...{card[-4:]}  account={account_id}")

    print("\n=== 3. an empty wallet cannot spend ===")
    # Accounts open at zero and overdrafts are refused, so this is the state
    # every real customer is in immediately after registering.
    status, broke = call(GATEWAY_1, "POST", "/transactions/purchase", {
        "amount": 25.50, "card_number": card, "pin": "1234",
        "idempotency_key": f"broke-{uuid.uuid4().hex[:12]}",
    }, token=token)
    check("purchase on a zero balance declined",
          status == 200 and broke.get("status") == "declined", f"{status} {json.dumps(broke)}")

    print("\n=== 4. top up ===")
    status, topped = call(GATEWAY_1, "POST", "/transactions/topup", {
        "amount": 100.00, "card_number": card,
        "idempotency_key": f"tu-{uuid.uuid4().hex[:12]}",
    }, token=token)
    check("top-up approved", status == 200 and topped.get("status") == "approved",
          f"{status} {json.dumps(topped)}")

    print("\n=== 5. purchase: REST -> SOAP -> ISO 8583 -> SOAP -> REST ===")
    key = f"smoke-{uuid.uuid4().hex[:12]}"
    purchase = {"amount": 25.50, "card_number": card, "pin": "1234", "idempotency_key": key}

    status, result = call(GATEWAY_1, "POST", "/transactions/purchase", purchase, token=token)
    check("purchase approved", status == 200 and result.get("status") == "approved",
          f"{status} {json.dumps(result)}")
    check("RRN returned", bool(result.get("rrn")), json.dumps(result))
    check("posted to the ledger", result.get("ledger_status") == "recorded", json.dumps(result))
    print(f"        rrn={result.get('rrn')}  stan={result.get('stan')}  auth={result.get('authorization_id')}")

    print("\n=== 6. the debit actually landed ===")
    status, balance = call(GATEWAY_1, "GET", f"/accounts/{card}/balance", token=token)
    check("balance reflects the debit", status == 200 and balance.get("balance_cents") == 7450,
          json.dumps(balance))

    print("\n=== 7. idempotency: same key, DIFFERENT replica ===")
    # Replica 2 has never seen this request. Only shared Redis state can make
    # it return the identical result rather than charging a second time.
    status, replay = call(GATEWAY_2, "POST", "/transactions/purchase", purchase, token=token)
    check("replica 2 returned the cached result", status == 200 and replay.get("rrn") == result.get("rrn"),
          f"{status} {json.dumps(replay)}")

    status, balance_after = call(GATEWAY_1, "GET", f"/accounts/{card}/balance", token=token)
    check("balance UNCHANGED after the replay", balance_after.get("balance_cents") == 7450,
          f"charged twice: {json.dumps(balance_after)}")

    print("\n=== 8. spending more than you hold ===")
    status, over = call(GATEWAY_1, "POST", "/transactions/purchase", {
        "amount": 500.00, "card_number": card, "pin": "1234",
        "idempotency_key": f"over-{uuid.uuid4().hex[:12]}",
    }, token=token)
    check("overspend declined", status == 200 and over.get("status") == "declined",
          f"{status} {json.dumps(over)}")
    status, intact = call(GATEWAY_1, "GET", f"/accounts/{card}/balance", token=token)
    check("balance untouched by the declined attempt", intact.get("balance_cents") == 7450,
          json.dumps(intact))

    print("\n=== 9. same key, different body, must be rejected ===")
    tampered = dict(purchase, amount=999.00)
    status, _ = call(GATEWAY_1, "POST", "/transactions/purchase", tampered, token=token)
    check("idempotency key reuse rejected", status == 400, f"got {status}")

    print("\n=== 10. risk velocity escalates ACROSS replicas ===")
    velocity_token, velocity_card, _ = register(GATEWAY_1)
    # Funded first. Otherwise all seven attempts decline for insufficient
    # funds, the velocity rule is never reached, and the check passes for
    # entirely the wrong reason.
    call(GATEWAY_1, "POST", "/transactions/topup", {
        "amount": 100.00, "card_number": velocity_card,
        "idempotency_key": f"tu-{uuid.uuid4().hex[:12]}",
    }, token=velocity_token)
    outcomes = []
    for i in range(7):
        target = GATEWAY_1 if i % 2 == 0 else GATEWAY_2   # alternate every attempt
        _, body = call(target, "POST", "/transactions/purchase", {
            "amount": 5.00, "card_number": velocity_card, "pin": "1234",
            "idempotency_key": f"vel-{uuid.uuid4().hex[:12]}",
        }, token=velocity_token)
        outcomes.append(body.get("status"))
    print(f"        {' -> '.join(str(o) for o in outcomes)}")
    check("velocity escalated despite alternating replicas",
          any(o in ("review", "decline") for o in outcomes),
          "shared velocity state is NOT working")
    # "decline" is risk's word, "declined" is the solvency pre-check's. If the
    # latter appears the top-up above did not land and this section proved
    # nothing about velocity.
    check("the escalation came from risk, not from an empty wallet",
          "declined" not in outcomes, str(outcomes))

    print("\n=== 11. authorization: you cannot spend from someone else's card ===")
    other_token, _other_card, _ = register(GATEWAY_1)
    status, _ = call(GATEWAY_1, "POST", "/transactions/purchase", {
        "amount": 10.00, "card_number": card, "pin": "1234",   # the FIRST user's card
        "idempotency_key": f"authz-{uuid.uuid4().hex[:12]}",
    }, token=other_token)
    check("cross-account spend refused", status == 403, f"got {status}")

    print("\n=== 12. an unregistered card is refused ===")
    status, _ = call(GATEWAY_1, "POST", "/transactions/purchase", {
        "amount": 10.00, "card_number": "4000000000009999", "pin": "1234",
        "idempotency_key": f"unknown-{uuid.uuid4().hex[:12]}",
    }, token=token)
    check("unknown card refused", status == 404, f"got {status}")

    print("\n=== 13. no token, no transaction ===")
    status, _ = call(GATEWAY_1, "POST", "/transactions/purchase", purchase)
    check("unauthenticated request refused", status == 401, f"got {status}")

    print("\n=== 14. the console is actually served ===")
    # Added because a pipeline went fully green while api-gateway was running
    # an image that contained no console at all. Every other check passed: the
    # API worked, readiness passed, the rollout completed. Nothing verified
    # that the thing the deploy existed FOR had actually shipped.
    #
    # This checks the IMAGE, not the config. ENABLE_CONSOLE can be set
    # correctly, present in the ConfigMap and visible in the process
    # environment, and the page still be missing because the running image
    # predates it. That combination produces a plain 404 rather than an error,
    # which is why it went unnoticed.
    try:
        request = urllib.request.Request(GATEWAY_1 + "/")
        with urllib.request.urlopen(request, timeout=20, context=_SSL_CONTEXT) as resp:
            body = resp.read().decode(errors="replace")
            served = resp.status == 200 and "microfinance console" in body
    except Exception as exc:  # noqa: BLE001
        served = False
        body = repr(exc)
    check(
        "console page served at /", served,
        "a 404 here means the running IMAGE predates the console even when "
        "ENABLE_CONSOLE=1. Fix: oc start-build api-gateway --wait",
    )

    status, _ = call(GATEWAY_1, "GET", "/console/traces")
    check("console API rejects an unauthenticated caller", status == 401, f"got {status}")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("All smoke checks passed.")
    print("REST -> SOAP -> ISO 8583 -> SOAP -> REST works end to end.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        help=(
            "Gateway base URL. Use this against an OpenShift Route, e.g. "
            "--base https://$(oc get route api-gateway -o jsonpath='{.spec.host}'). "
            "Omit it for docker-compose, which exposes both replicas separately."
        ),
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS verification. Needed when the Route uses the cluster's own CA.",
    )
    args = parser.parse_args()

    if args.base:
        base = args.base.rstrip("/")
        GATEWAY_1 = GATEWAY_2 = base
        TARGETING_ROUTE = True
        print(f"targeting {base} (single endpoint, the Route load-balances across replicas)")

    if args.insecure:
        import ssl

        _SSL_CONTEXT = ssl.create_default_context()
        _SSL_CONTEXT.check_hostname = False
        _SSL_CONTEXT.verify_mode = ssl.CERT_NONE
        print("TLS verification DISABLED (--insecure)")

    sys.exit(main())
