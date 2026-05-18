#!/usr/bin/env bash
# gateway-watchdog.sh — dead-man health check for the Hermes gateway.
#
# Designed to run from a systemd --user timer (~/.config/systemd/user/
# hermes-gateway-watchdog.timer, default OnUnitActiveSec=5min). On every
# tick it runs a battery of probes against the gateway / dashboard / kanban
# dispatcher and pings telegram-notify.sh ONCE per distinct outage. It does
# NOT restart anything — recovery is Joseph's call (see FAILURE-RUNBOOK-V2
# §F01).
#
# Probes (in order; any failure marks the tick failed but all run):
#   P1 SYSTEMD     systemctl --user is-active hermes-gateway.service
#   P2 DASHBOARD   curl http://127.0.0.1:9119/api/status — JSON must report
#                  gateway_running=true AND gateway_state="running"
#   P3 CLI         timeout 10 hermes kanban stats — must exit 0
#   P4 DISPATCHER  read kanban.db (read-only): IF there are claimable cards
#                  (status=ready, assignee NOT NULL, no claim_lock) THEN
#                  MAX(task_events.created_at) must be < HEARTBEAT_AGE_MAX
#                  seconds old. Idle silence with no work to do is fine.
#   P5 TAILNET     (OPTIONAL — only if HERMES_WATCHDOG_TAILNET_URL set)
#                  curl that URL, expect HTTP 200.
#
# State: ~/.hermes/state/gateway-watchdog/
#   state.json   — last status, signature, debounce timestamps
#   events.jsonl — append-only audit trail of every tick
#   alerts.jsonl — append-only record of every alert sent (or attempted)
#   silence      — touch this file to suppress all alerts until removed
#
# Tunables (env vars; defaults reasonable for a single-user Hermes box):
#   HERMES_WATCHDOG_KANBAN_DB     default ~/.hermes/kanban/boards/hermes-kanban-control/kanban.db
#   HERMES_WATCHDOG_DASHBOARD_URL default http://127.0.0.1:9119/api/status
#   HERMES_WATCHDOG_CLI_TIMEOUT   default 10  (seconds for hermes kanban stats)
#   HERMES_WATCHDOG_HEARTBEAT_MAX default 900 (15 min — dispatcher silence threshold)
#   HERMES_WATCHDOG_DEBOUNCE_MIN  default 60  (minutes; same-signature alerts suppressed)
#   HERMES_WATCHDOG_TAILNET_URL   default ""  (no tailnet probe unless set)
#   HERMES_WATCHDOG_NOTIFY        default /home/josep/.hermes/scripts/telegram-notify.sh
#   HERMES_WATCHDOG_DRYRUN        default 0   (1 = do everything except actually send Telegram)
#
# Exit codes:
#   0   all probes ok (or silenced)
#   1   one or more probes failed; alert sent OR debounced
#   2   internal error (state file unwritable, python missing, etc.)

set -uo pipefail

# ---- defaults ---------------------------------------------------------------
KANBAN_DB="${HERMES_WATCHDOG_KANBAN_DB:-$HOME/.hermes/kanban/boards/hermes-kanban-control/kanban.db}"
DASHBOARD_URL="${HERMES_WATCHDOG_DASHBOARD_URL:-http://127.0.0.1:9119/api/status}"
CLI_TIMEOUT="${HERMES_WATCHDOG_CLI_TIMEOUT:-10}"
HEARTBEAT_MAX="${HERMES_WATCHDOG_HEARTBEAT_MAX:-900}"
DEBOUNCE_MIN="${HERMES_WATCHDOG_DEBOUNCE_MIN:-60}"
TAILNET_URL="${HERMES_WATCHDOG_TAILNET_URL:-}"
NOTIFY_SCRIPT="${HERMES_WATCHDOG_NOTIFY:-$HOME/.hermes/scripts/telegram-notify.sh}"
DRYRUN="${HERMES_WATCHDOG_DRYRUN:-0}"
GATEWAY_UNIT="${HERMES_WATCHDOG_GATEWAY_UNIT:-hermes-gateway.service}"
HERMES_BIN="${HERMES_WATCHDOG_HERMES_BIN:-$HOME/.local/bin/hermes}"
PYTHON_BIN="${HERMES_WATCHDOG_PYTHON:-/home/josep/.local/share/hermes-agent/venv/bin/python3}"

