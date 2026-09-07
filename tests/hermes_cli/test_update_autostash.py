from pathlib import Path
from subprocess import CalledProcessError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import config as hermes_config
from hermes_cli import main as hermes_main


# ---------------------------------------------------------------------------
# Managed-uv compatibility for tests that patch shutil.which
# ---------------------------------------------------------------------------
# The production code now uses ``ensure_uv()`` / ``update_managed_uv()``
# instead of ``shutil.which("uv")``.  Many tests in this file patch
# ``shutil.which`` to control whether uv is "available" — these autouse
# fixtures make the managed_uv functions delegate to the patched
# ``shutil.which`` so the existing test setup keeps working without
# per-test changes.
@pytest.fixture(autouse=True)
def _patch_managed_uv(request):
    """Make managed_uv helpers follow shutil.which mocking in tests."""
    import shutil

    # resolve_uv delegates to shutil.which("uv") so that test patches
    # on shutil.which flow through naturally.
    def _fake_resolve_uv(**kwargs):
        return shutil.which("uv")

    def _fake_ensure_uv(**kwargs):
        return shutil.which("uv")

    def _fake_update_managed_uv(**kwargs):
        return None  # never actually self-update in tests

    with patch("hermes_cli.managed_uv.resolve_uv", side_effect=_fake_resolve_uv), \
         patch("hermes_cli.managed_uv.ensure_uv", side_effect=_fake_ensure_uv), \
         patch("hermes_cli.managed_uv.update_managed_uv", side_effect=_fake_update_managed_uv):
        yield













# ---------------------------------------------------------------------------
# Update uses .[all] with fallback to .
# ---------------------------------------------------------------------------

