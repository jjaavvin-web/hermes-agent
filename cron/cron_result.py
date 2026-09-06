"""Self-reporting cron contracts (Card 64).

A *contract* is a one-line, machine-readable record a cron emits on every run:
how much it was supposed to do (``quota``), how much it actually did
(``achieved``), what was missed (``gaps``), and how many retries it burned.
Records append to a SEPARATE ledger — ``HERMES_HOME/observability/cron-contracts.jsonl``
— never to ``slo-timeseries.jsonl`` (that file has a fixed 8-key schema written
solely by ``observability_slo.write_snapshot`` and consumed line-by-line;
mixing heterogeneous records would corrupt its consumers).

The scheduler records a mechanical ``0/1`` contract for any job whose
``jobs.json`` def sets ``contract: true``; a cron may instead print a richer
machine-readable ``CONTRACT: {json}`` trailer (mirroring the ``[SILENT]`` marker
pattern in ``scheduler.py``) so a real domain quota flows up.

Everything here is pull-side: the dashboard reads the ledger.
``hard_floor_breaches`` is provided for an OPT-IN push gate ONLY and is wired to
no always-on timer in this cut — the cry-wolf vector is real (slo-alert was
recalibrated 2026-06-25; cron-deadman carries ``suspend_suppressed`` discipline).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Resolve HERMES_HOME exactly like observability_slo.py / cron-deadman-digest.py:
# the env var if set, else ~/.hermes.
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
DEFAULT_LEDGER = HERMES_HOME / "observability" / "cron-contracts.jsonl"

# Machine-readable trailer a cron may print so a real domain quota flows up,
# mirroring scheduler.SILENT_MARKER. Recognised only as the LAST non-blank line.
CONTRACT_TRAILER_PREFIX = "CONTRACT:"

# Consecutive achieved==0 runs that trip the hard floor for the opt-in push gate.
DEFAULT_FLOOR_MISSES = 3


@dataclass
class CronContract:
    """One self-reported cron outcome."""

    name: str
    quota: Optional[int]
    achieved: int
    gaps: list[str] = field(default_factory=list)
    retries: int = 0
    status: str = "ok"
    generated_at: str = ""

    def to_record(self) -> dict:
        return {
            "name": self.name,
            "quota": self.quota,
            "achieved": self.achieved,
            "gaps": list(self.gaps),
            "retries": self.retries,
            "status": self.status,
            "generated_at": self.generated_at,
        }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_contract(
    name: str,
    quota: Optional[int],
    achieved: int,
    gaps: Optional[list[str]],
    retries: int,
    status: str,
    *,
    ledger_path: Optional[Path] = None,
) -> CronContract:
    """Append one contract record (JSONL) to the cron-contracts ledger.

    Returns the :class:`CronContract` that was persisted so callers can format
    :func:`gap_line` from the same object. Creates the ledger's parent dir if
    needed. Writes are append-only and never touch ``slo-timeseries.jsonl``.
    """
    contract = CronContract(
        name=str(name),
        quota=None if quota is None else int(quota),
        achieved=int(achieved),
        gaps=[str(g) for g in (gaps or [])],
        retries=int(retries or 0),
        status=str(status or "ok"),
        generated_at=_utc_iso(),
    )
    path = Path(ledger_path) if ledger_path is not None else DEFAULT_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(contract.to_record(), sort_keys=True) + "\n")
    return contract


def gap_line(contract: CronContract) -> str:
    """The one-line human string each contract cron appends to its delivery."""
    quota = contract.quota if contract.quota is not None else "?"
    gaps = ", ".join(contract.gaps) if contract.gaps else "none"
    return (
        f"[contract] {contract.name}: achieved {contract.achieved}/{quota}; "
        f"gaps: {gaps}; retries: {contract.retries}"
    )


def parse_contract_trailer(text: Optional[str]) -> Optional[dict]:
    """Return the dict from a trailing ``CONTRACT: {json}`` line, else ``None``.

    Mirrors the ``[SILENT]`` convention: a cron may print, as its last non-blank
    line, ``CONTRACT: {"quota": 5, "achieved": 4, "gaps": [...], ...}`` to report
    a real domain quota instead of the mechanical ``0/1``. Tolerant of
    surrounding whitespace; returns ``None`` on any parse error or if the last
    non-blank line is not a contract trailer.
    """
    if not text:
        return None
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith(CONTRACT_TRAILER_PREFIX):
            payload = stripped[len(CONTRACT_TRAILER_PREFIX):].strip()
            try:
                data = json.loads(payload)
            except (ValueError, TypeError):
                return None
            return data if isinstance(data, dict) else None
        # Only the LAST non-blank line is ever a trailer.
        return None
    return None


def strip_contract_trailer(text: Optional[str]) -> str:
    """Drop a trailing ``CONTRACT: {json}`` line so it never reaches delivery."""
    if not text:
        return text or ""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip().upper().startswith(CONTRACT_TRAILER_PREFIX):
        lines.pop()
    return "\n".join(lines)


def _coerce_int(value: object, default: int = 0) -> int:
    """Best-effort int coercion so one malformed ledger field never raises.

    A hand-edited or partially-flushed record may carry a non-numeric
    ``achieved`` (e.g. ``"n/a"``); treat anything unparseable as ``default``
    rather than letting a single bad line crash the streak/breach helpers.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _iter_ledger_records(
    ledger_path: Optional[Path] = None, *, tail: Optional[int] = None
) -> list[dict]:
    """Read the ledger (tail-bounded) and return parsed dict records in order."""
    path = Path(ledger_path) if ledger_path is not None else DEFAULT_LEDGER
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if tail is not None:
        lines = lines[-tail:]
    records: list[dict] = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def consecutive_misses(
    name: str, ledger_path: Optional[Path] = None, *, tail: int = 500
) -> int:
    """Count trailing ``achieved == 0`` records for ``name`` (most-recent first).

    Reads only the ledger tail. The streak breaks at the first record for
    ``name`` whose ``achieved`` is non-zero; other crons' records are skipped.
    """
    records = _iter_ledger_records(ledger_path, tail=tail)
    streak = 0
    for rec in reversed(records):
        if rec.get("name") != name:
            continue
        if _coerce_int(rec.get("achieved", 0)) == 0:
            streak += 1
        else:
            break
    return streak


def hard_floor_breaches(
    ledger_path: Optional[Path] = None,
    n: int = DEFAULT_FLOOR_MISSES,
    *,
    tail: int = 2000,
) -> list[dict]:
    """Return per-cron breach descriptors for the OPT-IN push gate.

    A cron breaches the hard floor when its LATEST record has ``achieved == 0``
    OR it has ``>= n`` consecutive ``achieved == 0`` records. Pull-side by
    default — wired to no always-on timer in this cut (cry-wolf vector). Each
    descriptor: ``{name, achieved, streak, reason}``.
    """
    records = _iter_ledger_records(ledger_path, tail=tail)
    latest: dict[str, dict] = {}
    for rec in records:
        nm = rec.get("name")
        if nm:
            latest[nm] = rec
    breaches: list[dict] = []
    for nm, rec in latest.items():
        achieved = _coerce_int(rec.get("achieved", 0))
        streak = consecutive_misses(nm, ledger_path, tail=tail)
        if achieved == 0 or streak >= n:
            reason = "zero-achieved" if achieved == 0 else f"{streak} consecutive misses"
            breaches.append(
                {"name": nm, "achieved": achieved, "streak": streak, "reason": reason}
            )
    return breaches
