# Module Spec — Peer-Review Orchestrator (Opus pane pool over tmux)

**Implements:** `DESIGN.md §6.3` + billing constraint `DESIGN.md §9`
**Phase:** P2 (`isas/P2-peer-review.md`)
**File produced:** `agent/peer_review.py` (new, greenfield)

---

## 1. Purpose & scope

This module is the automated Opus peer reviewer for Codex-session diffs. When a Codex session transitions to `phase: verify` (`ISA-SPEC.md:131-140`), the dispatcher calls `PeerReviewOrchestrator.review()`, which claims a warm tmux pane running interactive `claude`, injects a prompt via `tmux send-keys`, polls `tmux capture-pane -p` for a structured verdict, and returns the result to the dispatcher. The design principle is lineage diversity: Opus 4.7 reading a Codex-produced diff catches what Codex missed during write (`DESIGN.md §2 Decision A`; `PROVIDER-STACK.md:38-40`). The invocation is exclusively interactive Claude (`tmux new-session -d 'claude'`, no `-p`) — this is the load-bearing billing constraint keeping reviewer cycles on Max after 2026-06-15 (`PROVIDER-STACK.md:7-16`; `DESIGN.md §9`). Any implementation that routes through `claude -p`, `claude --print`, or the Agent SDK is a defect and must fail review.

---

## 2. Files created

| Path | Kind | Notes |
|---|---|---|
| `agent/peer_review.py` | new, permanent | Module ≤ 500 lines; split at natural class boundaries if needed |
| `/tmp/review-<sid>.md` | ephemeral, per-review | Written immediately before `send-keys`; deleted on verdict capture or timeout |
| `~/.hermes/codex-review-<i>.log` | per-pane, persistent | `tmux pipe-pane -o` side-channel; rotated at 10 MB or via logrotate |
| `~/.hermes/codex-review-state.json` | per-session/day counters | Atomic write; schema defined in §9 |

---

## 3. Public API

```python
class PeerReviewOrchestrator:
    def __init__(
        self,
        *,
        hermes_home: Path,
        pool_size: int = 2,
        iteration_cap: int = 3,
        daily_cap: int = 10,
        review_timeout_sec: int = 300,
        idle_threshold_sec: int = 15,
    ) -> None:
        """
        hermes_home       — resolves ~/.hermes; all state files written here.
        pool_size         — number of warm Opus panes to maintain. Matches the
                            global concurrent-review cap (collision-matrix.md §4).
        iteration_cap     — max REVISE rounds before auto-ESCALATE (DESIGN §2 Decision A).
        daily_cap         — max reviews per session_id per calendar day; runaway-loop
                            guard, not a billing guard (DESIGN §9).
        review_timeout_sec — hard per-review TTL; pane marked DEAD on breach.
        idle_threshold_sec — seconds of no new pane output after VERDICT: sentinel
                            before capture is considered complete.
        """

    async def start(self) -> None:
        """
        Spawn pool_size detached tmux sessions (codex-review-0 .. codex-review-<N-1>),
        each running interactive claude. Wire pipe-pane logging. Run the
        dialog-clearing loop (ruflo-launch-interactive.template.sh:117-145) on each
        pane. Block until all panes reach WARM state or raise RuntimeError if any
        pane fails to become prompt-ready within 120 s.
        """

    async def stop(self) -> None:
        """
        Mark all panes BUSY (block new requests), drain in-flight reviews to
        completion or timeout, then kill all tmux sessions. Flush
        codex-review-state.json.
        """

    async def review(
        self,
        *,
        session_id: str,
        isa_path: Path,
        diff: str,
    ) -> "Verdict":
        """
        Acquire a free WARM pane (queue if all BUSY; dedup so only one in-flight
        review per session_id at a time). Check daily and iteration caps. Write the
        prompt file, inject via send-keys, poll for verdict, return Verdict.

        Raises nothing — all failure modes return Verdict(kind="ESCALATE").
        """


@dataclass
class Verdict:
    kind: Literal["APPROVE", "REVISE", "ESCALATE"]
    rationale: str          # everything after the VERDICT: line; empty string if none
    iteration: int          # which REVISE round this was (1-indexed); 0 = first review
    raw_capture: str        # full tmux capture-pane output for audit log
    duration_sec: float     # wall time from send-keys to idle-detection
    pane_id: str            # e.g. "codex-review-0"; for log correlation
```