def _patch_gateway_fleet_verification(monkeypatch):
    """Keep cmd_update's gateway auto-restart + fleet verification off this
    machine's real gateways (mirrors tests/hermes_cli/test_cmd_update.py's
    ``_patch_gateway_discovery`` autouse fixture).

    ``_cmd_update_impl`` evicts every cached hermes_cli/gateway module from
    ``sys.modules`` mid-run (``_purge_stale_hermes_modules``) and re-imports
    ``hermes_cli.gateway`` from scratch for the restart phase — which
    silently drops a bare ``find_gateway_pids`` patch (new module object,
    the monkeypatch doesn't survive) and lets the restart phase discover
    THIS machine's real live gateway PIDs. Only the test process's
    live-system guard (tests/conftest.py) stood between that and a real
    ``os.kill`` on this box, and it still turned several of this file's
    ``cmd_update`` tests into a ~30s poll against real machine state that
    then failed closed. No-op the purge, keep gateway discovery empty, and
    pin the Phase 1 (#91277) fleet-plan/verification collectors to empty so
    the post-update fleet-verification loop's real ``_time.sleep(2.0)`` (up
    to a 30s deadline) is never entered either.
    """
    import hermes_cli.gateway as hermes_gateway
    from hermes_cli import update_cmd

    monkeypatch.setattr(
        hermes_gateway, "find_gateway_pids", lambda *a, **kw: []
    )
    monkeypatch.setattr(hermes_gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(
        hermes_gateway, "find_profile_gateway_processes", lambda *a, **kw: []
    )
    monkeypatch.setattr(hermes_main, "_purge_stale_hermes_modules", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions", lambda **k: []
    )
    monkeypatch.setattr(
        "hermes_cli.update_inventory.collect_runtime_inventory",
        lambda: SimpleNamespace(runtimes=[], to_dict=lambda: {}),
    )
    monkeypatch.setattr(update_cmd._time, "sleep", lambda *a, **k: None)


def _setup_update_mocks(monkeypatch, tmp_path):
    """Common setup for cmd_update tests."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hermes_main, "_stash_local_changes_if_needed", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_main, "_restore_stashed_changes", lambda *a, **kw: True)
    monkeypatch.setattr(hermes_config, "get_missing_env_vars", lambda required_only=True: [])
    monkeypatch.setattr(hermes_config, "get_missing_config_fields", lambda: [])
    monkeypatch.setattr(hermes_config, "check_config_version", lambda: (5, 5))
    monkeypatch.setattr(hermes_config, "migrate_config", lambda **kw: {"env_added": [], "config_added": []})
    monkeypatch.setattr(hermes_main, "_verify_editable_install", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_main, "_upgrade_pip_before_lazy_refresh", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_main, "_refresh_active_lazy_features", lambda *a, **kw: True)
    # The ``hermes tools`` dependency snapshot (d1df111ccd) probes which
    # allowlisted modules (faster_whisper, ddgs, langfuse, ...) are importable
    # in THIS test process and re-installs each one after the venv sync —
    # so the recorded ``uv pip install`` list would vary with whatever the
    # host venv happens to carry. Pin it empty (same seam as
    # tests/hermes_cli/test_update_parked_branch_guard.py) so the extras
    # assertions below see only the .[all] / fallback installs.
    monkeypatch.setattr(hermes_main, "_capture_active_tool_dependencies", lambda: [])
    _patch_gateway_fleet_verification(monkeypatch)




def _norm_install(cmd):
    """Strip the interpreter pin (``--python <venv>``) the updater now passes so
    the extras-fallback assertions stay about WHICH targets were installed."""
    out = list(cmd)
    while "--python" in out:
        i = out.index("--python"); del out[i:i + 2]
    return out


def _is_extras_install(cmd):
    return "pip" in cmd and "install" in cmd


def test_cmd_update_retries_optional_extras_individually_when_all_fails(monkeypatch, tmp_path, capsys):
    """When .[all] fails, update should keep base deps and retry extras individually."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(hermes_main, "_is_termux_env", lambda env=None: False)
    monkeypatch.setattr(hermes_main, "_load_installable_optional_extras", lambda group="all": ["matrix", "mcp"])

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        if cmd == ["git", "fetch", "origin", "main"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return SimpleNamespace(stdout="main\n", stderr="", returncode=0)
        if cmd == ["git", "rev-list", "HEAD..origin/main", "--count"]:
            return SimpleNamespace(stdout="1\n", stderr="", returncode=0)
        if cmd == ["git", "pull", "--ff-only", "origin", "main"]:
            return SimpleNamespace(stdout="Updating\n", stderr="", returncode=0)
        n = _norm_install(cmd)
        if n == ["/usr/bin/uv", "pip", "install", "-e", ".[all]"]:
            raise CalledProcessError(returncode=1, cmd=cmd)
        if n == ["/usr/bin/uv", "pip", "install", "-e", "."]:
            return SimpleNamespace(returncode=0)
        if n == ["/usr/bin/uv", "pip", "install", "-e", ".[matrix]"]:
            raise CalledProcessError(returncode=1, cmd=cmd)
        if n == ["/usr/bin/uv", "pip", "install", "-e", ".[mcp]"]:
            return SimpleNamespace(returncode=0)
        # Catch-all must include stdout/stderr so consumers that parse
        # output (e.g. the dashboard-restart `ps -A` scan added in the
        # updater) don't crash on AttributeError.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    hermes_main.cmd_update(SimpleNamespace())

    install_cmds = [_norm_install(c) for c in recorded if _is_extras_install(c)]
    assert install_cmds == [
        ["/usr/bin/uv", "pip", "install", "-e", ".[all]"],
        ["/usr/bin/uv", "pip", "install", "-e", "."],
        ["/usr/bin/uv", "pip", "install", "-e", ".[matrix]"],
        ["/usr/bin/uv", "pip", "install", "-e", ".[mcp]"],
    ]

    out = capsys.readouterr().out
    assert "retrying extras individually" in out
    assert "Reinstalled optional extras individually: mcp" in out
    assert "Skipped optional extras that still failed: matrix" in out


def test_cmd_update_succeeds_with_extras(monkeypatch, tmp_path):
    """When .[all] succeeds, no fallback should be attempted."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(hermes_main, "_is_termux_env", lambda env=None: False)

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        if cmd == ["git", "fetch", "origin", "main"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return SimpleNamespace(stdout="main\n", stderr="", returncode=0)
        if cmd == ["git", "rev-list", "HEAD..origin/main", "--count"]:
            return SimpleNamespace(stdout="1\n", stderr="", returncode=0)
        if cmd == ["git", "pull", "--ff-only", "origin", "main"]:
            return SimpleNamespace(stdout="Updating\n", stderr="", returncode=0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    hermes_main.cmd_update(SimpleNamespace())

    install_cmds = [_norm_install(c) for c in recorded if _is_extras_install(c)]
    assert len(install_cmds) == 1  # no `-e .` / per-extra fallback attempted
    assert ".[all]" in install_cmds[0]


def test_install_with_optional_fallback_honors_custom_group(monkeypatch):
    """Termux update path should target .[termux-all] when requested."""
    calls = []
    monkeypatch.setattr(hermes_main, "_verify_editable_install", lambda *a, **kw: None)
    monkeypatch.setattr(
        hermes_main,
        "_load_installable_optional_extras",
        lambda group="all": ["termux", "mcp"] if group == "termux-all" else [],
    )

    def fake_run_with_heartbeat(cmd, **kwargs):
        calls.append(cmd)
        if cmd[-1] == ".[termux-all]":
            raise CalledProcessError(returncode=1, cmd=cmd)
        return None

    monkeypatch.setattr(hermes_main, "_run_install_with_heartbeat", fake_run_with_heartbeat)

    hermes_main._install_python_dependencies_with_optional_fallback(
        ["/usr/bin/uv", "pip"],
        group="termux-all",
    )

    assert calls == [
        ["/usr/bin/uv", "pip", "install", "-e", ".[termux-all]"],
        ["/usr/bin/uv", "pip", "install", "-e", "."],
        ["/usr/bin/uv", "pip", "install", "-e", ".[termux]"],
        ["/usr/bin/uv", "pip", "install", "-e", ".[mcp]"],
    ]


def test_refresh_active_memory_provider_dependencies_reinstalls_active_provider(monkeypatch):
    """#53272/#70636: update must re-run the active provider's dep install."""
    recorded = []

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"provider": "mem0"}},
    )
    monkeypatch.setattr(
        "hermes_cli.memory_setup._install_dependencies",
        lambda provider_name, force=False: recorded.append((provider_name, force)),
    )

    hermes_main._refresh_active_memory_provider_dependencies()

    assert recorded == [("mem0", True)]




