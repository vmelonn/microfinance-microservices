"""
Binary ISO 8583 parser / builder.

Everything stays in raw bytes, no ASCII digit encoding. Numeric fields are
packed as BCD (2 decimal digits per byte). The bitmap is raw binary (8 bytes,
extended to 16 bytes via the secondary bitmap when any DE 65-128 is present).
Includes a reader for raw files/streams framed with a 2-byte length header
(MLI - Message Length Indicator), which is how most real switches send
ISO 8583 over a TCP socket.

Vendor note: BCD padding for odd-length numeric strings, and whether the
length prefix on LLVAR/LLLVAR fields is itself BCD or ASCII, both vary by
processor. This implementation documents its choices inline, check your
target switch's spec and adjust if it differs.

PLATFORM NOTE: this module is the executable specification that the IBM ACE
DFDL schema (ace/Iso8583Library/dfdl/ISO8583.xsd) must agree with, byte for
byte. Two independent implementations of a binary wire format WILL drift
unless something forces them together, tests/e2e/test_dfdl_conformance.py
is that forcing function. If you change padding, length-prefix encoding, or
the binary-field list here, the DFDL schema is now wrong and the
conformance test is what will tell you.
"""

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# BCD helpers
# ---------------------------------------------------------------------------

def bcd_encode(digits: str) -> bytes:
    """2 decimal digits per byte. Odd-length strings get a trailing 'F' filler nibble."""
    if len(digits) % 2 == 1:
        digits += "F"
    return bytes.fromhex(digits)


def bcd_decode(data: bytes, num_digits: int) -> str:
    """Decode `data` as BCD, returning exactly num_digits decimal characters (drops filler)."""
    hexstr = data.hex().upper()
    return hexstr[:num_digits]


def bcd_byte_len(num_digits: int) -> int:
    return (num_digits + 1) // 2


# ---------------------------------------------------------------------------
# Field catalog
# ---------------------------------------------------------------------------

@dataclass
class FieldSpec:
    length_type: str   # "FIXED", "LLVAR", "LLLVAR"
    length: int          # digit/char count (fixed length, or max for LLVAR/LLLVAR)
    numeric: bool = True  # True = BCD packed, False = raw ASCII bytes
    binary: bool = False   # True = genuinely binary content (PIN blocks, MACs, EMV
                            # data), must NEVER have trailing bytes stripped on
                            # decode, unlike space-padded text fields, where a
                            # coincidental trailing byte (e.g. random ciphertext
                            # ending in 0x20, or any latin-1 whitespace codepoint)
                            # would otherwise be silently and incorrectly stripped
                            # by the same .rstrip() that correctly trims padding
                            # off a real text field like DE 43's merchant name.


