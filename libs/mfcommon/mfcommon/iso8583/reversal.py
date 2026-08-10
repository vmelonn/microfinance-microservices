"""
Builds an ISO 8583 reversal request (MTI 0400) referencing an original
request that timed out.

DE 90 (Original data elements) is what ties the reversal back to the
original message. In the base standard it's built from the original MTI
(4 digits) + original STAN (6 digits) + the original transmission date/time
(10) + acquiring institution ID (11) + forwarding institution ID (11) = 42
digits. A real switch would echo the *actual* values from the original
request; this learning build simplifies the last three parts to zero-padded
placeholders, since our host simulator only needs the MTI and STAN to
recognize which transaction is being reversed.
"""


def build_original_data_elements(original_mti: str, original_stan: str) -> str:
    return (
        original_mti
        + original_stan
        + "0" * 10   # placeholder: original transmission date/time
        + "0" * 11   # placeholder: original acquiring institution ID
        + "0" * 11   # placeholder: original forwarding institution ID
    )


def build_reversal_fields(reversal_stan: str, original_mti: str, original_stan: str) -> dict:
    return {
        11: reversal_stan,
        90: build_original_data_elements(original_mti, original_stan),
    }