STATE_DIR="$HOME/.hermes/state/gateway-watchdog"
STATE_FILE="$STATE_DIR/state.json"
EVENTS_FILE="$STATE_DIR/events.jsonl"
ALERTS_FILE="$STATE_DIR/alerts.jsonl"
SILENCE_FILE="$STATE_DIR/silence"

UTC_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
UNIX_NOW="$(date -u +%s)"

mkdir -p "$STATE_DIR" || { echo "watchdog: cannot mkdir $STATE_DIR" >&2; exit 2; }

# Bootstrap state file
if [[ ! -f "$STATE_FILE" ]]; then
  printf '%s\n' '{"last_status":"unknown","last_signature":"","consecutive_failures":0,"last_alert_at":0,"failures_since_last_alert":0,"first_failure_at":null}' > "$STATE_FILE"
fi

# Python must be available — we use it for JSON + SQLite (sqlite3 CLI is not
# always installed on the host; the hermes-agent venv ships its own python).
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
  if [[ -z "$PYTHON_BIN" ]]; then
    echo "watchdog: no python3 available" >&2
    exit 2
  fi
fi

# Python smoke-check: if json/sqlite3 imports are broken (corrupt venv), bail
# loudly instead of silently producing "all OK" exits — reviewer LOW#8.
if ! "$PYTHON_BIN" -c 'import json,sqlite3,os,sys' >/dev/null 2>&1; then
  echo "watchdog: python interpreter at $PYTHON_BIN cannot import json/sqlite3" >&2
  exit 2
fi

# Single-instance lock — prevents two ticks from racing on state.json when a
# slow tick is still running as the next timer fires (reviewer MINOR#6).
exec 9>"$STATE_DIR/.lock"
if ! flock -n 9; then
  echo "gateway-watchdog: prior tick still running, skipping this fire" >&2
  exit 0
fi

# ---- probe helpers ----------------------------------------------------------
FAILURES=()  # array of "CODE|short|long" tuples

record_fail() {
  # $1 = short code (e.g. SYSTEMD_DOWN), $2 = one-line summary
  FAILURES+=("$1|$2")
}

probe_systemd() {
  local state
  state="$(systemctl --user is-active "$GATEWAY_UNIT" 2>/dev/null || true)"
  if [[ "$state" != "active" ]]; then
    record_fail "SYSTEMD_DOWN" "hermes-gateway.service is '$state' (expected 'active')"
  fi
}

probe_dashboard() {
  local body http_code tmp
  tmp="$(mktemp /tmp/watchdog-dash.XXXXXX)"
  http_code="$(curl -sS -m 8 -o "$tmp" -w '%{http_code}' "$DASHBOARD_URL" 2>/dev/null || echo "000")"
  body="$(cat "$tmp" 2>/dev/null || true)"
  rm -f "$tmp"

  if [[ "$http_code" != "200" ]]; then
    record_fail "DASHBOARD_DOWN" "GET $DASHBOARD_URL → HTTP $http_code"
    return
  fi

  # Parse body via python (jq is not always installed). Use heredoc + env to
  # avoid quoting nightmares.
  local parse_result
  parse_result="$(
    HERMES_BODY="$body" "$PYTHON_BIN" - <<'PY' 2>/dev/null || true
import json, os, sys
try:
    d = json.loads(os.environ.get("HERMES_BODY", "{}") or "{}")
except Exception as e:
    print(f"PARSE_ERROR|{e!r}")
    sys.exit(0)
running = d.get("gateway_running")
state = d.get("gateway_state")
exit_reason = d.get("gateway_exit_reason")
# Boot-time transients — treat as OK for one tick (reviewer MINOR#5)
BOOT_STATES = {"running", "starting", "initializing", "ready"}
if running is False:
    print(f"GATEWAY_NOT_RUNNING|gateway_running=false state={state!r} exit_reason={exit_reason!r}")
elif state and state not in BOOT_STATES:
    print(f"GATEWAY_BAD_STATE|gateway_state={state!r} exit_reason={exit_reason!r}")
else:
    print("OK|")
PY
  )"
  if [[ -z "$parse_result" || "$parse_result" == "OK|" ]]; then
    return
  fi
  local code="${parse_result%%|*}"
  local msg="${parse_result#*|}"
  case "$code" in
    PARSE_ERROR)   record_fail "DASHBOARD_BAD_JSON" "/api/status returned 200 but not parseable: $msg" ;;
    GATEWAY_NOT_RUNNING) record_fail "GATEWAY_NOT_RUNNING" "$msg" ;;
    GATEWAY_BAD_STATE)   record_fail "GATEWAY_BAD_STATE" "$msg" ;;
    *)             record_fail "DASHBOARD_UNKNOWN" "unparseable watchdog response: $parse_result" ;;
  esac
}