# Full DE 2-128 catalog (ISO 8583:1987 base standard).
# DE 1 is reserved as the secondary-bitmap indicator, not a real data field.
# n = numeric (BCD packed), ans/an/b = alphanumeric or binary (raw bytes, not BCD).
# Fields 56-63 and 105-127 have no fixed standard meaning, every processor
# repurposes them, so their length here is a generic placeholder only.
FIELD_SPECS = {
    2:   FieldSpec("LLVAR", 19),                    # PAN
    3:   FieldSpec("FIXED", 6),                      # Processing code
    4:   FieldSpec("FIXED", 12),                     # Amount, transaction
    5:   FieldSpec("FIXED", 12),                     # Amount, settlement
    6:   FieldSpec("FIXED", 12),                     # Amount, cardholder billing
    7:   FieldSpec("FIXED", 10),                     # Transmission date/time
    8:   FieldSpec("FIXED", 8),                      # Amount, cardholder billing fee
    9:   FieldSpec("FIXED", 8),                      # Conversion rate, settlement
    10:  FieldSpec("FIXED", 8),                      # Conversion rate, cardholder billing
    11:  FieldSpec("FIXED", 6),                      # STAN
    12:  FieldSpec("FIXED", 6),                      # Local time
    13:  FieldSpec("FIXED", 4),                      # Local date
    14:  FieldSpec("FIXED", 4),                      # Date, expiration (YYMM)
    15:  FieldSpec("FIXED", 4),                      # Date, settlement
    16:  FieldSpec("FIXED", 4),                      # Date, conversion
    17:  FieldSpec("FIXED", 4),                      # Date, capture
    18:  FieldSpec("FIXED", 4),                      # Merchant category code (MCC)
    19:  FieldSpec("FIXED", 3),                      # Acquiring institution country code
    20:  FieldSpec("FIXED", 3),                      # PAN extended, country code
    21:  FieldSpec("FIXED", 3),                      # Forwarding institution country code
    22:  FieldSpec("FIXED", 3),                      # POS entry mode
    23:  FieldSpec("FIXED", 3),                      # Application PAN sequence number
    24:  FieldSpec("FIXED", 3),                      # Network International ID (NII)
    25:  FieldSpec("FIXED", 2),                      # POS condition code
    26:  FieldSpec("FIXED", 2),                      # POS PIN capture code
    27:  FieldSpec("FIXED", 1),                      # Authorizing ID response length
    28:  FieldSpec("FIXED", 9, numeric=False),       # Amount, transaction fee (x+n, sign+8 digits)
    29:  FieldSpec("FIXED", 9, numeric=False),       # Amount, settlement fee (x+n)
    30:  FieldSpec("FIXED", 9, numeric=False),       # Amount, transaction processing fee (x+n)
    31:  FieldSpec("FIXED", 9, numeric=False),       # Amount, settlement processing fee (x+n)
    32:  FieldSpec("LLVAR", 11),                     # Acquiring institution ID code
    33:  FieldSpec("LLVAR", 11),                     # Forwarding institution ID code
    34:  FieldSpec("LLVAR", 28),                     # PAN, extended
    35:  FieldSpec("LLVAR", 37, numeric=False),      # Track 2 data
    36:  FieldSpec("LLLVAR", 104, numeric=False),    # Track 3 data
    37:  FieldSpec("FIXED", 12, numeric=False),      # RRN
    38:  FieldSpec("FIXED", 6, numeric=False),       # Auth ID response
    39:  FieldSpec("FIXED", 2, numeric=False),       # Response code
    40:  FieldSpec("FIXED", 3, numeric=False),       # Service restriction code
    41:  FieldSpec("FIXED", 8, numeric=False),       # Terminal ID
    42:  FieldSpec("FIXED", 15, numeric=False),      # Merchant ID
    43:  FieldSpec("FIXED", 40, numeric=False),      # Card acceptor name/location
    44:  FieldSpec("LLVAR", 25, numeric=False, binary=True),      # Additional response data
    45:  FieldSpec("LLVAR", 76, numeric=False),      # Track 1 data
    46:  FieldSpec("LLLVAR", 999, numeric=False),    # Additional data, ISO use
    47:  FieldSpec("LLLVAR", 999, numeric=False),    # Additional data, national use
    48:  FieldSpec("LLLVAR", 999, numeric=False),    # Additional data, private use
    49:  FieldSpec("FIXED", 3),                      # Currency code, transaction
    50:  FieldSpec("FIXED", 3),                      # Currency code, settlement
    51:  FieldSpec("FIXED", 3),                      # Currency code, cardholder billing
    52:  FieldSpec("FIXED", 8, numeric=False, binary=True), # PIN data (binary block)
    53:  FieldSpec("FIXED", 16),                     # Security related control information
    54:  FieldSpec("LLLVAR", 120, numeric=False),    # Additional amounts
    55:  FieldSpec("LLLVAR", 255, numeric=False),    # ICC/EMV data (binary TLV)
    56:  FieldSpec("LLLVAR", 999, numeric=False, binary=True),    # Reserved, ISO use
    57:  FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, national use
    58:  FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, national use
    59:  FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, national use
    60:  FieldSpec("LLLVAR", 999, numeric=False),    # Advice/reason code, national use
    61:  FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, private use
    62:  FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, private use
    63:  FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, private use
    64:  FieldSpec("FIXED", 8, numeric=False, binary=True), # MAC
    66:  FieldSpec("FIXED", 1),                      # Settlement code
    67:  FieldSpec("FIXED", 2),                      # Extended payment code
    68:  FieldSpec("FIXED", 3),                      # Receiving institution country code
    69:  FieldSpec("FIXED", 3),                      # Settlement institution country code
    70:  FieldSpec("FIXED", 3),                      # Network management information code
    71:  FieldSpec("FIXED", 4),                      # Message number
    72:  FieldSpec("FIXED", 4),                      # Message number, last
    73:  FieldSpec("FIXED", 6),                      # Date, action
    74:  FieldSpec("FIXED", 10),                     # Credits, number
    75:  FieldSpec("FIXED", 10),                     # Credits, reversal number
    76:  FieldSpec("FIXED", 10),                     # Debits, number
    77:  FieldSpec("FIXED", 10),                     # Debits, reversal number
    78:  FieldSpec("FIXED", 10),                     # Transfer, number
    79:  FieldSpec("FIXED", 10),                     # Transfer, reversal number
    80:  FieldSpec("FIXED", 10),                     # Inquiries, number
    81:  FieldSpec("FIXED", 10),                     # Authorizations, number
    82:  FieldSpec("FIXED", 12),                     # Credits, processing fee amount
    83:  FieldSpec("FIXED", 12),                     # Credits, transaction fee amount
    84:  FieldSpec("FIXED", 12),                     # Debits, processing fee amount
    85:  FieldSpec("FIXED", 12),                     # Debits, transaction fee amount
    86:  FieldSpec("FIXED", 15),                     # Credits, amount
    87:  FieldSpec("FIXED", 15),                     # Credits, reversal amount
    88:  FieldSpec("FIXED", 15),                     # Debits, amount
    89:  FieldSpec("FIXED", 15),                     # Debits, reversal amount
    90:  FieldSpec("FIXED", 42),                     # Original data elements
    91:  FieldSpec("FIXED", 1, numeric=False),       # File update code
    92:  FieldSpec("FIXED", 2),                      # File security code
    93:  FieldSpec("FIXED", 5),                      # Response indicator
    94:  FieldSpec("FIXED", 7, numeric=False),       # Service indicator
    95:  FieldSpec("FIXED", 42, numeric=False),      # Replacement amounts
    96:  FieldSpec("FIXED", 8, numeric=False, binary=True), # Message security code
    97:  FieldSpec("FIXED", 17, numeric=False),      # Amount, net settlement (x+n)
    98:  FieldSpec("FIXED", 25, numeric=False),      # Payee
    99:  FieldSpec("LLVAR", 11),                     # Settlement institution ID code
    100: FieldSpec("LLVAR", 11),                     # Receiving institution ID code
    101: FieldSpec("LLVAR", 17, numeric=False),      # File name
    102: FieldSpec("LLVAR", 28, numeric=False),      # Account ID 1
    103: FieldSpec("LLVAR", 28, numeric=False),      # Account ID 2
    104: FieldSpec("LLLVAR", 100, numeric=False),    # Transaction description
    105: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, ISO use
    106: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, ISO use
    107: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, ISO use
    108: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, ISO use
    109: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, ISO use
    110: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, ISO use
    111: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, ISO use
    112: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, national use
    113: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, national use
    114: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, national use
    115: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, national use
    116: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, national use
    117: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, national use
    118: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, national use
    119: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, national use
    120: FieldSpec("LLLVAR", 999, numeric=False, binary=True),    # Reserved, national use
    121: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, private use
    122: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, private use
    123: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, private use
    124: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, private use
    125: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, private use
    126: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, private use
    127: FieldSpec("LLLVAR", 999, numeric=False),    # Reserved, private use
    128: FieldSpec("FIXED", 8, numeric=False, binary=True), # MAC, field 2
}

