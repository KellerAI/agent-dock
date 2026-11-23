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

## PermissionRequest hook — Phase 0 results

### Claude Code 2.1.330 (Linux dev sandbox, `claude -p`, 2026-11-23)

Driven with a real `claude -p` run, `--settings` adding only the Agent Dock hook,
and an auto-answering broker (`scripts`-free harness; see commit history).

| Case | Result |
|---|---|
| Allow once | `{"behavior":"allow"}` → command ran, file created |
| Deny + message | `{"behavior":"deny","message":…}` → command not run, model reported it was blocked |
| Always allow (echo suggestion 0) | Accepted; command ran |
| No broker running | Hook prints nothing within ~0.5 s; agent continues with its native flow (in `-p` mode that means the call is denied, as documented) |

Observed input details:
- `permission_suggestions` for a `touch` in the project directory contained **no `addRules`
  entry**. It contained `addDirectories` (destination `session`) and `setMode` →
  `acceptEdits` (destination `session`). Never assume a command-level rule exists:
  the UI only lists what the agent sent.
- `TERM_PROGRAM` is inherited by the hook process (observed `ghostty`), so "Open terminal"
  can target the right app.
- `transcript_path` and `session_id` are always present; `permission_mode` was `default`.

Still to verify on the Mac (interactive CLI, not `-p`):
- Whether the native permission dialog is shown while the hook is blocked, and what happens
  to it when the hook answers (expected: dialog dismissed/never shown).
- Behaviour when the request times out (45 s) — the native dialog must appear.
- Claude Desktop / IDE extensions: confirm hooks fire.

### Codex

Not installed in the sandbox. From the official docs (developers.openai.com/codex/hooks,
fetched 2026-11-23):
- Hooks are enabled by default (`[features] hooks = false` disables; `codex_hooks` is a
  deprecated alias). No config.toml change is needed.
- User hooks live in `$CODEX_HOME/hooks.json`; **non-managed hooks must be trusted via
  `/hooks`**, and trust is pinned to the hook's hash — changing the command string requires
  re-trust. The installer therefore keeps the command byte-identical across reinstalls.
- Output: only `behavior` allow/deny and `message`. `updatedInput`, `updatedPermissions`
  and `interrupt` fail closed and are never emitted.
- Matcher is a regex; `"*"` matches all tools (`Bash`, `apply_patch`, `mcp__…`).

## macOS UI — smoke test (2026-11-23)

`scripts/smoke_test` passed on the user's Mac: non-activating drop-down, 10 s / 30 s timers,
menu-bar badge and inbox, three concurrent requests, always-allow form, and native fallback
after quitting the app all behaved as specified.

Not yet covered by that run: interactive Claude Code CLI (native dialog while the hook waits,
45 s timeout hand-off), real Codex sessions and `/hooks` trust, full-screen apps / Stage Manager,
multiple displays, VoiceOver.
