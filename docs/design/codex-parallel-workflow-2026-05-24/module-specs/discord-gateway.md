# Module Spec — Discord Gateway (Codex Session Dispatcher)

## 1. Purpose & scope

`gateway/codex_session_dispatcher.py` is the routing layer that translates Discord thread events into Codex session lifecycle operations. It sits between the existing `DiscordAdapter` (which normalises raw Discord events into `MessageEvent` objects) and the rest of the Tier-2 pipeline (`WorktreeBroker`, `PeerReviewOrchestrator`, `MergeBroker`). It owns one authoritative record per active session — the row in `~/.hermes/codex_sessions.json` — and drives every state transition from `NEW` through `ARCHIVED`. It does not touch the adapter's send/edit/receive paths; it adds four event-hook registrations and a slash-command surface. The scope is exactly §6.1 of DESIGN.md: extension of the existing adapter, not a new file for the adapter itself.

---

## 2. Files touched / created

**Created:**
- `gateway/codex_session_dispatcher.py` (new)

**Touched — `gateway/platforms/discord.py` (4 hooks only):**

| Hook | Estimated insertion point | Rationale |
|------|--------------------------|-----------|
| Register `on_thread_create` handler | `discord.py:853` — after `self._client.start(token)` connects, in the `on_ready` body | `discord.py:853` is the `_client.start` call; hook goes in the `on_ready` event registered just before it (cluster-B §"Lifecycle: register → bind → run loop → shutdown") |
| Register `on_message` handler to route tracked-thread messages | `discord.py:587` area — alongside `self._dedup = MessageDeduplicator()` init block | Dedup init is the last setup step before the run loop; message handler is registered here (cluster-B, `discord.py:587`) |
| Register `on_thread_update(archived=True)` handler | Same `on_ready` registration block as `on_thread_create`, ~`discord.py:853` | Paired with create so archive is never missed after a restart |
| `on_ready` hook: call `dispatcher.on_bot_restart()` | `discord.py:853` `on_ready` callback body — first line after adapter confirms connection | Ensures reattach runs before any new events are processed |

None of these hooks modify the adapter's `send`, `edit_message`, `get_chat_info`, or `disconnect` paths.

**Data files:**
- `~/.hermes/codex_sessions.json` — NEW. Owns `thread_id ↔ session` mapping. See §5.
- `~/.hermes/codex-ports.json` — NEW. Port broker state (range 50000–50007, one port per session). Managed by `WorktreeBroker`; the dispatcher reads port assignments from it but does not write it directly. See `module-specs/worktree-broker.md` for the write protocol.

**NOT touched:**
- `~/.hermes/discord_threads.json` — production-active, 20-entry flat array of seen message-ID snowflakes written by a predecessor mechanism (cluster-B §"discord_threads.json — actual content vs objective's claim"). `ThreadParticipationTracker` references it at `gateway/platforms/helpers.py:27`. Leave it alone.

---

## 3. Public API

