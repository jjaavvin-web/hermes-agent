# Cluster A — Codex session + transports

## Files audited

| Path | Lines | What it does |
|------|-------|-------------|
| `agent/transports/codex_app_server_session.py` | 1–811 | Session adapter: owns one Codex subprocess per Hermes session, drives turn/start, projects events, handles approvals, returns TurnResult |
| `agent/transports/codex_app_server.py` | 1–369 | Wire-level JSON-RPC 2.0 client: spawns `codex app-server`, owns reader threads, exposes blocking queues |
| `agent/transports/codex_event_projector.py` | 1–50+ | Translates codex `item/*` notifications → OpenAI-shaped `{role, content}` entries for Hermes' messages list |
| `agent/transports/__init__.py` | 1–69 | Transport registry (codex_app_server_session is NOT registered here; it is imported directly by run_agent.py) |
| `run_agent.py` | 16050–16168+ | Only live consumer: `AIAgent._run_codex_app_server_turn()`, lazy-creates `_codex_session` per AIAgent instance |

---

## Public API surface (citations)

- **`ensure_started() -> str`** — `codex_app_server_session.py:202–260`
  - Idempotent: early-returns `self._thread_id` if already set (line 206–207).
  - If not started: instantiates `CodexAppServerClient` (spawns subprocess), calls `initialize` handshake, then `thread/start`.
  - Returns the codex thread id (string). Tolerates multiple key names in the response: `thread.id`, `thread.sessionId`, `result.sessionId`, `result.threadId` (lines 239–244) — cross-version tolerance.
  - Raises `CodexAppServerError` or `TimeoutError` on failure (caught by `run_turn`).
  - `thread/start` timeout: 15 s (line 233).

- **`run_turn(user_input, *, turn_timeout=600, notification_poll_timeout=0.25, post_tool_quiet_timeout=90) -> TurnResult`** — `codex_app_server_session.py:328–578`
  - Calls `ensure_started()` internally; startup failure returns `TurnResult(error=..., should_retire=True)` without raising (lines 352–361).
  - Sends `turn/start` (10 s timeout, line 376–378), then polls notification + server-request queues in a loop until `turn/completed`, deadline, interrupt, or subprocess death.
  - Sets `result.should_retire = True` on: startup failure, `turn/start` timeout, subprocess death mid-turn, post-tool watchdog trip, turn deadline, OAuth failure.
  - Returns `TurnResult` always (never raises). Caller must check `.error` and `.should_retire`.

- **`close()`** — `codex_app_server_session.py:262–272`
  - Idempotent via `self._closed` flag (line 263–264).
  - Calls `self._client.close()` (terminate → wait → kill if needed; `codex_app_server.py:136–154`), nulls `self._client` and `self._thread_id`.
  - Context manager (`__enter__`/`__exit__`) delegates to `close()` (lines 274–278).

- **`request_interrupt()`** — `codex_app_server_session.py:282–285`
  - Sets `self._interrupt_event`; the `run_turn` loop checks it each iteration and issues `turn/interrupt` RPC.

---

## Subprocess lifecycle

- **Spawn**: `CodexAppServerClient.__init__` at `codex_app_server.py:86–93`. Command: `[codex_bin, "app-server"]`. `stdin/stdout/stderr` all piped, `bufsize=0`. `RUST_LOG` defaults to `warn`; `CODEX_HOME` injected if set.
- **Reader threads**: two daemon threads started at init — `_read_stdout` (line 104–105) and `_read_stderr` (lines 106–107). Both are daemon threads; they die with the parent process.
- **PID stability**: no PID is stored anywhere. `is_alive()` calls `self._proc.poll() is None` (line 240). There is no PID-based tracking, no PID file, no resurrection.
- **Subprocess death mid-turn**: detected each iteration via `self._client.is_alive()` check at `codex_app_server_session.py:425–436`. Sets `result.error` + `result.should_retire = True`, breaks loop. Does NOT raise.
- **Reattach by PID**: NOT supported. No code path accepts an existing PID or file descriptor. A new subprocess must be spawned from scratch.

