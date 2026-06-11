from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from hermes_cli import secrets_cli


FAKE_TOKEN = "0.FAKE_SUPER_SECRET_TOKEN"
FAKE_SECRET_VALUE = "sk-FAK...CRET"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CUSTOM_TOKEN_ENV", raising=False)
    monkeypatch.delenv("BWS_SERVER_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EXISTING_KEY", raising=False)
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.setattr(secrets_cli, "get_env_path", lambda: tmp_path / ".env")


@pytest.fixture
def config_store(monkeypatch):
    store = {"config": {}}
    saved = []
    env_saved = []

    monkeypatch.setattr(secrets_cli, "load_config", lambda: store["config"])
    monkeypatch.setattr(secrets_cli, "save_config", lambda cfg: saved.append(cfg.copy()))
    monkeypatch.setattr(
        secrets_cli,
        "save_env_value",
        lambda key, value: env_saved.append((key, value)),
    )
    return store, saved, env_saved


def _ns(**kwargs):
    return argparse.Namespace(**kwargs)


def test_register_cli_wires_bitwarden_subcommands_and_defaults():
    parser = argparse.ArgumentParser(prog="hermes secrets bitwarden")

    secrets_cli.register_cli(parser)

    setup = parser.parse_args(
        [
            "setup",
            "--project-id",
            "proj-123",
            "--access-token",
            FAKE_TOKEN,
            "--server-url",
            "https://vault.bitwarden.eu",
        ]
    )
    assert setup.func is secrets_cli.cmd_setup
    assert setup.project_id == "proj-123"
    assert setup.access_token == FAKE_TOKEN
    assert setup.server_url == "https://vault.bitwarden.eu"
    assert parser.parse_args(["status"]).func is secrets_cli.cmd_status
    assert parser.parse_args(["sync", "--apply"]).func is secrets_cli.cmd_sync
    assert parser.parse_args(["sync", "--apply"]).apply is True
    assert parser.parse_args(["disable"]).func is secrets_cli.cmd_disable
    install = parser.parse_args(["install", "--force"])
    assert install.func is secrets_cli.cmd_install
    assert install.force is True


