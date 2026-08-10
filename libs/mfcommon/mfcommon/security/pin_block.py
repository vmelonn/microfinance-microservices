"""
PIN block formatting (ISO 9564 Format 0 / ANSI X9.8) -- what happens to a
PIN before it's encrypted.

The PIN alone isn't encrypted directly. It's first combined (XORed) with
part of the account number, so that the same PIN encrypted for two
different cards produces different ciphertext, and an encrypted block can't
be replayed against a different account.
"""


def build_pin_field(pin: str) -> str:
    """
    Format 0 PIN field: control nibble '0', PIN length nibble, the PIN
    digits themselves, then padded with 'F' nibbles out to 16 hex digits
    (8 bytes) total.
    """
    length = len(pin)
    field = f"0{length:X}{pin}"
    return field.ljust(16, "F")


def build_pan_field(pan: str) -> str:
    """
    PAN field: 4 zero nibbles, then the 12 PAN digits immediately before
    the check digit (i.e. excluding the last digit), for 16 hex digits total.
    """
    pan_digits = pan[-13:-1]  # 12 digits, excluding the final check digit
    field = "0000" + pan_digits
    return field.rjust(16, "0")[-16:]


def build_iso0_pin_block(pin: str, pan: str) -> bytes:
    """Combines the PIN field and PAN field with XOR, per ISO 9564 Format 0."""
    pin_int = int(build_pin_field(pin), 16)
    pan_int = int(build_pan_field(pan), 16)
    return (pin_int ^ pan_int).to_bytes(8, byteorder="big")


def recover_pin(pin_block: bytes, pan: str) -> str:
    """Inverse operation: XOR is its own inverse, so this recovers the PIN field, then the PIN."""
    pan_int = int(build_pan_field(pan), 16)
    pin_field_int = int.from_bytes(pin_block, byteorder="big") ^ pan_int
    pin_field = f"{pin_field_int:016X}"
    length = int(pin_field[1], 16)
    return pin_field[2:2 + length]
