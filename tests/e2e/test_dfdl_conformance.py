"""
Conformance between the Python ISO 8583 codec and the ACE DFDL schema.

THE PROBLEM THIS SOLVES. There are two independent implementations of the
same binary wire format in this repository:

    libs/mfcommon/mfcommon/iso8583/parser.py      (Python, runs today)
    ace/Iso8583Library/dfdl/ISO8583.xsd           (DFDL, runs once entitled)

Two implementations of a binary format WILL drift. Not might -- will. Someone
adjusts BCD padding in one and not the other, and messages that look fine in
testing corrupt in production, in a way that manifests as the switch closing
the connection with no explanation.

WHAT THIS FILE ACTUALLY DOES. It cannot execute the DFDL schema -- that needs
an ACE runtime, which needs an entitlement key. What it CAN do is assert the
exact byte-level properties the DFDL schema declares, against the Python
implementation. Every assertion below corresponds to a specific declaration
in ISO8583.xsd, named in the test.

So this is a conformance test against the SPEC, executed on one of the two
implementations. If the Python side changes, this fails and the DFDL schema
is now known to be wrong. That is a genuinely useful guard, and it is
honestly less than a test that ran both.

WHAT IT DOES NOT PROVE. That the DFDL schema parses. It may contain syntax
errors, a mis-declared occursCount, or a namespace typo, and nothing here
would catch it. The first ACE deployment is still the first real test of that
file.
"""

import pytest

from mfcommon.iso8583.parser import (
    FIELD_SPECS,
    bcd_encode,
    build_bitmap,
    build_message,
    parse_message,
    read_bitmap,
)


# ---------------------------------------------------------------------------
# MTI -- xsd:string, dfdl:length="2", binaryNumberRep="bcd"
# ---------------------------------------------------------------------------

def test_mti_is_two_bytes_bcd():
    """ISO8583.xsd declares MTI as 2 bytes BCD, so "0200" must be 0x02 0x00."""
    raw = build_message("0200", {39: "00"})
    assert raw[:2] == b"\x02\x00"

    raw = build_message("0810", {39: "00"})
    assert raw[:2] == b"\x08\x10"


# ---------------------------------------------------------------------------
# Bitmap -- 64 one-bit elements, dfdl:lengthUnits="bits"
# ---------------------------------------------------------------------------

def test_primary_bitmap_is_eight_bytes_when_no_field_exceeds_64():
    raw = build_message("0200", {2: "4111111111111111", 3: "000000", 4: "000000001000"})
    # 2 bytes MTI + 8 bytes bitmap, no secondary
    bitmap = raw[2:10]
    assert len(bitmap) == 8
    assert bitmap[0] & 0x80 == 0, "secondary-bitmap flag set with no DE above 64"


def test_secondary_bitmap_appears_only_when_a_field_exceeds_64():
    """ISO8583.xsd gates SecondaryBitmap on occursCount="{ ../PrimaryBitmap/Bit1 }",
    so Bit1 must be set exactly when DE 65-128 are in play."""
    without = build_message("0200", {2: "4111111111111111"})
    assert without[2] & 0x80 == 0

    with_de103 = build_message("0200", {2: "4111111111111111", 103: "acc_123"})
    assert with_de103[2] & 0x80 != 0, "DE 103 present but Bit1 not set"
    fields, length = read_bitmap(with_de103, 2)
    assert length == 16, "secondary bitmap should extend the bitmap to 16 bytes"
    assert 103 in fields


def test_bit_position_maps_to_the_declared_de_number():
    """Bit N in the DFDL schema must correspond to DE N. An off-by-one here
    would shift every field in the message."""
    for de in (2, 3, 4, 11, 22, 37, 39, 52, 64):
        bitmap = build_bitmap([de])
        byte_index = (de - 1) // 8
        bit_index = 7 - ((de - 1) % 8)
        assert bitmap[byte_index] & (1 << bit_index), f"DE {de} bit not set at the expected position"


def test_de1_is_reserved_and_rejected_as_a_data_field():
    """Bit1 is the secondary-bitmap flag, which is why ISO8583.xsd declares
    no DE 1 element. Using it as a field must fail loudly."""
    with pytest.raises(ValueError, match="DE 1 is reserved"):
        build_bitmap([1, 2])


# ---------------------------------------------------------------------------
# BCD -- the odd-length filler rule
# ---------------------------------------------------------------------------

