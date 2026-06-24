import json
from pathlib import Path

import hermes_cli.dashboard_learning as learning


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _seed_home(home: Path) -> None:
    _write_json(home / "state/learning-index/snapshot-latest.json", {
        "source": "fixture mvms read-only",
        "generated_at": "2026-06-23T00:00:00Z",
        "lessons_total": 522,
        "trusted_count": 9,
        "trusted_ratio": 0.017241,
        "actionable_lessons_total": 116,
        "trusted_actionable_ratio": 0.077586,
        "auto_bridged_count": 399,
        "quarantine_count": 6,
        "dup_ratio": 0.505747,
        "importance_hist": {"2": 454, "3": 53},
        "embed_coverage": {"lesson": {"embedded": 522, "total": 522, "ratio": 1.0}},
    })
    _write_jsonl(home / "evals/recall/score-history.jsonl", [
        {
            "ts": "2026-06-23T07:01:37Z",
            "holdout_file": str(home / "evals/recall/holdout.jsonl"),
            "agg": {"k": 10, "n": 30, "n_target_resolved": 30, "recall_at_k": 0.9667, "mrr": 0.9011, "ndcg_at_k": 0.9173},
        },
        {
            "ts": "2026-06-22T18:04:36Z",
            "holdout_file": str(home / "evals/recall/holdout_wave2.jsonl"),
            "agg": {"k": 10, "n": 18, "n_target_resolved": 18, "recall_at_k": 0.6667, "mrr": 0.5648, "ndcg_at_k": 0.5906},
        },
    ])
    _write_jsonl(home / "state/learning-index/recall-events.jsonl", [
        {"ts": 100.0, "source": "warm-service", "n_lessons": 5},
        {"ts": 3700.0, "source": "warm-service", "n_lessons": 5},
        {"ts": 90_000.0, "source": "warm-service", "n_lessons": 5},
    ])
    _write_json(home / "state/learning-loop/promote-ready-latest.md", {
        "at": "2026-06-23T15:25:31Z",
        "processed": 0,
        "store_writes": 0,
        "queue_sha256": "abc123",
    })
    (home / "state/learning-loop/promote-ready-latest.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (home / "state/learning-loop/promote-ready-latest.jsonl").write_text("", encoding="utf-8")
    (home / "state/learning-loop/learning-loop-promote.log").write_text("ok\n", encoding="utf-8")
    (home / "state/learning-loop/verify-latest.log").write_text(
        "critic PASS hard_failures=0 report=/tmp/critic.md\n"
        "OK recall@10=0.9667 mrr=0.9011 ndcg@10=0.9173 resolved=30/30\n",
        encoding="utf-8",
    )
    _write_json(home / "state/learning-canary/result-latest.json", {
        "pass": True,
        "rank": 1,
        "recalled": True,
        "avoided_mistake": False,
        "probe_ranker": "recall_at_dispatch.recall",
        "probe_mode_used": "production-recall",
        "ts": "2026-06-23T07:01:24Z",
    })


def test_learning_snapshot_surfaces_blind_recall_not_self_seeded(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    _seed_home(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(learning, "_systemd_unit", lambda name: {"name": name, "enabled": "enabled", "active": "active"})
    monkeypatch.setattr(learning.time, "time", lambda: 90_100.0)
    learning._CACHE = None

    payload = learning.get_learning_snapshot()

    assert payload["status"] == "green"
    assert payload["recall_eval"]["label"] == "blind held-out RECALL@10 (not self-seeded)"
    assert payload["recall_eval"]["recall_at_k"] == 0.6667
    assert payload["recall_eval"]["holdout_file"].endswith("holdout_wave2.jsonl")
    assert payload["recall_eval"]["self_seeded_latest"]["recall_at_k"] == 0.9667
    assert payload["recall_eval"]["self_seeded_latest"]["classification"] == "self-seeded-or-default"


def test_learning_snapshot_counts_real_recall_events_and_artifacts(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    _seed_home(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(learning, "_systemd_unit", lambda name: {"name": name, "enabled": "enabled", "active": "active"})
    monkeypatch.setattr(learning.time, "time", lambda: 90_100.0)
    learning._CACHE = None

    payload = learning.get_learning_snapshot()

    activity = payload["recall_activity"]
    assert activity["source"].endswith("state/learning-index/recall-events.jsonl")
    assert activity["total_events"] == 3
    assert activity["recent_1h"] == 1
    assert activity["recent_24h"] == 2
    assert activity["latest_age_seconds"] == 100.0
    assert payload["promotion"]["latest"]["processed"] == 0
    assert payload["promotion"]["timer"]["active"] == "active"
    assert payload["verify"]["critic_status"] == "PASS"
    assert payload["verify"]["default_recall_at_k"] == 0.9667
    assert payload["mvms_lessons"]["lessons_total"] == 522


def test_learning_snapshot_reads_real_history_tail(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    _seed_home(home)
    _write_jsonl(home / "state/learning-index/history.jsonl", [
        {
            "generated_at": "2026-06-23T00:00:00Z",
            "lessons_total": 520,
            "trusted_count": 8,
            "trusted_ratio": 0.015384,
            "dup_ratio": 0.50,
            "actionable_lessons_total": 115,
            "quarantine_count": 5,
            "SIGNAL_SCORE": 8,
            "embed_coverage": {"large": "omitted by projection"},
        },
        {
            "generated_at": "2026-06-24T00:00:00Z",
            "lessons_total": 522,
            "trusted_count": 9,
            "trusted_ratio": 0.017241,
            "dup_ratio": 0.505747,
            "actionable_lessons_total": 116,
            "quarantine_count": 6,
            "SIGNAL_SCORE": 9,
            "importance_hist": {"large": "omitted by projection"},
        },
    ])
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(learning, "_systemd_unit", lambda name: {"name": name, "enabled": "enabled", "active": "active"})
    monkeypatch.setattr(learning.time, "time", lambda: 90_100.0)
    learning._CACHE = None

    payload = learning.get_learning_snapshot()

    assert payload["history_tail_count"] >= 1
    assert len(payload["history_tail"]) >= 1
    assert "lessons_total" in payload["history_tail"][-1]
    assert payload["history_tail"][-1]["lessons_total"] == 522
    assert "embed_coverage" not in payload["history_tail"][-1]
    assert "importance_hist" not in payload["history_tail"][-1]
    assert payload["history"]["status"] == "measured"
    assert payload["history"]["source"].endswith("state/learning-index/history.jsonl")


def test_learning_snapshot_marks_absent_history_unmeasured(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    _seed_home(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(learning, "_systemd_unit", lambda name: {"name": name, "enabled": "enabled", "active": "active"})
    monkeypatch.setattr(learning.time, "time", lambda: 90_100.0)
    learning._CACHE = None

    payload = learning.get_learning_snapshot()

    assert payload["history_tail"] == []
    assert payload["history_tail_count"] == 0
    assert payload["history"]["status"] == "unmeasured"
    assert payload["history"]["source"].endswith("state/learning-index/history.jsonl")
