from __future__ import annotations

import asyncio

from hermes_cli import dashboard_connectome as dc


def test_memory_query_uses_is_distinct_from_not_not_equals() -> None:
    assert "source IS DISTINCT FROM 'ict-brain'" in dc.MEMORY_QUERY_WHERE
    assert "source != 'ict-brain'" not in dc.MEMORY_QUERY_WHERE


def test_memory_clean_count_live_range() -> None:
    data = asyncio.run(dc._memory_query(limit=1))
    assert 4000 < data["clean_total"] < 6000


def test_l3_probes_never_raise() -> None:
    brain = dc._memory_probe()
    code = dc._code_probe()
    assert brain["hub"]["id"] == "brain"
    assert code["hub"]["id"] == "code"
