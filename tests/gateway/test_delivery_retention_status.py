import json
import os

from gateway.delivery_retention import scan_retained_dirty_deliveries
from gateway.status import read_runtime_status, write_runtime_status


def test_retained_dirty_delivery_scan_lists_paths_and_age(tmp_path):
    delivery = tmp_path / "wh-loki3-test"
    delivery.mkdir()
    dirty = delivery / ".dirty"
    dirty.write_text("awaiting harvest", encoding="utf-8")
    old = dirty.stat().st_mtime - 30
    os.utime(dirty, (old, old))

    result = scan_retained_dirty_deliveries(tmp_path)

    assert result["count"] == 1
    assert result["paths"] == [str(delivery)]
    assert result["items"][0]["age_seconds"] >= 0


def test_runtime_status_exposes_retained_dirty_deliveries(tmp_path, monkeypatch):
    home = tmp_path / "home"
    delivery = home / "relay-wt" / "deliveries" / "wh-loki3-test"
    delivery.mkdir(parents=True)
    (delivery / ".dirty").write_text("awaiting harvest", encoding="utf-8")
    monkeypatch.setattr("gateway.status.get_hermes_home", lambda: home)
    monkeypatch.setattr("gateway.delivery_retention.get_hermes_home", lambda: home)

    write_runtime_status(gateway_state="running", active_agents=0)

    status = read_runtime_status(home / "gateway_state.json")
    assert status is not None
    retained = status["retained_dirty_deliveries"]
    assert retained["count"] == 1
    assert retained["paths"] == [str(delivery)]
    json.dumps(retained)
