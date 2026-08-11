"""
MSISDN normalisation.

The load-bearing property is that every way a customer might type their
number collapses to ONE stored value. If it does not, registration succeeds
and every later transfer to that number fails with "recipient not
registered", while the number on screen looks identical to the one in the
database. That is a miserable bug to chase, so the equivalence classes are
pinned explicitly.
"""

import pytest

from mfcommon.identity.msisdn import (
    InvalidMsisdn,
    display,
    is_msisdn,
    mask,
    normalise,
)


@pytest.mark.parametrize("written", [
    "03001234567",
    "0300 123 4567",
    "0300-123-4567",
    "+92 300 1234567",
    "+923001234567",
    "923001234567",
    "92 300 1234567",
    "(0300) 1234567",
])
def test_every_way_of_writing_one_number_collapses_to_one_value(written):
    assert normalise(written) == "923001234567"


def test_two_customers_are_not_merged():
    """The flip side: normalisation must not be so eager that distinct
    subscribers collide."""
    assert normalise("03001234567") != normalise("03001234568")


def test_a_number_already_international_is_left_alone():
    """Prefixing a country code onto a number that already has one would
    produce a 14-digit value that matches nothing."""
    assert normalise("923001234567") == "923001234567"
    assert normalise("447700900123") == "447700900123"


def test_the_country_code_is_configurable():
    assert normalise("07700900123", country_code="44") == "447700900123"


@pytest.mark.parametrize("bad", ["", "   ", "abc", "12345", None, "-", "+"])
def test_unusable_input_raises_rather_than_returning_something_broken(bad):
    """Returning a mangled identifier creates an account nobody can pay.
    Failing loudly at registration is far cheaper."""
    with pytest.raises(InvalidMsisdn):
        normalise(bad)


def test_too_long_is_rejected():
    with pytest.raises(InvalidMsisdn):
        normalise("9230012345678901234")


def test_is_msisdn_separates_phone_numbers_from_account_ids():
    assert is_msisdn("03001234567")
    assert is_msisdn("923001234567")
    assert not is_msisdn("acc_9f8h2k3m")
    assert not is_msisdn("")


def test_card_numbers_and_msisdns_genuinely_overlap():
    """
    Documenting the ambiguity rather than pretending it away.

    A 16-digit PAN is too long to be an MSISDN, but a 13 to 15 digit card
    number is indistinguishable from one by length. ledger-service therefore
    tries the CARD lookup first and only then treats the value as a phone
    number. Getting that order wrong would route a payment to whichever
    account happened to match.
    """
    assert not is_msisdn("4532015112830366")   # 16 digits, too long
    assert is_msisdn("453201511283")           # 12 digits, ambiguous


def test_mask_keeps_only_the_last_four():
    assert mask("923001234567") == "...4567"
    assert mask(None) == "-"
    assert "92300" not in mask("923001234567")


def test_display_is_readable():
    assert display("923001234567") == "+92 300 1234567"
