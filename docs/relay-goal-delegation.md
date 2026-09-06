# Relay `/goal` delegation loop

This documents the Claude/Opus → Hermes relay for sandboxed `/goal` work: Claude dispatches an objective, Hermes works in a route-scoped worktree, and a human later harvests finished work through a gated PR.

## 1. One-command dispatch

Dispatch with `relay-goal.py run`, a positional objective, and `--accept`:

```bash
relay-goal.py run \
  "Create docs/relay-goal-delegation.md documenting the relay loop" \
  --accept "docs/relay-goal-delegation.md exists and is committed on relay/work"
```

- Put paths, rails, and the finish/block message contract in the objective.
- Put the concrete completion test in `--accept`; the monitor relies on it.

## 2. Phase-3 per-route worktree sandbox

Every Phase-3 route allocates an isolated git worktree before Hermes runs:

- Branch: `relay/work`; all relayed writes and local commits land there.
- Checkout: route-owned worktree, not the operator's live checkout.
- Webhook: loopback-only, HMAC-signed delivery at `127.0.0.1:8644`.
- Fail closed: if allocation fails, return HTTP `503`, not an unsafe checkout.

This keeps delegated work harvestable without mutating the active repo, default
branch, fork branch, or remote state.

## 3. OPUSHANDS monitor exit codes

The OPUSHANDS monitor watches Discord/the relay channel for the sentinel line:

| Code | Meaning | Operator response |
| ---: | --- | --- |
| `0` | `RELAY-GOAL DONE` observed. | Inspect commits, then harvest if acceptable. |
| `2` | `RELAY-GOAL BLOCKED` observed. | Read the reason; fix context, re-run, or stop. |
| `3` | Timeout: no signal arrived. | Inspect worktree/logs; the agent may be stuck. |
| `4` | Work landed, but no sentinel. | Verify acceptance criteria manually. |
| `5` | Discord read auth failed. | Repair read auth before judging. |

## 4. Intentional re-runs with `--force`

Relay requests are deduplicated by request id for one hour, preventing duplicate work from retries, reconnects, or repeated submissions. Use `--force` only when intentionally re-running the same request id:

```bash
relay-goal.py run "Re-run the same objective" --accept "same criteria" --force
```

## 5. Relayed-agent git rail

The relayed Hermes agent may edit files and commit locally, but only inside the
allocated worktree and only on `relay/work`.

Hard rail:

- Commit to `relay/work` only.
- Never `git push`.
- Never open a pull request.
- Never mutate the operator's live checkout or default/fork branches.

Pushing or opening a PR bypasses the human merge gate and is always a violation.

## 6. Operator harvest and human merge gate

After a successful or manually verified run, preview harvest first:

```bash
relay-goal.py harvest
```

If the dry-run preview is correct, create the harvest artifact:

```bash
relay-goal.py harvest --create
```

`harvest --create` pushes `relay/harvest-<timestamp>` and opens a `needs-human`
PR on the fork. That PR is the human merge gate.

## 7. Kill switch

Disable the relay route immediately with:

```bash
hermes webhook remove relay
```

Use this when the route should stop accepting objectives, auth/signature handling
is suspect, or the sandbox/harvest flow needs maintenance.