DE_NAMES = {
    2: "PAN", 3: "Processing code", 4: "Amount, transaction",
    5: "Amount, settlement", 6: "Amount, cardholder billing",
    7: "Transmission date/time", 8: "Amount, cardholder billing fee",
    9: "Conversion rate, settlement", 10: "Conversion rate, cardholder billing",
    11: "STAN", 12: "Local time", 13: "Local date", 14: "Date, expiration",
    15: "Date, settlement", 16: "Date, conversion", 17: "Date, capture",
    18: "Merchant category code", 19: "Acquiring institution country code",
    20: "PAN extended, country code", 21: "Forwarding institution country code",
    22: "POS entry mode", 23: "Application PAN sequence number",
    24: "Network International ID", 25: "POS condition code",
    26: "POS PIN capture code", 27: "Authorizing ID response length",
    28: "Amount, transaction fee", 29: "Amount, settlement fee",
    30: "Amount, transaction processing fee", 31: "Amount, settlement processing fee",
    32: "Acquiring institution ID", 33: "Forwarding institution ID",
    34: "PAN, extended", 35: "Track 2 data", 36: "Track 3 data",
    37: "RRN", 38: "Auth ID response", 39: "Response code",
    40: "Service restriction code", 41: "Terminal ID", 42: "Merchant ID",
    43: "Card acceptor name/location", 44: "Additional response data",
    45: "Track 1 data", 46: "Additional data, ISO use",
    47: "Additional data, national use", 48: "Additional data, private use",
    49: "Currency code, transaction", 50: "Currency code, settlement",
    51: "Currency code, cardholder billing", 52: "PIN block",
    53: "Security related control info", 54: "Additional amounts",
    55: "ICC/EMV data", 56: "Reserved, ISO use", 57: "Reserved, national use",
    58: "Reserved, national use", 59: "Reserved, national use",
    60: "Advice/reason code, national use", 61: "Reserved, private use",
    62: "Reserved, private use", 63: "Reserved, private use",
    64: "MAC", 66: "Settlement code", 67: "Extended payment code",
    68: "Receiving institution country code", 69: "Settlement institution country code",
    70: "Network management info code", 71: "Message number",
    72: "Message number, last", 73: "Date, action", 74: "Credits, number",
    75: "Credits, reversal number", 76: "Debits, number",
    77: "Debits, reversal number", 78: "Transfer, number",
    79: "Transfer, reversal number", 80: "Inquiries, number",
    81: "Authorizations, number", 82: "Credits, processing fee amount",
    83: "Credits, transaction fee amount", 84: "Debits, processing fee amount",
    85: "Debits, transaction fee amount", 86: "Credits, amount",
    87: "Credits, reversal amount", 88: "Debits, amount",
    89: "Debits, reversal amount", 90: "Original data elements",
    91: "File update code", 92: "File security code", 93: "Response indicator",
    94: "Service indicator", 95: "Replacement amounts", 96: "Message security code",
    97: "Amount, net settlement", 98: "Payee", 99: "Settlement institution ID",
    100: "Receiving institution ID", 101: "File name", 102: "Account ID 1",
    103: "Account ID 2", 104: "Transaction description",
    **{n: "Reserved, ISO use" for n in range(105, 112)},
    **{n: "Reserved, national use" for n in range(112, 121)},
    **{n: "Reserved, private use" for n in range(121, 128)},
    128: "MAC, field 2",
}