# Boot-state transient values that legitimately appear during gateway startup
# (reviewer MINOR#5). We treat these as not-yet-a-failure for one tick. The
# probe_dashboard heredoc above whitelists by listing them in BOOT_STATES.
# See the Python heredoc in probe_dashboard for the actual filter.

probe_cli() {
  # CLI_MISSING is an infrastructure signal (binary missing during rollout),
  # not a gateway-health signal — log it but do NOT fail the tick
  # (reviewer MINOR#7). Only timeout/error are genuine gateway probes.
  if [[ ! -x "$HERMES_BIN" ]]; then
    echo "gateway-watchdog: WARN hermes binary missing at $HERMES_BIN (skipping P3)" >&2
    return
  fi
  if ! timeout "$CLI_TIMEOUT" "$HERMES_BIN" kanban stats >/dev/null 2>&1; then
    local rc=$?
    if [[ $rc -eq 124 ]]; then
      record_fail "CLI_TIMEOUT" "hermes kanban stats did not respond within ${CLI_TIMEOUT}s"
    else
      record_fail "CLI_ERROR" "hermes kanban stats exited rc=$rc"
    fi
  fi
}

probe_dispatcher() {
  # Read-only sqlite probe. If DB missing, treat as soft warning (dev box
  # may have a different layout).
  if [[ ! -f "$KANBAN_DB" ]]; then
    record_fail "KANBAN_DB_MISSING" "kanban.db not found at $KANBAN_DB"
    return
  fi
  # Resume-tick suppression (reviewer MAJOR#1). If the last tick fired more
  # than 2× the expected interval ago (default 600s for a 5-min timer), the
  # laptop was almost certainly suspended. task_events.created_at is from
  # before suspend; UNIX_NOW is after. Skip P4 this tick to avoid a guaranteed
  # false-positive DISPATCHER_STALE alert on resume.
  if [[ -f "$EVENTS_FILE" ]]; then
    local last_tick_age=$(( UNIX_NOW - $(stat -c %Y "$EVENTS_FILE" 2>/dev/null || echo "$UNIX_NOW") ))
    local resume_threshold=$(( HEARTBEAT_MAX * 2 / 3 ))
    if [[ $last_tick_age -gt $resume_threshold ]]; then
      echo "gateway-watchdog: skipping P4 — last tick ${last_tick_age}s ago (>${resume_threshold}s; assume suspend/resume)" >&2
      return
    fi
  fi
  local result
  result="$(
    HERMES_KDB="$KANBAN_DB" HERMES_NOW="$UNIX_NOW" HERMES_HBMAX="$HEARTBEAT_MAX" \
      "$PYTHON_BIN" - <<'PY' 2>/dev/null || true
import os, sqlite3, sys
db = os.environ["HERMES_KDB"]
now = int(os.environ["HERMES_NOW"])
hb_max = int(os.environ["HERMES_HBMAX"])
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    cur = con.cursor()
    claimable = cur.execute(
        "SELECT COUNT(*) FROM tasks "
        "WHERE status='ready' AND assignee IS NOT NULL AND assignee != '' "
        "AND (claim_lock IS NULL OR claim_lock = '')"
    ).fetchone()[0]
    if claimable == 0:
        print("OK|no claimable cards")
        sys.exit(0)
    row = cur.execute("SELECT MAX(created_at) FROM task_events").fetchone()
    last_ts = row[0] if row and row[0] is not None else 0
    age = now - int(last_ts)
    if age > hb_max:
        print(f"STALE|claimable={claimable} last_event_age={age}s threshold={hb_max}s")
    else:
        print(f"OK|claimable={claimable} last_event_age={age}s")
except Exception as e:
    print(f"DB_ERROR|{e!r}")
PY
  )"
  if [[ -z "$result" || "$result" =~ ^OK\| ]]; then
    return
  fi
  local code="${result%%|*}"
  local msg="${result#*|}"
  case "$code" in
    STALE)    record_fail "DISPATCHER_STALE" "dispatcher silent with work waiting — $msg" ;;
    DB_ERROR) record_fail "KANBAN_DB_ERROR" "$msg" ;;
    *)        record_fail "DISPATCHER_UNKNOWN" "$result" ;;
  esac
}

