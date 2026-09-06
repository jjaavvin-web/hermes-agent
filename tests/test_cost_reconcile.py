from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import yaml

from hermes_cli import cost_reconcile


def _write_lock(path: Path) -> dict:
    policy = {
        "default_lane": {"provider": "openai-codex", "model": "gpt-5.5", "billing": "subscription_included"},
        "premium_lane": {
            "provider": "claude-cli-subprocess",
            "models_allowed": ["claude-opus-4-8"],
            "disable_paid_api_fallback": True,
        },
        "vision_aux": {"provider": "openai-codex", "model": "gpt-5.5"},
        "honcho_memory": {
            "provider": "openrouter-prepaid",
            "coupling": "isolated",
            "models": ["gpt-4.1-mini", "text-embedding-3-small"],
        },
        "forbidden": [
            "native_anthropic_api_pins",
            "openrouter_fallback_providers",
            "gemini_preview_routes",
            "anthropic_oauth_spoof_provider",
            "bare_claude_p",
        ],
    }
    path.write_text(yaml.safe_dump({"lock": policy}, sort_keys=True), encoding="utf-8")
    return policy


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            model TEXT,
            model_config TEXT,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            started_at REAL,
            ended_at REAL
        );
        CREATE TABLE turn_usage (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT,
            ts REAL,
            provider TEXT,
            model TEXT,
            prompt_version TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            total_tokens INTEGER,
            estimated_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            latency_ms INTEGER
        );
        """
    )
    now = time.time()
    sessions = [
        (
            "s-default",
            "discord",
            "gpt-5.5",
            "{}",
            "openai-codex",
            None,
            "subscription_included",
            0.0,
            0.0,
            "included",
            "none",
            now - 100,
            now - 50,
        ),
        (
            "s-paid-anthropic",
            "webhook",
            "claude-opus-4-8",
            "{}",
            "anthropic",
            None,
            None,
            1.25,
            None,
            "estimated",
            "provider_models_api",
            now - 100,
            now - 50,
        ),
        (
            "s-honcho",
            "honcho-memory",
            "gpt-4.1-mini",
            "{}",
            "openrouter-prepaid",
            None,
            None,
            0.04,
            None,
            "estimated",
            "provider_models_api",
            now - 100,
            now - 50,
        ),
        (
            "s-local",
            "cli",
            "llama-local",
            "{}",
            "openai-compat",
            "http://localhost:11434/v1",
            None,
            0.0,
            0.0,
            "unknown",
            "none",
            now - 100,
            now - 50,
        ),
    ]
    conn.executemany(
        """
        INSERT INTO sessions (
            id, source, model, model_config, billing_provider, billing_base_url,
            billing_mode, estimated_cost_usd, actual_cost_usd, cost_status,
            cost_source, started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        sessions,
    )
    turns = [
        ("t-default", "s-default", now - 10, "openai-codex", "gpt-5.5", "p", 10, 20, 1, 2, 3, 36, 0.0, "included", "none", 100),
        (
            "t-paid-anthropic",
            "s-paid-anthropic",
            now - 9,
            "anthropic",
            "claude-opus-4-8",
            "p",
            100,
            200,
            0,
            0,
            50,
            350,
            1.25,
            "estimated",
            "provider_models_api",
            100,
        ),
        (
            "t-honcho",
            "s-honcho",
            now - 8,
            "openrouter",
            "gpt-4.1-mini",
            "honcho",
            5,
            5,
            0,
            0,
            0,
            10,
            0.04,
            "estimated",
            "provider_models_api",
            100,
        ),
        ("t-local", "s-local", now - 7, "openai-compat", "llama-local", "p", 1, 2, 0, 0, 0, 3, 0.0, "unknown", "none", 100),
        ("t-orphan", "missing-session", now - 6, "", "unknown", "p", 1, 1, 0, 0, 0, 2, 0.0, "unknown", "none", 100),
    ]
    conn.executemany(
        """
        INSERT INTO turn_usage (
            turn_id, session_id, ts, provider, model, prompt_version,
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            reasoning_tokens, total_tokens, estimated_cost_usd, cost_status,
            cost_source, latency_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        turns,
    )
    conn.commit()
    return conn


def test_reconciliation_classifies_modes_and_flags_only_true_violation(tmp_path: Path) -> None:
    lock_path = tmp_path / "provider-stack.lock.yaml"
    policy = _write_lock(lock_path)
    db_path = tmp_path / "state.db"
    conn = _make_db(db_path)
    since_ts = time.time() - 86400

    rows = {row["turn_id"]: row for row in cost_reconcile._joined_turn_rows(conn, since_ts)}
    assert cost_reconcile.classify_billing_mode(rows["t-default"], policy) == "subscription_included"
    assert cost_reconcile.classify_billing_mode(rows["t-paid-anthropic"], policy) == "paid_api"
    assert cost_reconcile.classify_billing_mode(rows["t-honcho"], policy) == "paid_api"
    assert cost_reconcile.classify_billing_mode(rows["t-local"], policy) == "local_free"
    assert cost_reconcile.classify_billing_mode(rows["t-orphan"], policy) == "local_free"

    report = cost_reconcile.build_reconciliation(window_days=1, db_path=db_path, lock_path=lock_path)

    assert report["dbPath"].endswith("state.db")
    assert report["byBillingMode"]["subscription_included"]["turns"] == 1
    assert report["byBillingMode"]["paid_api"]["turns"] == 2
    assert report["byBillingMode"]["local_free"]["turns"] == 2
    assert report["violationCount"] == 1
    assert report["violationCostUsd"] == 1.25
    assert [item["turn_id"] for item in report["paidFallbackViolations"]] == ["t-paid-anthropic"]
    assert report["paidFallbackViolations"][0]["why"] == "native_anthropic_api_pins"
    assert "t-honcho" not in {item["turn_id"] for item in report["paidFallbackViolations"]}


def test_budget_rollup_includes_orphan_without_crashing(tmp_path: Path) -> None:
    lock_path = tmp_path / "provider-stack.lock.yaml"
    policy = _write_lock(lock_path)
    conn = _make_db(tmp_path / "state.db")

    rollup = cost_reconcile.budget_rollup(conn, time.time() - 86400, policy)

    modes = {(row["lane_key"], row["billing_provider"], row["billing_mode"]): row for row in rollup}
    assert modes[("default", "openai-codex", "subscription_included")]["turns"] == 1
    assert modes[("premium", "anthropic", "paid_api")]["estimated_cost_usd"] == 1.25
    assert modes[("honcho", "openrouter-prepaid", "paid_api")]["turns"] == 1
    assert modes[("other", "unknown", "local_free")]["turns"] == 1