```python
class CodexSessionDispatcher:
    def __init__(
        self,
        *,
        hermes_home: Path,
        worktree_broker,          # WorktreeBroker — module-specs/worktree-broker.md
        peer_review_orchestrator, # PeerReviewOrchestrator — module-specs/peer-review-orchestrator.md
        merge_broker,             # MergeBroker — module-specs/merge-broker.md
        discord_send,             # Callable[[str, str], Awaitable[None]] — wraps discord_tool.py:908
    ) -> None:
        """
        Pre-conditions: hermes_home exists and is writable.
        Post-conditions: codex_sessions.json is loaded (or created empty); dispatcher
          is ready to receive events. No network calls are made in __init__.
        Raises: PermissionError if hermes_home is not writable.
        """

    async def on_thread_create(self, event: MessageEvent) -> None:
        """
        Called when DiscordAdapter emits on_thread_create for a channel the bot
        monitors (DESIGN.md §4 step 2).

        Pre-conditions:
          - event.source.thread_id is non-empty.
          - No existing session row for event.source.thread_id (enforced by flock read).
        Post-conditions:
          - WorktreeBroker.allocate(session_id) has been called; worktree exists.
          - tmux session `codex-sess-<sid>` is running.
          - New row written to codex_sessions.json (flock + atomic rename — §5).
          - Confirmation message posted to the Discord thread via discord_tool.py:908.
          - Session state is CLAIMED (architecture-diagram.md §2 state machine).
        Raises:
          - WorktreeAllocationError: if broker cannot allocate (disk full, 8-session cap
            hit). Posts error banner to thread; does NOT write a session row.
          - TmuxLaunchError: if `tmux new-session` fails. Releases worktree, posts banner.
        Idempotency: if a row for thread_id already exists, logs a warning and returns
          without creating a duplicate. Safe to call twice on reconnect.
        """

    async def on_thread_message(self, event: MessageEvent) -> None:
        """
        Called when DiscordAdapter delivers a message in a thread that has an active
        session row (DESIGN.md §4 step 7).

        Pre-conditions:
          - Session row for event.source.thread_id exists in codex_sessions.json.
          - Session state is EXECUTING (or CLAIMED for turn 0).
        Post-conditions:
          - Message is forwarded to the tmux session via `tmux send-keys` targeting
            `codex-sess-<sid>` (ruflo-launch-interactive.template.sh:134-138 pattern).
          - codex_sessions.json row updated: last_message_at = now (flock + atomic rename).
          - If ISA phase transitions to `verify` during this turn:
              peer_review_orchestrator.review(sid) is enqueued (P2+).
              In P1: dispatcher posts a `/review` prompt to the thread for operator to trigger.
        Raises:
          - SessionNotFoundError: thread_id has no row. Posts "no active session" reply.
          - TmuxDeadError: tmux session is gone. Transitions row to ORPHANED, posts
            "needs revive" banner (§6).
        Idempotency: each message has a Discord message_id; the dispatcher checks
          event.message_id against the session row's last_message_id before forwarding.
          Duplicate delivery (rare but possible on reconnect) is silently dropped.
        """

    async def on_thread_archive(self, event: MessageEvent) -> None:
        """
        Called when DiscordAdapter fires on_thread_update with archived=True
        (DESIGN.md §4 step 12; architecture-diagram.md §2 "Discord thread archived →
        COMPLETE").

        Pre-conditions:
          - event.source.thread_id may or may not have a session row.
        Post-conditions:
          - If a row exists and state is COMPLETE or MERGING: row is removed from
            codex_sessions.json. WorktreeBroker.release(sid) is called. Kanban card
            marked complete via kanban_tools.py:360.
          - If a row exists and state is EXECUTING/VERIFYING/REVIEWING: session is
            treated as operator-aborted. tmux kill-session, worktree released, row
            removed. Kanban card NOT marked complete — left in "interrupted" status for
            operator triage.
          - If no row: no-op.
        Raises: none (all failures logged, not raised; archive is terminal).
        Idempotency: removing a non-existent row is a no-op. Safe to call twice.
        """

    async def on_bot_restart(self) -> list[ReattachResult]:
        """
        Called from the DiscordAdapter on_ready hook immediately after the bot
        reconnects (DESIGN.md §2 Decision C corrected design).

        Pre-conditions: codex_sessions.json is readable (may be empty or missing).
        Post-conditions:
          - `tmux ls -F '#{session_name}'` output is intersected with
            codex_sessions.json rows (§6).
          - LIVE rows: session state confirmed; no action.
          - ORPHANED rows: "needs revive" banner posted to Discord thread; row
            status updated to NEEDS_REVIVE.
          - Returns list of ReattachResult(sid, thread_id, status: "live"|"orphaned").
        Raises: none (partial failures are per-row; logged individually).
        Idempotency: calling twice in the same process lifetime is safe; live rows are
          re-confirmed, NEEDS_REVIVE rows get a duplicate banner (acceptable; rare).
        """

    async def slash_command(self, name: str, ctx: SlashContext) -> SlashResponse:
        """
        Unified entry point for all dispatcher-owned slash commands. Routes by `name`
        to the sub-handlers documented in §4.

        Pre-conditions: ctx.interaction is a valid Discord interaction object.
        Post-conditions: SlashResponse is returned and the interaction is acknowledged.
        Raises: UnknownCommandError if name is not in the registered set.
        Idempotency: commands that mutate state are not idempotent by design; the
          dispatcher defers to the caller to avoid double-submission.
        """
```

