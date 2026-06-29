"""READY_SPEC trust-compiler — the pure, fail-closed validator for Kanban dispatch.

A card's ``ready`` status is only *mechanically* safe today (status/claim-lock/parents/
assignee/workspace). READY_SPEC makes it *provably* safe-to-dispatch: a card must declare
a small machine-checkable contract in a fenced ``ready-spec`` YAML block in its body, and
``validate_ready_spec`` checks it before ``dispatch_once`` claims the card.

Design (locked by the ready-spec-council, 2026-06-29; co-designed with Hermes):
  * Five fields; only ``scope`` is required (the one thing no machine can infer). The other
    four have safe defaults so a minimal card satisfies them for free.
  * The block lives in the card BODY (canonical, diffable, additive — no schema migration).
  * This module is PURE: no I/O, no mutation, never raises. Any parse/validation error
    fails CLOSED -> ok=False (never half-parses into a false PASS).
  * READY_SPEC is a DECLARATION gate, not a runtime jail: ``stop_gates``/``allowed_workspace``
    are self-assertions that bound declared intent + blast radius. The command-guard floor /
    dispatch sandbox remains the TRUE runtime enforcement boundary.

Enforcement wiring (warn/enforce modes, grandfather epoch) lives in dispatch_once, NOT here.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import yaml  # PyYAML — already a dependency (config.yaml is loaded with it)
    _HAVE_YAML = True
except ImportError:  # pragma: no cover - defensive; fail closed if absent
    _HAVE_YAML = False


# --- vocabulary / defaults --------------------------------------------------
STOP_GATE_VOCAB = {
    "config", "auth", "security", "service", "cron",
    "git-push", "global-dispatch", "network-spend",
}
# The global no-touch set a card inherits when it declares no stop_gates of its own.
DEFAULT_STOP_GATES = sorted(STOP_GATE_VOCAB)
SAFE_WORKSPACE_SLUGS = {"scratch", "sandbox"}
DEFAULT_AUDITS_PREFIX = os.path.expanduser("~/.hermes/audits/")
# Path fragments that make a declared workspace unsafe (blast-radius denylist).
_UNSAFE_WS_FRAGMENTS = ("/.hermes/config", "/.claude", "/.ssh", "/.gnupg", "/auth", "/.env")

# Single ``ready-spec`` fence in the body; everything else is ignored free text.
_FENCE_RE = re.compile(r"```ready-spec[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass
class ReadySpecResult:
    """Result of validating one card. ``errors`` is the stable machine contract:
    a list of ``{"code": <stable-code>, "message": <human>}``. ``ok`` == no errors."""
    ok: bool
    errors: list[dict] = field(default_factory=list)
    resolved: dict[str, Any] = field(default_factory=dict)

    @property
    def codes(self) -> list[str]:
        return [e["code"] for e in self.errors]

    def summary(self) -> str:
        return "PASS" if self.ok else "FAIL  " + ",".join(self.codes)


class ReadySpecParseError(Exception):
    """Raised internally when the fenced block cannot be parsed; caller fails closed."""


def parse_ready_spec(body: Optional[str]) -> Optional[dict]:
    """Extract + parse the single ``ready-spec`` YAML fence from a card body.

    Returns the parsed dict, or None when no fence is present (caller then applies
    all-defaults so only ``scope`` can fail). Raises ReadySpecParseError on a present-
    but-malformed fence so the caller fails CLOSED — never a half-parsed false PASS.
    """
    if not body:
        return None
    m = _FENCE_RE.search(body)
    if not m:
        return None
    raw = m.group(1)
    if not _HAVE_YAML:
        raise ReadySpecParseError("pyyaml-unavailable")
    try:
        data = yaml.safe_load(raw)
    except Exception as e:  # noqa: BLE001 - any YAML error fails closed
        raise ReadySpecParseError(f"yaml: {e}") from e
    if data is None:
        raise ReadySpecParseError("empty-ready-spec-block")
    if not isinstance(data, dict):
        raise ReadySpecParseError("ready-spec-not-a-mapping")
    return data


def _canon(p: str) -> str:
    """Canonicalize a path for comparison: expand ~, resolve symlinks AND ``..``
    (os.realpath normalizes traversal even for non-existent tails). This is what makes
    ``/safe/path/../live`` and symlink escapes fail the under-root checks. (Hermes #3)"""
    return os.path.realpath(os.path.expanduser(str(p)))


def _is_safe_workspace(ws: str, policy: dict) -> bool:
    """A declared workspace is safe iff it is a known sandbox slug, or a CANONICAL path
    under an explicit safe root with no blast-radius denylist fragment / live-repo path."""
    if not ws or ws == "/":
        return False
    if ws in policy.get("safe_workspace_slugs", SAFE_WORKSPACE_SLUGS):
        return True
    norm = _canon(ws)
    repo_root = policy.get("repo_root")
    if repo_root and (norm == _canon(repo_root) or norm.startswith(_canon(repo_root) + os.sep)):
        return False
    if any(frag in norm for frag in _UNSAFE_WS_FRAGMENTS):
        return False
    safe_roots = policy.get("safe_path_roots") or [DEFAULT_AUDITS_PREFIX,
                                                   os.path.expanduser("~/.hermes/scratch")]
    return any(norm.startswith(_canon(r) + os.sep) or norm == _canon(r) for r in safe_roots)


def validate_ready_spec(task: dict, *, resolved_workspace: Optional[str] = None,
                        board_policy: Optional[dict] = None) -> ReadySpecResult:
    """Pure validator. NEVER raises: a top-level guard turns ANY unexpected internal
    error into a fail-CLOSED result (ok=False, code ``validator_internal_error``)."""
    try:
        return _validate_ready_spec_inner(
            task, resolved_workspace=resolved_workspace, board_policy=board_policy)
    except Exception as e:  # noqa: BLE001 - make the "never raises" contract TRUE
        return ReadySpecResult(
            ok=False, errors=[{"code": "validator_internal_error", "message": repr(e)}])


def _validate_ready_spec_inner(task: dict, *, resolved_workspace: Optional[str] = None,
                               board_policy: Optional[dict] = None) -> ReadySpecResult:
    """Validation body (the public ``validate_ready_spec`` wraps this and fails closed
    on any unexpected error).

    `task`: a card mapping with at least ``body``; optionally ``id``, ``metadata``,
            ``assignee``, ``workspace``, ``board``.
    `resolved_workspace`: the workspace dispatch actually resolved for this card.
    `board_policy`: optional context — audits_prefix, safe_path_roots, repo_root,
            known_profiles (set), default_verifier, hard_rails (default stop_gates).
    """
    policy = board_policy or {}
    audits_prefix = _canon(policy.get("audits_prefix", DEFAULT_AUDITS_PREFIX))
    resolved_ws = resolved_workspace or task.get("workspace") or "scratch"

    errors: list[dict] = []
    resolved: dict[str, Any] = {"resolved_workspace": resolved_ws}

    def err(code: str, message: str) -> None:
        errors.append({"code": code, "message": message})

    try:
        raw = parse_ready_spec(task.get("body"))
    except ReadySpecParseError as e:
        return ReadySpecResult(ok=False, resolved={"parse_error": str(e)},
                               errors=[{"code": "ready_spec_parse_error", "message": str(e)}])
    except Exception as e:  # noqa: BLE001 - belt-and-suspenders: anything => fail closed
        return ReadySpecResult(ok=False,
                               errors=[{"code": "ready_spec_parse_error", "message": repr(e)}])

    spec = raw or {}

    # 1) scope — REQUIRED, presence-only (verifier is the semantic backstop).
    scope = spec.get("scope")
    if not (isinstance(scope, str) and scope.strip()):
        err("missing_scope", "ready-spec must declare a non-empty 'scope'")
    else:
        resolved["scope"] = scope.strip()

    # 2) allowed_workspace — must EQUAL the dispatcher's RESOLVED workspace AND be sandbox-safe
    #    (compare against what dispatch actually resolved, never the raw card text).
    allowed_ws = spec.get("allowed_workspace", resolved_ws)
    resolved["allowed_workspace"] = allowed_ws
    # Equal as raw strings (slugs like 'scratch') OR as canonical paths (tilde/abs forms).
    if str(allowed_ws) != str(resolved_ws) and _canon(allowed_ws) != _canon(resolved_ws):
        err("allowed_workspace_mismatch",
            f"declared allowed_workspace {allowed_ws!r} != resolved {resolved_ws!r}")
    elif not _is_safe_workspace(str(allowed_ws), policy):
        err("unsafe_allowed_workspace",
            f"allowed_workspace {allowed_ws!r} is not a sandbox/scratch root")

    # 3) evidence_path — must resolve STRICTLY under ~/.hermes/audits/ (task-scoped; never the
    #    bare audits root, /tmp, or repo-local).
    board = task.get("board", "")
    tid = task.get("id", "")
    ev = spec.get("evidence_path", os.path.join(audits_prefix, str(board), str(tid)) + "/")
    resolved["evidence_path"] = ev
    ev_norm = _canon(ev)
    if not ev_norm.startswith(audits_prefix + os.sep):
        err("unsafe_evidence_path", f"evidence_path {ev!r} is not strictly under {audits_prefix}/")

    # 4) stop_gates — default = inherited board hard rails; each must be in the vocab.
    gates = spec.get("stop_gates", policy.get("hard_rails", DEFAULT_STOP_GATES))
    if isinstance(gates, str):
        gates = [g.strip() for g in gates.split(",") if g.strip()]
    if not isinstance(gates, list):
        err("stop_gates_not_a_list", f"stop_gates must be a list, got {type(gates).__name__}")
        gates = []
    bad = [g for g in gates if g not in STOP_GATE_VOCAB]
    if bad:
        err("stop_gates_unknown", f"unknown stop_gates {bad}; vocab={sorted(STOP_GATE_VOCAB)}")
    resolved["stop_gates"] = gates

    # 5) verifier — default = reviewer profile; must resolve against REAL profiles.
    #    Accepts either a set (known_profiles, pure) or a callable (verifier_resolver,
    #    e.g. profiles.profile_exists) so dispatch checks live profiles, not a fake set.
    verifier = spec.get("verifier", policy.get("default_verifier", "h2reviewer"))
    resolved["verifier"] = verifier
    resolver = policy.get("verifier_resolver")
    known = policy.get("known_profiles")
    if not (isinstance(verifier, str) and verifier.strip()):
        err("verifier_empty", "verifier must be a non-empty profile/skill id")
    elif resolver is not None:
        # FAIL CLOSED: a resolver crash (e.g. DB locked) means we cannot CONFIRM the
        # verifier — the trust compiler must then refuse, not silently pass.
        try:
            resolved_ok = bool(resolver(verifier))
        except Exception:  # noqa: BLE001
            resolved_ok = False
        if not resolved_ok:
            err("verifier_unresolved", f"verifier {verifier!r} could not be resolved")
    elif known is not None and verifier not in known:
        err("verifier_unresolved", f"verifier {verifier!r} does not resolve to a known profile")

    resolved["has_ready_spec_block"] = raw is not None
    return ReadySpecResult(ok=not errors, errors=errors, resolved=resolved)


# --- offline selftest -------------------------------------------------------
def _selftest() -> int:
    fails = []

    def check(name, cond):
        print(f"  {'PASS' if cond else 'FAIL'} {name}")
        if not cond:
            fails.append(name)

    policy = {"repo_root": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
              "known_profiles": {"h2reviewer", "h2librarian", "h2coder"}}

    def card(body, **kw):
        return {"id": kw.get("id", "t_test"), "board": kw.get("board", "hermes"), "body": body}

    good = """blah blah
```ready-spec
scope: prove the canary dispatches
allowed_workspace: scratch
evidence_path: ~/.hermes/audits/kanban-ready-spec-canary/t1/
stop_gates: [config, auth, git-push]
verifier: h2reviewer
```
trailing text"""
    r = validate_ready_spec(card(good), resolved_workspace="scratch", board_policy=policy)
    check("well-formed -> PASS", r.ok)

    r = validate_ready_spec(card("no block here"), resolved_workspace="scratch", board_policy=policy)
    check("absent block -> FAIL (missing scope)", (not r.ok) and "missing_scope" in r.codes)

    miss_scope = "```ready-spec\nallowed_workspace: scratch\n```"
    r = validate_ready_spec(card(miss_scope), resolved_workspace="scratch", board_policy=policy)
    check("missing scope -> FAIL", (not r.ok) and "missing_scope" in r.codes)

    bad_ev = """```ready-spec
scope: x
allowed_workspace: scratch
evidence_path: /etc/passwd
```"""
    r = validate_ready_spec(card(bad_ev), resolved_workspace="scratch", board_policy=policy)
    check("evidence outside audits -> FAIL", (not r.ok) and "unsafe_evidence_path" in r.codes)

    bare_audits = """```ready-spec
scope: x
allowed_workspace: scratch
evidence_path: ~/.hermes/audits/
```"""
    r = validate_ready_spec(card(bare_audits), resolved_workspace="scratch", board_policy=policy)
    check("bare audits root (no task isolation) -> FAIL", (not r.ok) and "unsafe_evidence_path" in r.codes)

    traversal = """```ready-spec
scope: x
allowed_workspace: scratch
evidence_path: ~/.hermes/audits/../../etc/pwned/
```"""
    r = validate_ready_spec(card(traversal), resolved_workspace="scratch", board_policy=policy)
    check("`..` traversal escape -> FAIL (canonicalized)", (not r.ok) and "unsafe_evidence_path" in r.codes)

    repo_ws = """```ready-spec
scope: x
allowed_workspace: /home/josep/.local/share/hermes-agent
```"""
    r = validate_ready_spec(card(repo_ws), resolved_workspace="/home/josep/.local/share/hermes-agent",
                            board_policy=policy)
    check("live-repo workspace -> FAIL (unsafe)", (not r.ok) and "unsafe_allowed_workspace" in r.codes)

    ws_mismatch = """```ready-spec
scope: x
allowed_workspace: scratch
```"""
    r = validate_ready_spec(card(ws_mismatch), resolved_workspace="some-other-ws", board_policy=policy)
    check("declared != resolved workspace -> FAIL", (not r.ok) and "allowed_workspace_mismatch" in r.codes)

    broken = "```ready-spec\nscope: [unclosed\n  bad: : :\n```"
    r = validate_ready_spec(card(broken), resolved_workspace="scratch", board_policy=policy)
    check("broken fence -> FAIL CLOSED", (not r.ok) and "ready_spec_parse_error" in r.codes)

    bad_gate = """```ready-spec
scope: x
allowed_workspace: scratch
stop_gates: [config, NUKE_PROD]
```"""
    r = validate_ready_spec(card(bad_gate), resolved_workspace="scratch", board_policy=policy)
    check("unknown stop_gate -> FAIL", (not r.ok) and "stop_gates_unknown" in r.codes)

    unk_verifier = """```ready-spec
scope: x
allowed_workspace: scratch
verifier: nobody_profile
```"""
    r = validate_ready_spec(card(unk_verifier), resolved_workspace="scratch", board_policy=policy)
    check("unresolved verifier -> FAIL", (not r.ok) and "verifier_unresolved" in r.codes)

    def _boom(_):
        raise RuntimeError("db locked")
    crash_card = "```ready-spec\nscope: x\nallowed_workspace: scratch\nverifier: someone\n```"
    r = validate_ready_spec(card(crash_card), resolved_workspace="scratch",
                            board_policy={"verifier_resolver": _boom})
    check("verifier resolver CRASH -> FAIL CLOSED", (not r.ok) and "verifier_unresolved" in r.codes)

    tilde_ws = """```ready-spec
scope: x
allowed_workspace: ~/.hermes/scratch/wt1
evidence_path: ~/.hermes/audits/b/t/
```"""
    r = validate_ready_spec(card(tilde_ws), resolved_workspace=os.path.expanduser("~/.hermes/scratch/wt1"),
                            board_policy=policy)
    check("tilde vs abs workspace -> canonical match (no false mismatch)",
          "allowed_workspace_mismatch" not in r.codes)

    # defaults: a card with only scope + matching workspace passes (rest default-safe)
    minimal = """```ready-spec
scope: minimal card, defaults fill the rest
```"""
    r = validate_ready_spec(card(minimal), resolved_workspace="scratch", board_policy=policy)
    check("scope-only + safe defaults -> PASS", r.ok)

    print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
