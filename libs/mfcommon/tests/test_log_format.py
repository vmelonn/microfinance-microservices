"""
Every log line must be parseable JSON.

The formatter used to interpolate the message straight into a JSON document,
so any message containing a quote, a backslash or a newline produced a line
that would not parse. On one ordinary local run 38 of 59 lines were invalid.

Nothing noticed for a long time because nothing read the logs back. The
moment something did, reconstructing a request's path across services for the
architecture doc, two thirds of the evidence was unreadable. These tests
exist so that cannot come back.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from mfcommon.observability.correlation import configure_logging


@pytest.fixture
def captured():
    """A configured logger whose output this test can read back."""
    buffer = io.StringIO()
    logger = configure_logging("test-service", "DEBUG")
    logging.getLogger().handlers[0].stream = buffer

    def lines():
        return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]

    return logger, lines


@pytest.mark.parametrize("message", [
    'a message with "double quotes"',
    r"a message with a \ backslash",
    "a message with\na newline",
    "a message with\ta tab",
    'card={"pan": "4111111111111111"}',
    "unicode: Ayesha Khan, +92 300 1234567",
    "control chars: \x01\x02",
    "",
])
def test_any_message_still_produces_valid_json(captured, message):
    """
    Parametrised over the characters that break naive string interpolation.
    The JSON payload case is the one that actually occurred: trace.emit
    embeds a detail object in its message.
    """
    logger, lines = captured
    logger.info(message)

    records = lines()          # json.loads raises here if the line is broken
    assert len(records) == 1
    assert records[0]["message"] == message


def test_the_line_carries_the_fields_an_aggregator_needs(captured):
    logger, lines = captured
    logger.warning("something worth finding")

    record = lines()[0]
    assert record["level"] == "WARNING"
    assert record["service"] == "test-service"
    assert record["logger"] == "test-service"
    assert "correlation_id" in record
    assert record["message"] == "something worth finding"


def test_the_timestamp_has_millisecond_resolution(captured):
    """
    A purchase crosses five services in tens of milliseconds. At one-second
    resolution every line in a flow shares a timestamp and the order has to
    be guessed, which is how two adapter lines were first documented in the
    wrong order.
    """
    logger, lines = captured
    logger.info("one")
    logger.info("two")

    stamps = [r["ts"] for r in lines()]
    assert all("." in s for s in stamps), stamps
    # yyyy-mm-ddThh:mm:ss.mmm+zzzz
    fractional = stamps[0].split(".")[1]
    assert fractional[:3].isdigit(), stamps[0]


def test_the_timestamp_keeps_its_utc_offset(captured):
    """Logs from several hosts end up in one place, so the offset matters.
    It is appended after the milliseconds because strftime formats the whole
    timestamp in one pass and %z would land in the middle."""
    logger, lines = captured
    logger.info("offset please")

    ts = lines()[0]["ts"]
    assert ts[-5] in "+-", ts
    assert ts[-4:].isdigit(), ts


def test_a_traceback_is_a_field_not_a_mangled_message(captured):
    """
    A traceback is multi-line. Pasted into a single-line record it turned one
    error into several lines that no parser could read, which is the worst
    possible time to lose your logs.
    """
    logger, lines = captured
    try:
        raise ValueError('boom "with quotes"')
    except ValueError:
        logger.exception("the saga failed")

    records = lines()
    assert len(records) == 1, "the traceback split the record across lines"
    assert records[0]["message"] == "the saga failed"
    assert "ValueError" in records[0]["exception"]
    assert "\n" in records[0]["exception"], "the traceback lost its structure"


def test_trace_emit_is_readable_without_redis(captured):
    """
    trace.emit used to return early when Redis was absent, so every trace
    point vanished in exactly the environments that have no Redis: local
    runs, CI, and any deployment where the cache is the thing that broke.
    """
    from mfcommon.observability import trace

    logger, lines = captured
    trace.emit("saga", "RRN generated", {"rrn": "178660003892"})

    records = lines()
    assert len(records) == 1, "trace.emit produced nothing without Redis"
    assert "stage=saga" in records[0]["message"]
    assert "178660003892" in records[0]["message"]
