"""
The SOAP client that talks to IBM ACE.

This is the single place in the whole platform where a REST request becomes
a SOAP request. Everything above it speaks JSON; everything below it speaks
ISO 8583. Keeping that translation in exactly one file is the reason the
ACE swap is a one-variable change.

TIMEOUT SEMANTICS -- the part that actually matters:

A timeout here is NOT "the transaction failed." It is "we do not know
whether the transaction happened." ACE may have already forwarded the 0200
to the switch, the switch may have already approved it and debited the
cardholder, and only the response may be lost. Treating that as a failure
and returning "declined" to the client, while the money genuinely moved, is
the single worst outcome this system can produce.

So SoapTimeout is raised as its own exception type, distinct from any other
error, and the caller (iso8583-adapter) responds to it by triggering a
reversal -- exactly what correlation/tracker.py does in the monolith when
send_and_wait() times out. The behaviour is preserved across the
decomposition; only the layer that detects it moved.
"""

from __future__ import annotations

import logging

from mfcommon.observability.correlation import get_correlation_id
from mfcommon.soap.envelope import (
    ISO8583_NS,
    SoapFault,
    SoapProtocolError,
    build_envelope,
    parse_response,
)

log = logging.getLogger(__name__)


class SoapTimeout(Exception):
    """
    No response from ACE before the deadline. Outcome genuinely unknown --
    the caller MUST reverse rather than assume either success or failure.
    """


class SoapTransportError(Exception):
    """Could not reach ACE at all (connection refused, DNS, TLS)."""


class Iso8583SoapClient:
    """
    One instance per process, held for the process lifetime.

    endpoint points at either:
      - the ace-stub service (today, no IBM licence required), or
      - a real ACE integration server (once entitled)

    Nothing else in the codebase changes between those two. The stub serves
    the identical WSDL and returns identical envelopes.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 15.0,
        username: str | None = None,
        password: str | None = None,
        namespace: str = ISO8583_NS,
    ):
        import httpx

        self.endpoint = endpoint
        self.timeout = timeout
        self.username = username
        self.password = password
        self.namespace = namespace
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _call(self, operation: str, fields: dict, *, timeout: float | None = None) -> dict:
        import httpx

        payload = build_envelope(
            operation,
            fields,
            namespace=self.namespace,
            username=self.username,
            password=self.password,
            correlation_id=get_correlation_id(),
        )

        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            # SOAPAction is mandatory in SOAP 1.1. ACE routes on it, and
            # omitting it produces a 500 with a message that does not
            # mention SOAPAction at all.
            "SOAPAction": f'"{self.namespace}/{operation}"',
        }

        try:
            response = self._client.post(
                self.endpoint, content=payload, headers=headers, timeout=timeout or self.timeout
            )
        except httpx.TimeoutException as exc:
            raise SoapTimeout(
                f"No response from {self.endpoint} for {operation} within "
                f"{timeout or self.timeout}s -- transaction outcome is UNKNOWN"
            ) from exc
        except httpx.RequestError as exc:
            # Connection refused / DNS failure means ACE was never reached,
            # so nothing was sent to the switch. Distinct from a timeout,
            # and safe to fail cleanly without a reversal.
            raise SoapTransportError(f"Cannot reach {self.endpoint}: {exc!r}") from exc

        # Note: no status_code check before parsing. A SOAP fault legitimately
        # arrives as HTTP 500, and parse_response raises SoapFault for it.
        try:
            return parse_response(response.content, namespace=self.namespace)
        except SoapProtocolError:
            if response.status_code >= 500:
                raise SoapTransportError(
                    f"{self.endpoint} returned HTTP {response.status_code} with a "
                    f"non-SOAP body ({len(response.content)} bytes)"
                )
            raise

    # -- operations, matching the three in the WSDL ------------------------

    def authorize(
        self,
        *,
        pan: str,
        processing_code: str,
        amount_minor: str,
        entry_mode: str,
        rrn: str,
        currency_code: str,
        pin_block_hex: str | None = None,
        ksn: str | None = None,
        stan: str | None = None,
        account_id_2: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        """
        MTI 0200 -> 0210. Returns {responseCode, responseText, authId, stan, rrn}.

        pin_block_hex is HEX, not raw bytes. The monolith passed DE 52 as a
        latin-1-decoded string, which works over a socket but is actively
        dangerous inside XML: several byte values in an 8-byte binary PIN
        block are control characters that are ILLEGAL in XML 1.0 and cannot
        be escaped at all. Hex-encoding at this boundary makes the transport
        safe, and ACE's ESQL decodes it back with CAST(... AS BLOB) before
        the DFDL serializer writes DE 52.
        """
        return self._call(
            "authorizeRequest",
            {
                "pan": pan,
                "processingCode": processing_code,
                "amountMinor": amount_minor,
                "entryMode": entry_mode,
                "rrn": rrn,
                "currencyCode": currency_code,
                "pinBlockHex": pin_block_hex,
                "ksn": ksn,
                "stan": stan,
                "accountId2": account_id_2,
            },
            timeout=timeout,
        )

    def reverse(
        self, *, original_mti: str, original_stan: str, rrn: str, amount_minor: str,
        pan: str, timeout: float | None = None,
    ) -> dict:
        """
        MTI 0400 -> 0410. Sent when authorize() timed out, i.e. when the
        outcome is unknown. ACE builds DE 90 (original data elements) from
        original_mti + original_stan.

        A reversal must itself be retried until acknowledged -- an unacked
        reversal leaves the cardholder debited for a transaction we already
        decided did not happen.
        """
        return self._call(
            "reverseRequest",
            {
                "originalMti": original_mti,
                "originalStan": original_stan,
                "rrn": rrn,
                "amountMinor": amount_minor,
                "pan": pan,
            },
            timeout=timeout,
        )

    def network_management(self, *, code: str = "301", timeout: float | None = None) -> dict:
        """
        MTI 0800 -> 0810. code 001 = sign-on, 002 = sign-off, 301 = echo.

        The echo is what the readiness probe uses: it proves the whole chain
        (adapter -> ACE -> TCP -> switch) is alive, not merely that the
        adapter's own process is running. A health check that only confirms
        "this pod responds" will happily report ready while every
        transaction fails.
        """
        return self._call("networkManagementRequest", {"networkCode": code}, timeout=timeout)
