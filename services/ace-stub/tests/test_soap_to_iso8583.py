"""
The load-bearing test of the whole platform.

This proves the SOAP boundary genuinely mediates to binary ISO 8583 and
back -- not against a mock, but against a real HostSimulator over a real
TCP socket, with real BCD-packed bytes and a real bitmap on the wire.

If this passes, the REST -> SOAP -> ISO 8583 -> SOAP -> REST path works.
Everything else in the platform is REST plumbing around it.

These tests remain valid when IBM ACE replaces ace-stub, because they
assert on the WSDL contract rather than on the implementation. That is the
point of having written the WSDL first.
"""

import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from mfcommon.iso8583.parser import parse_message
from mfcommon.soap.envelope import SoapFault, build_envelope, parse_response


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def gateway():
    """Boots a real HostSimulator and a real ace-stub app wired to it."""
    port = _free_port()

    import os

    os.environ["SWITCH_HOST"] = "127.0.0.1"
    os.environ["SWITCH_PORT"] = str(port)
    os.environ["SWITCH_TIMEOUT_SECONDS"] = "3"

    from mfcommon.iso8583.host_simulator import HostSimulator

    simulator = HostSimulator(host="127.0.0.1", port=port)
    simulator.start()
    time.sleep(0.3)

    # Imported after the environment is set -- module-level config reads it.
    from app.main import app

    with TestClient(app) as client:
        yield client, simulator

    simulator.stop()


def _post_soap(client, operation, fields, correlation_id="cid-test"):
    payload = build_envelope(operation, fields, correlation_id=correlation_id)
    return client.post(
        "/Iso8583Gateway",
        content=payload,
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"urn:microfinance:iso8583:v1/{operation}"',
        },
    )


def test_authorize_produces_a_real_iso8583_message_on_the_wire(gateway):
    client, simulator = gateway
    before = len(simulator.received)

    response = _post_soap(
        client,
        "authorizeRequest",
        {
            "pan": "4532015112830366",
            "processingCode": "000000",
            "amountMinor": "000000005000",
            "entryMode": "05",
            "rrn": "000123456789",
            "currencyCode": "840",
            "pinBlockHex": "8f2100ff9a0d1e7c",
            "ksn": "0000000001",
        },
    )

    assert response.status_code == 200
    body = parse_response(response.content)

    assert body["responseCode"] == "00"
    assert body["responseText"] == "Approved / completed successfully"
    assert body["authId"] == "A18008"
    assert body["rrn"] == "000123456789"
    assert body["mti"] == "0210"

    # The switch genuinely received a binary 0200 -- this is what separates
    # a stand-in from a mock.
    assert len(simulator.received) > before
    received = [m for m in simulator.received if m["mti"] == "0200"][-1]
    assert received["fields"][2] == "4532015112830366"
    assert received["fields"][4] == "000000005000"
    assert received["fields"][37] == "000123456789"


def test_pin_block_survives_the_hex_round_trip_byte_for_byte(gateway):
    """
    DE 52 is 8 bytes of ciphertext. Hex-encoding it across XML must be
    lossless: a single flipped bit means the switch derives a different PIN
    and declines a valid transaction with DE 39 = 55, which is close to
    impossible to diagnose from the outside.

    The value here deliberately contains 0x00 and 0x1e -- both ILLEGAL in
    XML 1.0 with no legal escape sequence. Sending DE 52 as raw text rather
    than hex would make this request unparseable.
    """
    client, simulator = gateway
    pin_block_hex = "008f1e00ff0b0c1f"

    _post_soap(
        client,
        "authorizeRequest",
        {
            "pan": "4111111111111111",
            "processingCode": "000000",
            "amountMinor": "000000001000",
            "entryMode": "05",
            "rrn": "000999888777",
            "currencyCode": "840",
            "pinBlockHex": pin_block_hex,
        },
    )

    received = [m for m in simulator.received if m["fields"].get(37) == "000999888777"][-1]
    on_the_wire = received["fields"][52].encode("latin-1")
    assert on_the_wire == bytes.fromhex(pin_block_hex)
    assert len(on_the_wire) == 8


def test_absent_pin_block_leaves_de52_bit_clear(gateway):
    """A balance inquiry has no PIN. The DE 52 bitmap bit must be CLEAR, not
    set with empty content -- a real switch answers the latter with DE 39 =
    30 (format error)."""
    client, simulator = gateway

    _post_soap(
        client,
        "authorizeRequest",
        {
            "pan": "4111111111111111",
            "processingCode": "300000",
            "amountMinor": "000000000000",
            "entryMode": "05",
            "rrn": "000111222333",
            "currencyCode": "840",
        },
    )

    received = [m for m in simulator.received if m["fields"].get(37) == "000111222333"][-1]
    assert 52 not in received["fields"]


def test_stan_is_assigned_by_the_gateway_and_echoed_back(gateway):
    """STAN uniqueness is per-connection, so only the connection owner can
    assign it. Two concurrent requests must never collide."""
    client, _simulator = gateway

    stans = []
    for i in range(4):
        response = _post_soap(
            client,
            "authorizeRequest",
            {
                "pan": "4111111111111111",
                "processingCode": "000000",
                "amountMinor": "000000000100",
                "entryMode": "05",
                "rrn": f"00055500000{i}",
                "currencyCode": "840",
            },
        )
        stans.append(parse_response(response.content)["stan"])

    assert len(set(stans)) == 4, f"STAN collision: {stans}"


