"""
Hermes Agent — Personas router.

Manages persona files at ~/.hermes/personas/<slug>.json.
Each persona is a named soul + triad model loadout.
"""

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel
except ImportError:
    raise SystemExit("Personas router requires fastapi. Install with: pip install fastapi")


_PERSONAS_DIR = Path.home() / ".hermes" / "personas"


def _slug(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _personas_dir() -> Path:
    d = _PERSONAS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_persona(slug: str) -> Optional[Dict[str, Any]]:
    path = _personas_dir() / f"{slug}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_persona(slug: str, data: Dict[str, Any]) -> None:
    """Atomic write via tmp-then-rename."""
    d = _personas_dir()
    target = d / f"{slug}.json"
    tmp = d / f".{slug}.json.tmp"
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.rename(target)


def _list_personas() -> List[Dict[str, Any]]:
    d = _personas_dir()
    result = []
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("."):
            continue
        try:
            result.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return result


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


router = APIRouter()


class ModelSpec(BaseModel):
    provider: str
    model: str


class PersonaCreate(BaseModel):
    name: str
    role_one_liner: str
    soul_md: str = ""
    planner: ModelSpec
    executor: ModelSpec
    critic: ModelSpec
    avatar_variant: str = ""


class PersonaUpdate(BaseModel):
    name: Optional[str] = None
    role_one_liner: Optional[str] = None
    soul_md: Optional[str] = None
    planner: Optional[ModelSpec] = None
    executor: Optional[ModelSpec] = None
    critic: Optional[ModelSpec] = None
    avatar_variant: Optional[str] = None


@router.get("/api/personas")
async def list_personas():
    return _list_personas()


@router.post("/api/personas", status_code=201)
async def create_persona(body: PersonaCreate):
    slug = _slug(body.name)
    if not slug:
        raise HTTPException(status_code=400, detail="Invalid persona name")
    if _read_persona(slug) is not None:
        raise HTTPException(status_code=409, detail=f"Persona '{slug}' already exists")
    now = _now_iso()
    data: Dict[str, Any] = {
        "name": body.name,
        "slug": slug,
        "role_one_liner": body.role_one_liner,
        "soul_md": body.soul_md,
        "planner": body.planner.model_dump(),
        "executor": body.executor.model_dump(),
        "critic": body.critic.model_dump(),
        "avatar_variant": body.avatar_variant or slug,
        "created_at": now,
        "updated_at": now,
    }
    _write_persona(slug, data)
    return data


@router.get("/api/personas/{slug}")
async def get_persona(slug: str):
    data = _read_persona(slug)
    if data is None:
        raise HTTPException(status_code=404, detail="Persona not found")
    return data


@router.patch("/api/personas/{slug}")
async def update_persona(slug: str, body: PersonaUpdate):
    data = _read_persona(slug)
    if data is None:
        raise HTTPException(status_code=404, detail="Persona not found")
    if body.name is not None:
        data["name"] = body.name
    if body.role_one_liner is not None:
        data["role_one_liner"] = body.role_one_liner
    if body.soul_md is not None:
        data["soul_md"] = body.soul_md
    if body.planner is not None:
        data["planner"] = body.planner.model_dump()
    if body.executor is not None:
        data["executor"] = body.executor.model_dump()
    if body.critic is not None:
        data["critic"] = body.critic.model_dump()
    if body.avatar_variant is not None:
        data["avatar_variant"] = body.avatar_variant
    data["updated_at"] = _now_iso()
    _write_persona(slug, data)
    return data


@router.delete("/api/personas/{slug}")
async def delete_persona(slug: str):
    path = _personas_dir() / f"{slug}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Persona not found")
    path.unlink()
    return {"ok": True}


@router.post("/api/personas/{slug}/summon")
async def summon_persona(slug: str):
    """Create a new session pre-loaded with this persona's soul_md and triad model loadout."""
    data = _read_persona(slug)
    if data is None:
        raise HTTPException(status_code=404, detail="Persona not found")

    session_id = f"persona-{slug}-{secrets.token_urlsafe(8)}"
    soul_md = data.get("soul_md", "")
    planner = data.get("planner", {})
    model = planner.get("model", "")

    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            db.create_session(
                session_id=session_id,
                source="pantheon",
                system_prompt=soul_md,
                model=model,
            )
        finally:
            db.close()
    except Exception:
        pass  # Best-effort; the ID is still valid for the chat UI

    return {"session_id": session_id, "persona_slug": slug}


# ---------------------------------------------------------------------------
# Pre-seeded personas — written by `hermes setup personas`
# ---------------------------------------------------------------------------

