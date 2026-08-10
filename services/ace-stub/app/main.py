"""
ace-stub -- a SOAP/ISO 8583 gateway that stands in for IBM ACE.

Serves ace/Iso8583Library/wsdl/Iso8583Gateway.wsdl verbatim, accepts the
same envelopes, holds a real TCP connection to the switch, and speaks real
binary ISO 8583. Nothing about it is fake except that it is Python instead
of an ACE integration server.

TO SWAP IN THE REAL ACE:
    ISO8583_SOAP_ENDPOINT=http://ace:7800/Iso8583Gateway
That is the entire migration. No application code changes anywhere.

WHY THIS EXISTS: the IBM entitlement had not come through, and "we will
build the SOAP layer once the licence arrives" would have left the single
riskiest integration in the platform untested until the last possible
moment. Instead the contract is exercised end-to-end from day one, and ACE
arrives into a slot whose shape is already proven.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response

from mfcommon.iso8583.client import ISO8583Client
from mfcommon.iso8583.correlation import CorrelationManager, TransactionTimeout
from mfcommon.observability.audit import mask_fields
from mfcommon.observability.correlation import configure_logging, set_correlation_id
from mfcommon.soap.envelope import (
    SoapProtocolError,
    build_fault,
    build_response,
    parse_request,
)

from app.mediation import (
    MediationError,
    iso_to_soap_authorize,
    iso_to_soap_network,
    iso_to_soap_reverse,
    soap_to_iso_authorize,
    soap_to_iso_network,
    soap_to_iso_reverse,
)

SWITCH_HOST = os.environ.get("SWITCH_HOST", "127.0.0.1")
SWITCH_PORT = int(os.environ.get("SWITCH_PORT", "9999"))
SWITCH_TIMEOUT = float(os.environ.get("SWITCH_TIMEOUT_SECONDS", "10"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "3"))
CONNECT_TIMEOUT = float(os.environ.get("SWITCH_CONNECT_TIMEOUT_SECONDS", "15"))

WSDL_PATH = Path(
    os.environ.get("WSDL_PATH", "/app/wsdl/Iso8583Gateway.wsdl")
)

log = configure_logging("ace-stub", os.environ.get("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = ISO8583Client(
        SWITCH_HOST,
        SWITCH_PORT,
        heartbeat_interval=HEARTBEAT_INTERVAL,
    )
    client.connect()

    # Fail fast and loudly. A gateway that starts "successfully" without a
    # switch connection passes its startup probe and then rejects every
    # transaction -- far worse than refusing to start, because OpenShift
    # will not roll it back.
    deadline = time.monotonic() + CONNECT_TIMEOUT
    while not client._connected.is_set():
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"ace-stub could not reach the switch at {SWITCH_HOST}:{SWITCH_PORT} "
                f"within {CONNECT_TIMEOUT}s. Refusing to start."
            )
        time.sleep(0.05)

    app.state.client = client
    app.state.correlator = CorrelationManager(client, timeout_seconds=SWITCH_TIMEOUT)
    log.info(f"connected to switch {SWITCH_HOST}:{SWITCH_PORT}, serving SOAP on /Iso8583Gateway")

    yield

    client.close()


app = FastAPI(title="ISO 8583 SOAP Gateway (ACE stand-in)", lifespan=lifespan)


# --------------------------------------------------------------------------
# WSDL discovery
# --------------------------------------------------------------------------

@app.get("/Iso8583Gateway")
def serve_wsdl(wsdl: str | None = None):
    """
    ?wsdl returns the contract. Real ACE exposes the same thing at the same
    path, so tooling pointed at either gets an identical document -- which
    is exactly the property that makes the swap safe.
    """
    if wsdl is None:
        return Response(
            content="Append ?wsdl for the service contract. POST here for SOAP operations.",
            media_type="text/plain",
        )
    if not WSDL_PATH.exists():
        return Response(
            content=f"WSDL not found at {WSDL_PATH}", status_code=500, media_type="text/plain"
        )
    return Response(content=WSDL_PATH.read_bytes(), media_type="text/xml")


# --------------------------------------------------------------------------
# The SOAP endpoint
# --------------------------------------------------------------------------

_OPERATIONS = {
    "authorizeRequest": (soap_to_iso_authorize, iso_to_soap_authorize, "authorizeResponse"),
    "reverseRequest": (soap_to_iso_reverse, iso_to_soap_reverse, "reverseResponse"),
    "networkManagementRequest": (soap_to_iso_network, iso_to_soap_network, "networkManagementResponse"),
}


def _fault(code: str, message: str, status: int, detail: str | None = None) -> Response:
    # Faults go out as HTTP 500 per SOAP 1.1 convention. The body is what
    # carries meaning; mfcommon's parse_response checks for a Fault element
    # before it ever looks at the status code.
    return Response(
        content=build_fault(code, message, detail),
        media_type="text/xml",
        status_code=status,
    )


@app.post("/Iso8583Gateway")
async def soap_endpoint(request: Request):
    raw = await request.body()

    try:
        operation, fields, headers = parse_request(raw)
    except SoapProtocolError as exc:
        return _fault("soapenv:Client", f"Malformed SOAP request: {exc}", 500)

    # Adopt the caller's correlation ID so this service's logs join the same
    # trace as every other hop, including the REST ones either side.
    if headers.get("correlation_id"):
        set_correlation_id(headers["correlation_id"])

    handler = _OPERATIONS.get(operation)
    if handler is None:
        return _fault(
            "soapenv:Client",
            f"Unknown operation {operation!r}. Supported: {', '.join(sorted(_OPERATIONS))}",
            500,
        )

    to_iso, to_soap, response_tag = handler

    try:
        mti, de_fields = to_iso(fields)
    except MediationError as exc:
        # A validation failure is a Client fault: the identical request will
        # fail identically, so the caller must not retry it.
        return _fault("soapenv:Client", str(exc), 500, detail=f"category={exc.category}")

    log.info(f"{operation} -> MTI {mti} fields={mask_fields(de_fields)}")

    correlator = request.app.state.correlator
    try:
        parsed = correlator.send_and_wait(mti, de_fields)
    except TransactionTimeout as exc:
        # The critical path. A reversal has ALREADY been sent by the
        # correlation manager, but the business outcome is still unknown --
        # the switch may have approved before going quiet. The caller is
        # told this explicitly via the SWITCH_TIMEOUT category so it can
        # avoid recording a ledger posting for a transaction it cannot
        # confirm either way.
        log.error(f"switch timeout on {operation}: {exc}")
        return _fault(
            "soapenv:Server",
            f"Switch did not respond within {SWITCH_TIMEOUT}s -- outcome UNKNOWN, reversal sent",
            504,
            detail="category=SWITCH_TIMEOUT",
        )
    except ConnectionError as exc:
        # Never reached the switch, so nothing was authorized and no
        # reversal is needed. Deliberately distinct from a timeout.
        log.error(f"switch unreachable on {operation}: {exc!r}")
        return _fault(
            "soapenv:Server",
            f"No connection to the switch: {exc}",
            503,
            detail="category=SWITCH_DOWN",
        )

    log.info(f"{operation} <- MTI {parsed['mti']} fields={mask_fields(parsed['fields'])}")

    if operation == "authorizeRequest":
        body = to_soap(parsed, fields.get("rrn", ""))
    else:
        body = to_soap(parsed)

    return Response(content=build_response(response_tag, body), media_type="text/xml")


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness: is this process alive? Deliberately does NOT check the
    switch -- a liveness probe that fails on a downstream outage makes
    OpenShift restart a healthy pod, which fixes nothing and loses the
    in-flight requests."""
    return {"status": "ok", "service": "ace-stub"}


@app.get("/ready")
def ready(request: Request):
    """Readiness: should this pod receive traffic? Here the switch
    connection genuinely is the answer -- without it every request fails,
    so the pod should be taken out of the Service's endpoint list."""
    connected = request.app.state.client._connected.is_set()
    return Response(
        content='{"status":"%s","switch_connected":%s}'
        % ("ready" if connected else "not_ready", "true" if connected else "false"),
        media_type="application/json",
        status_code=200 if connected else 503,
    )
