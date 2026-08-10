"""
SOAP 1.1 envelope construction and parsing -- the REST/SOAP boundary in one
file.

WHY HAND-ROLLED, NOT zeep:

zeep is the obvious choice and was deliberately rejected. It fetches and
compiles the WSDL at client construction time, which means (a) service
startup acquires a hard network dependency on ACE being reachable, so an
ACE restart cascades into iso8583-adapter failing its readiness probe, and
(b) the request shape becomes whatever zeep infers, which is exactly the
thing you want to be explicit and version-controlled at a protocol
boundary. The contract here is three operations and a dozen fields. It does
not need a WSDL compiler; it needs to be obvious and to fail loudly.

This also matches the house style already set by the monolith: the JWT
implementation, the BCD codec, and the PIN block are all hand-rolled for
the same reason -- at a protocol boundary, knowing exactly what bytes go
out matters more than the convenience of a library.

THE FAULT TRAP:

A SOAP fault is not an HTTP error. It arrives as HTTP 500 with a
well-formed <soap:Fault> in the body, and -- worse -- some stacks return
faults as HTTP 200. Code that branches on response.status_code alone will
either treat a real fault as a transport failure (and retry a transaction
that was explicitly rejected) or treat it as success (and parse garbage).
Every response goes through parse_response(), which checks for a Fault
element FIRST, regardless of status code.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE_NS = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
WSU_NS = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
PASSWORD_TEXT_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordText"
)

# The service namespace. Must match ace/Iso8583Library/wsdl/Iso8583Gateway.wsdl
# exactly -- ACE validates against the WSDL and rejects a mismatched
# namespace with an unhelpful parse error rather than a clear one.
ISO8583_NS = "urn:microfinance:iso8583:v1"

ET.register_namespace("soapenv", SOAP_ENV_NS)
ET.register_namespace("iso", ISO8583_NS)
ET.register_namespace("wsse", WSSE_NS)


class SoapFault(Exception):
    """
    A fault the peer deliberately returned. Carries the fault code so
    callers can distinguish the two cases that need opposite handling:

      Client.*  -- our request was malformed or invalid. Retrying sends the
                   identical bad request again. Never retry.
      Server.*  -- the peer failed internally. May be transient.
    """

    def __init__(self, code: str, string: str, detail: str | None = None):
        self.code = code
        self.string = string
        self.detail = detail
        super().__init__(f"SOAP Fault [{code}]: {string}")

    @property
    def is_client_fault(self) -> bool:
        return self.code.split(":")[-1].lower().startswith("client")


class SoapProtocolError(Exception):
    """The response was not parseable as a SOAP envelope at all."""


def _qn(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def build_envelope(
    body_tag: str,
    fields: dict,
    *,
    namespace: str = ISO8583_NS,
    username: str | None = None,
    password: str | None = None,
    correlation_id: str | None = None,
) -> bytes:
    """
    Builds a complete SOAP 1.1 envelope.

    fields values of None are OMITTED rather than sent as empty elements.
    This is deliberate and matters: in ISO 8583 an absent data element (bit
    clear in the bitmap) and a present-but-empty one are different messages.
    Sending <pinBlock/> for a transaction that genuinely has no PIN would
    set DE 52's bitmap bit with zero-length content, which a real switch
    rejects as a format error (DE 39 = 30).
    """
    envelope = ET.Element(_qn(SOAP_ENV_NS, "Envelope"))
    header = ET.SubElement(envelope, _qn(SOAP_ENV_NS, "Header"))

    if username is not None:
        security = ET.SubElement(header, _qn(WSSE_NS, "Security"))
        security.set(_qn(SOAP_ENV_NS, "mustUnderstand"), "1")
        token = ET.SubElement(security, _qn(WSSE_NS, "UsernameToken"))
        ET.SubElement(token, _qn(WSSE_NS, "Username")).text = username
        pw = ET.SubElement(token, _qn(WSSE_NS, "Password"))
        pw.set("Type", PASSWORD_TEXT_TYPE)
        pw.text = password or ""

    if correlation_id is not None:
        # Propagating the correlation ID through the SOAP hop is what keeps
        # the trace unbroken across ACE. Without it, the trace stops at
        # iso8583-adapter and resumes on the way back with no link between
        # the two halves.
        ET.SubElement(header, _qn(namespace, "CorrelationId")).text = correlation_id

    body = ET.SubElement(envelope, _qn(SOAP_ENV_NS, "Body"))
    operation = ET.SubElement(body, _qn(namespace, body_tag))
    for name, value in fields.items():
        if value is None:
            continue
        ET.SubElement(operation, _qn(namespace, name)).text = str(value)

    return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(envelope, encoding="utf-8")


def _preview(raw: bytes, limit: int = 200) -> str:
    text = raw[:limit].decode("utf-8", errors="replace").strip()
    suffix = "..." if len(raw) > limit else ""
    return f"{text}{suffix}"


def parse_response(raw: bytes, *, namespace: str = ISO8583_NS) -> dict:
    """
    Returns the body's child elements as a flat dict. Raises SoapFault if
    the peer returned a fault -- checked BEFORE anything else, and
    independently of the HTTP status code, for the reason in the module
    docstring.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SoapProtocolError(
            f"Response is not well-formed XML: {exc}. Received: {_preview(raw)!r}"
        )

    # Checking the root element explicitly, rather than only looking for a
    # Body, because well-formed NON-SOAP XML is a routine failure mode: an
    # ingress returning an HTML error page parses as XML perfectly happily.
    # Reporting "SOAP envelope has no Body element" for a 502 page is
    # technically true and diagnostically useless -- the person reading it
    # needs to know an HTML page arrived, not that it lacked a Body.
    if root.tag != _qn(SOAP_ENV_NS, "Envelope"):
        raise SoapProtocolError(
            f"Response root element is <{root.tag}>, not a SOAP Envelope. "
            f"This usually means a proxy or ingress answered instead of the SOAP "
            f"service. Received: {_preview(raw)!r}"
        )

    body = root.find(_qn(SOAP_ENV_NS, "Body"))
    if body is None:
        raise SoapProtocolError(
            f"SOAP envelope has no Body element. Received: {_preview(raw)!r}"
        )

    fault = body.find(_qn(SOAP_ENV_NS, "Fault"))
    if fault is not None:
        # SOAP 1.1 fault children are unqualified -- no namespace. A
        # surprising amount of SOAP client code looks for them in the
        # envelope namespace and silently finds nothing.
        code = (fault.findtext("faultcode") or "unknown").strip()
        string = (fault.findtext("faultstring") or "no faultstring provided").strip()
        detail_el = fault.find("detail")
        detail = ET.tostring(detail_el, encoding="unicode").strip() if detail_el is not None else None
        raise SoapFault(code, string, detail)

    if len(body) == 0:
        raise SoapProtocolError("SOAP Body is empty -- expected a response element")

    response_element = body[0]
    result = {}
    for child in response_element:
        # Strip the namespace so callers work with plain field names.
        tag = child.tag.split("}", 1)[-1]
        result[tag] = child.text if child.text is not None else ""
    return result