def test_concurrent_requests_each_get_their_own_response(gateway):
    """
    The correlation manager's actual job. The host simulator answers each
    message on its own thread, so responses genuinely arrive out of order.
    Every caller must still receive the response to ITS request -- crossing
    them would approve one cardholder's transaction against another's.
    """
    client, _simulator = gateway
    results = {}
    errors = []

    def fire(index: int):
        rrn = f"00077700000{index}"
        try:
            response = _post_soap(
                client,
                "authorizeRequest",
                {
                    "pan": "4111111111111111",
                    "processingCode": "000000",
                    "amountMinor": f"{index:012d}",
                    "entryMode": "05",
                    "rrn": rrn,
                    "currencyCode": "840",
                },
            )
            results[rrn] = parse_response(response.content)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"concurrent requests raised: {errors}"
    assert len(results) == 6
    for rrn, body in results.items():
        assert body["rrn"] == rrn, f"response crossed: asked for {rrn}, got {body['rrn']}"


def test_switch_timeout_becomes_a_server_fault_not_a_success(gateway):
    """
    The most dangerous failure in the system. The switch goes silent; the
    outcome is genuinely unknown. This must NOT return a decline (the money
    may have moved) and must NOT return an approval (it may not have). It
    must fault, with SWITCH_TIMEOUT, so the caller reverses.
    """
    client, _simulator = gateway

    # additionalData maps to DE 48, which the simulator reads as a hook to
    # stay completely silent -- a real switch that has gone away.
    response = _post_soap(
        client,
        "authorizeRequest",
        {
            "pan": "4111111111111111",
            "processingCode": "000000",
            "amountMinor": "000000002500",
            "entryMode": "05",
            "rrn": "000444555666",
            "currencyCode": "840",
            "additionalData": "SIMULATE_TIMEOUT",
        },
    )

    with pytest.raises(SoapFault) as exc_info:
        parse_response(response.content)

    fault = exc_info.value
    assert fault.is_client_fault is False, "a timeout is not the caller's fault -- must not be Client"
    assert "SWITCH_TIMEOUT" in (fault.detail or "")
    assert "UNKNOWN" in fault.string


def test_malformed_request_is_a_client_fault_so_it_is_never_retried(gateway):
    client, _simulator = gateway

    response = _post_soap(
        client,
        "authorizeRequest",
        {"pan": "4111111111111111"},  # missing processingCode, amountMinor, rrn
    )

    with pytest.raises(SoapFault) as exc_info:
        parse_response(response.content)

    assert exc_info.value.is_client_fault is True
    assert "processingCode" in exc_info.value.string


def test_bad_pin_block_length_is_rejected_before_reaching_the_switch(gateway):
    client, _simulator = gateway

    response = _post_soap(
        client,
        "authorizeRequest",
        {
            "pan": "4111111111111111",
            "processingCode": "000000",
            "amountMinor": "000000001000",
            "entryMode": "05",
            "rrn": "000222333444",
            "currencyCode": "840",
            "pinBlockHex": "8f21",  # 2 bytes, not 8
        },
    )

    with pytest.raises(SoapFault) as exc_info:
        parse_response(response.content)
    assert "8 bytes" in exc_info.value.string


def test_unknown_operation_is_rejected_clearly(gateway):
    client, _simulator = gateway
    response = _post_soap(client, "definitelyNotAnOperation", {"foo": "bar"})

    with pytest.raises(SoapFault) as exc_info:
        parse_response(response.content)
    assert "Unknown operation" in exc_info.value.string
    assert "authorizeRequest" in exc_info.value.string  # tells you what IS supported


def test_network_management_echo_reaches_the_switch(gateway):
    """What the readiness probe calls -- proves the whole chain, not just
    that this process is running."""
    client, _simulator = gateway
    response = _post_soap(client, "networkManagementRequest", {"networkCode": "301"})

    body = parse_response(response.content)
    assert body["responseCode"] == "00"
    assert body["networkCode"] == "301"


def test_reversal_reaches_the_switch_as_an_0400(gateway):
    client, simulator = gateway

    response = _post_soap(
        client,
        "reverseRequest",
        {
            "originalMti": "0200",
            "originalStan": "000042",
            "rrn": "000123456789",
            "amountMinor": "000000005000",
            "pan": "4532015112830366",
        },
    )

    body = parse_response(response.content)
    assert body["responseCode"] == "00"
    assert any(m["mti"] == "0400" for m in simulator.received)


def test_wsdl_is_served_and_matches_the_committed_contract(gateway):
    """Tooling pointed at the stub must get the same document ACE will
    serve, or the swap is not actually transparent."""
    client, _simulator = gateway
    response = client.get("/Iso8583Gateway?wsdl")

    assert response.status_code == 200
    assert b"Iso8583GatewayService" in response.content
    assert b"urn:microfinance:iso8583:v1" in response.content
    for operation in (b"authorize", b"reverse", b"networkManagement"):
        assert operation in response.content


def test_readiness_reflects_the_switch_connection(gateway):
    client, _simulator = gateway
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["switch_connected"] is True
