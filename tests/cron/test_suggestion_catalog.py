"""Dedicated tests for the curated cron suggestion catalog."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cron.suggestion_catalog import (
    CATALOG,
    CatalogEntry,
    classify_items_script_path,
    seed_catalog_suggestions,
)


class AddRecorder:
    """Small fake add_suggestion replacement that records real call kwargs."""

    def __init__(self, skip_key=None, mutate_job_spec=False):
        self.calls = []
        self.skip_key = skip_key
        self.mutate_job_spec = mutate_job_spec

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.mutate_job_spec:
            kwargs["job_spec"]["prompt"] = "mutated by fake add_fn"
        if kwargs["dedup_key"] == self.skip_key:
            return None
        return {"id": f"rec-{len(self.calls)}", "dedup_key": kwargs["dedup_key"]}


def _catalog_entry(key: str) -> CatalogEntry:
    return next(entry for entry in CATALOG if entry.key == key)


def _looks_like_supported_schedule(schedule: str) -> bool:
    if schedule.startswith("every "):
        parts = schedule.split()
        return len(parts) == 2 and parts[1][:-1].isdigit() and parts[1][-1:] in {"s", "m", "h", "d"}
    cron_fields = schedule.split()
    allowed = set("0123456789*,-/")
    return len(cron_fields) == 5 and all(field and set(field) <= allowed for field in cron_fields)


def test_seed_with_keys_filter_creates_only_requested_entry():
    recorder = AddRecorder()
    target_key = "catalog:weekly-review"

    created = seed_catalog_suggestions(add_fn=recorder, keys=[target_key])

    assert created == [{"id": "rec-1", "dedup_key": target_key}]
    assert [call["dedup_key"] for call in recorder.calls] == [target_key]
    assert recorder.calls[0]["title"] == _catalog_entry(target_key).title


def test_seed_skips_entries_when_add_fn_returns_none():
    skipped_key = "catalog:important-mail-monitor"
    recorder = AddRecorder(skip_key=skipped_key)

    created = seed_catalog_suggestions(add_fn=recorder)

    assert len(recorder.calls) == len(CATALOG)
    assert len(created) == len(CATALOG) - 1
    assert skipped_key not in {record["dedup_key"] for record in created}


def test_seed_lazy_default_add_fn_uses_cron_suggestions_attribute(monkeypatch):
    import cron.suggestions as suggestions

    recorder = AddRecorder()
    monkeypatch.setattr(suggestions, "add_suggestion", recorder)

    created = seed_catalog_suggestions()

    assert len(created) == len(CATALOG)
    assert len(recorder.calls) == len(CATALOG)
    for entry, call in zip(CATALOG, recorder.calls):
        assert call == {
            "title": entry.title,
            "description": entry.description,
            "source": "catalog",
            "job_spec": entry.job_spec,
            "dedup_key": entry.key,
        }
        assert call["job_spec"] is not entry.job_spec


def test_seed_passes_catalog_source_dedup_key_and_job_spec_copy():
    target_key = "catalog:daily-briefing"
    entry = _catalog_entry(target_key)
    original_prompt = entry.job_spec["prompt"]
    recorder = AddRecorder(mutate_job_spec=True)

    created = seed_catalog_suggestions(add_fn=recorder, keys=[target_key])

    assert created == [{"id": "rec-1", "dedup_key": target_key}]
    assert recorder.calls[0]["source"] == "catalog"
    assert recorder.calls[0]["dedup_key"] == entry.key
    assert recorder.calls[0]["job_spec"] is not entry.job_spec
    assert recorder.calls[0]["job_spec"]["prompt"] == "mutated by fake add_fn"
    assert entry.job_spec["prompt"] == original_prompt


def test_catalog_keys_are_unique():
    keys = [entry.key for entry in CATALOG]

    assert len(keys) == len(set(keys))


def test_catalog_job_specs_have_required_non_empty_create_job_fields():
    required = {"prompt", "schedule", "name", "deliver"}

    for entry in CATALOG:
        assert required <= entry.job_spec.keys()
        for key in required:
            assert isinstance(entry.job_spec[key], str)
            assert entry.job_spec[key].strip()
        assert entry.job_spec["deliver"] == "origin"
        assert _looks_like_supported_schedule(entry.job_spec["schedule"])


def test_catalog_entry_is_frozen_dataclass():
    entry = CATALOG[0]

    with pytest.raises(FrozenInstanceError):
        setattr(entry, "key", "catalog:changed")


def test_classifier_path_and_monitor_prompt_use_relocatable_module_reference():
    script_path = classify_items_script_path()
    script = Path(script_path)
    monitor = _catalog_entry("catalog:important-mail-monitor")

    assert script.is_absolute()
    assert script.name == "classify_items.py"
    assert script.parent.name == "scripts"
    assert "cron.scripts.classify_items" in monitor.job_spec["prompt"]
    assert script_path not in monitor.job_spec["prompt"]