def test_reload_updated_runtime_modules_restores_new_hermes_constants_symbol(monkeypatch):
    """A pre-pull module object missing a new helper is repaired by reload."""
    import hermes_constants

    monkeypatch.delattr(hermes_constants, "apply_subprocess_home_env", raising=False)
    assert not hasattr(hermes_constants, "apply_subprocess_home_env")

    hermes_main._reload_updated_runtime_modules()

    assert callable(hermes_constants.apply_subprocess_home_env)






# ---------------------------------------------------------------------------
# ff-only fallback to reset --hard on diverged history
# ---------------------------------------------------------------------------

def _make_update_side_effect(
    current_branch="main",
    commit_count="3",
    ff_only_fails=False,
    reset_fails=False,
    fetch_fails=False,
    fetch_stderr="",
):
    """Build a subprocess.run side_effect for cmd_update tests."""
    recorded = []

    def side_effect(cmd, **kwargs):
        recorded.append(cmd)
        joined = " ".join(str(c) for c in cmd)
        if "fetch" in joined and "origin" in joined:
            if fetch_fails:
                return SimpleNamespace(stdout="", stderr=fetch_stderr, returncode=128)
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(stdout=f"{current_branch}\n", stderr="", returncode=0)
        if "checkout" in joined and "main" in joined:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "rev-list" in joined:
            return SimpleNamespace(stdout=f"{commit_count}\n", stderr="", returncode=0)
        if "--ff-only" in joined:
            if ff_only_fails:
                return SimpleNamespace(
                    stdout="",
                    stderr="fatal: Not possible to fast-forward, aborting.\n",
                    returncode=128,
                )
            return SimpleNamespace(stdout="Updating abc..def\n", stderr="", returncode=0)
        if "reset" in joined and "--hard" in joined:
            if reset_fails:
                return SimpleNamespace(stdout="", stderr="error: unable to write\n", returncode=1)
            return SimpleNamespace(stdout="HEAD is now at abc123\n", stderr="", returncode=0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect, recorded


# ---------------------------------------------------------------------------
# Non-main branch → auto-checkout main
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fetch failure — friendly error messages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# reset --hard failure — don't attempt stash restore
# ---------------------------------------------------------------------------

def test_cmd_update_skips_stash_restore_when_reset_fails(monkeypatch, tmp_path, capsys):
    """When reset --hard fails, stash restore is skipped with a helpful message."""
    _setup_update_mocks(monkeypatch, tmp_path)
    # Re-enable stash so it actually returns a ref
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed",
        lambda *a, **kw: "abc123deadbeef",
    )
    restore_calls = []
    monkeypatch.setattr(
        hermes_main, "_restore_stashed_changes",
        lambda *a, **kw: restore_calls.append(1) or True,
    )

    side_effect, _ = _make_update_side_effect(ff_only_fails=True, reset_fails=True)
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace())

    # Stash restore should NOT have been called
    assert len(restore_calls) == 0

    out = capsys.readouterr().out
    assert "preserved in stash" in out


