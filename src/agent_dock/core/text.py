"""Helpers for turning untrusted strings into safe display/log text."""

from __future__ import annotations

import re
import unicodedata

# Strip C0/C1 control chars (except newline/tab) and bidi overrides that can
# make a command render differently from what will execute.
_BIDI = {
    "‪", "‫", "‬", "‭", "‮",
    "⁦", "⁧", "⁨", "⁩", "‎", "‏",
}  # fmt: skip


def sanitize(text: str, *, keep_newlines: bool = True) -> str:
    out = []
    for ch in text:
        if ch in _BIDI:
            out.append("�")
            continue
        if ch in ("\n", "\t"):
            out.append(ch if keep_newlines else " ")
            continue
        if unicodedata.category(ch) == "Cc":
            out.append("�")
            continue
        out.append(ch)
    return "".join(out)


def one_line(text: str, limit: int = 160) -> str:
    t = re.sub(r"\s+", " ", sanitize(text, keep_newlines=False)).strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n… (truncated)", True


_SECRETISH = re.compile(
    r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[=:]\s*|bearer\s+)(\S+)"
)


def redact(text: str) -> str:
    """Best-effort secret masking for anything that might reach a log."""
    return _SECRETISH.sub(lambda m: m.group(1) + "••••", text)
