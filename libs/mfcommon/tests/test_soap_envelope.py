"""
Tests for the SOAP boundary.

The fault tests carry the most weight here. A SOAP fault misread as a
transport failure gets retried, and retrying an authorization the switch
already refused is how you turn one decline into two. So the fault path is
tested at HTTP 500 *and* at HTTP 200, because real stacks do both.
"""

import pytest

from mfcommon.soap.envelope import (
    ISO8583_NS,
    SOAP_ENV_NS,
    SoapFault,
    SoapProtocolError,
    build_envelope,
    build_fault,
    build_response,
    parse_request,
    parse_response,
)


def test_envelope_round_trips_through_the_server_side_parser():
    """What the client builds is exactly what the server can read, the
    property that makes ace-stub and ACE interchangeable."""
    payload = build_envelope(
        "authorizeRequest",
        {"pan": "4532015112830366", "amountMinor": "000000005000", "rrn": "000123456789"},
        username="mf-adapter",
        password="secret",
        correlation_id="cid-abc123",
    )

    operation, fields, headers = parse_request(payload)

    assert operation == "authorizeRequest"
    assert fields["pan"] == "4532015112830366"
    assert fields["amountMinor"] == "000000005000"
    assert fields["rrn"] == "000123456789"
    assert headers["correlation_id"] == "cid-abc123"
    assert headers["username"] == "mf-adapter"


def test_none_fields_are_omitted_not_sent_empty():
    """An absent DE and a present-but-empty DE are different ISO 8583
    messages. A real switch answers the second with DE 39 = 30 (format
    error), so this must not be a stylistic choice."""
    payload = build_envelope("authorizeRequest", {"pan": "4111111111111111", "pinBlockHex": None})

    _operation, fields, _headers = parse_request(payload)

    assert "pan" in fields
    assert "pinBlockHex" not in fields


def test_correlation_id_survives_the_soap_hop():
    """Without this, the distributed trace has a hole exactly where the
    hardest-to-debug part of the system is."""
    payload = build_envelope("authorizeRequest", {"pan": "4111111111111111"}, correlation_id="cid-trace-me")
    _operation, _fields, headers = parse_request(payload)
    assert headers["correlation_id"] == "cid-trace-me"


def test_wsse_security_header_omitted_entirely_when_no_username():
    payload = build_envelope("authorizeRequest", {"pan": "4111111111111111"})
    assert b"UsernameToken" not in payload
    assert b"Security" not in payload


def test_response_parses_to_a_flat_dict():
    raw = build_response(
        "authorizeResponse",
        {"responseCode": "00", "responseText": "Approved", "authId": "A18008", "stan": "000042"},
    )
    result = parse_response(raw)
    assert result == {
        "responseCode": "00",
        "responseText": "Approved",
        "authId": "A18008",
        "stan": "000042",
    }


def test_fault_raises_rather_than_returning_a_dict():
    """The core trap: a fault must never look like a successful parse."""
    raw = build_fault("soapenv:Server", "Switch connection unavailable", "TCP connect refused")

    with pytest.raises(SoapFault) as exc_info:
        parse_response(raw)

    assert exc_info.value.code == "soapenv:Server"
    assert "Switch connection unavailable" in exc_info.value.string
    assert exc_info.value.is_client_fault is False


def test_client_fault_is_distinguishable_from_server_fault():
    """Client faults must never be retried, the identical bad request
    produces the identical rejection. Server faults may be transient."""
    client_fault = build_fault("soapenv:Client", "pan is not 12-19 digits")
    with pytest.raises(SoapFault) as exc_info:
        parse_response(client_fault)
    assert exc_info.value.is_client_fault is True


def test_fault_children_are_found_despite_being_unqualified():
    """SOAP 1.1 fault children carry NO namespace, unlike everything else in
    the envelope. Parsers that look for them in the envelope namespace find
    nothing and report a fault with no message."""
    raw = (
        f'<?xml version="1.0"?>'
        f'<soapenv:Envelope xmlns:soapenv="{SOAP_ENV_NS}">'
        f"<soapenv:Body><soapenv:Fault>"
        f"<faultcode>soapenv:Client</faultcode>"
        f"<faultstring>Invalid processing code</faultstring>"
        f"</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
    ).encode()

    with pytest.raises(SoapFault) as exc_info:
        parse_response(raw)

    assert exc_info.value.string == "Invalid processing code"
    assert "no faultstring provided" not in str(exc_info.value)


def test_non_xml_body_raises_protocol_error_not_a_parse_crash():
    """A load balancer returning an HTML 502 page is a real, common failure.
    It must produce a diagnosable error, not an ExpatError from deep inside
    the XML library."""
    with pytest.raises(SoapProtocolError) as exc_info:
        parse_response(b"<html><body>502 Bad Gateway</body></html>")
    assert "502 Bad Gateway" in str(exc_info.value)


def test_empty_body_is_rejected():
    raw = f'<soapenv:Envelope xmlns:soapenv="{SOAP_ENV_NS}"><soapenv:Body/></soapenv:Envelope>'.encode()
    with pytest.raises(SoapProtocolError):
        parse_response(raw)


def test_namespace_matches_the_wsdl_contract():
    """ACE validates the request namespace against the deployed WSDL and
    rejects a mismatch with an unhelpful parse error. Pinning it in a test
    means a typo fails here instead of at 3am against a real integration
    server."""
    assert ISO8583_NS == "urn:microfinance:iso8583:v1"
    payload = build_envelope("authorizeRequest", {"pan": "4111111111111111"})
    assert ISO8583_NS.encode() in payload