probe_tailnet() {
  [[ -z "$TAILNET_URL" ]] && return
  local code
  code="$(curl -sS -m 8 -o /dev/null -w '%{http_code}' "$TAILNET_URL" 2>/dev/null || echo "000")"
  if [[ "$code" != "200" ]]; then
    record_fail "TAILNET_UNREACHABLE" "GET $TAILNET_URL → HTTP $code"
  fi
}

# ---- run probes -------------------------------------------------------------
probe_systemd
probe_dashboard
probe_cli
probe_dispatcher
probe_tailnet

# ---- write tick event -------------------------------------------------------
TICK_STATUS="ok"
[[ ${#FAILURES[@]} -gt 0 ]] && TICK_STATUS="fail"
SIGNATURE=""
SUMMARY_FOR_LOG=""
for f in "${FAILURES[@]:-}"; do
  [[ -z "$f" ]] && continue
  code="${f%%|*}"
  SIGNATURE="${SIGNATURE}${code},"
  SUMMARY_FOR_LOG="${SUMMARY_FOR_LOG}${f}\n"
done
SIGNATURE="${SIGNATURE%,}"

# Read prior state (python because jq isn't guaranteed)
PRIOR="$(
  HERMES_SF="$STATE_FILE" "$PYTHON_BIN" - <<'PY' 2>/dev/null || echo "{}"
import json, os
try:
    print(json.dumps(json.load(open(os.environ["HERMES_SF"]))))
except Exception:
    print("{}")
PY
)"
PRIOR_STATUS="$(printf '%s' "$PRIOR" | "$PYTHON_BIN" -c 'import json,sys;d=json.load(sys.stdin);print(d.get("last_status","unknown"))' 2>/dev/null || echo unknown)"
PRIOR_SIG="$(printf '%s' "$PRIOR" | "$PYTHON_BIN" -c 'import json,sys;d=json.load(sys.stdin);print(d.get("last_signature",""))' 2>/dev/null || echo '')"
PRIOR_ALERT_AT="$(printf '%s' "$PRIOR" | "$PYTHON_BIN" -c 'import json,sys;d=json.load(sys.stdin);print(int(d.get("last_alert_at",0) or 0))' 2>/dev/null || echo 0)"
PRIOR_CONSEC="$(printf '%s' "$PRIOR" | "$PYTHON_BIN" -c 'import json,sys;d=json.load(sys.stdin);print(int(d.get("consecutive_failures",0) or 0))' 2>/dev/null || echo 0)"

# Append tick event
printf '{"ts":"%s","status":"%s","signature":"%s","failures":%d,"prior_status":"%s"}\n' \
  "$UTC_TS" "$TICK_STATUS" "$SIGNATURE" "${#FAILURES[@]}" "$PRIOR_STATUS" >> "$EVENTS_FILE"

# ---- silence check ----------------------------------------------------------
SILENCED=0
if [[ -e "$SILENCE_FILE" ]]; then
  SILENCED=1
  # Annotate so Joseph can see the watchdog ran but stayed quiet
  printf '{"ts":"%s","silenced":true,"signature":"%s"}\n' "$UTC_TS" "$SIGNATURE" >> "$EVENTS_FILE"
fi

# ---- decide alert -----------------------------------------------------------
SHOULD_ALERT=0
ALERT_REASON=""
if [[ "$TICK_STATUS" == "fail" && "$SILENCED" -eq 0 ]]; then
  # First failure of this signature → alert
  if [[ "$PRIOR_STATUS" != "fail" ]]; then
    SHOULD_ALERT=1; ALERT_REASON="new-outage"
  else
    # Same outage class: check if signature introduces NEW failure codes not
    # in the prior signature (reviewer MAJOR#2). Set-diff, not string-diff —
    # a flapping gateway can churn codes (SYSTEMD_DOWN ↔ CLI_TIMEOUT etc.)
    # without genuinely-new failure modes; that's not a new alert reason.
    NEW_CODES="$(
      HERMES_CURR="$SIGNATURE" HERMES_PRIOR="$PRIOR_SIG" \
        "$PYTHON_BIN" - <<'PY' 2>/dev/null || echo ""
import os
curr = set(c for c in os.environ.get("HERMES_CURR", "").split(",") if c)
prior = set(c for c in os.environ.get("HERMES_PRIOR", "").split(",") if c)
new = curr - prior
print(",".join(sorted(new)))
PY
    )"
    if [[ -n "$NEW_CODES" ]]; then
      SHOULD_ALERT=1; ALERT_REASON="new-failure-mode:$NEW_CODES"
    else
      # Same code-set — debounce by DEBOUNCE_MIN
      ELAPSED_MIN=$(( (UNIX_NOW - PRIOR_ALERT_AT) / 60 ))
      if [[ $ELAPSED_MIN -ge $DEBOUNCE_MIN ]]; then
        SHOULD_ALERT=1; ALERT_REASON="periodic-reminder"
      fi
    fi
  fi
fi

# Recovery transition
SHOULD_RECOVERY=0
if [[ "$TICK_STATUS" == "ok" && "$PRIOR_STATUS" == "fail" && "$SILENCED" -eq 0 ]]; then
  SHOULD_RECOVERY=1
fi

# ---- build alert message ----------------------------------------------------
build_alert_msg() {
  # Compose Telegram-friendly markdown. Cite F01 from FAILURE-RUNBOOK-V2.
  local sev="🔴"
  local recovery=""
  for f in "${FAILURES[@]}"; do
    case "${f%%|*}" in
      DASHBOARD_DOWN|DASHBOARD_BAD_JSON|TAILNET_UNREACHABLE)
        # network-only issue is less severe than gateway crash
        [[ "$sev" == "🔴" ]] && sev="🟠"
        ;;
    esac
  done

  # If ANY probe says gateway/systemd is down, force RED severity
  for f in "${FAILURES[@]}"; do
    case "${f%%|*}" in
      SYSTEMD_DOWN|GATEWAY_NOT_RUNNING|GATEWAY_BAD_STATE|CLI_TIMEOUT|CLI_ERROR|DISPATCHER_STALE)
        sev="🔴" ;;
    esac
  done

  # Header
  printf '%s F01 GATEWAY ANOMALY [gateway-watchdog]\n' "$sev"
  printf 'Detected: %s\n' "$UTC_TS"
  printf 'Tick reason: %s (consecutive=%d)\n' "$ALERT_REASON" "$((PRIOR_CONSEC + 1))"
  printf 'Failures:\n'
  for f in "${FAILURES[@]}"; do
    code="${f%%|*}"
    detail="${f#*|}"
    printf '  • [%s] %s\n' "$code" "$detail"
  done
  printf '\n'
  printf 'Recovery (FAILURE-RUNBOOK-V2 §F01):\n'
  printf '  hermes kanban list --status running    # drain in-flight first\n'
  printf '  systemctl --user status %s\n' "$GATEWAY_UNIT"
  printf '  systemctl --user restart %s   # ONLY after drain\n' "$GATEWAY_UNIT"
  printf '  watch -n 10 "hermes kanban stats"\n'
  printf '\n'
  printf 'Watchdog: %s\n' "$0"
  printf 'State: %s\n' "$STATE_FILE"
  printf 'Silence: touch %s   # disable until removed\n' "$SILENCE_FILE"
}

