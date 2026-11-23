"""Best-effort peer UID lookup for Unix-domain sockets (Linux SO_PEERCRED, macOS LOCAL_PEERCRED)."""

from __future__ import annotations

import socket
import struct
import sys

# macOS <sys/un.h>: SOL_LOCAL = 0, LOCAL_PEERCRED = 0x001.
# struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]; }
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 0x001
_XUCRED_SIZE = 4 + 4 + 2 + 2 + 16 * 4  # includes alignment padding


def peer_uid(sock: socket.socket) -> int | None:
    try:
        if sys.platform.startswith("linux"):
            so_peercred = getattr(socket, "SO_PEERCRED", 17)
            data = sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", data)
            return uid
        if sys.platform == "darwin":
            data = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
            _version, uid = struct.unpack_from("=II", data, 0)
            return uid
    except (OSError, struct.error):
        return None
    return None
