from __future__ import annotations

import asyncio

import pytest

from hermes_cli import dashboard_connectome as dc


def test_memory_query_uses_is_distinct_from_not_not_equals() -> None:
    assert "source IS DISTINCT FROM 'ict-brain'" in dc.MEMORY_QUERY_WHERE
    assert "source != 'ict-brain'" not in dc.MEMORY_QUERY_WHERE


class _FakeTransaction:
    async def __aenter__(self) -> "_FakeTransaction":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeConn:
    """Stand-in for an asyncpg connection returning fixed, non-live counts.

    ``_memory_query`` issues its four scalar reads in a fixed order
    (clean_total, signal_total, unfiltered_total, ict_total) followed by
    the leaf row fetch; this fake replays canned values in that order so
    the test never touches a real (growing) Postgres database.
    """

    def __init__(self, values: list[int], rows: list[dict[str, object]]) -> None:
        self._values = list(values)
        self._rows = rows

    def transaction(self, readonly: bool = True) -> _FakeTransaction:
        return _FakeTransaction()

    async def fetchval(self, query: str, *args: object) -> int:
        return self._values.pop(0)

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        return self._rows

    async def close(self) -> None:
        return None


def test_memory_clean_count_live_range(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_conn = _FakeConn(values=[4321, 12, 9999, 42], rows=[])

    async def _fake_connect(dsn: str, command_timeout: int = 10) -> _FakeConn:
        return fake_conn

    monkeypatch.setattr(dc, "_mvms_dsn", lambda: "postgresql://fake-dsn/for-test")
    monkeypatch.setattr(dc.asyncpg, "connect", _fake_connect)
    data = asyncio.run(dc._memory_query(limit=1))
    assert data["clean_total"] == 4321
    assert data["signal_total"] == 12
    assert data["unfiltered_total"] == 9999
    assert data["ict_total"] == 42


def test_l3_probes_never_raise() -> None:
    brain = dc._memory_probe()
    code = dc._code_probe()
    assert brain["hub"]["id"] == "brain"
    assert code["hub"]["id"] == "code"
