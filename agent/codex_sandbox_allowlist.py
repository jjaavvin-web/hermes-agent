"""Optional opt-in allowlist for the P1.5 Codex sandbox.

By default `tools.file_tools._enforce_codex_sandbox` denies any write
whose realpath falls outside the Discord thread's active worktree —
the guard that turned a silent live-tree corruption into a loud refusal.

Some tasks legitimately need to edit specific paths *outside* the
worktree (a separate Obsidian vault, an ISA spec at a known location,
a generated-artifacts directory the user owns).  Granting blanket
escape from the sandbox would re-open the corruption mode; granting a
narrow, configured allowlist does not.

Config file (opt-in, absent by default):
    ~/.hermes/codex-sandbox-allow.yaml

Schema:
    allowed_paths:
      - /mnt/d/ai-work/obsidian/Hermes-Infrastructure-Atlas
      - /home/josep/.hermes/ISA-SPEC.md

Env-var override (takes precedence over the file when set non-empty):
    HERMES_CODEX_SANDBOX_ALLOW=/path/a:/path/b

Semantics: each entry is treated as a writable root — the entry itself
and anything beneath it (by realpath) are allowed.  Non-absolute entries
are dropped with a warning.  A missing file, missing key, or malformed
YAML yields an empty list — the sandbox stays in its default deny-all-
outside-worktree mode.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_ENV_VAR = "HERMES_CODEX_SANDBOX_ALLOW"
_CONFIG_FILENAME = "codex-sandbox-allow.yaml"

_lock = threading.Lock()
# Cache key for the file-based source: (path, mtime_ns, size) → canonical roots.
# When the env var is set, we bypass the cache entirely (it's the authoritative
# source and is cheap to parse).
_file_cache_key: Optional[Tuple[str, int, int]] = None
_file_cache_value: List[str] = []


def _config_path() -> Path:
    """Return the allowlist config path under HERMES_HOME."""
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415
        return get_hermes_home() / _CONFIG_FILENAME
    except Exception:  # pragma: no cover — fallback for partial installs
        return Path(os.path.expanduser("~/.hermes")) / _CONFIG_FILENAME


def _canonicalize_entries(raw_entries) -> List[str]:
    """Filter to absolute paths and canonicalize via realpath.

    Drops anything that isn't a non-empty string or isn't absolute, with
    a warning.  The realpath canonicalization mirrors what the sandbox
    does to the target path, so symlinked allowlist entries match
    symlinked writes consistently.
    """
    out: List[str] = []
    if not isinstance(raw_entries, list):
        if raw_entries is not None:
            logger.warning(
                "codex sandbox allowlist: 'allowed_paths' must be a list, got %r — ignoring",
                type(raw_entries).__name__,
            )
        return out
    for entry in raw_entries:
        if not isinstance(entry, str) or not entry.strip():
            logger.warning("codex sandbox allowlist: skipping non-string entry %r", entry)
            continue
        if not os.path.isabs(entry):
            logger.warning(
                "codex sandbox allowlist: skipping non-absolute entry %r "
                "(entries must be absolute paths)",
                entry,
            )
            continue
        try:
            real = os.path.realpath(entry)
        except (OSError, ValueError) as exc:
            logger.warning("codex sandbox allowlist: cannot canonicalize %r: %s", entry, exc)
            continue
        out.append(real)
    return out


def _load_from_env() -> Optional[List[str]]:
    """If HERMES_CODEX_SANDBOX_ALLOW is set non-empty, parse it.

    Returns None when the env var is unset/empty so the file source is
    consulted.  An empty list means "env var was set but contained no
    usable entries" — and we deliberately honor that to give users a way
    to wipe the file allowlist for a single run (`HERMES_CODEX_SANDBOX_ALLOW= `).
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    entries = [p for p in raw.split(os.pathsep) if p.strip()]
    return _canonicalize_entries(entries)


def _load_from_file() -> List[str]:
    """Load + cache the YAML allowlist file.

    Cache invalidates on (path, mtime_ns, size) change — the same
    pattern hermes_cli.config uses for its main config file.
    """
    global _file_cache_key, _file_cache_value
    path = _config_path()
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        with _lock:
            _file_cache_key = None
            _file_cache_value = []
        return []
    except OSError as exc:
        logger.warning("codex sandbox allowlist: stat(%s) failed: %s", path, exc)
        return []

    key = (str(path), st.st_mtime_ns, st.st_size)
    with _lock:
        if _file_cache_key == key:
            return list(_file_cache_value)

    try:
        import yaml  # noqa: PLC0415
    except ImportError:  # pragma: no cover — PyYAML is a hard dep elsewhere
        logger.warning("codex sandbox allowlist: PyYAML unavailable; ignoring %s", path)
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        logger.warning(
            "codex sandbox allowlist: %s is not valid YAML (%s); "
            "falling back to no extra roots until fixed",
            path, exc,
        )
        return []
    except OSError as exc:
        logger.warning("codex sandbox allowlist: cannot read %s: %s", path, exc)
        return []

    if not isinstance(doc, dict):
        logger.warning(
            "codex sandbox allowlist: %s must be a YAML mapping with 'allowed_paths'; "
            "got %r",
            path, type(doc).__name__,
        )
        return []

    canon = _canonicalize_entries(doc.get("allowed_paths"))
    with _lock:
        _file_cache_key = key
        _file_cache_value = list(canon)
    return canon


def get_allowed_roots() -> List[str]:
    """Return the active list of canonical extra-writable roots.

    Env var (when set non-empty) takes precedence over the config file —
    handy for one-off test runs without touching the YAML.  Both sources
    can yield an empty list, in which case the sandbox stays in its
    worktree-only mode.
    """
    env = _load_from_env()
    if env is not None:
        return env
    return _load_from_file()


def is_path_allowed(target_realpath: str) -> bool:
    """Return True if target_realpath is at or under any allowlist root.

    Caller is expected to have already canonicalized the target via
    ``os.path.realpath``; ``_enforce_codex_sandbox`` does this.
    """
    roots = get_allowed_roots()
    if not roots:
        return False
    for root in roots:
        if target_realpath == root or target_realpath.startswith(root + os.sep):
            return True
    return False


def reset_cache_for_tests() -> None:
    """Test hook: clear the file cache so a fresh stat is done next call."""
    global _file_cache_key, _file_cache_value
    with _lock:
        _file_cache_key = None
        _file_cache_value = []
