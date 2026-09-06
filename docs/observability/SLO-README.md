# Hermes SLO observability layer

This adds the first SLO-oriented measurement path without Prometheus/Grafana:

1. `scripts/observability/loki_shipper.py` pushes recent gateway journald and Hermes lane/log files into the already-running Loki at `http://127.0.0.1:3100`.
2. `scripts/observability/slo_exporter.py` reads `~/.hermes/state.db` in SQLite `mode=ro`, scans bounded journald, and writes `~/.hermes/observability/slo-latest.json` plus `slo-timeseries.jsonl`.
3. `hermes_cli/dashboard_slo.py` exposes a JSON contract and an HTML panel at `/api/dashboard/slo` and `/api/dashboard/slo/panel` once the dashboard service is later restarted onto this branch.
4. `scripts/observability/slo_alert_check.py` compares latest metrics to `SLO-DEFINITIONS.md` and uses the existing `~/.hermes/scripts/discord-notify.sh` helper. Synthetic proof should be run with `DISCORD_NOTIFY_DRYRUN=1`.

## Manual proof commands

From the repo root:

```bash
PYTHONPATH=. python scripts/observability/loki_shipper.py --loki-url http://127.0.0.1:3100 --limit 50
curl -sS http://127.0.0.1:3100/loki/api/v1/labels
PYTHONPATH=. python scripts/observability/slo_exporter.py --print
PYTHONPATH=. DISCORD_NOTIFY_DRYRUN=1 python scripts/observability/slo_alert_check.py --synthetic-breach --dry-run
```

## Staged systemd units

Unit files are staged under `packaging/systemd/user/` only. They are not installed, daemon-reloaded, enabled, or started by this packet.

After owner approval only:

```bash
install -Dm0644 packaging/systemd/user/hermes-loki-shipper.service ~/.config/systemd/user/hermes-loki-shipper.service
install -Dm0644 packaging/systemd/user/hermes-loki-shipper.timer ~/.config/systemd/user/hermes-loki-shipper.timer
install -Dm0644 packaging/systemd/user/hermes-slo-exporter.service ~/.config/systemd/user/hermes-slo-exporter.service
install -Dm0644 packaging/systemd/user/hermes-slo-exporter.timer ~/.config/systemd/user/hermes-slo-exporter.timer
install -Dm0644 packaging/systemd/user/hermes-slo-alert-check.service ~/.config/systemd/user/hermes-slo-alert-check.service
install -Dm0644 packaging/systemd/user/hermes-slo-alert-check.timer ~/.config/systemd/user/hermes-slo-alert-check.timer
systemctl --user daemon-reload
systemctl --user enable --now hermes-loki-shipper.timer hermes-slo-exporter.timer hermes-slo-alert-check.timer
```

Rollback after approval if needed:

```bash
systemctl --user disable --now hermes-loki-shipper.timer hermes-slo-exporter.timer hermes-slo-alert-check.timer
rm -f ~/.config/systemd/user/hermes-loki-shipper.{service,timer} ~/.config/systemd/user/hermes-slo-exporter.{service,timer} ~/.config/systemd/user/hermes-slo-alert-check.{service,timer}
systemctl --user daemon-reload
```

## Dashboard deployment gate

The route is wired in source, but the live dashboard at `:9119` will not expose it until the dashboard service is intentionally restarted/cut over. That restart is outside this packet.