# ---------------------------------------------------------------------------
# Bitmap (raw binary, primary + secondary)
# ---------------------------------------------------------------------------

def _set_bit(bitmap: bytearray, n: int) -> None:
    byte_idx = (n - 1) // 8
    bit_idx = 7 - ((n - 1) % 8)
    bitmap[byte_idx] |= (1 << bit_idx)


def build_bitmap(field_numbers) -> bytes:
    """DE 1 is reserved as the 'secondary bitmap follows' flag, not a data field."""
    has_secondary = any(n > 64 for n in field_numbers)
    bitmap = bytearray(16 if has_secondary else 8)
    if has_secondary:
        _set_bit(bitmap, 1)
    for n in field_numbers:
        if n == 1:
            raise ValueError("DE 1 is reserved for the secondary bitmap indicator")
        _set_bit(bitmap, n)
    return bytes(bitmap)


def read_bitmap(raw: bytes, offset: int):
    """Returns (sorted list of present DE numbers, bytes consumed)."""
    first8 = raw[offset:offset + 8]
    has_secondary = bool(first8[0] & 0x80)  # bit 1 = MSB of byte 0
    total_len = 16 if has_secondary else 8
    bitmap = raw[offset:offset + total_len]

    fields = []
    for i in range(total_len * 8):
        n = i + 1
        if n == 1:
            continue
        byte_idx = i // 8
        bit_idx = 7 - (i % 8)
        if bitmap[byte_idx] & (1 << bit_idx):
            fields.append(n)
    return fields, total_len


# ---------------------------------------------------------------------------
# Build / parse a single message
# ---------------------------------------------------------------------------

def build_message(mti: str, fields: dict) -> bytes:
    """fields: {de_number: value_as_string}. Returns raw bytes: MTI + bitmap + DEs."""
    out = bytearray()
    out += bcd_encode(mti)                      # MTI: 2 bytes BCD (4 digits)
    out += build_bitmap(fields.keys())

    for de in sorted(fields.keys()):
        spec = FIELD_SPECS.get(de)
        if spec is None:
            raise ValueError(f"No FieldSpec defined for DE {de}")
        value = str(fields[de])

        if spec.length_type == "FIXED":
            if spec.numeric:
                out += bcd_encode(value.rjust(spec.length, "0"))
            else:
                out += value.ljust(spec.length).encode("latin-1")[:spec.length]

        elif spec.length_type == "LLVAR":
            out += bcd_encode(f"{len(value):02d}")   # 1-byte BCD length prefix
            out += bcd_encode(value) if spec.numeric else value.encode("latin-1")

        elif spec.length_type == "LLLVAR":
            out += bcd_encode(f"{len(value):04d}")   # 2-byte BCD length prefix (0 + 3 digits)
            out += bcd_encode(value) if spec.numeric else value.encode("latin-1")

    return bytes(out)


