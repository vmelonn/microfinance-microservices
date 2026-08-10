"""
iso8583-adapter -- the REST/SOAP boundary, and the platform's crypto edge.

Two responsibilities, both of which are the reason this is a separate pod:

1. PROTOCOL. It is the only service that speaks SOAP. Everything above it is
   REST/JSON; below it, through ACE, is binary ISO 8583. Confining the
   translation to one deployable means swapping ace-stub for a real ACE
   integration server touches exactly one Deployment.

2. KEY MATERIAL. It is the only service that holds the HSM base key and
   therefore the only one that can produce a PIN block. That makes it the
   PCI-relevant blast radius: one pod, one ServiceAccount with the KMS role,
   one NetworkPolicy. Had PIN encryption stayed inside the orchestrator, the
   sensitive scope would be smeared across the service that also talks to
   risk, the ledger, and the gateway.

THE TIMEOUT CONTRACT, restated because it is the thing most easily got wrong:

A timeout is not a decline. When ACE reports SWITCH_TIMEOUT the transaction
may well have been authorized and the cardholder already debited -- only the
response was lost. This service therefore returns outcome="unknown", never
"declined", and reports whether a reversal was issued. transaction-service
uses that to decide NOT to post to the ledger. Any code that collapses
"unknown" into "failed" will eventually take money from someone and record
nothing.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from mfcommon.observability.audit import mask_pan
from mfcommon.observability.correlation import (
    CORRELATION_HEADER,
    configure_logging,
    set_correlation_id,
)
from mfcommon.security.kms import LocalKeyManagementService
from mfcommon.security.mock_hsm import MockHSM
from mfcommon.soap.client import Iso8583SoapClient, SoapTimeout, SoapTransportError
from mfcommon.soap.envelope import SoapFault

SOAP_ENDPOINT = os.environ.get("ISO8583_SOAP_ENDPOINT", "http://ace-stub:8090/Iso8583Gateway")
SOAP_TIMEOUT = float(os.environ.get("ISO8583_SOAP_TIMEOUT_SECONDS", "20"))
SOAP_USERNAME = os.environ.get("ISO8583_SOAP_USERNAME")
SOAP_PASSWORD = os.environ.get("ISO8583_SOAP_PASSWORD")

HSM_KEY_PATH = os.environ.get("HSM_KEY_PERSISTENCE_PATH")
HSM_MASTER_KEY_HEX = os.environ.get("HSM_MASTER_KEY_HEX")

DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY_CODE", "840")

log = configure_logging("iso8583-adapter", os.environ.get("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if HSM_KEY_PATH and HSM_MASTER_KEY_HEX:
        kms = LocalKeyManagementService(master_key=bytes.fromhex(HSM_MASTER_KEY_HEX))
        app.state.hsm = MockHSM(kms=kms, persisted_key_path=HSM_KEY_PATH)
        log.info(f"HSM base key persisted at {HSM_KEY_PATH} -- survives restarts")
    else:
        app.state.hsm = MockHSM()
        log.warning(
            "HSM_KEY_PERSISTENCE_PATH/HSM_MASTER_KEY_HEX unset -- the base key is "
            "regenerated every process start. Anything encrypted before a restart "
            "cannot be decrypted after one. Acceptable for a demo, never for a "
            "deployment with more than one replica: two pods hold DIFFERENT keys, "
            "so a PIN block encrypted by one is undecryptable by the other."
        )

    app.state.soap = Iso8583SoapClient(
        SOAP_ENDPOINT, timeout=SOAP_TIMEOUT, username=SOAP_USERNAME, password=SOAP_PASSWORD
    )
    log.info(f"SOAP gateway endpoint: {SOAP_ENDPOINT}")

    yield

    app.state.soap.close()


app = FastAPI(title="ISO 8583 Adapter", lifespan=lifespan)


@app.middleware("http")
async def adopt_correlation_id(request: Request, call_next):
    incoming = request.headers.get(CORRELATION_HEADER)
    if incoming:
        set_correlation_id(incoming)
    return await call_next(request)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class AuthorizeRequest(BaseModel):
    card_number: str = Field(..., min_length=12, max_length=19)
    # Plaintext PIN crosses exactly ONE internal hop -- gateway to here --
    # and is encrypted into a PIN block before going any further. It is
    # never logged (mfcommon.observability.audit redacts it), never
    # persisted, and never leaves this service in plaintext. In a real PCI
    # environment that hop would be mutual-TLS at minimum; the NetworkPolicy
    # restricting who may call this service is the current mitigation.
    pin: str | None = Field(None, min_length=4, max_length=12)
    amount_cents: int = Field(..., gt=0)
    processing_code: str = "000000"
    entry_mode: str = "05"
    rrn: str = Field(..., min_length=6, max_length=12)
    currency_code: str | None = None
    credit_account: str | None = None


class AuthorizeResponse(BaseModel):
    # "approved" | "declined" | "unknown". Three values, not two, because
    # "unknown" is a genuinely distinct state and the caller must handle it
    # differently from both of the others.
    outcome: str
    response_code: str | None = None
    response_text: str | None = None
    authorization_id: str | None = None
    stan: str | None = None
    rrn: str
    reversal_sent: bool = False


def _amount_to_de4(amount_cents: int) -> str:
    """DE 4 is exactly 12 digits, zero-padded, in minor units."""
    return f"{amount_cents:012d}"


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.post("/internal/iso8583/authorize", response_model=AuthorizeResponse)
def authorize(body: AuthorizeRequest, request: Request):
    state = request.app.state

    pin_block_hex = None
    ksn = None
    if body.pin is not None:
        # The ONLY place a PIN becomes a PIN block. Returns hex, not raw
        # bytes -- see the PinBlockHex note in the WSDL for why raw bytes
        # cannot cross XML.
        ksn, encrypted = state.hsm.encrypt_pin_block(body.pin, body.card_number)
        pin_block_hex = encrypted.hex()

    log.info(
        f"authorize rrn={body.rrn} card={mask_pan(body.card_number)} "
        f"amount_cents={body.amount_cents} pin_present={body.pin is not None}"
    )

    try:
        result = state.soap.authorize(
            pan=body.card_number,
            processing_code=body.processing_code,
            amount_minor=_amount_to_de4(body.amount_cents),
            entry_mode=body.entry_mode,
            rrn=body.rrn,
            currency_code=body.currency_code or DEFAULT_CURRENCY,
            pin_block_hex=pin_block_hex,
            ksn=ksn,
            account_id_2=body.credit_account,
        )

    except SoapFault as fault:
        detail = fault.detail or ""

        if "SWITCH_TIMEOUT" in detail:
            # The dangerous branch. ACE already sent a reversal, but the
            # business outcome remains unknown. Reported as "unknown" so the
            # caller declines to post to the ledger -- rather than as
            # "declined", which would imply the money definitely did not
            # move.
            log.error(f"SWITCH TIMEOUT rrn={body.rrn} -- outcome unknown, reversal sent")
            return AuthorizeResponse(
                outcome="unknown",
                response_text=fault.string,
                rrn=body.rrn,
                reversal_sent=True,
            )

        if "SWITCH_DOWN" in detail:
            # Never reached the switch, so nothing was authorized and no
            # reversal is needed. A clean 503 the caller may safely retry.
            log.error(f"switch unreachable rrn={body.rrn}: {fault.string}")
            raise HTTPException(status_code=503, detail=f"Switch unavailable: {fault.string}")

        if fault.is_client_fault:
            # We built a bad message. Retrying sends the identical bad
            # message, so surface it as a 422 the caller will not retry.
            log.error(f"gateway rejected our request rrn={body.rrn}: {fault.string}")
            raise HTTPException(status_code=422, detail=f"Gateway rejected the request: {fault.string}")

        raise HTTPException(status_code=502, detail=f"Gateway fault: {fault.string}")

    except SoapTimeout as exc:
        # OUR http client timed out before ACE answered. Same ambiguity as
        # above, and importantly we do NOT know whether ACE sent a reversal,
        # so we send one ourselves. A duplicate reversal is harmless (the
        # switch acknowledges it); a missing one is not.
        log.error(f"SOAP timeout rrn={body.rrn}: {exc}")
        reversal_sent = _attempt_reversal(state, body)
        return AuthorizeResponse(
            outcome="unknown",
            response_text=str(exc),
            rrn=body.rrn,
            reversal_sent=reversal_sent,
        )

    except SoapTransportError as exc:
        log.error(f"cannot reach the SOAP gateway: {exc}")
        raise HTTPException(status_code=503, detail=f"ISO 8583 gateway unreachable: {exc}")

    response_code = result.get("responseCode", "")
    outcome = "approved" if response_code == "00" else "declined"

    log.info(f"authorize rrn={body.rrn} -> {outcome} ({response_code} {result.get('responseText')})")

    return AuthorizeResponse(
        outcome=outcome,
        response_code=response_code,
        response_text=result.get("responseText"),
        authorization_id=result.get("authId"),
        stan=result.get("stan"),
        rrn=result.get("rrn") or body.rrn,
    )


def _attempt_reversal(state, body: AuthorizeRequest) -> bool:
    """
    Best-effort reversal after an ambiguous timeout.

    Returns whether it was acknowledged. A failure here is logged at ERROR
    and reported honestly rather than swallowed: an unacknowledged reversal
    means a cardholder may stay debited for a transaction the platform has
    already decided did not happen, and that needs a human, not a silent
    retry loop.
    """
    try:
        state.soap.reverse(
            original_mti="0200",
            original_stan="000000",  # unknown; ACE matches on RRN as well
            rrn=body.rrn,
            amount_minor=_amount_to_de4(body.amount_cents),
            pan=body.card_number,
            timeout=10.0,
        )
        log.warning(f"reversal acknowledged for rrn={body.rrn}")
        return True
    except Exception as exc:  # noqa: BLE001 - genuinely anything, and it must not mask the original error
        log.error(
            f"REVERSAL FAILED for rrn={body.rrn}: {exc!r}. Cardholder may remain "
            f"debited for a transaction that was never recorded. Requires manual "
            f"reconciliation."
        )
        return False


class ReverseRequest(BaseModel):
    rrn: str
    original_stan: str = "000000"
    original_mti: str = "0200"
    amount_cents: int = Field(..., gt=0)
    card_number: str


@app.post("/internal/iso8583/reverse")
def reverse(body: ReverseRequest, request: Request):
    """
    Explicit reversal, for the saga's compensation path -- when the ledger
    posting fails after an approved authorization.

    Idempotent at the switch, so this is safe to retry until acknowledged.
    """
    try:
        result = request.app.state.soap.reverse(
            original_mti=body.original_mti,
            original_stan=body.original_stan,
            rrn=body.rrn,
            amount_minor=_amount_to_de4(body.amount_cents),
            pan=body.card_number,
        )
    except (SoapFault, SoapTimeout, SoapTransportError) as exc:
        log.error(f"reversal failed rrn={body.rrn}: {exc!r}")
        raise HTTPException(status_code=502, detail=f"Reversal failed: {exc}")

    return {
        "status": "acknowledged",
        "response_code": result.get("responseCode"),
        "rrn": body.rrn,
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "iso8583-adapter"}


@app.get("/ready")
def ready(request: Request):
    """
    An echo test through the whole chain: adapter -> ACE -> TCP -> switch.

    Deliberately not a bare "this process is up". A pod that answers
    readiness while the switch is unreachable gets traffic it can only fail,
    and OpenShift has no way to know.
    """
    try:
        result = request.app.state.soap.network_management(code="301", timeout=5.0)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"ISO 8583 chain not ready: {exc}")
    return {"status": "ready", "echo_response_code": result.get("responseCode")}
