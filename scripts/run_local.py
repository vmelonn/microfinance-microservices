"""
Run the whole platform as ordinary processes, no Docker required.

    python scripts/run_local.py            # start, wait for ready, then idle
    python scripts/run_local.py --verify   # start, run the checks, tear down

WHY THIS EXISTS ALONGSIDE docker-compose. Compose is the honest local mirror
of the cluster and is what you should normally use. But it needs a running
container engine, and "the engine is not up" should not be the difference
between being able to prove the platform works and not. Every service is a
plain uvicorn process; SQLite stands in for Postgres and in-memory state for
Redis.

WHAT THIS CANNOT PROVE, and compose can:
  - cross-replica idempotency and velocity, since without Redis each process
    keeps its own state. That is the single most important thing compose
    demonstrates, and it is genuinely absent here.
  - anything about the images, the arbitrary-UID handling, or the manifests.

What it does prove is the part that matters most and is hardest to reason
about on paper: a REST request crossing into SOAP, becoming real binary
ISO 8583 on a real socket, and coming back.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PYTHON = str(VENV_PY) if VENV_PY.exists() else sys.executable

STATE = ROOT / ".local-run"

# Order matters: each service's readiness probe depends on the ones before it.
# ace-stub refuses to start at all without a reachable switch, which is
# deliberate, a gateway that starts "successfully" with no switch connection
# passes its probe and then fails every transaction.
SERVICES = [
    {
        "name": "host-simulator",
        "port": 9999,
        "module": None,                 # not an ASGI app; runs as a plain script
        "cmd": ["-m", "app.main"],
        "env": {"SIMULATOR_HOST": "127.0.0.1", "SIMULATOR_PORT": "9999"},
        "probe": None,                  # raw TCP, no HTTP endpoint
    },
    {
        "name": "ace-stub",
        "port": 8090,
        "module": "app.main:app",
        "env": {
            "SWITCH_HOST": "127.0.0.1",
            "SWITCH_PORT": "9999",
            "SWITCH_TIMEOUT_SECONDS": "10",
            "WSDL_PATH": str(ROOT / "ace" / "Iso8583Library" / "wsdl" / "Iso8583Gateway.wsdl"),
        },
    },
    {
        "name": "iso8583-adapter",
        "port": 8085,
        "module": "app.main:app",
        "env": {
            "ISO8583_SOAP_ENDPOINT": "http://127.0.0.1:8090/Iso8583Gateway",
            "ISO8583_SOAP_TIMEOUT_SECONDS": "20",
        },
    },
    {
        "name": "ledger-service",
        "port": 8084,
        "module": "app.main:app",
        "env": {"LEDGER_DSN": str(STATE / "ledger.db"), "ALLOW_LEDGER_RESET": "1"},
    },
    {
        "name": "auth-service",
        "port": 8081,
        "module": "app.main:app",
        "env": {"AUTH_DSN": str(STATE / "auth.db")},
    },
    {
        "name": "risk-service",
        "port": 8083,
        "module": "app.main:app",
        "env": {},                      # REDIS_URL only if TRACE_REDIS_URL is set
    },
    {
        "name": "transaction-service",
        "port": 8082,
        "module": "app.main:app",
        "env": {
            "RISK_SERVICE_URL": "http://127.0.0.1:8083",
            "LEDGER_SERVICE_URL": "http://127.0.0.1:8084",
            "ISO8583_ADAPTER_URL": "http://127.0.0.1:8085",
        },
    },
    {
        "name": "api-gateway",
        # 18080, not 8080: 8080 is very commonly already taken on a dev
        # machine, and the failure is an opaque WinError 10013 rather than
        # anything mentioning a port conflict. Compose and OpenShift both
        # still use 8080, only this local runner differs.
        "port": 18080,
        "module": "app.main:app",
        "env": {
            "AUTH_SERVICE_URL": "http://127.0.0.1:8081",
            "TRANSACTION_SERVICE_URL": "http://127.0.0.1:8082",
            "LEDGER_SERVICE_URL": "http://127.0.0.1:8084",
        },
    },
]

# Tracing needs a Redis every service can reach. Without one the console's
# trace tab is empty but everything else works, so this is opportunistic:
# set TRACE_REDIS_URL before running to enable it.
TRACE_REDIS = os.environ.get("TRACE_REDIS_URL", "")

SHARED_ENV = {
    "JWT_SECRET": "local-run-shared-secret-not-for-any-real-use",
    "ENABLE_CONSOLE": "1",
    "LOG_LEVEL": "WARNING",   # keep the console readable; per-service logs go to files
    "PYTHONUNBUFFERED": "1",
}


def start(service: dict) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(SHARED_ENV)
    if TRACE_REDIS:
        env["REDIS_URL"] = TRACE_REDIS
    env.update(service["env"])
    # Each service owns a top-level `app` package, so they cannot share one
    # sys.path, two services' app.main would shadow each other. A separate
    # process per service with its own PYTHONPATH is the whole reason this
    # runs as subprocesses rather than in one interpreter.
    env["PYTHONPATH"] = str(ROOT / "services" / service["name"])

    if service["module"] is None:
        cmd = [PYTHON, *service["cmd"]]
    else:
        cmd = [PYTHON, "-m", "uvicorn", service["module"],
               "--host", "127.0.0.1", "--port", str(service["port"]), "--log-level", "warning"]

    log = open(STATE / f"{service['name']}.log", "w", encoding="utf-8")
    return subprocess.Popen(
        cmd, cwd=str(ROOT / "services" / service["name"]),
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def wait_ready(service: dict, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout

    if service["port"] == 9999:
        import socket

        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", 9999), timeout=2).close()
                return True
            except OSError:
                time.sleep(0.3)
        return False

    url = f"http://127.0.0.1:{service['port']}/ready"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except urllib.error.HTTPError:
            time.sleep(0.4)      # 503 from a readiness probe, keep waiting
        except Exception:
            time.sleep(0.4)
    return False


def call(method, path, body=None, token=None, base="http://127.0.0.1:18080"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}
    except Exception as exc:
        return 0, {"error": repr(exc)}


def verify() -> int:
    failures = []

    def check(label, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(label)

    print("\n--- register merchant + cardholder ---")
    status, _ = call("POST", "/users/register", {
        "full_name": "Demo Merchant", "msisdn": "03" + str(uuid.uuid4().int)[:9],
        "bind_card_number": "merchant:demo", "password": "merchant-password-1",
    })
    check("merchant registered", status in (200, 409), str(status))

    msisdn = "03" + str(uuid.uuid4().int)[:9]
    card = "4" + str(uuid.uuid4().int)[:15]
    status, reg = call("POST", "/users/register", {
        "full_name": "Local Runner", "msisdn": msisdn,
        "bind_card_number": card, "password": "a-real-password-123",
    })
    check("cardholder registered", status == 200, f"{status} {reg}")

    status, tok = call("POST", "/auth/login", {"msisdn": msisdn, "password": "a-real-password-123"})
    check("login issued a token", status == 200 and "access_token" in tok, f"{status} {tok}")
    token = tok.get("access_token")

    print("\n--- purchase: REST -> SOAP -> ISO 8583 -> SOAP -> REST ---")
    key = f"local-{uuid.uuid4().hex[:10]}"
    purchase = {"amount": 25.50, "card_number": card, "pin": "1234", "idempotency_key": key}
    status, result = call("POST", "/transactions/purchase", purchase, token=token)

    check("purchase approved", status == 200 and result.get("status") == "approved", f"{status} {result}")
    check("RRN returned", bool(result.get("rrn")), str(result))
    check("STAN assigned by the gateway", bool(result.get("stan")), str(result))
    check("authorization ID from the switch", result.get("authorization_id") == "A18008", str(result))
    check("posted to the ledger", result.get("ledger_status") == "recorded", str(result))
    check("DE 39 decoded to text",
          result.get("reason") == "Approved / completed successfully", str(result.get("reason")))
    print(f"        rrn={result.get('rrn')}  stan={result.get('stan')}  auth={result.get('authorization_id')}")

    print("\n--- the debit actually landed ---")
    status, balance = call("GET", f"/accounts/{card}/balance", token=token)
    check("balance is -2550 cents", status == 200 and balance.get("balance_cents") == -2550, str(balance))

    print("\n--- idempotent replay, same process ---")
    status, replay = call("POST", "/transactions/purchase", purchase, token=token)
    check("replay returned the cached result", status == 200 and replay.get("rrn") == result.get("rrn"),
          f"{status} {replay}")
    status, after = call("GET", f"/accounts/{card}/balance", token=token)
    check("balance unchanged after the replay", after.get("balance_cents") == -2550, str(after))

    print("\n--- key reuse with a different body is rejected ---")
    status, _ = call("POST", "/transactions/purchase", dict(purchase, amount=999.0), token=token)
    check("mismatched body rejected", status == 400, f"got {status}")

    print("\n--- authorization ---")
    other_msisdn = "03" + str(uuid.uuid4().int)[:9]
    call("POST", "/users/register", {
        "full_name": "Someone Else", "msisdn": other_msisdn,
        "bind_card_number": "4" + str(uuid.uuid4().int)[:15], "password": "another-password-1",
    })
    _, other_tok = call("POST", "/auth/login", {"msisdn": other_msisdn, "password": "another-password-1"})
    status, _ = call("POST", "/transactions/purchase", {
        "amount": 10.0, "card_number": card, "pin": "1234",
        "idempotency_key": f"authz-{uuid.uuid4().hex[:10]}",
    }, token=other_tok.get("access_token"))
    check("cannot spend from someone else's card", status == 403, f"got {status}")

    status, _ = call("POST", "/transactions/purchase", purchase)
    check("unauthenticated request refused", status == 401, f"got {status}")

    print("\n--- risk velocity escalates ---")
    vel_msisdn = "03" + str(uuid.uuid4().int)[:9]
    vel_card = "4" + str(uuid.uuid4().int)[:15]
    call("POST", "/users/register", {
        "full_name": "Velocity Test", "msisdn": vel_msisdn,
        "bind_card_number": vel_card, "password": "velocity-password-1",
    })
    _, vt = call("POST", "/auth/login", {"msisdn": vel_msisdn, "password": "velocity-password-1"})
    outcomes = []
    for _ in range(7):
        _, body = call("POST", "/transactions/purchase", {
            "amount": 5.0, "card_number": vel_card, "pin": "1234",
            "idempotency_key": f"vel-{uuid.uuid4().hex[:10]}",
        }, token=vt.get("access_token"))
        outcomes.append(body.get("status"))
    print(f"        {' -> '.join(str(o) for o in outcomes)}")
    check("velocity escalated to review or decline",
          any(o in ("review", "decline") for o in outcomes), str(outcomes))

    print("\n--- transfer to a PHONE NUMBER ---")
    # The MSISDN is the account. This proves a payee resolves by the number a
    # customer would actually type rather than only by an opaque acc_ id, and
    # that normalisation agrees across services: auth-service stores the
    # number, ledger-service looks it up, and if those two disagree by one
    # character registration succeeds while every transfer silently misses.
    peer_msisdn = "0300" + str(uuid.uuid4().int)[:7]
    peer_card = "4" + str(uuid.uuid4().int)[:15]
    status, peer = call("POST", "/users/register", {
        "full_name": "Peer", "msisdn": peer_msisdn,
        "bind_card_number": peer_card, "password": "a-real-password-123",
    })
    check("recipient registered by phone number", status == 200, str(peer))

    # Deliberately sent to a DIFFERENT spelling of the same number than the
    # one registered. Both must normalise to one stored value.
    spaced = f"{peer_msisdn[:4]} {peer_msisdn[4:7]} {peer_msisdn[7:]}"
    status, transfer = call("POST", "/transactions/transfer", {
        "amount": 7.50, "sender_card_number": card, "sender_pin": "1234",
        "recipient_account": spaced,
        "idempotency_key": f"xfer-{uuid.uuid4().hex[:10]}",
    }, token=token)
    check("transfer to a differently formatted number approved",
          status == 200 and transfer.get("status") == "approved",
          f"{status} {transfer}")

    # Checked against the ledger rather than the balance endpoint, because the
    # sender's token cannot read the recipient's balance. That refusal is
    # correct behaviour, not an obstacle to work around.
    #
    # Matched on the NORMALISED number that registration returned, not on the
    # "0300..." that was typed. Those differ by design, and comparing the raw
    # form here would fail while the platform was working correctly, which is
    # what happened the first time this check was written.
    stored = peer.get("msisdn")
    status, listing = call("GET", "/internal/ledger/inspect/accounts",
                           base="http://127.0.0.1:8084")
    credited = [a for a in listing.get("accounts", []) if a.get("msisdn") == stored]
    check(f"recipient {stored} credited 750 cents",
          bool(credited) and credited[0]["balance_cents"] == 750,
          f"looked for {stored!r} among "
          f"{[a.get('msisdn') for a in listing.get('accounts', [])][:5]}")

    print("\n--- the ledger balances ---")
    status, integrity = call("GET", "/internal/ledger/integrity", base="http://127.0.0.1:8084")
    check("total debits == total credits", integrity.get("balanced") is True, str(integrity))

    print("\n--- the SOAP gateway serves its WSDL ---")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8090/Iso8583Gateway?wsdl", timeout=10) as r:
            wsdl = r.read()
        check("WSDL served", b"Iso8583GatewayService" in wsdl and b"urn:microfinance:iso8583:v1" in wsdl)
    except Exception as exc:
        check("WSDL served", False, repr(exc))

    print("\n--- the console is served ---")
    # The same check the smoke test gained, for the same reason: a deploy went
    # green while api-gateway ran an image with no console in it. Everything
    # else passed, because nothing verified that the feature being deployed
    # had actually shipped.
    try:
        with urllib.request.urlopen("http://127.0.0.1:18080/", timeout=10) as r:
            page = r.read().decode(errors="replace")
        check("console page served at /", r.status == 200 and "microfinance console" in page)
    except Exception as exc:  # noqa: BLE001
        check("console page served at /", False, repr(exc))

    status, _ = call("GET", "/console/traces")
    check("console API rejects an unauthenticated caller", status == 401, f"got {status}")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed. REST -> SOAP -> ISO 8583 -> SOAP -> REST works end to end.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true", help="run the checks, then stop")
    args = parser.parse_args()

    STATE.mkdir(exist_ok=True)
    for stale in STATE.glob("*.db*"):
        stale.unlink()

    procs = []
    try:
        for service in SERVICES:
            print(f"starting {service['name']:22} :{service['port']}", end="", flush=True)
            proc = start(service)
            procs.append((service, proc))

            if wait_ready(service):
                print("  ready")
            else:
                print("  FAILED")
                log = (STATE / f"{service['name']}.log").read_text(encoding="utf-8", errors="replace")
                print(f"\n--- {service['name']} log ---\n{log[-2500:]}")
                return 1

        if not args.verify:
            print("\nAll services up. Gateway on http://127.0.0.1:18080  Â·  Ctrl+C to stop.")
            signal.pause() if hasattr(signal, "pause") else time.sleep(10 ** 6)
            return 0

        return verify()

    finally:
        for service, proc in reversed(procs):
            proc.terminate()
        for service, proc in reversed(procs):
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("stopped.")


if __name__ == "__main__":
    sys.exit(main())