def parse_message(raw: bytes, offset: int = 0):
    """Returns (parsed_dict, bytes_consumed) starting at `offset` in `raw`."""
    pos = offset
    mti = bcd_decode(raw[pos:pos + 2], 4)
    pos += 2

    present_fields, bitmap_len = read_bitmap(raw, pos)
    pos += bitmap_len

    fields = {}
    for de in present_fields:
        spec = FIELD_SPECS.get(de)
        if spec is None:
            raise ValueError(f"No FieldSpec defined for DE {de}, cannot continue parsing")

        if spec.length_type == "FIXED":
            if spec.numeric:
                nbytes = bcd_byte_len(spec.length)
                value = bcd_decode(raw[pos:pos + nbytes], spec.length)
                pos += nbytes
            else:
                raw_value = raw[pos:pos + spec.length].decode("latin-1")
                # rstrip() correctly trims space-padding off a real text
                # field (e.g. DE 43's merchant name), but binary content
                # (a PIN block, a MAC) can coincidentally end in a byte
                # that decodes to a whitespace codepoint, stripping it
                # there would silently corrupt genuine data, not padding.
                value = raw_value if spec.binary else raw_value.rstrip()
                pos += spec.length

        elif spec.length_type == "LLVAR":
            length = int(bcd_decode(raw[pos:pos + 1], 2))
            pos += 1
            if spec.numeric:
                nbytes = bcd_byte_len(length)
                value = bcd_decode(raw[pos:pos + nbytes], length)
                pos += nbytes
            else:
                value = raw[pos:pos + length].decode("latin-1")
                pos += length

        elif spec.length_type == "LLLVAR":
            length = int(bcd_decode(raw[pos:pos + 2], 4))
            pos += 2
            if spec.numeric:
                nbytes = bcd_byte_len(length)
                value = bcd_decode(raw[pos:pos + nbytes], length)
                pos += nbytes
            else:
                value = raw[pos:pos + length].decode("latin-1")
                pos += length

        fields[de] = value

    return {"mti": mti, "fields": fields}, pos - offset


# ---------------------------------------------------------------------------
# Enumerated code lookups (DEs whose values are drawn from a fixed list)
# ---------------------------------------------------------------------------

DE3_TRANSACTION_TYPES = {
    "00": "Purchase", "01": "Withdrawal (cash)", "02": "Adjustment",
    "09": "Purchase with cashback", "10": "Balance inquiry", "17": "Payment",
    "18": "Transfer", "20": "Refund/deposit", "21": "Deposit",
    "22": "Balance inquiry (alt)", "28": "Fee collection",
    "30": "Balance inquiry (savings)", "40": "Transfer between accounts",
    "50": "Bill payment", "90": "Reversal", "91": "Reversal, partial",
}

DE3_ACCOUNT_TYPES = {
    "00": "Default/not specified", "10": "Savings account",
    "20": "Checking/current account", "30": "Credit card account",
    "40": "Universal/general ledger account", "50": "Investment account",
}

DE22_ENTRY_MODES = {
    "00": "Unknown/unspecified", "01": "Manual key entry",
    "02": "Magnetic stripe read", "03": "Bar code", "04": "OCR",
    "05": "Chip (EMV)", "07": "Contactless chip (EMV)",
    "10": "Credit card imprinter only", "51": "Chip, PIN verified",
    "79": "Chip fallback to magstripe", "81": "Contactless magstripe",
    "90": "Magnetic stripe, all track data present", "91": "Contactless chip",
}

DE25_POS_CONDITION_CODES = {
    "00": "Normal presentment, cardholder present",
    "01": "Cardholder not present (mail/phone/internet)",
    "02": "Unattended terminal", "03": "Merchant suspicious of transaction",
    "04": "Cardholder present, card not present", "05": "Preauthorized",
    "06": "Magnetic stripe read failure", "08": "Mail order",
    "59": "Suspicious transaction", "71": "Chip read failure, fallback",
    "90": "Original transaction",
}

