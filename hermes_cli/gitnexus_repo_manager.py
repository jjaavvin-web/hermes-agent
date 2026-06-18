"""
gitnexus_repo_manager — multi-repo indexing for GitNexus.

Reads ~/.hermes/gitnexus-repos.yml, indexes listed repos that are not
already indexed, and auto-discovers new git repos under parent_dirs.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore

GITNEXUS_API = "http://127.0.0.1:4747"
GITNEXUS_CONTAINER = "gitnexus-server"
GITNEXUS_REPOS_ROOT = "/data/gitnexus/repos"
CONFIG_PATH = Path.home() / ".hermes" / "gitnexus-repos.yml"
ANALYZE_TIMEOUT = 600  # 10 min for large repos
HOOK_MARKER_START = "# >>> gitnexus-reindex >>>"
HOOK_MARKER_END = "# <<< gitnexus-reindex <<<"
HOOK_PYTHON = "/home/josep/.local/share/hermes-agent/venv/bin/python"
HOOK_LOG = "$HOME/.hermes/logs/gitnexus-reindex.log"
HOOK_LOCK_DIR = "$HOME/.hermes/locks"


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


def _safe_label(label: str) -> str:
    """Return a Docker-path-safe repo label or raise ValueError."""
    if not label or label in {".", ".."} or "/" in label or "\\" in label:
        raise ValueError(f"unsafe GitNexus repo label: {label!r}")
    if label.startswith("-"):
        raise ValueError(f"unsafe GitNexus repo label: {label!r}")
    return label


def _container_repo_path(label: str) -> str:
    return f"{GITNEXUS_REPOS_ROOT}/{_safe_label(label)}"


def _run_docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", GITNEXUS_CONTAINER, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _recreate_container_staging(label: str) -> str:
    container_path = _container_repo_path(label)
    quoted_path = shlex.quote(container_path)
    _run_docker("sh", "-c", f"rm -rf {quoted_path} && mkdir -p {quoted_path}")
    return container_path


def _archive_tracked_files_to_container(host_path: Path, container_path: str) -> None:
    """Stream tracked files from host git HEAD into the GitNexus container."""
    archive_proc = subprocess.Popen(
        ["git", "-C", str(host_path), "archive", "--format=tar", "HEAD"],
        stdout=subprocess.PIPE,
    )
    try:
        tar_proc = subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
                GITNEXUS_CONTAINER,
                "tar",
                "-x",
                "-C",
                container_path,
            ],
            stdin=archive_proc.stdout,
        )
        if archive_proc.stdout is not None:
            archive_proc.stdout.close()
        tar_rc = tar_proc.wait()
        archive_rc = archive_proc.wait()
    finally:
        if archive_proc.poll() is None:
            archive_proc.kill()
            archive_proc.wait()
    if archive_rc != 0:
        raise subprocess.CalledProcessError(archive_rc, archive_proc.args)
    if tar_rc != 0:
        raise subprocess.CalledProcessError(tar_rc, tar_proc.args)


def _container_secret_hits(container_path: str) -> list[str]:
    quoted_path = shlex.quote(container_path)
    result = _run_docker(
        "sh",
        "-c",
        f"find {quoted_path} \\( -name .env -o -name auth.json \\) -print",
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _stage_repo_in_container(host_path: Path, label: str) -> str | None:
    """Stage tracked repo files inside the GitNexus container and secret-check them."""
    safe_label = _safe_label(label)
    container_path = _recreate_container_staging(safe_label)
    print(f"  Staging {safe_label}: {host_path} -> {container_path}")
    _archive_tracked_files_to_container(host_path, container_path)
    secret_hits = _container_secret_hits(container_path)
    if secret_hits:
        print(f"  ABORT {safe_label}: secret guard found forbidden tracked files:")
        for hit in secret_hits:
            print(f"    {hit}")
        return None
    return container_path


_ANALYZE_BUSY_RE = re.compile(r"job\s+([0-9a-fA-F][0-9a-fA-F-]{7,})")


def _post_analyze(container_path: str, label: str, *, deadline: float) -> dict:
    """POST /api/analyze, coalescing with GitNexus's single global job slot.

    GitNexus runs one analysis at a time across ALL repos; a concurrent POST
    returns HTTP 409 with the in-flight job id. Rather than dropping this
    repo's reindex (leaving it stale until its next commit), wait for the
    busy job to finish and retry, until our job is accepted or the deadline
    passes.
    """
    while True:
        try:
            return _api("POST", "/api/analyze", {"path": container_path})
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise
            body = e.read().decode("utf-8", "replace")
            match = _ANALYZE_BUSY_RE.search(body)
            if not match or time.time() >= deadline:
                raise
            busy_job = match.group(1)
            print(f"  [{label}] GitNexus busy (job {busy_job[:8]}); waiting to reindex…")
            _wait_for_job(busy_job, f"{label}:busy", timeout=max(1, int(deadline - time.time())))


def _index_repo(path: Path, label: str) -> bool:
    """Stage a repo inside the GitNexus container, analyze it, and wait."""
    print(f"  Indexing {label} ({path})…")
    try:
        container_path = _stage_repo_in_container(path, label)
        if container_path is None:
            return False
        job = _post_analyze(container_path, label, deadline=time.time() + ANALYZE_TIMEOUT)
    except Exception as e:
        print(f"  ERROR triggering analyze for {label}: {e}")
        return False
    return _wait_for_job(job["jobId"], label)


# ---------------------------------------------------------------------------
# Config loading and repo resolution
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"repos": [], "auto_discover": {}}

    text = CONFIG_PATH.read_text(encoding="utf-8")

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


def _expanded_path(path: str | Path) -> Path:
    return Path(path).expanduser()


def _resolved_path(path: str | Path) -> Path:
    return _expanded_path(path).resolve(strict=False)


def _repo_label(repo_cfg: dict) -> str:
    path = _expanded_path(repo_cfg.get("path", ""))
    return repo_cfg.get("label") or path.name


def _resolve_repo_selection(
    config: dict,
    *,
    repo_label: str | None = None,
    host_path: str | None = None,
) -> tuple[Path, str]:
    """Resolve --repo/--path into a host path and GitNexus label."""
    repos = config.get("repos", [])
    if repo_label:
        for repo_cfg in repos:
            label = _repo_label(repo_cfg)
            if label == repo_label:
                return _expanded_path(repo_cfg.get("path", "")), label
        raise ValueError(f"repo label not found in {CONFIG_PATH}: {repo_label}")

    if host_path is None:
        raise ValueError("either repo_label or host_path is required")

    requested = _expanded_path(host_path)
    requested_resolved = _resolved_path(requested)
    for repo_cfg in repos:
        cfg_path = _expanded_path(repo_cfg.get("path", ""))
        if _resolved_path(cfg_path) == requested_resolved:
            return requested, _repo_label(repo_cfg)
    return requested, requested.name


# ---------------------------------------------------------------------------
# Hook installation
# ---------------------------------------------------------------------------

def _hook_block(label: str) -> str:
    safe_label = _safe_label(label)
    return f"""{HOOK_MARKER_START}
