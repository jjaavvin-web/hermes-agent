from __future__ import annotations

import json
from pathlib import Path

from agent.reflect_promote import (
    ReflectCandidate,
    approve_candidate,
    build_promotion_writer,
    drain_approved_queue,
    list_approved,
    list_candidates,
    mvms_writer,
    promote_via_recorder,
    reject_candidate,
)


class FakeRecorder:
    """In-memory stand-in for the mvms-writer MCP ``mvms_record_lesson`` path.

    Records every call and returns the MCP-shaped result dict. ``dedup_ids``
    lets a test simulate the writer's 24h source-keyed dedup (returns
    ``deduplicated`` instead of a fresh ``id``).
    """

    def __init__(self, *, dedup_sources: set[str] | None = None, fail: bool = False):
        self.calls: list[dict] = []
        self._dedup_sources = dedup_sources or set()
        self._fail = fail
        self._n = 0

    def record_lesson(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self._fail:
            return {"ok": False, "tool": "mvms_record_lesson", "error": "boom"}
        if kwargs["source"] in self._dedup_sources:
            return {"ok": True, "deduplicated": True, "existing_id": "dup-xyz"}
        self._n += 1
        return {"ok": True, "id": f"obs-{self._n}", "kind": "lesson"}


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


# ─── MEM-10 / MEM-11: gated promotion write path + drainer ───────────────────

_FLAG_OFF: dict = {}
_FLAG_OFF_EXPLICIT = {"reflect": {"promotion_enabled": False}}
_FLAG_ON = {"reflect": {"promotion_enabled": True}}


def _seed(path: Path, candidates: list[dict]) -> None:
    rows = []
    for i, c in enumerate(candidates):
        rows.append({
            "id": c.get("id", f"lesson-{i}"),
            "project": c.get("project", "hermes"),
            "situation": c.get("situation", f"situation {i}"),
            "mistake_or_insight": c.get("mistake_or_insight", f"insight {i}"),
            "correction": c.get("correction", f"correction {i}"),
            "status": c.get("status", "pending"),
            "source": c.get("source", "dream-reflect"),
            "tags": c.get("tags", []),
        })
    _write_queue(path, rows)


# -- the writer factory is gated by the flag --------------------------------

def test_build_promotion_writer_returns_none_when_flag_off():
    assert build_promotion_writer(config=_FLAG_OFF) is None
    assert build_promotion_writer(config=_FLAG_OFF_EXPLICIT) is None


def test_mvms_writer_raises_when_flag_off(tmp_path, monkeypatch):
    # Force the default config path to a disabled config so the legacy raise
    # (which the web endpoint turns into 501) is preserved.
    import agent.reflect_promote as rp

    monkeypatch.setattr(rp, "_promotion_enabled", lambda config=None: False)
    cand = ReflectCandidate(id="x", project="hermes", situation="s",
                            mistake_or_insight="m", correction="c")
    try:
        mvms_writer(cand)
        assert False, "expected RuntimeError when flag off"
    except RuntimeError as exc:
        assert "not configured" in str(exc)


def test_build_promotion_writer_uses_injected_recorder_when_flag_on():
    rec = FakeRecorder()
    writer = build_promotion_writer(config=_FLAG_ON, recorder=rec)
    assert writer is not None
    cand = ReflectCandidate(id="lesson-9", project="proj", situation="sit",
                            mistake_or_insight="ins", correction="fix",
                            source="dream-reflect", tags=["ops"])
    result = writer(cand)
    assert result["ok"] is True
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["project"] == "proj"
    assert call["situation"] == "sit"
    assert call["mistake_or_insight"] == "ins"
    assert call["correction"] == "fix"
    assert call["source"] == "reflect-promote:lesson-9"
    assert "reflect-promote" in call["tags"] and "lesson" in call["tags"]
    assert "ops" in call["tags"]
    assert "reflect-candidate:lesson-9" in call["evidence_refs"]


def test_promote_via_recorder_raises_on_backend_error():
    rec = FakeRecorder(fail=True)
    cand = ReflectCandidate(id="bad", project="hermes", situation="s",
                            mistake_or_insight="m", correction="c")
    try:
        promote_via_recorder(cand, recorder=rec)
        assert False, "expected RuntimeError on non-ok backend result"
    except RuntimeError as exc:
        assert "bad" in str(exc)


# -- drainer: flag OFF is a strict no-op -------------------------------------

def test_drainer_flag_off_is_noop(tmp_path):
    queue = tmp_path / "reflect-queue.jsonl"
    _seed(queue, [{"id": "a"}, {"id": "b"}])
    rec = FakeRecorder()

    report = drain_approved_queue(
        queue_path=queue,
        config=_FLAG_OFF,
        writer=lambda c: rec.record_lesson(),  # would be called if not gated
    )

    assert report.enabled is False
    assert report.promoted == []
    assert rec.calls == []
    # Queue untouched: both still pending.
    stored = list_candidates(queue_path=queue, include_resolved=True)
    assert all(c.status == "pending" for c in stored)


# -- drainer: flag ON calls writer once per awaiting candidate ---------------

def test_drainer_flag_on_promotes_each_pending_once(tmp_path):
    queue = tmp_path / "reflect-queue.jsonl"
    _seed(queue, [{"id": "a"}, {"id": "b"}, {"id": "c"}])
    rec = FakeRecorder()
    writer = build_promotion_writer(config=_FLAG_ON, recorder=rec)

    report = drain_approved_queue(queue_path=queue, config=_FLAG_ON, writer=writer)

    assert report.enabled is True
    assert sorted(report.promoted) == ["a", "b", "c"]
    assert len(rec.calls) == 3
    # Each promoted in the queue now.
    stored = {c.id: c.status for c in list_candidates(queue_path=queue, include_resolved=True)}
    assert stored == {"a": "promoted", "b": "promoted", "c": "promoted"}


# -- drainer: idempotent across re-runs --------------------------------------

def test_drainer_is_idempotent_on_rerun(tmp_path):
    queue = tmp_path / "reflect-queue.jsonl"
    _seed(queue, [{"id": "a"}, {"id": "b"}])
    rec = FakeRecorder()
    writer = build_promotion_writer(config=_FLAG_ON, recorder=rec)

    first = drain_approved_queue(queue_path=queue, config=_FLAG_ON, writer=writer)
    assert sorted(first.promoted) == ["a", "b"]
    assert len(rec.calls) == 2

    # Second run: both already promoted → skipped, writer NOT called again.
    second = drain_approved_queue(queue_path=queue, config=_FLAG_ON, writer=writer)
    assert second.promoted == []
    assert sorted(second.skipped_already_promoted) == ["a", "b"]
    assert len(rec.calls) == 2  # unchanged


# -- drainer: respects writer-side dedup result ------------------------------

def test_drainer_records_writer_dedup(tmp_path):
    queue = tmp_path / "reflect-queue.jsonl"
    _seed(queue, [{"id": "a"}, {"id": "b"}])
    # Simulate MVMS 24h dedup for candidate 'a' (its source already exists).
    rec = FakeRecorder(dedup_sources={"reflect-promote:a"})
    writer = build_promotion_writer(config=_FLAG_ON, recorder=rec)

    report = drain_approved_queue(queue_path=queue, config=_FLAG_ON, writer=writer)

    assert report.deduplicated == ["a"]
    assert report.promoted == ["b"]
    # Both rows still flipped to promoted so they won't be re-attempted.
    stored = {c.id: c.status for c in list_candidates(queue_path=queue, include_resolved=True)}
    assert stored == {"a": "promoted", "b": "promoted"}


# -- drainer: error handling keeps going + leaves row unpromoted -------------

def test_drainer_error_leaves_candidate_unpromoted(tmp_path):
    queue = tmp_path / "reflect-queue.jsonl"
    _seed(queue, [{"id": "good"}, {"id": "bad"}])

    def writer(candidate):
        if candidate.id == "bad":
            raise RuntimeError("write failed")
        return {"ok": True, "id": "obs-1"}

    report = drain_approved_queue(queue_path=queue, config=_FLAG_ON, writer=writer)

    assert report.promoted == ["good"]
    assert [cid for cid, _ in report.errors] == ["bad"]
    stored = {c.id: c.status for c in list_candidates(queue_path=queue, include_resolved=True)}
    assert stored["good"] == "promoted"
    assert stored["bad"] == "pending"  # errored row NOT marked promoted


# -- drainer: never resurrects rejected rows ---------------------------------

def test_drainer_skips_rejected(tmp_path):
    queue = tmp_path / "reflect-queue.jsonl"
    _seed(queue, [{"id": "a"}, {"id": "no", "status": "rejected"}])
    rec = FakeRecorder()
    writer = build_promotion_writer(config=_FLAG_ON, recorder=rec)

    report = drain_approved_queue(queue_path=queue, config=_FLAG_ON, writer=writer)

    assert report.promoted == ["a"]
    assert [c["source"] for c in rec.calls] == ["reflect-promote:a"]
    stored = {c.id: c.status for c in list_candidates(queue_path=queue, include_resolved=True)}
    assert stored["no"] == "rejected"


# -- list_approved surfaces awaiting candidates only -------------------------

def test_list_approved_excludes_resolved(tmp_path):
    queue = tmp_path / "reflect-queue.jsonl"
    _seed(queue, [
        {"id": "p1"},
        {"id": "done", "status": "promoted"},
        {"id": "no", "status": "rejected"},
    ])
    awaiting = list_approved(queue_path=queue)
    assert [c.id for c in awaiting] == ["p1"]
