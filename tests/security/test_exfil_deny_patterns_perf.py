"""Perf pin for the credential-exfil deny patterns (webhook deny path).

Mutation evidence (audit 20260903T202249Z-v021-candidate, M6b): removing the
``\\A`` anchor from a ``(?=[\\s\\S]*A)(?=[\\s\\S]*B)`` lookahead chain leaves every
behavioral exfil test green (semantics are unchanged) while ``re.search`` on a
long benign command degrades to O(n^2) — the pattern hung for >20s on a
4000-segment command. ``detect_dangerous_command``'s benchmark never sees these
patterns (they are consumed by ``gateway/platforms/webhook.py`` through
``DEFAULT_WEBHOOK_DENY_PATTERNS``), so this file times them directly, with the
same compile flags the rail tests use.
"""
from __future__ import annotations

import re
import time

import pytest

from tools.approval import CREDENTIAL_EXFIL_DENY_PATTERNS

_BENIGN_4000 = " ; ".join(f"printf 'segment {i}'" for i in range(4000))
_FLAGS = re.IGNORECASE | re.DOTALL


@pytest.mark.parametrize("index", range(len(CREDENTIAL_EXFIL_DENY_PATTERNS)))
def test_exfil_deny_pattern_is_fast_on_long_benign_command(index: int) -> None:
    pattern = re.compile(CREDENTIAL_EXFIL_DENY_PATTERNS[index], _FLAGS)
    started = time.perf_counter()
    assert pattern.search(_BENIGN_4000) is None
    elapsed = time.perf_counter() - started
    assert elapsed < 3.0, (
        f"deny pattern #{index} took {elapsed:.2f}s on a 4000-segment benign "
        "command — an unanchored lookahead chain is O(n^2) on .search()"
    )
