"""
gitnexus_repo_manager — multi-repo indexing for GitNexus.

Reads ~/.hermes/gitnexus-repos.yml, indexes listed repos that are not
already indexed, and auto-discovers new git repos under parent_dirs.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore

GITNEXUS_API = "http://127.0.0.1:4747"
CONFIG_PATH = Path.home() / ".hermes" / "gitnexus-repos.yml"
ANALYZE_TIMEOUT = 600  # 10 min for large repos


# ---------------------------------------------------------------------------
# GitNexus API helpers
# ---------------------------------------------------------------------------

def _api(method: str, path: str, body: object = None) -> object:
    url = f"{GITNEXUS_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _existing_repos() -> set[str]:
    try:
        repos = _api("GET", "/api/repos")
        return {r["name"] for r in repos}
    except Exception:
        return set()


def _wait_for_job(job_id: str, label: str, timeout: int = ANALYZE_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _api("GET", f"/api/analyze/{job_id}")
        status = result.get("status")
        pct = result.get("progress", {}).get("percent", 0)
        print(f"  [{label}] {status} {pct}%", end="\r")
        if status in ("complete", "error", "failed"):
            print()
            return status == "complete"
        time.sleep(5)
    print()
    return False


def _index_repo(path: Path, label: str) -> bool:
    """Trigger GitNexus analyze for a repo and wait for it to finish."""
    print(f"  Indexing {label} ({path})…")
    try:
        job = _api("POST", "/api/analyze", {"path": str(path)})
    except Exception as e:
        print(f"  ERROR triggering analyze for {label}: {e}")
        return False
    return _wait_for_job(job["jobId"], label)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"repos": [], "auto_discover": {}}

    text = CONFIG_PATH.read_text()

    if yaml is not None:
        return yaml.safe_load(text) or {}

    # Minimal YAML parser for our simple schema (no pyyaml available)
    config: dict = {"repos": [], "auto_discover": {}}
    current_repo: dict | None = None
    in_repos = False
    in_auto = False
    in_parent_dirs = False
    in_exclude = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        if stripped == "repos:":
            in_repos = True
            in_auto = False
            continue
        if stripped == "auto_discover:":
            in_auto = True
            in_repos = False
            if current_repo:
                config["repos"].append(current_repo)
                current_repo = None
            continue

        if in_repos:
            if stripped.startswith("- path:"):
                if current_repo:
                    config["repos"].append(current_repo)
                current_repo = {"path": stripped[7:].strip(), "label": "", "reindex_on_commit": False}
            elif stripped.startswith("label:") and current_repo:
                current_repo["label"] = stripped[6:].strip()
            elif stripped.startswith("reindex_on_commit:") and current_repo:
                current_repo["reindex_on_commit"] = "true" in stripped.lower()

        if in_auto:
            if "parent_dirs:" in stripped:
                in_parent_dirs = True
                in_exclude = False
            elif "exclude:" in stripped:
                in_exclude = True
                in_parent_dirs = False
                config["auto_discover"].setdefault("exclude", [])
            elif in_parent_dirs and stripped.startswith("-"):
                config["auto_discover"].setdefault("parent_dirs", []).append(
                    stripped.lstrip("- ").strip()
                )
            elif in_exclude and stripped.startswith("-"):
                config["auto_discover"].setdefault("exclude", []).append(
                    stripped.lstrip("- ").strip()
                )

    if current_repo:
        config["repos"].append(current_repo)

    return config


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------

def _is_git_repo(p: Path) -> bool:
    return (p / ".git").exists()


def _discover_repos(parent_dirs: list[str], excludes: list[str]) -> list[Path]:
    found = []
    for parent_str in parent_dirs:
        parent = Path(parent_str).expanduser()
        if not parent.is_dir():
            continue
        for candidate in parent.iterdir():
            if not candidate.is_dir():
                continue
            # Check exclude patterns (simple glob-style)
            skip = False
            for ex in excludes:
                ex_name = ex.lstrip("**/").lstrip("*")
                if candidate.name == ex_name or candidate.name.startswith(ex_name):
                    skip = True
                    break
            if skip:
                continue
            if _is_git_repo(candidate):
                found.append(candidate)
    return found


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    print("GitNexus Repo Manager")
    print("=" * 40)

    config = _load_config()
    existing = _existing_repos()
    print(f"Currently indexed repos: {sorted(existing)}")

    indexed_count = 0
    skipped_count = 0

    # 1. Explicitly listed repos
    for repo_cfg in config.get("repos", []):
        path = Path(repo_cfg.get("path", "")).expanduser()
        label = repo_cfg.get("label") or path.name
        if not path.exists():
            print(f"  SKIP {label}: path does not exist ({path})")
            skipped_count += 1
            continue
        if label in existing:
            print(f"  SKIP {label}: already indexed")
            skipped_count += 1
            continue
        ok = _index_repo(path, label)
        if ok:
            indexed_count += 1
            existing.add(label)
        else:
            print(f"  FAILED {label}")

    # 2. Auto-discovered repos
    auto = config.get("auto_discover", {})
    parent_dirs = auto.get("parent_dirs", [])
    excludes = auto.get("exclude", [])

    if parent_dirs:
        discovered = _discover_repos(parent_dirs, excludes)
        print(f"\nAuto-discovered {len(discovered)} git repos under {parent_dirs}")
        for repo_path in discovered:
            label = repo_path.name
            if label in existing:
                print(f"  SKIP {label}: already indexed")
                skipped_count += 1
                continue
            ok = _index_repo(repo_path, label)
            if ok:
                indexed_count += 1
                existing.add(label)
            else:
                print(f"  FAILED {label}")

    print(f"\nDone. Indexed: {indexed_count}, Skipped: {skipped_count}")


if __name__ == "__main__":
    run()