---

## 4. Pane pool lifecycle

```
Pool init (start())
  for i in 0 .. pool_size-1:
    tmux new-session -d -s codex-review-<i> 'claude'
    tmux pipe-pane -o -t codex-review-<i> 'cat >> ~/.hermes/codex-review-<i>.log'
    run dialog-clearing loop (see below) → mark WARM

Pane states:
  WARM ──► (review claimed) ──► BUSY ──► (verdict captured) ──► WARM
                                    └──► (timeout / death)  ──► DEAD ──► (respawn) ──► WARM

Health check (background task, 30 s interval):
  tmux has-session -t codex-review-<i>
  on missing → mark DEAD → respawn via same init sequence

Per-pane usage counter:
  increment on each review; at K=50 reviews, recycle pane after current review
  completes (kill + respawn) to prevent session context accumulation.
```

**Dialog-clearing loop** (mirrors `ruflo-launch-interactive.template.sh:117-145`):

```
DIALOGS=0; CLEAR_STREAK=0
for _ in 1..24:          # 24 × 5 s = 120 s max
    sleep 5
    if not tmux has-session -t codex-review-<i>: raise RuntimeError
    PANE = tmux capture-pane -p -t codex-review-<i>
    if "Enter to confirm" in PANE:
        tmux send-keys -t codex-review-<i> Enter
        DIALOGS++; CLEAR_STREAK=0
    else:
        CLEAR_STREAK++
        if DIALOGS > 0 and CLEAR_STREAK >= 2: break   # prompt visible, no more dialogs
if DIALOGS == 0: log WARNING "no startup dialogs seen — verify pane is running"
```

Auth resolves from `~/.claude/.credentials.json` at runtime; no token env var is exported (`cluster-D §"Key env vars that must propagate"`).

---

## 5. Review dispatch protocol

1. **Dedup check.** If `session_id` already has an in-flight review, queue the new request behind it (one in-flight + at most one queued per sid).
2. **Daily cap.** Read `codex-review-state.json` for `<sid>.reviews_today` (resetting if `day_started != today`). If `>= daily_cap` (default 10), return `Verdict(kind="ESCALATE", rationale="daily review cap reached", ...)` without claiming a pane.
3. **Iteration cap.** Read `<sid>.iterations`. If `> iteration_cap` (default 3), return `Verdict(kind="ESCALATE", rationale="iteration cap reached", ...)`.
4. **Acquire pane.** Block on a `asyncio.Queue` of WARM pane IDs. FIFO. Claim the pane → mark BUSY.
5. **Compose payload.**
   - `if len(diff) > 20480`: `diff_payload = summarize_diff(diff)` (in-process, deterministic; strip hunks to ±3 context lines, prefix each file with `--- a/` header and line-delta summary; flag as `<truncated>`).
   - `else`: `diff_payload = diff` (flag as `<raw>`).
6. **Write prompt file.** `Path(f"/tmp/review-{session_id}.md").write_text(prompt_body)`. Template in §6.
7. **Mark pane BUSY.**
8. **Inject prompt.** Single `tmux send-keys` call:
   ```
   tmux send-keys -t codex-review-<pane_id> \
     "Review the diff and ISA at /tmp/review-<sid>.md and reply with VERDICT: APPROVE | REVISE | ESCALATE followed by the rationale." Enter
   ```
   One line only — multiline `send-keys` corrupts on `$`, backticks, and newlines. The temp file absorbs all quoting complexity.