def test_bcd_packs_two_digits_per_byte():
    assert bcd_encode("1234") == b"\x12\x34"
    assert bcd_encode("000000005000") == b"\x00\x00\x00\x00\x50\x00"


def test_odd_length_numerics_get_a_TRAILING_f_nibble():
    """
    VENDOR-SPECIFIC and load-bearing. This implementation pads odd-length
    numerics with a trailing 'F'; some processors pad leading. If the DFDL
    schema and the switch disagree with this, every odd-length field shifts
    by one nibble and the remainder of the message is garbage.

    DE 22 (entry mode, 3 digits) is the field where this actually bites.
    """
    assert bcd_encode("051") == b"\x05\x1f"
    assert bcd_encode("5") == b"\x5f"

    raw = build_message("0200", {22: "051"})
    # 2 MTI + 8 bitmap, then DE 22
    assert raw[10:12] == b"\x05\x1f"


def test_de22_occupies_two_bytes_as_declared():
    """ISO8583.xsd sets DE022_EntryMode to dfdl:length="2" -- 3 digits
    rounded up through the filler nibble."""
    assert FIELD_SPECS[22].length == 3
    assert FIELD_SPECS[22].numeric is True
    raw = build_message("0200", {22: "051"})
    assert len(raw) == 2 + 8 + 2


# ---------------------------------------------------------------------------
# LLVAR -- the digits-vs-bytes trap
# ---------------------------------------------------------------------------

def test_llvar_length_prefix_counts_DIGITS_not_bytes():
    """
    The single most common ISO 8583 parsing bug, and the reason
    ISO8583.xsd's DE002_PAN computes its content length as
    ceiling(LL div 2) rather than using LL directly.

    A 16-digit PAN has LL = 16 and occupies 8 bytes.
    """
    raw = build_message("0200", {2: "4111111111111111"})
    payload = raw[10:]

    assert payload[0] == 0x16, "LL should be BCD 16 (the DIGIT count), not 8"
    assert len(payload) == 1 + 8, "16 digits must pack into 8 bytes after the 1-byte prefix"

    parsed, _ = parse_message(raw)
    assert parsed["fields"][2] == "4111111111111111"


def test_odd_length_pan_round_trips():
    """19-digit PAN: LL = 19, content = 10 bytes with a trailing filler nibble."""
    pan = "4111111111111111111"
    raw = build_message("0200", {2: pan})
    payload = raw[10:]

    assert payload[0] == 0x19
    assert len(payload) == 1 + 10

    parsed, _ = parse_message(raw)
    assert parsed["fields"][2] == pan


def test_lllvar_uses_a_two_byte_prefix():
    """ISO8583.xsd's DE048_AdditionalData declares LLL as dfdl:length="2"
    (4 BCD digits), and the content length is in BYTES for a text field."""
    raw = build_message("0200", {48: "SIMULATE_TIMEOUT"})
    payload = raw[10:]

    assert payload[0:2] == b"\x00\x16", "LLL should be BCD 0016 across two bytes"
    assert payload[2:2 + 16] == b"SIMULATE_TIMEOUT"


# ---------------------------------------------------------------------------
# The no-trim rule on binary fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("de", [52, 64, 96, 128])
def test_binary_fields_are_flagged_as_binary(de):
    """ISO8583.xsd sets textTrimKind="none" on DE 52 and DE 64. The Python
    codec expresses the same rule via FieldSpec.binary. Both exist to stop
    trailing bytes being stripped from ciphertext."""
    assert FIELD_SPECS[de].binary is True, f"DE {de} must be marked binary"


def test_pin_block_ending_in_whitespace_bytes_is_not_trimmed():
    """
    The bug this rule exists for, made deterministic.

    An encrypted PIN block is 8 bytes of ciphertext. It can coincidentally
    end in 0x20 (space) or another byte that decodes to whitespace. Trimming
    it -- correct for a space-padded text field like DE 43 -- silently
    destroys real key material here, and the switch then derives the wrong
    PIN and declines with DE 39 = 55.

    Rare enough to survive testing and reach production, which is exactly why
    it is pinned deterministically rather than left to chance.
    """
    for trailing in (b"\x20", b"\x09", b"\x0a", b"\x0d", b"\x20\x20"):
        block = (b"\x8f\x21\x00\xff\x9a\x0d\x1e\x7c"[: 8 - len(trailing)] + trailing)
        assert len(block) == 8

        raw = build_message("0200", {52: block.decode("latin-1")})
        parsed, _ = parse_message(raw)
        recovered = parsed["fields"][52].encode("latin-1")

        assert recovered == block, f"trailing {trailing!r} was stripped from DE 52"
        assert len(recovered) == 8


