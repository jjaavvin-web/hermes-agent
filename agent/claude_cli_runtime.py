"""Claude CLI subprocess runtime — delegate a turn to local Claude Code.

This is the turn executor for the ``claude-cli-subprocess`` provider
(api_mode ``claude_cli_subprocess``).  It hands an entire conversation turn
to the locally installed ``claude`` CLI (Claude Code) running in an
interactive tmux session backed by the user's claude.ai OAuth / Max plan
login.

Why this exists
---------------
The native Anthropic provider bills against Anthropic's metered API. Claude
Code's normal interactive CLI authenticates against Claude Code's own
first-party OAuth credential store tied to a claude.ai subscription. This
runtime intentionally uses that interactive CLI path, not Claude Code print
mode and not any Anthropic HTTP/API-key route.

Hard guarantees
---------------
* Interactive/Max path only. ``build_claude_command()`` never emits ``-p`` /
  print mode arguments, and ``run_claude_cli_turn()`` launches Claude inside
  tmux as an interactive TUI session.
* Paid API env vars are stripped from the subprocess environment so an
  injected Anthropic API key/base URL cannot silently reroute the call.
* File handoff avoids screen-scraping the final answer: Hermes writes a turn
  packet, asks interactive Claude to read it and use the Write tool to create
  ``result.md``, then reads that file.
* Fail loud. Missing binaries, tmux launch failures, timeout, or an empty
  result surface as an explicit runtime error; there is no paid fallback.

Scope
-----
This path bypasses Hermes' own tool-dispatch loop entirely (same high-level
shape as ``codex_app_server``). Claude Code's own Read/Write tools are used
only for the handoff files unless the user's prompt asks Claude to do more in
its interactive runtime.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Anthropic auth env vars that MUST be removed from the ``claude`` subprocess
# environment. Stripping these forces Claude Code to authenticate with its own
# logged-in claude.ai credential store instead of an injected paid API token.
_BLOCKED_AUTH_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

_DEFAULT_TIMEOUT_SECONDS = 600
_DEFAULT_READY_TIMEOUT_SECONDS = 45

_SYNTHETIC_MODEL_NAMES = {
    "claude-via-cli",
    "claude-cli",
    "claude-cli-subprocess",
    "claude-code-cli",
}

_VALID_EFFORTS = {"low", "medium", "high", "max", "auto"}
_VALID_PERMISSION_MODES = {"default", "acceptEdits", "plan", "auto", "dontAsk"}
_VALID_WORKFLOW_MODES = {"off", "on_request", "always"}
_WORKFLOW_TRIGGER_TERMS = (
    "ultracode",
    "/workflows",
    "/deep-research",
    "deep research",
    "workflow",
    "workflows",
    "multi-agent",
    "multi agent",
    "agent team",
    "subagent",
    "subagents",
    "orchestration",
)


@dataclass(frozen=True)
class ClaudeCliOptions:
    """Config-backed options for the Max/OAuth Claude Code subprocess bridge."""

    allowed_tools: str = "Read,Write"
    effort: Optional[str] = None
    permission_mode: Optional[str] = None
    workflow_mode: str = "off"
    timeout_seconds: Optional[int] = None
    ready_timeout_seconds: int = _DEFAULT_READY_TIMEOUT_SECONDS


class ClaudeCliError(RuntimeError):
    """Raised when the interactive Claude CLI cannot produce a usable turn."""

    def __init__(self, message: str, *, returncode: Optional[int] = None, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Environment + binaries
# ---------------------------------------------------------------------------


def _claude_subprocess_env() -> Dict[str, str]:
    """Return an env dict for Claude Code with paid API env vars stripped."""
    env = dict(os.environ)
    for name in _BLOCKED_AUTH_ENV:
        env.pop(name, None)
    return env


def _find_binary(name: str, env_var: str) -> str:
    override = os.environ.get(env_var, "").strip()
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        raise ClaudeCliError(f"{env_var} points at {override!r}, which is not an executable file.")
    path = shutil.which(name)
    if path:
        return path
    raise ClaudeCliError(f"Required binary {name!r} was not found on PATH; set {env_var} to the executable path.")


def _find_claude_binary() -> str:
    """Return the executable path for Claude Code."""
    try:
        return _find_binary("claude", "HERMES_CLAUDE_CLI_PATH")
    except ClaudeCliError as exc:
        raise ClaudeCliError(
            "The 'claude' CLI (Claude Code) was not found on PATH. The "
            "claude-cli-subprocess provider delegates turns to Claude Code "
            "running on your claude.ai OAuth session. Install Claude Code and "
            "run 'claude /login', or set HERMES_CLAUDE_CLI_PATH."
        ) from exc


def _find_tmux_binary() -> str:
    """Return the executable path for tmux."""
    try:
        return _find_binary("tmux", "HERMES_TMUX_PATH")
    except ClaudeCliError as exc:
        raise ClaudeCliError(
            "tmux is required for the claude-cli-subprocess provider because "
            "Claude must run through an interactive TTY to stay on the Max/OAuth path. "
            "Install tmux or set HERMES_TMUX_PATH."
        ) from exc


def _coerce_positive_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_effort(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered not in _VALID_EFFORTS:
        raise ClaudeCliError(
            f"Unsupported claude_cli.effort={raw!r}; expected one of {sorted(_VALID_EFFORTS)}."
        )
    return lowered


def _normalize_permission_mode(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    canonical = {mode.lower(): mode for mode in _VALID_PERMISSION_MODES}
    lowered = raw.lower()
    if lowered not in canonical:
        raise ClaudeCliError(
            "Unsupported claude_cli.permission_mode="
            f"{raw!r}; expected one of {sorted(_VALID_PERMISSION_MODES)}. "
            "The subprocess bridge intentionally does not expose bypassPermissions "
            "because it would auto-approve shell/file/network actions beyond the safe workflow lane."
        )
    return canonical[lowered]


def _normalize_workflow_mode(value: Any) -> str:
    raw = str(value or "off").strip().lower().replace("-", "_")
    aliases = {
        "false": "off",
        "0": "off",
        "none": "off",
        "disabled": "off",
        "true": "always",
        "1": "always",
        "enabled": "always",
        "onrequest": "on_request",
        "on_request": "on_request",
        "request": "on_request",
        "auto": "on_request",
        "always": "always",
        "off": "off",
    }
    normalized = aliases.get(raw, raw)
    if normalized not in _VALID_WORKFLOW_MODES:
        raise ClaudeCliError(
            f"Unsupported claude_cli.workflow_mode={value!r}; expected one of {sorted(_VALID_WORKFLOW_MODES)}."
        )
    return normalized


def _read_claude_cli_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        return {}
    for section_name in ("claude_cli", "claude_cli_subprocess", "claude_code"):
        section = cfg.get(section_name) if isinstance(cfg, dict) else None
        if isinstance(section, dict):
            return section
    return {}


def _load_claude_cli_options() -> ClaudeCliOptions:
    """Load non-secret Claude Code bridge options from config/env."""
    cfg = _read_claude_cli_config()
    allowed_tools = str(
        os.environ.get("HERMES_CLAUDE_CLI_ALLOWED_TOOLS")
        or cfg.get("allowed_tools")
        or cfg.get("allowedTools")
        or "Read,Write"
    ).strip()
    effort = _normalize_effort(
        os.environ.get("HERMES_CLAUDE_CLI_EFFORT")
        or cfg.get("effort")
        or cfg.get("reasoning_effort")
    )
    permission_mode = _normalize_permission_mode(
        os.environ.get("HERMES_CLAUDE_CLI_PERMISSION_MODE")
        or cfg.get("permission_mode")
        or cfg.get("permissionMode")
    )
    workflow_mode = _normalize_workflow_mode(
        os.environ.get("HERMES_CLAUDE_CLI_WORKFLOW_MODE")
        or cfg.get("workflow_mode")
        or cfg.get("workflowMode")
    )
    timeout_seconds = _coerce_positive_int(
        os.environ.get("HERMES_CLAUDE_CLI_TIMEOUT")
        or cfg.get("timeout_seconds")
        or cfg.get("timeoutSeconds")
        or cfg.get("timeout")
    )
    ready_timeout_seconds = (
        _coerce_positive_int(
            os.environ.get("HERMES_CLAUDE_CLI_READY_TIMEOUT")
            or cfg.get("ready_timeout_seconds")
            or cfg.get("readyTimeoutSeconds")
            or cfg.get("ready_timeout")
        )
        or _DEFAULT_READY_TIMEOUT_SECONDS
    )
    return ClaudeCliOptions(
        allowed_tools=allowed_tools,
        effort=effort,
        permission_mode=permission_mode,
        workflow_mode=workflow_mode,
        timeout_seconds=timeout_seconds,
        ready_timeout_seconds=ready_timeout_seconds,
    )


def _resolve_timeout() -> int:
    raw = os.environ.get("HERMES_CLAUDE_CLI_TIMEOUT", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("Ignoring invalid HERMES_CLAUDE_CLI_TIMEOUT=%r", raw)
    return _DEFAULT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Prompt serialization
# ---------------------------------------------------------------------------


def _coerce_content_to_text(content: Any) -> str:
    """Reduce an OpenAI-shaped message ``content`` to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: List[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "text" and isinstance(part.get("text"), str):
                    pieces.append(part["text"])
                elif ptype in {"image_url", "input_audio", "image"}:
                    pieces.append(f"[{ptype} omitted: not supported on the claude-cli handoff]")
        return "\n".join(p for p in pieces if p)
    return str(content)


def _serialize_conversation(messages: List[Dict[str, Any]]) -> str:
    """Flatten the non-system conversation history into a text transcript."""
    convo: List[Dict[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        if role == "system":
            continue
        text = _coerce_content_to_text(msg.get("content"))
        if not text.strip():
            continue
        convo.append({"role": str(role), "text": text.strip()})

    if not convo:
        return ""
    if len(convo) == 1 and convo[0]["role"] == "user":
        return convo[0]["text"]

    label = {"user": "User", "assistant": "Assistant", "tool": "Tool result"}
    return "\n\n".join(
        f"{label.get(entry['role'], entry['role'].title())}: {entry['text']}"
        for entry in convo
    )


def _normalize_model_for_claude_cli(model: Optional[str]) -> Optional[str]:
    raw = (model or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in _SYNTHETIC_MODEL_NAMES:
        return None
    if "/" in raw:
        raw = raw.split("/", 1)[1]
    # Claude Code accepts aliases (sonnet/opus) and dashed model identifiers.
    # Provider-style names occasionally use dots (e.g. claude-sonnet-4.6);
    # normalize those to Claude CLI's dashed form.
    return raw.replace(".", "-")


def build_claude_command(
    claude_bin: str,
    *,
    model: Optional[str] = None,
    add_dirs: Optional[Sequence[str]] = None,
    allowed_tools: str = "Read,Write",
    system_prompt: Optional[str] = None,
    effort: Optional[str] = None,
    permission_mode: Optional[str] = None,
) -> List[str]:
    """Assemble the interactive ``claude`` argv.

    This deliberately does not include print-mode flags. The provider must use
    a real TTY-backed Claude Code session so it remains on Claude Code's
    Max/OAuth path.
    """
    cmd = [claude_bin]
    normalized_model = _normalize_model_for_claude_cli(model)
    if normalized_model:
        cmd.extend(["--model", normalized_model])
    normalized_effort = _normalize_effort(effort)
    if normalized_effort:
        cmd.extend(["--effort", normalized_effort])
    normalized_permission_mode = _normalize_permission_mode(permission_mode)
    if normalized_permission_mode:
        cmd.extend(["--permission-mode", normalized_permission_mode])
    if allowed_tools:
        cmd.extend(["--allowed-tools", allowed_tools])
    for directory in add_dirs or []:
        directory_str = str(directory).strip()
        if directory_str:
            cmd.extend(["--add-dir", directory_str])
    # Kept for API compatibility with the orphaned runtime, but intentionally
    # not passed as a command-line argument: large system prompts belong in the
    # turn handoff file to avoid tmux/shell command length edge cases.
    _ = system_prompt
    return cmd


def _render_turn_packet(*, system_prompt: Optional[str], transcript: str, user_message: Any) -> str:
    fallback_user = _coerce_content_to_text(user_message)
    body = transcript.strip() or fallback_user.strip()
    return (
        "# Hermes delegated Claude CLI turn\n\n"
        "You are answering a Hermes Agent turn via the interactive Claude Code CLI.\n"
        "Return the assistant response requested by the user.\n\n"
        "## Hermes system prompt\n\n"
        f"{(system_prompt or '').strip() or '[none]'}\n\n"
        "## Conversation / user request\n\n"
        f"{body}\n"
    )


def _cleanup_handoff_dir(path: Path) -> None:
    """Remove a turn handoff dir so prompt/result content doesn't linger in /tmp.

    Set HERMES_CLAUDE_CLI_RETAIN_HANDOFF=1 to keep dirs for debugging failed turns.
    """
    if os.getenv("HERMES_CLAUDE_CLI_RETAIN_HANDOFF"):
        return
    shutil.rmtree(path, ignore_errors=True)


def _workflow_requested(*, workflow_mode: str, transcript: str, user_message: Any) -> bool:
    mode = _normalize_workflow_mode(workflow_mode)
    if mode == "always":
        return True
    if mode != "on_request":
        return False
    haystack = "\n".join(
        part.lower()
        for part in (transcript, _coerce_content_to_text(user_message))
        if isinstance(part, str) and part
    )
    return any(term in haystack for term in _WORKFLOW_TRIGGER_TERMS)


def _render_invocation(*, turn_path: Path, result_path: Path, workflow_requested: bool) -> str:
    base = (
        f"Read the Hermes turn packet at {turn_path}. "
        f"Write only the final assistant response to {result_path} using the Write tool. "
        "Do not write analysis, metadata, markdown fences, or any extra commentary outside the requested answer."
    )
    if not workflow_requested:
        return base
    return (
        "ultracode: "
        + base
        + " Use Claude Code Dynamic Workflows / Ultracode natively for the requested "
        "workflow or multi-agent orchestration when useful, but still finish by writing "
        "only the final assistant response to the result file."
    )


def _redact_pane_for_error(text: str) -> str:
    if not text:
        return ""
    redacted = text
    for pattern in (
        r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+",
        r"sk-[A-Za-z0-9_-]{20,}",
        r"[A-Za-z0-9_=-]{32,}\.[A-Za-z0-9_=-]{16,}\.[A-Za-z0-9_=-]{16,}",
    ):
        redacted = re.sub(pattern, "[REDACTED]", redacted)
    lines = [line.rstrip() for line in redacted.splitlines() if line.strip()]
    return "\n".join(lines[-20:])


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------


def _tmux_run(tmux_bin: str, *args: str, env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [tmux_bin, *args],
        text=True,
        capture_output=True,
        env=env,
        cwd=cwd,
    )


def _start_tmux_session(tmux_bin: str, session_id: str, command: Sequence[str], *, env: Dict[str, str], cwd: str) -> None:
    _tmux_run(tmux_bin, "kill-session", "-t", session_id, env=env, cwd=cwd)
    proc = _tmux_run(
        tmux_bin,
        "new-session",
        "-d",
        "-s",
        session_id,
        "-c",
        cwd,
        *command,
        env=env,
        cwd=cwd,
    )
    if proc.returncode != 0:
        raise ClaudeCliError(
            f"tmux new-session failed for {session_id}: {(proc.stderr or proc.stdout or '').strip()}",
            returncode=proc.returncode,
            stderr=proc.stderr or "",
        )


def _kill_tmux_session(tmux_bin: str, session_id: str) -> None:
    try:
        _tmux_run(tmux_bin, "kill-session", "-t", session_id)
    except Exception:
        logger.debug("claude-cli-subprocess: failed to kill tmux session %s", session_id, exc_info=True)


def _capture_tmux_pane(tmux_bin: str, session_id: str) -> str:
    proc = _tmux_run(tmux_bin, "capture-pane", "-p", "-t", session_id)
    return proc.stdout or ""


def _send_tmux_text(tmux_bin: str, session_id: str, text: str) -> None:
    send = _tmux_run(tmux_bin, "send-keys", "-t", session_id, text)
    if send.returncode != 0:
        raise ClaudeCliError(f"tmux send-keys failed: {(send.stderr or send.stdout or '').strip()}")
    time.sleep(0.5)
    enter = _tmux_run(tmux_bin, "send-keys", "-t", session_id, "Enter")
    if enter.returncode != 0:
        raise ClaudeCliError(f"tmux send Enter failed: {(enter.stderr or enter.stdout or '').strip()}")


def _wait_for_claude_ready(tmux_bin: str, session_id: str, *, timeout: int = _DEFAULT_READY_TIMEOUT_SECONDS) -> None:
    """Wait until the interactive Claude TUI appears, clearing simple trust prompts."""
    deadline = time.monotonic() + max(1, timeout)
    saw_claude = False
    while time.monotonic() < deadline:
        has = _tmux_run(tmux_bin, "has-session", "-t", session_id)
        if has.returncode != 0:
            raise ClaudeCliError(f"Claude tmux session {session_id} died before it was ready.")
        pane = _capture_tmux_pane(tmux_bin, session_id)
        if "Enter to confirm" in pane or "Press Enter" in pane:
            _tmux_run(tmux_bin, "send-keys", "-t", session_id, "Enter")
            time.sleep(1)
            continue
        if "Claude Code" in pane or "Claude Max" in pane or "? for shortcuts" in pane:
            saw_claude = True
        if saw_claude:
            # Give the TUI a short settle window so the subsequent prompt lands
            # in the input box rather than during startup repaint.
            time.sleep(1)
            return
        time.sleep(1)
    # Some themes clear the splash quickly; do not fail if the tmux pane is
    # still alive. Sending a prompt is the real readiness probe.
    logger.warning("claude-cli-subprocess: did not see Claude ready banner before timeout; proceeding")


def _wait_for_result_file(
    result_path: Path,
    *,
    timeout: int,
    tmux_bin: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    deadline = time.monotonic() + max(1, timeout)
    last_size = -1
    stable_since: Optional[float] = None
    last_pane = ""
    while time.monotonic() < deadline:
        if tmux_bin and session_id:
            try:
                last_pane = _capture_tmux_pane(tmux_bin, session_id)
            except Exception:
                pass
        if result_path.exists():
            try:
                size = result_path.stat().st_size
                if size > 0:
                    if size == last_size:
                        if stable_since is not None and time.monotonic() - stable_since >= 0.5:
                            text = result_path.read_text(encoding="utf-8", errors="replace").strip()
                            if text:
                                return text
                    else:
                        last_size = size
                        stable_since = time.monotonic()
            except OSError:
                pass
        time.sleep(0.5)
    detail = ""
    pane_tail = _redact_pane_for_error(last_pane)
    if pane_tail:
        detail = f" Last Claude pane tail (redacted):\n{pane_tail}"
    raise ClaudeCliError(
        f"Claude did not write a non-empty result file at {result_path} within {timeout}s."
        + detail
    )


# ---------------------------------------------------------------------------
# Turn executor
# ---------------------------------------------------------------------------


def run_claude_cli_turn(
    agent,
    *,
    user_message: str,
    original_user_message: Any,
    messages: List[Dict[str, Any]],
    effective_task_id: str,
    should_review_memory: bool = False,
) -> Dict[str, Any]:
    """Run one Hermes turn through an interactive Claude Code subprocess."""
    system_prompt = getattr(agent, "_cached_system_prompt", None) or None
    transcript = _serialize_conversation(messages)
    cwd = getattr(agent, "session_cwd", None) or os.getcwd()
    options = _load_claude_cli_options()
    timeout = options.timeout_seconds or _resolve_timeout()

    handoff_dir = Path(tempfile.mkdtemp(prefix="hermes-claude-cli-"))
    handoff_dir.mkdir(parents=True, exist_ok=True)
    turn_path = handoff_dir / "turn.md"
    result_path = handoff_dir / "result.md"
    turn_path.write_text(
        _render_turn_packet(
            system_prompt=system_prompt,
            transcript=transcript,
            user_message=user_message,
        ),
        encoding="utf-8",
    )

    try:
        claude_bin = _find_claude_binary()
        tmux_bin = _find_tmux_binary()
        session_id = f"hermes-claude-cli-{uuid.uuid4().hex[:12]}"
        cmd = build_claude_command(
            claude_bin,
            model=getattr(agent, "model", None),
            add_dirs=[str(handoff_dir)],
            allowed_tools=options.allowed_tools,
            system_prompt=system_prompt,
            effort=options.effort,
            permission_mode=options.permission_mode,
        )
        env = _claude_subprocess_env()

        logger.info(
            "claude-cli-subprocess: delegating turn through interactive Claude CLI (session=%s, cwd=%s, handoff=%s)",
            session_id,
            cwd,
            handoff_dir,
        )
        _start_tmux_session(tmux_bin, session_id, cmd, env=env, cwd=cwd)
        try:
            _wait_for_claude_ready(
                tmux_bin,
                session_id,
                timeout=options.ready_timeout_seconds,
            )
            invocation = _render_invocation(
                turn_path=turn_path,
                result_path=result_path,
                workflow_requested=_workflow_requested(
                    workflow_mode=options.workflow_mode,
                    transcript=transcript,
                    user_message=user_message,
                ),
            )
            _send_tmux_text(tmux_bin, session_id, invocation)
            final_text = _wait_for_result_file(
                result_path,
                timeout=timeout,
                tmux_bin=tmux_bin,
                session_id=session_id,
            )
        finally:
            _kill_tmux_session(tmux_bin, session_id)
            _cleanup_handoff_dir(handoff_dir)
    except ClaudeCliError as exc:
        logger.error("claude-cli-subprocess: %s", exc)
        return _error_turn(messages, str(exc))
    except OSError as exc:
        msg = f"Claude CLI interactive handoff failed: {exc}"
        logger.error("claude-cli-subprocess: %s", msg)
        return _error_turn(messages, msg)

    assistant_msg = {"role": "assistant", "content": final_text}
    messages.append(assistant_msg)

    try:
        agent._sync_external_memory_for_turn(
            original_user_message=original_user_message,
            final_response=final_text,
            interrupted=False,
        )
    except Exception:
        logger.debug("claude-cli-subprocess: external memory sync raised", exc_info=True)

    if final_text and should_review_memory:
        try:
            agent._spawn_background_review(
                messages_snapshot=list(messages),
                review_memory=True,
                review_skills=False,
            )
        except Exception:
            logger.debug("claude-cli-subprocess: background review spawn raised", exc_info=True)

    return {
        "final_response": final_text,
        "messages": messages,
        "api_calls": 1,
        "completed": True,
        "partial": False,
        "error": None,
    }


def run_claude_oneshot(
    prompt: str,
    *,
    model: Optional[str] = None,
    system_prompt: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    effort: Optional[str] = None,
    permission_mode: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    workflow_mode: Optional[str] = None,
) -> str:
    """Run a single headless Claude turn on the Max/OAuth plan and return its text.

    A one-shot convenience wrapper around the same interactive-tmux mechanism the
    ``claude-cli-subprocess`` provider uses for agent turns — intended as the
    Max-preserving replacement for standalone scripts that currently shell out to
    ``claude -p`` (which moves to a metered API pool after the 2026-06-15 billing
    split). Drives the *interactive* Claude CLI through a tmux TTY with paid-API
    env vars stripped, so it can only authenticate via the user's claude.ai
    OAuth / Max session. Returns the assistant's final text verbatim (the caller
    parses any JSON itself). Raises ``ClaudeCliError`` on failure.
    """
    options = _load_claude_cli_options()
    resolved_timeout = (
        timeout if timeout is not None else options.timeout_seconds or _resolve_timeout()
    )
    work_cwd = cwd or os.getcwd()
    effective_effort = effort if effort is not None else options.effort
    effective_permission_mode = (
        permission_mode if permission_mode is not None else options.permission_mode
    )
    effective_allowed_tools = allowed_tools if allowed_tools is not None else options.allowed_tools
    effective_workflow_mode = workflow_mode if workflow_mode is not None else options.workflow_mode

    handoff_dir = Path(tempfile.mkdtemp(prefix="hermes-claude-oneshot-"))
    handoff_dir.mkdir(parents=True, exist_ok=True)
    turn_path = handoff_dir / "turn.md"
    result_path = handoff_dir / "result.md"
    if system_prompt:
        # build_claude_command intentionally never puts the system prompt on
        # the argv; the handoff file is its only route to the model.
        turn_path.write_text(
            _render_turn_packet(
                system_prompt=system_prompt,
                transcript="",
                user_message=prompt,
            ),
            encoding="utf-8",
        )
    else:
        turn_path.write_text(prompt, encoding="utf-8")

    claude_bin = _find_claude_binary()
    tmux_bin = _find_tmux_binary()
    session_id = f"hermes-claude-oneshot-{uuid.uuid4().hex[:12]}"
    cmd = build_claude_command(
        claude_bin,
        model=model,
        add_dirs=[str(handoff_dir)],
        allowed_tools=effective_allowed_tools,
        system_prompt=system_prompt,
        effort=effective_effort,
        permission_mode=effective_permission_mode,
    )
    env = _claude_subprocess_env()

    logger.info(
        "claude-cli-subprocess oneshot: session=%s cwd=%s handoff=%s",
        session_id,
        work_cwd,
        handoff_dir,
    )
    _start_tmux_session(tmux_bin, session_id, cmd, env=env, cwd=work_cwd)
    try:
        _wait_for_claude_ready(
            tmux_bin,
            session_id,
            timeout=options.ready_timeout_seconds,
        )
        invocation = _render_invocation(
            turn_path=turn_path,
            result_path=result_path,
            workflow_requested=_workflow_requested(
                workflow_mode=effective_workflow_mode,
                transcript=prompt,
                user_message=prompt,
            ),
        )
        _send_tmux_text(tmux_bin, session_id, invocation)
        return _wait_for_result_file(
            result_path,
            timeout=resolved_timeout,
            tmux_bin=tmux_bin,
            session_id=session_id,
        )
    finally:
        _kill_tmux_session(tmux_bin, session_id)
        _cleanup_handoff_dir(handoff_dir)


def _error_turn(messages: List[Dict[str, Any]], error: str) -> Dict[str, Any]:
    return {
        "final_response": f"claude-cli-subprocess runtime error: {error}",
        "messages": messages,
        "api_calls": 0,
        "completed": False,
        "partial": True,
        "error": error,
    }


__all__ = [
    "ClaudeCliError",
    "ClaudeCliOptions",
    "build_claude_command",
    "run_claude_cli_turn",
    "run_claude_oneshot",
]