def test_cmd_setup_happy_path_saves_config_without_echoing_plaintext_secrets(
    config_store, monkeypatch, tmp_path, capsys
):
    store, saved_configs, env_saved = config_store
    fake_binary = tmp_path / "bws"
    monkeypatch.setattr(secrets_cli.bw, "find_bws", lambda install_if_missing=False: fake_binary)
    monkeypatch.setattr(secrets_cli, "_bws_version", lambda binary: "bws 1.2.3")
    monkeypatch.setattr(
        secrets_cli.bw,
        "fetch_bitwarden_secrets",
        lambda **kwargs: ({"OPENAI_API_KEY": FAKE_SECRET_VALUE}, ["unicode warning ✓"]),
    )

    rc = secrets_cli.cmd_setup(
        _ns(
            access_token=f"  {FAKE_TOKEN}  ",
            project_id="  proj-123  ",
            server_url="https://vault.bitwarden.eu",
        )
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert env_saved == [("BWS_ACCESS_TOKEN", FAKE_TOKEN)]
    assert saved_configs == [store["config"]]
    assert store["config"]["secrets"]["bitwarden"] == {
        "enabled": True,
        "project_id": "proj-123",
        "server_url": "https://vault.bitwarden.eu",
        "access_token_env": "BWS_ACCESS_TOKEN",
        "cache_ttl_seconds": 300,
        "override_existing": True,
        "auto_install": True,
    }
    assert "OPENAI_API_KEY" in out
    assert "unicode warning" in out
    assert FAKE_TOKEN not in out
    assert FAKE_SECRET_VALUE not in out


def test_cmd_setup_downloads_missing_binary_then_prompts_for_project(monkeypatch, config_store, tmp_path):
    fake_binary = tmp_path / "managed-bws"
    calls = {"install": 0, "fetch": None}
    inputs = iter(["1", "2"])

    monkeypatch.setattr(secrets_cli.bw, "find_bws", lambda install_if_missing=False: None)

    def fake_install_bws():
        calls["install"] += 1
        return fake_binary

    monkeypatch.setattr(secrets_cli.bw, "install_bws", fake_install_bws)
    monkeypatch.setattr(secrets_cli, "_bws_version", lambda binary: "bws 9.9.9")
    monkeypatch.setattr(secrets_cli.Console, "input", lambda self, prompt="": next(inputs))
    monkeypatch.setattr(
        secrets_cli,
        "_list_projects",
        lambda binary, token, console, *, server_url="": [
            {"name": "One", "id": "proj-one"},
            {"name": "Two", "id": "proj-two"},
        ],
    )

    def fake_fetch(**kwargs):
        calls["fetch"] = kwargs
        return ({}, [])

    monkeypatch.setattr(secrets_cli.bw, "fetch_bitwarden_secrets", fake_fetch)

    rc = secrets_cli.cmd_setup(
        _ns(access_token=FAKE_TOKEN, project_id="", server_url="")
    )

    assert rc == 0
    assert calls["install"] == 1
    assert calls["fetch"]["project_id"] == "proj-two"
    assert calls["fetch"]["server_url"] == ""


def test_cmd_setup_error_paths_do_not_save_config(config_store, monkeypatch, tmp_path, capsys):
    store, saved_configs, env_saved = config_store
    fake_binary = tmp_path / "bws"
    monkeypatch.setattr(secrets_cli, "_bws_version", lambda binary: "bws fake")

    monkeypatch.setattr(secrets_cli.bw, "find_bws", lambda install_if_missing=False: None)
    monkeypatch.setattr(secrets_cli.bw, "install_bws", mock.Mock(side_effect=RuntimeError("boom")))
    assert secrets_cli.cmd_setup(_ns(access_token=FAKE_TOKEN, project_id="p", server_url="")) == 1

    monkeypatch.setattr(secrets_cli.bw, "find_bws", lambda install_if_missing=False: fake_binary)
    monkeypatch.setattr(secrets_cli, "masked_secret_prompt", lambda prompt: "")
    assert secrets_cli.cmd_setup(_ns(access_token="", project_id="p", server_url="")) == 1

    monkeypatch.setattr(secrets_cli, "masked_secret_prompt", lambda prompt: FAKE_TOKEN)
    monkeypatch.setattr(secrets_cli, "_resolve_server_url", lambda args, cfg, console: None)
    assert secrets_cli.cmd_setup(_ns(access_token="", project_id="p", server_url="")) == 1

    monkeypatch.setattr(secrets_cli, "_resolve_server_url", lambda args, cfg, console: "")
    monkeypatch.setattr(secrets_cli, "_list_projects", lambda *a, **kw: [])
    assert secrets_cli.cmd_setup(_ns(access_token=FAKE_TOKEN, project_id="", server_url="")) == 1

    monkeypatch.setattr(secrets_cli, "_list_projects", lambda *a, **kw: None)
    assert secrets_cli.cmd_setup(_ns(access_token=FAKE_TOKEN, project_id="", server_url="")) == 1

    monkeypatch.setattr(
        secrets_cli.bw,
        "fetch_bitwarden_secrets",
        mock.Mock(side_effect=RuntimeError("fetch failed without token")),
    )
    assert secrets_cli.cmd_setup(_ns(access_token=FAKE_TOKEN, project_id="p", server_url="")) == 1

    assert saved_configs == []
    # Token may be stored before late setup failures so the fetch can authenticate,
    # but the plaintext token must not be printed to stdout.
    assert FAKE_TOKEN not in capsys.readouterr().out
    assert store["config"].get("secrets", {}).get("bitwarden", {}).get("enabled") is not True
    assert all(value == FAKE_TOKEN for _, value in env_saved)


def test_cmd_status_reports_configuration_without_echoing_token_value(
    config_store, monkeypatch, capsys
):
    store, _, _ = config_store
    store["config"] = {
        "secrets": {
            "bitwarden": {
                "enabled": True,
                "access_token_env": "CUSTOM_TOKEN_ENV",
                "project_id": "",
                "server_url": "https://vault.bitwarden.eu",
                "override_existing": False,
                "cache_ttl_seconds": 17,
                "auto_install": False,
            }
        }
    }
    monkeypatch.setenv("CUSTOM_TOKEN_ENV", FAKE_TOKEN)
    monkeypatch.setattr(secrets_cli.bw, "find_bws", lambda install_if_missing=False: None)

    rc = secrets_cli.cmd_status(_ns())

    out = capsys.readouterr().out
    assert rc == 0
    assert "CUSTOM_TOKEN_ENV" in out
    assert "https://vault.bitwarden.eu" in out
    assert "Enabled" in out
    assert "no project_id" in out
    assert FAKE_TOKEN not in out


def test_cmd_status_disabled_and_missing_token_warnings(config_store, monkeypatch, capsys):
    store, _, _ = config_store
    monkeypatch.setattr(secrets_cli.bw, "find_bws", lambda install_if_missing=False: Path("/fake/bws"))
    monkeypatch.setattr(secrets_cli, "_bws_version", lambda binary: "bws fake")

    assert secrets_cli.cmd_status(_ns()) == 0
    disabled_out = capsys.readouterr().out
    assert "Run" in disabled_out and "setup" in disabled_out

    store["config"] = {
        "secrets": {
            "bitwarden": {
                "enabled": True,
                "access_token_env": "CUSTOM_TOKEN_ENV",
                "project_id": "proj-123",
            }
        }
    }
    assert secrets_cli.cmd_status(_ns()) == 0
    missing_token_out = capsys.readouterr().out
    assert "CUSTOM_TOKEN_ENV is not set" in missing_token_out


def test_cmd_sync_dry_run_apply_and_bootstrap_skip_do_not_print_secret_values(
    config_store, monkeypatch, capsys
):
    store, _, _ = config_store
    store["config"] = {
        "secrets": {
            "bitwarden": {
                "enabled": True,
                "access_token_env": "BWS_ACCESS_TOKEN",
                "project_id": "proj-123",
                "server_url": "https://vault.bitwarden.eu",
                "override_existing": False,
            }
        }
    }
    monkeypatch.setenv("BWS_ACCESS_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("EXISTING_KEY", "already-here")
    monkeypatch.setattr(
        secrets_cli.bw,
        "fetch_bitwarden_secrets",
        lambda **kwargs: (
            {
                "BWS_ACCESS_TOKEN": "0.SHOULD_NOT_OVERRIDE",
                "EXISTING_KEY": "replacement-secret",
                "NEW_KEY": FAKE_SECRET_VALUE,
            },
            ["safe warning"],
        ),
    )

    assert secrets_cli.cmd_sync(_ns(apply=False)) == 0
    dry_out = capsys.readouterr().out
    assert "dry-run" in dry_out
    assert "BWS_ACCESS_TOKEN" in dry_out
    assert "EXISTING_KEY" in dry_out
    assert "NEW_KEY" in dry_out
    assert "NEW_KEY" not in os.environ
    assert os.environ["EXISTING_KEY"] == "already-here"
    assert FAKE_TOKEN not in dry_out
    assert FAKE_SECRET_VALUE not in dry_out
    assert "replacement-secret" not in dry_out
    assert "0.SHOULD_NOT_OVERRIDE" not in dry_out

    assert secrets_cli.cmd_sync(_ns(apply=True)) == 0
    apply_out = capsys.readouterr().out
    assert os.environ["NEW_KEY"] == FAKE_SECRET_VALUE
    assert os.environ["EXISTING_KEY"] == "replacement-secret"
    assert os.environ["BWS_ACCESS_TOKEN"] == FAKE_TOKEN
    assert "Exported 2 secret" in apply_out
    assert FAKE_TOKEN not in apply_out
    assert FAKE_SECRET_VALUE not in apply_out
    assert "replacement-secret" not in apply_out
    assert "0.SHOULD_NOT_OVERRIDE" not in apply_out


def test_cmd_sync_error_and_empty_paths(config_store, monkeypatch, capsys):
    store, _, _ = config_store

    assert secrets_cli.cmd_sync(_ns(apply=False)) == 1
    assert "disabled" in capsys.readouterr().out

    store["config"] = {"secrets": {"bitwarden": {"enabled": True, "project_id": "p"}}}
    assert secrets_cli.cmd_sync(_ns(apply=False)) == 1
    assert "BWS_ACCESS_TOKEN is not set" in capsys.readouterr().out

    monkeypatch.setenv("BWS_ACCESS_TOKEN", FAKE_TOKEN)
    store["config"] = {"secrets": {"bitwarden": {"enabled": True, "project_id": ""}}}
    assert secrets_cli.cmd_sync(_ns(apply=False)) == 1
    assert "No project_id configured" in capsys.readouterr().out

    store["config"] = {"secrets": {"bitwarden": {"enabled": True, "project_id": "p"}}}
    monkeypatch.setattr(
        secrets_cli.bw,
        "fetch_bitwarden_secrets",
        mock.Mock(side_effect=RuntimeError("offline fake failure")),
    )
    assert secrets_cli.cmd_sync(_ns(apply=False)) == 1
    assert "offline fake failure" in capsys.readouterr().out

    monkeypatch.setattr(secrets_cli.bw, "fetch_bitwarden_secrets", lambda **kw: ({}, []))
    assert secrets_cli.cmd_sync(_ns(apply=False)) == 0
    assert "No secrets in project" in capsys.readouterr().out


def test_cmd_disable_only_flips_enabled_flag(config_store, capsys):
    store, saved_configs, _ = config_store
    store["config"] = {
        "secrets": {"bitwarden": {"enabled": True, "project_id": "proj-123"}}
    }

    assert secrets_cli.cmd_disable(_ns()) == 0

    assert store["config"]["secrets"]["bitwarden"] == {
        "enabled": False,
        "project_id": "proj-123",
    }
    assert saved_configs == [store["config"]]
    assert "access token is left in .env" in capsys.readouterr().out


def test_cmd_install_success_and_failure(monkeypatch, tmp_path, capsys):
    fake_binary = tmp_path / "bws"
    install_mock = mock.Mock(return_value=fake_binary)
    monkeypatch.setattr(secrets_cli.bw, "install_bws", install_mock)
    monkeypatch.setattr(secrets_cli, "_bws_version", lambda binary: "bws fake")

    assert secrets_cli.cmd_install(_ns(force=True)) == 0
    install_mock.assert_called_once_with(force=True)
    assert str(fake_binary) in capsys.readouterr().out

    monkeypatch.setattr(
        secrets_cli.bw,
        "install_bws",
        mock.Mock(side_effect=RuntimeError("download failed")),
    )
    assert secrets_cli.cmd_install(_ns(force=False)) == 1
    assert "download failed" in capsys.readouterr().out


def test_yn_returns_rich_yes_no_markup():
    assert secrets_cli._yn(True) == "[green]yes[/green]"
    assert secrets_cli._yn(False) == "[dim]no[/dim]"


def test_bws_version_uses_stdout_stderr_and_handles_failures(monkeypatch, tmp_path):
    binary = tmp_path / "bws"

    monkeypatch.setattr(
        secrets_cli.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="bws 1.0\nextra", stderr=""),
    )
    assert secrets_cli._bws_version(binary) == "bws 1.0"

    monkeypatch.setattr(
        secrets_cli.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="", stderr="bws 2.0\n"),
    )
    assert secrets_cli._bws_version(binary) == "bws 2.0"

    monkeypatch.setattr(
        secrets_cli.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="bad", stderr="worse"),
    )
    assert secrets_cli._bws_version(binary) == "version unknown"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="bws", timeout=5)

    monkeypatch.setattr(secrets_cli.subprocess, "run", timeout)
    assert secrets_cli._bws_version(binary) == "version unknown"


