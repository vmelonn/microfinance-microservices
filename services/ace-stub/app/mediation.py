"""
The protocol mediation IBM ACE will eventually perform, written in Python.

This is the substantive part of ace-stub, and the reason it is a stand-in
rather than a mock. A mock returns canned responses. This module genuinely:

  - maps a SOAP request onto an ISO 8583 data-element dictionary
  - builds real BCD-packed, bitmapped binary via mfcommon.iso8583.parser
  - sends it MLI-framed over a real TCP socket to the switch
  - correlates the response by STAN
  - parses the real binary answer back
  - maps it onto a SOAP response

Every one of those steps is a step ACE will perform, using a DFDL schema
and ESQL instead of Python. The bytes on the wire are identical. That means
the integration tests written against this stub remain valid the day ACE
takes over -- they are testing the contract, not the implementation.

The mapping below is the specification that ace/Iso8583Gateway/esql/
SoapToIso.esql implements. Those two must agree. If you add a field here,
add it there.
"""

from __future__ import annotations

import logging

from mfcommon.iso8583.parser import DE39_RESPONSE_CODES

log = logging.getLogger(__name__)

# DE 49, currency. Kept as a named constant because "840" appearing bare in
# a field dict is the kind of thing that gets copied into the wrong slot.
DEFAULT_CURRENCY = "840"


class MediationError(Exception):
    """A request that cannot be mapped. Becomes a SOAP Client fault."""

    def __init__(self, message: str, category: str = "VALIDATION"):
        self.category = category
        super().__init__(message)


def _require(fields: dict, name: str) -> str:
    value = fields.get(name)
    if value is None or value == "":
        raise MediationError(f"Required element <{name}> is missing or empty")
    return value


def soap_to_iso_authorize(fields: dict) -> tuple[str, dict]:
    """
    authorizeRequest -> (MTI, {DE: value}).

    DE 11 (STAN) is deliberately NOT set here. It is assigned by the
    correlation manager at send time, because STAN must be unique per
    connection and only the component owning the connection can guarantee
    that. A caller-supplied STAN is accepted in the WSDL but treated as a
    hint that this layer overrides -- the alternative is two in-flight
    requests colliding on the same STAN and each receiving the other's
    response, which is the worst possible failure in a payment system.
    """
    pan = _require(fields, "pan")
    de = {
        2: pan,
        3: _require(fields, "processingCode"),
        4: _require(fields, "amountMinor"),
        22: fields.get("entryMode") or "05",
        37: _require(fields, "rrn"),
        49: fields.get("currencyCode") or DEFAULT_CURRENCY,
    }

    pin_block_hex = fields.get("pinBlockHex")
    if pin_block_hex:
        try:
            pin_block = bytes.fromhex(pin_block_hex)
        except ValueError as exc:
            raise MediationError(f"pinBlockHex is not valid hex: {exc}")
        if len(pin_block) != 8:
            raise MediationError(
                f"DE 52 must be exactly 8 bytes; got {len(pin_block)} from "
                f"{len(pin_block_hex)} hex characters"
            )
        # latin-1 is a byte-for-byte mapping of 0x00-0xFF onto U+0000-U+00FF,
        # so this is a lossless bytes->str carrier, not a text decode. The
        # parser re-encodes with the same codec. Any other codec here
        # corrupts the PIN block.
        de[52] = pin_block.decode("latin-1")

    ksn = fields.get("ksn")
    if ksn:
        de[53] = ksn.rjust(16, "0")

    account_id_2 = fields.get("accountId2")
    if account_id_2:
        de[103] = account_id_2

    # DE 48, additional data (private use). Pass-through: this layer assigns
    # it no meaning, because the standard assigns it none either -- every
    # processor defines its own. The host simulator happens to read two
    # sentinel values out of it to drive its timeout and delay behaviour,
    # which is how the failure paths get tested without a real misbehaving
    # switch.
    additional_data = fields.get("additionalData")
    if additional_data:
        de[48] = additional_data

    return "0200", de


def soap_to_iso_reverse(fields: dict) -> tuple[str, dict]:
    """
    reverseRequest -> (0400, {DE}).

    DE 90 is built by the correlation layer from originalMti + originalStan,
    so it is not assembled here -- see mfcommon.iso8583.reversal.
    """
    return "0400", {
        2: _require(fields, "pan"),
        4: _require(fields, "amountMinor"),
        37: _require(fields, "rrn"),
    }


def soap_to_iso_network(fields: dict) -> tuple[str, dict]:
    code = fields.get("networkCode") or "301"
    if code not in ("001", "002", "301", "161", "201", "302"):
        raise MediationError(f"Unrecognised DE 70 network management code: {code!r}")
    return "0800", {70: code}


def iso_to_soap_authorize(parsed: dict, fallback_rrn: str) -> dict:
    """
    0210 -> authorizeResponse.

    responseText is resolved from the standard's DE 39 table here rather
    than in each caller, so no service downstream needs to carry a copy of
    the code table. An unknown code produces a descriptive string instead
    of None -- a missing reason in a decline log is a support ticket.
    """
    de = parsed["fields"]
    code = de.get(39, "")
    return {
        "responseCode": code,
        "responseText": DE39_RESPONSE_CODES.get(code, f"Unmapped response code {code!r}"),
        "authId": de.get(38),
        "stan": de.get(11, "000000"),
        # The switch echoes DE 37 back; if it did not, keep our own so the
        # response is never missing the key the ledger will post against.
        "rrn": de.get(37, fallback_rrn),
        "mti": parsed["mti"],
    }


def iso_to_soap_reverse(parsed: dict) -> dict:
    de = parsed["fields"]
    code = de.get(39, "")
    return {
        "responseCode": code,
        "responseText": DE39_RESPONSE_CODES.get(code, f"Unmapped response code {code!r}"),
        "stan": de.get(11, "000000"),
    }


def iso_to_soap_network(parsed: dict) -> dict:
    de = parsed["fields"]
    return {
        # An 0810 carries no DE 39 in this switch's dialect; arrival of the
        # echo response IS the acknowledgement, so "00" is synthesised.
        "responseCode": de.get(39, "00"),
        "networkCode": de.get(70, ""),
        "stan": de.get(11, "000000"),
    }
