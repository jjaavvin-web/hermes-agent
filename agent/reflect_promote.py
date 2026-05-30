from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal, Protocol

from hermes_constants import get_hermes_home

ReflectStatus = Literal["pending", "promoted", "rejected"]

# Config flag (in ~/.hermes/config.yaml) that gates the *real* MVMS promotion
# write path. Default OFF: when this is False (or absent), promotion behaves
# exactly as before this change — ``mvms_writer`` raises and the dashboard /
# web approve endpoint surfaces 501. The operator flips this to True (and
# un-disables the Approve button) only after reviewing the write path.
PROMOTION_ENABLED_KEY = ("reflect", "promotion_enabled")


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

    The in-repo dashboard must never auto-promote.  This is the *default-OFF*
    hook: it raises unless the operator has flipped ``reflect.promotion_enabled``
    to True in ``config.yaml`` (and even then the dashboard Approve button stays
    disabled until the operator un-disables it).

    Behaviour:
      * flag OFF (default) → raises ``RuntimeError`` (web endpoint → 501),
        exactly as before MEM-10.
      * flag ON            → builds the real MVMS lesson writer and records the
        approved candidate as a lesson.

    Keeping the hook injectable elsewhere (``approve_candidate(writer=...)``,
    ``drain_approved_queue(writer=...)``) means tests and the dashboard wiring
    never trigger a real write by accident.
    """
    writer = build_promotion_writer()
    if writer is None:
        raise RuntimeError("MVMS promotion writer is not configured in this runtime")
    return writer(candidate)


# ─── MEM-10: gated MVMS promotion write path ─────────────────────────────────


def _promotion_enabled(config: dict | None = None) -> bool:
    """Return True only when ``reflect.promotion_enabled`` is truthy.

    ``config`` may be injected (tests / drainer); otherwise it is read from
    ``~/.hermes/config.yaml`` via the read-only fast path. Any failure to load
    config is treated as the safe default (OFF).
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly() or {}
        except Exception:
            return False
    section = config.get("reflect") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        return False
    return bool(section.get("promotion_enabled", False))


class LessonRecorder(Protocol):
    """Minimal contract a backend must satisfy to record a promoted lesson.

    Mirrors the constrained ``mvms-writer`` MCP ``mvms_record_lesson`` tool:
    given the lesson fields it performs exactly one INSERT (or a no-op dedup)
    and returns a result dict shaped like the MCP's
    ``{"ok": True, "id": ...}`` / ``{"ok": True, "deduplicated": True, ...}``.
    """

    def record_lesson(
        self,
        *,
        project: str,
        situation: str,
        mistake_or_insight: str,
        correction: str,
        evidence_refs: list[str],
        source: str,
        tags: list[str],
        importance: int,
    ) -> dict: ...


def _candidate_source(candidate: ReflectCandidate) -> str:
    """Stable per-candidate source key for writer-side dedup.

    The mvms-writer applies a 24h ``(source, content_hash)`` dedup, so a stable
    source means re-draining an already-promoted candidate is a no-op there too
    (defence in depth on top of the queue's ``status == "promoted"`` guard).
    """
    return f"reflect-promote:{candidate.id}"


