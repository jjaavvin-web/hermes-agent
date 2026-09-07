"""Certification that Kanban retirement is fail-closed at the ROUTE and
AUTHORITY layer, independent of ``config.yaml`` (P7 item 8 follow-up).

Kanban was fully retired live on 2026-09-01 (the former ``kanban.db`` file
path is now a mode-0555 **directory** containing a ``RETIRED`` marker — see
``tests/security/test_kanban_tombstone_inert.py``). A prior review found the
retirement was only *config*-deep in the candidate:

* ``hermes_cli/web_server.py`` unconditionally mounted
  ``dashboard_nexus_actions`` (``/api/dashboard/nexus/actions/*``) — a W2B
  action backend whose every registered ticket carries the
  ``kanban-comment`` effect tag and whose live dispatch chokepoint
  hard-requires a kanban task id.
* The bundled Kanban plugin's write API (``/api/plugins/kanban/*``) stayed
  fail-closed *only* while ``plugins.disabled`` retained ``"kanban"`` — a
  lost config key would have resurrected it.

This module builds the real dashboard FastAPI app (``hermes_cli.web_server``
imports its routers at module import time, so each scenario runs in its own
subprocess with ``HERMES_HOME`` and ``config.yaml`` set *before* import) and
proves the tombstone — not config — is the gate:

(i)   tombstone present + a normal post-retirement config (``plugins.
      disabled: [kanban]``, ``dashboard.hidden_plugins: [kanban, nexus]``)
      → the Kanban write API and the nexus-actions router are both ABSENT,
      and the nexus-action registry's runtime authorization gate rejects
      every ticket (all of them carry ``kanban-comment``).
(ii)  tombstone present + config-loss (neither ``plugins.disabled`` nor
      ``dashboard.hidden_plugins`` set) → identical assertions to (i),
      proving the gate is the tombstone, not the config keys.
(iii) no tombstone + Kanban enabled → both route families DO mount, proving
      the gate only fires on retirement, not a blanket removal.

Nothing under the real ``~/.hermes`` is ever opened or modified — every
scenario builds its own isolated ``HERMES_HOME`` under ``tmp_path``.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The nexus-actions router prefix (hermes_cli/dashboard_nexus_actions.py)
# and the bundled Kanban plugin's mounted prefix
# (_mount_plugin_api_routes(): ``/api/plugins/<name>/``, plugins/kanban/
# dashboard/manifest.json name="kanban").
_NEXUS_ACTIONS_PREFIX = "/api/dashboard/nexus/actions"
_KANBAN_PLUGIN_PREFIX = "/api/plugins/kanban"

# Executed in a fresh subprocess per scenario (module-level import time is
# when hermes_cli.web_server decides what to mount), with
# HERMES_TEST_EXPECT_RETIRED=1|0 selecting which assertions apply.
_SCRIPT = textwrap.dedent(
    """
    import os

    expect_retired = os.environ["HERMES_TEST_EXPECT_RETIRED"] == "1"

    from hermes_cli import web_server as ws

    paths = {route.path for route in ws.app.routes}
    nexus_actions_paths = {p for p in paths if p.startswith(%(nexus_prefix)r)}
    kanban_plugin_paths = {p for p in paths if p.startswith(%(kanban_prefix)r)}

    from hermes_cli import nexus_action_registry as registry

    if expect_retired:
        assert nexus_actions_paths == set(), (
            f"nexus actions routes mounted while Kanban retired: {nexus_actions_paths}"
        )
        assert kanban_plugin_paths == set(), (
            f"Kanban plugin write API mounted while Kanban retired: {kanban_plugin_paths}"
        )
        try:
            registry.validate_registry()
        except registry.NexusRegistryError as exc:
            assert "kanban" in str(exc).lower(), f"unexpected rejection reason: {exc}"
        else:
            raise AssertionError(
                "validate_registry() must reject kanban-comment tickets while Kanban is retired"
            )
    else:
        assert %(nexus_prefix)r + "/registry" in paths, (
            f"nexus actions routes missing while Kanban is NOT retired: {sorted(paths)[:20]}..."
        )
        assert %(kanban_prefix)r + "/board" in paths, (
            f"Kanban plugin routes missing while Kanban is NOT retired: {sorted(paths)[:20]}..."
        )
        # Fully validates without raising when Kanban is live.
        registry.validate_registry()
    """
) % {"nexus_prefix": _NEXUS_ACTIONS_PREFIX, "kanban_prefix": _KANBAN_PLUGIN_PREFIX}


def _write_config(home: Path, *, disable_kanban_plugin: bool, hide_kanban_tabs: bool) -> None:
    home.mkdir(parents=True, exist_ok=True)
    if not disable_kanban_plugin and not hide_kanban_tabs:
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        return
    lines: list[str] = []
    if disable_kanban_plugin:
        lines += ["plugins:", "  disabled:", "    - kanban"]
    if hide_kanban_tabs:
        lines += ["dashboard:", "  hidden_plugins:", "    - kanban", "    - nexus"]
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_tombstone(home: Path) -> None:
    """Mirror the live retired shape: ``<home>/kanban.db`` is a mode-0555
    directory containing a single read-only ``RETIRED`` file.
    """
    kanban_db_dir = home / "kanban.db"
    kanban_db_dir.mkdir(parents=True, exist_ok=True)
    (kanban_db_dir / "RETIRED").write_text(
        "Kanban retired 2026-09-01. Board history is read-only.\n",
        encoding="utf-8",
    )
    kanban_db_dir.chmod(0o555)


def _restore_writable(home: Path) -> None:
    # So pytest's own tmp_path cleanup can rmtree the tombstone directory —
    # the read-only bit is the thing under test, not a teardown constraint.
    kanban_db_dir = home / "kanban.db"
    try:
        kanban_db_dir.chmod(0o755)
    except OSError:
        pass


def _run_scenario(home: Path, *, expect_retired: bool) -> None:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_TEST_EXPECT_RETIRED"] = "1" if expect_retired else "0"
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.fail(
            f"scenario subprocess failed (expect_retired={expect_retired}):\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


def test_i_tombstone_present_normal_config_routes_and_registry_fail_closed(tmp_path: Path):
    home = tmp_path / ".hermes"
    _write_config(home, disable_kanban_plugin=True, hide_kanban_tabs=True)
    _make_tombstone(home)
    try:
        _run_scenario(home, expect_retired=True)
    finally:
        _restore_writable(home)


def test_ii_tombstone_present_config_loss_routes_and_registry_still_fail_closed(tmp_path: Path):
    """The gate must be the filesystem tombstone, not the config keys —
    prove it survives ``plugins.disabled`` / ``dashboard.hidden_plugins``
    both being absent (a lost or hand-edited config.yaml)."""
    home = tmp_path / ".hermes"
    _write_config(home, disable_kanban_plugin=False, hide_kanban_tabs=False)
    _make_tombstone(home)
    try:
        _run_scenario(home, expect_retired=True)
    finally:
        _restore_writable(home)


def test_iii_no_tombstone_kanban_enabled_routes_mount(tmp_path: Path):
    """Anti-fake-green floor: without the tombstone, both route families
    mount and the registry validates cleanly — proves the gate added above
    is the retirement tombstone, not a blanket removal of these routers."""
    home = tmp_path / ".hermes"
    _write_config(home, disable_kanban_plugin=False, hide_kanban_tabs=False)
    _run_scenario(home, expect_retired=False)
