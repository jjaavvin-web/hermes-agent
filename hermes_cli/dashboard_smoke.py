"""Route-level smoke harness for the Hermes dashboard FastAPI app.

The dashboard historically mounted several router modules inside broad
``try/except`` blocks.  A broken import could therefore drop an entire tab's API
surface while ``/api/status`` stayed green.  This module gives both humans and
pytest a single, runtime-derived route smoke that:

* enumerates FastAPI routes from ``app.routes``;
* compares known dashboard router modules against what actually mounted;
* probes GET routes with the same injected SPA session token the browser uses;
* records cold/warm latency and a machine-readable JSON status file.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import importlib
import inspect
import json
import re
import time
from pathlib import Path
from typing import Any, Coroutine, Iterable, Mapping, Sequence, cast

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from hermes_constants import get_hermes_home

SESSION_TOKEN_RE = re.compile(
    r"window\.__HERMES_SESSION_TOKEN__\s*=\s*['\"]([^'\"]+)['\"]"
)
SESSION_HEADER_NAME = "X-Hermes-Session-Token"
DEFAULT_STATUS_PATH = "state/dashboard-smoke-status.json"

# These are the dashboard tab/router modules that web_server mounts through
# import-time try/except blocks.  Expected paths are derived from the routers at
# runtime, so adding a new route inside a known router automatically expands the
# count.  Adding a new router module should update this small registry.
DASHBOARD_ROUTER_MODULES: tuple[str, ...] = (
    "hermes_cli.dashboard_health",
    "hermes_cli.dashboard_codex_sessions",
    "hermes_cli.dashboard_os",
    "hermes_cli.dashboard_connectome",
    "hermes_cli.dashboard_learning",
    "hermes_cli.dashboard_cost",
    "hermes_cli.dashboard_get_some",
    "hermes_cli.dashboard_command_center",
)

# Dynamic path params need live ids.  The harness seeds what it can from list
# endpoints; if no live object exists (e.g. no hives), that detail route is
# reported as skipped rather than faked with a guaranteed 404 id.
_STATIC_PATH_PARAM_SAMPLES: Mapping[str, str] = {
    "name": "hermes",
    "node_id": "hermes",
}


@dataclass
class ProbeResult:
    path_template: str
    path: str | None
    name: str
    status_code: int | None = None
    ok: bool = False
    body_bytes: int = 0
    cold_ms: float | None = None
    warm_ms: float | None = None
    response_model: bool = False
    content_type: str = ""
    error: str | None = None
    skipped: bool = False
    skip_reason: str | None = None


@dataclass
class DashboardSmokeReport:
    generated_at: str
    ok: bool
    route_count_total: int
    get_route_count_total: int
    probed_route_count: int
    skipped_route_count: int
    expected_dashboard_router_count: int
    mounted_dashboard_router_count: int
    missing_dashboard_routes: list[str] = field(default_factory=list)
    unexpected_dashboard_router_routes: list[str] = field(default_factory=list)
    expected_import_errors: dict[str, str] = field(default_factory=dict)
    probes: list[ProbeResult] = field(default_factory=list)
    status_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["probes"] = [asdict(p) for p in self.probes]
        return data


def _route_methods(route: Any) -> set[str]:
    methods = getattr(route, "methods", None)
    return set(methods or [])


def iter_get_routes(app: FastAPI, prefixes: Sequence[str] = ("/api/dashboard",)) -> list[APIRoute]:
    """Return runtime FastAPI GET routes under the requested prefixes."""
    routes: list[APIRoute] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not isinstance(route, APIRoute):
            continue
        if "GET" not in _route_methods(route):
            continue
        if prefixes and not any(path.startswith(prefix) for prefix in prefixes):
            continue
        routes.append(route)
    return routes


def all_get_route_count(app: FastAPI) -> int:
    return sum(
        1
        for route in app.routes
        if isinstance(route, APIRoute) and "GET" in _route_methods(route)
    )


def _router_get_paths(router: Any) -> set[str]:
    paths: set[str] = set()
    for route in getattr(router, "routes", []):
        path = getattr(route, "path", "")
        if "GET" in _route_methods(route):
            paths.add(path)
    return paths


def expected_dashboard_router_paths(
    modules: Sequence[str] = DASHBOARD_ROUTER_MODULES,
) -> tuple[set[str], dict[str, str]]:
    """Import known dashboard routers independently and return their GET paths.

    Import errors are part of the signal: if a router module currently cannot be
    imported, the smoke report goes red instead of letting web_server's
    try/except turn the failure into a fake-green app.
    """
    expected: set[str] = set()
    errors: dict[str, str] = {}
    for module_name in modules:
        try:
            module = importlib.import_module(module_name)
            router = getattr(module, "router")
            expected.update(_router_get_paths(router))
        except Exception as exc:  # noqa: BLE001 - report, do not hide
            errors[module_name] = f"{type(exc).__name__}: {exc}"
    return expected, errors


def mounted_dashboard_router_paths(app: FastAPI, expected_paths: Iterable[str]) -> set[str]:
    actual_paths = {route.path for route in iter_get_routes(app, prefixes=("/api/dashboard",))}
    expected = set(expected_paths)
    return actual_paths & expected


def compare_dashboard_router_mounts(
    app: FastAPI,
    *,
    expected_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    expected, import_errors = expected_dashboard_router_paths() if expected_paths is None else (set(expected_paths), {})
    mounted = mounted_dashboard_router_paths(app, expected)
    missing = sorted(expected - mounted)
    unexpected = sorted(mounted - expected)
    return {
        "ok": not missing and not unexpected and not import_errors,
        "expected_count": len(expected),
        "mounted_count": len(mounted),
        "missing": missing,
        "unexpected": unexpected,
        "import_errors": import_errors,
    }


def extract_session_token(html: str) -> str:
    """Extract the dashboard SPA token from injected HTML like the browser does."""
    match = SESSION_TOKEN_RE.search(html)
    if not match:
        raise ValueError("window.__HERMES_SESSION_TOKEN__ not found in dashboard HTML")
    return match.group(1)


def get_spa_session_token(client: TestClient) -> str:
    response = client.get("/")
    if response.status_code != 200:
        raise RuntimeError(f"dashboard root returned HTTP {response.status_code}")
    return extract_session_token(response.text)


def _json_or_none(response: Any) -> Any | None:
    try:
        return response.json()
    except Exception:
        return None


def _first_id(rows: Any, keys: Sequence[str] = ("id", "name", "sid")) -> str | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in keys:
            value = row.get(key)
            if value:
                return str(value)
    return None


def collect_route_seeds(client: TestClient, headers: Mapping[str, str]) -> dict[str, str]:
    """Collect live ids needed to probe parameterized dashboard routes."""
    seeds: dict[str, str] = dict(_STATIC_PATH_PARAM_SAMPLES)

    def get_json(path: str) -> Any | None:
        try:
            response = client.get(path, headers=dict(headers))
            if response.status_code == 200:
                return _json_or_none(response)
        except Exception:
            return None
        return None

    nexus = get_json("/api/dashboard/nexus-health")
    if isinstance(nexus, dict):
        node_id = _first_id(nexus.get("nodes"), keys=("id", "name"))
        if node_id:
            seeds["node_id"] = node_id

    hives = get_json("/api/dashboard/hives")
    if isinstance(hives, dict):
        hive_id = _first_id(hives.get("hives"), keys=("id", "hive_id", "name"))
        if hive_id:
            seeds["hive_id"] = hive_id

    codex = get_json("/api/dashboard/codex-sessions")
    if isinstance(codex, dict):
        sid = _first_id(
            codex.get("sessions") or codex.get("rows") or codex.get("items"),
            keys=("sid", "id", "session_id"),
        )
        if sid:
            seeds["sid"] = sid

    connectome = get_json("/api/dashboard/connectome")
    if isinstance(connectome, dict):
        cluster_id = _first_id(
            connectome.get("clusters") or connectome.get("nodes"),
            keys=("id", "cluster_id", "name"),
        )
        if cluster_id:
            seeds["cluster_id"] = cluster_id
    return seeds


_PATH_PARAM_RE = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def materialize_path(path_template: str, seeds: Mapping[str, str]) -> tuple[str | None, str | None]:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = seeds.get(key)
        if value is None:
            missing.append(key)
            return match.group(0)
        return value

    path = _PATH_PARAM_RE.sub(replace, path_template)
    if missing:
        return None, f"missing seed for path parameter(s): {', '.join(sorted(set(missing)))}"
    return path, None


def _run_endpoint_direct(route: APIRoute) -> Any:
    value = route.endpoint()
    if inspect.isawaitable(value):
        return asyncio.run(cast(Coroutine[Any, Any, Any], value))
    return value


def _probe_stream_route(route: APIRoute, path: str) -> ProbeResult:
    start = time.perf_counter()
    try:
        response = _run_endpoint_direct(route)
        elapsed = (time.perf_counter() - start) * 1000
        ok = isinstance(response, StreamingResponse)
        return ProbeResult(
            path_template=route.path,
            path=path,
            name=route.name,
            status_code=200 if ok else None,
            ok=ok,
            body_bytes=1 if ok else 0,
            cold_ms=elapsed,
            warm_ms=elapsed,
            response_model=bool(route.response_model),
            content_type=getattr(response, "media_type", "") or "",
            error=None if ok else f"expected StreamingResponse, got {type(response).__name__}",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            path_template=route.path,
            path=path,
            name=route.name,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def probe_get_route(
    client: TestClient,
    route: APIRoute,
    *,
    path: str,
    headers: Mapping[str, str],
) -> ProbeResult:
    if path.endswith("/stream"):
        return _probe_stream_route(route, path)

    result = ProbeResult(
        path_template=route.path,
        path=path,
        name=route.name,
        response_model=bool(route.response_model),
    )
    timings: list[float] = []
    response = None
    try:
        for _ in range(2):
            start = time.perf_counter()
            response = client.get(path, headers=dict(headers))
            timings.append((time.perf_counter() - start) * 1000)
        assert response is not None
        result.status_code = response.status_code
        result.body_bytes = len(response.content or b"")
        result.content_type = response.headers.get("content-type", "")
        result.cold_ms = timings[0]
        result.warm_ms = timings[1] if len(timings) > 1 else None
        if response.status_code != 200:
            result.error = f"HTTP {response.status_code}: {response.text[:200]}"
            return result
        if not response.content:
            result.error = "empty response body"
            return result
        if route.response_model:
            parsed = _json_or_none(response)
            if parsed is None:
                result.error = "response_model route did not return parseable JSON"
                return result
        result.ok = True
        return result
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
        if timings:
            result.cold_ms = timings[0]
            result.warm_ms = timings[1] if len(timings) > 1 else None
        return result


def run_dashboard_smoke(
    app: FastAPI,
    *,
    prefixes: Sequence[str] = ("/api/dashboard",),
    status_file: str | Path | None = None,
) -> DashboardSmokeReport:
    routes = iter_get_routes(app, prefixes=prefixes)
    mount = compare_dashboard_router_mounts(app)
    probes: list[ProbeResult] = []

    with TestClient(app) as client:
        token = get_spa_session_token(client)
        headers = {SESSION_HEADER_NAME: token}
        seeds = collect_route_seeds(client, headers)
        for route in routes:
            path, skip_reason = materialize_path(route.path, seeds)
            if skip_reason:
                probes.append(
                    ProbeResult(
                        path_template=route.path,
                        path=None,
                        name=route.name,
                        response_model=bool(route.response_model),
                        skipped=True,
                        skip_reason=skip_reason,
                    )
                )
                continue
            assert path is not None
            probes.append(probe_get_route(client, route, path=path, headers=headers))

    ok = bool(mount["ok"]) and all(p.ok or p.skipped for p in probes)
    report = DashboardSmokeReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        ok=ok,
        route_count_total=len(app.routes),
        get_route_count_total=all_get_route_count(app),
        probed_route_count=sum(1 for p in probes if not p.skipped),
        skipped_route_count=sum(1 for p in probes if p.skipped),
        expected_dashboard_router_count=int(mount["expected_count"]),
        mounted_dashboard_router_count=int(mount["mounted_count"]),
        missing_dashboard_routes=list(mount["missing"]),
        unexpected_dashboard_router_routes=list(mount["unexpected"]),
        expected_import_errors=dict(mount["import_errors"]),
        probes=probes,
    )
    if status_file is not None:
        path = Path(status_file)
        if not path.is_absolute():
            path = get_hermes_home() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        report.status_file = str(path)
        # Persist the file path in the just-written payload too.
        path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return report


def _print_summary(report: DashboardSmokeReport) -> None:
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test Hermes dashboard routes.")
    parser.add_argument(
        "--status-file",
        default=DEFAULT_STATUS_PATH,
        help="JSON status file path. Relative paths live under HERMES_HOME.",
    )
    parser.add_argument(
        "--prefix",
        action="append",
        dest="prefixes",
        help="Route prefix to probe. Repeatable. Defaults to /api/dashboard.",
    )
    args = parser.parse_args(argv)

    from hermes_cli.web_server import app

    report = run_dashboard_smoke(
        app,
        prefixes=tuple(args.prefixes or ("/api/dashboard",)),
        status_file=args.status_file,
    )
    _print_summary(report)
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
