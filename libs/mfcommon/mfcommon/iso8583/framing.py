"""
Message framing for ISO 8583 over TCP.

Real switches don't send raw messages on the wire -- each message is
preceded by an MLI (Message Length Indicator), a fixed-size integer stating
how many bytes follow. TCP is just a continuous stream with no built-in
message boundaries, so this framing is what lets a receiver correctly split
the stream back into individual messages.
"""

import socket


def read_exact(sock: socket.socket, num_bytes: int) -> bytes:
    """
    A single recv() call isn't guaranteed to return all the bytes you asked
    for -- it can return fewer. This loops until exactly num_bytes have been
    read, or raises if the connection closes before that happens.
    """
    chunks = bytearray()
    while len(chunks) < num_bytes:
        chunk = sock.recv(num_bytes - len(chunks))
        if not chunk:
            raise ConnectionError("Socket closed while reading a framed message")
        chunks += chunk
    return bytes(chunks)


def read_framed_message(sock: socket.socket, prefix_bytes: int = 2) -> bytes:
    """Reads one MLI-framed message: the length header, then that many bytes."""
    header = read_exact(sock, prefix_bytes)
    length = int.from_bytes(header, byteorder="big")
    return read_exact(sock, length)


def write_framed_message(sock: socket.socket, message: bytes, prefix_bytes: int = 2) -> None:
    """Writes one MLI-framed message: the length header, then the message bytes."""
    header = len(message).to_bytes(prefix_bytes, byteorder="big")
    sock.sendall(header + message)
