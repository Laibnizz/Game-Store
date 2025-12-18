from __future__ import annotations

import socket
import struct
from typing import Optional

MAX_MSG_SIZE = 65536


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes or raise ConnectionError."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf.extend(chunk)
    return bytes(buf)


def send_message(sock: socket.socket, message: str) -> None:
    """Send a single length-prefixed UTF-8 message on the control channel.

The receiver must call recv_message() to decode the 4-byte length header and payload."""
    data = message.encode("utf-8")
    if not (0 < len(data) <= MAX_MSG_SIZE):
        raise ValueError("message size invalid")
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_message(sock: socket.socket) -> str:
    """Receive a single length-prefixed UTF-8 message from the control channel.

Raises if the peer closes the socket early or if the announced length is invalid."""
    hdr = _recv_exact(sock, 4)
    (length,) = struct.unpack("!I", hdr)
    if length == 0 or length > MAX_MSG_SIZE:
        raise ValueError("invalid message length")
    data = _recv_exact(sock, length)
    return data.decode("utf-8", errors="replace")


def send_raw(sock: socket.socket, data: bytes) -> None:
    """Send raw bytes (used for file transfer sockets)."""
    sock.sendall(data)


def recv_raw_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n raw bytes (used for file transfer sockets)."""
    return _recv_exact(sock, n)


def pick_python() -> str:
    """Return a python executable name likely to work."""
    # Prefer the current interpreter.
    import sys
    return sys.executable or "python3"