# ---------------------------------------------------------------------------
# Non-interactive update.non_interactive_local_changes setting
# (chat app / gateway): "discard" throws stashed changes away, "stash"
# (default) restores them. Interactive terminal updates ignore the setting
# and always go through the restore path.
# ---------------------------------------------------------------------------

def _setup_setting_test(monkeypatch, tmp_path, mode):
    """Common wiring: real stash returns a ref, restore + discard are
    recorded, and load_config reports the given non_interactive_local_changes
    mode."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed",
        lambda *a, **kw: "abc123deadbeef",
    )
    restore_calls = []
    discard_calls = []
    monkeypatch.setattr(
        hermes_main, "_restore_stashed_changes",
        lambda *a, **kw: restore_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_main, "_discard_stashed_changes",
        lambda *a, **kw: discard_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda *a, **kw: {"updates": {"non_interactive_local_changes": mode}},
    )
    side_effect, recorded = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)
    return restore_calls, discard_calls, recorded


# ---------------------------------------------------------------------------
# --keep-stash (desktop updater): stash for the update, never re-apply.
# ---------------------------------------------------------------------------

def _setup_keep_stash_test(monkeypatch, tmp_path):
    """Wiring for --keep-stash tests: stash returns a ref; restore, discard,
    and park are all recorded."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed",
        lambda *a, **kw: "abc123deadbeef",
    )
    restore_calls = []
    discard_calls = []
    park_calls = []
    monkeypatch.setattr(
        hermes_main, "_restore_stashed_changes",
        lambda *a, **kw: restore_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_main, "_discard_stashed_changes",
        lambda *a, **kw: discard_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_main, "_park_stashed_changes",
        lambda *a, **kw: park_calls.append(a) or None,
    )
    # Keep the update flow away from the real gateway fleet on this machine —
    # a live gateway PID would trip the test-suite kill guard and turn the
    # run into exit 1 (gateway_fleet_restart_incomplete).
    monkeypatch.setattr(
        "hermes_cli.gateway.find_gateway_pids", lambda **kw: [], raising=False
    )
    return restore_calls, discard_calls, park_calls


def test_update_keep_stash_parks_instead_of_restoring(monkeypatch, tmp_path):
    """--keep-stash: after a successful update, the autostash is parked (left
    in git stash) — never re-applied, never discarded."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=True))

    assert len(park_calls) == 1
    assert park_calls[0][0] == "abc123deadbeef"
    assert restore_calls == []
    assert discard_calls == []


def test_update_without_keep_stash_still_restores(monkeypatch, tmp_path):
    """Regression guard: default behavior (no --keep-stash) is unchanged —
    the autostash is auto-restored under --yes."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=False))

    assert restore_calls == [1]
    assert park_calls == []
    assert discard_calls == []


