"""Session title resolution. All fixtures are synthetic."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_dock.core.session_identity import SessionRegistry, TitleResolver, fallback_title

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "66666666-7777-8888-9999-000000000000"


def jl(*records: dict) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


def user(i: int) -> dict:
    return {"type": "user", "sessionId": SID, "message": {"role": "user", "content": f"msg {i}"}}


@pytest.fixture
def roots(tmp_path: Path):
    claude = tmp_path / "claude" / "projects"
    (claude / "-tmp-proj").mkdir(parents=True)
    codex = tmp_path / "codex"
    codex.mkdir()
    return claude, codex


def make_resolver(roots, **kw) -> TitleResolver:
    claude, codex = roots
    return TitleResolver(claude_roots=[claude], codex_home=codex, **kw)


def transcript(roots, sid: str = SID) -> Path:
    return roots[0] / "-tmp-proj" / f"{sid}.jsonl"


def append(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
    # Make sure (mtime, size) changes even on coarse-mtime filesystems.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


# -- fallback ---------------------------------------------------------------


def test_fallback_title_variants():
    assert fallback_title("claude", SID, "/Users/x/macro-tracker/", "Bash") == (
        "macro-tracker · Bash approval",
        "fallback",
    )
    assert fallback_title("claude", "abcd1234a9f2", None, "Bash") == (
        "Claude session · …a9f2",
        "fallback",
    )
    assert fallback_title("codex", "xyz-77b1", None, None)[0] == "Codex session · …77b1"


# -- Claude ------------------------------------------------------------------


def test_claude_custom_title_beats_generated(roots):
    t = transcript(roots)
    t.write_text(
        jl(
            user(1),
            {"type": "ai-title", "aiTitle": "Generated name", "sessionId": SID},
            user(2),
        )
    )
    r = make_resolver(roots)
    assert r.resolve("claude", SID, str(t)) == ("Generated name", "transcript")

    append(t, jl({"type": "custom-title", "customTitle": "My rename", "sessionId": SID}))
    # A later generated title must not override an explicit rename.
    append(t, jl({"type": "ai-title", "aiTitle": "Newer generated", "sessionId": SID}))
    assert r.resolve("claude", SID, str(t)) == ("My rename", "transcript")

    append(t, jl({"type": "custom-title", "customTitle": "Second rename", "sessionId": SID}))
    assert r.resolve("claude", SID, str(t)) == ("Second rename", "transcript")


def test_claude_legacy_summary_and_title_cleanup(roots):
    t = transcript(roots)
    t.write_text(jl({"type": "summary", "summary": "  Old\tsummary\x1b[31m  ", "leafUuid": "u"}))
    title, source = make_resolver(roots).resolve("claude", SID, str(t))
    assert source == "transcript"
    assert title is not None and "\x1b" not in title and title.startswith("Old summary")


def test_claude_long_title_truncated_and_blank_ignored(roots):
    t = transcript(roots)
    t.write_text(
        jl(
            {"type": "ai-title", "aiTitle": "x" * 500, "sessionId": SID},
            {"type": "custom-title", "customTitle": "   ", "sessionId": SID},
        )
    )
    title, _ = make_resolver(roots).resolve("claude", SID, str(t))
    assert title is not None and len(title) == 120


def test_claude_records_for_other_session_ignored(roots):
    t = transcript(roots)
    t.write_text(jl({"type": "custom-title", "customTitle": "Other", "sessionId": SID2}))
    assert make_resolver(roots).resolve("claude", SID, str(t)) == (None, "none")


def test_claude_locates_transcript_without_path(roots):
    t = transcript(roots)
    t.write_text(jl({"type": "custom-title", "customTitle": "Found it", "sessionId": SID}))
    assert make_resolver(roots).resolve("claude", SID, None) == ("Found it", "transcript")


def test_claude_mismatched_basename_ignored(roots, tmp_path):
    wrong = roots[0] / "-tmp-proj" / f"{SID2}.jsonl"
    wrong.write_text(jl({"type": "custom-title", "customTitle": "Wrong", "sessionId": SID}))
    assert make_resolver(roots).resolve("claude", SID, str(wrong)) == (None, "none")


def test_missing_and_inaccessible_files(roots):
    r = make_resolver(roots)
    assert r.resolve("claude", SID, str(transcript(roots))) == (None, "none")
    assert r.resolve("codex", SID, None) == (None, "none")
    t = transcript(roots)
    t.write_text(jl({"type": "custom-title", "customTitle": "Secret", "sessionId": SID}))
    t.chmod(0)
    try:
        if os.access(t, os.R_OK):
            pytest.skip("running as root; chmod does not restrict reads")
        assert r.resolve("claude", SID, str(t)) == (None, "none")
    finally:
        t.chmod(0o600)


def test_unsafe_session_ids_refused(roots):
    r = make_resolver(roots)
    assert r.resolve("claude", "../../etc/passwd", None) == (None, "none")
    assert r.resolve("claude", "", None) == (None, "none")


def test_symlink_outside_root_rejected(roots, tmp_path):
    outside = tmp_path / "outside" / f"{SID}.jsonl"
    outside.parent.mkdir()
    outside.write_text(jl({"type": "custom-title", "customTitle": "Escaped", "sessionId": SID}))
    link = transcript(roots)
    link.symlink_to(outside)
    r = make_resolver(roots)
    assert r.resolve("claude", SID, str(link)) == (None, "none")
    assert r.resolve("claude", SID, None) == (None, "none")
    # A direct path outside the root is refused too.
    assert r.resolve("claude", SID, str(outside)) == (None, "none")


def test_codex_index_symlink_outside_rejected(roots, tmp_path):
    outside = tmp_path / "evil.jsonl"
    outside.write_text(jl({"id": SID, "thread_name": "Escaped", "updated_at": "x"}))
    (roots[1] / "session_index.jsonl").symlink_to(outside)
    assert make_resolver(roots).resolve("codex", SID, None) == (None, "none")


def test_corrupt_and_partial_lines(roots):
    t = transcript(roots)
    with open(t, "wb") as f:
        f.write(b"{not json\n")
        f.write(b"\xff\xfe garbage \x00\n")
        f.write(b"[1, 2, 3]\n")
        f.write(json.dumps({"type": "ai-title", "aiTitle": "Good", "sessionId": SID}).encode())
        f.write(b"\n")
        f.write(b'{"type": "custom-title", "customTitle": "Half wri')  # mid-write
    assert make_resolver(roots).resolve("claude", SID, str(t)) == ("Good", "transcript")


def test_huge_line_skipped(roots):
    t = transcript(roots)
    big = {
        "type": "custom-title",
        "customTitle": "Huge " + "y" * (2 * 1024 * 1024),
        "sessionId": SID,
    }
    t.write_text(jl({"type": "ai-title", "aiTitle": "Small", "sessionId": SID}, big))
    r = make_resolver(roots, tail_bytes=8 * 1024 * 1024)
    assert r.resolve("claude", SID, str(t)) == ("Small", "transcript")


def test_large_file_head_tail_scan(roots):
    t = transcript(roots)
    filler = jl(*(user(i) for i in range(2000)))  # ~150 KB
    t.write_text(
        jl({"type": "ai-title", "aiTitle": "Head title", "sessionId": SID})
        + filler
        + jl({"type": "custom-title", "customTitle": "Lost in middle", "sessionId": SID})
        + filler
    )
    r = make_resolver(roots, tail_bytes=16 * 1024, max_file_bytes=64 * 1024)
    assert t.stat().st_size > 64 * 1024
    # The middle is not scanned; the head's generated title is found.
    assert r.resolve("claude", SID, str(t)) == ("Head title", "transcript")
    append(t, jl({"type": "custom-title", "customTitle": "Tail rename", "sessionId": SID}))
    assert r.resolve("claude", SID, str(t)) == ("Tail rename", "transcript")


def test_cache_hit_does_not_reread(roots, monkeypatch):
    t = transcript(roots)
    t.write_text(jl({"type": "ai-title", "aiTitle": "Cached", "sessionId": SID}))
    r = make_resolver(roots)
    preads = []
    real_pread = os.pread
    monkeypatch.setattr(os, "pread", lambda *a: preads.append(a) or real_pread(*a))
    assert r.resolve("claude", SID, str(t))[0] == "Cached"
    n = len(preads)
    assert n >= 1 and r.reads == 1
    for _ in range(5):
        assert r.resolve("claude", SID, str(t))[0] == "Cached"
    assert len(preads) == n and r.reads == 1
    append(t, jl({"type": "ai-title", "aiTitle": "Changed", "sessionId": SID}))
    assert r.resolve("claude", SID, str(t))[0] == "Changed"
    assert r.reads == 2


# -- Codex -------------------------------------------------------------------


def test_codex_index_last_wins(roots):
    idx = roots[1] / "session_index.jsonl"
    idx.write_text(
        jl(
            {"id": SID, "thread_name": "First", "updated_at": "2026-01-01T00:00:00Z"},
            {"id": SID2, "thread_name": "Other thread", "updated_at": "2026-01-01T00:00:01Z"},
            {"id": SID, "thread_name": "Renamed", "updated_at": "2026-01-01T00:00:02Z"},
            {"id": SID, "thread_name": "  ", "updated_at": "2026-01-01T00:00:03Z"},
        )
        + "{partial"
    )
    r = make_resolver(roots)
    assert r.resolve("codex", SID, None) == ("Renamed", "index")
    assert r.resolve("codex", SID2, None) == ("Other thread", "index")
    assert r.resolve("codex", "unknown-id", None) == (None, "none")


def test_codex_alternate_keys(roots):
    (roots[1] / "session_index.jsonl").write_text(
        jl({"session_id": SID, "title": "Alt keys"}, {"thread_id": SID2, "name": "Alt 2"})
    )
    r = make_resolver(roots)
    assert r.resolve("codex", SID, None) == ("Alt keys", "index")
    assert r.resolve("codex", SID2, None) == ("Alt 2", "index")


def test_codex_oversized_index_head_tail_only(roots):
    idx = roots[1] / "session_index.jsonl"
    filler = jl(*({"id": f"f{i}", "thread_name": f"n{i}"} for i in range(3000)))
    idx.write_text(filler + jl({"id": SID, "thread_name": "Recent"}))
    r = make_resolver(roots, max_file_bytes=32 * 1024, tail_bytes=4 * 1024)
    assert r.resolve("codex", SID, None) == ("Recent", "index")
    assert r.resolve("codex", "f1500", None) == (None, "none")  # middle not scanned


# -- registry ----------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        self.t += 1
        return self.t


async def test_registry_rename_while_pending(roots):
    t = transcript(roots)
    t.write_text(jl({"type": "ai-title", "aiTitle": "Generated", "sessionId": SID}))
    reg = SessionRegistry(make_resolver(roots), clock=Clock())

    ident = reg.ensure("claude", SID, "/work/macro-tracker", "Bash")
    assert ident.display_title == "macro-tracker · Bash approval"
    assert ident.title_source == "fallback"

    changed = await reg.refresh("claude", SID, str(t))
    assert changed is ident and ident.display_title == "Generated"
    assert ident.title_source == "transcript"
    first_ts = ident.title_updated_at

    assert await reg.refresh("claude", SID, str(t)) is None  # no change

    append(t, jl({"type": "custom-title", "customTitle": "Renamed live", "sessionId": SID}))
    changed = await reg.refresh("claude", SID, str(t))
    assert changed is not None and changed.display_title == "Renamed live"
    assert changed.title_updated_at > first_ts
    assert reg.get("claude", SID).display_title == "Renamed live"


async def test_registry_keeps_cached_title_when_source_disappears(roots):
    idx = roots[1] / "session_index.jsonl"
    idx.write_text(jl({"id": SID, "thread_name": "Codex thread"}))
    reg = SessionRegistry(make_resolver(roots))
    reg.ensure("codex", SID, None, "shell")
    assert (await reg.refresh("codex", SID, None)).display_title == "Codex thread"
    idx.unlink()
    assert await reg.refresh("codex", SID, None) is None
    ident = reg.get("codex", SID)
    assert ident.display_title == "Codex thread" and ident.title_source == "cache"


async def test_registry_missing_metadata_keeps_fallback(roots):
    reg = SessionRegistry(make_resolver(roots))
    ident = reg.ensure("claude", "abcdef12a9f2", None, "Bash")
    assert await reg.refresh("claude", "abcdef12a9f2", "/nonexistent/x.jsonl") is None
    assert ident.display_title == "Claude session · …a9f2"
    assert ident.title_source == "fallback"


async def test_registry_refresh_never_raises():
    class Boom(TitleResolver):
        def resolve(self, *a):
            raise OSError("disk on fire")

    reg = SessionRegistry(Boom(claude_roots=[], codex_home=Path("/nonexistent")))
    reg.ensure("claude", SID, None, "Bash")
    assert await reg.refresh("claude", SID, None) is None


async def test_same_title_distinct_identities(roots):
    for sid in (SID, SID2):
        transcript(roots, sid).write_text(
            jl({"type": "custom-title", "customTitle": "Same name", "sessionId": sid})
        )
    (roots[1] / "session_index.jsonl").write_text(jl({"id": SID, "thread_name": "Same name"}))
    reg = SessionRegistry(make_resolver(roots))
    a = reg.ensure("claude", SID, "/p", "Bash")
    b = reg.ensure("claude", SID2, "/p", "Bash")
    c = reg.ensure("codex", SID, "/p", "shell")
    await reg.refresh("claude", SID, str(transcript(roots, SID)))
    await reg.refresh("claude", SID2, str(transcript(roots, SID2)))
    await reg.refresh("codex", SID, None)
    assert a.display_title == b.display_title == c.display_title == "Same name"
    assert len({id(a), id(b), id(c)}) == 3
    assert reg.get("claude", SID) is a and reg.get("claude", SID2) is b
    assert reg.get("codex", SID) is c
    reg.forget("claude", SID)
    assert reg.get("claude", SID) is None and reg.get("claude", SID2) is b


def test_ensure_updates_fallback_with_latest_tool(roots):
    reg = SessionRegistry(make_resolver(roots))
    ident = reg.ensure("claude", SID, "/w/proj", "Bash")
    assert reg.ensure("claude", SID, "/w/proj", "Edit") is ident
    assert ident.display_title == "proj · Edit approval"