def test_list_projects_success_filters_payload_sets_server_env_and_hides_token(
    monkeypatch, tmp_path, capsys
):
    binary = tmp_path / "bws"
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout='[{"id":"p1","name":"One"},{"name":"missing id"},"bad",{"id":"p2"}]',
            stderr="",
        )

    monkeypatch.setattr(secrets_cli.subprocess, "run", fake_run)

    projects = secrets_cli._list_projects(
        binary,
        FAKE_TOKEN,
        secrets_cli.Console(),
        server_url="https://vault.bitwarden.eu",
    )

    assert projects == [{"id": "p1", "name": "One"}, {"id": "p2"}]
    assert captured["cmd"] == [str(binary), "project", "list", "--output", "json"]
    assert captured["env"]["BWS_ACCESS_TOKEN"] == FAKE_TOKEN
    assert captured["env"]["BWS_SERVER_URL"] == "https://vault.bitwarden.eu"
    assert FAKE_TOKEN not in capsys.readouterr().out


def test_list_projects_handles_process_errors_non_json_and_non_list(monkeypatch, tmp_path, capsys):
    binary = tmp_path / "bws"
    console = secrets_cli.Console()

    def raises_oserror(*args, **kwargs):
        raise OSError("missing binary")

    monkeypatch.setattr(secrets_cli.subprocess, "run", raises_oserror)
    assert secrets_cli._list_projects(binary, FAKE_TOKEN, console) is None
    assert "missing binary" in capsys.readouterr().out

    monkeypatch.setattr(
        secrets_cli.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1, stdout="", stderr="invalid_client 400 bad request"
        ),
    )
    assert secrets_cli._list_projects(binary, FAKE_TOKEN, console) is None
    err_out = capsys.readouterr().out
    assert "different Bitwarden region" in err_out
    assert FAKE_TOKEN not in err_out

    monkeypatch.setattr(
        secrets_cli.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="{not json", stderr=""),
    )
    assert secrets_cli._list_projects(binary, FAKE_TOKEN, console) is None
    assert "non-JSON" in capsys.readouterr().out

    monkeypatch.setattr(
        secrets_cli.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout='{"id":"not-a-list"}', stderr=""),
    )
    assert secrets_cli._list_projects(binary, FAKE_TOKEN, console) == []


