"""Tests for tools.fal_common managed FAL.ai SDK plumbing."""

from types import SimpleNamespace

import pytest

from tools.fal_common import (
    _ManagedFalSyncClient,
    _extract_http_status,
    _normalize_fal_queue_url_format,
)


def test_normalize_fal_queue_url_format_strips_whitespace_and_appends_one_slash():
    assert (
        _normalize_fal_queue_url_format("  https://x.fal.run/queue/  ")
        == "https://x.fal.run/queue/"
    )
    assert (
        _normalize_fal_queue_url_format("https://x.fal.run/queue")
        == "https://x.fal.run/queue/"
    )


@pytest.mark.parametrize("bad_origin", ["", "   ", None])
def test_normalize_fal_queue_url_format_rejects_empty_origin(bad_origin):
    with pytest.raises(ValueError):
        _normalize_fal_queue_url_format(bad_origin)


class ResponseStatusError(Exception):
    def __init__(self, status_code):
        super().__init__("provider unavailable")
        self.response = SimpleNamespace(status_code=status_code)


class DirectStatusError(Exception):
    def __init__(self, status_code):
        super().__init__("rate limited")
        self.status_code = status_code


def test_extract_http_status_returns_response_status_code():
    exc = ResponseStatusError(503)

    assert _extract_http_status(exc) == 503


def test_extract_http_status_falls_back_to_exception_status_code():
    exc = DirectStatusError(429)

    assert _extract_http_status(exc) == 429


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("boom"),
        SimpleNamespace(response=SimpleNamespace(status_code="503")),
    ],
)
def test_extract_http_status_returns_none_without_usable_int_status(exc):
    assert _extract_http_status(exc) is None


def test_managed_fal_sync_client_requires_sync_client():
    fake_fal_client = SimpleNamespace(SyncClient=None)

    with pytest.raises(RuntimeError):
        _ManagedFalSyncClient(
            fake_fal_client,
            key="k",
            queue_run_origin="https://x.fal.run/queue",
        )


def _fake_fal_client(captured_urls):
    http_client = object()

    class FakeSyncClient:
        def __init__(self, *, key):
            self.key = key
            self._client = http_client
            self.default_timeout = 120.0

    class FakeResponse:
        def json(self):
            return {
                "request_id": "r",
                "response_url": "ru",
                "status_url": "su",
                "cancel_url": "cu",
            }

    class FakeSyncRequestHandle:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def maybe_retry_request(http_client_arg, method, url, *, json, timeout, headers):
        assert http_client_arg is http_client
        assert method == "POST"
        assert json == {"prompt": "hi"} or json == {"p": 1}
        assert timeout == 120.0
        assert headers == {}
        captured_urls.append(url)
        return FakeResponse()

    client_module = SimpleNamespace(
        _maybe_retry_request=maybe_retry_request,
        _raise_for_status=lambda response: None,
        SyncRequestHandle=FakeSyncRequestHandle,
        add_hint_header=lambda hint, headers: None,
        add_priority_header=lambda priority, headers: None,
        add_timeout_header=lambda start_timeout, headers: None,
    )
    return SimpleNamespace(SyncClient=FakeSyncClient, client=client_module)


def test_managed_fal_sync_client_submit_builds_submit_url_and_returns_handle():
    captured_urls = []
    client = _ManagedFalSyncClient(
        _fake_fal_client(captured_urls),
        key="k",
        queue_run_origin="https://x.fal.run/queue",
    )

    handle = client.submit("fal-ai/flux", {"prompt": "hi"})

    assert captured_urls == ["https://x.fal.run/queue/fal-ai/flux"]
    assert handle.request_id == "r"
    assert handle.response_url == "ru"
    assert handle.status_url == "su"
    assert handle.cancel_url == "cu"


def test_managed_fal_sync_client_submit_joins_path_and_encodes_webhook_url():
    captured_urls = []
    client = _ManagedFalSyncClient(
        _fake_fal_client(captured_urls),
        key="k",
        queue_run_origin="https://x.fal.run/queue/",
    )

    client.submit(
        "fal-ai/flux",
        {"p": 1},
        path="/stream",
        webhook_url="https://cb.example/x?y=1",
    )

    assert len(captured_urls) == 1
    url = captured_urls[0]
    assert url.startswith("https://x.fal.run/queue/fal-ai/flux/stream")
    assert "fal_webhook=" in url
    assert "https%3A%2F%2Fcb.example%2Fx%3Fy%3D1" in url
    assert "://" not in url.split("?", maxsplit=1)[1]
