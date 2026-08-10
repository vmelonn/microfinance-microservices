"""
Correlation layer: matches responses to the requests that caused them, using
the STAN (DE 11) that Layer 1 already puts in every message. Owns the
timeout -> reversal decision when a response never arrives.

This is the layer that lets everything above it pretend a transaction is a
simple synchronous call. send_and_wait() blocks and returns a result, even
though underneath, the actual exchange over the socket is fully
asynchronous and Layer 2 has no idea which response belongs to which
request -- that bookkeeping happens entirely here.
"""

import threading

from mfcommon.iso8583.reversal import build_reversal_fields


class PendingRequest:
    def __init__(self):
        self.event = threading.Event()
        self.response = None


class TransactionTimeout(Exception):
    """
    Raised when no response arrives before the timeout expires.
    By the time this is raised, a reversal has already been sent --
    the caller doesn't need to (and shouldn't) send one itself.
    """
    pass


class CorrelationManager:
    def __init__(self, client, timeout_seconds=10):
        self.client = client
        self.timeout_seconds = timeout_seconds
        self._pending = {}       # stan -> PendingRequest
        self._lock = threading.Lock()

        # Chain onto whatever on_message the client already had, so nothing
        # else that was listening (e.g. logging) gets silently dropped.
        self._downstream_on_message = client.on_message
        client.on_message = self._handle_incoming

    def _handle_incoming(self, parsed: dict):
        stan = parsed["fields"].get(11)
        pending = None
        if stan is not None:
            with self._lock:
                pending = self._pending.get(stan)

        if pending is not None:
            pending.response = parsed
            pending.event.set()
            return  # matched to a waiting request -- absorbed here, goes no further

        if self._downstream_on_message:
            self._downstream_on_message(parsed)

    def send_and_wait(self, mti: str, fields: dict, timeout: float = None) -> dict:
        """
        Sends a transaction and blocks until its matching response arrives,
        or the timeout expires. On timeout, sends a reversal automatically
        and raises TransactionTimeout -- the caller never has to think
        about STANs, timers, or reversal logic itself.
        """
        stan = self.client.next_stan()
        outgoing_fields = dict(fields)
        outgoing_fields[11] = stan

        pending = PendingRequest()
        with self._lock:
            self._pending[stan] = pending

        try:
            self.client.send_message(mti, outgoing_fields)
            arrived = pending.event.wait(timeout or self.timeout_seconds)
            if not arrived:
                self._send_reversal(mti, stan)
                raise TransactionTimeout(
                    f"No response for STAN {stan} within "
                    f"{timeout or self.timeout_seconds}s -- reversal sent"
                )
            return pending.response
        finally:
            with self._lock:
                self._pending.pop(stan, None)

    def _send_reversal(self, original_mti: str, original_stan: str):
        reversal_stan = self.client.next_stan()
        fields = build_reversal_fields(reversal_stan, original_mti, original_stan)
        self.client.send_message("0400", fields)
