"""Tests for plugins.cron.chronos._nas_client."""

from __future__ import annotations

import pytest
import requests

from plugins.cron.chronos._nas_client import NasCronClient, NasCronClientError


class FakeResponse:
    """Small response double for the fields used by NasCronClient."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "OK",
        content: bytes = b"{}",
        json_data: object | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content
        self._json_data = {} if json_data is None else json_data
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._json_data


def _patch_token(monkeypatch: pytest.MonkeyPatch, token: str = "TESTTOKEN") -> None:
    monkeypatch.setattr("hermes_cli.auth.resolve_nous_access_token", lambda: token)


def test_provision_posts_exact_body_headers_timeout_and_returns_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_token(monkeypatch)
    calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(json_data={"schedule_id": "sched-123"})

    monkeypatch.setattr(requests, "post", fake_post)

    result = NasCronClient("https://p.example", timeout_seconds=3.5).provision(
        job_id="job-1",
        fire_at="2026-06-24T12:00:00Z",
        agent_callback_url="https://agent.example/callback",
        dedup_key="job-1:2026-06-24T12:00:00Z",
    )

    assert result == {"schedule_id": "sched-123"}
    assert calls == [
        {
            "url": "https://p.example/api/agent-cron/provision",
            "json": {
                "job_id": "job-1",
                "fire_at": "2026-06-24T12:00:00Z",
                "agent_callback_url": "https://agent.example/callback",
                "dedup_key": "job-1:2026-06-24T12:00:00Z",
            },
            "headers": {
                "Authorization": "Bearer TESTTOKEN",
                "Content-Type": "application/json",
            },
            "timeout": 3.5,
        }
    ]


def test_trailing_slash_portal_url_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_token(monkeypatch)
    urls: list[str] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        urls.append(url)
        return FakeResponse(json_data={"schedule_id": "sched-456"})

    monkeypatch.setattr(requests, "post", fake_post)

    result = NasCronClient("https://p.example/").provision(
        job_id="job-2",
        fire_at="2026-06-24T13:00:00Z",
        agent_callback_url="https://agent.example/callback",
        dedup_key="job-2:2026-06-24T13:00:00Z",
    )

    assert result == {"schedule_id": "sched-456"}
    assert urls == ["https://p.example/api/agent-cron/provision"]


@pytest.mark.parametrize("status_code", [401, 500])
def test_post_non_2xx_raises_error_with_status_code(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    _patch_token(monkeypatch)

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        return FakeResponse(status_code=status_code, text=f"failure {status_code}")

    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(NasCronClientError) as excinfo:
        NasCronClient("https://p.example").cancel(job_id="job-err")

    assert f"POST /api/agent-cron/cancel returned {status_code}" in str(excinfo.value)


def test_empty_204_post_response_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_token(monkeypatch)

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        return FakeResponse(status_code=204, text="", content=b"")

    monkeypatch.setattr(requests, "post", fake_post)

    result = NasCronClient("https://p.example").cancel(job_id="job-empty")

    assert result == {}


def test_invalid_json_response_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_token(monkeypatch)

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        return FakeResponse(content=b"not-json", json_error=ValueError("bad json"))

    monkeypatch.setattr(requests, "post", fake_post)

    result = NasCronClient("https://p.example").cancel(job_id="job-invalid-json")

    assert result == {}


def test_transport_failure_is_wrapped_in_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_token(monkeypatch)

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(NasCronClientError) as excinfo:
        NasCronClient("https://p.example").cancel(job_id="job-transport")

    assert "POST /api/agent-cron/cancel failed: network down" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, requests.ConnectionError)


def test_cancel_posts_exact_job_id_to_cancel_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_token(monkeypatch)
    calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(json_data={"cancelled": True})

    monkeypatch.setattr(requests, "post", fake_post)

    result = NasCronClient("https://p.example", timeout_seconds=8.0).cancel(job_id="job-cancel")

    assert result == {"cancelled": True}
    assert calls == [
        {
            "url": "https://p.example/api/agent-cron/cancel",
            "json": {"job_id": "job-cancel"},
            "headers": {
                "Authorization": "Bearer TESTTOKEN",
                "Content-Type": "application/json",
            },
            "timeout": 8.0,
        }
    ]


def test_list_armed_gets_list_path_and_returns_inner_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_token(monkeypatch)
    calls: list[dict[str, object]] = []
    armed = [{"job_id": "job-1", "fire_at": "2026-06-24T12:00:00Z", "schedule_id": "sched-1"}]

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(json_data={"armed": armed})

    monkeypatch.setattr(requests, "get", fake_get)

    result = NasCronClient("https://p.example", timeout_seconds=9.0).list_armed()

    assert result == armed
    assert calls == [
        {
            "url": "https://p.example/api/agent-cron/list",
            "params": {},
            "headers": {
                "Authorization": "Bearer TESTTOKEN",
                "Content-Type": "application/json",
            },
            "timeout": 9.0,
        }
    ]


@pytest.mark.parametrize(
    "json_data",
    [
        {},
        {"armed": "not-a-list"},
        ["top-level-not-dict"],
    ],
)
def test_list_armed_returns_empty_list_for_unusable_payloads(
    monkeypatch: pytest.MonkeyPatch,
    json_data: object,
) -> None:
    _patch_token(monkeypatch)

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        return FakeResponse(json_data=json_data)

    monkeypatch.setattr(requests, "get", fake_get)

    result = NasCronClient("https://p.example").list_armed()

    assert result == []


def test_get_non_2xx_raises_error_with_status_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_token(monkeypatch)

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        return FakeResponse(status_code=500, text="list failed")

    monkeypatch.setattr(requests, "get", fake_get)

    with pytest.raises(NasCronClientError) as excinfo:
        NasCronClient("https://p.example").list_armed()

    assert "GET /api/agent-cron/list returned 500" in str(excinfo.value)


def test_get_transport_failure_is_wrapped_in_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_token(monkeypatch)

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        raise requests.ConnectionError("list network down")

    monkeypatch.setattr(requests, "get", fake_get)

    with pytest.raises(NasCronClientError) as excinfo:
        NasCronClient("https://p.example").list_armed()

    assert "GET /api/agent-cron/list failed: list network down" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, requests.ConnectionError)
