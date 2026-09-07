# Hermes SLO definitions (first real measurement layer)

This is intentionally small and operational: 6 SLOs, every one tied to a concrete local data source. The implementation lives in `hermes_cli/observability_slo.py`; scheduled execution is staged but not armed.

| SLO | Target | Warning | Data source | Notes |
|---|---:|---:|---|---|
| Gateway turn p95 latency | `<= 120000 ms` over rolling 24h | `>= 90000 ms` | `~/.hermes/state.db` table `turn_usage.latency_ms`, opened as `file:...state.db?mode=ro` | Measures end-to-end model/tool turn runtime as currently recorded by Hermes. |
| Turn error rate | `<= 5%` over rolling 24h | `>= 2%` | `journalctl --user -u hermes-gateway.service` error/failure/traceback/timeout/crash lines divided by `turn_usage` turn count | Journald is the current error source of truth; no app code path is modified. |
| Fallback-trigger rate | `<= 10%` over rolling 24h | `>= 5%` | Real provider/model fallback lines from `journalctl --user -u hermes-gateway.service`, divided by `turn_usage` turn count | Ordinary retries and auxiliary title-generation fail-closed fallback-chain messages are tracked separately and do not page this SLO. |
| Recall hit-rate | `>= 80%` over rolling 24h | `< 80%` | `~/.hermes/state/learning-index/recall-canary.jsonl` when present | Uses the recall-quality canary target-in-top-k result plus discrimination-gap floor; reports `no_data` instead of fake-green when missing. |
| Watchdog restart count | `0 / 24h` | `>= 1` | `journalctl --user -u hermes-gateway-watchdog.service` start/restart/failure markers | Captures watchdog churn, not gateway business logic. |
| Cost-burn rate | `<= $10 / 24h` | `>= $5 / 24h` | `~/.hermes/state.db` table `turn_usage.estimated_cost_usd`, rolling 24h sum | Current Claude/Max-included flows often record `$0`; this catches metered leakage if it appears. |

## Outputs

- Latest snapshot: `~/.hermes/observability/slo-latest.json`
- Append-only time-series: `~/.hermes/observability/slo-timeseries.jsonl`
- Loki streams pushed by `scripts/observability/loki_shipper.py`:
  - `{job="hermes-gateway", source="journald", unit="hermes-gateway.service"}`
  - `{job="hermes-lane-log", source="file", logfile="gateway.log|agent.log|errors.log"}`
- Dashboard panel route after dashboard code is deployed/restarted: `/api/dashboard/slo/panel`

## Gates preserved

- No write to `state.db`; exporter opens SQLite with `mode=ro`.
- No gateway turn-dispatch app code touched.
- No service restart, no `daemon-reload`, no timer enable/start, no push/merge/PR.
- Discord proof uses `DISCORD_NOTIFY_DRYRUN=1`.
