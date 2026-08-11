"""
A fake switch, for testing the client without touching real financial
infrastructure. Accepts connections, reads MLI-framed ISO 8583 messages,
and sends back scripted responses based on MTI:

  0800 (network mgmt)      -> 0810 acknowledged
  0200 (financial request) -> 0210 approved (DE 39 = 00)
  0400 (reversal request)  -> 0410 acknowledged

Anything else gets no response at all, which is exactly how you'd
simulate a silent/timed-out host later, once Layer 3's timeout logic
needs something to time out against.
"""

# Bumped whenever this file changes in a way worth being able to verify at
# a glance, print(HOST_SIMULATOR_BUILD) or check it in a debugger if
# you're ever unsure whether stale bytecode is masking a fix.
HOST_SIMULATOR_BUILD = "2026-08-05-idle-timeout-fix"


def _ts() -> str:
    return time.strftime("%H:%M:%S")

import os
import socket
import sys
import threading
import time


from mfcommon.iso8583.parser import build_message, parse_message
from mfcommon.iso8583.framing import read_framed_message, write_framed_message


class HostSimulator:
    def __init__(self, host="127.0.0.1", port=9000, prefix_bytes=2):
        self.host = host
        self.port = port
        self.prefix_bytes = prefix_bytes
        self._server_sock = None
        self._stop = threading.Event()
        self._accept_thread = None
        self.received = []          # every parsed message the simulator has seen, useful for tests
        self._received_lock = threading.Lock()

    def start(self):
        print(f"[HostSimulator] starting, build: {HOST_SIMULATOR_BUILD}")
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # A just-closed listener on this port can briefly linger in TIME_WAIT
        # even with SO_REUSEADDR, retry a few times rather than fail outright.
        for attempt in range(15):
            try:
                self._server_sock.bind((self.host, self.port))
                break
            except OSError:
                if attempt == 14:
                    raise
                time.sleep(0.3)
        self._server_sock.listen(5)
        self._server_sock.settimeout(0.5)  # lets _accept_loop notice self._stop periodically,
                                             # instead of blocking in accept() forever
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def stop(self):
        self._stop.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        if self._accept_thread:
            self._accept_thread.join(timeout=2)

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._server_sock.accept()
            except socket.timeout:
                continue  # just here to re-check self._stop, not a real error
            except OSError:
                return
            # The accepted connection inherits the listening socket's 0.5s
            # timeout by default, which would make it drop out from under
            # any client that goes more than 0.5s between messages (i.e.
            # every real connection, since heartbeats are tens of seconds
            # apart). Reset it to blocking; only the LISTENING socket needs
            # a short timeout, so accept() can notice shutdown promptly.
            conn.settimeout(None)
            threading.Thread(target=self._handle_connection, args=(conn,), daemon=True).start()

    def _handle_connection(self, conn: socket.socket):
        write_lock = threading.Lock()
        peer = conn.getpeername()
        print(f"[HostSimulator {_ts()}] connection accepted from {peer}")
        try:
            while not self._stop.is_set():
                raw = read_framed_message(conn, self.prefix_bytes)
                parsed, _consumed = parse_message(raw)
                with self._received_lock:
                    self.received.append(parsed)
                # Each message is handled on its own thread, so an
                # artificially slow one doesn't block the others behind it,
                # this is what lets responses genuinely arrive out of order.
                threading.Thread(
                    target=self._respond, args=(conn, parsed, write_lock), daemon=True
                ).start()
        except Exception as e:
            print(f"[HostSimulator {_ts()}] connection to {peer} closing due to: {e!r}")
        finally:
            print(f"[HostSimulator {_ts()}] closing connection to {peer}")
            conn.close()

    def _respond(self, conn: socket.socket, parsed: dict, write_lock: threading.Lock):
        fields = parsed["fields"]
        delay_tag = fields.get(48, "")
        if delay_tag.startswith("DELAY:"):
            try:
                time.sleep(float(delay_tag.split(":", 1)[1]))
            except ValueError:
                pass
        response = self._build_response(parsed)
        if response is not None:
            try:
                with write_lock:
                    write_framed_message(conn, response, self.prefix_bytes)
            except Exception as e:
                print(f"[HostSimulator {_ts()}] failed to write response: {e!r}")

    def _build_response(self, parsed: dict):
        mti = parsed["mti"]
        fields = parsed["fields"]
        stan = fields.get(11, "000000")

        if mti == "0800":
            return build_message("0810", {11: stan, 70: fields.get(70, "001")})

        if mti == "0200":
            if fields.get(48) == "SIMULATE_TIMEOUT":
                return None  # deliberately stay silent, to test Layer 3's timeout/reversal path
            response_fields = {11: stan, 39: "00", 38: "A18008"}
            if 37 in fields:
                response_fields[37] = fields[37]
            return build_message("0210", response_fields)

        if mti == "0400":
            return build_message("0410", {11: stan, 39: "00"})

        return None  # unrecognized MTI: stay silent


if __name__ == "__main__":
    sim = HostSimulator()
    sim.start()
    print(f"Host simulator listening on {sim.host}:{sim.port}, Ctrl+C to stop")
    try:
        while True:
            pass
    except KeyboardInterrupt:
        sim.stop()
