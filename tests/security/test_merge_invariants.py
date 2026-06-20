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
