"""
MSISDN handling: the customer identifier for a mobile money platform.

MSISDN (Mobile Station International Subscriber Directory Number) is the full
international phone number. In mobile money it is not merely a contact
detail, it IS the account identifier: you log in with it and you send money
to it. That is why this lives in mfcommon rather than in one service.

WHY SHARED CODE AND NOT A CONVENTION. auth-service stores the MSISDN a
customer registered with, and ledger-service resolves an MSISDN to an account
when someone sends money. If those two normalise differently by even one
character, registration succeeds and every subsequent transfer to that number
fails with "recipient not registered" while the number on screen looks
identical to the one in the database. Two implementations of a normalisation
rule is the same class of hazard as two implementations of a wire format.

NORMALISATION, and its honest limits.

A customer will type the same number several ways:

    0300 123 4567      local, spaced
    03001234567        local
    +92 300 1234567    international
    923001234567       international, no plus

All four are one subscriber and must resolve to one account. So the stored
form is digits only, in international format without a plus:

    923001234567

The leading-zero rule is the part that carries an assumption: a national
number beginning with a single 0 has that 0 replaced by DEFAULT_COUNTRY_CODE.
That is correct for Pakistan and for most of the ITU E.164 world, and wrong
for the handful of countries where the trunk prefix differs. It is
configurable rather than hardcoded, and a number already in international
form is left alone.

What this deliberately does NOT do is verify that a number exists, is
reachable, or belongs to the person registering it. Real mobile money proves
that with an OTP before the account is usable. This platform does not, and
that is a genuine gap rather than an oversight.
"""

from __future__ import annotations

import os
import re

# Pakistan. Overridable, because the trunk-prefix rule below is only correct
# for countries that use a single leading 0 nationally.
DEFAULT_COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "92")

# E.164 allows at most 15 digits including the country code. The lower bound
# is deliberately loose: national numbering plans vary, and rejecting a valid
# short number is worse than accepting a wrong one that simply will not
# resolve to an account.
MIN_DIGITS = 10
MAX_DIGITS = 15

_NON_DIGITS = re.compile(r"[^\d]")


class InvalidMsisdn(ValueError):
    """The value cannot be a phone number. Carries a message safe to show."""


def normalise(raw: str, country_code: str | None = None) -> str:
    """
    Turn anything a customer might type into the single stored form.

        "0300 123 4567"   -> "923001234567"
        "+92 300 1234567" -> "923001234567"
        "923001234567"    -> "923001234567"

    Raises InvalidMsisdn rather than returning something unusable, because a
    silently mangled identifier becomes an account nobody can pay.
    """
    if raw is None:
        raise InvalidMsisdn("A phone number is required.")

    digits = _NON_DIGITS.sub("", str(raw))
    if not digits:
        raise InvalidMsisdn("A phone number must contain digits.")

    # A single leading 0 is a national trunk prefix, not part of the number.
    # Replace it with the country code. A number that already starts with a
    # country code is left as it is.
    if digits.startswith("0"):
        digits = (country_code or DEFAULT_COUNTRY_CODE) + digits.lstrip("0")

    if not (MIN_DIGITS <= len(digits) <= MAX_DIGITS):
        raise InvalidMsisdn(
            f"A phone number must be {MIN_DIGITS} to {MAX_DIGITS} digits once "
            f"normalised; got {len(digits)} from {raw!r}."
        )

    return digits


def is_msisdn(value: str) -> bool:
    """
    True if this looks like a phone number rather than an account ID or a card.

    Used by ledger-service to decide how to interpret an identifier, so it has
    to be decisive against the other two things that arrive in the same field:

        acc_9f8h2k3m      an account ID, has letters and an underscore
        4532015112830366  a card number, 12 to 19 digits
        923001234567      an MSISDN, 10 to 15 digits

    Cards and MSISDNs overlap at 12 to 15 digits, so the caller must try the
    card lookup FIRST and fall back to this. Length alone cannot separate
    them, and pretending otherwise would route payments to the wrong account.
    """
    try:
        normalise(value)
        return True
    except InvalidMsisdn:
        return False


def mask(value: str | None) -> str:
    """
    For logs and traces. Keeps the last four digits, which is enough to
    recognise your own number on a receipt and not enough to be a contact
    list. Same reasoning as PAN truncation in the audit rules.
    """
    if not value:
        return "-"
    digits = _NON_DIGITS.sub("", str(value))
    return f"...{digits[-4:]}" if len(digits) > 4 else "..."


def display(value: str) -> str:
    """Stored form back to something readable: 923001234567 -> +92 300 1234567."""
    if not value:
        return ""
    if len(value) < 5:
        return value
    # Country codes are 1 to 3 digits and cannot be derived from the number
    # alone, so this assumes the configured one when it matches.
    cc = DEFAULT_COUNTRY_CODE
    if value.startswith(cc):
        rest = value[len(cc):]
        return f"+{cc} {rest[:3]} {rest[3:]}" if len(rest) > 3 else f"+{cc} {rest}"
    return f"+{value}"
