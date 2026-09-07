from __future__ import annotations

from pathlib import Path


def test_retained_dirty_deliveries_cache_reuses_same_window(tmp_path, monkeypatch):
    from gateway import delivery_retention as mod

    root = tmp_path / "deliveries"
    root.mkdir()
    first = root / "wh-first"
    first.mkdir()
    (first / ".dirty").write_text("x", encoding="utf-8")
    calls = {"count": 0}
    real = mod._scan_retained_dirty_deliveries_uncached

    def wrapped(path: Path | None = None):
        calls["count"] += 1
        return real(path)

    monkeypatch.setattr(mod, "_scan_retained_dirty_deliveries_uncached", wrapped)
    monkeypatch.setattr(mod.time, "monotonic", lambda: 100.0)
    mod.invalidate_scan_cache()

    one = mod.scan_retained_dirty_deliveries(root)
    two = mod.scan_retained_dirty_deliveries(root)

    assert one == two
    assert one["count"] == 1
    assert calls["count"] == 1


def test_retained_dirty_deliveries_cache_expires_at_boundary(tmp_path, monkeypatch):
    from gateway import delivery_retention as mod

    root = tmp_path / "deliveries"
    root.mkdir()
    first = root / "wh-first"
    first.mkdir()
    (first / ".dirty").write_text("x", encoding="utf-8")
    clock = {"now": 200.0}
    calls = {"count": 0}
    real = mod._scan_retained_dirty_deliveries_uncached

    def wrapped(path: Path | None = None):
        calls["count"] += 1
        return real(path)

    monkeypatch.setattr(mod, "_scan_retained_dirty_deliveries_uncached", wrapped)
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])
    mod.invalidate_scan_cache()

    assert mod.scan_retained_dirty_deliveries(root)["count"] == 1
    second = root / "wh-second"
    second.mkdir()
    (second / ".dirty").write_text("x", encoding="utf-8")
    clock["now"] = 200.0 + mod.SCAN_CACHE_TTL_SECONDS

    assert mod.scan_retained_dirty_deliveries(root)["count"] == 2
    assert calls["count"] == 2