def test_resolve_server_url_noninteractive_sources(monkeypatch):
    console = mock.Mock()
    assert (
        secrets_cli._resolve_server_url(
            _ns(server_url="  https://vault.example.com  "), {}, console
        )
        == "https://vault.example.com"
    )

    monkeypatch.setenv("BWS_SERVER_URL", "https://vault.from-env.example")
    assert (
        secrets_cli._resolve_server_url(_ns(server_url=""), {}, secrets_cli.Console())
        == "https://vault.from-env.example"
    )


def test_resolve_server_url_interactive_keep_preset_custom_and_abort(monkeypatch, capsys):
    existing_console = mock.Mock()
    existing_console.input.return_value = ""
    assert (
        secrets_cli._resolve_server_url(
            _ns(server_url=""), {"server_url": "https://existing.example"}, existing_console
        )
        == "https://existing.example"
    )

    eu_console = mock.Mock()
    eu_console.input.side_effect = ["bad", "99", "2"]
    assert secrets_cli._resolve_server_url(_ns(server_url=""), {}, eu_console) == "https://vault.bitwarden.eu"
    assert eu_console.print.call_count >= 2

    custom_console = mock.Mock()
    custom_console.input.side_effect = ["3", "vault.local"]
    assert secrets_cli._resolve_server_url(_ns(server_url=""), {}, custom_console) == "vault.local"
    assert any("doesn't start" in str(call) for call in custom_console.print.call_args_list)

    abort_console = mock.Mock()
    abort_console.input.side_effect = ["3", ""]
    assert secrets_cli._resolve_server_url(_ns(server_url=""), {}, abort_console) is None
    assert any("Empty URL" in str(call) for call in abort_console.print.call_args_list)
    assert FAKE_TOKEN not in capsys.readouterr().out