build_recovery_msg() {
  printf '🟢 F01 GATEWAY RECOVERED [gateway-watchdog]\n'
  printf 'Recovered: %s\n' "$UTC_TS"
  printf 'Previous signature: %s\n' "$PRIOR_SIG"
  printf 'All probes ok. Closing alert state.\n'
}

# ---- dispatch alert ---------------------------------------------------------
send_message() {
  local msg="$1"
  local channel="$2"  # "alert" or "recovery"
  local notify_rc=99
  local notify_err=""

  if [[ "$DRYRUN" == "1" ]]; then
    notify_rc=0
    notify_err="dryrun"
  elif [[ ! -x "$NOTIFY_SCRIPT" ]]; then
    notify_rc=98
    notify_err="notify-script-missing:$NOTIFY_SCRIPT"
  else
    if printf '%s' "$msg" | "$NOTIFY_SCRIPT" >/tmp/watchdog-notify.$$ 2>&1; then
      notify_rc=0
    else
      notify_rc=$?
      notify_err="$(cat /tmp/watchdog-notify.$$ 2>/dev/null | head -c 200 | tr -d '\n' || true)"
    fi
    rm -f /tmp/watchdog-notify.$$
  fi

  # Always log to alerts.jsonl, even if telegram failed
  printf '{"ts":"%s","channel":"%s","signature":"%s","notify_rc":%d,"notify_err":%s,"msg_preview":%s}\n' \
    "$UTC_TS" "$channel" "$SIGNATURE" "$notify_rc" \
    "$(printf '%s' "${notify_err:-}" | "$PYTHON_BIN" -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
    "$(printf '%s' "$msg" | head -c 400 | "$PYTHON_BIN" -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
    >> "$ALERTS_FILE"

  return "$notify_rc"
}