def build_fault(code: str, string: str, detail: str | None = None) -> bytes:
    """Server side: build a SOAP fault. Used by ace-stub, and by any service
    that has to answer a SOAP caller with an error."""
    envelope = ET.Element(_qn(SOAP_ENV_NS, "Envelope"))
    body = ET.SubElement(envelope, _qn(SOAP_ENV_NS, "Body"))
    fault = ET.SubElement(body, _qn(SOAP_ENV_NS, "Fault"))
    ET.SubElement(fault, "faultcode").text = code
    ET.SubElement(fault, "faultstring").text = string
    if detail:
        ET.SubElement(fault, "detail").text = detail
    return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(envelope, encoding="utf-8")


def build_response(body_tag: str, fields: dict, *, namespace: str = ISO8583_NS) -> bytes:
    """Server side: build a normal (non-fault) SOAP response envelope."""
    envelope = ET.Element(_qn(SOAP_ENV_NS, "Envelope"))
    body = ET.SubElement(envelope, _qn(SOAP_ENV_NS, "Body"))
    response = ET.SubElement(body, _qn(namespace, body_tag))
    for name, value in fields.items():
        if value is None:
            continue
        ET.SubElement(response, _qn(namespace, name)).text = str(value)
    return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(envelope, encoding="utf-8")


def parse_request(raw: bytes) -> tuple[str, dict, dict]:
    """
    Server side: returns (operation_name, fields, headers).

    headers is a flat dict of any recognised header values -- currently just
    CorrelationId and the WS-Security username, which is all the stub and
    ACE need to agree on.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SoapProtocolError(f"Request is not well-formed XML: {exc}")

    headers: dict = {}
    header_el = root.find(_qn(SOAP_ENV_NS, "Header"))
    if header_el is not None:
        for child in header_el.iter():
            tag = child.tag.split("}", 1)[-1]
            if tag == "CorrelationId" and child.text:
                headers["correlation_id"] = child.text
            elif tag == "Username" and child.text:
                headers["username"] = child.text

    body = root.find(_qn(SOAP_ENV_NS, "Body"))
    if body is None or len(body) == 0:
        raise SoapProtocolError("SOAP request has no Body content")

    operation_el = body[0]
    operation = operation_el.tag.split("}", 1)[-1]
    fields = {}
    for child in operation_el:
        tag = child.tag.split("}", 1)[-1]
        fields[tag] = child.text if child.text is not None else ""

    return operation, fields, headers
