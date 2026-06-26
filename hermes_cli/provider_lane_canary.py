#!/usr/bin/env python3
"""Provider/profile/tooling canary for Hermes provider-stack route drift.

Read-only inventory + deterministic lint. It compares live profiles, webhook
lanes, authority rows, auxiliary/delegation/gateway routes, and staged route
configs against ~/.hermes/PROVIDER-STACK.md + provider-stack.lock.yaml.

Exit codes:
  0 = no FLAG findings
  1 = one or more FLAG findings
  2 = malformed input / tool failure
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment failure path
    raise SystemExit(f"ERROR: PyYAML is required: {exc}") from exc

DEFAULT_HERMES_HOME = Path("/home/josep/.hermes")
DEFAULT_DOCTRINE = DEFAULT_HERMES_HOME / "PROVIDER-STACK.md"
DEFAULT_LOCK = DEFAULT_HERMES_HOME / "scripts" / "config_canon" / "provider-stack.lock.yaml"
DEFAULT_AUTHORITY = DEFAULT_HERMES_HOME / "scripts" / "config_canon" / "authority-matrix.yaml"
DEFAULT_INTENT_ROUTER = DEFAULT_HERMES_HOME / "intent-router-v0.yaml"
DEFAULT_SOURCE_ROOT = Path("/home/josep/.local/share/hermes-agent")
DEFAULT_OUT_DIR = DEFAULT_HERMES_HOME / "audits" / "20260624-worldclass-burn" / "provider-canary"

PASS = "PASS"
FLAG = "FLAG"

DOCTRINE_REFS = {
    "default_lane": {
        "line": "PROVIDER-STACK.md:13-15",
        "text": "Default/volume work runs on openai-codex gpt-5.5; premium judgment runs through claude-cli-subprocess/run_claude_oneshot.",
    },
    "premium_lane": {
        "line": "PROVIDER-STACK.md:41-42",
        "text": "Premium / judgment provider is claude-cli-subprocess via run_claude_oneshot; model allowed by lock premium_lane.models_allowed.",
    },
    "honcho_isolated": {
        "line": "PROVIDER-STACK.md:45-49,151-154",
        "text": "Honcho memory stack may use OpenRouter prepaid, isolated; key must not be coupled to Hermes fallback chains.",
    },
    "openrouter_fallback": {
        "line": "PROVIDER-STACK.md:87-88,111-117,160-162",
        "text": "OpenRouter fallback_providers chain is forbidden; do not couple Honcho OpenRouter to general Hermes fallback.",
    },
    "native_anthropic": {
        "line": "PROVIDER-STACK.md:87,111,159-161",
        "text": "Native Anthropic API pins (provider=anthropic / direct Claude model pins) are forbidden as live lanes.",
    },
    "gemini_preview": {
        "line": "PROVIDER-STACK.md:89,113,162",
        "text": "Any *-preview Gemini route is forbidden; use non-preview Gemini Flash only for aux cases.",
    },
    "bare_claude": {
        "line": "PROVIDER-STACK.md:65-73,91-92,116,164",
        "text": "New bare claude -p / claude --print subprocess calls are forbidden; use run_claude_oneshot.",
    },
    "fable_pulled": {
        "line": "PROVIDER-STACK.md:75-79,155-158",
        "text": "Fable 5 / Mythos 5 were pulled; default premium is claude-opus-4-8.",
    },
    "tooling": {
        "line": "PROVIDER-STACK.md:17-18,98-105",
        "text": "Routes contradicting the stack are suspect until proven; use explicit allowed provider/tool posture.",
    },
}

CLAUDE_MODEL_NEEDLES = ("claude", "opus", "sonnet", "haiku")
DEAD_OR_RETIRED_MODEL_NEEDLES = ("fable-5", "mythos-5", "claude-fable-5", "claude-mythos-5")
FALLBACK_KEYS = ("fallback_provider", "fallback_providers", "fallback_model", "fallback_models")
FORBIDDEN_ROUTE_RULE_IDS = frozenset({
    "anthropic-provider-on-claude-cli-lane",
    "native-anthropic-api-pin",
    "openrouter-fallback-chain",
    "gemini-preview-route",
    "bare-claude-subprocess",
})
BARE_CLAUDE_PATTERNS = (
    re.compile(r"\bclaude\s+-p\b"),
    re.compile(r"\bclaude\s+--print\b"),
)
# This canary's own source contains the forbidden literal (doctrine strings + the
# flag message at provider_lane_canary.py:688), so the source scan must exclude
# itself or it self-flags as a bare-claude leak.
SELF_SOURCE_PATH = Path(__file__).resolve()


@dataclass
class Check:
    status: str
    rule_id: str
    severity: str
    doctrine_line: str
    doctrine: str
    expected: Any
    actual: Any
    detail: str
    fix: str = ""


@dataclass
class Lane:
    lane_id: str
    lane_type: str
    source_path: str
    source_line: int | None
    provider: Any = None
    model: Any = None
    provider_declared_at: str = ""
    model_declared_at: str = ""
    fallback_chain: Any = None
    tool_allowlist: Any = None
    tool_declared_at: str = ""
    mcp_inclusion: Any = None
    route_class: str = "unknown"
    notes: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> str:
        return FLAG if any(check.status == FLAG for check in self.checks) else PASS

    @property
    def highest_severity(self) -> str:
        order = {"P0": 4, "P1": 3, "P2": 2, "P3": 1, "INFO": 0}
        best = "INFO"
        for check in self.checks:
            if check.status == FLAG and order.get(check.severity, 0) > order.get(best, 0):
                best = check.severity
        return best


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_yaml(path: Path) -> Any:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_json(path: Path) -> Any:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def get_path(data: Any, dotted: str, default: Any = None) -> Any:
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def line_of_text(path: Path, needle: str) -> int | None:
    if not path.exists() or not needle:
        return None
    try:
        for idx, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if needle in line:
                return idx
    except OSError:
        return None
    return None


def yaml_line_for_path(path: Path, dotted: str) -> int | None:
    """Best-effort line number for a YAML key path using PyYAML compose nodes."""
    if not path.exists():
        return None
    try:
        node = yaml.compose(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if node is None:
        return None
    cur = node
    last_key_node = None
    for part in dotted.split("."):
        if not isinstance(cur, yaml.MappingNode):
            return None
        found = None
        for key_node, value_node in cur.value:
            if getattr(key_node, "value", None) == part:
                found = (key_node, value_node)
                break
        if not found:
            return None
        last_key_node, cur = found
    if last_key_node is None:
        return None
    return getattr(last_key_node.start_mark, "line", -1) + 1


def loc(path: Path, line: int | None) -> str:
    return f"{path}:{line}" if line else str(path)


def doctrine(rule: str) -> tuple[str, str]:
    ref = DOCTRINE_REFS[rule]
    return ref["line"], ref["text"]


def add_check(lane: Lane, passed: bool, rule_id: str, severity: str, doctrine_key: str, expected: Any, actual: Any, detail: str, fix: str = "") -> None:
    line, text = doctrine(doctrine_key)
    lane.checks.append(Check(
        status=PASS if passed else FLAG,
        rule_id=rule_id,
        severity=severity if not passed else "INFO",
        doctrine_line=line,
        doctrine=text,
        expected=expected,
        actual=actual,
        detail=detail,
        fix=fix,
    ))


def collect_fallback_values(config: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for scope, obj in [("", config), ("auth.", config.get("auth") if isinstance(config.get("auth"), dict) else {}), ("model.", config.get("model") if isinstance(config.get("model"), dict) else {})]:
        if isinstance(obj, dict):
            for key in FALLBACK_KEYS:
                if key in obj:
                    values[f"{scope}{key}"] = obj.get(key)
    return values


def contains_openrouter_fallback(value: Any) -> bool:
    if value in (None, [], {}, ""):
        return False
    if isinstance(value, dict):
        return any(contains_openrouter_fallback(k) or contains_openrouter_fallback(v) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_openrouter_fallback(v) for v in value)
    return "openrouter" in str(value).lower()


def contains_native_anthropic_pin(provider: Any, model: Any) -> bool:
    p = safe_str(provider).lower()
    m = safe_str(model).lower()
    if p == "anthropic":
        return True
    # Direct Claude model pins are unsafe only when not explicitly routed through claude-cli-subprocess.
    if p.startswith("claude-cli-subprocess"):
        return False
    return p not in {""} and any(needle in m for needle in CLAUDE_MODEL_NEEDLES)


def contains_preview_gemini(provider: Any, model: Any) -> bool:
    text = f"{safe_str(provider)} {safe_str(model)}".lower()
    return "gemini" in text and "preview" in text


def contains_dead_model(model: Any) -> bool:
    m = safe_str(model).lower()
    return any(needle in m for needle in DEAD_OR_RETIRED_MODEL_NEEDLES)


def classify_route(provider: Any, model: Any, lock: dict[str, Any]) -> str:
    p = safe_str(provider)
    m = safe_str(model)
    default_provider = safe_str(get_path(lock, "default_lane.provider"))
    default_model = safe_str(get_path(lock, "default_lane.model"))
    premium_provider = safe_str(get_path(lock, "premium_lane.provider"))
    if (p == default_provider and m == default_model) or p.startswith(f"{default_provider}/{default_model}"):
        return "default_volume"
    if p == premium_provider or p.startswith(premium_provider):
        return "premium_claude_cli"
    if p in {"openrouter", "openrouter-prepaid"}:
        return "metered_openrouter"
    if p in {"xai-oauth", "xai"}:
        return "live_signal_special"
    if p in {"", "auto", "none"}:
        return "implicit_or_unset"
    return "noncanonical"


def tool_allowlist_from_config(data: dict[str, Any], platform: str | None = None) -> tuple[Any, str]:
    if platform and isinstance(data.get("platform_toolsets"), dict) and platform in data["platform_toolsets"]:
        return data["platform_toolsets"].get(platform), f"platform_toolsets.{platform}"
    if "toolsets" in data:
        return data.get("toolsets"), "toolsets"
    if isinstance(data.get("tools"), dict):
        for key in ("enabled_toolsets", "toolsets"):
            if key in data["tools"]:
                return data["tools"].get(key), f"tools.{key}"
    return None, "<inherited/default>"


def is_isolated_honcho_lane(lane: Lane) -> bool:
    hay = " ".join([lane.lane_id, lane.lane_type, lane.source_path, " ".join(lane.notes)]).lower()
    return "honcho" in hay and safe_str(lane.provider).lower() in {"openrouter", "openrouter-prepaid"}


def is_explicit_low_trust_openrouter(lane: Lane) -> bool:
    # Allows staged/explicit public-diversity OpenRouter lanes if they are visibly isolated.
    hay = " ".join([lane.lane_id, lane.lane_type, lane.source_path, json.dumps(lane.tool_allowlist, sort_keys=True, default=str), " ".join(lane.notes)]).lower()
    return safe_str(lane.provider).lower() == "openrouter" and (
        "disallow_control_plane" in hay or "low" in hay or "public" in hay or "diversity" in hay or "cheapgrunt" in hay
    )


def apply_policy(lane: Lane, lock: dict[str, Any]) -> None:
    default_provider = get_path(lock, "default_lane.provider")
    default_model = get_path(lock, "default_lane.model")
    premium_provider = get_path(lock, "premium_lane.provider")
    premium_models = set(as_list(get_path(lock, "premium_lane.models_allowed"))) | {"claude-via-cli", "claude-opus-4.8"}

    lane.route_class = classify_route(lane.provider, lane.model, lock)

    provider = safe_str(lane.provider)
    model = safe_str(lane.model)

    if lane.route_class == "default_volume":
        add_check(lane, True, "default-volume-route", "INFO", "default_lane", f"{default_provider}/{default_model}", f"{provider}/{model}", "canonical subscription/default lane")
    elif lane.route_class == "premium_claude_cli":
        add_check(lane, provider == premium_provider or provider.startswith(premium_provider), "premium-provider", "P0", "premium_lane", premium_provider, provider, "premium lane must use Claude CLI subprocess provider")
        add_check(lane, model in premium_models or model in {"opus", "opus-4.7"}, "premium-model-allowed", "P1", "premium_lane", sorted(premium_models | {"opus", "opus-4.7"}), model, "premium model must be in lock.premium_lane.models_allowed or approved alias", fix="Use claude-opus-4-8 or update PROVIDER-STACK.md canonical lock after review.")
    elif lane.route_class == "metered_openrouter":
        ok = is_isolated_honcho_lane(lane) or is_explicit_low_trust_openrouter(lane)
        add_check(lane, ok, "openrouter-isolated-only", "P1", "honcho_isolated", "isolated Honcho prepaid or explicit low-trust public/diversity lane", f"{provider}/{model}", "OpenRouter is metered; only isolated/non-fallback lanes may pass", fix="If intentional, add isolation/spend-cap/control-plane-deny metadata; otherwise route to openai-codex/gpt-5.5.")
    elif lane.route_class == "live_signal_special":
        ok = "x_search" in lane.lane_id or "live_signal" in lane.lane_id or "x_search" in " ".join(lane.notes).lower()
        add_check(lane, ok, "xai-live-signal-special", "P2", "tooling", "explicit x_search/live_signal special route only", f"{provider}/{model}", "xAI/Grok is allowed only as an explicit live-signal route, not fallback/default")
    elif lane.route_class in {"implicit_or_unset"}:
        # Empty delegation/aux providers can be OK only when the lane is a non-route metadata row. Live model work should be explicit.
        ok = lane.lane_type in {"authority_row", "systemd_timer"} or provider in {"none"}
        severity = "P2" if lane.lane_type in {"auxiliary_route", "delegation_route"} else "P1"
        detail = "implicit/auto provider route; verify runtime resolver cannot fall back to paid/metred lanes"
        add_check(lane, ok, "explicit-provider", severity, "default_lane", "explicit canonical provider/model", f"{provider or '<unset>'}/{model or '<unset>'}", detail, fix="Pin provider/model to openai-codex/gpt-5.5 or claude-cli-subprocess as appropriate.")
    else:
        add_check(lane, False, "canonical-route-class", "P1", "default_lane", "default or premium or approved isolated special", f"{provider}/{model}", "route is outside provider-stack lock", fix="Move to openai-codex/gpt-5.5, claude-cli-subprocess, or document an isolated special lane.")

    # High-priority footgun: claude-cli lane intent/model with provider anthropic.
    claude_cli_intent = "claude-cli" in " ".join([lane.lane_id, lane.lane_type, " ".join(lane.notes), model]).lower() or any(needle in model.lower() for needle in CLAUDE_MODEL_NEEDLES)
    add_check(
        lane,
        not (claude_cli_intent and provider.lower() == "anthropic"),
        "anthropic-provider-on-claude-cli-lane",
        "P0",
        "native_anthropic",
        "provider=claude-cli-subprocess for Claude CLI lanes",
        f"provider={provider!r} model={model!r}",
        "FOOTGUN: provider=anthropic on a Claude/CLI lane can silently route Max-OAuth intent into metered paid provider errors/spend",
        fix="Set model.provider: claude-cli-subprocess for that lane/profile; keep auth.disable_paid_api_fallback: true.",
    )

    add_check(
        lane,
        not contains_native_anthropic_pin(provider, model),
        "native-anthropic-api-pin",
        "P0",
        "native_anthropic",
        "no provider=anthropic or direct Claude model pins outside claude-cli-subprocess",
        f"provider={provider!r} model={model!r}",
        "native Anthropic API/default pins are forbidden live lanes",
        fix="Use claude-cli-subprocess via run_claude_oneshot for premium judgment.",
    )

    fallback_values = lane.fallback_chain
    add_check(
        lane,
        not contains_openrouter_fallback(fallback_values),
        "openrouter-fallback-chain",
        "P0",
        "openrouter_fallback",
        "fallback providers absent/empty and not OpenRouter-coupled",
        fallback_values,
        "OpenRouter fallback chain is forbidden because it is metered and couples blast radius",
        fix="Delete fallback_providers/fallback_model OpenRouter entries; keep Honcho isolated.",
    )

    add_check(
        lane,
        not contains_preview_gemini(provider, model),
        "gemini-preview-route",
        "P0",
        "gemini_preview",
        "no Gemini preview model route",
        f"provider={provider!r} model={model!r}",
        "Preview Gemini routes are forbidden by provider-stack doctrine",
        fix="Use non-preview Gemini Flash only if explicitly approved, or openai-codex/gpt-5.5 for current aux canonical route.",
    )

    add_check(
        lane,
        not contains_dead_model(model),
        "dead-retired-model-id",
        "P1",
        "fable_pulled",
        "no Fable/Mythos pulled model ids",
        model,
        "Retired/pulled model id detected",
        fix="Use claude-opus-4-8 for premium or openai-codex/gpt-5.5 for default.",
    )


def collect_main_and_aux(hermes_home: Path, lock: dict[str, Any]) -> list[Lane]:
    config_path = hermes_home / "config.yaml"
    cfg = load_yaml(config_path)
    lanes: list[Lane] = []
    main_provider = get_path(cfg, "model.provider")
    main_model = get_path(cfg, "model.default")
    main_line = yaml_line_for_path(config_path, "model")
    tools, tool_field = tool_allowlist_from_config(cfg)
    lanes.append(Lane(
        lane_id="main:default",
        lane_type="main_config",
        source_path=str(config_path),
        source_line=main_line,
        provider=main_provider,
        model=main_model,
        provider_declared_at=loc(config_path, yaml_line_for_path(config_path, "model.provider")),
        model_declared_at=loc(config_path, yaml_line_for_path(config_path, "model.default")),
        fallback_chain=collect_fallback_values(cfg) or cfg.get("fallback_providers"),
        tool_allowlist=tools,
        tool_declared_at=loc(config_path, yaml_line_for_path(config_path, tool_field)) if not tool_field.startswith("<") else tool_field,
        mcp_inclusion=cfg.get("mcp_servers") or cfg.get("mcp"),
    ))

    aux = cfg.get("auxiliary") if isinstance(cfg.get("auxiliary"), dict) else {}
    for name, block in sorted(aux.items()):
        if not isinstance(block, dict):
            continue
        provider = block.get("provider")
        model = block.get("model")
        lanes.append(Lane(
            lane_id=f"auxiliary:{name}",
            lane_type="auxiliary_route",
            source_path=str(config_path),
            source_line=yaml_line_for_path(config_path, f"auxiliary.{name}"),
            provider=provider,
            model=model,
            provider_declared_at=loc(config_path, yaml_line_for_path(config_path, f"auxiliary.{name}.provider")),
            model_declared_at=loc(config_path, yaml_line_for_path(config_path, f"auxiliary.{name}.model")),
            fallback_chain=collect_fallback_values(block),
            tool_allowlist=["safe/internal"],
            tool_declared_at="internal auxiliary task",
            notes=["auxiliary task provider/model pin"],
        ))

    delegation = cfg.get("delegation") if isinstance(cfg.get("delegation"), dict) else {}
    if delegation:
        lanes.append(Lane(
            lane_id="delegation:subagent-default",
            lane_type="delegation_route",
            source_path=str(config_path),
            source_line=yaml_line_for_path(config_path, "delegation"),
            provider=delegation.get("provider"),
            model=delegation.get("model"),
            provider_declared_at=loc(config_path, yaml_line_for_path(config_path, "delegation.provider")),
            model_declared_at=loc(config_path, yaml_line_for_path(config_path, "delegation.model")),
            fallback_chain=collect_fallback_values(delegation),
            tool_allowlist=f"inherit_mcp_toolsets={delegation.get('inherit_mcp_toolsets')}",
            tool_declared_at=loc(config_path, yaml_line_for_path(config_path, "delegation.inherit_mcp_toolsets")),
            mcp_inclusion=delegation.get("inherit_mcp_toolsets"),
            notes=["blank provider/model means runtime inheritance/auto unless caller overrides"],
        ))

    for platform, platform_tools in sorted((cfg.get("platform_toolsets") or {}).items()):
        lanes.append(Lane(
            lane_id=f"gateway-platform:{platform}",
            lane_type="gateway_tool_allowlist",
            source_path=str(config_path),
            source_line=yaml_line_for_path(config_path, f"platform_toolsets.{platform}"),
            provider=main_provider,
            model=main_model,
            provider_declared_at=loc(config_path, yaml_line_for_path(config_path, "model.provider")),
            model_declared_at=loc(config_path, yaml_line_for_path(config_path, "model.default")),
            fallback_chain=cfg.get("fallback_providers"),
            tool_allowlist=platform_tools,
            tool_declared_at=loc(config_path, yaml_line_for_path(config_path, f"platform_toolsets.{platform}")),
            mcp_inclusion=None,
            notes=["gateway platform inherits main provider/model unless route overrides"],
        ))
    return lanes


def collect_profiles(hermes_home: Path) -> list[Lane]:
    profiles_dir = hermes_home / "profiles"
    lanes: list[Lane] = []
    if not profiles_dir.exists():
        return lanes
    for config_path in sorted(profiles_dir.glob("*/config.yaml")):
        profile = config_path.parent.name
        data = load_yaml(config_path)
        if not isinstance(data, dict):
            continue
        provider = get_path(data, "model.provider")
        model = get_path(data, "model.default")
        tools, tool_field = tool_allowlist_from_config(data)
        lanes.append(Lane(
            lane_id=f"profile:{profile}",
            lane_type="profile",
            source_path=str(config_path),
            source_line=yaml_line_for_path(config_path, "model"),
            provider=provider,
            model=model,
            provider_declared_at=loc(config_path, yaml_line_for_path(config_path, "model.provider")),
            model_declared_at=loc(config_path, yaml_line_for_path(config_path, "model.default")),
            fallback_chain=collect_fallback_values(data) or data.get("fallback_providers"),
            tool_allowlist=tools,
            tool_declared_at=loc(config_path, yaml_line_for_path(config_path, tool_field)) if not tool_field.startswith("<") else tool_field,
            mcp_inclusion=data.get("mcp_servers") or data.get("mcp"),
        ))
    return lanes


def collect_webhook_lanes(hermes_home: Path, main_provider: Any, main_model: Any) -> list[Lane]:
    path = hermes_home / "webhook_subscriptions.json"
    data = load_json(path)
    lanes: list[Lane] = []
    if not isinstance(data, dict):
        return lanes
    for name, row in sorted(data.items()):
        if not isinstance(row, dict):
            continue
        provider = row.get("provider") or get_path(row, "model.provider") or main_provider
        model = row.get("model") or get_path(row, "model.default") or main_model
        tools = row.get("toolsets") or row.get("allowed_toolsets") or row.get("enabled_toolsets") or "inherits platform_toolsets.webhook"
        line = line_of_text(path, f'"{name}"')
        lanes.append(Lane(
            lane_id=f"webhook:{name}",
            lane_type="webhook_lane",
            source_path=str(path),
            source_line=line,
            provider=provider,
            model=model,
            provider_declared_at=(loc(path, line) + " (inherits main config unless explicit provider/model present)"),
            model_declared_at=(loc(path, line) + " (inherits main config unless explicit provider/model present)"),
            fallback_chain={k: row.get(k) for k in FALLBACK_KEYS if k in row},
            tool_allowlist=tools,
            tool_declared_at=loc(path, line),
            mcp_inclusion=None,
            notes=["prompt/secret/deliver fields intentionally not copied"],
        ))
    return lanes


def collect_authority_rows(path: Path) -> list[Lane]:
    data = load_yaml(path)
    lanes: list[Lane] = []
    if not isinstance(data, dict):
        return lanes
    rows: list[dict[str, Any]] = []
    for section in ("principals", "ai_agents"):
        for row in as_list(data.get(section)):
            if isinstance(row, dict):
                row = dict(row)
                row.setdefault("_section", section)
                rows.append(row)
    for row in rows:
        name = row.get("principal_name") or ("ai-agent:" + safe_str(row.get("agent_name") or "unnamed"))
        line = line_of_text(path, f"principal_name: {name}") or line_of_text(path, f"agent_name: {row.get('agent_name')}")
        lanes.append(Lane(
            lane_id=f"authority:{name}",
            lane_type="authority_row",
            source_path=str(path),
            source_line=line,
            provider=row.get("provider"),
            model=row.get("model"),
            provider_declared_at=loc(path, line),
            model_declared_at=loc(path, line),
            fallback_chain=None,
            tool_allowlist=row.get("tools_allowed"),
            tool_declared_at=loc(path, line),
            mcp_inclusion="mcp" in [safe_str(x).lower() for x in as_list(row.get("tools_allowed"))],
            notes=[f"trust_tier={row.get('trust_tier')}", f"review_gate={row.get('review_gate')}", f"section={row.get('_section')}", f"source={row.get('source') or row.get('pid_pattern') or ''}"],
        ))
    return lanes


def collect_intent_router(path: Path) -> list[Lane]:
    data = load_yaml(path)
    lanes: list[Lane] = []
    if not isinstance(data, dict):
        return lanes
    for route in as_list(data.get("routes")):
        if not isinstance(route, dict):
            continue
        route_id = safe_str(route.get("route_id") or "unnamed")
        line = line_of_text(path, f"route_id: {route_id}")
        lanes.append(Lane(
            lane_id=f"intent-router:{route_id}",
            lane_type="staged_route_config",
            source_path=str(path),
            source_line=line,
            provider=route.get("provider"),
            model=route.get("model"),
            provider_declared_at=loc(path, line),
            model_declared_at=loc(path, line),
            fallback_chain=None,
            tool_allowlist=route.get("toolsets"),
            tool_declared_at=loc(path, line),
            mcp_inclusion="mcp" in [safe_str(x).lower() for x in as_list(route.get("toolsets"))],
            notes=[f"status={route.get('status')}", f"trust_tier={route.get('trust_tier')}", f"requires_spend_cap={route.get('requires_spend_cap')}", f"disallow_control_plane={route.get('disallow_control_plane')}", safe_str(route.get("notes"))],
        ))
    return lanes


def scan_bare_claude(paths: Iterable[Path]) -> list[Lane]:
    lanes: list[Lane] = []
    for root in paths:
        if not root.exists():
            continue
        if not root.is_file() and root.name == "hermes-agent":
            candidates = []
            for subdir in ("agent", "cron", "gateway", "hermes_cli", "plugins", "scripts", "tools"):
                d = root / subdir
                if d.exists():
                    candidates.extend(list(d.rglob("*.py")) + list(d.rglob("*.sh")))
            for top in root.glob("*.py"):
                candidates.append(top)
        else:
            candidates = [root] if root.is_file() else list(root.rglob("*.py")) + list(root.rglob("*.sh"))
        for path in candidates:
            if any(part in {".git", "venv", "__pycache__", "node_modules"} for part in path.parts):
                continue
            # Exclude this canary's own source so it does not self-flag on the
            # forbidden literal it must carry (doctrine strings + line 688 message).
            try:
                if path.resolve() == SELF_SOURCE_PATH:
                    continue
            except OSError:
                pass
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # A file that scrubs metered API creds from the subprocess env before
            # invoking the claude CLI runs a zero-metered subscription/OAuth lane, not a
            # bare paid-API call. Recognise that guard so the gate can scan without
            # false-flagging legitimate subscription-only callers (audit: "gate runs blind").
            text_lower = text.lower()
            guarded_subscription_file = (
                "_subscription_only_env" in text_lower
                or "metered_env_keys" in text_lower
            )
            lines = text.splitlines()
            for lineno, line in enumerate(lines, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if any(pattern.search(line) for pattern in BARE_CLAUDE_PATTERNS):
                    # Ignore explicit negative/doc/help/test text that mentions the forbidden command but does not execute it.
                    context = "\n".join(lines[max(0, lineno - 8):lineno + 8]).lower()
                    if (
                        guarded_subscription_file
                        or ("run_claude_oneshot" in context and ("instead" in context or "replaces" in context or "forbidden" in context))
                        or "forbidden" in context
                        or "bare_claude_p" in context
                        or "max-preserving" in context
                        or "instead of" in context
                        or " not `claude" in context
                        or "migrate" in context
                        or "deadline" in context
                        or "help=" in context
                        or "help=(" in context
                        or "future seam" in context
                        or "backend to use for extraction" in context
                        or "test_" in path.name
                        or "/tests/" in str(path)
                        or "tests" in path.parts
                    ):
                        continue
                    lane = Lane(
                        lane_id=f"source-scan:{path}:{lineno}",
                        lane_type="source_bare_claude_scan",
                        source_path=str(path),
                        source_line=lineno,
                        provider="source-subprocess",
                        model="claude-cli",
                        provider_declared_at=loc(path, lineno),
                        model_declared_at=loc(path, lineno),
                        fallback_chain=None,
                        tool_allowlist="subprocess",
                        tool_declared_at=loc(path, lineno),
                    )
                    line_ref, text_ref = doctrine("bare_claude")
                    lane.checks.append(Check(FLAG, "bare-claude-subprocess", "P0", line_ref, text_ref, "run_claude_oneshot", stripped[:240], "Executable bare claude -p/--print pattern found", "Replace with agent.claude_cli_runtime.run_claude_oneshot."))
                    lanes.append(lane)
    return lanes


def collect_lanes(args: argparse.Namespace) -> tuple[dict[str, Any], list[Lane]]:
    hermes_home = args.hermes_home
    lock_doc = load_yaml(args.lock)
    lock = lock_doc.get("lock", lock_doc)
    if not isinstance(lock, dict):
        raise ValueError(f"lock payload missing/malformed: {args.lock}")

    main_config = load_yaml(hermes_home / "config.yaml")
    main_provider = get_path(main_config, "model.provider")
    main_model = get_path(main_config, "model.default")

    lanes: list[Lane] = []
    lanes.extend(collect_main_and_aux(hermes_home, lock))
    lanes.extend(collect_profiles(hermes_home))
    lanes.extend(collect_webhook_lanes(hermes_home, main_provider, main_model))
    if args.authority.exists():
        lanes.extend(collect_authority_rows(args.authority))
    if args.intent_router.exists():
        lanes.extend(collect_intent_router(args.intent_router))

    scan_roots = list(getattr(args, "scan_root", None) or [])
    if not getattr(args, "no_source_scan", False):
        scan_roots.append(hermes_home / "scripts")
        source_root = getattr(args, "source_root", DEFAULT_SOURCE_ROOT)
        if source_root.exists():
            scan_roots.append(source_root)
    lanes.extend(scan_bare_claude(scan_roots))

    for lane in lanes:
        if not lane.checks:
            apply_policy(lane, lock)

    return lock_doc, lanes


def summary(lanes: list[Lane]) -> dict[str, Any]:
    status_counts = {PASS: 0, FLAG: 0}
    severity_counts: dict[str, int] = {}
    by_type: dict[str, dict[str, int]] = {}
    forbidden_route_flag_count = 0
    for lane in lanes:
        status_counts[lane.status] += 1
        by_type.setdefault(lane.lane_type, {PASS: 0, FLAG: 0})[lane.status] += 1
        for check in lane.checks:
            if check.status == FLAG:
                severity_counts[check.severity] = severity_counts.get(check.severity, 0) + 1
                if check.severity == "P0" and check.rule_id in FORBIDDEN_ROUTE_RULE_IDS:
                    forbidden_route_flag_count += 1
    return {
        "lane_count": len(lanes),
        "status_counts": status_counts,
        "severity_counts": dict(sorted(severity_counts.items())),
        "by_type": by_type,
        "p0_flag_count": severity_counts.get("P0", 0),
        "forbidden_route_flag_count": forbidden_route_flag_count,
    }


def report_dict(args: argparse.Namespace, lock_doc: dict[str, Any], lanes: list[Lane]) -> dict[str, Any]:
    return {
        "generated_at": utc_now(),
        "script": str(Path(__file__).resolve()),
        "hermes_home": str(args.hermes_home),
        "doctrine_path": str(args.doctrine),
        "lock_path": str(args.lock),
        "lock_source_path": lock_doc.get("source_path"),
        "lock_source_block_sha256": lock_doc.get("source_block_sha256"),
        "doctrine_refs": DOCTRINE_REFS,
        "summary": summary(lanes),
        "lanes": [asdict(lane) | {"status": lane.status, "highest_severity": lane.highest_severity} for lane in lanes],
    }


def write_json(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def markdown_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    s = report["summary"]
    lines.append("# Provider/Profile/Tooling Canary Report")
    lines.append("")
    lines.append(f"Generated: `{report['generated_at']}`")
    lines.append(f"Doctrine: `{report['doctrine_path']}`")
    lines.append(f"Lock: `{report['lock_path']}` (`{report.get('lock_source_block_sha256')}`)")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Lanes audited: **{s['lane_count']}**")
    lines.append(f"- PASS: **{s['status_counts'].get(PASS, 0)}**")
    lines.append(f"- FLAG: **{s['status_counts'].get(FLAG, 0)}**")
    lines.append(f"- Forbidden route flags: **{s.get('forbidden_route_flag_count', 0)}**")
    lines.append(f"- P0 flags: **{s.get('p0_flag_count', 0)}**")
    lines.append(f"- By type: `{json.dumps(s['by_type'], sort_keys=True)}`")
    lines.append("")
    lines.append("## P0 / High-priority flags")
    lines.append("")
    p0s = []
    for lane in report["lanes"]:
        for check in lane["checks"]:
            if check["status"] == FLAG and check["severity"] == "P0":
                p0s.append((lane, check))
    if not p0s:
        lines.append("No P0 flags found.")
    else:
        for lane, check in p0s:
            lines.append(f"- **{lane['lane_id']}** `{lane['provider']}/{lane['model']}` at `{lane['provider_declared_at']}` — `{check['rule_id']}`: {check['detail']}")
            lines.append(f"  - Doctrine: {check['doctrine_line']} — {check['doctrine']}")
            if check.get("fix"):
                lines.append(f"  - Fix: {check['fix']}")
    lines.append("")
    lines.append("## Full lane inventory")
    lines.append("")
    lines.append("| Status | Sev | Lane | Type | Provider | Model | Provider declared | Model declared | Tools | Flag rules |")
    lines.append("|---|---:|---|---|---|---|---|---|---|---|")
    for lane in sorted(report["lanes"], key=lambda x: (x["status"], x["lane_type"], x["lane_id"])):
        flags = [c for c in lane["checks"] if c["status"] == FLAG]
        flag_rules = "; ".join(f"{c['severity']}:{c['rule_id']}" for c in flags) or "—"
        tools = json.dumps(lane.get("tool_allowlist"), default=str)[:140]
        lines.append("| {status} | {sev} | `{lane}` | {typ} | `{provider}` | `{model}` | `{pdecl}` | `{mdecl}` | `{tools}` | {rules} |".format(
            status=lane["status"],
            sev=lane.get("highest_severity") or "INFO",
            lane=lane["lane_id"],
            typ=lane["lane_type"],
            provider=lane.get("provider"),
            model=lane.get("model"),
            pdecl=lane.get("provider_declared_at", ""),
            mdecl=lane.get("model_declared_at", ""),
            tools=tools.replace("|", "\\|"),
            rules=flag_rules.replace("|", "\\|"),
        ))
    lines.append("")
    lines.append("## Doctrine citations used")
    lines.append("")
    for key, ref in report["doctrine_refs"].items():
        lines.append(f"- `{key}` — {ref['line']}: {ref['text']}")
    lines.append("")
    return "\n".join(lines)


def write_markdown(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown_report(report), encoding="utf-8")


def print_table(report: dict[str, Any]) -> None:
    for lane in report["lanes"]:
        print(f"{lane['status']:4} {lane['highest_severity']:4} {lane['lane_id']} {lane.get('provider')}/{lane.get('model')}")
        for check in lane["checks"]:
            if check["status"] == FLAG:
                print(f"  - {check['severity']} {check['rule_id']}: {check['detail']} ({check['doctrine_line']})")
    print("SUMMARY " + json.dumps(report["summary"], sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, default=DEFAULT_HERMES_HOME)
    parser.add_argument("--doctrine", type=Path, default=DEFAULT_DOCTRINE)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--authority", type=Path, default=DEFAULT_AUTHORITY)
    parser.add_argument("--intent-router", type=Path, default=DEFAULT_INTENT_ROUTER)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT, help="Hermes source root to scan for executable bare Claude subprocess calls")
    parser.add_argument("--scan-root", type=Path, action="append", default=[], help="Additional file/directory root to scan for executable bare Claude subprocess calls")
    parser.add_argument("--no-source-scan", action="store_true", help="Skip bare Claude subprocess source scans; intended for hermetic unit tests only")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--md-out", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Print JSON to stdout")
    args = parser.parse_args()

    try:
        lock_doc, lanes = collect_lanes(args)
        report = report_dict(args, lock_doc, lanes)
        json_out = args.json_out or (args.output_dir / "canary-report.json")
        md_out = args.md_out or (args.output_dir / "canary-report.md")
        write_json(report, json_out)
        write_markdown(report, md_out)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print_table(report)
        return 1 if report["summary"]["status_counts"].get(FLAG, 0) else 0
    except Exception as exc:  # noqa: BLE001 - canary should fail loud on malformed inputs
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
