"""Human-readable session titles for permission cards.

Titles are *display-only* metadata. Routing always uses ``(provider, session_id)``
plus the per-request ``request_id``; nothing here may influence a decision.

Sources (see docs/compatibility.md, "Session titles"):

* Claude Code appends title records to the session transcript
  ``<claude_config_dir>/projects/<slug>/<session_id>.jsonl``::

      {"type": "custom-title", "customTitle": "...", "sessionId": "..."}   # /rename
      {"type": "ai-title", "aiTitle": "...", "sessionId": "..."}           # generated
      {"type": "summary", "summary": "...", "leafUuid": "..."}             # legacy

  Claude's own precedence is ``customTitle ?? aiTitle ?? summary``; the latest
  record of each kind wins. Claude re-appends the current titles near the end
  of the file, so a bounded head + tail scan finds renames in large files.

* Codex appends ``{"id", "thread_name", "updated_at"}`` lines to
  ``$CODEX_HOME/session_index.jsonl``; the newest non-empty name for an id wins.

All files are untrusted: we only read regular files whose real path is inside an
approved root, bound how many bytes we read, and tolerate corrupt/partial lines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .. import paths
from .models import Provider, SessionIdentity
from .text import one_line

log = logging.getLogger(__name__)

MiB = 1024 * 1024
TITLE_LIMIT = 120
MAX_LINE_BYTES = 1 * MiB
_CACHE_ENTRIES = 256
_MAX_PROJECT_DIRS = 5000  # bound for the transcript lookup when no path is given

# Session ids are used to build file names; anything else is refused.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Claude record type -> (field, rank). Higher rank wins; within a rank the
# latest occurrence wins.
_CLAUDE_TITLE_RECORDS: dict[str, tuple[str, int]] = {
    "custom-title": ("customTitle", 3),
    "ai-title": ("aiTitle", 2),
    "summary": ("summary", 1),
}
_CLAUDE_NEEDLES = tuple(f'"{t}"'.encode() for t in _CLAUDE_TITLE_RECORDS)

_CODEX_ID_KEYS = ("id", "session_id", "thread_id")
_CODEX_TITLE_KEYS = ("thread_name", "title", "name")

_PROVIDER_LABEL = {"claude": "Claude", "codex": "Codex"}


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = one_line(value, limit=TITLE_LIMIT)
    return cleaned or None


def fallback_title(
    provider: str, session_id: str, cwd: str | None, tool_name: str | None
) -> tuple[str, str]:
    """Title shown before (or instead of) a resolved session title."""
    project = None
    if cwd:
        project = _clean(os.path.basename(os.path.normpath(cwd)))
    if project:
        tool = _clean(tool_name or "")
        return (f"{project} · {tool} approval" if tool else f"{project} · approval"), "fallback"
    label = _PROVIDER_LABEL.get(provider, one_line(provider, limit=20) or "Agent")
    suffix = one_line(session_id or "", limit=64)[-4:]
    return (f"{label} session · …{suffix}" if suffix else f"{label} session"), "fallback"


@dataclass(frozen=True)
class _CacheEntry:
    mtime_ns: int
    size: int
    value: object


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _iter_lines(data: bytes, *, drop_first: bool, drop_last: bool) -> Iterator[bytes]:
    """Split a region into complete lines; partial edge lines are dropped."""
    lines = data.split(b"\n")
    if drop_first and lines:
        lines = lines[1:]
    if drop_last and lines:
        lines = lines[:-1]
    for line in lines:
        if line and len(line) <= MAX_LINE_BYTES:
            yield line


def _parse(line: bytes) -> dict | None:
    try:
        obj = json.loads(line.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return None  # corrupt or partially written line
    return obj if isinstance(obj, dict) else None


class TitleResolver:
    """Blocking, thread-safe title lookup. Call via ``asyncio.to_thread``."""

    def __init__(
        self,
        claude_roots: list[Path] | None = None,
        codex_home: Path | None = None,
        max_file_bytes: int = 64 * MiB,
        tail_bytes: int = 2 * MiB,
    ) -> None:
        roots = (
            claude_roots if claude_roots is not None else [paths.claude_config_dir() / "projects"]
        )
        self._claude_roots = [Path(os.path.realpath(r)) for r in roots]
        home = codex_home if codex_home is not None else paths.codex_home()
        self._codex_home = Path(os.path.realpath(home))
        self.max_file_bytes = max_file_bytes
        self.tail_bytes = tail_bytes
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._located: dict[str, str] = {}  # claude session_id -> transcript realpath
        self.reads = 0  # number of file reads performed (diagnostics/tests)

    # -- public -----------------------------------------------------------

    def resolve(
        self, provider: str, session_id: str, transcript_path: str | None
    ) -> tuple[str | None, str]:
        if not session_id or not _SAFE_ID.match(session_id):
            return None, "none"
        if provider == "claude":
            title = self._resolve_claude(session_id, transcript_path)
            return (title, "transcript") if title else (None, "none")
        if provider == "codex":
            title = self._resolve_codex(session_id)
            return (title, "index") if title else (None, "none")
        return None, "none"

    # -- path safety --------------------------------------------------------

    def _approved(self, candidate: str | os.PathLike[str], roots: list[Path]) -> Path | None:
        """Real path of ``candidate`` if it is a regular file inside a root."""
        try:
            real = Path(os.path.realpath(os.path.expanduser(os.fspath(candidate))))
        except (OSError, ValueError):
            return None
        if not any(_is_within(real, root) for root in roots):
            log.debug("title lookup: refusing path outside approved roots")
            return None
        return real if real.is_file() else None

    # -- Claude -------------------------------------------------------------

    def _locate_claude(self, session_id: str, transcript_path: str | None) -> Path | None:
        if transcript_path:
            base = os.path.basename(transcript_path)
            if base == f"{session_id}.jsonl":
                real = self._approved(transcript_path, self._claude_roots)
                if real is not None:
                    return real
        with self._lock:
            known = self._located.get(session_id)
        if known:
            real = self._approved(known, self._claude_roots)
            if real is not None:
                return real
        name = f"{session_id}.jsonl"
        for root in self._claude_roots:
            try:
                with os.scandir(root) as it:
                    for n, entry in enumerate(it):
                        if n >= _MAX_PROJECT_DIRS:
                            break
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        real = self._approved(os.path.join(entry.path, name), self._claude_roots)
                        if real is not None:
                            with self._lock:
                                self._located[session_id] = str(real)
                            return real
            except OSError:
                continue
        return None

    def _resolve_claude(self, session_id: str, transcript_path: str | None) -> str | None:
        path = self._locate_claude(session_id, transcript_path)
        if path is None:
            return None
        titles = self._cached(path, self._scan_claude, full_scan_limit=2 * self.tail_bytes)
        if not isinstance(titles, dict):
            return None
        return titles.get(session_id) or titles.get(None)

    def _scan_claude(self, regions: list[tuple[bytes, bool, bool]]) -> dict[str | None, str]:
        # Per sessionId: best (rank, title). Records without sessionId (legacy
        # summaries) are kept under None.
        best: dict[str | None, tuple[int, str]] = {}
        for data, drop_first, drop_last in regions:
            for line in _iter_lines(data, drop_first=drop_first, drop_last=drop_last):
                if not any(n in line for n in _CLAUDE_NEEDLES):
                    continue
                rec = _parse(line)
                if rec is None:
                    continue
                spec = _CLAUDE_TITLE_RECORDS.get(rec.get("type"))  # type: ignore[arg-type]
                if spec is None:
                    continue
                field, rank = spec
                title = _clean(rec.get(field))
                if title is None:
                    continue
                sid = rec.get("sessionId")
                key = sid if isinstance(sid, str) else None
                prev = best.get(key)
                if prev is None or rank >= prev[0]:
                    best[key] = (rank, title)
        return {k: v[1] for k, v in best.items()}

    # -- Codex --------------------------------------------------------------

    def _resolve_codex(self, session_id: str) -> str | None:
        path = self._approved(self._codex_home / "session_index.jsonl", [self._codex_home])
        if path is None:
            return None
        names = self._cached(path, self._scan_codex, full_scan_limit=self.max_file_bytes)
        if not isinstance(names, dict):
            return None
        return names.get(session_id)

    def _scan_codex(self, regions: list[tuple[bytes, bool, bool]]) -> dict[str, str]:
        names: dict[str, str] = {}
        for data, drop_first, drop_last in regions:
            for line in _iter_lines(data, drop_first=drop_first, drop_last=drop_last):
                rec = _parse(line)
                if rec is None:
                    continue
                sid = next((rec[k] for k in _CODEX_ID_KEYS if isinstance(rec.get(k), str)), None)
                if not sid:
                    continue
                title = next(
                    (t for k in _CODEX_TITLE_KEYS if (t := _clean(rec.get(k))) is not None), None
                )
                if title is not None:  # Codex ignores blank names; latest non-blank wins
                    names[sid] = title
        return names

    # -- bounded, cached reading -------------------------------------------------

    def _cached(self, path: Path, scan, *, full_scan_limit: int) -> object:
        key = str(path)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            log.debug("title lookup: cannot open transcript/index (%s)", type(exc).__name__)
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return None
            with self._lock:
                hit = self._cache.get(key)
                if hit and hit.mtime_ns == st.st_mtime_ns and hit.size == st.st_size:
                    self._cache.move_to_end(key)
                    return hit.value
            regions = self._read_regions(fd, st.st_size, full_scan_limit)
        finally:
            os.close(fd)
        value = scan(regions)
        with self._lock:
            self._cache[key] = _CacheEntry(st.st_mtime_ns, st.st_size, value)
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return value

    def _read_regions(
        self, fd: int, size: int, full_scan_limit: int
    ) -> list[tuple[bytes, bool, bool]]:
        """Return ``(data, drop_first_line, drop_last_line)`` regions to scan.

        Files within ``full_scan_limit`` (and ``max_file_bytes``) are read whole;
        anything larger only has its head and tail read.
        """
        self.reads += 1
        n = self.tail_bytes
        if size <= min(full_scan_limit, self.max_file_bytes) or size <= 2 * n:
            data = os.pread(fd, size, 0)
            # A last line without "\n" may be mid-write; _parse rejects it if so.
            return [(data, False, False)]
        head = os.pread(fd, n, 0)
        tail = os.pread(fd, n, size - n)
        return [(head, False, True), (tail, True, False)]


class SessionRegistry:
    """Display identity per ``(provider, session_id)``; never used for routing."""

    def __init__(self, resolver: TitleResolver | None = None, *, clock=time.time) -> None:
        self._resolver = resolver if resolver is not None else TitleResolver()
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], SessionIdentity] = {}
        self._seq: dict[tuple[str, str], int] = {}
        self._applied: dict[tuple[str, str], int] = {}

    def get(self, provider: str, session_id: str) -> SessionIdentity | None:
        with self._lock:
            return self._sessions.get((provider, session_id))

    def ensure(
        self, provider: Provider, session_id: str, cwd: str | None, tool_name: str | None
    ) -> SessionIdentity:
        """Return the identity, creating it with a fallback title. Never does IO."""
        key = (provider, session_id)
        with self._lock:
            ident = self._sessions.get(key)
            if ident is None:
                title, source = fallback_title(provider, session_id, cwd, tool_name)
                ident = SessionIdentity(
                    provider=provider,
                    session_id=session_id,
                    display_title=title,
                    title_source=source,
                    title_updated_at=self._clock(),
                    project_path=cwd,
                )
                self._sessions[key] = ident
                return ident
            if cwd and not ident.project_path:
                ident.project_path = cwd
            if ident.title_source == "fallback":
                # Keep the fallback describing the latest request.
                ident.display_title, _ = fallback_title(
                    provider, session_id, ident.project_path, tool_name
                )
            return ident

    async def refresh(
        self, provider: Provider, session_id: str, transcript_path: str | None
    ) -> SessionIdentity | None:
        """Re-resolve the title; return the identity only if its title changed."""
        key = (provider, session_id)
        with self._lock:
            seq = self._seq.get(key, 0) + 1
            self._seq[key] = seq
        try:
            title, source = await asyncio.to_thread(
                self._resolver.resolve, provider, session_id, transcript_path
            )
        except Exception as exc:  # noqa: BLE001 - titles are best-effort
            log.debug("title refresh failed for %s session: %s", provider, type(exc).__name__)
            return None
        with self._lock:
            ident = self._sessions.get(key)
            if ident is None:
                return None  # forgotten meanwhile (or never ensured)
            if seq < self._applied.get(key, 0):
                return None  # a newer refresh already landed
            self._applied[key] = seq
            if title is None:
                if ident.title_source in ("transcript", "index", "hook", "generated"):
                    ident.title_source = "cache"  # keep last known good title
                return None
            if title == ident.display_title:
                ident.title_source = source
                return None
            ident.display_title = title
            ident.title_source = source
            ident.title_updated_at = self._clock()
            return ident

    def forget(self, provider: str, session_id: str) -> None:
        key = (provider, session_id)
        with self._lock:
            self._sessions.pop(key, None)
            self._seq.pop(key, None)
            self._applied.pop(key, None)
