"""GitSpawn / GHSA-7x36-8jrh-v4pw regression suite.

A repository delivered as files (zip, sync folder, USB, subagent worktree) can
carry a ``.git/config`` that names a command in an execution-sink git setting —
``core.fsmonitor``, a ``core.hooksPath`` hook script, an attribute-scoped
``[diff "x"] command=``/``textconv=`` driver, or a ``[filter "x"] clean=``
driver. Hermes gathers workspace context by running git against the session
directory automatically, before any prompt, approval, or trust gate, so an
unhardened probe executes that command on the host as the user.

WHAT THIS PACKET CLOSES (asserted below, on a real armed repo):

* ``core.fsmonitor`` / ``core.hooksPath`` — pinned to inert values through
  ``GIT_CONFIG_KEY_N``/``GIT_CONFIG_VALUE_N`` in ``hardened_probe_git_env()``.
  Those env pairs carry *command-line* precedence (same as ``git -c``), which
  is what lets them override the attacker's repo-local ``.git/config``.
* attribute-scoped ``[diff "x"] command=``/``textconv=`` — neutralized by
  ``harden_git_argv()`` inserting ``--no-ext-diff --no-textconv`` after a
  diff-rendering subcommand.

...on the six automatic pre-trust probe paths only. ``noninteractive_git_env()``
is deliberately left at its original behaviour, because it is also the env for
mutation/network paths (dashboard commit and push, plugin/MCP clones, profile
distribution) that need the operator's identity, ``safe.directory``, stored
credential helper and repo hooks. ``TestOperatorConfigIsNotCollateral`` pins
that: it is the regression that sank the first version of this packet.

WHAT STAYS OPEN — every one of these is an ``xfail(strict=True)`` below, so the
gap is a visible known failure rather than an unstated assumption:

* ``filter.<name>.clean`` / ``.smudge``. The driver name is attacker-chosen, so
  a fixed ``GIT_CONFIG_KEY_N`` list cannot pin it, git has no ``--no-filters``,
  and every worktree-comparing diff output mode (``--numstat``/``--name-only``/
  ``--raw``/``--stat``) was measured to run it just like a full patch.
* the three fork-only sinks: ``hermes_cli/dashboard_codex_sessions._collect_diff``,
  ``gateway/codex_session_dispatcher._collect_diff`` and ``agent/merge_broker``
  fetch/rebase — out of scope for this packet, tracked separately.
* ``hermes_cli/web_git``'s index-refresh sinks: it is the dashboard git
  *mutation* surface, not a probe.

These tests use a real ``git`` and skip if it is unavailable. A fired sink is
proven by a benign ``touch`` marker on disk, never by running a payload.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli._subprocess_compat import (
    NO_DRIVER_DIFF_FLAGS,
    harden_git_argv,
    hardened_probe_git_env,
    noninteractive_git_env,
)

_HAS_GIT = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not _HAS_GIT, reason="git not installed")

# Sinks this packet claims to close. A "safe" assertion checks exactly these.
CLOSED_SINKS = ("fsmonitor", "hook", "extdiff", "textconv")
# Sinks knowingly left open; each has its own strict-xfail pin below.
OPEN_SINKS = ("filterclean",)
ALL_SINKS = CLOSED_SINKS + OPEN_SINKS


# ---------------------------------------------------------------------------
# 1. harden_git_argv unit contract  (unchanged — adjudicated sound)
# ---------------------------------------------------------------------------


class TestHardenGitArgv:
    def test_diff_gets_flags_after_subcommand(self):
        assert harden_git_argv(["diff", "HEAD"]) == [
            "diff", *NO_DRIVER_DIFF_FLAGS, "HEAD",
        ]

    def test_show_log_blame_are_hardened(self):
        for sub in ("show", "log", "blame"):
            out = harden_git_argv([sub, "x"])
            assert out[0] == sub
            assert out[1:3] == list(NO_DRIVER_DIFF_FLAGS)

    def test_status_is_not_touched(self):
        # status rejects --no-ext-diff (`unknown option`), so it must pass through.
        assert harden_git_argv(["status", "--porcelain=2", "--branch"]) == [
            "status", "--porcelain=2", "--branch",
        ]

    def test_worktree_and_other_subcommands_untouched(self):
        assert harden_git_argv(["worktree", "add", "x"]) == ["worktree", "add", "x"]
        assert harden_git_argv(["rev-parse", "HEAD"]) == ["rev-parse", "HEAD"]

    def test_global_options_are_skipped_when_finding_subcommand(self):
        out = harden_git_argv(["-C", "/repo", "diff", "HEAD"])
        assert out == ["-C", "/repo", "diff", *NO_DRIVER_DIFF_FLAGS, "HEAD"]

    def test_dash_c_value_is_not_mistaken_for_subcommand(self):
        # ``-C diff`` is a path; the real subcommand is status → no flags.
        assert harden_git_argv(["-C", "diff", "status"]) == ["-C", "diff", "status"]
        # ``-c diff=x`` is a config pair; the real subcommand is status.
        assert harden_git_argv(["-c", "diff=x", "status"]) == ["-c", "diff=x", "status"]

    def test_config_pair_before_diff_still_hardens(self):
        out = harden_git_argv(["-c", "core.quotePath=false", "diff", "--numstat"])
        assert out == [
            "-c", "core.quotePath=false", "diff", *NO_DRIVER_DIFF_FLAGS, "--numstat",
        ]


# ---------------------------------------------------------------------------
# 2. The armed repository
# ---------------------------------------------------------------------------


_CLEAN_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
_IDENT = ["-c", "user.email=a@b", "-c", "user.name=a"]


def _make_malicious_repo(
    tmp: Path, name: str = "poc", *, dirty: bool = True, divergent_base: bool = False,
) -> tuple[Path, Path]:
    """Build a repo whose ``.git/config`` arms fsmonitor, a hook directory, an
    attribute-scoped external-diff/textconv driver and a clean/smudge filter.

    Returns ``(repo, marker_stem)``; a fired sink leaves ``<marker>.<sink>``.
    Everything is committed BEFORE the config is armed, so setup itself never
    trips a sink.
    """
    repo = tmp / name

    def g(*args: str, check: bool = True):
        return subprocess.run(
            ["git", "-C", str(repo), *_IDENT, *args],
            check=check, capture_output=True, text=True, env=_CLEAN_ENV,
        )

    subprocess.run(["git", "init", "-q", "-b", "work", str(repo)], check=True, env=_CLEAN_ENV)
    (repo / "README").write_text("hi\n", encoding="utf-8")
    # Tracked, so the diff/filter drivers are actually reachable.
    (repo / ".gitattributes").write_text(
        "* diff=evil\npayload.txt filter=evil\n", encoding="utf-8",
    )
    (repo / "payload.txt").write_text("payload\n", encoding="utf-8")
    g("add", ".")
    g("commit", "-qm", "c1")

    if divergent_base:
        g("checkout", "-q", "-b", "basetip")
        (repo / "OTHER").write_text("base\n", encoding="utf-8")
        g("add", ".")
        g("commit", "-qm", "base-tip")
        base_tip = g("rev-parse", "HEAD").stdout.strip()
        g("checkout", "-q", "work")
    else:
        base_tip = g("rev-parse", "HEAD").stdout.strip()

    # The fork-only review paths diff against these remote-tracking refs.
    g("update-ref", "refs/remotes/origin/main", base_tip)
    g("update-ref", "refs/remotes/fork/main", base_tip)

    (repo / "README").write_text("feature\n", encoding="utf-8")
    g("add", ".")
    g("commit", "-qm", "c2")

    marker = tmp / f"MARKER-{name}"
    hooks = repo / "evil-hooks"
    hooks.mkdir()
    for hook_name in ("post-checkout", "post-index-change", "pre-rebase",
                      "post-rewrite", "pre-commit"):
        hook = hooks / hook_name
        hook.write_text(f"#!/bin/sh\ntouch {marker}.hook\n", encoding="utf-8")
        hook.chmod(0o755)

    with (repo / ".git" / "config").open("a", encoding="utf-8") as f:
        f.write(f'[core]\n\tfsmonitor = "touch {marker}.fsmonitor"\n\thooksPath = {hooks}\n')
        f.write(f'[diff "evil"]\n\tcommand = "touch {marker}.extdiff"\n')
        f.write(f'\ttextconv = "sh -c \'touch {marker}.textconv; cat\'"\n')
        f.write(f'[filter "evil"]\n\tclean = "sh -c \'touch {marker}.filterclean; cat\'"\n')
        f.write(f'\tsmudge = "sh -c \'touch {marker}.filterclean; cat\'"\n')

    if dirty:
        # A dirty worktree is what makes the working-tree diff probes do work.
        (repo / "payload.txt").write_text("payload MODIFIED\n", encoding="utf-8")
        (repo / "README").write_text("changed\n", encoding="utf-8")

    _fired(marker)  # discard anything setup itself tripped
    return repo, marker


def _fired(marker: Path, sinks: tuple[str, ...] = ALL_SINKS) -> list[str]:
    """Which sinks left a marker (consuming it, so each test starts clean)."""
    out = []
    for sink in sinks:
        p = Path(f"{marker}.{sink}")
        if p.exists():
            out.append(sink)
            p.unlink()
    return out


def _fired_closed(marker: Path) -> list[str]:
    """Only the sinks this packet claims to close. Consumes the open ones too so
    they cannot leak into the next assertion."""
    fired = _fired(marker)
    return [s for s in fired if s in CLOSED_SINKS]


@pytest.fixture()
def malicious_repo(tmp_path):
    return _make_malicious_repo(tmp_path)


# ---------------------------------------------------------------------------
# 3. Operator config is NOT collateral  (the round-1 regression pin)
# ---------------------------------------------------------------------------


class TestOperatorConfigIsNotCollateral:
    """``noninteractive_git_env`` is shared with the dashboard commit/push path,
    plugin and MCP clones and profile distribution. It must stay transparent to
    the operator's own global config: blanking ``GIT_CONFIG_GLOBAL`` there broke
    ``review_commit`` ("Author identity unknown"), ``safe.directory`` on
    foreign-owned repos, and the global-only ``!gh auth git-credential`` helper —
    while adding nothing, because the threat is repo-LOCAL config."""

    def test_noninteractive_env_touches_no_git_config_variable(self):
        env = noninteractive_git_env({})
        assert env == {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}

    def test_noninteractive_env_does_not_override_config(self):
        env = noninteractive_git_env()
        assert "GIT_CONFIG_COUNT" not in env
        assert not [k for k in env if k.startswith("GIT_CONFIG_")]

    def _global_config(self, tmp_path: Path) -> dict[str, str]:
        gitconfig = tmp_path / "operator-gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = Operator\n\temail = op@example.com\n"
            "[credential]\n\thelper = !gh auth git-credential\n"
            f"[safe]\n\tdirectory = {tmp_path / 'owned-by-someone-else'}\n",
            encoding="utf-8",
        )
        return {**os.environ, "GIT_CONFIG_GLOBAL": str(gitconfig)}

    @pytest.mark.parametrize("factory", [noninteractive_git_env, hardened_probe_git_env])
    def test_operator_identity_and_credentials_survive(self, tmp_path, factory):
        env = factory(self._global_config(tmp_path))
        for key, expected in (
            ("user.email", "op@example.com"),
            ("user.name", "Operator"),
            ("credential.helper", "!gh auth git-credential"),
        ):
            proc = subprocess.run(
                ["git", "config", "--get", key],
                capture_output=True, text=True, env=env, cwd=str(tmp_path),
            )
            if key == "credential.helper" and factory is hardened_probe_git_env:
                # The probe env deliberately clears the helper (a probe never
                # authenticates); the mutation env must not.
                assert proc.stdout.strip() == "", proc.stdout
                continue
            assert proc.returncode == 0, (key, proc.stderr)
            assert proc.stdout.strip() == expected

    @pytest.mark.parametrize("factory", [noninteractive_git_env, hardened_probe_git_env])
    def test_safe_directory_still_applies_to_a_foreign_owned_repo(self, tmp_path, factory):
        """GIT_TEST_ASSUME_DIFFERENT_OWNER makes git take its "dubious
        ownership" path without needing root. ``safe.directory`` lives ONLY in
        global/system config, so a probe that cannot read them fails rc=128 and
        the workspace snapshot silently goes blind."""
        foreign = tmp_path / "owned-by-someone-else"
        subprocess.run(["git", "init", "-q", str(foreign)], check=True, env=_CLEAN_ENV)
        subprocess.run(
            ["git", "-C", str(foreign), *_IDENT, "commit", "-q", "--allow-empty", "-m", "x"],
            check=True, capture_output=True, env=_CLEAN_ENV,
        )
        env = {**factory(self._global_config(tmp_path)),
               "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
        proc = subprocess.run(
            ["git", "-C", str(foreign), "rev-parse", "HEAD"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr

    def test_probe_env_pins_the_sinks_and_only_the_sinks(self):
        env = hardened_probe_git_env({})
        pins = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        assert pins["core.fsmonitor"] == "false"
        assert pins["core.hooksPath"] == os.devnull
        assert pins["diff.external"] == ""
        # NOT pinned: not an execution sink, and clearing it would let
        # globally-ignored file content into a model-facing diff payload.
        assert "core.excludesFile" not in pins
        # NOT pinned: a correctness-preserving cache, not a sink.
        assert "core.untrackedCache" not in pins
        # The operator's own config files stay selected by the operator.
        assert "GIT_CONFIG_GLOBAL" not in env
        assert "GIT_CONFIG_SYSTEM" not in env
        assert "GIT_CONFIG_NOSYSTEM" not in env

    def test_probe_env_drops_injected_config_parameters(self):
        env = hardened_probe_git_env({
            "GIT_CONFIG_PARAMETERS": "'core.fsmonitor=touch /tmp/x'",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "touch /tmp/x",
        })
        assert "GIT_CONFIG_PARAMETERS" not in env
        pins = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        assert pins["core.fsmonitor"] == "false"


# ---------------------------------------------------------------------------
# 4. Real-git E2E: the automatic probe paths neutralize the closed sinks
# ---------------------------------------------------------------------------


def test_baseline_unhardened_git_fires_sinks(malicious_repo):
    """RED control: without hardening the payload actually fires, so a GREEN
    below means something and a regression would be caught."""
    repo, marker = malicious_repo
    plain = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
    subprocess.run(["git", "-C", str(repo), "diff", "HEAD"], capture_output=True, env=plain)
    fired = _fired(marker)
    assert "fsmonitor" in fired and "extdiff" in fired, fired
    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                   capture_output=True, env=plain)
    assert "fsmonitor" in _fired(marker)


def test_key_n_overrides_alone_neutralize_the_local_config(malicious_repo):
    """The load-bearing precedence claim: ``GIT_CONFIG_KEY_N`` has command-line
    scope, so it beats the attacker's repo-local ``.git/config`` WITHOUT
    redirecting the operator's global/system config to /dev/null."""
    repo, marker = malicious_repo
    env = hardened_probe_git_env()
    assert "GIT_CONFIG_GLOBAL" not in env
    for args in (["diff", "--no-ext-diff", "--no-textconv", "HEAD"],
                 ["status", "--porcelain"],
                 ["worktree", "add", str(repo.parent / "wt-keyn"), "-b", "keyn"]):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env)
        assert _fired_closed(marker) == [], args