DE39_RESPONSE_CODES = {
    "00": "Approved / completed successfully", "01": "Refer to card issuer",
    "02": "Refer to card issuer, special condition", "03": "Invalid merchant",
    "04": "Pick up card", "05": "Do not honor", "06": "Error",
    "07": "Pick up card, special condition", "08": "Honor with identification",
    "09": "Request in progress", "10": "Approved for partial amount",
    "12": "Invalid transaction", "13": "Invalid amount", "14": "Invalid card number",
    "15": "No such issuer", "17": "Customer cancellation", "19": "Re-enter transaction",
    "20": "Invalid response", "21": "No action taken", "25": "Unable to locate record",
    "30": "Format error", "31": "Bank not supported by switch",
    "33": "Expired card, pick up", "34": "Suspected fraud, pick up",
    "38": "PIN tries exceeded, pick up", "39": "No credit account",
    "41": "Lost card, pick up", "43": "Stolen card, pick up",
    "51": "Insufficient funds", "52": "No checking account", "53": "No savings account",
    "54": "Expired card", "55": "Incorrect PIN", "56": "No card record",
    "57": "Transaction not permitted to cardholder",
    "58": "Transaction not permitted to terminal", "59": "Suspected fraud",
    "61": "Exceeds withdrawal amount limit", "62": "Restricted card",
    "63": "Security violation", "65": "Exceeds withdrawal frequency limit",
    "68": "Response received too late", "75": "PIN tries exceeded",
    "76": "Unable to locate previous message", "77": "Original amount incorrect",
    "78": "No account", "80": "Invalid date", "81": "Cryptographic error in PIN",
    "82": "Negative CVV", "83": "Cannot verify PIN",
    "85": "No reason to decline", "91": "Issuer or switch inoperative",
    "92": "Financial institution not found for routing",
    "93": "Transaction cannot be completed, violation of law",
    "94": "Duplicate transmission", "96": "System malfunction",
}


def decode_meaning(de: int, value: str):
    """Returns a human-readable meaning for coded DEs, or None if DE isn't coded."""
    if de == 3 and len(value) == 6:
        tt, from_acct, to_acct = value[0:2], value[2:4], value[4:6]
        tt_name = DE3_TRANSACTION_TYPES.get(tt, f"unknown type {tt}")
        from_name = DE3_ACCOUNT_TYPES.get(from_acct, f"unknown acct {from_acct}")
        to_name = DE3_ACCOUNT_TYPES.get(to_acct, f"unknown acct {to_acct}")
        return f"{tt_name}, from {from_name}, to {to_name}"
    if de == 22:
        return DE22_ENTRY_MODES.get(value[:2])
    if de == 25:
        return DE25_POS_CONDITION_CODES.get(value)
    if de == 39:
        return DE39_RESPONSE_CODES.get(value)
    return None


def explain(parsed: dict) -> None:
    print(f"MTI: {parsed['mti']}")
    for de, value in sorted(parsed["fields"].items()):
        name = DE_NAMES.get(de, "Unknown field")
        meaning = decode_meaning(de, value)
        suffix = f"  ({meaning})" if meaning else ""
        print(f"  DE {de:<3} {name:<28} = {value}{suffix}")


# ---------------------------------------------------------------------------
# Reading a raw file / byte stream
# ---------------------------------------------------------------------------

def iter_messages_from_file(path: str, length_prefixed: bool = True, prefix_bytes: int = 2):
    """
    Yields parsed ISO 8583 messages from a raw binary file.

    length_prefixed=True assumes each message is preceded by an MLI
    (Message Length Indicator), a big-endian integer giving the byte
    length of the message that follows. This is the standard TCP framing
    most switches use, so a captured raw socket stream will look like:
        [2-byte length][message bytes][2-byte length][message bytes]...

    length_prefixed=False assumes the file is a single raw message with
    no framing at all.
    """
    with open(path, "rb") as f:
        data = f.read()

    if not length_prefixed:
        parsed, _ = parse_message(data)
        yield parsed
        return

    pos = 0
    while pos < len(data):
        msg_len = int.from_bytes(data[pos:pos + prefix_bytes], byteorder="big")
        pos += prefix_bytes
        msg_bytes = data[pos:pos + msg_len]
        parsed, consumed = parse_message(msg_bytes)
        yield parsed
        pos += msg_len


def write_messages_to_file(path: str, messages, length_prefixed: bool = True, prefix_bytes: int = 2) -> None:
    """messages: list of (mti, fields_dict) tuples."""
    with open(path, "wb") as f:
        for mti, fields in messages:
            raw = build_message(mti, fields)
            if length_prefixed:
                f.write(len(raw).to_bytes(prefix_bytes, byteorder="big"))
            f.write(raw)
