"""Merge-invariant guards — the REAL upstream-merge-guard (audit SEC-2).

These assert fork-local security/operator invariants survive a NousResearch upstream
merge (which has silently reverted local patches before — the 0.16.0 backup.py
regression, PR#70). A reverted dispatch default, a shrunk secret-exclusion set, or a
dropped CVE-pin turns a NAMED CI check RED *before* merge — so josep can judge from a
red check, never a diff. Mirrors the inspect.getsource technique already used in
tests/hermes_cli/test_config_drift.py.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_dispatch_in_gateway_defaults_fail_closed():
    """All dispatch_in_gateway CODE defaults must be False (fail-closed) — DISP-1/ARCH-2.

    Upstream-vanilla default is True; flipping it back re-arms unbounded multi-board
    worker spawn before the global flock singleton lands (the safety contract forbids it).
    NOTE: the deployed runtime value lives in ~/.hermes/config.yaml and is guarded
    separately by config_drift_lint (weekly-hygiene); this test guards the CODE default.
    """
    cfg = _read("hermes_cli/config.py")
    assert '"dispatch_in_gateway": False' in cfg, "config.py DEFAULT must be False"
    assert '"dispatch_in_gateway": True' not in cfg, "config.py re-armed dispatch to True"
    for rel in ("hermes_cli/kanban.py", "gateway/kanban_watchers.py"):
        src = _read(rel)
        assert 'dispatch_in_gateway", True' not in src, f"{rel} has a default-True dispatch site"


def test_secret_exclusion_set_not_shrunk():
    """backup.py must exclude at least the baseline secret files/suffixes (PR#70 survival)."""
    src = _read("hermes_cli/backup.py")
    for name in ('".env"', '"auth.json"', '"relay.secret"'):
        assert name in src, f"backup secret-exclusion shrunk: {name} no longer excluded"
    for suffix in ('".key"', '".pem"'):
        assert suffix in src, f"backup secret-suffix shrunk: {suffix} no longer excluded"


def test_cve_pins_not_dropped():
    """Every CVE-annotated exact pin in the baseline must still be present in pyproject."""
    pyproject = _read("pyproject.toml")
    baseline = (Path(__file__).parent / "cve_pin_baseline.txt").read_text(encoding="utf-8")
    for line in baseline.splitlines():
        pin = line.strip()
        if not pin or pin.startswith("#"):
            continue
        assert pin in pyproject, f"CVE-annotated pin dropped/loosened: {pin}"


def test_disp5_autonomous_floor_present_and_armed():
    """The DISP-5 push/PR/workflow floor must EXIST and be ARMED at the webhook
    dispatch site — an upstream merge that drops either turns this RED (SEC-2).

    The floor is the fail-closed backstop consulted when a session deny-list is
    empty or its key mismatches; losing the symbols (revert) or the arming call
    (regression) silently re-opens autonomous push/PR/CI-trigger.
    """
    approval = _read("tools/approval.py")
    for sym in ("def mark_autonomous_dispatch", "_GIT_PUSH_FLOOR_RE",
                "def _floor_block_if_autonomous"):
        assert sym in approval, f"DISP-5 floor symbol dropped: {sym}"
    assert "_floor_block_if_autonomous(command)" in approval, \
        "floor no longer consulted inside check_session_deny_patterns"
    webhook = _read("gateway/platforms/webhook.py")
    assert "mark_autonomous_dispatch(True)" in webhook, \
        "webhook dispatch no longer ARMS the DISP-5 floor (contextvar never set)"


def test_webhook_deny_patterns_cover_ci_and_push():
    """Primary worker protection (DEFAULT_WEBHOOK_DENY_PATTERNS) must block push,
    PR open/merge, AND CI-workflow triggers — `gh workflow run` can push/merge/deploy.
    """
    webhook = _read("gateway/platforms/webhook.py")
    for needle in (r"git\s+(?:-\S+\s+\S*\s*)*push",
                   r"gh\s+pr\s+(?:create|merge|ready)",
                   r"gh\s+workflow\s+run"):
        assert needle in webhook, f"webhook deny patterns no longer cover: {needle}"


# --- v0.17 integration folded in here (one suite, not a parallel mechanism) ---

_LIFECYCLE_ENDPOINTS = (
    "/api/gateway/restart",
    "/api/gateway/stop",
    "/api/gateway/start",
    "/api/hermes/update",  # FORK-WIPE — the highest-consequence dashboard action
    "/api/webhooks/enable",
)


def test_dashboard_lifecycle_endpoints_stay_token_gated():
    """The :9119 lifecycle endpoints must NEVER join the public allowlist (0.17 POSTURE-PROBE).

    These five token-only POST routes (gateway restart/stop/start, hermes update=FORK-WIPE,
    webhooks enable) are gated solely by the global auth_middleware checking each request
    path against PUBLIC_API_PATHS. Adding any of them to the public set — or removing the
    global gate symbols — silently exposes a loopback-token action to an unauthenticated
    caller. An upstream merge (or a fat-fingered allowlist edit) that does either turns this
    RED before merge, so josep judges from a red check, not a diff.
    """
    public = _read("hermes_cli/dashboard_auth/public_paths.py")
    for ep in _LIFECYCLE_ENDPOINTS:
        assert f'"{ep}"' not in public, f"lifecycle endpoint joined PUBLIC_API_PATHS: {ep}"
    web = _read("hermes_cli/web_server.py")
    assert "_has_valid_session_token" in web, "dashboard token-gate symbol dropped"
    assert "PUBLIC_API_PATHS" in web, "dashboard no longer consults the public allowlist"
    for ep in _LIFECYCLE_ENDPOINTS:
        assert ep in web, f"lifecycle route vanished from web_server.py (gate void): {ep}"


def test_backup_should_exclude_recurses_secrets():
    """backup _should_exclude() must BEHAVIORALLY exclude secrets and KEEP config/soul.

    test_secret_exclusion_set_not_shrunk guards the exclusion *strings*; this guards the
    *behavior* of the recursive matcher (the 0.16.0 PR#70 regression changed behavior, not
    just the set). Args are pathlib.Path — the signature is ``rel_path: Path`` and str args
    raise AttributeError, so a passing str-based test would be fake-green.
    """
    from hermes_cli.backup import _should_exclude

    for secret in ("profiles/cheapgrunt/auth.json", "profiles/x/.env", "relay.secret",
                   "id_rsa.key", "tls/server.pem"):
        assert _should_exclude(Path(secret)) is True, f"secret no longer excluded: {secret}"
    for keep in ("hermes_cli/config.py", "SOUL.md", "pyproject.toml"):
        assert _should_exclude(Path(keep)) is False, f"non-secret wrongly excluded: {keep}"


def test_dashboard_bundle_internally_consistent():
    """The served SPA must not be a phantom: every asset index.html references must exist.

    The 05a0381da failure ('rebuild web_dist after upstream merge') served an index.html
    pointing at dropped/renamed bundles. A merge that skips the web rebuild leaves index.html
    and /assets out of sync — a dangling reference — which this catches. (It does not catch a
    fully-stale-but-self-consistent bundle; the dangerous, common case is the inconsistent one.)
    """
    import re

    dist = REPO / "hermes_cli" / "web_dist"
    index = (dist / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r"/assets/[A-Za-z0-9._-]+\.(?:js|css)", index)
    assert refs, "index.html references no /assets bundles — build missing/empty"
    for ref in refs:
        assert (dist / ref.lstrip("/")).exists(), f"phantom SPA: missing asset {ref}"


def test_send_message_stays_unregistered_for_agent():
    """The agent-callable send_message tool must stay REMOVED (0.17 safety hardening).

    v0.17 deliberately unregistered send_message so the model cannot ambiently message real
    people/platforms; the transport engine is retained for cron/kanban/CLI. An upstream merge
    that re-adds a registry.register for it silently re-opens that path. Our relay/loki
    automation does not depend on it (HMAC webhook + Discord REST), so this is a pure floor.
    """
    src = _read("tools/send_message_tool.py")
    assert src.count("registry.register") == 0, \
        "send_message re-registered as an agent tool (0.17 safety removal reverted)"
    assert "intentionally NOT registered" in src, \
        "send_message removal-intent marker dropped (merge may have rewritten the guard)"