def test_coding_workspace_snapshot_is_safe(malicious_repo):
    import agent.coding_context as cc
    repo, marker = malicious_repo
    cc.build_coding_workspace_block(cwd=repo)
    assert _fired_closed(marker) == []


def test_gateway_git_probe_is_safe(malicious_repo):
    from tui_gateway import git_probe
    repo, marker = malicious_repo
    git_probe.branch(str(repo))
    git_probe.run_git(str(repo), "status", "--porcelain")
    assert _fired_closed(marker) == []


def test_working_diff_is_safe(malicious_repo):
    from tools.working_diff import collect_working_diff
    repo, marker = malicious_repo
    collect_working_diff(str(repo), "working")
    assert _fired_closed(marker) == []


def test_goals_fingerprint_is_safe(malicious_repo):
    from hermes_cli.goals import workspace_fingerprint
    repo, marker = malicious_repo
    workspace_fingerprint(str(repo))
    assert _fired_closed(marker) == []


def test_context_reference_diff_is_safe(malicious_repo):
    from agent import context_references as cr
    repo, marker = malicious_repo
    ref = type("R", (), {"raw": "@diff"})()
    cr._expand_git_reference(ref, repo, ["diff", "HEAD"], "git diff")
    assert _fired_closed(marker) == []


def test_subagent_worktree_add_is_safe(malicious_repo, tmp_path):
    from tools import subagent_worktree as sw
    repo, marker = malicious_repo
    sw._run_git(["worktree", "add", str(tmp_path / "wt1"), "-b", "safe1"], str(repo))
    assert _fired_closed(marker) == []


