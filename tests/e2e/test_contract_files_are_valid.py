"""
The WSDL and DFDL schema must be well-formed XML.

WHY THIS EXISTS. Both files shipped invalid for several commits and nothing
noticed, because the only test touching the WSDL fetched it over HTTP and
grepped for substrings:

    assert b"Iso8583GatewayService" in response.content

A grep passes happily on a document no parser will accept. ace-stub was
serving a WSDL that any real SOAP client would have rejected on fetch, and
ACE would have refused the XSD on import, discoverable only at the point
where the entitlement finally arrived and the schema was loaded for the first
time. That is the worst possible moment to find out.

The specific fault was mundane: XML forbids '--' inside a comment body, and
the comments in both files used ', ' as an em-dash throughout. One
character of prose style, and the contract stopped being a contract.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WSDL = REPO_ROOT / "ace" / "Iso8583Library" / "wsdl" / "Iso8583Gateway.wsdl"
XSD = REPO_ROOT / "ace" / "Iso8583Library" / "dfdl" / "ISO8583.xsd"

WSDL_NS = "http://schemas.xmlsoap.org/wsdl/"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
SERVICE_NS = "urn:microfinance:iso8583:v1"


@pytest.mark.parametrize("path", [WSDL, XSD], ids=["wsdl", "dfdl-xsd"])
def test_file_is_well_formed_xml(path):
    """The assertion that would have caught the bug on the commit that
    introduced it."""
    assert path.exists(), f"{path} is missing"
    try:
        ET.parse(path)
    except ET.ParseError as exc:
        pytest.fail(f"{path.name} is not well-formed XML: {exc}")


@pytest.mark.parametrize("path", [WSDL, XSD], ids=["wsdl", "dfdl-xsd"])
def test_no_double_hyphen_inside_comments(path):
    """
    Checks the specific rule directly, so the failure names the cause.

    Well-formedness alone would catch this, but reports it as "invalid token"
    at a line and column, which does not obviously mean "your comment has a
    double hyphen in it".
    """
    text = path.read_text(encoding="utf-8")
    offenders = []
    for match in re.finditer(r"<!--(.*?)-->", text, re.DOTALL):
        if "--" in match.group(1):
            line = text[: match.start()].count("\n") + 1
            offenders.append(line)

    assert not offenders, (
        f"{path.name} has '--' inside XML comments at line(s) {offenders}. "
        f"XML forbids it. Use a single hyphen."
    )


@pytest.mark.parametrize("path", [WSDL, XSD], ids=["wsdl", "dfdl-xsd"])
def test_no_utf8_bom(path):
    """
    A BOM breaks more tooling than it has any right to, GitHub Actions
    silently stops triggering on a workflow file that has one, and some XML
    parsers reject a document whose declaration is not the first thing in it.

    PowerShell's `Set-Content -Encoding utf8` writes one, which is how these
    acquired theirs.
    """
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf"), (
        f"{path.name} starts with a UTF-8 BOM"
    )


def test_wsdl_declares_the_three_operations():
    """Structural, not textual: finds real portType operations rather than
    matching the word 'authorize' anywhere in the file."""
    root = ET.parse(WSDL).getroot()
    port_type = root.find(f"{{{WSDL_NS}}}portType")
    assert port_type is not None, "no portType in the WSDL"

    operations = {
        op.get("name") for op in port_type.findall(f"{{{WSDL_NS}}}operation")
    }
    assert operations == {"authorize", "reverse", "networkManagement"}, operations


def test_wsdl_target_namespace_matches_the_client():
    """ACE validates the request namespace against the deployed WSDL and
    rejects a mismatch with an unhelpful parse error, so this is pinned in
    both places."""
    from mfcommon.soap.envelope import ISO8583_NS

    root = ET.parse(WSDL).getroot()
    assert root.get("targetNamespace") == SERVICE_NS
    assert ISO8583_NS == SERVICE_NS, (
        "mfcommon's namespace has drifted from the WSDL's targetNamespace"
    )


def test_wsdl_soap_actions_match_what_the_client_sends():
    """
    SOAPAction is mandatory in SOAP 1.1 and ACE routes on it. A mismatch
    produces an HTTP 500 whose message never mentions SOAPAction, so it is
    worth pinning that the client builds exactly what the WSDL declares.
    """
    root = ET.parse(WSDL).getroot()
    binding = root.find(f"{{{WSDL_NS}}}binding")
    assert binding is not None

    soap_ns = "http://schemas.xmlsoap.org/wsdl/soap/"
    declared = {
        op.find(f"{{{soap_ns}}}operation").get("soapAction")
        for op in binding.findall(f"{{{WSDL_NS}}}operation")
    }

    # mfcommon builds these as f'"{namespace}/{operation}"', same strings,
    # without the surrounding quotes the HTTP header adds.
    expected = {
        f"{SERVICE_NS}/authorizeRequest",
        f"{SERVICE_NS}/reverseRequest",
        f"{SERVICE_NS}/networkManagementRequest",
    }
    assert declared == expected, f"declared={declared} expected={expected}"


def test_dfdl_models_every_field_the_mediation_sends():
    """
    ace-stub's mediation builds a DE dict; the DFDL schema has to have an
    element for each one, or ACE cannot serialize what the stub can.
    """
    root = ET.parse(XSD).getroot()
    declared = set()
    for element in root.iter(f"{{{XSD_NS}}}element"):
        name = element.get("name") or ""
        match = re.match(r"DE(\d{3})_", name)
        if match:
            declared.add(int(match.group(1)))

    # Every DE that services/ace-stub/app/mediation.py can emit.
    required = {2, 3, 4, 11, 22, 37, 48, 49, 52, 53}
    missing = required - declared
    assert not missing, f"DFDL schema has no element for DE {sorted(missing)}"
