"""Real MVMS lesson recorder for the reflect-promote drainer (MEM-10 backend).

I/O-heavy by design — kept out of ``agent.reflect_promote`` so that module
stays pure and import-light, and so tests can inject a fake ``LessonRecorder``
without ever constructing this. This file talks to the constrained
``mvms-writer`` MCP over a stdio JSON-RPC subprocess, mirroring the
``WriterSession`` pattern in ``~/.hermes/scripts/kanban-mvms-bridge.py``.

Nothing here runs unless ``reflect.promotion_enabled`` is True in config.yaml
AND a real promotion is requested (drainer flag-on / web approve with flag-on).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# Same canonical paths used by kanban-mvms-bridge.py.
ENV_FILE = "/home/josep/workspace/goattrade-system/.env"
PY_BIN = "/home/josep/.local/share/hermes-agent/venv/bin/python"
WRITER_MCP = "/home/josep/.hermes/mcp/mvms-writer/mvms_writer_mcp.py"


def _load_env(env_file: str = ENV_FILE) -> dict:
    out: dict = {}
    if not os.path.exists(env_file):
        return out
    for line in Path(env_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("\"'")
    return out


class MvmsWriterRecorder:
    """Spawns one mvms-writer MCP subprocess and records lessons through it.

    Implements the ``LessonRecorder`` protocol from ``agent.reflect_promote``.
    Each ``record_lesson`` call performs exactly one ``mvms_record_lesson`` MCP
    call (which is itself a single INSERT or a 24h-dedup no-op).
    """

    def __init__(self, env: dict | None = None) -> None:
        self._env_overrides = env
        self._proc: subprocess.Popen | None = None
        self._id = 0

    # -- lifecycle -----------------------------------------------------------
    def _ensure_started(self) -> None:
        if self._proc is not None:
            return
        env = self._env_overrides if self._env_overrides is not None else _load_env()
        dsn = env.get("SUPABASE_DB_URL") or env.get("DATABASE_URL") or ""
        if not dsn:
            raise RuntimeError(
                "MVMS DB URL missing: set SUPABASE_DB_URL or DATABASE_URL in "
                f"{ENV_FILE} before enabling reflect.promotion_enabled"
            )
        proc_env = {**os.environ, **env, "MVMS_DATABASE_URL": dsn}
        self._proc = subprocess.Popen(
            [PY_BIN, WRITER_MCP],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=proc_env,
        )
        self._send({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "reflect-promote-drainer", "version": "1"}},
        })
        self._recv()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _send(self, message: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        self._proc.stdin.flush()

    def _recv(self) -> dict:
        assert self._proc is not None and self._proc.stdout is not None
        return json.loads(self._proc.stdout.readline())

    # -- LessonRecorder ------------------------------------------------------
    def record_lesson(
        self,
        *,
        project: str,
        situation: str,
        mistake_or_insight: str,
        correction: str,
        evidence_refs: list[str],
        source: str,
        tags: list[str],
        importance: int,
    ) -> dict:
        self._ensure_started()
        self._id += 1
        self._send({
            "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
            "params": {
                "name": "mvms_record_lesson",
                "arguments": {
                    "project": project,
                    "situation": situation,
                    "mistake_or_insight": mistake_or_insight,
                    "correction": correction,
                    "evidence_refs": evidence_refs,
                    "tags": tags,
                    "importance": importance,
                    "source": source,
                },
            },
        })
        reply = self._recv()
        return reply.get("result", {}).get("structuredContent", {})

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        self._proc = None