def test_web_git_diff_driver_is_neutralized(malicious_repo):
    """web_git keeps the operator's env (it commits and pushes), so only the
    ``harden_git_argv`` half applies there: the attribute-scoped diff driver
    must not fire. Its index-refresh sinks are pinned open below."""
    from hermes_cli import web_git
    repo, marker = malicious_repo
    web_git._git_out(str(repo), ["diff", "HEAD"])
    assert [s for s in _fired(marker) if s in ("extdiff", "textconv")] == []


# ---------------------------------------------------------------------------
# 5. Knowingly-open gaps, pinned as strict xfails
# ---------------------------------------------------------------------------

_FILTER_REASON = (
    "filter.<name>.clean/.smudge sink open on base "
    "ffb1d333edc55d30bbbbb5d7d0c5e36dc829a9e3; NOT closed by packet 11 — the "
    "driver name is attacker-chosen so no fixed GIT_CONFIG_KEY_N list can pin "
    "it, git has no --no-filters, and --numstat/--name-only/--raw/--stat were "
    "all measured to run the clean filter exactly like a full patch"
)


@pytest.mark.xfail(strict=True, reason=_FILTER_REASON)
def test_working_diff_filter_driver_does_not_fire(malicious_repo):
    from tools.working_diff import collect_working_diff
    repo, marker = malicious_repo
    collect_working_diff(str(repo), "working")
    assert "filterclean" not in _fired(marker)


