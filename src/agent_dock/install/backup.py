"""Safe file primitives for editing user-owned config files.

lock -> read -> (merge) -> backup -> atomic write. Malformed files are never overwritten.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from .. import paths
from .errors import ConfigError

DEFAULT_MODE = 0o600


def _lock_path(path: Path) -> Path:
    return path.parent / f"{path.name}.agent-dock.lock"


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on a sibling ``<name>.agent-dock.lock`` file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_lock_path(path), os.O_RDWR | os.O_CREAT, DEFAULT_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def backups_dir() -> Path:
    return paths.app_support_dir() / "backups"


def backup(path: Path) -> Path | None:
    """Copy ``path`` (following symlinks) to a timestamped file in the backups dir.

    Returns the backup path, or None if ``path`` does not exist.
    """
    path = Path(path)
    if not path.exists():
        return None
    dest_dir = paths.ensure_private_dir(backups_dir())
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    prefix = path.parent.name.lstrip(".") or "root"
    dest = dest_dir / f"{prefix}-{path.name}.{stamp}.bak"
    n = 1
    while dest.exists():
        dest = dest_dir / f"{prefix}-{path.name}.{stamp}-{n}.bak"
        n += 1
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, DEFAULT_MODE)
    with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
        shutil.copyfileobj(src, out)
    os.chmod(dest, DEFAULT_MODE)
    return dest


def atomic_write_bytes(path: Path, data: bytes, *, default_mode: int = DEFAULT_MODE) -> Path:
    """Atomically replace ``path`` with ``data``.

    A symlink is resolved and its target written, so the link itself survives.
    Existing file mode is preserved; new files get ``default_mode``. Returns the
    path actually written.
    """
    path = Path(path)
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.stat(target).st_mode & 0o7777
    except FileNotFoundError:
        mode = default_mode
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return target


def atomic_write_json(path: Path, data: object) -> Path:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return atomic_write_bytes(path, text.encode("utf-8"))


def read_json_object(path: Path) -> dict:
    """Read a JSON object. Missing/empty file -> {}. Malformed -> ConfigError."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{path} is not valid JSON ({exc}). Fix or move the file and re-run; "
            "Agent Dock will not overwrite it."
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"{path} must contain a JSON object at the top level, found "
            f"{type(data).__name__}. Agent Dock will not overwrite it."
        )
    return data