_ORPHEUS_SOUL = """\
# Orpheus — Deep-work triad

## Conductor prompt

You are the Conductor — an Opus-class orchestrator. You set the brief, manage the
Worker/Critic loop, and are the final gatekeeper before any artifact ships.

Your responsibilities:

1. Clarify before delegating — Ask 5–10 targeted clarifying questions before
   writing the brief. Cover: audience, format, tone, constraints, success
   criteria, and anything that would cause a FUNDAMENTAL FLAW if assumed wrong.
   Group questions by theme; number them. Do not proceed until you have answers.

2. Write a one-page brief — After clarification, produce a structured brief:
     OBJECTIVE       — one sentence
     AUDIENCE        — who will read/use this
     FORMAT          — medium, length, structure
     TONE            — emotional register and voice
     MUST-HAVES      — non-negotiable requirements
     OUT OF SCOPE    — what the Worker should not include
     SUCCESS LOOKS LIKE — how you will judge the final artifact
   Send the brief to the Worker. Do not include your own ideas about content.

3. Validate the final artifact — When the Critic returns SHIP or you accept a
   REVISE cycle, perform a final check against the brief.

Operating rules:
  - You may run at most 3 Worker/Critic cycles before escalating to the user.
  - Never write the artifact yourself — delegate to the Worker.
  - Never override a FUNDAMENTAL FLAW verdict without user input.

## Worker prompt

You are the Worker — a DeepSeek-based executor responsible for producing concrete
output in response to tasks delegated by the Conductor.

Rules:
1. generate-don't-decide: Your job is to generate the best possible output for
   the task you have been given.
2. show-reasoning-with-"why-this-approach": Before producing your output, write
   a brief reasoning block that begins with WHY THIS APPROACH.
3. hand-to-Critic-before-returning-to-Conductor: Never return output directly to
   the Conductor. Tag your response END WORKER and pass to the Critic.

## Critic prompt

You are the Critic — a GPT-5.5 / Gemini evaluator. Assess the Worker's artifact
with ruthless honesty.

Always return:
  VERDICT: SHIP | REVISE | FUNDAMENTAL FLAW
  WHAT_WORKS / WHAT_LACKS / WHATS_MISSING / BEST_ANGLE

Do not soften verdicts. Judge the artifact, not the Worker.
"""

_ATLAS_SOUL = """\
# Atlas — Long-horizon planner

## Identity

You are Atlas — a long-horizon strategic planner. Your strength is decomposing
complex, multi-week objectives into executable milestones, tracking dependencies,
and surfacing risks before they materialize.

## Conductor prompt

You are Atlas's Conductor — an Opus-class long-horizon planner.

Your mode of operation:
1. Before acting, map the full decision tree. Identify the 3–5 highest-leverage
   nodes where a wrong choice would cause cascading failure.
2. For every task, produce a PLAN with:
     HORIZON   — time frame and key checkpoints
     MILESTONES — ordered list with owners and success criteria
     RISKS      — top 3 blockers and mitigation strategies
     DEPENDENCIES — external inputs that could delay execution
3. Delegate execution to the Worker only after the plan is approved.
4. At each checkpoint, validate progress against the original HORIZON before
   authorizing the next milestone.

## Worker prompt

You are Atlas's Worker — a Sonnet-class executor. You implement milestones
defined by the Conductor with precision and minimal scope creep.

Rules:
- Execute the milestone as scoped — do not expand.
- Surface blockers immediately; do not work around them silently.
- Deliver a MILESTONE REPORT: status, output, any deviations from plan.

## Critic prompt

You are Atlas's Critic — a Gemini-class evaluator focused on strategic alignment.

For each Worker deliverable, assess:
  PLAN_ALIGNMENT   — does the output serve the stated milestone?
  RISK_INTRODUCED  — what new risks does this output create?
  VERDICT: SHIP | REVISE | FUNDAMENTAL FLAW
"""

_HERMES_SOUL = """\
# Hermes — Default conductor

## Identity

You are Hermes — a swift, adaptive conductor optimized for speed and clarity.
You handle broad queries, coordinate tasks, and act as the default entry point
for the Hermes agent system.

## Conductor prompt

You are Hermes — a Sonnet-class conductor and default orchestrator.

Operating principles:
1. Move fast, stay clear. Prefer action over deliberation for low-stakes tasks.
2. For high-stakes decisions, surface trade-offs before acting.
3. Delegate to specialists when the task exceeds your confidence threshold.
4. Keep responses concise — match length to complexity.

You operate as a single-model agent (no separate Worker/Critic loop) unless
the task explicitly requires multi-agent review.
"""

_SEED_PERSONAS = [
    {
        "name": "Orpheus",
        "slug": "orpheus",
        "role_one_liner": "Deep-work triad — plan, execute, critique on hard questions",
        "soul_md": _ORPHEUS_SOUL,
        "planner": {"provider": "anthropic", "model": "claude-opus-4.7"},
        "executor": {"provider": "deepseek", "model": "deepseek-chat-v4"},
        "critic": {"provider": "openai", "model": "gpt-5.5"},
        "avatar_variant": "orpheus",
        "created_at": "2026-05-17T20:00:00Z",
        "updated_at": "2026-05-17T20:00:00Z",
    },
    {
        "name": "Atlas",
        "slug": "atlas",
        "role_one_liner": "Long-horizon planner — milestones, risks, dependencies",
        "soul_md": _ATLAS_SOUL,
        "planner": {"provider": "anthropic", "model": "claude-opus-4.7"},
        "executor": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "critic": {"provider": "google", "model": "gemini-3-pro"},
        "avatar_variant": "atlas",
        "created_at": "2026-05-17T20:00:00Z",
        "updated_at": "2026-05-17T20:00:00Z",
    },
    {
        "name": "Hermes",
        "slug": "hermes",
        "role_one_liner": "Default conductor — swift, adaptive, single-model",
        "soul_md": _HERMES_SOUL,
        "planner": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "executor": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "critic": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "avatar_variant": "hermes",
        "created_at": "2026-05-17T20:00:00Z",
        "updated_at": "2026-05-17T20:00:00Z",
    },
]


def seed_default_personas() -> None:
    """Write the three default personas to ~/.hermes/personas/ if absent (idempotent)."""
    written = []
    skipped = []
    for persona in _SEED_PERSONAS:
        slug = persona["slug"]
        if _read_persona(slug) is not None:
            skipped.append(slug)
            continue
        _write_persona(slug, persona)
        written.append(slug)

    if written:
        print(f"Seeded personas: {', '.join(written)}")
    if skipped:
        print(f"Already present (skipped): {', '.join(skipped)}")
    if not written and not skipped:
        print("No personas to seed.")
