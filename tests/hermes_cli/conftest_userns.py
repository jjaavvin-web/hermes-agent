"""Shared probe for tests that need unprivileged user namespaces.

The specialist test sandbox (``tools/specialist_test_tool.py``) runs
``unshare -Urnpf``; GitHub-hosted runners deny it with
``unshare: write failed /proc/self/uid_map: Operation not permitted``.
"""

import subprocess

import pytest


def _userns_available() -> bool:
    try:
        return subprocess.run(
            ["unshare", "-Ur", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


requires_userns = pytest.mark.skipif(
    not _userns_available(),
    reason="unprivileged user namespaces unavailable (GitHub-hosted runners)",
)
