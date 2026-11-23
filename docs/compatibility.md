# Compatibility notes

## Session titles

Implemented in `src/agent_dock/core/session_identity.py`. Titles are display-only,
never used for routing. Verified 2026-11-23 on the Linux dev sandbox. Re-verify on
the macOS target.

### Claude Code (verified against 2.1.330, `claude --version`)

Hook input carries `session_id` and `transcript_path`
(`~/.claude/projects/<slug>/<session_id>.jsonl`, or under `$CLAUDE_CONFIG_DIR`).
Titles are JSONL records appended to that transcript:

| Record | Shape | Written by |
|---|---|---|
| custom title | `{"type":"custom-title","customTitle":str,"sessionId":str}` | `/rename` and remote/host rename (`saveCustomTitle`) |
| generated title | `{"type":"ai-title","aiTitle":str,"sessionId":str}` | auto title (`saveAiGeneratedTitle`) |
| legacy summary | `{"type":"summary","summary":str,"leafUuid":str}` | older versions. Read as a lowest-priority fallback |
| agent name | `{"type":"agent-name","agentName":str,"sessionId":str}` | not a session title, so it is ignored |

Evidence:
- Searching the 2.1.330 binary turned up these literals: `"custom-title"` (25), `"ai-title"` (12), `"agent-name"` (8), `customTitle` (58), `aiTitle` (39) and `agentName` (278).
- Writer code in the binary: `tF(path,{type:"custom-title",customTitle:o,sessionId:e})` and `tF(Dh(e),{type:"ai-title",aiTitle:o,sessionId:e})`.
- On checkpoint, Claude adds the current values to the end of the transcript again: `if(this.currentSessionTitle)z.push({type:"custom-title",...});if(this.currentSessionAiTitle)z.push({type:"ai-title",...})`. Because of that, scanning only the head and tail of a large transcript still finds the newest values.
- Claude's own display precedence is `e.customTitle??e.aiTitle??e.summary??firstPrompt`. Its reader extracts the newest occurrence of each field from a head/tail read.
- A real local transcript contained `ai-title` records with keys `{type, aiTitle, sessionId}`. It had no `custom-title` records because the session was never renamed. No transcript content was copied into fixtures.

Resolver rules:
- An explicit `customTitle` (latest) wins, then `aiTitle` (latest), then `summary`.
- Records whose `sessionId` differs from the requested id are ignored.
- Files of 2×`tail_bytes` (4 MiB) or less are scanned whole. Larger files are scanned only in the first and last `tail_bytes`.

### Codex (verified against openai/codex `main` @ 70e0099, 2026-11-23)

- File: `$CODEX_HOME/session_index.jsonl` (default `~/.codex`). Source: `codex-rs/rollout/src/session_index.rs`.
- Entry shape: `SessionIndexEntry { id: ThreadId, thread_name: String, updated_at: String }`, with `updated_at` in RFC 3339.
- The file is append-only (`append_thread_name`), and "the most recent entry wins". Readers scan from the end.
- The batch lookup (`find_thread_names_by_ids`) skips blank, whitespace-only names. Agent Dock does the same: the latest non-blank name wins.
- The file can be rewritten in place by `remove_thread_name_entries`, which uses a temp file and a rename under `session_index.jsonl.lock`. That rename changes mtime and size, so the cache reloads.
- Hook `session_id` is `sess.session_id()`, described as "the identity shared by the root thread and all descendant threads". It equals the root thread id in the index.
- Agent Dock also accepts `session_id`/`thread_id` as the id key and `title`/`name` as the title key, in case the format changes.
- The index is scanned whole when it is at most `max_file_bytes` (64 MiB). Above that, only the head and tail are scanned.
- Codex also has SQLite state. It is deliberately not read, because its schema is private.

### Safety

- Only regular files whose realpath is inside the approved roots are read: `<claude_config_dir>/projects` and `$CODEX_HOME`. Symlinks that escape those roots are rejected.
- Files are opened with `O_NOFOLLOW`.
- Session ids must match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`.
- Lines over 1 MiB, invalid JSON, partial last lines and non-UTF-8 bytes are skipped or tolerated.
- Titles are cleaned with `text.one_line(limit=120)`.
- Results are cached per file by `(st_mtime_ns, st_size)`.