# GitNexus real-time reindex hook installed by hermes_cli.gitnexus_repo_manager.
(
  mkdir -p "$HOME/.hermes/logs" {HOOK_LOCK_DIR}
  repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
  lockfile="{HOOK_LOCK_DIR}/gitnexus-reindex-{safe_label}.lock"
  echo "[$(date -Is)] gitnexus hook for {safe_label}: repo=$repo_root" >> {HOOK_LOG}
  if flock -n -E 75 "$lockfile" {HOOK_PYTHON} -m hermes_cli.gitnexus_repo_manager --path "$repo_root" >> {HOOK_LOG} 2>&1; then
    echo "[$(date -Is)] gitnexus hook for {safe_label}: complete" >> {HOOK_LOG}
  else
    rc=$?
    if [ "$rc" -eq 75 ]; then
      echo "[$(date -Is)] gitnexus hook for {safe_label}: coalesced because lock is busy" >> {HOOK_LOG}
    else
      echo "[$(date -Is)] gitnexus hook for {safe_label}: failed rc=$rc" >> {HOOK_LOG}
    fi
  fi
) >/dev/null 2>&1 &
{HOOK_MARKER_END}"""


def _replace_marker_block(existing: str, block: str) -> str:
    pattern = re.compile(
        rf"\n?{re.escape(HOOK_MARKER_START)}.*?{re.escape(HOOK_MARKER_END)}\n?",
        re.DOTALL,
    )
    trimmed_block = f"\n{block}\n"
    if pattern.search(existing):
        new_text = pattern.sub(trimmed_block, existing).rstrip() + "\n"
    else:
        prefix = existing.rstrip() + "\n" if existing else "#!/bin/sh\n"
        new_text = prefix + trimmed_block.lstrip("\n")
    return new_text


def _git_common_dir(repo_path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    )
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = repo_path / git_dir
    return git_dir


def _install_hook(repo_path: Path, label: str, hook_name: str) -> Path:
    hooks_dir = _git_common_dir(repo_path) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / hook_name
    existing = hook_path.read_text(encoding="utf-8") if hook_path.exists() else ""
    hook_path.write_text(_replace_marker_block(existing, _hook_block(label)), encoding="utf-8")
    mode = hook_path.stat().st_mode
    hook_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hook_path


def install_hooks(config: dict) -> int:
    installed = 0
    for repo_cfg in config.get("repos", []):
        if not repo_cfg.get("reindex_on_commit"):
            continue
        repo_path = _expanded_path(repo_cfg.get("path", ""))
        label = _repo_label(repo_cfg)
        if not repo_path.exists():
            print(f"  SKIP hook {label}: path does not exist ({repo_path})")
            continue
        for hook_name in ("post-commit", "post-merge"):
            hook_path = _install_hook(repo_path, label, hook_name)
            installed += 1
            print(f"  Installed {hook_name} hook for {label}: {hook_path}")
    return installed


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

def _run_single_repo(config: dict, *, repo_label: str | None, host_path: str | None) -> bool:
    path, label = _resolve_repo_selection(config, repo_label=repo_label, host_path=host_path)
    if not path.exists():
        print(f"  FAILED {label}: path does not exist ({path})")
        return False
    return _index_repo(path, label)


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage GitNexus repo indexing")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--repo", help="Config repo label to stage and reindex")
    selector.add_argument("--path", help="Host repo path to stage and reindex")
    parser.add_argument("--install-hooks", action="store_true", help="Install post-commit/post-merge hooks")
    args = parser.parse_args(argv)

    print("GitNexus Repo Manager")
    print("=" * 40)

    config = _load_config()

    if args.install_hooks:
        count = install_hooks(config)
        print(f"\nDone. Installed/updated hooks: {count}")
        if not (args.repo or args.path):
            return

    if args.repo or args.path:
        ok = _run_single_repo(config, repo_label=args.repo, host_path=args.path)
        print(f"\nDone. Indexed: {1 if ok else 0}, Skipped: {0 if ok else 1}")
        return

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
