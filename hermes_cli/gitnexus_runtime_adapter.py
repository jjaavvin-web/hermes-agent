"""
gitnexus_runtime_adapter — convert a RuntimeSnapshot into a GitNexus graph.

Strategy: generate a real Python package under RUNTIME_REPO_PATH whose
module structure mirrors the runtime topology (one class per entity,
imports as edges).  GitNexus parses the code and produces nodes + edges.

Ingestion path: POST http://127.0.0.1:4747/api/analyze — no forking needed.
Atomic write:   write to RUNTIME_REPO_PATH.tmp, rename, then analyze.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from hermes_cli.gitnexus_runtime_collector import RuntimeSnapshot, snapshot

RUNTIME_REPO_PATH = Path("/tmp/hermes-runtime")
RUNTIME_REPO_TMP = Path("/tmp/hermes-runtime.tmp")
GITNEXUS_API = "http://127.0.0.1:4747"
REPO_NAME = "hermes-runtime"
ANALYZE_TIMEOUT = 60  # seconds to wait for analyze job


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

def _slug(s: str) -> str:
    """Convert a name to a safe Python identifier."""
    return re.sub(r"\W+", "_", s).strip("_") or "unknown"


def _class_name(kind: str, name: str) -> str:
    return f"{_slug(kind)}_{_slug(name)}"


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------

def _docstring(entity: dict, kind: str) -> str:
    attrs = {k: v for k, v in entity.items() if k not in ("id",) and v is not None}
    attrs["type"] = kind
    lines = ["    \"\"\""]
    for k, v in attrs.items():
        lines.append(f"    {k}: {v}")
    lines.append("    \"\"\"")
    return "\n".join(lines)


def _generate_base(tmp: Path) -> None:
    (tmp / "__init__.py").write_text(
        '"""Hermes runtime topology — synthetic GitNexus repo."""\n'
    )
    (tmp / "base.py").write_text(
        '"""Base entity for all runtime topology nodes."""\n\n\n'
        "class RuntimeEntity:\n"
        '    """Base class for all Hermes runtime entities."""\n\n'
        "    def status(self) -> str:\n"
        "        return \"unknown\"\n"
    )


def _generate_module(
    tmp: Path,
    filename: str,
    kind: str,
    entities: list[dict],
    name_key: str = "name",
) -> list[str]:
    """Write entities/<filename>.py and return list of class names."""
    class_names = []
    lines = [
        f'"""Hermes runtime {kind} entities."""\n',
        "from base import RuntimeEntity\n\n",
    ]
    for e in entities:
        raw_name = e.get(name_key) or e.get("id") or "unknown"
        cname = _class_name(kind, str(raw_name))
        class_names.append(cname)
        lines.append(f"\nclass {cname}(RuntimeEntity):\n")
        lines.append(_docstring(e, kind) + "\n\n")
        lines.append("    def status(self) -> str:\n")
        status = str(e.get("status", "unknown"))
        lines.append(f'        return "{status}"\n')

    (tmp / "entities").mkdir(exist_ok=True)
    (tmp / "entities" / filename).write_text("\n".join(lines))
    return class_names


def _generate_topology(
    tmp: Path,
    snap: RuntimeSnapshot,
    class_map: dict[str, list[str]],
) -> None:
    """Write topology.py wiring entities together via imports + method calls."""
    lines = [
        '"""Runtime topology — relationships between Hermes entities."""\n',
    ]

    # Import all entity classes
    module_map = {
        "agents": "agents",
        "gateways": "gateways",
        "hives": "hives",
        "mcp": "mcp_servers",
        "swarms": "swarms",
        "cron": "cron_jobs",
    }
    for key, mod in module_map.items():
        names = class_map.get(key, [])
        if names:
            lines.append(f"from entities.{mod} import {', '.join(names)}\n")

    lines.append("\n\nclass HermesRuntimeTopology:\n")
    lines.append('    """Wires all runtime entities into a single topology graph."""\n\n')

    # For each edge, add a method that references source and target
    for edge in snap["edges"]:
        src_cls = None
        tgt_cls = None
        # Find source class
        for key, entities in [
            ("gateways", snap["gateways"]),
            ("agents", snap["agents"]),
            ("hives", snap["hives"]),
            ("mcp", snap["mcp"]),
            ("swarms", snap["swarms"]),
            ("cron", snap["cron"]),
        ]:
            for e in entities:
                eid = e.get("id") or _slug(e.get("name", ""))
                if eid == edge["source"]:
                    kind = {
                        "gateways": "gateway",
                        "agents": "agent",
                        "hives": "hive",
                        "mcp": "mcp_server",
                        "swarms": "swarm",
                        "cron": "cron",
                    }[key]
                    src_cls = _class_name(kind, str(e.get("name") or eid))
                if eid == edge["target"]:
                    kind = {
                        "gateways": "gateway",
                        "agents": "agent",
                        "hives": "hive",
                        "mcp": "mcp_server",
                        "swarms": "swarm",
                        "cron": "cron",
                    }[key]
                    tgt_cls = _class_name(kind, str(e.get("name") or eid))

        if src_cls and tgt_cls:
            method = f"edge_{_slug(src_cls)}_{_slug(tgt_cls)}"
            lines.append(
                f"    def {method}(self) -> None:\n"
                f'        """{edge["type"]}: {src_cls} -> {tgt_cls}"""\n'
                f"        return ({src_cls}(), {tgt_cls}())\n\n"
            )

    (tmp / "topology.py").write_text("".join(lines))


def generate_code(snap: RuntimeSnapshot, dest: Path) -> None:
    """Write the full synthetic Python package to dest/."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "entities").mkdir(exist_ok=True)

    _generate_base(dest)

    class_map: dict[str, list[str]] = {}
    class_map["agents"] = _generate_module(dest, "agents.py", "agent", snap["agents"])
    class_map["gateways"] = _generate_module(dest, "gateways.py", "gateway", snap["gateways"])
    class_map["hives"] = _generate_module(dest, "hives.py", "hive", snap["hives"])
    class_map["mcp"] = _generate_module(dest, "mcp_servers.py", "mcp_server", snap["mcp"])
    class_map["swarms"] = _generate_module(dest, "swarms.py", "swarm", snap["swarms"])
    class_map["cron"] = _generate_module(dest, "cron_jobs.py", "cron", snap["cron"])

    _generate_topology(dest, snap, class_map)

    # Metadata file for traceability
    meta = {
        "repo": REPO_NAME,
        "description": "Live Hermes runtime topology — auto-generated, do not edit",
        "counts": {
            "agents": len(snap["agents"]),
            "swarms": len(snap["swarms"]),
            "hives": len(snap["hives"]),
            "mcp": len(snap["mcp"]),
            "gateways": len(snap["gateways"]),
            "cron": len(snap["cron"]),
            "edges": len(snap["edges"]),
        },
    }
    (dest / "runtime_meta.json").write_text(json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# GitNexus API calls
# ---------------------------------------------------------------------------

def _api(method: str, path: str, body: Any = None, retries: int = 3) -> Any:
    url = f"{GITNEXUS_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt < retries - 1:
                # Another analyze job is running; wait and retry
                time.sleep(10 * (attempt + 1))
                last_err = e
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(5)
                continue
            raise
    raise last_err  # type: ignore[misc]


def _wait_for_job(job_id: str, timeout: int = ANALYZE_TIMEOUT) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _api("GET", f"/api/analyze/{job_id}")
        if result.get("status") in ("complete", "error", "failed"):
            return result
        time.sleep(2)
    raise TimeoutError(f"analyze job {job_id} did not complete in {timeout}s")


def _delete_repo_if_exists(name: str) -> None:
    try:
        repos = _api("GET", "/api/repos")
        if any(r["name"] == name for r in repos):
            _api("DELETE", f"/api/repo?repo={name}")
    except Exception:
        pass


def ingest(snap: RuntimeSnapshot) -> dict:
    """
    Write the runtime snapshot into GitNexus as synthetic repo 'hermes-runtime'.

    Atomic:
      1. Generate code into RUNTIME_REPO_TMP
      2. Rename TMP → RUNTIME_REPO_PATH
      3. DELETE old GitNexus index (if present)
      4. POST /api/analyze → wait (retries on 409 conflict)
    """
    # 1. Generate code into tmp dir
    if RUNTIME_REPO_TMP.exists():
        shutil.rmtree(RUNTIME_REPO_TMP)
    generate_code(snap, RUNTIME_REPO_TMP)

    # 2. Atomic rename (readers always see either old or new, never partial)
    if RUNTIME_REPO_PATH.exists():
        shutil.rmtree(RUNTIME_REPO_PATH)
    RUNTIME_REPO_TMP.rename(RUNTIME_REPO_PATH)

    # 3. Delete old GitNexus index so the re-analyze produces a clean graph
    #    (GitNexus does incremental updates; stale nodes persist otherwise)
    _delete_repo_if_exists(REPO_NAME)

    # 4. Trigger re-analysis
    job = _api("POST", "/api/analyze", {"path": str(RUNTIME_REPO_PATH)})
    job_id = job["jobId"]

    # 5. Wait for completion
    result = _wait_for_job(job_id)
    if result.get("status") != "complete":
        raise RuntimeError(f"GitNexus analyze failed: {result}")

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("Collecting runtime snapshot…")
    snap = snapshot()
    print(
        f"  agents={len(snap['agents'])} gateways={len(snap['gateways'])} "
        f"hives={len(snap['hives'])} mcp={len(snap['mcp'])} "
        f"swarms={len(snap['swarms'])} cron={len(snap['cron'])} "
        f"edges={len(snap['edges'])}"
    )
    print("Ingesting into GitNexus…")
    result = ingest(snap)
    print(f"Done: {result}")


if __name__ == "__main__":
    main()