@pytest.mark.xfail(strict=True, reason=_FILTER_REASON)
def test_raw_output_modes_avoid_the_filter(malicious_repo):
    """The obvious mitigation — ask git for names/counts instead of content —
    does not work: every worktree-comparing mode still runs the clean filter."""
    repo, marker = malicious_repo
    env = hardened_probe_git_env()
    for mode in ("--numstat", "--name-only", "--raw", "--stat"):
        subprocess.run(
            ["git", "-C", str(repo), "diff", "--no-ext-diff", "--no-textconv", mode],
            capture_output=True, env=env,
        )
        assert "filterclean" not in _fired(marker), mode


@pytest.mark.xfail(
    strict=True,
    reason="fork-only sink, out of packet-11 scope: hermes_cli/"
           "dashboard_codex_sessions.py::_collect_diff runs a bare `git diff "
           "origin/main...HEAD` with no hardened env and no --no-ext-diff",
)
def test_dashboard_codex_sessions_collect_diff_is_safe(tmp_path):
    from hermes_cli import dashboard_codex_sessions as dcs
    repo, marker = _make_malicious_repo(tmp_path, "dcs")
    dcs._collect_diff(str(repo))
    assert _fired_closed(marker) == []


@pytest.mark.xfail(
    strict=True,
    reason="fork-only sink, out of packet-11 scope: gateway/"
           "codex_session_dispatcher.py::_collect_diff runs a bare `git diff "
           "<base>...HEAD` with no hardened env and no --no-ext-diff",
)
def test_codex_session_dispatcher_collect_diff_is_safe(tmp_path):
    from gateway.codex_session_dispatcher import CodexSessionDispatcher
    repo, marker = _make_malicious_repo(tmp_path, "gcsd")
    stub = type("S", (), {"_base_branch": "origin/main"})()
    CodexSessionDispatcher._collect_diff(stub, Path(repo))
    assert _fired_closed(marker) == []


@pytest.mark.xfail(
    strict=True,
    reason="fork-only sink, out of packet-11 scope: agent/merge_broker.py "
           "fetch/rebase/checkout run with the inherited environment, so a "
           "repo-local core.hooksPath/fsmonitor still executes",
)
def test_merge_broker_rebase_is_safe(tmp_path):
    from agent.merge_broker import MergeBroker
    repo, marker = _make_malicious_repo(tmp_path, "mb", dirty=False, divergent_base=True)
    broker = MergeBroker(hermes_home=tmp_path, base_branch="main", base_remote="fork")
    try:
        broker._rebase_onto_base(Path(repo))
    except Exception:
        pass
    assert _fired_closed(marker) == []


@pytest.mark.xfail(
    strict=True,
    reason="web_git is the dashboard git MUTATION surface (commit/push/fetch), "
           "so it keeps the operator's env on purpose; core.fsmonitor / "
           "core.hooksPath therefore still fire on its index-refreshing calls",
)
def test_web_git_index_refresh_sinks_are_closed(malicious_repo):
    from hermes_cli import web_git
    repo, marker = malicious_repo
    web_git._git(str(repo), ["status", "--porcelain=v2", "-z"])
    assert _fired_closed(marker) == []
