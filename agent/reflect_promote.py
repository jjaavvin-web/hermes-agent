from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal

from hermes_constants import get_hermes_home

ReflectStatus = Literal["pending", "promoted", "rejected"]


def default_queue_path() -> Path:
    return get_hermes_home() / "state" / "reflect-queue.jsonl"


@dataclass(slots=True)
class ReflectCandidate:
    id: str
    project: str
    situation: str
    mistake_or_insight: str
    correction: str
    status: ReflectStatus = "pending"
    created_at: str | None = None
    source: str | None = None
    tags: list[str] = field(default_factory=list)
    review_reason: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "ReflectCandidate":
        cid = str(row.get("id") or row.get("candidate_id") or "").strip()
        if not cid:
            raise ValueError("reflect candidate missing id")
        return cls(
            id=cid,
            project=str(row.get("project") or "hermes"),
            situation=str(row.get("situation") or row.get("summary") or ""),
            mistake_or_insight=str(row.get("mistake_or_insight") or row.get("insight") or ""),
            correction=str(row.get("correction") or row.get("takeaway") or ""),
            status=_normalize_status(row.get("status")),
            created_at=row.get("created_at"),
            source=row.get("source"),
            tags=[str(tag) for tag in row.get("tags", []) if tag is not None],
            review_reason=row.get("review_reason"),
        )

    def to_row(self) -> dict:
        return asdict(self)


def _normalize_status(raw: object) -> ReflectStatus:
    value = str(raw or "pending").strip().lower()
    if value in {"promoted", "approved"}:
        return "promoted"
    if value in {"rejected", "skipped"}:
        return "rejected"
    return "pending"


def _read_rows(queue_path: Path | None = None) -> list[dict]:
    path = Path(queue_path) if queue_path is not None else default_queue_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _write_rows(rows: Iterable[dict], queue_path: Path | None = None) -> None:
    path = Path(queue_path) if queue_path is not None else default_queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(row, sort_keys=True) for row in rows)
    path.write_text((payload + "\n") if payload else "", encoding="utf-8")


def list_candidates(
    *,
    queue_path: Path | None = None,
    include_resolved: bool = False,
    writer: Callable[[ReflectCandidate], object] | None = None,
) -> list[ReflectCandidate]:
    """Return queued reflect candidates without promoting them.

    ``writer`` is accepted only so tests and dashboard wiring can prove listing is
    propose-only: this function intentionally never calls it.
    """
    del writer
    candidates = [ReflectCandidate.from_row(row) for row in _read_rows(queue_path)]
    if include_resolved:
        return candidates
    return [candidate for candidate in candidates if candidate.status == "pending"]


def approve_candidate(
    candidate_id: str,
    *,
    queue_path: Path | None = None,
    writer: Callable[[ReflectCandidate], object],
) -> ReflectCandidate:
    rows = _read_rows(queue_path)
    candidates = [ReflectCandidate.from_row(row) for row in rows]
    for idx, candidate in enumerate(candidates):
        if candidate.id != candidate_id:
            continue
        if candidate.status != "promoted":
            writer(candidate)
            candidate.status = "promoted"
        rows[idx] = candidate.to_row()
        _write_rows(rows, queue_path)
        return candidate
    raise KeyError(candidate_id)


def reject_candidate(
    candidate_id: str,
    *,
    queue_path: Path | None = None,
    writer: Callable[[ReflectCandidate], object] | None = None,
    reason: str | None = None,
) -> ReflectCandidate:
    del writer
    rows = _read_rows(queue_path)
    candidates = [ReflectCandidate.from_row(row) for row in rows]
    for idx, candidate in enumerate(candidates):
        if candidate.id != candidate_id:
            continue
        candidate.status = "rejected"
        candidate.review_reason = reason
        rows[idx] = candidate.to_row()
        _write_rows(rows, queue_path)
        return candidate
    raise KeyError(candidate_id)


def mvms_writer(candidate: ReflectCandidate) -> object:
    """Promotion hook used only after explicit operator approval.

    The in-repo dashboard must never auto-promote.  MEM-10/MVMS runtime wiring
    can replace this hook with the actual mvms-writer call path; keeping it
    injectable here prevents accidental writes during listing/draining.
    """
    raise RuntimeError("MVMS promotion writer is not configured in this runtime")


def pending_payload(*, queue_path: Path | None = None) -> dict:
    return {"candidates": [c.to_row() for c in list_candidates(queue_path=queue_path)]}
