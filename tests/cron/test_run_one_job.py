"""Characterization + unit tests for the `run_one_job` shared helper (Phase 4A).

`tick`'s per-job body (`_process_job`) is the execute → save → deliver → mark
sequence that fires ONE due job. Phase 4A extracts it into a module-level
`run_one_job(job, *, adapters=None, loop=None, verbose=False)` so the external
Chronos provider's `fire_due` can reuse the IDENTICAL body — no duplicated
correctness.

The first test characterizes the sequence as driven through `tick()` (proving
the extraction didn't change `tick`'s behavior); the rest unit-test the
extracted helper directly.
"""
import json

import pytest

import cron.scheduler as s


def _patch_pipeline(monkeypatch, *, success=True, output="out", final="final response",
                    error=None, silent_marker_in=None):
    """Patch the job pipeline primitives and record the call order."""
    calls = []

    def fake_run_job(job):
        calls.append(("run_job", job["id"]))
        fr = final if silent_marker_in is None else silent_marker_in
        return (success, output, fr, error)

    def fake_save(jid, out):
        calls.append(("save", jid))
        return f"/tmp/{jid}.txt"

    def fake_deliver(job, content, adapters=None, loop=None):
        calls.append(("deliver", job["id"]))
        return None

    def fake_mark(jid, ok, err=None, delivery_error=None):
        calls.append(("mark", jid, ok))

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    return calls


def test_tick_process_job_sequence(monkeypatch):
    """Characterization: a single due job driven through tick() runs the
    sequence run_job → save → deliver → mark, in that order."""
    calls = _patch_pipeline(monkeypatch)
    monkeypatch.setattr(s, "get_due_jobs", lambda: [{"id": "j1", "name": "t"}])
    monkeypatch.setattr(s, "advance_next_run", lambda jid: True)

    s.tick(verbose=False, sync=True)

    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j1", True)


def test_run_one_job_success_sequence(monkeypatch):
    """The extracted helper runs the same execute→save→deliver→mark sequence
    for a successful job."""
    calls = _patch_pipeline(monkeypatch)

    ok = s.run_one_job({"id": "j2", "name": "t"})

    assert ok is True
    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j2", True)


def test_run_one_job_silent_skips_delivery(monkeypatch):
    """A [SILENT] final response saves output + marks the run but does NOT
    deliver."""
    calls = _patch_pipeline(monkeypatch, silent_marker_in="[SILENT]")

    s.run_one_job({"id": "j3", "name": "t"})

    kinds = [c[0] for c in calls]
    assert "run_job" in kinds and "save" in kinds and "mark" in kinds
    assert "deliver" not in kinds


def test_run_one_job_empty_response_is_soft_failure(monkeypatch):
    """An empty final response marks the run as NOT ok (issue #8585)."""
    calls = _patch_pipeline(monkeypatch, final="   ")

    s.run_one_job({"id": "j4", "name": "t"})

    mark = [c for c in calls if c[0] == "mark"][0]
    assert mark == ("mark", "j4", False)


def test_run_one_job_failed_job_delivers_error(monkeypatch):
    """A failed job still delivers (the error notice) and marks not-ok."""
    calls = _patch_pipeline(monkeypatch, success=False, final="", error="boom")

    s.run_one_job({"id": "j5", "name": "t"})

    kinds = [c[0] for c in calls]
    assert "deliver" in kinds  # failures always deliver
    mark = [c for c in calls if c[0] == "mark"][0]
    assert mark == ("mark", "j5", False)


def test_run_one_job_exception_marks_failure(monkeypatch):
    """If run_job raises, the helper marks the run failed and returns False
    rather than propagating."""
    def boom(job):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(s, "run_job", boom)
    marks = []
    monkeypatch.setattr(
        s, "mark_job_run",
        lambda jid, ok, err=None, delivery_error=None: marks.append((jid, ok)),
    )

    ok = s.run_one_job({"id": "j6", "name": "t"})

    assert ok is False
    assert marks == [("j6", False)]


# ---------------------------------------------------------------------------
# Self-reporting contract branch (Card 64) — scheduler.run_one_job:~2081-2119
# ---------------------------------------------------------------------------


def _patch_pipeline_capture(monkeypatch, *, success=True, output="out",
                            final="final response", error=None):
    """Patch the pipeline and CAPTURE delivered content + mark calls.

    Unlike ``_patch_pipeline``, ``fake_deliver`` records the exact body handed to
    delivery so the contract trailer/gap-line behavior can be asserted directly.
    """
    delivered: list[str] = []
    marks: list[tuple] = []

    def fake_run_job(job):
        return (success, output, final, error)

    def fake_save(jid, out):
        return f"/tmp/{jid}.txt"

    def fake_deliver(job, content, adapters=None, loop=None):
        delivered.append(content)
        return None

    def fake_mark(jid, ok, err=None, delivery_error=None):
        marks.append((jid, ok))

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    return delivered, marks


@pytest.fixture()
def contract_ledger(tmp_path, monkeypatch):
    """Redirect the cron-contracts ledger into tmp_path via HERMES_HOME.

    ``record_contract`` writes to ``cron_result.DEFAULT_LEDGER`` (computed from
    HERMES_HOME at import), so we set the env *and* repoint the already-resolved
    module constants — the real ~/.hermes ledger is never touched.
    """
    from cron import cron_result

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ledger = tmp_path / "observability" / "cron-contracts.jsonl"
    monkeypatch.setattr(cron_result, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(cron_result, "DEFAULT_LEDGER", ledger)
    return ledger


def test_run_one_job_contract_records_and_strips_trailer(contract_ledger, monkeypatch):
    """A contract:true job with a ``CONTRACT: {json}`` trailer (a) records one
    ledger line with the self-reported quota/achieved, (b) strips the trailer
    from the delivered body while appending the human gap line."""
    final = (
        "Report body line 1.\n"
        "Report body line 2.\n"
        'CONTRACT: {"quota": 5, "achieved": 4, "gaps": ["one source down"]}'
    )
    delivered, marks = _patch_pipeline_capture(monkeypatch, final=final)

    ok = s.run_one_job({"id": "jc1", "name": "weekly-scan", "contract": True})
    assert ok is True

    # (a) exactly one record landed in the redirected ledger with the trailer's data.
    lines = contract_ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["name"] == "weekly-scan"
    assert rec["quota"] == 5
    assert rec["achieved"] == 4
    assert rec["gaps"] == ["one source down"]

    # (b) the trailer is stripped from delivery; the gap line is appended.
    assert len(delivered) == 1
    body = delivered[0]
    assert "CONTRACT:" not in body
    assert "Report body line 2." in body
    assert "[contract] weekly-scan: achieved 4/5" in body
    assert marks == [("jc1", True)]


def test_run_one_job_contract_silent_records_but_does_not_deliver(contract_ledger, monkeypatch):
    """A [SILENT] contract:true job still writes a ledger record but suppresses
    delivery (the SILENT gate fires after the contract hook)."""
    delivered, marks = _patch_pipeline_capture(monkeypatch, final="[SILENT]")

    ok = s.run_one_job({"id": "jc2", "name": "silent-cron", "contract": True})
    assert ok is True

    # Records despite being silent.
    lines = contract_ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["name"] == "silent-cron"

    # But never delivers.
    assert delivered == []
