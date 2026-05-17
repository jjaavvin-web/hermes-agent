"""
gitnexus_runtime_collector — read-only snapshot of the live Hermes runtime.

Returns a RuntimeSnapshot dict ready for the adapter to turn into a GitNexus
graph.  All I/O is read-only: subprocess calls and filesystem reads only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import TypedDict

# Absolute paths for CLI tools (required when running under systemd with minimal PATH)
_HERMES = "/home/josep/.local/bin/hermes"
_TMUX = "/usr/bin/tmux"
_CRONTAB = "/usr/bin/crontab"


class RuntimeSnapshot(TypedDict):
    agents: list[dict]
    swarms: list[dict]
    hives: list[dict]
    mcp: list[dict]
    gateways: list[dict]
    cron: list[dict]
    edges: list[dict]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _safe_json(text: str) -> object:
    try:
        return json.loads(text)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def _collect_gateways() -> list[dict]:
    """Read gateway state from ~/.hermes/gateway_state.json."""
    path = Path.home() / ".hermes" / "gateway_state.json"
    try:
        data = json.loads(path.read_text())
        gw = {
            "id": "default",
            "name": "default",
            "pid": data.get("pid"),
            "status": data.get("gateway_state", "unknown"),
            "kind": data.get("kind", "hermes-gateway"),
            "platforms": list(data.get("platforms", {}).keys()),
            "active_agents": data.get("active_agents", 0),
        }
        return [gw]
    except Exception:
        return []


def _collect_agents() -> list[dict]:
    """
    Agents are hermes gateway profiles.  Enumerate from the gateway list
    output; only the profile names are reliably available without an
    active ruflo session.
    """
    output = _run([_HERMES, "gateway", "list"])
    agents = []
    for line in output.splitlines():
        m = re.match(r"\s*[✓✗]\s+(\S+)\s+—\s+(.+)", line)
        if m:
            name, note = m.group(1).strip(), m.group(2).strip()
            status = "running" if "PID" in note else "stopped"
            pid = None
            pid_m = re.search(r"PID\s+(\d+)", note)
            if pid_m:
                pid = int(pid_m.group(1))
            agents.append({"id": name, "name": name, "status": status, "pid": pid})
    return agents


def _collect_hives() -> list[dict]:
    """Return all active tmux sessions as hive candidates."""
    output = _run([_TMUX, "ls", "-F", "#{session_name}|#{session_created}"])
    hives = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        name = parts[0]
        created = parts[1] if len(parts) > 1 else ""
        hives.append({"id": name.replace("-", "_"), "name": name, "created": created})
    return hives


def _collect_mcp() -> list[dict]:
    """
    Parse `hermes mcp list` text output.  Falls back to reading
    ~/.hermes/mcp/ directory names if the command fails.
    """
    output = _run([_HERMES, "mcp", "list"])
    servers = []
    # Line format: "  Name   Transport   Tools   Status"
    # Data lines:  "  notion   /path...   13 selected   ✓ enabled"
    for line in output.splitlines():
        # Skip headers / blank lines
        if re.match(r"\s*(Name|─|MCP|$)", line):
            continue
        cols = re.split(r"\s{2,}", line.strip())
        if len(cols) >= 2:
            name = cols[0]
            if not name or name == "Name":
                continue
            status = "enabled" if any("enabled" in c for c in cols) else "disabled"
            servers.append({"id": name, "name": name, "status": status})

    if not servers:
        mcp_dir = Path.home() / ".hermes" / "mcp"
        if mcp_dir.is_dir():
            for p in mcp_dir.iterdir():
                if p.is_dir():
                    servers.append({"id": p.name, "name": p.name, "status": "unknown"})
    return servers


def _collect_swarms() -> list[dict]:
    """
    Swarms are hive-mind sessions — derived from hive tmux sessions.
    Each hive session maps to a swarm context.
    """
    hives = _collect_hives()
    # Treat each hive tmux session as a swarm
    swarms = []
    for h in hives:
        if "hive" in h["name"].lower() or "swarm" in h["name"].lower():
            swarms.append({
                "id": h["id"],
                "name": h["name"],
                "session": h["name"],
                "status": "active",
            })
    return swarms


def _collect_cron() -> list[dict]:
    """Collect cron jobs from crontab and hermes cron."""
    jobs = []

    # System crontab
    crontab = _run([_CRONTAB, "-l"])
    for line in crontab.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 5)
        if len(parts) >= 6:
            schedule = " ".join(parts[:5])
            cmd = parts[5]
            name = Path(cmd.split()[0]).name if cmd else "cron"
            jobs.append({
                "id": re.sub(r"\W+", "_", name),
                "name": name,
                "schedule": schedule,
                "command": cmd,
                "source": "crontab",
            })

    # Hermes cron
    hermes_cron_dir = Path.home() / ".hermes" / "cron.d"
    if hermes_cron_dir.is_dir():
        for f in hermes_cron_dir.iterdir():
            try:
                data = json.loads(f.read_text())
                jobs.append({
                    "id": re.sub(r"\W+", "_", data.get("name", f.stem)),
                    "name": data.get("name", f.stem),
                    "schedule": data.get("schedule", ""),
                    "command": data.get("command", ""),
                    "source": "hermes-cron",
                })
            except Exception:
                pass

    return jobs


def _infer_edges(
    agents: list[dict],
    swarms: list[dict],
    hives: list[dict],
    mcp: list[dict],
    gateways: list[dict],
    cron: list[dict],
) -> list[dict]:
    """Infer directed relationships between runtime entities."""
    edges = []

    # Each agent connects to the running gateway
    running_gw = next((g for g in gateways if g["status"] == "running"), None)
    if running_gw:
        for a in agents:
            if a["status"] == "running":
                edges.append({
                    "source": a["id"],
                    "target": running_gw["id"],
                    "type": "CONNECTS_TO",
                })

    # Swarms reference their hive session
    hive_ids = {h["name"]: h["id"] for h in hives}
    for s in swarms:
        if s["session"] in hive_ids:
            edges.append({
                "source": s["id"],
                "target": hive_ids[s["session"]],
                "type": "RUNS_IN",
            })

    # Gateway uses MCP servers
    if running_gw:
        for m in mcp:
            edges.append({
                "source": running_gw["id"],
                "target": m["id"],
                "type": "USES_MCP",
            })

    # Cron jobs invoke gateway or agents
    if running_gw:
        for c in cron:
            edges.append({
                "source": c["id"],
                "target": running_gw["id"],
                "type": "SCHEDULED_VIA",
            })

    return edges


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def snapshot() -> RuntimeSnapshot:
    """
    Return a point-in-time snapshot of the live Hermes runtime.

    All reads are non-mutating: subprocess + filesystem only.
    Budget: 60 seconds total.
    """
    gateways = _collect_gateways()
    agents = _collect_agents()
    hives = _collect_hives()
    mcp = _collect_mcp()
    swarms = _collect_swarms()
    cron = _collect_cron()
    edges = _infer_edges(agents, swarms, hives, mcp, gateways, cron)

    return RuntimeSnapshot(
        agents=agents,
        swarms=swarms,
        hives=hives,
        mcp=mcp,
        gateways=gateways,
        cron=cron,
        edges=edges,
    )


if __name__ == "__main__":
    import pprint
    pprint.pprint(snapshot())