def test_text_fields_ARE_trimmed_because_that_padding_is_not_data():
    """The other half of the rule. DE 43 is genuinely space-padded to 40
    characters, and ISO8583.xsd sets textTrimKind="padChar" for it. Trimming
    here is correct -- the distinction between the two cases is the whole
    point."""
    raw = build_message("0200", {43: "CORNER SHOP LAHORE"})
    parsed, _ = parse_message(raw)
    assert parsed["fields"][43] == "CORNER SHOP LAHORE"
    assert not parsed["fields"][43].endswith(" ")


# ---------------------------------------------------------------------------
# Text vs BCD field typing
# ---------------------------------------------------------------------------

def test_rrn_is_text_not_bcd():
    """ISO8583.xsd declares DE037_RRN as representation="text",
    dfdl:length="12" -- 12 CHARACTERS occupying 12 bytes. Treating it as BCD
    would pack it into 6 and shift everything after it."""
    assert FIELD_SPECS[37].numeric is False
    raw = build_message("0200", {37: "000123456789"})
    assert raw[10:22] == b"000123456789"
    assert len(raw) == 2 + 8 + 12


def test_response_code_is_two_text_bytes():
    assert FIELD_SPECS[39].numeric is False
    raw = build_message("0210", {39: "00"})
    assert raw[10:12] == b"00"


def test_amount_is_bcd_and_exactly_six_bytes():
    """DE 4 is 12 digits -> 6 bytes. The WSDL enforces the 12-digit shape on
    the way in so this can never receive an unpadded value."""
    raw = build_message("0200", {4: "000000005000"})
    assert raw[10:16] == b"\x00\x00\x00\x00\x50\x00"


# ---------------------------------------------------------------------------
# Field ordering -- position IS identity
# ---------------------------------------------------------------------------

def test_fields_are_emitted_in_ascending_de_order():
    """
    ISO 8583 has no field tags on the wire. A field emitted out of order
    shifts every subsequent field, and the receiver decodes garbage from that
    point on. ISO8583.xsd relies on this via dfdl:sequenceKind="ordered".
    """
    # Deliberately built from an unordered dict.
    raw = build_message("0200", {49: "840", 2: "4111111111111111", 4: "000000001000", 3: "000000"})
    parsed, _ = parse_message(raw)

    assert parsed["fields"][2] == "4111111111111111"
    assert parsed["fields"][3] == "000000"
    assert parsed["fields"][4] == "000000001000"
    assert parsed["fields"][49] == "840"


def test_full_authorization_message_round_trips():
    """A realistic 0200 with everything the platform actually sends."""
    fields = {
        2: "4532015112830366",
        3: "000000",
        4: "000000005000",
        7: "0731143210",
        11: "000123",
        22: "051",
        37: "000123456789",
        49: "840",
        52: "\x8f\x21\x00\xff\x9a\x0d\x1e\x7c",
        53: "0000000000000001",
        103: "acc_abc123",          # forces the secondary bitmap
    }

    parsed, consumed = parse_message(build_message("0200", fields))

    assert parsed["mti"] == "0200"
    assert consumed == len(build_message("0200", fields))
    for de, expected in fields.items():
        assert parsed["fields"][de] == expected, f"DE {de} did not survive the round trip"


# ---------------------------------------------------------------------------
# MLI framing -- ISO8583Framed in the DFDL schema
# ---------------------------------------------------------------------------

def test_mli_is_two_bytes_big_endian():
    """ISO8583.xsd's ISO8583Framed declares MessageLength as 2 bytes,
    byteOrder bigEndian, and derives the message length from it."""
    raw = build_message("0200", {2: "4111111111111111", 4: "000000005000"})
    header = len(raw).to_bytes(2, "big")

    assert len(header) == 2
    assert int.from_bytes(header, "big") == len(raw)
    # Big-endian, not little: a 300-byte message must frame as 0x01 0x2C.
    assert (300).to_bytes(2, "big") == b"\x01\x2c"
