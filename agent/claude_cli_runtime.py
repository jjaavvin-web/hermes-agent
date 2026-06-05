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
import shutil
import subprocess
import tempfile
import time
import uuid
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


def _wait_for_result_file(result_path: Path, *, timeout: int) -> str:
    deadline = time.monotonic() + max(1, timeout)
    last_size = -1
    stable_since: Optional[float] = None
    while time.monotonic() < deadline:
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
    raise ClaudeCliError(f"Claude did not write a non-empty result file at {result_path} within {timeout}s.")


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
    timeout = _resolve_timeout()

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
            system_prompt=system_prompt,
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
            _wait_for_claude_ready(tmux_bin, session_id)
            invocation = (
                f"Read the Hermes turn packet at {turn_path}. "
                f"Write only the final assistant response to {result_path} using the Write tool. "
                "Do not write analysis, metadata, markdown fences, or any extra commentary outside the requested answer."
            )
            _send_tmux_text(tmux_bin, session_id, invocation)
            final_text = _wait_for_result_file(result_path, timeout=timeout)
        finally:
            _kill_tmux_session(tmux_bin, session_id)
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
    "build_claude_command",
    "run_claude_cli_turn",
]
