"""Immutable-deployment refusal for `evaluate_update_admission` (ledger row 135).

This fork's fleet runs from immutable deployments built by
scripts/build_deployment.sh: a `git archive` export (no `.git`) stamped with
a `.deployed-commit` file, plus an `.install_method` = "immutable-deployment"
marker. Upstream is absorbed as a merge candidate + GATE-B cutover, never an
in-place `hermes update` against a built deployment — an in-place update
would let update_cmd.py's machinery take a live-state.db backup and reach a
SIGUSR1 fleet restart, colliding with the josep-only live-restart gate.

Before this fix, `evaluate_update_admission()` had no layer for this
deployment shape: an unknown marker fell through the image-provenance layer
and the docker/nix/apt heuristics as "legacy in-place-updatable" and the
update was admitted (`None`). These tests pin the fail-closed refusal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hermes_cli.update_contract import UpdateRefusal, evaluate_update_admission


def _absent_image_provenance(tmp_path: Path, monkeypatch) -> None:
    """Keep the unrelated image-provenance layer out of the way.

    Mirrors tests/hermes_cli/test_update_contract.py: point the baked
    provenance marker at a path that certainly does not exist, so these
    tests exercise only the new immutable-deployment layer (Layer 0), not
    whatever /etc/hermes/image-provenance.json happens to hold on the host
    running the suite.
    """
    import hermes_cli.image_provenance as ip

    monkeypatch.setattr(ip, "IMAGE_PROVENANCE_PATH", tmp_path / "absent-provenance.json")


# ---------------------------------------------------------------------------
# evaluate_update_admission() — Layer 0
# ---------------------------------------------------------------------------


def test_deployed_commit_without_git_dir_refuses(tmp_path, monkeypatch):
    """`.deployed-commit` present + no `.git` == a build_deployment.sh export."""
    _absent_image_provenance(tmp_path, monkeypatch)
    (tmp_path / ".deployed-commit").write_text("a" * 40 + "\n", encoding="utf-8")

    refusal = evaluate_update_admission(tmp_path)

    assert refusal is not None
    assert isinstance(refusal, UpdateRefusal)
    assert refusal.code == "immutable-deployment"
    assert "immutable deployment" in refusal.message.lower()
    assert "build_deployment.sh" in refusal.message
    assert "gate-b" in refusal.message.lower()


def test_install_method_immutable_deployment_stamp_alone_refuses(tmp_path, monkeypatch):
    """`.install_method` == "immutable-deployment" refuses even with no
    `.deployed-commit` file (the marker the task listed as the alternate
    trigger)."""
    _absent_image_provenance(tmp_path, monkeypatch)
    (tmp_path / ".install_method").write_text("immutable-deployment\n", encoding="utf-8")

    refusal = evaluate_update_admission(tmp_path)

    assert refusal is not None
    assert refusal.code == "immutable-deployment"
    assert "in-place update is disabled" in refusal.message.lower()


def test_git_checkout_no_immutable_markers_not_refused_by_this_rule(tmp_path, monkeypatch):
    """A normal git checkout (no `.deployed-commit`, no immutable
    `.install_method` stamp) must not be caught by the new rule.

    We only assert that *this* rule did not fire — not the overall verdict
    of evaluate_update_admission(), which is Layer 2's call (heuristics) and
    is already covered by tests/hermes_cli/test_update_contract.py. Keeping
    the assertion loose here decouples this test from that layer's future
    behavior.
    """
    _absent_image_provenance(tmp_path, monkeypatch)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda *a, **k: "git")

    refusal = evaluate_update_admission(tmp_path)

    if refusal is not None:
        assert refusal.code != "immutable-deployment"
        assert "immutable deployment" not in refusal.message.lower()
    # In today's heuristic layer a git checkout with no markers is admitted
    # outright (matches test_admission_git_checkout_no_marker_is_admitted).
    assert refusal is None


def test_deployed_commit_with_git_dir_present_not_refused_by_this_rule(tmp_path, monkeypatch):
    """`.deployed-commit` can legitimately exist next to `.git` in a dev
    checkout that copy-pasted the stamp; the marker alone (without an
    accompanying `.git`-less export) must not be enough to trip the rule.
    """
    _absent_image_provenance(tmp_path, monkeypatch)
    (tmp_path / ".deployed-commit").write_text("b" * 40 + "\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda *a, **k: "git")

    refusal = evaluate_update_admission(tmp_path)

    if refusal is not None:
        assert refusal.code != "immutable-deployment"
        assert "immutable deployment" not in refusal.message.lower()


# ---------------------------------------------------------------------------
# hermes_cli/web_server.py: POST /api/hermes/update
# ---------------------------------------------------------------------------
#
# The route handler (`update_hermes()`) never raises HTTPException on a
# refusal — every existing refusal branch (docker/nix/apt/image-marker)
# returns HTTP 200 with a JSON body carrying `"ok": false`, and the
# dashboard UI keys off that field, not the status code. So "4xx" is not
# literally this endpoint's contract; the security-relevant property is
# structural: a refusal short-circuits BEFORE `_spawn_hermes_action` (the
# thing that would exec `hermes update`, which is what takes the live
# state.db backup and can reach a SIGUSR1 fleet restart). We assert that
# contract directly at the function level rather than standing up a
# TestClient + full FastAPI app for one route.


def test_web_server_update_route_refuses_immutable_deployment_without_spawning(
    tmp_path, monkeypatch
):
    _absent_image_provenance(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / ".deployed-commit").write_text("c" * 40 + "\n", encoding="utf-8")

    import hermes_cli.web_server as web_server

    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_server, "_dashboard_local_update_managed_externally", lambda: False)

    def _fail_if_spawned(*args, **kwargs):
        raise AssertionError(
            "update route must not spawn `hermes update` (and its live "
            "state.db backup) against an immutable deployment"
        )

    monkeypatch.setattr(web_server, "_spawn_hermes_action", _fail_if_spawned)

    response = asyncio.run(web_server.update_hermes())

    assert response["ok"] is False
    assert response["pid"] is None
    assert "immutable deployment" in response["message"].lower()
    assert response.get("update_command")