---

## Concurrency story

- **Session-level**: `CodexAppServerSession` docstring explicitly states "Not thread-safe — one caller drives it at a time" (`codex_app_server_session.py:158–160`). No lock guards `run_turn`, projector state, or `_pending_file_changes`.
- **Client-level**: `CodexAppServerClient` is designed for one caller thread + two internal reader daemon threads. `_pending` dict is guarded by `_pending_lock` (`codex_app_server.py:95, 174, 295`). Notification/server-request queues are thread-safe (`queue.Queue`).
- **N sessions coexisting**: each `AIAgent` instance gets its own `_codex_session` attribute (run_agent.py:16065–16078). No shared client, no shared queue, no shared state between sessions. N sessions = N subprocesses. Nothing shared except process-level environment.
- **`run_turn` thread safety**: not safe; designed for single-threaded use. The interrupt event (`threading.Event`) is the only thread-safe hook (line 191).

---

## Resume semantics

**No resume is supported. This is load-bearing for the stateless-bot design.**

- `_thread_id` is stored in Python object memory only (`codex_app_server_session.py:253`). Not persisted to disk, not written to DB, not stored in any registry.
- `close()` nulls `self._thread_id` (line 272) and kills the subprocess.
- If Hermes restarts, `_codex_session` is `None` (run_agent.py:16065 checks `not hasattr(self, "_codex_session") or self._codex_session is None`). A new subprocess is spawned fresh.
- Codex's own `thread_id` (a UUID the codex server assigns) is not persisted by Hermes. Even if Hermes stored it, there is no RPC method in the observed surface to reattach an existing subprocess by thread ID from a new client.
- The `hermes_tools_mcp_server.py:41` comment describes itself as "Spawned by: CodexAppServerSession.ensure_started()" — confirming the lifecycle is tied to the session object, not to any persistent daemon.

**Conclusion**: Hermes restart = Codex subprocess death + new subprocess on next turn. All in-progress Codex threads are silently abandoned.

---

## Logs and observability

- **Session start**: `logger.info("codex app-server thread started: id=... profile=... cwd=...")` — `codex_app_server_session.py:254–259`. Thread ID is truncated to 8 chars.
- **Session retirement**: `logger.warning("codex app-server session retired (turn error: ...)")` — `run_agent.py:16113–16116`.
- **turn/interrupt non-fatal**: `logger.debug(...)` — `codex_app_server_session.py:593`.
- **turn/interrupt timeout**: `logger.warning(...)` — `codex_app_server_session.py:595`.
- **Unknown server request**: `logger.warning("Unknown codex server request: ...")` — `codex_app_server_session.py:650`.
- **Approval callback exceptions**: `logger.exception(...)` — lines 673–674, 714–715.
- **Unhandled turn exception** (outer catch in run_agent.py): `logger.exception("codex app-server turn failed")` — `run_agent.py:16087`.
- **Codex stderr**: buffered in `_stderr_lines` (capped at 500 lines, `codex_app_server.py:321–322`). Appended to error messages via `_format_error_with_stderr` with `redact_sensitive_text(force=True)` (line 323). Never streamed to the user proactively.
- **No structured tracing / span IDs / turn metrics** are emitted. All observability is via Python logging.

---

## Edge cases the design must handle

