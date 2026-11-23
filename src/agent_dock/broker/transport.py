"""Length-prefixed JSON framing shared by the broker (asyncio) and hook (blocking socket).

Frame = 4-byte big-endian unsigned length + UTF-8 JSON object.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from typing import Any

MAX_FRAME = 256 * 1024
_HEADER = struct.Struct(">I")


class ProtocolError(Exception):
    pass


def encode(msg: dict[str, Any]) -> bytes:
    body = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ProtocolError(f"frame too large ({len(body)} bytes)")
    return _HEADER.pack(len(body)) + body


def _decode(body: bytes) -> dict[str, Any]:
    try:
        msg = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"malformed frame: {exc}") from None
    if not isinstance(msg, dict):
        raise ProtocolError("frame must be a JSON object")
    return msg


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Return the next message, or None on clean EOF."""
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise ProtocolError("truncated header") from None
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME:
        raise ProtocolError(f"frame too large ({length} bytes)")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        raise ProtocolError("truncated frame") from None
    return _decode(body)


async def write_frame(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    writer.write(encode(msg))
    await writer.drain()


# ---- blocking helpers (hook side; avoids asyncio start-up cost) -------------


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if not buf:
                return None
            raise ProtocolError("connection closed mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock: socket.socket) -> dict[str, Any] | None:
    header = _recv_exact(sock, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME:
        raise ProtocolError(f"frame too large ({length} bytes)")
    body = _recv_exact(sock, length)
    if body is None:
        raise ProtocolError("connection closed mid-frame")
    return _decode(body)


def send_frame(sock: socket.socket, msg: dict[str, Any]) -> None:
    sock.sendall(encode(msg))