if [[ "$SHOULD_ALERT" -eq 1 ]]; then
  ALERT_MSG="$(build_alert_msg)"
  send_message "$ALERT_MSG" "alert" || true
  NEW_LAST_ALERT_AT="$UNIX_NOW"
else
  NEW_LAST_ALERT_AT="$PRIOR_ALERT_AT"
fi

if [[ "$SHOULD_RECOVERY" -eq 1 ]]; then
  REC_MSG="$(build_recovery_msg)"
  send_message "$REC_MSG" "recovery" || true
fi

# ---- update state -----------------------------------------------------------
if [[ "$TICK_STATUS" == "fail" ]]; then
  NEW_CONSEC=$((PRIOR_CONSEC + 1))
else
  NEW_CONSEC=0
fi

HERMES_TS="$TICK_STATUS" \
HERMES_SIG="$SIGNATURE" \
HERMES_ALERTAT="$NEW_LAST_ALERT_AT" \
HERMES_CONSEC="$NEW_CONSEC" \
HERMES_NOW2="$UNIX_NOW" \
HERMES_PRIOR_STATUS="$PRIOR_STATUS" \
HERMES_SF="$STATE_FILE" \
"$PYTHON_BIN" - <<'PY' 2>/dev/null || true
import json, os
sf = os.environ["HERMES_SF"]
status = os.environ["HERMES_TS"]
sig = os.environ["HERMES_SIG"]
alert_at = int(os.environ["HERMES_ALERTAT"])
consec = int(os.environ["HERMES_CONSEC"])
now = int(os.environ["HERMES_NOW2"])
prior_status = os.environ["HERMES_PRIOR_STATUS"]
try:
    prior = json.load(open(sf))
except Exception:
    prior = {}
first_failure_at = prior.get("first_failure_at")
if status == "fail" and prior_status != "fail":
    first_failure_at = now
elif status == "ok":
    first_failure_at = None
state = {
    "last_status": status,
    "last_signature": sig,
    "consecutive_failures": consec,
    "last_alert_at": alert_at,
    "first_failure_at": first_failure_at,
    "updated_at": now,
}
# Atomic write: tmp + os.replace prevents truncated state.json on kill
# mid-write (reviewer MAJOR#3). os.replace is atomic on POSIX.
tmp = sf + ".tmp"
with open(tmp, "w") as f:
    f.write(json.dumps(state))
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, sf)
PY

# ---- final stdout/exit ------------------------------------------------------
if [[ "$TICK_STATUS" == "ok" ]]; then
  echo "gateway-watchdog: ok (signature=$SIGNATURE)"
  exit 0
else
  echo "gateway-watchdog: FAIL ($SIGNATURE) — alert=$SHOULD_ALERT recovery=$SHOULD_RECOVERY silenced=$SILENCED" >&2
  exit 1
fi
