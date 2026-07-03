"""All-territories Nexus truth API.

This module serves the static V6 Nexus topology with normalized truth objects.
It is read-only: donor snapshots are imported, never mutated, and action execution is
outside this API's scope.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from hermes_cli import dashboard_connectome, dashboard_nexus_slice, dashboard_os
from hermes_cli.dashboard_nexus_slice import PROBE_STATES, TRUTH_ID as BACKUP_OFFBOX_EDGE_ID, TRUTH_KEYS

router = APIRouter(prefix="/api/dashboard/nexus", tags=["dashboard-nexus"])

_TTL_S = 20.0
_CACHE: tuple[dict[str, Any], float] | None = None
_LOCK = threading.Lock()
DESIGN_SNAP = "2026-07-03T03:02:10+00:00"
DEFAULT_LOCKED_ACTIONS = ["fix/restart/deploy/config/provider/credential/cron mutations"]

STATE_RANK = {"broken": 0, "stale": 1, "gated": 2, "unknown": 3, "manual": 4, "live": 5}

TERRITORY_SPECS: tuple[dict[str, Any], ...] = (
    {"id": "territory:memory", "label": "MEMORY", "members": ["mvms", "supabase", "kanban-db", "state-db", "honcho-api", "honcho-db", "honcho-deriver", "honcho-redis", "claude-memory", "hermes-memories"]},
    {"id": "territory:surfaces", "label": "SURFACES", "members": ["claude-code", "discord", "dashboard", "codex"]},
    {"id": "territory:control", "label": "CONTROL", "members": ["gateway", "watchdog", "hermes-cron", "timers", "mvms-watcher", "honcho-watcher"]},
    {"id": "territory:providers", "label": "PROVIDERS", "members": ["chatgpt", "claude-max", "openrouter"]},
    {"id": "territory:ingest", "label": "INGEST", "members": ["ict-brain", "opus-extractor", "x_search"]},
    {"id": "territory:protection", "label": "PROTECTION", "members": ["nightly-backup", "backups-dir", "veracrypt", "compactor", "offbox"]},
    {"id": "territory:learning", "label": "LEARNING", "members": ["learning-verify", "distiller", "reflect-gate"]},
    {"id": "territory:git", "label": "GIT", "members": ["hermes-repo", "deploy-div", "relay-wt", "gitnexus", "lane-runs"]},
    {"id": "territory:host", "label": "HOST", "members": ["wsl"]},
)

SYSTEM_LABELS: dict[str, str] = {
    "system:memory/mvms": "MVMS",
    "system:memory/supabase": "supabase-db",
    "system:memory/kanban-db": "kanban-db",
    "system:memory/state-db": "state-db",
    "system:memory/honcho-api": "honcho-api",
    "system:memory/honcho-db": "honcho-db",
    "system:memory/honcho-deriver": "honcho-deriver",
    "system:memory/honcho-redis": "honcho-redis",
    "system:memory/claude-memory": "claude-memory",
    "system:memory/hermes-memories": "hermes-memories",
    "system:surfaces/claude-code": "claude-code",
    "system:surfaces/discord": "discord",
    "system:surfaces/dashboard": "dashboard :9119",
    "system:surfaces/codex": "codex-pipeline",
    "system:control/gateway": "gateway",
    "system:control/watchdog": "watchdog",
    "system:control/hermes-cron": "hermes-cron",
    "system:control/timers": "timers (systemd)",
    "system:control/mvms-watcher": "mvms-watcher",
    "system:control/honcho-watcher": "honcho-watcher",
    "system:providers/chatgpt": "chatgpt-backend",
    "system:providers/claude-max": "claude-max",
    "system:providers/openrouter": "openrouter",
    "system:ingest/ict-brain": "ict-brain",
    "system:ingest/opus-extractor": "opus_extractor",
    "system:ingest/x_search": "x_search",
    "system:protection/nightly-backup": "nightly-backup",
    "system:protection/backups-dir": "backups-dir",
    "system:protection/veracrypt": "veracrypt",
    "system:protection/compactor": "mvms-compactor",
    "system:protection/offbox": "off-box-gap",
    "system:learning/learning-verify": "learning-verify",
    "system:learning/distiller": "distiller",
    "system:learning/reflect-gate": "reflect-gate",
    "system:git/hermes-repo": "hermes-agent repo",
    "system:git/deploy-div": "deploy divergence",
    "system:git/relay-wt": "relay worktree",
    "system:git/gitnexus": "GitNexus index",
    "system:git/lane-runs": "lane runs",
    "system:host/wsl": "wsl-host",
}

SYSTEM_SOURCE_MAP: dict[str, tuple[str, str]] = {
    "system:memory/mvms": ("os", "memory_stores/mvms_observations"),
    "system:memory/supabase": ("os", "containers/supabase_db_goattrade-system"),
    "system:memory/kanban-db": ("os", "memory_stores/kanban_db"),
    "system:memory/state-db": ("os", "memory_stores/state_db"),
    "system:memory/honcho-api": ("os", "containers/honcho-api-1"),
    "system:memory/honcho-db": ("os", "containers/honcho-database-1"),
    "system:memory/honcho-deriver": ("os", "containers/honcho-deriver-1"),
    "system:memory/honcho-redis": ("os", "containers/honcho-redis-1"),
    "system:memory/claude-memory": ("os", "memory_stores/memory_md"),
    "system:memory/hermes-memories": ("os", "memory_stores/memory_md"),
    "system:surfaces/claude-code": ("os", "providers/claude_cli"),
    "system:surfaces/discord": ("os", "gateway/gateway_state"),
    "system:surfaces/dashboard": ("os", "systemd/failed_units"),
    "system:surfaces/codex": ("os", "providers/codex_live_sessions"),
    "system:control/gateway": ("os", "gateway/gateway_state"),
    "system:control/watchdog": ("os", "gateway/watchdog_events"),
    "system:control/hermes-cron": ("os", "cron/cron_last_status"),
    "system:control/timers": ("os", "systemd/timers_keep"),
    "system:control/mvms-watcher": ("os", "infra/config_drift"),
    "system:control/honcho-watcher": ("os", "infra/security"),
    "system:providers/chatgpt": ("os", "providers/codex_pipeline_load"),
    "system:providers/claude-max": ("os", "providers/claude_cli"),
    "system:providers/openrouter": ("os", "providers/openrouter_key"),
    "system:ingest/ict-brain": ("os", "memory_stores/mvms_observations"),
    "system:ingest/opus-extractor": ("os", "providers/claude_cli"),
    "system:ingest/x_search": ("os", "providers/openrouter_key"),
    "system:protection/nightly-backup": ("os", "backups/mvms-canonical-*.sql.gz"),
    "system:protection/backups-dir": ("os", "backups/hermes-app-state-*.tar.gz"),
    "system:protection/veracrypt": ("os", "backups/veracrypt_weekly"),
    "system:protection/compactor": ("os", "systemd/timers_disabled_by_design"),
    "system:protection/offbox": ("os", "backups/mvms-backup-gap-offbox"),
    "system:learning/learning-verify": ("os", "infra/evals"),
    "system:learning/distiller": ("os", "memory_stores/mvms_observations"),
    "system:learning/reflect-gate": ("os", "cron/cron_last_status"),
    "system:git/hermes-repo": ("os", "repo/readiness"),
    "system:git/deploy-div": ("connectome", "deploy"),
    "system:git/relay-wt": ("os", "repo/uncommitted"),
    "system:git/gitnexus": ("connectome", "code"),
    "system:git/lane-runs": ("connectome", "lanes"),
    "system:host/wsl": ("os", "host/wsl_uptime"),
}

NON_DONOR_DEGRADED_REASONS: list[str] = []

HONESTY_CAPS: dict[str, tuple[str, str]] = {
    "system:providers/claude-max": ("cap", "manual"),
    "system:providers/openrouter": ("cap", "manual"),
    "system:ingest/ict-brain": ("cap", "manual"),
    "system:ingest/opus-extractor": ("cap", "manual"),
    "system:ingest/x_search": ("cap", "manual"),
    "system:memory/hermes-memories": ("pin:aliased-file", "unknown"),
    "system:surfaces/dashboard": ("pin:proxy-signal", "unknown"),
    "system:protection/compactor": ("pin:disabled-by-design", "gated"),
}

HONESTY_LINES = {
    "system:providers/claude-max": "231-233",
    "system:providers/openrouter": "234-236",
    "system:ingest/ict-brain": "243",
    "system:ingest/opus-extractor": "244",
    "system:ingest/x_search": "245",
    "system:memory/hermes-memories": "187-190",
    "system:surfaces/dashboard": "199-202",
    "system:protection/compactor": "255",
}

MANUAL_EDGES: tuple[tuple[str, str | None, str | None, str, str], ...] = (
    ("edge:discord->gateway--chat-ingress", "territory:surfaces", "territory:control", "discord → gateway · chat ingress", "331"),
    ("edge:gateway->chatgpt--main-lane-gpt-5-5", "territory:control", "territory:providers", "gateway → chatgpt · main lane gpt-5.5", "331"),
    ("edge:gateway->claude-max--cli-lane", "territory:control", "territory:providers", "gateway → claude-max · cli lane", "332"),
    ("edge:claude-max->opus-extractor--gated-oauth", "territory:providers", "territory:ingest", "claude-max → opus_extractor · gated OAuth", "332"),
    ("edge:honcho-deriver->openrouter", "territory:memory", "territory:providers", "honcho-deriver → openrouter", "333"),
    ("edge:gateway->state-db--turn-log", "territory:control", "territory:memory", "gateway → state-db · turn log", "333"),
    ("edge:gateway->kanban-db--dispatch", "territory:control", "territory:memory", "gateway → kanban-db · dispatch", "334"),
    ("edge:ict-brain->mvms--ingestion", "territory:ingest", "territory:memory", "ict-brain → mvms · ingestion", "334"),
    ("edge:x-search->mvms--ingestion", "territory:ingest", "territory:memory", "x_search → mvms · ingestion", "335"),
    ("edge:mvms-watcher->mvms", "territory:control", "territory:memory", "mvms-watcher → mvms", "335"),
    ("edge:honcho-watcher->honcho-db", "territory:control", "territory:memory", "honcho-watcher → honcho-db", "336"),
    ("edge:learning-verify->mvms", "territory:learning", "territory:memory", "learning-verify → mvms", "336"),
    ("edge:distiller->mvms--gated-promotion", "territory:learning", "territory:memory", "distiller → mvms · gated promotion", "337"),
    ("edge:reflect-gate->mvms--gated", "territory:learning", "territory:memory", "reflect-gate → mvms · gated", "337"),
    ("edge:nightly-backup->mvms--dump", "territory:protection", "territory:memory", "nightly-backup → mvms · dump", "338"),
    ("edge:nightly-backup->honcho-db--dump", "territory:protection", "territory:memory", "nightly-backup → honcho-db · dump", "338"),
    ("edge:nightly-backup->claude-memory--dump", "territory:protection", "territory:memory", "nightly-backup → claude-memory · dump", "339"),
    ("edge:veracrypt->backups-dir", "territory:protection", "territory:protection", "veracrypt → backups-dir", "339"),
    ("edge:claude-code->gateway--cockpit", "territory:surfaces", "territory:control", "claude-code → gateway · cockpit", "340"),
    ("edge:dashboard->gateway--reads-state", "territory:surfaces", "territory:control", "dashboard → gateway · reads state", "340"),
    ("edge:codex-pipeline->chatgpt--sessions", "territory:surfaces", "territory:providers", "codex-pipeline → chatgpt · sessions", "341"),
    ("edge:watchdog->gateway", "territory:control", "territory:control", "watchdog → gateway", "341"),
    ("edge:hermes-cron->timers", "territory:control", "territory:control", "hermes-cron → timers", "342"),
    ("edge:wsl-host->everything--substrate", "territory:host", "territory:control", "wsl-host → everything · substrate", "342"),
    ("edge:gitnexus->gateway--reindex-on-commit", "territory:git", "territory:control", "gitnexus → gateway · reindex on commit", "343"),
    ("edge:gitnexus->mvms--code-brain", "territory:git", "territory:memory", "gitnexus → mvms · code brain", "344"),
    ("edge:deploy-branch->dashboard-bundle--serving", "territory:git", "territory:surfaces", "deploy-branch → dashboard-bundle · serving", "345"),
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _manual_freshness_age_s() -> float:
    snap = datetime.fromisoformat(DESIGN_SNAP)
    return round(max(0.0, (datetime.now(timezone.utc) - snap).total_seconds()), 3)


def _manual_truth(object_id: str, generated_at: str, *, line: str = "158") -> dict[str, Any]:
    return {
        "id": object_id,
        "probe_state": "manual",
        "freshness_age_s": _manual_freshness_age_s(),
        "confidence": "claimed",
        "evidence_refs": [f"design:V6-DESIGN-LOCKED-blended-organism.html:{line}"],
        "last_checked": generated_at,
        "safe_next_action": "write/attach a verifier spec; do not dispatch a fix from this edge",
        "locked_actions": DEFAULT_LOCKED_ACTIONS,
        "what_would_prove_green": "a named verifier function returns live with evidence_refs and freshness_age_s",
        "what_breaks_if_false": "a false manual claim can route operators to nonexistent verified dependencies",
    }


def _manual_system_truth(object_id: str, generated_at: str) -> dict[str, Any]:
    truth = _manual_truth(object_id, generated_at, line="manual-system")
    truth["safe_next_action"] = "attach a named system verifier before treating this as live"
    truth["what_breaks_if_false"] = "a false system state can mislead graph drill-down and action queues"
    return truth


def validate_truth_object(payload: dict[str, Any], expected_id: str) -> dict[str, Any]:
    if set(payload) != set(TRUTH_KEYS):
        raise ValueError(f"truth object keys mismatch: {sorted(payload)}")
    if payload["id"] != expected_id:
        raise ValueError(f"unexpected truth id: {payload['id']}")
    if payload["probe_state"] not in PROBE_STATES:
        raise ValueError(f"invalid probe_state: {payload['probe_state']}")
    if not isinstance(payload["evidence_refs"], list) or not payload["evidence_refs"]:
        raise ValueError("evidence_refs must be non-empty list")
    if not isinstance(payload["freshness_age_s"], int | float) or payload["freshness_age_s"] < 0:
        raise ValueError("freshness_age_s must be numeric >= 0")
    return {key: payload[key] for key in TRUTH_KEYS}


def _unknown_truth(object_id: str, generated_at: str, reason: str) -> dict[str, Any]:
    return {
        "id": object_id,
        "probe_state": "unknown",
        "freshness_age_s": 0.0,
        "confidence": "claimed",
        "evidence_refs": [reason],
        "last_checked": generated_at,
        "safe_next_action": "audit the failed verifier seam; do not remediate from this aggregate API",
        "locked_actions": DEFAULT_LOCKED_ACTIONS,
        "what_would_prove_green": "a named verifier function returns live with evidence_refs and freshness_age_s",
        "what_breaks_if_false": "a false unknown masks whether the graph is backed by a working probe",
    }


def _resolve_selector(snapshot: dict[str, Any], donor: str, path: str) -> dict[str, Any] | None:
    if donor == "os":
        section_id, item_name = path.split("/", 1)
        for section in snapshot.get("sections", []):
            if section.get("id") == section_id:
                return next((item for item in section.get("items", []) if item.get("name") == item_name), None)
        return None
    if donor == "connectome":
        return next((node for node in snapshot.get("nodes", []) if node.get("id") == path), None)
    return None


def _status_to_state(donor: str, status: str) -> tuple[str, str | None]:
    if donor == "os":
        mapping = {"green": "live", "amber": "stale", "red": "broken", "unknown": "unknown", "info": "unknown"}
    else:
        mapping = {"ok": "live", "active": "live", "degraded": "stale", "blocked": "broken", "source unreachable": "broken", "unknown": "unknown", "not-serving": "stale", "completed": "live", "serving": "live"}
    if status in mapping:
        return mapping[status], None
    return "unknown", f"unmapped-status:{status}"


def _detail_from_selector(selector: dict[str, Any]) -> str:
    detail = selector.get("detail") or selector.get("label") or selector.get("id") or selector.get("name") or "n/a"
    return str(detail)


def _donor_evidence(donor: str, path: str, selector: dict[str, Any]) -> list[str]:
    if donor == "connectome":
        prov = selector.get("provenance") or {}
        detail = selector.get("detail") or selector.get("label") or selector.get("id")
        refs = [f"donor:connectome:{path}:{detail}"]
        refs.extend(f"donor:connectome:{path}:{key}={value}" for key, value in prov.items() if key in {"source", "query", "field", "value"})
        return refs
    return [f"donor:os:{path}:{_detail_from_selector(selector)}"]


def _freshness_from_selector(selector: dict[str, Any]) -> float:
    metric = selector.get("metric")
    if isinstance(metric, dict):
        for key in ("age_s", "age_seconds"):
            if isinstance(metric.get(key), int | float):
                return round(float(metric[key]), 3)
    return 0.0


def _apply_honesty(system_id: str, truth: dict[str, Any], donor_refs: list[str]) -> dict[str, Any]:
    if system_id not in HONESTY_CAPS:
        return truth
    kind, capped_state = HONESTY_CAPS[system_id]
    line = HONESTY_LINES[system_id]
    if kind == "cap":
        if STATE_RANK[truth["probe_state"]] > STATE_RANK[capped_state]:
            truth = dict(truth)
            truth["probe_state"] = capped_state
            truth["confidence"] = "claimed"
            truth["evidence_refs"] = [
                "honesty-cap:presence-only-probe",
                f"design:V6-DESIGN-LOCKED-blended-organism.html:{line}",
                *donor_refs,
            ]
        return truth
    truth = dict(truth)
    pin_reason = kind.split(":", 1)[1]
    truth["probe_state"] = capped_state
    truth["confidence"] = "claimed"
    truth["evidence_refs"] = [
        f"honesty-pin:{pin_reason}",
        f"design:V6-DESIGN-LOCKED-blended-organism.html:{line}",
        *donor_refs,
    ]
    return truth


def _system_truth(system_id: str, generated_at: str, os_snapshot: dict[str, Any], connectome_snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    donor, path = SYSTEM_SOURCE_MAP[system_id]
    snapshot = os_snapshot if donor == "os" else connectome_snapshot
    selector = _resolve_selector(snapshot, donor, path)
    if selector is None:
        truth = _unknown_truth(system_id, generated_at, f"missing-selector:{donor}:{path}")
        return truth, {}
    state, unmapped = _status_to_state(donor, str(selector.get("status") or "unknown"))
    refs = _donor_evidence(donor, path, selector)
    if unmapped:
        refs.append(unmapped)
    truth = {
        "id": system_id,
        "probe_state": state,
        "freshness_age_s": _freshness_from_selector(selector),
        "confidence": "single-probe",
        "evidence_refs": refs,
        "last_checked": generated_at,
        "safe_next_action": f"audit/re-probe {system_id}; action-ticket only",
        "locked_actions": DEFAULT_LOCKED_ACTIONS,
        "what_would_prove_green": "mapped probe reports live/green with fresh evidence; manual claim upgraded only after verifier exists",
        "what_breaks_if_false": "false system state can mislead graph drill-down and action queues",
    }
    truth = _apply_honesty(system_id, truth, refs)
    observed = selector.get("metric") if isinstance(selector.get("metric"), dict) else {}
    return truth, observed or {}


def _territory_truth_from_members(territory_id: str, member_truths: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    worst = min(member_truths, key=lambda truth: STATE_RANK[truth["probe_state"]])
    probed_count = sum(1 for truth in member_truths if truth.get("confidence") != "claimed")
    confidence = "corroborated" if probed_count >= 2 else "single-probe" if probed_count == 1 else "claimed"
    return {
        "id": territory_id,
        "probe_state": worst["probe_state"],
        "freshness_age_s": max(float(truth["freshness_age_s"]) for truth in member_truths),
        "confidence": confidence,
        "evidence_refs": [
            f"rollup:{probed_count}/{len(member_truths)} members probed",
            f"worst:{worst['id']}:{worst['probe_state']}",
            str(worst["evidence_refs"][0]),
        ],
        "last_checked": generated_at,
        "safe_next_action": f"drill into {worst['id']}; act only through that member's truth object",
        "locked_actions": DEFAULT_LOCKED_ACTIONS,
        "what_would_prove_green": "every member's own verifier reports live (worst member governs)",
        "what_breaks_if_false": "a false territory rollup hides a failing member behind an aggregate",
    }


def _build_systems_and_territories(generated_at: str, os_snapshot: dict[str, Any], connectome_snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    systems: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for territory in TERRITORY_SPECS:
        terr_slug = territory["id"].split(":", 1)[1]
        for member in territory["members"]:
            system_id = f"system:{terr_slug}/{member}"
            try:
                truth, observed = _system_truth(system_id, generated_at, os_snapshot, connectome_snapshot)
                truth = validate_truth_object(truth, system_id)
            except Exception as exc:
                truth = validate_truth_object(_unknown_truth(system_id, generated_at, f"exception:{type(exc).__name__}:{exc}"), system_id)
                observed = {}
            wrapper = {"id": system_id, "label": SYSTEM_LABELS[system_id], "territory": territory["id"], "truth": truth, "observed": observed}
            systems.append(wrapper)
            by_id[system_id] = wrapper
    territories: list[dict[str, Any]] = []
    for territory in TERRITORY_SPECS:
        terr_slug = territory["id"].split(":", 1)[1]
        member_ids = [f"system:{terr_slug}/{member}" for member in territory["members"]]
        member_truths = [by_id[member_id]["truth"] for member_id in member_ids]
        truth = validate_truth_object(_territory_truth_from_members(territory["id"], member_truths, generated_at), territory["id"])
        territories.append({"id": territory["id"], "label": territory["label"], "members": member_ids, "truth": truth})
    return systems, territories


def _bridge_edge_truth(generated_at: str, connectome_snapshot: dict[str, Any]) -> dict[str, Any]:
    entry = next((edge for edge in connectome_snapshot.get("edges", []) if edge.get("id") == "projects-brain"), None)
    if entry is None:
        return validate_truth_object(_unknown_truth("edge:kanban-db->mvms--bridge", generated_at, "verifier-absent:projects-brain"), "edge:kanban-db->mvms--bridge")
    prov = entry.get("provenance") or {}
    refs = ["donor:connectome:edges/projects-brain", *(f"donor:connectome:edges/projects-brain:{key}={value}" for key, value in prov.items())]
    return validate_truth_object(
        {
            "id": "edge:kanban-db->mvms--bridge",
            "probe_state": "live",
            "freshness_age_s": 0.0,
            "confidence": "single-probe",
            "evidence_refs": refs,
            "last_checked": generated_at,
            "safe_next_action": "read bridge provenance; no mutation from graph edge",
            "locked_actions": DEFAULT_LOCKED_ACTIONS,
            "what_would_prove_green": "connectome snapshot includes verified projects-brain bridge entry",
            "what_breaks_if_false": "a false bridge edge hides broken Kanban→MVMS learning feedback",
        },
        "edge:kanban-db->mvms--bridge",
    )


def _backup_edge_truth(generated_at: str) -> dict[str, Any]:
    try:
        payload = dashboard_nexus_slice.backup_offbox_slice()
        return validate_truth_object(payload, BACKUP_OFFBOX_EDGE_ID)
    except Exception as exc:
        reason = f"exception:{type(exc).__name__}:{exc}"
        NON_DONOR_DEGRADED_REASONS.append(reason)
        return validate_truth_object(_unknown_truth(BACKUP_OFFBOX_EDGE_ID, generated_at, reason), BACKUP_OFFBOX_EDGE_ID)


def _build_edges(generated_at: str, connectome_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    edges = [
        {"id": "edge:kanban-db->mvms--bridge", "label": "kanban-db → mvms (bridge)", "source": "territory:memory", "target": "territory:protection", "kind": "probed", "truth": _bridge_edge_truth(generated_at, connectome_snapshot)},
        {"id": BACKUP_OFFBOX_EDGE_ID, "label": "nightly-backup → off-box", "source": "territory:protection", "target": "territory:memory", "kind": "probed", "truth": _backup_edge_truth(generated_at)},
    ]
    for edge_id, source, target, label, line in MANUAL_EDGES:
        edges.append({"id": edge_id, "label": label, "source": source, "target": target, "kind": "manual", "truth": validate_truth_object(_manual_truth(edge_id, generated_at, line=line), edge_id)})
    for n in range(28, 37):
        edge_id = f"edge:manual-reserved-{n}"
        label = f"reserved claimed dependency #{n} — V6 copy asserts 36 claimed edges but JS does not enumerate this edge"
        edges.append({"id": edge_id, "label": label, "source": None, "target": None, "kind": "manual", "truth": validate_truth_object(_manual_truth(edge_id, generated_at, line="158"), edge_id)})
    return edges


def _coverage(territories: list[dict[str, Any]], systems: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "territories_total": len(territories),
        "systems_total": len(systems),
        "edges_total": len(edges),
        "edges_probed": sum(1 for edge in edges if edge.get("kind") == "probed"),
        "edges_manual": sum(1 for edge in edges if edge.get("kind") == "manual"),
    }


def _diagnostics(systems: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    for wrapper in [*systems, *edges]:
        truth = wrapper["truth"]
        if truth["confidence"] == "claimed":
            continue
        if truth["probe_state"] in {"broken", "stale"}:
            diagnostics.append({"severity": "red" if truth["probe_state"] == "broken" else "amber", "object_id": wrapper["id"], "reason": str(truth["evidence_refs"][0])})
    return diagnostics


def build_envelope() -> dict[str, Any]:
    generated_at = _iso_now()
    status = "ok"
    donor_errors: list[str] = []
    NON_DONOR_DEGRADED_REASONS.clear()
    try:
        os_snapshot = dashboard_os.get_os_snapshot()
    except Exception as exc:
        os_snapshot = {"sections": [], "generated_at": generated_at}
        status = "degraded"
        donor_errors.append(f"exception:{type(exc).__name__}:{exc}")
    try:
        connectome_snapshot = dashboard_connectome.get_connectome_snapshot()
    except Exception as exc:
        connectome_snapshot = {"nodes": [], "edges": [], "meta": {"generated_at": generated_at}}
        status = "degraded"
        donor_errors.append(f"exception:{type(exc).__name__}:{exc}")
    systems, territories = _build_systems_and_territories(generated_at, os_snapshot, connectome_snapshot)
    edges = _build_edges(generated_at, connectome_snapshot)
    if NON_DONOR_DEGRADED_REASONS:
        status = "degraded"
    if donor_errors:
        for wrapper in [*systems, *edges]:
            if wrapper["truth"]["evidence_refs"][0].startswith(("missing-selector", "verifier-absent")):
                wrapper["truth"] = validate_truth_object(_unknown_truth(wrapper["id"], generated_at, donor_errors[0]), wrapper["id"])
    return {
        "generated_at": generated_at,
        "cache_ttl_seconds": int(_TTL_S),
        "status": status,
        "territories": territories,
        "systems": systems,
        "edges": edges,
        "coverage": _coverage(territories, systems, edges),
        "diagnostics": _diagnostics(systems, edges),
    }


def get_cached_envelope() -> dict[str, Any]:
    global _CACHE
    now = time.monotonic()
    with _LOCK:
        if _CACHE is not None and now < _CACHE[1]:
            return _CACHE[0]
        try:
            envelope = build_envelope()
        except Exception as exc:
            generated_at = _iso_now()
            os_snapshot = {"sections": [], "generated_at": generated_at}
            connectome_snapshot = {"nodes": [], "edges": [], "meta": {"generated_at": generated_at}}
            systems, territories = _build_systems_and_territories(generated_at, os_snapshot, connectome_snapshot)
            edges = _build_edges(generated_at, connectome_snapshot)
            for wrapper in [*systems, *edges]:
                wrapper["truth"] = validate_truth_object(_unknown_truth(wrapper["id"], generated_at, f"exception:{type(exc).__name__}:{exc}"), wrapper["id"])
            envelope = {"generated_at": generated_at, "cache_ttl_seconds": int(_TTL_S), "status": "degraded", "territories": territories, "systems": systems, "edges": edges, "coverage": _coverage(territories, systems, edges), "diagnostics": []}
        _CACHE = (envelope, now + _TTL_S)
        return envelope


def clear_cache_for_tests() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


@router.get("")
def get_nexus() -> dict[str, Any]:
    return get_cached_envelope()


@router.get("/object/{object_id:path}", response_model=None)
def get_nexus_object(object_id: str):
    envelope = get_cached_envelope()
    for wrapper in [*envelope["territories"], *envelope["systems"], *envelope["edges"]]:
        if wrapper["id"] == object_id:
            return {"object": wrapper, "generated_at": envelope["generated_at"]}
    return JSONResponse(status_code=404, content={"error": "unknown truth object id", "id": object_id})


@router.get("/coverage")
def get_nexus_coverage() -> dict[str, Any]:
    envelope = get_cached_envelope()
    manual_edge_ids = [edge["id"] for edge in envelope["edges"] if edge["kind"] == "manual"]
    return {"generated_at": envelope["generated_at"], "coverage": envelope["coverage"], "manual_edge_ids": manual_edge_ids}