def promote_via_recorder(
    candidate: ReflectCandidate,
    *,
    recorder: LessonRecorder,
) -> dict:
    """Record a single approved candidate as an MVMS lesson via ``recorder``.

    Pure and reviewable: builds the ``mvms_record_lesson`` argument shape from
    the candidate and delegates the actual write to the injected recorder. Does
    no config-gating itself — callers gate (see ``build_promotion_writer``).
    Raises ``RuntimeError`` if the backend reports a non-ok result so the
    drainer / endpoint can surface the failure (and the queue row is NOT marked
    promoted).
    """
    situation = candidate.situation or f"[reflect-promote {candidate.id}]"
    mistake = candidate.mistake_or_insight or "(insight not captured)"
    correction = candidate.correction or "(correction not captured)"
    evidence_refs = [f"reflect-candidate:{candidate.id}"]
    if candidate.source:
        evidence_refs.append(f"reflect-source:{candidate.source}")
    tags = ["reflect-promote", "lesson", *candidate.tags]
    result = recorder.record_lesson(
        project=candidate.project or "hermes",
        situation=situation,
        mistake_or_insight=mistake,
        correction=correction,
        evidence_refs=evidence_refs,
        source=_candidate_source(candidate),
        tags=tags,
        importance=2,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        err = result.get("error") if isinstance(result, dict) else result
        raise RuntimeError(f"MVMS lesson write failed for {candidate.id}: {err!r}")
    return result


def build_promotion_writer(
    *,
    config: dict | None = None,
    recorder: LessonRecorder | None = None,
) -> Callable[[ReflectCandidate], dict] | None:
    """Return a writer callable when promotion is enabled, else ``None``.

    * Returns ``None`` when ``reflect.promotion_enabled`` is off (default) —
      callers translate that into the legacy not-configured / 501 behaviour.
    * When enabled, returns a closure that promotes a candidate through
      ``promote_via_recorder``. ``recorder`` is injectable so tests pass a fake;
      in production it defaults to the real MVMS lesson recorder built lazily
      from ``~/.hermes/config.yaml`` / MVMS env.
    """
    if not _promotion_enabled(config):
        return None
    if recorder is None:
        recorder = _default_mvms_recorder()

    def _writer(candidate: ReflectCandidate) -> dict:
        return promote_via_recorder(candidate, recorder=recorder)

    return _writer


def _default_mvms_recorder() -> LessonRecorder:
    """Lazily build the real MVMS lesson recorder (subprocess mvms-writer MCP).

    Imported lazily so this module stays import-light and test-safe — tests
    NEVER reach here because they inject a fake recorder. The implementation
    lives in ``agent.reflect_promote_mvms`` to keep the I/O-heavy subprocess
    code out of this pure module.
    """
    from agent.reflect_promote_mvms import MvmsWriterRecorder

    return MvmsWriterRecorder()


# ─── MEM-11: approved-queue drainer core ─────────────────────────────────────


@dataclass(slots=True)
class DrainReport:
    enabled: bool
    promoted: list[str] = field(default_factory=list)
    deduplicated: list[str] = field(default_factory=list)
    skipped_already_promoted: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "promoted": list(self.promoted),
            "deduplicated": list(self.deduplicated),
            "skipped_already_promoted": list(self.skipped_already_promoted),
            "errors": [{"id": cid, "error": err} for cid, err in self.errors],
        }


def list_approved(*, queue_path: Path | None = None) -> list[ReflectCandidate]:
    """Return the candidates the drainer would promote on its next run.

    These are the queued candidates still awaiting promotion: ``pending`` rows
    (the operator running the drainer IS the bulk-approval gate) and any row
    carrying an explicit ``approved`` sentinel from a future approve-without-
    write flow. Rows already ``promoted`` or ``rejected`` are excluded.
    """
    out: list[ReflectCandidate] = []
    for row in _read_rows(queue_path):
        raw_status = str(row.get("status") or "").strip().lower()
        if raw_status in {"", "pending", "approved"}:
            out.append(ReflectCandidate.from_row(row))
    return out


def drain_approved_queue(
    *,
    queue_path: Path | None = None,
    config: dict | None = None,
    writer: Callable[[ReflectCandidate], object] | None = None,
) -> DrainReport:
    """Promote every awaiting candidate when the flag is on; else no-op.

    Idempotent in two layers:
      1. queue: a row already at ``status == "promoted"`` is skipped, and on a
         successful write the row is flipped to ``promoted`` + persisted, so a
         re-run never re-writes it;
      2. writer: the stable per-candidate source key lets the MVMS-side 24h
         dedup return ``deduplicated`` rather than inserting twice.

    When the flag is off this returns ``enabled=False`` and touches nothing
    (no reads-to-write, no writer construction).

    ``writer`` is injectable (tests pass a fake). When omitted it is built from
    the (gated) config via ``build_promotion_writer``.
    """
    enabled = _promotion_enabled(config)
    report = DrainReport(enabled=enabled)
    if not enabled:
        return report

    if writer is None:
        writer = build_promotion_writer(config=config)
        if writer is None:  # belt-and-braces; should not happen when enabled
            report.enabled = False
            return report

    rows = _read_rows(queue_path)
    candidates = [ReflectCandidate.from_row(row) for row in rows]
    changed = False
    for idx, candidate in enumerate(candidates):
        raw_status = str(rows[idx].get("status") or "").strip().lower()
        if raw_status == "promoted" or candidate.status == "promoted":
            report.skipped_already_promoted.append(candidate.id)
            continue
        if raw_status == "rejected":
            # Never resurrect an explicitly rejected candidate.
            continue
        try:
            result = writer(candidate)
        except Exception as exc:  # noqa: BLE001 — record & continue draining
            report.errors.append((candidate.id, str(exc)))
            continue
        if isinstance(result, dict) and result.get("deduplicated"):
            report.deduplicated.append(candidate.id)
        else:
            report.promoted.append(candidate.id)
        candidate.status = "promoted"
        rows[idx] = candidate.to_row()
        changed = True
    if changed:
        _write_rows(rows, queue_path)
    return report


def pending_payload(*, queue_path: Path | None = None) -> dict:
    return {"candidates": [c.to_row() for c in list_candidates(queue_path=queue_path)]}