def test_update_keep_stash_failure_path_still_preserves(monkeypatch, tmp_path, capsys):
    """--keep-stash + failed update: neither restore nor park runs; the
    existing preserved-in-stash message fires (working tree unknown)."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect(ff_only_fails=True, reset_fails=True)
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=True))

    assert restore_calls == []
    assert park_calls == []
    assert discard_calls == []
    assert "preserved in stash" in capsys.readouterr().out


def test_update_parser_accepts_keep_stash():
    """The flag parses and defaults off."""
    import argparse

    from hermes_cli.subcommands.update import build_update_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    build_update_parser(subparsers, cmd_update=lambda args: None)

    args = parser.parse_args(["update", "--keep-stash"])
    assert args.keep_stash is True
    args = parser.parse_args(["update"])
    assert args.keep_stash is False






def test_bootstrap_marker_not_autostashed_by_update(tmp_path):
    """#38529: the Desktop bootstrap marker must be git-ignored so that
    ``hermes update``'s ``git stash push --include-untracked`` does not sweep it
    into an autostash on every run.

    Behavioral + hermetic: build a throwaway repo that adopts the project's real
    ``.gitignore`` (the contract under test), drop the marker, and confirm the
    same stash invocation the updater uses leaves it untouched.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")

    repo_gitignore = Path(hermes_main.__file__).resolve().parents[1] / ".gitignore"

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True
        )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(repo_gitignore.read_text())
    (tmp_path / "tracked.txt").write_text("x\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    marker = tmp_path / ".hermes-bootstrap-complete"
    marker.write_text("")

    # Exact flags used by hermes update (hermes_cli/main.py).
    git("stash", "push", "--include-untracked", "-m", "hermes-update-autostash")

    assert marker.exists(), (
        ".hermes-bootstrap-complete was swept into the update autostash — it must "
        "be listed in .gitignore so `git stash -u` skips it (#38529)."
    )
    # It must not even register as a dirty/untracked change.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert ".hermes-bootstrap-complete" not in status


# ---------------------------------------------------------------------------
# Permission-denied autostash class: undeletable untracked files (root-owned
# packaging/ etc.) must not abort the update when the stash entry was created.
# ---------------------------------------------------------------------------






def test_update_autostash_survives_undeletable_untracked_dir(tmp_path):
    """Behavioral E2E of the whole permission-denied class with real git:
    root-owned-style undeletable untracked dir → stash succeeds, update-style
    reset works, restore round-trips, nothing lost. (#70127 follow-up)"""
    import os
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")
    if os.name == "nt":
        pytest.skip("POSIX permission semantics")
    if os.geteuid() == 0:
        pytest.skip("root ignores directory write bits")

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=check
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("v1\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    (tmp_path / "tracked.txt").write_text("v2 local change\n")
    pkg = tmp_path / "packaging" / "homebrew"
    pkg.mkdir(parents=True)
    (pkg / "hermes-agent.rb").write_text("formula\n")
    os.chmod(pkg, 0o555)  # undeletable contents, like a root-owned dir
    try:
        stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
        assert stash_ref

        # The tracked change is stashed; simulate the updater's checkout window.
        assert (tmp_path / "tracked.txt").read_text() == "v1\n"

        restored = hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )
        assert restored is True
        assert (tmp_path / "tracked.txt").read_text() == "v2 local change\n"
        assert (pkg / "hermes-agent.rb").read_text() == "formula\n"
    finally:
        os.chmod(pkg, 0o755)


def test_restore_rejects_invalid_python_and_keeps_clean_updated_tree(
    monkeypatch, tmp_path, capsys
):
    """A cleanly-applied stash must not be allowed to brick every agent turn."""
    import subprocess
    from hermes_cli import update_cmd

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    source = tmp_path / "tools" / "terminal_tool.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    source.write_text("<<<<<<< Updated upstream\nVALUE = 2\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ())

    with pytest.raises(SystemExit) as exc_info:
        hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git("status", "--porcelain").stdout == ""
    assert git("stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "made the Hermes agent unexecutable" in output
    assert "gateway was not restarted" in output
    assert f"git stash apply {stash_ref}" in output


def test_restore_rejects_new_import_time_failure_and_preserves_stash(
    monkeypatch, tmp_path, capsys
):
    """A valid-Python stash must not introduce a critical import failure."""
    import subprocess
    from hermes_cli import update_cmd

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    source = tmp_path / "consumer.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    source.write_text("raise RuntimeError('restored local failure')\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("consumer",))

    with pytest.raises(SystemExit) as exc_info:
        hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git("status", "--porcelain").stdout == ""
    assert git("stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "agent import consumer" in output
    assert "restored local failure" in output
    assert "gateway was not restarted" in output


def test_restore_allows_preexisting_import_time_failure(monkeypatch, tmp_path):
    """A restore may proceed when it does not worsen an environment failure."""
    import subprocess
    from hermes_cli import update_cmd

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "consumer.py").write_text(
        "raise RuntimeError('missing local config')\n", encoding="utf-8"
    )
    local_file = tmp_path / "local.txt"
    local_file.write_text("original\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    local_file.write_text("restored\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("consumer",))

    assert hermes_main._restore_stashed_changes(
        ["git"], tmp_path, stash_ref, prompt_user=False
    )
    assert local_file.read_text(encoding="utf-8") == "restored\n"
    assert git("stash", "list").stdout.strip() == ""


