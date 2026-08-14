from __future__ import annotations

import importlib.util
import http.client
import sys
import urllib.error
from email.message import Message
from pathlib import Path


SHIPPER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "observability" / "loki_shipper.py"
spec = importlib.util.spec_from_file_location("loki_shipper_script", SHIPPER_PATH)
assert spec is not None and spec.loader is not None
shipper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = shipper
spec.loader.exec_module(shipper)


def test_transient_loki_failure_retries_then_succeeds(monkeypatch) -> None:
    results = iter([
        {"status": 500, "error": "no live replicas"},
        {"status": 204, "reason": "No Content"},
    ])
    monkeypatch.setattr(shipper, "_push_loki_once", lambda *_args, **_kwargs: next(results))
    sleeps: list[float] = []
    monkeypatch.setattr(shipper.time, "sleep", sleeps.append)

    result = shipper.push_loki("http://127.0.0.1:3100", [], max_attempts=3, retry_delay=0.25)

    assert result["status"] == 204
    assert result["attempts"] == 2
    assert sleeps == [0.25]


def test_non_transient_loki_failure_does_not_retry(monkeypatch) -> None:
    calls = 0

    def fail_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"status": 400, "error": "bad request"}

    monkeypatch.setattr(shipper, "_push_loki_once", fail_once)
    monkeypatch.setattr(shipper.time, "sleep", lambda _delay: None)

    result = shipper.push_loki("http://127.0.0.1:3100", [], max_attempts=3)

    assert result["status"] == 400
    assert result["attempts"] == 1
    assert calls == 1


def test_transport_failure_exhausts_retry_budget(monkeypatch) -> None:
    calls = 0

    def unavailable(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"status": 0, "error": "connection refused"}

    monkeypatch.setattr(shipper, "_push_loki_once", unavailable)
    monkeypatch.setattr(shipper.time, "sleep", lambda _delay: None)

    result = shipper.push_loki("http://127.0.0.1:3100", [], max_attempts=3)

    assert result["status"] == 0
    assert result["attempts"] == 3
    assert calls == 3


def test_truncated_transient_http_error_body_still_retries(monkeypatch) -> None:
    calls = 0

    class Truncated503(urllib.error.HTTPError):
        def read(self, n=-1):
            raise http.client.IncompleteRead(b"no live", 20)

    def urlopen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Truncated503("http://loki", 503, "unavailable", Message(), None)
        return type("Response", (), {
            "status": 204,
            "reason": "No Content",
            "__enter__": lambda self: self,
            "__exit__": lambda self, *_args: None,
        })()

    monkeypatch.setattr(shipper.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(shipper.time, "sleep", lambda _delay: None)

    result = shipper.push_loki("http://loki", [], max_attempts=3)

    assert result["status"] == 204
    assert result["attempts"] == 2
    assert calls == 2


def test_malformed_http_response_consumes_retry_budget(monkeypatch) -> None:
    calls = 0

    def malformed(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise http.client.BadStatusLine("not-http")

    monkeypatch.setattr(shipper.urllib.request, "urlopen", malformed)
    monkeypatch.setattr(shipper.time, "sleep", lambda _delay: None)

    result = shipper.push_loki("http://loki", [], max_attempts=3)

    assert result["status"] == 0
    assert result["attempts"] == 3
    assert calls == 3