`ReattachResult` is a `dataclass(sid: str, thread_id: str, status: str)`.

`SlashContext` and `SlashResponse` are thin wrappers around the `discord.py` `Interaction` type; exact shape follows the existing command-sync pattern at `discord.py:901-926`.

---

## 4. Slash command surface

All commands are registered against the adapter's existing command-sync state (`discord.py:901-926`, constant `_DISCORD_COMMAND_SYNC_STATE_FILENAME` at `discord.py:30`).

### `/spawn`
- **Arguments:** `task: str` (required) — one-line task description; `isa_path: str` (optional) — path to an existing ISA file in `~/.hermes/work/`.
- **Behavior:** Equivalent to `on_thread_create` but operator-invoked from within an existing thread (e.g., the operator wants to spawn a session in a pre-existing thread). Creates a new session row for the current thread_id. If a session already exists, responds with an error.
- **Side effects:** WorktreeBroker.allocate, tmux launch, codex_sessions.json write, confirmation message.
- **Error responses:** "Session already exists for this thread — use `/status` to check it." | "Session cap (8) reached — kill or merge an existing session first."

### `/pause`
- **Arguments:** none (operates on the current thread's session).
- **Behavior:** Sends `tmux send-keys -t codex-sess-<sid> C-c` to interrupt the current turn, then sets the session row's `paused: true`. Subsequent `on_thread_message` calls are dropped (message queued in the row, max 10) until `/resume`.
- **Side effects:** codex_sessions.json updated. No worktree or tmux teardown.
- **Error responses:** "No active session in this thread." | "Session is already paused."

### `/resume`
- **Arguments:** none.
- **Behavior:** Clears `paused: false` in the row. Flushes queued messages (up to 10) into `tmux send-keys` in order.
- **Side effects:** codex_sessions.json updated. Queued messages forwarded.
- **Error responses:** "No active session in this thread." | "Session is not paused."

### `/kill`
- **Arguments:** `confirm: bool` (required, Discord checkbox) — prevents accidental invocation.
- **Behavior:** `tmux kill-session -t codex-sess-<sid>`, WorktreeBroker.release(sid), removes row from codex_sessions.json. Does NOT open a PR. Kanban card left in interrupted state.
- **Side effects:** worktree deleted, tmux session dead, row removed. Irreversible.
- **Error responses:** "No active session in this thread." | "confirm=True required."

### `/status`
- **Arguments:** none.
- **Behavior:** Reads the session row for the current thread_id. Posts a formatted message: `sid`, `state`, `ISA phase`, `progress (N/M ISCs)`, `tmux alive: yes/no`, `last_message_at`. If no row: "No active session in this thread."
- **Side effects:** none (read-only).
- **Error responses:** "No active session in this thread."

### `/handoff-to-ruflo`
- **Arguments:** `summary: str` (required) — one-paragraph description of what Codex completed and what remains for Ruflo.
- **Behavior:** Pauses the session (as `/pause`), posts the summary to the thread, writes a handoff marker file `<worktree>/_ephemeral/handoff-<ts>.md` containing the summary + current ISA state + diff stats. Does not kill the session; operator closes it manually after Ruflo picks up the task.
- **Side effects:** codex_sessions.json row updated (`state: HANDOFF`), handoff file written.
- **Error responses:** "No active session in this thread." | "Summary must not be empty."

---

## 5. Persistence — `~/.hermes/codex_sessions.json`

### Schema

```json
{
  "version": 1,
  "sessions": {
    "<thread_id>": {
      "session_id": "<uuid4>",
      "thread_id": "<discord-snowflake>",
      "channel_id": "<discord-snowflake>",
      "kanban_card_id": "<uuid4-or-int>",
      "worktree_path": "/home/josep/.hermes/codex-wt/<sid>",
      "tmux_session": "codex-sess-<sid-short>",
      "isa_id": "<isa-slug>",
      "isa_path": "/home/josep/.hermes/work/<isa-id>/ISA.md",
      "state": "CLAIMED|EXECUTING|VERIFYING|REVIEWING|MERGING|COMPLETE|ORPHANED|NEEDS_REVIVE|PAUSED|HANDOFF",
      "paused": false,
      "queued_messages": [],
      "last_message_id": "<snowflake-or-null>",
      "last_message_at": "<ISO-8601-or-null>",
      "created_at": "<ISO-8601>",
      "review_round": 0,
      "port": 50000
    }
  }
}
```

`queued_messages` is a list of `{message_id, text, ts}` objects, max 10 entries (oldest dropped on overflow). `port` is the dev-server port allocated by WorktreeBroker from `codex-ports.json`.

### Write protocol

Matches the `atomic_replace` pattern from `telegram.py:1077-1133` and the `atomic_json_write` already imported in `discord.py:49`:

```
1. fcntl.flock(sessions_file, LOCK_EX)
2. read current state
3. apply mutation
4. write to <sessions_file>.tmp (same directory, same filesystem)
5. os.replace(<sessions_file>.tmp, sessions_file)  # atomic on POSIX
6. fcntl.flock(sessions_file, LOCK_UN)
```

Use `utils.atomic_json_write` (already imported at `discord.py:49`) rather than re-implementing.

### Read protocol on bot start

Read inside `__init__`: `flock(LOCK_SH)` → `json.load` → `flock(LOCK_UN)`. If file is absent, start with `{"version": 1, "sessions": {}}` and write it.

### Recovery on missing / corrupt file

Mirror `tests/gateway/test_discord_thread_persistence.py` `test_corrupted_state_file_falls_back_to_empty` pattern:
- `json.JSONDecodeError` or `OSError` on read → log `WARNING: codex_sessions.json unreadable, starting empty` → treat as `{"version": 1, "sessions": {}}`.
- Do not crash the bot. Let `on_bot_restart` classify all tmux sessions as ORPHANED (since no rows exist), which posts "needs revive" banners to any threads it cannot match.

### Schema migration

The `version` key gates migration. On read, if `version < CURRENT_VERSION`, run the appropriate migration function before using the data, then write back with the new version. Document the bump path in a `_MIGRATIONS` dict:

```python
_MIGRATIONS = {
    # (from_version, to_version): migration_fn
    (1, 2): _migrate_v1_to_v2,
}
```

No migration is needed at v1. The pattern is established so a later phase can add fields without breaking existing rows.

---

## 6. Bot-restart / PID-revive flow

On `on_ready` (called whenever the `discord.py` client reconnects — including cold start and reconnect after network drop):

```
Step 1: Read codex_sessions.json → get dict of {thread_id: row}.
        If file missing/corrupt → empty dict (§5 recovery).

Step 2: Run `tmux ls -F '#{session_name}'` (cf. dashboard_health.py:447-458).
        Parse output into set tmux_live = {name for name in output
                                           if name.startswith("codex-sess-")}.

Step 3: For each row in codex_sessions.json:
          expected_tmux = row["tmux_session"]  # e.g. "codex-sess-<sid-short>"
          if expected_tmux in tmux_live:
            → tmux session exists. Now probe whether hermes is actually running:
              pane_pid = tmux display-message -p -t <expected_tmux> '#{pane_pid}'
              hermes_alive = (pgrep -P <pane_pid> hermes → returncode 0)
              if hermes_alive:
                → LIVE: update row["state"] if it was NEEDS_REVIVE → restore prior state.
                  Log INFO: "session <sid> reattached (thread <tid>)".
              else:
                → hermes dead (shell at prompt after OOM/SIGKILL): do NOT classify LIVE.
                  Classify as NEEDS_REVIVE even though tmux is alive.
                  Log WARNING: "session <sid> tmux alive but hermes not running — classifying NEEDS_REVIVE".
                  Post "needs revive" banner to Discord thread (see below).
          else:
            → ORPHANED: set row["state"] = "NEEDS_REVIVE".
              Post "needs revive" banner to Discord thread (see below).

Step 4: Write updated codex_sessions.json (flock + atomic rename).

Step 5: Return list[ReattachResult].
```

ASCII classification diagram:

```
  codex_sessions.json rows
         │
         ├── tmux_session in tmux_live ──► LIVE   (session still running)
         │
         └── tmux_session NOT in tmux_live
                  │
                  ├── worktree_path exists on disk ──► ORPHANED / NEEDS_REVIVE
                  │     (Codex subprocess gone but worktree intact — recoverable)
                  │
                  └── worktree_path missing ──────► ORPHANED / NEEDS_REVIVE
                        (full loss — fresh spawn needed, ISA progress on branch)
```

**NEEDS_REVIVE banner** (posted to the Discord thread via `discord_tool.py:908`):

```
[Session needs revive]
Session <sid> was running when the bot restarted but its tmux session
(codex-sess-<short>) is gone.

Worktree: <worktree_path> [exists / missing]
Last active: <last_message_at>
ISA: <isa_path>

Warning: the old worktree at <worktree_path> may contain uncommitted
source changes. Run `git -C <worktree_path> diff` before reviving to
capture any unsaved work. The /revive command will post a `git diff
--stat` summary before allocating the new worktree, but you can inspect
the full diff now if the worktree is still on disk.

Use /revive to launch a fresh session on the same worktree and branch,
or /kill to discard and free the slot.
```

The `/revive` slash command (added in P5 ISA — `isas/P5-hardening.md`) runs: `tmux new-session -d -s codex-sess-<sid>` in the existing worktree, restarts `hermes` with the same env vars, updates row state to EXECUTING.

---

## 7. Hook points in existing DiscordAdapter

Four hooks, minimum invasive. All additions are in `gateway/platforms/discord.py`:

| # | Hook | Estimated line | What to add |
|---|------|---------------|-------------|
| 1 | `on_thread_create` registration | ~`discord.py:853` (inside `on_ready` callback registered before `_client.start`) | `@client.event async def on_thread_create(thread): await dispatcher.on_thread_create(MessageEvent.from_thread(thread))` |
| 2 | `on_message` routing for tracked threads | ~`discord.py:587` (alongside dedup init at `self._dedup = MessageDeduplicator()`) | In the existing `on_message` handler body: `if dispatcher.is_tracked(message.channel.id): await dispatcher.on_thread_message(event); return` — short-circuits before the normal gateway runner path |
| 3 | `on_thread_update(archived=True)` | ~`discord.py:853` (same `on_ready` block as hook 1) | `@client.event async def on_thread_update(before, after): if after.archived and not before.archived: await dispatcher.on_thread_archive(MessageEvent.from_thread(after))` |
| 4 | `on_ready` restart flow | `discord.py:853` `on_ready` body, first line | `await dispatcher.on_bot_restart()` |

The dispatcher instance is constructed once in the bot startup path (alongside `DiscordAdapter.__init__`) and passed in via the existing dependency-injection pattern the adapter already uses for `session_store` (`base.py:1526-1536`).

---

## 8. Error modes

| Error | Detection | Recovery | Visible to operator |
|-------|-----------|----------|---------------------|
| WorktreeBroker.allocate fails (disk full, cap hit) | Exception from broker on `on_thread_create` | Release partial allocation; post error banner to thread | Discord thread: "Could not allocate session — reason: <msg>" |
| `tmux new-session` fails | Non-zero exit from subprocess | Release worktree; no row written | Discord thread: "tmux launch failed — session not started" |
| tmux session dies mid-session | `TmuxDeadError` on `on_thread_message` or detected in `on_bot_restart` | Row → NEEDS_REVIVE; "needs revive" banner posted | Discord thread: revive banner with `/revive` / `/kill` options |
| codex_sessions.json corrupt | `JSONDecodeError` on read | Fall back to empty dict (§5 recovery) | Bot log: WARNING level; no Discord message (no thread context) |
| Discord rate-limit (429) on outbound send | Raised by `discord.py` library's built-in rate-limiter | Delegate to adapter's existing rate-limit handling (`discord.py` library manages retry-after automatically) | None (transparent retry) |
| discord_tool.py returns DiscordAPIError (403) | Checked return from `discord_core` call | Log error; post fallback text to thread if context allows | Discord thread: "Send failed — check bot permissions" |
| Kanban card not found | Exception from `kanban_tools.py:360` on archive | Log warning; skip kanban update; still clean up worktree + row | Bot log only; no Discord message |
| `on_bot_restart` partial failure (one row fails) | Per-row exception caught | Continue processing remaining rows; log each failure | Bot log: per-row WARNING; "needs revive" banner for that thread |
| Schema version mismatch on read | `row["version"] > CURRENT_VERSION` | Raise `UnsupportedSchemaVersion`; treat file as corrupt | Bot log: ERROR; bot refuses to start until resolved |

---

## 9. Edge cases

**Two threads in the same channel both want a session.**
Allowed. Sessions are keyed by `thread_id`, not `channel_id`. Both get independent rows, worktrees, tmux sessions, and kanban cards. The 8-session cap applies globally (DESIGN.md §1 concurrency target), not per-channel.

**Operator deletes a Discord thread.**
The `discord.py` library fires `on_thread_delete` (not `on_thread_update(archived=True)`). Add a fifth handler — `on_thread_delete` — that calls `on_thread_archive` directly. Semantically equivalent: treat deletion as a terminal archive. This is a one-line addition in the same `on_ready` registration block as hooks 1 and 3.

**Discord rate-limited during outbound send.**
The `discord.py` library (version `2.7.1`, `pyproject.toml:84`) handles 429 / retry-after internally before the error surfaces. The dispatcher delegates entirely; no custom retry logic is needed here (cluster-B §"Rate-limit handling" — the tool path has a gap, but the gateway adapter path does not).

**`codex_sessions.json` schema migration.**
Handled by the `_MIGRATIONS` dict in §5. A version bump adds a migration function and increments `CURRENT_VERSION`. The migration runs on first read after upgrade. If the migration function raises, the bot logs ERROR and treats the file as corrupt (fall back to empty). Operators are expected to back up the file before upgrading (WORKFLOW-LESSONS pattern).

**Sub-agent (child Codex) spawned by a session.**
No new dispatcher row is created. Per DESIGN.md §2 Decision B, sub-agent semantics are option A: the child Codex runs in the same worktree as the parent, driving an `_ephemeral/<feature>.md` ISA slice. The parent session row remains the authoritative entry. The dispatcher sees no new thread event; the child is an implementation detail of the parent's turn. (ISA-SPEC.md §7 confirms sub-agents reconcile back to the parent ISA via `isa_reconcile.py:146-261`.)

---

## 10. Test strategy

Unit tests (mock WorktreeBroker, mock discord_send, in-memory codex_sessions.json):

- `on_thread_create` with a new thread_id writes exactly one row and calls `allocate` once.
- `on_thread_create` with a duplicate thread_id is a no-op (no second row, no second `allocate`).
- `on_thread_message` with a non-tracked thread_id raises `SessionNotFoundError`.
- `on_thread_message` with a paused session queues the message and does not call tmux.
- `on_thread_archive` in state EXECUTING kills tmux and leaves kanban card in interrupted state.
- `on_thread_archive` in state COMPLETE calls kanban_tools.py:360 and removes the row.
- `on_bot_restart` with all rows live returns all `status="live"` and writes no banners.
- `on_bot_restart` with all rows orphaned returns all `status="orphaned"` and calls discord_send once per row.
- `on_bot_restart` with a missing codex_sessions.json starts empty and returns an empty list.
- `on_bot_restart` with a corrupt codex_sessions.json falls back to empty (mirrors `test_corrupted_state_file_falls_back_to_empty`).
- Atomic write: concurrent flock contention resolves without data loss (two writers, one wins).
- Schema migration: v1 file is upgraded to v2 correctly on first read.

Integration assertions (real tmux, mocked Discord):

- Session row survives a simulated bot restart when tmux session is still alive.
- Session row becomes NEEDS_REVIVE when tmux session is killed externally before restart.
- `/pause` + queued messages + `/resume` delivers messages in order.
- `/kill confirm=True` leaves no tmux session and no worktree on disk.

---

## 11. Citations

| Citation | Used in |
|----------|---------|
| `gateway/platforms/discord.py:532` — `class DiscordAdapter` | §1, §2, §7 |
| `gateway/platforms/discord.py:30` — `_DISCORD_COMMAND_SYNC_STATE_FILENAME` | §2, §4 |
| `gateway/platforms/discord.py:49` — `atomic_json_write` import | §5 write protocol |
| `gateway/platforms/discord.py:587` — `self._dedup = MessageDeduplicator()` | §2 hook table, §7 hook 2 |
| `gateway/platforms/discord.py:632` — `_acquire_platform_lock` | §1 (adapter lifecycle) |
| `gateway/platforms/discord.py:853` — `self._client.start(token)` | §2 hook table, §7 hooks 1, 3, 4 |
| `gateway/platforms/discord.py:901-926` — command-sync state | §3, §4 |
| `gateway/platforms/discord.py:2749` — `get_chat_info` | §2 (adapter interface) |
| `gateway/platforms/base.py:916-999` — `MessageEvent` dataclass | §3 method signatures |
| `gateway/platforms/base.py:1526-1536` — `set_message_handler` / `set_session_store` | §7 DI pattern |
| `gateway/platforms/base.py:1538-1545` — `connect` abstract | §1 abstract interface |
| `gateway/platforms/base.py:1547-1550` — `disconnect` abstract | §1 |
| `gateway/platforms/base.py:1552-1572` — `send` abstract | §1 |
| `gateway/platforms/base.py:3612-3621` — `get_chat_info` abstract | §1 |
| `gateway/platforms/helpers.py:27` — `MessageDeduplicator` | §2 NOT-touched note |
| `gateway/platforms/telegram.py:1077-1133` — `atomic_replace` pattern | §5 write protocol |
| `agent/transports/codex_app_server_session.py:202-260` — `ensure_started` | §3 `on_thread_create` rationale |
| `agent/transports/codex_app_server_session.py:262-272` — `close()` nulls `_thread_id` | §1, §6 |
| `agent/transports/codex_app_server_session.py:158-160` — "Not thread-safe" | §6 (one session, one driver) |
| `tools/discord_tool.py:908` — `discord_core` | §3 `discord_send` param, §6 banner delivery |
| `dashboard_health.py:447-458` — `tmux ls` pattern | §6 step 2 |
| `kanban_db.py:1922-1931` — CAS claim | §1 (inherited, not re-implemented) |
| `kanban_tools.py:360` — `kanban_complete` | §3 `on_thread_archive`, §8 error table |
| `kanban_tools.py:521` — `kanban_comment` | §1 (peer-review REVISE path) |
| `ISA-SPEC.md:107-113` — sub-agent option A | §9 sub-agent edge case |
| `ISA-SPEC.md:131-140` — `phase: verify` | §3 `on_thread_message` post-conditions |
| `isa_reconcile.py:146-261` — reconcile tool | §9 sub-agent edge case |
| `ruflo-launch-interactive.template.sh:104-145` — tmux pattern | §3 `on_thread_create`, §6 revive |
| `ruflo-launch-interactive.template.sh:134-138` — `tmux send-keys` | §3 `on_thread_message` |
| `run_agent.py:16050-16168` — `_run_codex_app_server_turn` | §1 (session lifecycle anchor) |
| `memory_manager.py:317-326` — no internal write lock | §1 (namespacing note) |
| `tests/gateway/test_discord_thread_persistence.py` — `test_corrupted_state_file_falls_back_to_empty` | §5 recovery, §10 |
| `pyproject.toml:84` — `discord.py[voice]==2.7.1` | §9 rate-limit edge case |
| DESIGN.md §1 — 8-session cap | §4 `/spawn` error, §9 two-thread edge case |
| DESIGN.md §2 Decision B — sub-agent option A | §9 |
| DESIGN.md §2 Decision C corrected — tmux reattach by name | §6 |
| DESIGN.md §4 — canonical happy path steps 2, 7, 12 | §3 method post-conditions |
| DESIGN.md §6.1 — extension not greenfield | §1 |
| architecture-diagram.md §2 — state machine | §3 method post-conditions, §6 |
| architecture-diagram.md §8 — data placement | §2 data files |
| cluster-B §"Discord tool API" — `discord_tool.py:908` | §3, §6 |
| cluster-B §"Lifecycle: register → bind → run loop → shutdown" | §2 hook 1 |
| cluster-B §"discord_threads.json — actual content" | §2 NOT-touched note, §5 rationale |
| cluster-B §"Rate-limit handling" | §9 rate-limit edge case |
| cluster-A §"Resume semantics" — no PID reattach | §6 |
| cluster-A §"Subprocess lifecycle" — `codex_app_server.py:86-93` | §6 |