def test_restore_rejects_later_failure_masked_by_preexisting_failure(
    monkeypatch, tmp_path, capsys
):
    """Every critical module must be compared, not only the first failure."""
    import subprocess
    from hermes_cli import update_cmd

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "first.py").write_text(
        "raise RuntimeError('missing local config')\n", encoding="utf-8"
    )
    second = tmp_path / "second.py"
    second.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    second.write_text("raise RuntimeError('restored later failure')\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("first", "second"))

    with pytest.raises(SystemExit) as exc_info:
        hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )

    assert exc_info.value.code == 1
    assert second.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git("status", "--porcelain").stdout == ""
    assert git("stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "agent import second" in output
    assert "restored later failure" in output
    assert "gateway was not restarted" in output


def test_restore_rejects_system_exit_masked_by_preexisting_failure(
    monkeypatch, tmp_path, capsys
):
    """A terminating import must be compared instead of hiding the marker."""
    import subprocess
    from hermes_cli import update_cmd

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "first.py").write_text(
        "raise RuntimeError('missing local config')\n", encoding="utf-8"
    )
    second = tmp_path / "second.py"
    second.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    second.write_text("raise SystemExit('restored exit')\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("first", "second"))

    with pytest.raises(SystemExit) as exc_info:
        hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )

    assert exc_info.value.code == 1
    assert second.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git("status", "--porcelain").stdout == ""
    assert git("stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "agent import second" in output
    assert "restored exit" in output
    assert "gateway was not restarted" in output


def test_restore_rejects_probe_termination(monkeypatch, tmp_path, capsys):
    """A stash cannot bypass import validation by terminating the probe."""
    import subprocess
    from hermes_cli import update_cmd

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    source = tmp_path / "consumer.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    source.write_text("import os\nos._exit(7)\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("consumer",))

    with pytest.raises(SystemExit) as exc_info:
        hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git("status", "--porcelain").stdout == ""
    assert git("stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "critical-module probe" in output
    assert "exit code 7" in output
    assert "gateway was not restarted" in output


def test_restore_stays_parked_when_untracked_baseline_is_unknown(
    monkeypatch, tmp_path, capsys
):
    """Unknown cleanup scope must not turn into a destructive empty baseline."""
    from hermes_cli import update_cmd

    monkeypatch.setattr(update_cmd, "_git_untracked_paths", lambda *_args: None)

    restored = hermes_main._restore_stashed_changes(
        ["git"], tmp_path, "stash@{0}", prompt_user=False
    )

    assert restored is False
    output = capsys.readouterr().out
    assert "cleanup baseline is unknown" in output
    assert "git stash apply stash@{0}" in output


def test_reject_does_not_claim_cleanup_when_git_state_is_unknown(
    monkeypatch, tmp_path, capsys
):
    """Cleanup failures must not be reported as a restored clean tree."""
    from hermes_cli import update_cmd

    monkeypatch.setattr(update_cmd, "_git_untracked_paths", lambda *_args: None)

    with pytest.raises(SystemExit):
        update_cmd._reject_unsafe_stash_restore(
            ["git"], tmp_path, "stash@{0}", set(), "consumer.py", "invalid"
        )

    output = capsys.readouterr().out
    assert "could not be fully restored automatically" in output
    assert "The clean updated tree has been restored" not in output


def test_restore_rejects_unknown_restored_python_paths(
    monkeypatch, tmp_path, capsys
):
    """A failed post-apply path query cannot skip restored syntax validation."""
    import subprocess
    from hermes_cli import update_cmd

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    source = tmp_path / "consumer.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ())
    monkeypatch.setattr(update_cmd, "_restored_python_paths", lambda *_args: None)

    with pytest.raises(SystemExit) as exc_info:
        hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git("status", "--porcelain").stdout == ""
    assert git("stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "restored Python source discovery" in output
    assert "gateway was not restarted" in output


def test_gateway_restore_prompt_defaults_to_keep_stash(tmp_path, capsys):
    prompts = []

    restored = hermes_main._restore_stashed_changes(
        ["git"],
        tmp_path,
        "stash@{0}",
        prompt_user=True,
        input_fn=lambda prompt, default: prompts.append((prompt, default)) or "",
    )

    assert restored is False
    assert prompts == [("Restore local changes now? [y/N]", "n")]
    assert "still preserved in git stash" in capsys.readouterr().out
