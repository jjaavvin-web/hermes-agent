"""Auto-generate short session titles from the first user/assistant exchange.

Runs asynchronously after the first response is delivered so it never
adds latency to the user-facing reply.
"""

import atexit
import logging
import threading
import time
from typing import Callable, Optional

from agent.auxiliary_client import call_llm

logger = logging.getLogger(__name__)

# Callback signature: (task_name, exception) -> None. Used to surface
# auxiliary failures to the user through AIAgent._emit_auxiliary_failure
# so silent-drops (e.g. OpenRouter 402 exhausting the fallback chain)
# become visible instead of piling up as NULL session titles.
FailureCallback = Callable[[str, BaseException], None]
TitleCallback = Callable[[str], None]

# Background title generation must stay auxiliary: it should never block the
# user-facing answer, and it must not leave live network I/O racing Python
# interpreter teardown in one-shot CLI processes.  Keep the background request
# short, then drain active workers briefly at process exit.
_BACKGROUND_TITLE_TIMEOUT = 3.0
_SHUTDOWN_JOIN_TIMEOUT = 3.5
_active_title_threads: set[threading.Thread] = set()
_active_title_threads_lock = threading.Lock()
_shutdown_registered = False

_TITLE_PROMPT = (
    "Generate a short, descriptive title (3-7 words) for a conversation that starts with the "
    "following exchange. The title should capture the main topic or intent. "
    "Write the title in the same language the user is writing in. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the end, no prefixes."
)

_TITLE_PROMPT_PINNED_LANGUAGE = (
    "Generate a short, descriptive title (3-7 words) for a conversation that starts with the "
    "following exchange. The title should capture the main topic or intent. "
    "Write the title in {language}. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the end, no prefixes."
)


def _title_language() -> str:
    """Return configured title language, or empty string to match the user."""
    try:
        from hermes_cli.config import load_config

        return str(
            ((load_config() or {}).get("auxiliary") or {})
            .get("title_generation", {})
            .get("language", "")
        ).strip()
    except Exception:
        return ""


def _register_shutdown_hook() -> None:
    global _shutdown_registered
    if _shutdown_registered:
        return
    with _active_title_threads_lock:
        if _shutdown_registered:
            return
        atexit.register(_drain_title_threads_at_shutdown)
        _shutdown_registered = True


def _track_title_thread(thread: threading.Thread) -> None:
    with _active_title_threads_lock:
        _active_title_threads.add(thread)


def _untrack_title_thread(thread: threading.Thread) -> None:
    with _active_title_threads_lock:
        _active_title_threads.discard(thread)


def _drain_title_threads_at_shutdown(timeout: float = _SHUTDOWN_JOIN_TIMEOUT) -> None:
    """Bounded drain for auxiliary title threads during interpreter shutdown.

    The title worker is best-effort.  Joining briefly lets normal short
    auxiliary failures finish and report their warning before process exit, but
    a stuck client must not hold one-shot CLI teardown open indefinitely.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        with _active_title_threads_lock:
            threads = [thread for thread in _active_title_threads if thread.is_alive()]
        if not threads:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.debug(
                "Auto-title shutdown drain timed out with %d worker(s) still active",
                len(threads),
            )
            return
        per_thread = max(0.0, remaining / len(threads))
        for thread in threads:
            thread.join(timeout=per_thread)


def _run_auto_title_thread(*args, **kwargs) -> None:
    try:
        auto_title_session(*args, **kwargs)
    finally:
        _untrack_title_thread(threading.current_thread())


def generate_title(
    user_message: str,
    assistant_response: str,
    timeout: float = 30.0,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
) -> Optional[str]:
    """Generate a session title from the first exchange.

    Uses the main runtime's model when available, falling back to the
    auxiliary LLM client (cheapest/fastest available model).
    Returns the title string or None on failure.

    ``failure_callback`` is invoked with ``(task, exception)`` when the
    auxiliary call raises — the caller typically wires this to
    ``AIAgent._emit_auxiliary_failure`` so the user sees a warning instead
    of silently accumulating untitled sessions.
    """
    # Truncate long messages to keep the request small
    user_snippet = user_message[:500] if user_message else ""
    assistant_snippet = assistant_response[:500] if assistant_response else ""

    language = _title_language()
    prompt = _TITLE_PROMPT_PINNED_LANGUAGE.format(language=language) if language else _TITLE_PROMPT

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"User: {user_snippet}\n\nAssistant: {assistant_snippet}"},
    ]

    try:
        response = call_llm(
            task="title_generation",
            messages=messages,
            max_tokens=500,
            temperature=0.3,
            timeout=timeout,
            main_runtime=main_runtime,
        )
        title = (response.choices[0].message.content or "").strip()
        # Clean up: remove quotes, trailing punctuation, prefixes like "Title: "
        title = title.strip('"\'')
        if title.lower().startswith("title:"):
            title = title[6:].strip()
        # Enforce reasonable length
        if len(title) > 80:
            title = title[:77] + "..."
        return title if title else None
    except Exception as e:
        # Log at WARNING so this shows up in agent.log without debug mode.
        # Full detail at debug level for operators who need the stack.
        logger.warning("Title generation failed: %s", e)
        logger.debug("Title generation traceback", exc_info=True)
        if failure_callback is not None:
            try:
                failure_callback("title generation", e)
            except Exception:
                logger.debug("Title generation failure_callback raised", exc_info=True)
        return None


def auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    assistant_response: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    generation_timeout: Optional[float] = None,
) -> None:
    """Generate and set a session title if one doesn't already exist.

    Called in a background thread after the first exchange completes.
    Silently skips if:
    - session_db is None
    - session already has a title (user-set or previously auto-generated)
    - title generation fails
    """
    if not session_db or not session_id:
        return

    # Check if title already exists (user may have set one via /title before first response)
    try:
        existing = session_db.get_session_title(session_id)
        if existing:
            return
    except Exception:
        return

    title = generate_title(
        user_message,
        assistant_response,
        timeout=generation_timeout if generation_timeout is not None else 30.0,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
    )
    if not title:
        return

    try:
        session_db.set_session_title(session_id, title)
        logger.debug("Auto-generated session title: %s", title)
        if title_callback is not None:
            try:
                title_callback(title)
            except Exception:
                logger.debug("Auto-title callback failed", exc_info=True)
    except Exception as e:
        logger.debug("Failed to set auto-generated title: %s", e)


def maybe_auto_title(
    session_db,
    session_id: str,
    user_message: str,
    assistant_response: str,
    conversation_history: list,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    generation_timeout: Optional[float] = None,
) -> None:
    """Fire-and-forget title generation after the first exchange.

    Only generates a title when:
    - This appears to be the first user→assistant exchange
    - No title is already set
    """
    if not session_db or not session_id or not user_message or not assistant_response:
        return

    # Count user messages in history to detect first exchange.
    # conversation_history includes the exchange that just happened,
    # so for a first exchange we expect exactly 1 user message
    # (or 2 counting system). Be generous: generate on first 2 exchanges.
    user_msg_count = sum(1 for m in (conversation_history or []) if m.get("role") == "user")
    if user_msg_count > 2:
        return

    _register_shutdown_hook()
    thread = threading.Thread(
        target=_run_auto_title_thread,
        args=(session_db, session_id, user_message, assistant_response),
        kwargs={
            "failure_callback": failure_callback,
            "main_runtime": main_runtime,
            "title_callback": title_callback,
            "generation_timeout": generation_timeout or _BACKGROUND_TITLE_TIMEOUT,
        },
        daemon=True,
        name="auto-title",
    )
    _track_title_thread(thread)
    thread.start()
