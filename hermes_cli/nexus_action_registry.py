"""Server-owned Nexus action-ticket registry for W2B.

Atomicity/deploy note: the action backend assumes one uvicorn worker. The
capability/event store lock is module-global in ``dashboard_nexus_actions``;
a multi-worker deploy requires a file-lock rework before arming live dispatch.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema

REGISTRY_VERSION = "w2b-2026-07-03"
SCHEMA_PATH = Path(__file__).with_name("nexus_action_assets") / "action-ticket-schema.json"
ALLOWED_EFFECT_TAGS = frozenset({"read-only", "audit-dir-write", "kanban-comment"})
FORBIDDEN_EFFECT_TAGS = frozenset(
    {
        "worktree-patch",
        "process-signal",
        "service-restart",
        "cron-mutation",
        "config-change",
        "provider-change",
        "auth-change",
        "credential-touch",
        "deploy",
        "destructive-cleanup",
        "gbrain",
        "mvms-writeback",
    }
)
NEXUS_ACTION_LANES = (11, 12)
KANBAN_COMMENT_ALLOWLIST = frozenset({"t_30c5cdd7"})
_FORBIDDEN_ACTIONS = (
    "service restart",
    "cron/timer mutation",
    "config/provider/auth/credential/security change",
    "git push/merge/branch-switch/reset",
    "deploy",
    "MVMS writes",
    "GBrain/SANDGB access",
    "destructive cleanup",
)


class NexusRegistryError(RuntimeError):
    """Raised when the static W2B registry violates its authority contract."""


def _ticket(
    action_id: str,
    *,
    title: str,
    finding_id: str,
    finding_label: str,
    severity: str,
    source_surface: str,
    evidence_refs: list[str],
    gate_class: str,
    action_verb: str,
    execution_mode: str,
    requires_explicit_go: bool,
    phases: list[str],
    allowed_actions: list[str],
    success_condition: str,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "title": title,
        "trigger_source": {
            "finding_id": finding_id,
            "finding_label": finding_label,
            "severity": severity,
            "source_surface": source_surface,
            "evidence_refs": evidence_refs,
        },
        "gate_class": gate_class,
        "action_verb": action_verb,
        "bounded_workflow": {
            "lane_profile": "loki",
            "phases": phases,
            "allowed_actions": allowed_actions,
            "forbidden_actions": list(_FORBIDDEN_ACTIONS),
            "success_condition": success_condition,
            "stop_conditions": list(_FORBIDDEN_ACTIONS),
        },
        "scope_lock": {
            "workspace_mode": "audit-dir-only",
            "write_allowlist": [f"audits/os-nexus-actions/{action_id}/"],
            "read_allowlist": ["state", "logs", "git status", "kanban card t_30c5cdd7"],
            "requires_worktree_isolation": False,
            "budget": {"wall_clock_minutes": 40, "max_tool_calls": 200, "max_retries": 1},
        },
        "evidence_output": {
            "audit_dir_template": f"audits/os-nexus-actions/{action_id}/<utc-ts>",
            "required_files": ["REQUEST.json", "REGISTRY-ENTRY.json", "SCOPE-LOCK.txt", "PACKET.md", "REPORT.md"],
            "kanban_comment": "kanban card t_30c5cdd7",
            "mvms_record": False,
        },
        "rollback": {
            "strategy": "No mutation is performed in W2B; rollback is evidence-only closeout.",
            "pre_state_capture": ["REQUEST.json", "REGISTRY-ENTRY.json", "SCOPE-LOCK.txt"],
            "revert_steps": ["No live revert step exists for audit-dir-only W2B packets."],
        },
        "dispatch": {
            "chokepoint": "loki_send.py",
            "idempotency_key": "server-issued sha256(session_hash + action_id + finding_id + preflight_hash + server_nonce)",
            "audit_event": f"nexus_action:{action_id}",
            "requires_explicit_go": requires_explicit_go,
        },
        "execution_mode": execution_mode,
        "effect_tags": ["read-only", "audit-dir-write", "kanban-comment"],
    }


TICKETS: tuple[dict[str, Any], ...] = (
    _ticket(
        "act-reap-codex-orphans",
        title="Reap Codex orphan audit plan",
        finding_id="codex-orphans",
        finding_label="49 tracked: EXECUTING=14 · ORPHANED=35 · missing_worktree=37",
        severity="red",
        source_surface="os-nexus/git/lane-runs",
        evidence_refs=["49 tracked: EXECUTING=14 · ORPHANED=35 · missing_worktree=37"],
        gate_class="agent-drainable",
        action_verb="Prepare Safe Repair",
        execution_mode="audit",
        requires_explicit_go=False,
        phases=["inventory", "dry-run reap plan", "approval packet"],
        allowed_actions=["read registries", "write audit plan", "comment kanban"],
        success_condition="A dry-run orphan reap plan exists with no process kills or deletions.",
    ),
    _ticket(
        "act-cron-deadman-triage",
        title="Cron deadman triage audit",
        finding_id="cron-deadman",
        finding_label="Cron/deadman health needs read-only freshness triage before any timer work.",
        severity="amber",
        source_surface="os-nexus/control/hermes-cron",
        evidence_refs=["cron reliability snapshot", "deadman freshness signal"],
        gate_class="agent-drainable",
        action_verb="Summon Hermes Audit",
        execution_mode="audit",
        requires_explicit_go=False,
        phases=["read timers", "read latest logs", "write triage"],
        allowed_actions=["systemctl show reads", "journal reads", "write audit report", "comment kanban"],
        success_condition="A cron/deadman triage report ranks stale jobs without arming timers.",
    ),
    _ticket(
        "act-mvms-dr-approval",
        title="MVMS DR approval packet",
        finding_id="mvms-dr",
        finding_label="MVMS disaster-recovery action requires josep approval packet only.",
        severity="red",
        source_surface="os-nexus/memory/mvms",
        evidence_refs=["MVMS backup/restore-drill status", "DR gate requires owner approval"],
        gate_class="josep-gated",
        action_verb="Draft Approval Packet",
        execution_mode="approval-packet",
        requires_explicit_go=True,
        phases=["summarize DR state", "draft approval packet"],
        allowed_actions=["read DR artifacts", "write approval packet", "comment kanban"],
        success_condition="A josep-readable approval packet names exact DR commands and rollback gates without executing them.",
    ),
    _ticket(
        "act-distiller-review-packet",
        title="Distiller review approval packet",
        finding_id="distiller-review",
        finding_label="Learning distiller promotion needs review packet before writes.",
        severity="watch",
        source_surface="os-nexus/learning/distiller",
        evidence_refs=["distiller promotion queue", "learning-loop review gate"],
        gate_class="josep-gated",
        action_verb="Draft Approval Packet",
        execution_mode="approval-packet",
        requires_explicit_go=True,
        phases=["read queue", "draft review packet"],
        allowed_actions=["read queue artifacts", "write review packet", "comment kanban"],
        success_condition="A review packet separates promote-ready candidates from blocked garbage without writing MVMS.",
    ),
    _ticket(
        "act-deploy-divergence-brief",
        title="Deploy divergence approval brief",
        finding_id="deploy-divergence",
        finding_label="Deploy branch/source divergence needs audit brief before merge/push/restart.",
        severity="amber",
        source_surface="os-nexus/git/deploy-div",
        evidence_refs=["deploy divergence signal", "git health source map"],
        gate_class="josep-gated",
        action_verb="Audit Git Drift",
        execution_mode="approval-packet",
        requires_explicit_go=True,
        phases=["read git status", "write divergence brief"],
        allowed_actions=["read git history", "write approval brief", "comment kanban"],
        success_condition="A divergence brief identifies exact commits and preserves no push/merge/restart gates.",
    ),
    _ticket(
        "act-recall-repair-plan",
        title="Recall repair investigation lane",
        finding_id="recall-repair",
        finding_label="Recall quality needs read-only investigation and repair plan.",
        severity="stale",
        source_surface="os-nexus/learning/recall",
        evidence_refs=["recall eval snapshots", "learning index status"],
        gate_class="agent-drainable",
        action_verb="Open Investigation Lane",
        execution_mode="audit",
        requires_explicit_go=False,
        phases=["read evals", "rank misses", "write repair plan"],
        allowed_actions=["read recall evals", "write investigation report", "comment kanban"],
        success_condition="A repair plan ranks recall failure modes with no store writes or retraining.",
    ),
    _ticket(
        "act-authority-drift-audit",
        title="Authority drift audit lane",
        finding_id="authority-drift",
        finding_label="Authority matrix drift needs audit before config/profile changes.",
        severity="watch",
        source_surface="os-nexus/control/authority",
        evidence_refs=["authority matrix lint", "profile/tool policy snapshot"],
        gate_class="agent-drainable",
        action_verb="Summon Hermes Audit",
        execution_mode="audit",
        requires_explicit_go=False,
        phases=["read authority rows", "compare live posture", "write audit"],
        allowed_actions=["read config/profile snapshots", "write drift audit", "comment kanban"],
        success_condition="An authority drift audit names mismatches without changing config/profile/tool policy.",
    ),
)


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _extract_kanban_target(comment: str) -> str | None:
    for token in KANBAN_COMMENT_ALLOWLIST:
        if token in comment:
            return token
    return None


def validate_registry(tickets: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Validate and lint W2B tickets, returning a deep-copied list."""
    selected = list(TICKETS if tickets is None else tickets)
    schema = _schema()
    validator = jsonschema.Draft202012Validator(schema)
    validated: list[dict[str, Any]] = []
    for ticket in selected:
        errors = sorted(validator.iter_errors(ticket), key=lambda err: list(err.path))
        if errors:
            detail = "; ".join(error.message for error in errors)
            raise NexusRegistryError(f"{ticket.get('id', '<unknown>')} schema invalid: {detail}")
        action_id = str(ticket["id"])
        tags = set(ticket.get("effect_tags", []))
        if not tags <= ALLOWED_EFFECT_TAGS:
            raise NexusRegistryError(f"{action_id} effect tags outside allowlist: {sorted(tags - ALLOWED_EFFECT_TAGS)}")
        if "mvms-writeback" in tags or ticket["evidence_output"].get("mvms_record") is not False:
            raise NexusRegistryError(f"{action_id} MVMS writeback is forbidden in W2B")
        workspace_mode = ticket["scope_lock"].get("workspace_mode")
        if workspace_mode not in {"audit-dir-only", "read-only", "approval-packet-only"}:
            raise NexusRegistryError(f"{action_id} workspace_mode not implemented in W2B: {workspace_mode}")
        if ticket["gate_class"] == "josep-gated":
            if ticket["execution_mode"] not in {"audit", "approval-packet", "dry-run-only"}:
                raise NexusRegistryError(f"{action_id} invalid josep-gated execution mode")
            if ticket["dispatch"].get("requires_explicit_go") is not True:
                raise NexusRegistryError(f"{action_id} josep-gated ticket must require explicit GO")
        target = _extract_kanban_target(str(ticket["evidence_output"].get("kanban_comment", "")))
        if target not in KANBAN_COMMENT_ALLOWLIST:
            raise NexusRegistryError(f"{action_id} kanban target outside allowlist")
        for root in ticket["scope_lock"].get("write_allowlist", []):
            if not str(root).startswith("audits/os-nexus-actions/"):
                raise NexusRegistryError(f"{action_id} write_allowlist outside audit root: {root}")
        validated.append(copy.deepcopy(ticket))
    return validated


VALIDATED_TICKETS = validate_registry()
