"""Route-level smoke harness for the Hermes dashboard FastAPI app.

The dashboard historically mounted several router modules inside broad
``try/except`` blocks.  A broken import could therefore drop an entire tab's API
surface while ``/api/status`` stayed green.  This module gives both humans and
pytest a single, runtime-derived route smoke that:

* enumerates every route registered on ``app.routes``;
* compares known dashboard router modules against what actually mounted;
* optionally compares the full app route table against a checked-in manifest;
* probes every GET route under ``/api`` with the same injected SPA session token
  the browser uses, or records an explicit skip reason;
* verifies mutating routes are registered and their handlers are importable
  without executing those handlers;
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
import sys
import time
from pathlib import Path
from typing import Any, Coroutine, Iterable, Mapping, Sequence, cast
from urllib.parse import quote

from pydantic_core import PydanticUndefined

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from hermes_constants import get_hermes_home

SESSION_TOKEN_RE = re.compile(
    r"window\.__HERMES_SESSION_TOKEN__\s*=\s*['\"]([^'\"]+)['\"]"
)
SESSION_HEADER_NAME = "X-Hermes-Session-Token"
DEFAULT_STATUS_PATH = "state/dashboard-smoke-status.json"
DEFAULT_GET_PREFIXES = ("/api",)
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DECLARED_4XX_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 422})
DECLARED_PROXY_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 422, 502, 503})

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

# GET routes that are registration-covered but not body-probed by default.  The
# harness still reports them per-route with an explicit reason.
SKIPPED_GET_ROUTES: Mapping[str, str] = {
    "/api/gitnexus/{path:path}": "proxy mutating/GET route: registration + handler import checked without executing side effects or sidecar/network calls",
    "/api/hermes/update/check": "network update-check route; skipped to preserve no-metered/no-network smoke",
    "/api/model/options": "provider catalog route; skipped to preserve no-metered/no-network smoke",
    "/api/model/recommended-default": "provider recommendation route; skipped to preserve no-metered/no-network smoke",
}

# Some dynamic path params can use stable harmless values.  They may still return
# a declared 4xx when the object does not exist; that is success for the smoke
# because it proves the route is registered, authenticated, and validation-safe.
_STATIC_PATH_PARAM_SAMPLES: Mapping[str, str] = {
    "attachment_id": "dashboard-smoke-missing",
    "cluster_id": "dashboard-smoke-missing",
    "filename": "dashboard-smoke.css",
    "full_path": "dashboard-smoke-missing",
    "hive_id": "dashboard-smoke-missing",
    "index": "0",
    "job_id": "dashboard-smoke-missing",
    "name": "dashboard-smoke-missing",
    "node_id": "dashboard-smoke-missing",
    "pairing_id": "dashboard-smoke-missing",
    "path": "dashboard-smoke-missing",
    "platform": "discord",
    "profile_name": "dashboard-smoke-missing",
    "provider": "openai",
    "provider_id": "openai",
    "run_id": "dashboard-smoke-missing",
    "session_id": "dashboard-smoke-missing",
    "sid": "dashboard-smoke-missing",
    "slug": "dashboard-smoke-missing",
    "task_id": "dashboard-smoke-missing",
}

_PATH_PARAM_RE = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


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
    expected_statuses: list[int] = field(default_factory=list)
    expected_status_reason: str | None = None


@dataclass
class RouteAuditResult:
    path: str
    name: str
    methods: list[str] = field(default_factory=list)
    route_type: str = ""
    registered: bool = True
    is_api_route: bool = False
    is_get_route: bool = False
    is_mutating_route: bool = False
    handler_module: str | None = None
    handler_qualname: str | None = None
    handler_importable: bool | None = None
    handler_import_error: str | None = None
    probed: bool = False
    skipped: bool = False
    skip_reason: str | None = None
    probe: ProbeResult | None = None


@dataclass
class DashboardSmokeReport:
    generated_at: str
    ok: bool
    route_count_total: int
    api_route_count_total: int
    get_route_count_total: int
    api_get_route_count_total: int
    mutating_route_count_total: int
    probed_route_count: int
    skipped_route_count: int
    expected_dashboard_router_count: int
    mounted_dashboard_router_count: int
    missing_dashboard_routes: list[str] = field(default_factory=list)
    unexpected_dashboard_router_routes: list[str] = field(default_factory=list)
    expected_import_errors: dict[str, str] = field(default_factory=dict)
    missing_app_routes: list[str] = field(default_factory=list)
    unexpected_app_routes: list[str] = field(default_factory=list)
    route_manifest_expected_count: int | None = None
    route_manifest_actual_count: int | None = None
    route_results: list[RouteAuditResult] = field(default_factory=list)
    probes: list[ProbeResult] = field(default_factory=list)
    status_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["route_results"] = [
            {**asdict(route), "probe": asdict(route.probe) if route.probe else None}
            for route in self.route_results
        ]
        data["probes"] = [asdict(p) for p in self.probes]
        return data


def _route_methods(route: Any) -> set[str]:
    methods = getattr(route, "methods", None)
    return set(methods or [])


def _sorted_methods(route: Any) -> list[str]:
    return sorted(_route_methods(route))


def _route_key(path: str, methods: Sequence[str]) -> str:
    return f"{','.join(sorted(methods))} {path}"


def route_manifest(app: FastAPI) -> list[dict[str, Any]]:
    """Return a stable, JSON-serialisable manifest of all registered routes."""
    rows: list[dict[str, Any]] = []
    for route in app.routes:
        methods = _sorted_methods(route)
        rows.append(
            {
                "path": str(getattr(route, "path", "")),
                "methods": methods,
                "name": str(getattr(route, "name", "")),
                "route_type": type(route).__name__,
            }
        )
    return sorted(rows, key=lambda row: (row["path"], row["methods"], row["name"], row["route_type"]))


def load_route_manifest(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        routes = data.get("routes", [])
    else:
        routes = data
    if not isinstance(routes, list):
        raise ValueError(f"route manifest {path} does not contain a routes list")
    return [cast(dict[str, Any], row) for row in routes]


def _user_dashboard_plugin_names() -> set[str]:
    names: set[str] = set()
    root = get_hermes_home() / "plugins"
    if not root.is_dir():
        return names
    for manifest_file in sorted(root.glob("*/dashboard/manifest.json")):
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a bad user manifest should not crash CI manifest comparison
            names.add(manifest_file.parents[1].name)
            continue
        raw_name = data.get("name") if isinstance(data, dict) else None
        names.add(str(raw_name or manifest_file.parents[1].name))
    return names


def _is_user_dashboard_plugin_api_path(path: str, user_plugin_names: set[str]) -> bool:
    prefix = "/api/plugins/"
    if not path.startswith(prefix):
        return False
    rest = path[len(prefix) :]
    plugin_name = rest.split("/", 1)[0]
    return plugin_name in user_plugin_names


def compare_route_manifest(
    app: FastAPI,
    expected_routes: Sequence[Mapping[str, Any]],
    *,
    ignore_dynamic_user_plugins: bool = True,
) -> dict[str, Any]:
    """Compare the current app route table with a checked-in route manifest.

    User-installed dashboard plugins mount into ``app.routes`` at runtime and are
    supposed to vary across machines.  The CI drift gate pins repo-controlled
    routes and bundled plugin routes; local user plugin routes are still
    enumerated in the smoke report but ignored for checked-in manifest drift.
    """
    actual = route_manifest(app)
    user_plugin_names = _user_dashboard_plugin_names() if ignore_dynamic_user_plugins else set()

    def include(row: Mapping[str, Any]) -> bool:
        return not _is_user_dashboard_plugin_api_path(str(row.get("path", "")), user_plugin_names)

    expected_keys = {
        _route_key(str(row.get("path", "")), [str(method) for method in row.get("methods", [])])
        for row in expected_routes
        if include(row)
    }
    actual_keys = {
        _route_key(str(row["path"]), [str(method) for method in row["methods"]])
        for row in actual
        if include(row)
    }
    missing = sorted(expected_keys - actual_keys)
    unexpected = sorted(actual_keys - expected_keys)
    return {
        "ok": not missing and not unexpected,
        "expected_count": len(expected_keys),
        "actual_count": len(actual_keys),
        "missing": missing,
        "unexpected": unexpected,
    }


def iter_get_routes(app: FastAPI, prefixes: Sequence[str] = DEFAULT_GET_PREFIXES) -> list[APIRoute]:
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


def _all_api_routes(app: FastAPI) -> list[APIRoute]:
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and str(getattr(route, "path", "")).startswith("/api")
    ]


def all_get_route_count(app: FastAPI) -> int:
    return sum(
        1
        for route in app.routes
        if isinstance(route, APIRoute) and "GET" in _route_methods(route)
    )


def api_get_route_count(app: FastAPI) -> int:
    return sum(1 for route in _all_api_routes(app) if "GET" in _route_methods(route))


def mutating_route_count(app: FastAPI) -> int:
    return sum(
        1
        for route in app.routes
        if isinstance(route, APIRoute) and _route_methods(route) & MUTATING_METHODS
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


def _path_param_names(path_template: str) -> list[str]:
    return _PATH_PARAM_RE.findall(path_template)


def _quote_path_param(value: str) -> str:
    return quote(value, safe="/")


def materialize_path(path_template: str, seeds: Mapping[str, str]) -> tuple[str | None, str | None]:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = seeds.get(key)
        if value is None:
            missing.append(key)
            return match.group(0)
        return _quote_path_param(value)

    path = _PATH_PARAM_RE.sub(replace, path_template)
    if missing:
        return None, f"missing seed for path parameter(s): {', '.join(sorted(set(missing)))}"
    return path, None


def _required_param_names(route: APIRoute, attr: str) -> list[str]:
    dependant = getattr(route, "dependant", None)
    params = getattr(dependant, attr, []) if dependant is not None else []
    required: list[str] = []
    for param in params:
        name = getattr(param, "name", "")
        # FastAPI/Pydantic's ModelField.required can be None under the versions
        # used here. PydanticUndefined defaults are the stable signal that the
        # request must provide the value, and the route will return 422 if the
        # smoke omits it.
        default = getattr(param, "default", None)
        required_flag = bool(getattr(param, "required", False)) or default is PydanticUndefined
        if name and required_flag:
            required.append(str(name))
    return sorted(required)


def _declared_expected_for_route(route: APIRoute) -> tuple[set[int], str | None]:
    if route.path == "/api/gitnexus/{path:path}":
        return set(DECLARED_PROXY_STATUSES), "declared proxy/sidecar 4xx/5xx response accepted"
    path_params = _path_param_names(route.path)
    required_query = _required_param_names(route, "query_params")
    if path_params or required_query:
        reason_parts: list[str] = []
        if path_params:
            reason_parts.append(f"path seed(s): {', '.join(path_params)}")
        if required_query:
            reason_parts.append(f"required query param(s): {', '.join(required_query)}")
        return set(DECLARED_4XX_STATUSES) | {200}, "declared validation/id-missing response accepted for " + "; ".join(reason_parts)
    if route.path in {"/api/skills/hub/preview", "/api/skills/hub/scan"}:
        return set(DECLARED_4XX_STATUSES) | {200}, "declared missing identifier response accepted"
    if route.path.startswith("/api/auth/"):
        return set(DECLARED_4XX_STATUSES) | {200, 503}, "declared auth setup/session response accepted"
    return {200}, None


def _run_endpoint_direct(route: APIRoute) -> Any:
    value = route.endpoint()
    if inspect.isawaitable(value):
        return asyncio.run(cast(Coroutine[Any, Any, Any], value))
    return value


def _direct_endpoint_has_required_args(route: APIRoute) -> bool:
    try:
        signature = inspect.signature(route.endpoint)
    except (TypeError, ValueError):
        return True
    for param in signature.parameters.values():
        if param.default is inspect.Parameter.empty and param.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            return True
    return False


def _probe_stream_route(route: APIRoute, path: str) -> ProbeResult:
    if _direct_endpoint_has_required_args(route):
        return ProbeResult(
            path_template=route.path,
            path=path,
            name=route.name,
            response_model=bool(route.response_model),
            skipped=True,
            skip_reason="streaming route has required endpoint arguments; registration-only to avoid hang",
        )
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
    if route.path.endswith("/stream"):
        return _probe_stream_route(route, path)

    expected_statuses, expected_status_reason = _declared_expected_for_route(route)
    result = ProbeResult(
        path_template=route.path,
        path=path,
        name=route.name,
        response_model=bool(route.response_model),
        expected_statuses=sorted(expected_statuses),
        expected_status_reason=expected_status_reason,
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
        if response.status_code not in expected_statuses:
            result.error = (
                f"HTTP {response.status_code}: {response.text[:200]} "
                f"(expected one of {sorted(expected_statuses)})"
            )
            return result
        if response.status_code != 200:
            result.ok = True
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


def _resolve_handler(endpoint: Any) -> tuple[bool, str | None]:
    module_name = getattr(endpoint, "__module__", None)
    qualname = getattr(endpoint, "__qualname__", None)
    if not module_name or not qualname:
        return False, "endpoint lacks __module__ or __qualname__"
    try:
        module = sys.modules.get(module_name) or importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001
        return False, f"import {module_name}: {type(exc).__name__}: {exc}"
    if "<locals>" in qualname:
        # Local closures (for example SPA/static helpers) are already live on
        # the route object but cannot be re-resolved from the module namespace.
        return True, None
    obj: Any = module
    try:
        for part in qualname.split("."):
            obj = getattr(obj, part)
    except Exception as exc:  # noqa: BLE001
        return False, f"resolve {module_name}.{qualname}: {type(exc).__name__}: {exc}"
    if obj is not endpoint and not callable(obj):
        return False, f"resolved {module_name}.{qualname} is not callable"
    return True, None


def build_route_audit(app: FastAPI) -> list[RouteAuditResult]:
    results: list[RouteAuditResult] = []
    for route in app.routes:
        path = str(getattr(route, "path", ""))
        methods = _sorted_methods(route)
        endpoint = getattr(route, "endpoint", None)
        handler_importable: bool | None = None
        handler_import_error: str | None = None
        handler_module = getattr(endpoint, "__module__", None) if endpoint else None
        handler_qualname = getattr(endpoint, "__qualname__", None) if endpoint else None
        if endpoint is not None:
            handler_importable, handler_import_error = _resolve_handler(endpoint)
        route_methods = set(methods)
        results.append(
            RouteAuditResult(
                path=path,
                name=str(getattr(route, "name", "")),
                methods=methods,
                route_type=type(route).__name__,
                registered=True,
                is_api_route=path.startswith("/api"),
                is_get_route="GET" in route_methods,
                is_mutating_route=bool(route_methods & MUTATING_METHODS),
                handler_module=handler_module,
                handler_qualname=handler_qualname,
                handler_importable=handler_importable,
                handler_import_error=handler_import_error,
            )
        )
    return results


def _route_should_be_probed(route: APIRoute, prefixes: Sequence[str]) -> tuple[bool, str | None]:
    path = str(route.path)
    if "GET" not in _route_methods(route):
        return False, "not a GET route"
    if prefixes and not any(path.startswith(prefix) for prefix in prefixes):
        return False, f"GET route outside smoke prefixes: {', '.join(prefixes)}"
    skip_reason = SKIPPED_GET_ROUTES.get(path)
    if skip_reason:
        return False, skip_reason
    return True, None


def run_dashboard_smoke(
    app: FastAPI,
    *,
    prefixes: Sequence[str] = DEFAULT_GET_PREFIXES,
    status_file: str | Path | None = None,
    expected_manifest: Sequence[Mapping[str, Any]] | None = None,
) -> DashboardSmokeReport:
    routes = iter_get_routes(app, prefixes=prefixes)
    mount = compare_dashboard_router_mounts(app)
    manifest_comparison = (
        compare_route_manifest(app, expected_manifest) if expected_manifest is not None else None
    )
    route_results = build_route_audit(app)
    by_route = {
        (result.path, tuple(result.methods), result.name, result.route_type): result
        for result in route_results
    }
    probes: list[ProbeResult] = []

    with TestClient(app) as client:
        token = get_spa_session_token(client)
        headers = {SESSION_HEADER_NAME: token}
        seeds = collect_route_seeds(client, headers)
        for route in routes:
            key = (route.path, tuple(_sorted_methods(route)), route.name, type(route).__name__)
            audit = by_route.get(key)
            should_probe, skip_reason = _route_should_be_probed(route, prefixes)
            if not should_probe:
                if audit is not None:
                    audit.skipped = True
                    audit.skip_reason = skip_reason
                continue
            path, materialize_error = materialize_path(route.path, seeds)
            if materialize_error:
                probe = ProbeResult(
                    path_template=route.path,
                    path=None,
                    name=route.name,
                    response_model=bool(route.response_model),
                    skipped=True,
                    skip_reason=materialize_error,
                )
            else:
                assert path is not None
                probe = probe_get_route(client, route, path=path, headers=headers)
            probes.append(probe)
            if audit is not None:
                audit.probe = probe
                audit.probed = not probe.skipped
                audit.skipped = probe.skipped
                audit.skip_reason = probe.skip_reason

    # Mark non-probed app routes with explicit registration-only reasons so the
    # JSON status has a row for every route in app.routes, not just the routes
    # we sent HTTP traffic to.
    for audit in route_results:
        if audit.probed or audit.skipped:
            continue
        if audit.is_mutating_route:
            audit.skipped = True
            audit.skip_reason = "mutating route: registration + handler import checked without executing side effects"
        elif audit.is_get_route and not audit.is_api_route:
            audit.skipped = True
            audit.skip_reason = "GET route outside /api smoke scope"
        elif audit.route_type != "APIRoute":
            audit.skipped = True
            audit.skip_reason = f"non-APIRoute {audit.route_type}: registration-only"
        elif not audit.is_get_route:
            audit.skipped = True
            audit.skip_reason = "non-GET route: registration-only"

    mutating_import_failures = [
        route for route in route_results if route.is_mutating_route and route.handler_importable is False
    ]
    manifest_ok = True if manifest_comparison is None else bool(manifest_comparison["ok"])
    ok = (
        bool(mount["ok"])
        and manifest_ok
        and all(p.ok or p.skipped for p in probes)
        and not mutating_import_failures
    )
    report = DashboardSmokeReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        ok=ok,
        route_count_total=len(app.routes),
        api_route_count_total=len(_all_api_routes(app)),
        get_route_count_total=all_get_route_count(app),
        api_get_route_count_total=api_get_route_count(app),
        mutating_route_count_total=mutating_route_count(app),
        probed_route_count=sum(1 for p in probes if not p.skipped),
        skipped_route_count=sum(1 for route in route_results if route.skipped),
        expected_dashboard_router_count=int(mount["expected_count"]),
        mounted_dashboard_router_count=int(mount["mounted_count"]),
        missing_dashboard_routes=list(mount["missing"]),
        unexpected_dashboard_router_routes=list(mount["unexpected"]),
        expected_import_errors=dict(mount["import_errors"]),
        missing_app_routes=list(manifest_comparison["missing"]) if manifest_comparison else [],
        unexpected_app_routes=list(manifest_comparison["unexpected"]) if manifest_comparison else [],
        route_manifest_expected_count=int(manifest_comparison["expected_count"]) if manifest_comparison else None,
        route_manifest_actual_count=int(manifest_comparison["actual_count"]) if manifest_comparison else None,
        route_results=route_results,
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
        help="Route prefix to probe. Repeatable. Defaults to /api.",
    )
    parser.add_argument(
        "--expected-manifest",
        help="Optional JSON route manifest to fail on missing/unexpected app route drift.",
    )
    args = parser.parse_args(argv)

    from hermes_cli.web_server import app

    expected_manifest = load_route_manifest(args.expected_manifest) if args.expected_manifest else None
    report = run_dashboard_smoke(
        app,
        prefixes=tuple(args.prefixes or DEFAULT_GET_PREFIXES),
        status_file=args.status_file,
        expected_manifest=expected_manifest,
    )
    _print_summary(report)
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