| # | Failure mode | Where found | Current handling |
|---|-------------|-------------|-----------------|
| 1 | Subprocess exits mid-turn (crash, SIGKILL, OOM) | `codex_app_server_session.py:425–436` | Detected via `is_alive()` each loop iter; retire + error |
| 2 | `turn/start` hangs (subprocess wedged at handshake) | lines 396–404 | 10 s timeout → `should_retire=True` |
| 3 | Full turn deadline exceeded (600 s default) | lines 565–577 | Sends `turn/interrupt`; retire |
| 4 | Post-tool quiet timeout (codex silent 90 s after tool result) | lines 440–454 | Sends `turn/interrupt`; retire |
| 5 | OAuth / token refresh failure | `_classify_oauth_failure` lines 120–140 | Detected in stderr + RPC errors; rewritten to `codex login` hint; retire |
| 6 | `<turn_aborted>` marker in stream (no turn/completed emitted) | lines 532–537, 477–483 | Treated as terminal; `result.interrupted = True` |
| 7 | Unknown server-initiated request method | lines 648–653 | `respond_error(-32601)`; warns; does NOT retire session |
| 8 | `mcpServer/elicitation/request` from non-hermes-tools server | lines 636–646 | Auto-declined |
| 9 | Permission escalation request mid-turn | lines 621–626 | Always declined |
| 10 | `apply_patch` approval arrives before `item/started` (no change summary) | lines 756–766 | Gracefully returns None; approval prompt falls back to reason string |
| 11 | Non-JSON on codex stdout | `codex_app_server.py:277–284` | Appended to stderr buffer; not fatal |
| 12 | BrokenPipeError on write (subprocess already dead) | `codex_app_server.py:260–263` | Raises RuntimeError; NOT caught by `run_turn` directly — would propagate to run_agent.py outer except |
| 13 | `close()` called while `run_turn` is blocking | Not handled — `close()` sets `_closed` but does not signal the turn loop or interrupt the RPC |
| 14 | Hermes restart / process death | No state persisted; Codex subprocess orphaned in OS, no cleanup |

---

## Existing consumers

| File | Line | Role |
|------|------|------|
| `run_agent.py` | 16060–16121 | Only production consumer. `AIAgent._run_codex_app_server_turn()` — lazy-creates one session per AIAgent instance, retires on `should_retire`, splices `projected_messages` into conversation |
| `tests/agent/transports/test_codex_app_server_session.py` | 17, 107–108 | Unit tests with `FakeClient` injection via `client_factory` |
| `tests/run_agent/test_codex_app_server_integration.py` | 20, 46, 48, 168, 170, 312, 314, 335, 337, 369–372, 406–409 | Integration tests — monkeypatches `ensure_started`, `run_turn`, `close` on AIAgent path |
| `agent/transports/hermes_tools_mcp_server.py` | 41 (comment only) | Documents that it is spawned by `ensure_started()`; not a direct import |

---

## Open questions for the queen

- **Orphaned subprocesses**: when Hermes dies (SIGKILL, OOM), the codex subprocess is orphaned. Is there a systemd/supervisor teardown hook, or do these accumulate?
- **`close()` vs active `run_turn()`**: no lock or signal coordination between them. If a background thread calls `close()` while `run_turn` is mid-loop, `self._client` becomes None mid-iteration. Is this reachable in production?
- **Session per AIAgent vs session per conversation**: `_codex_session` is on the `AIAgent` instance. If AIAgent is reused across multiple user conversations (pooling), does the old codex thread carry state across users?
- **`thread/start` permissions commented out**: `ensure_started()` explicitly does NOT send a permissions profile (lines 217–230, with detailed justification). The effective permission is whatever is in `~/.codex/config.toml`. Is this acceptable for multi-tenant or sandboxed deployments?
- **`_STDERR_TAIL_LINES = 12` vs `stderr_tail(40)` calls**: error paths call `stderr_tail(40)` directly (lines 382, 399, 427) but the `_format_error_with_stderr` default is 12. Inconsistency — intentional?
- **No structured turn IDs in logs**: `turn_id` is tracked in `TurnResult` but never logged. Cross-referencing a wedged turn against codex's own tracing requires matching on timestamp only.
- **`codex_app_server` not registered in transport registry** (`__init__.py`): `get_transport("codex_app_server")` returns None. This is fine if the session path is always invoked directly, but makes the registry misleading.
- **Parallel turns**: design explicitly forbids concurrent `run_turn` on one session. If the stateless-bot design needs to fan out N turns in parallel, each must have its own session (= N subprocesses). Is that the intended model?
