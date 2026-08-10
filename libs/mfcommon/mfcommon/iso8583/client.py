"""
ISO 8583 TCP client: owns the live connection to a switch/host simulator.

This layer knows nothing about *what* a message means -- it only knows how
to get bytes onto a socket and parsed messages back out, keep the
connection alive, and reconnect if it drops. Matching a response to the
request that caused it (STAN-based correlation) is deliberately NOT handled
here -- that's Layer 3's job. This client just hands every parsed message
that arrives to a callback and lets whatever's above it sort out what to
do with it.
"""

import os
import select
import socket
import sys
import threading
import time


def _ts() -> str:
    return time.strftime("%H:%M:%S")

from mfcommon.iso8583.parser import build_message, parse_message
from mfcommon.iso8583.framing import read_framed_message, write_framed_message


class ISO8583Client:
    def __init__(self, host, port, prefix_bytes=2, heartbeat_interval=3, 
                 on_message=None, reconnect_delay=3, audit_logger=None):
        self.host = host
        self.port = port
        self.prefix_bytes = prefix_bytes
        # Updated heartbeat_interval to 3 seconds to prevent the 5-second host timeout limit.
        self.heartbeat_interval = heartbeat_interval
        self.on_message = on_message  # callback(parsed_dict), called for every incoming message
        self.reconnect_delay = reconnect_delay
        self.audit_logger = audit_logger  # Layer 8 -- purely observes, never affects processing

        self._sock = None
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._stan_counter = 0

    # -- connection lifecycle --------------------------------------------------

    def connect(self):
        """Starts the connection loop in the background. Returns immediately."""
        self._stop.clear()
        threading.Thread(target=self._connection_loop, daemon=True).start()

    def close(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._connected.clear()

    def _connection_loop(self):
        """Connects, signs on, runs the receive loop; reconnects on any drop."""
        while not self._stop.is_set():
            try:
                self._sock = socket.create_connection((self.host, self.port))
                print(f"[ISO8583Client {_ts()}] connected to {self.host}:{self.port}")
                self._connected.set()
                self.sign_on()
                print(f"[ISO8583Client {_ts()}] sign-on sent, entering receive loop")
                # Trigger the background heartbeat loop
                threading.Thread(target=self._heartbeat_loop, daemon=True).start()
                self._receive_loop()  # blocks here until the connection drops
            except Exception as e:
                # Catching broadly (not just ConnectionError/OSError) is
                # deliberate: if _receive_loop dies from something we didn't
                # anticipate (a parsing error, for instance), we must still
                # reach the cleanup below -- otherwise self._connected stays
                # True forever even though nothing is actually listening for
                # responses anymore, which is a much worse failure than a
                # normal disconnect: sends keep "succeeding" while every
                # response silently vanishes.
                print(f"[ISO8583Client {_ts()}] connection loop error, will reconnect: {e!r}")
            finally:
                self._connected.clear()
            if self._stop.is_set():
                break
            time.sleep(self.reconnect_delay)  # backoff before trying again

    # -- sending ------------------------------------------------------------------

    def next_stan(self) -> str:
        self._stan_counter = (self._stan_counter + 1) % 1000000
        return f"{self._stan_counter:06d}"

    def send_message(self, mti: str, fields: dict):
        """Builds and sends a message. Does not wait for a response -- see Layer 3."""
        if not self._connected.is_set():
            raise ConnectionError("Not connected to switch")
        raw = build_message(mti, fields)
        with self._send_lock:  # one socket, multiple threads could call send_message
            write_framed_message(self._sock, raw, self.prefix_bytes)
        if self.audit_logger:
            self.audit_logger.log_outbound(mti, fields)

    def sign_on(self):
        self.send_message("0800", {11: self.next_stan(), 70: "001"})

    def sign_off(self):
        self.send_message("0800", {11: self.next_stan(), 70: "002"})

    # -- receiving ------------------------------------------------------------------

    def _receive_loop(self):
        while not self._stop.is_set():
            # Wait (with a timeout) for data to actually be available before
            # reading, rather than putting a timeout on the socket itself --
            # a socket-level timeout could fire in the MIDDLE of a multi-byte
            # read_exact() call, corrupting the byte stream for every message
            # after it. select() only tells us "there's something to read,"
            # then the actual read proceeds fully blocking, safely.
            readable, _, _ = select.select([self._sock], [], [], 1.0)
            if not readable:
                continue  # nothing arrived in this window -- just re-check self._stop
            raw = read_framed_message(self._sock, self.prefix_bytes)
            parsed, _consumed = parse_message(raw)
            if self.audit_logger:
                self.audit_logger.log_inbound(parsed)
            if self.on_message:
                self.on_message(parsed)

    # -- heartbeat ------------------------------------------------------------------

    def _heartbeat_loop(self):
        """Sends a periodic echo test so idle connections don't get silently dropped."""
        while self._connected.is_set() and not self._stop.is_set():
            time.sleep(self.heartbeat_interval)
            if not self._connected.is_set():
                return
            try:
                self.send_message("0800", {11: self.next_stan(), 70: "301"})
            except Exception as e:
                print(f"[ISO8583Client] heartbeat failed, stopping heartbeat loop: {e!r}")
                return