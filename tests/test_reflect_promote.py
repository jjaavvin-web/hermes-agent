from __future__ import annotations

import json
from pathlib import Path

from agent.reflect_promote import (
    ReflectCandidate,
    approve_candidate,
    list_candidates,
    reject_candidate,
)


def _write_queue(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_reflect_drainer_surfaces_candidates_without_auto_writing(tmp_path):
    queue = tmp_path / "state" / "reflect-queue.jsonl"
    writer_calls: list[ReflectCandidate] = []
    _write_queue(
        queue,
        [
            {
                "id": "lesson-1",
                "project": "hermes",
                "situation": "Goal resume",
                "mistake_or_insight": "Durable anchors beat memory.",
                "correction": "Cross-check git before ledger trust.",
                "source": "dream-reflect",
                "created_at": "2026-05-29T10:00:00Z",
                "tags": ["ops"],
            }
        ],
    )

    candidates = list_candidates(queue_path=queue, writer=lambda c: writer_calls.append(c))

    assert [candidate.id for candidate in candidates] == ["lesson-1"]
    assert candidates[0].status == "pending"
    assert writer_calls == []


def test_reflect_candidate_promotes_only_on_explicit_approve(tmp_path):
    queue = tmp_path / "reflect-queue.jsonl"
    writer_calls: list[ReflectCandidate] = []
    _write_queue(
        queue,
        [
            {
                "id": "lesson-approve",
                "project": "hermes",
                "situation": "Review gate",
                "mistake_or_insight": "Opus print path needs terse prompts.",
                "correction": "Use minified JSON review prompts.",
            }
        ],
    )

    listed = list_candidates(queue_path=queue, writer=lambda c: writer_calls.append(c))
    assert len(listed) == 1
    assert writer_calls == []

    result = approve_candidate(
        "lesson-approve",
        queue_path=queue,
        writer=lambda c: writer_calls.append(c),
    )

    assert result.status == "promoted"
    assert [candidate.id for candidate in writer_calls] == ["lesson-approve"]
    assert list_candidates(queue_path=queue, include_resolved=True)[0].status == "promoted"


def test_reflect_candidate_reject_leaves_it_unpromoted(tmp_path):
    queue = tmp_path / "reflect-queue.jsonl"
    writer_calls: list[ReflectCandidate] = []
    _write_queue(
        queue,
        [
            {
                "id": "lesson-reject",
                "project": "hermes",
                "situation": "Noise",
                "mistake_or_insight": "Temporary one-off status is not durable.",
                "correction": "Skip it.",
            }
        ],
    )

    result = reject_candidate(
        "lesson-reject",
        queue_path=queue,
        writer=lambda c: writer_calls.append(c),
        reason="not durable",
    )

    assert result.status == "rejected"
    assert writer_calls == []
    stored = list_candidates(queue_path=queue, include_resolved=True)[0]
    assert stored.status == "rejected"
    assert stored.review_reason == "not durable"