9. **Poll loop.** Every 5 s: `tmux capture-pane -p -t codex-review-<pane_id>`. Scan for `VERDICT:` sentinel. On first match, record `sentinel_ts`. Continue polling. Exit loop when `time.monotonic() - sentinel_ts >= idle_threshold_sec` (15 s) with no new output.
10. **Hard timeout.** If 5 min elapse from step 8 without exiting the poll loop: mark pane DEAD, respawn (background), return `Verdict(kind="ESCALATE", rationale="review timeout", ...)`.
11. **Parse verdict.** Extract from `raw_capture` using regex `r'[*#\s]*VERDICT:\s+(APPROVE|REVISE|ESCALATE)\b'` (multiline; tolerates leading `**`, `##`, or whitespace — Opus 4.7 frequently emits `**VERDICT: APPROVE**` or `## VERDICT: REVISE`). Take the **last** matching line (claude's final answer). Rationale = full text after the verdict line to end of capture. Failure modes in §7.
12. **Mark pane WARM.** Return pane ID to queue.
13. **Persist state.** Atomic write to `codex-review-state.json`: increment `iterations` and `reviews_today`, set `last_verdict`, `last_review_at`.
14. **Cleanup.** Delete `/tmp/review-<sid>.md`.
15. **Return** `Verdict`.

---

## 6. Prompt serialization

**Why one-liner + file:** `tmux send-keys` corrupts multi-line input on `$`, backtick substitution, and shell quoting of embedded newlines. A temp file avoids all of this and makes the full prompt auditable independently of the pane log.

**Prompt template** — verbatim content written to `/tmp/review-<sid>.md`:

```
You are reviewing diff produced by a Codex session for ISA at <isa_path>.
Lineage: Codex wrote the code; you (Opus 4.7) are checking what Codex missed.

ISA:
<<<ISA-VERBATIM>>>

Diff (unified, <truncated|raw>):
<<<DIFF-OR-SUMMARY>>>

Required output format (single line, then rationale):
VERDICT: APPROVE | REVISE | ESCALATE
<2-10 lines of rationale citing specific risk or what you'd change>

Rules:
- APPROVE when the diff implements the ISA's [x] ISCs and any open [ ] ISCs
  would be picked up next iteration; no Anti: ISC violated; no obvious
  security/correctness bug.
- REVISE when you can name specific changes the Codex session should make;
  include them as ISC-N references when possible.
- ESCALATE when the diff has a structural problem (wrong abstraction, missing
  files, unhandled error class) that the session probably can't fix in <3
  more turns.
```

`<<<ISA-VERBATIM>>>` is replaced with `isa_path.read_text()`. `<<<DIFF-OR-SUMMARY>>>` is replaced with `diff_payload`. `<isa_path>` is the absolute path string.

---

## 7. Verdict-parse failure modes

| Failure | Detection | Recovery |
|---|---|---|
| Sentinel never appears within 5 min | hard-timeout branch in poll loop | mark pane DEAD, respawn async, return `ESCALATE` |
| Multiple `VERDICT:` lines (model hedged, then committed) | regex finds N > 1 matches | use the **last** match — final answer wins |
| Verdict word misspelled (`APRROVE`, `REVEISE`, etc.) | Levenshtein distance ≤ 1 from any valid keyword | fuzzy-correct silently; log at DEBUG; distance > 1 → `ESCALATE` |
| Model refuses to review (content policy, confusion) | no `VERDICT:` line; refusal text present in capture | `ESCALATE`; `rationale = raw_capture[:500]` |
| Pane exits mid-review (DEAD) | `tmux has-session` returns non-zero | mark DEAD, respawn, return `ESCALATE` |
| Pane output identical across 3 consecutive polls (stuck) | poll loop detects no-change for 15 s pre-sentinel | treat as idle; if no sentinel yet, continue to hard-timeout path |

---

## 8. REVISE feedback loop

On `Verdict(kind="REVISE")` the orchestrator (or caller, dispatcher) must:

1. Call `kanban_comment(task_id=<kanban_card_id>, body=rationale)` with `author=peer-review-opus` (`kanban_tools.py:521`; note `author` is locked to `HERMES_PROFILE` env at the tool layer — set profile accordingly).
2. Append the rationale as a Decisions entry in the ISA at `isa_path`. Use 4-tuple Changelog format (`isa_common.py:70`): `conjectured / refuted by / learned / criterion now`; if rationale does not fit the 4-tuple, write as a free-form sub-bullet under the most recent Decisions heading.
3. Post rationale to the session's Discord thread via `discord_tool.py:908`.
4. Re-enter `phase: execute` on the Codex session.
5. On the 3rd consecutive `REVISE` (i.e., `iteration == iteration_cap`): the next call to `review()` will trip the iteration cap at step 3 of §5 and return `ESCALATE` automatically. No special-case code needed.

```
iteration flow:
  review 1 → REVISE (iteration=1) → re-execute
  review 2 → REVISE (iteration=2) → re-execute
  review 3 → REVISE (iteration=3) → re-execute
  review 4 → cap check: iterations(4) > cap(3) → ESCALATE (no pane used)
```

---

## 9. State persistence

**File:** `~/.hermes/codex-review-state.json`

```json
{
  "<session_id>": {
    "iterations": 2,
    "reviews_today": 3,
    "last_verdict": "REVISE",
    "last_review_at": "2026-05-24T19:37:52Z",
    "day_started": "2026-05-24"
  }
}
```

**Schema notes:**
- `iterations`: total REVISE rounds for this sid, lifetime. Compared against `iteration_cap`.
- `reviews_today`: total reviews attempted today (any verdict). Resets to 0 when `day_started != date.today().isoformat()`.
- `day_started`: the calendar date when `reviews_today` was last reset. Check at every read; reset + update before incrementing if stale.

**Write protocol:** `flock` on `~/.hermes/codex-review-state.json.lock` → read current JSON → mutate → write to `~/.hermes/codex-review-state.json.tmp` → `os.rename()` (atomic on Linux). Release flock. Never write directly to the target path.

---

## 10. Cost / quota

- **No per-review billing.** Interactive `claude` on Max OAuth incurs no per-token charge post-2026-06-15 (`PROVIDER-STACK.md:7-16`; `DESIGN.md §9`).
- **Daily cap = 10** is a runaway-loop guard. A misbehaving Codex session that loops `phase: verify → REVISE → phase: verify` would otherwise exhaust reviewer bandwidth. Not a billing guard.
- **Pool size = 2** limits Claude Code interactive context-switch overhead on the single WSL2 host, not API cost.
- **h2reviewer is not used.** `PROVIDER-STACK.md:76-79` notes h2reviewer misroutes to the paid API. No path in this module touches h2reviewer.

---

## 11. Error modes

| Error | Trigger | Behaviour |
|---|---|---|
| All panes DEAD simultaneously | host killed tmux sessions externally | orchestrator pauses `review()` dispatch (holds queued requests); respawns panes serially (not in parallel — avoid dialog storm); resumes queue after first WARM pane is ready |
| Pane respawn fails (claude binary missing / OAuth invalid) | `tmux new-session` exits non-zero | log CRITICAL; all pending reviews return `ESCALATE`; `stop()` is called |
| Session deleted while review in flight | dispatcher deletes `codex_sessions.json` row mid-review | poll loop completes normally; `Verdict` is returned but caller discards it; pane marked WARM; no state written for the deleted sid |
| Prompt file write fails (`/tmp` full) | `OSError` on `Path.write_text()` | return `ESCALATE` immediately; log ERROR; pane remains WARM (not consumed) |
| `codex-review-state.json` flock timeout (> 5 s wait) | another process holds the lock | log WARNING; proceed with in-memory state for this review; best-effort flush on next successful acquire |
| `isa_path` not found | `FileNotFoundError` on `isa_path.read_text()` | return `ESCALATE(rationale="ISA not found at <path>")` |
| Diff > 20 KB and `summarize_diff` raises | internal error in summarizer | fall back to raw diff truncated at 20 KB with a `[SUMMARIZER ERROR — raw truncated]` header; continue |

---

## 12. Edge cases

| Scenario | Handling |
|---|---|
| All panes DEAD simultaneously | orchestrator respawns serially; queued reviews wait; no escalation cascade (callers hold their queue position) |
| Session deleted while review in flight | review completes; caller discards verdict; pane returns to WARM; no state entry written |
| New day mid-review | daily counter check runs at step 2 of §5 before every review; if `day_started != today`, resets `reviews_today=0` and `day_started=today` atomically in the state file before incrementing |
| Pane hits recycle threshold (K=50) during BUSY | do not interrupt; let current review complete; after verdict is returned and pane marked WARM, kill + respawn before returning it to the queue |
| `pool_size=1` configured (minimal deploy) | all reviews serialize; FIFO queue depth can grow; no design change — just latency |
| Bot restart with reviews in flight | panes are tmux sessions; they survive bot restart; on `start()`, detect existing `codex-review-*` tmux sessions via `tmux ls`; if prompt-ready sentinel visible, mark WARM and skip dialog-clearing loop; if not, run dialog loop as normal |

---

## 13. Test strategy

Assertions the execution hive must satisfy before this module ships:

1. **Pool init:** after `start()`, `tmux ls` shows `N` sessions named `codex-review-{0..N-1}`; all pane states are WARM.
2. **Happy path APPROVE:** inject a mock pane that emits `VERDICT: APPROVE\nLooks good.` within 10 s; `review()` returns `Verdict(kind="APPROVE")`; pane returns to WARM.
3. **Happy path REVISE:** mock pane emits `VERDICT: REVISE\nISC-3 not implemented.`; `Verdict(kind="REVISE", rationale="ISC-3 not implemented.")`.
4. **Hard timeout:** mock pane emits nothing for 301 s; `Verdict(kind="ESCALATE")`; pane marked DEAD; respawn triggered.
5. **Daily cap:** call `review()` 10 times for the same sid (mock panes return APPROVE immediately); 11th call returns `ESCALATE` without claiming a pane.
6. **Iteration cap:** set `<sid>.iterations=3` in state; `review()` returns `ESCALATE` without claiming a pane.
7. **Multiple VERDICT lines:** pane output contains two `VERDICT:` lines; the **last** one wins.
8. **Fuzzy verdict match:** pane emits `VERDICT: APRROVE`; corrected to APPROVE; log entry at DEBUG.
9. **All panes DEAD:** kill all tmux sessions externally; `review()` queues; respawn completes; review proceeds.
10. **State persistence:** after a REVISE, read `codex-review-state.json`; verify `iterations` incremented, `last_verdict=REVISE`, `last_review_at` is recent ISO-8601.
11. **Day rollover:** set `day_started` to yesterday in state; call `review()`; verify `reviews_today` resets to 1 and `day_started` updates to today.
12. **Diff > 20 KB:** pass a 25 KB diff; verify the prompt file written to `/tmp/review-<sid>.md` contains `<truncated>` in the diff header and is smaller than 20 KB.
13. **Pane recycle at K=50:** set pane usage counter to 49; complete one review; verify pane is killed and respawned.
14. **Bot restart with existing panes:** create `codex-review-0` externally with claude at prompt; call `start()`; verify module skips dialog-clearing loop and marks pane WARM.
15. **Bold/markdown VERDICT format (test 8):** pane capture contains `**VERDICT: APPROVE**\nLooks good.`; assert `review()` returns `Verdict(kind="APPROVE")` and does NOT time out or ESCALATE. Additional sub-case: pane capture contains `## VERDICT: REVISE\nISC-3 missing.`; assert `Verdict(kind="REVISE", rationale="ISC-3 missing.")`. Both sub-cases must pass with the `r'[*#\s]*VERDICT:\s+(APPROVE|REVISE|ESCALATE)\b'` regex and would fail with the old `^VERDICT:` regex.

---

## 14. Citations

| Claim | Source |
|---|---|
| Invocation pattern: interactive claude, no `-p` | `DESIGN.md §9`; `PROVIDER-STACK.md:7-16`; `ruflo-launch-interactive.template.sh:104` |
| Pool size = 2, iteration cap = 3, daily cap = 10, timeout = 5 min, idle = 15 s, diff threshold = 20 KB | `DESIGN.md §1` (pool); `collision-matrix.md §4` (all caps) |
| tmux primitives: new-session, pipe-pane, capture-pane, send-keys, has-session | `ruflo-launch-interactive.template.sh:104,108,110,134,136` |
| Dialog-clearing bounded loop design | `ruflo-launch-interactive.template.sh:117-145` |
| Max OAuth credential path | `web_server.py:1378`; `dashboard_health.py:30`; `cluster-D §"Where Max OAuth state lives"` |
| Lineage-diversity argument | `DESIGN.md §2 Decision A`; `PROVIDER-STACK.md:38` |
| kanban_comment API | `kanban_tools.py:521` |
| ISA Changelog 4-tuple format | `isa_common.py:70` |
| discord_tool outbound | `discord_tool.py:908` |
| ISA phase: verify | `ISA-SPEC.md:131-140` |
| Pane-storm dedup by session_id | `collision-matrix.md §2` row "Two reviews queued for the same session" |
| h2reviewer misroute warning | `PROVIDER-STACK.md:76-79`; `DESIGN.md §9` table row |
| Atomic write via flock + rename | `collision-matrix.md §2` row "codex_sessions.json write race" (mirrors telegram.py:1077-1133 pattern) |
