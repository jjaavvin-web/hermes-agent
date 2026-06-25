from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import pytest
import yaml

from hermes_cli import provider_lane_canary as canary


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")


def _write_json(path: Path, payload: str = "{}\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _write_lock(path: Path) -> None:
    _write_yaml(
        path,
        {
            "lock": {
                "default_lane": {"provider": "openai-codex", "model": "gpt-5.5"},
                "premium_lane": {
                    "provider": "claude-cli-subprocess",
                    "models_allowed": ["claude-opus-4-8", "claude-via-cli"],
                    "disable_paid_api_fallback": True,
                },
            }
        },
    )


def _args(tmp_path: Path, hermes_home: Path) -> argparse.Namespace:
    lock = tmp_path / "provider-stack.lock.yaml"
    _write_lock(lock)
    doctrine = tmp_path / "PROVIDER-STACK.md"
    doctrine.write_text("provider-stack doctrine fixture\n", encoding="utf-8")
    authority = tmp_path / "authority-matrix.yaml"
    intent_router = tmp_path / "intent-router-v0.yaml"
    return argparse.Namespace(
        hermes_home=hermes_home,
        doctrine=doctrine,
        lock=lock,
        authority=authority,
        intent_router=intent_router,
        source_root=tmp_path / "missing-source-root",
        scan_root=[],
        no_source_scan=True,
        output_dir=tmp_path / "out",
    )


def _make_home(tmp_path: Path, provider: str, model: str) -> Path:
    hermes_home = tmp_path / "hermes-home"
    _write_yaml(
        hermes_home / "config.yaml",
        {
            "model": {"provider": "openai-codex", "default": "gpt-5.5"},
            "platform_toolsets": {"discord": ["terminal", "file"]},
        },
    )
    _write_yaml(
        hermes_home / "profiles" / "premium" / "config.yaml",
        {"model": {"provider": provider, "default": model}, "tools": {"enabled_toolsets": ["terminal"]}},
    )
    _write_json(hermes_home / "webhook_subscriptions.json")
    return hermes_home


def _collect_report(tmp_path: Path, hermes_home: Path) -> dict:
    args = _args(tmp_path, hermes_home)
    lock_doc, lanes = canary.collect_lanes(args)
    return canary.report_dict(args, lock_doc, lanes)


def _flag_rules(report: dict, lane_id: str) -> set[str]:
    lane = next(lane for lane in report["lanes"] if lane["lane_id"] == lane_id)
    return {check["rule_id"] for check in lane["checks"] if check["status"] == canary.FLAG}


def _forbidden_route_flags(report: dict) -> list[tuple[str, str]]:
    return [
        (lane["lane_id"], check["rule_id"])
        for lane in report["lanes"]
        for check in lane["checks"]
        if check["status"] == canary.FLAG
        and check["severity"] == "P0"
        and check["rule_id"] in canary.FORBIDDEN_ROUTE_RULE_IDS
    ]


def _advisory_flags(report: dict) -> list[tuple[str, str, str]]:
    return [
        (lane["lane_id"], check["severity"], check["rule_id"])
        for lane in report["lanes"]
        for check in lane["checks"]
        if check["status"] == canary.FLAG and (check["severity"] != "P0" or check["rule_id"] not in canary.FORBIDDEN_ROUTE_RULE_IDS)
    ]


def test_synthetic_anthropic_provider_on_claude_cli_lane_flags_p0(tmp_path: Path) -> None:
    hermes_home = _make_home(tmp_path, provider="anthropic", model="claude-via-cli")

    report = _collect_report(tmp_path, hermes_home)

    rules = _flag_rules(report, "profile:premium")
    assert "anthropic-provider-on-claude-cli-lane" in rules
    assert "native-anthropic-api-pin" in rules
    assert report["summary"]["status_counts"][canary.FLAG] >= 1
    assert report["summary"]["p0_flag_count"] >= 1


@pytest.mark.xfail(strict=True, reason="synthetic provider=anthropic on Claude CLI lane must fail the zero-forbidden-route gate")
def test_zero_forbidden_routes_gate_rejects_synthetic_anthropic_footgun(tmp_path: Path) -> None:
    hermes_home = _make_home(tmp_path, provider="anthropic", model="claude-via-cli")

    report = _collect_report(tmp_path, hermes_home)

    assert _forbidden_route_flags(report) == []


def test_correct_claude_cli_subprocess_profile_passes(tmp_path: Path) -> None:
    hermes_home = _make_home(tmp_path, provider="claude-cli-subprocess", model="claude-via-cli")

    report = _collect_report(tmp_path, hermes_home)

    assert _flag_rules(report, "profile:premium") == set()
    assert _forbidden_route_flags(report) == []
    assert report["summary"]["status_counts"][canary.FLAG] == 0
    assert report["summary"]["forbidden_route_flag_count"] == 0


def test_isolated_honcho_openrouter_profile_is_not_flagged(tmp_path: Path) -> None:
    hermes_home = _make_home(tmp_path, provider="openrouter", model="gpt-4.1-mini")
    (hermes_home / "profiles" / "premium").rename(hermes_home / "profiles" / "honcho-memory")

    report = _collect_report(tmp_path, hermes_home)

    assert _flag_rules(report, "profile:honcho-memory") == set()
    assert _forbidden_route_flags(report) == []
    assert report["summary"]["status_counts"][canary.FLAG] == 0
    assert report["summary"]["forbidden_route_flag_count"] == 0


def test_p2_auto_auxiliary_provider_is_advisory_not_forbidden_gate(tmp_path: Path) -> None:
    hermes_home = _make_home(tmp_path, provider="claude-cli-subprocess", model="claude-via-cli")
    config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    config["auxiliary"] = {"monitor": {"provider": "auto", "model": ""}}
    _write_yaml(hermes_home / "config.yaml", config)

    report = _collect_report(tmp_path, hermes_home)

    assert ("auxiliary:monitor", "P2", "explicit-provider") in _advisory_flags(report)
    assert _forbidden_route_flags(report) == []
    assert report["summary"]["forbidden_route_flag_count"] == 0


def test_live_profile_lane_config_has_zero_forbidden_routes(tmp_path: Path) -> None:
    hermes_home = canary.DEFAULT_HERMES_HOME
    if not (hermes_home / "config.yaml").is_file():
        pytest.skip(f"live Hermes config not present at {hermes_home}")

    args = argparse.Namespace(
        hermes_home=hermes_home,
        doctrine=canary.DEFAULT_DOCTRINE,
        lock=canary.DEFAULT_LOCK,
        authority=canary.DEFAULT_AUTHORITY,
        intent_router=canary.DEFAULT_INTENT_ROUTER,
        source_root=canary.DEFAULT_SOURCE_ROOT,
        scan_root=[],
        no_source_scan=True,
        output_dir=tmp_path / "live-canary-out",
    )
    lock_doc, lanes = canary.collect_lanes(args)
    report = canary.report_dict(args, lock_doc, lanes)

    advisories = _advisory_flags(report)
    if advisories:
        warnings.warn(f"provider lane canary advisory flags tolerated by P0 gate: {advisories}", UserWarning)

    assert _forbidden_route_flags(report) == []
    assert report["summary"]["forbidden_route_flag_count"] == 0
