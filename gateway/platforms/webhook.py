"""Generic webhook platform adapter.

Runs an aiohttp HTTP server that receives webhook POSTs from external
services (GitHub, GitLab, JIRA, Stripe, etc.), validates HMAC signatures,
transforms payloads into agent prompts, and routes responses back to the
source or to another configured platform.

Configuration lives in config.yaml under platforms.webhook.extra.routes.
Each route defines:
  - events: which event types to accept (header-based filtering)
  - secret: HMAC secret for signature validation (REQUIRED)
  - prompt: template string formatted with the webhook payload
  - skills: optional list of skills to load for the agent
  - deliver: where to send the response (github_comment, telegram, etc.)
  - deliver_extra: additional delivery config (repo, pr_number, chat_id)
  - deliver_only: if true, skip the agent — the rendered prompt IS the
    message that gets delivered.  Use for external push notifications
    (Supabase, monitoring alerts, inter-agent pings) where zero LLM cost
    and sub-second delivery matter more than agent reasoning.

Security:
  - HMAC secret is required per route (validated at startup)
  - Rate limiting per route (fixed-window, configurable)
  - Idempotency cache prevents duplicate agent runs on webhook retries
  - Body size limits checked before reading payload
  - Generic HMAC supports a V2 signature (X-Webhook-Signature-V2) that
    binds a timestamp into the signed data for replay protection; the
    legacy body-only V1 (X-Webhook-Signature) is deprecated but still
    accepted with a warning, since it has no replay protection
  - Set secret to "INSECURE_NO_AUTH" to skip validation (testing only)
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.platforms.webhook_filters import (
    DEFAULT_SCRIPT_TIMEOUT_SECONDS,
    WebhookRouteProcessor,
)
from gateway.response_filters import is_autonomous_silence_response
from hermes_cli.active_sessions import resolve_max_concurrent_agent_runs
from tools.approval import CREDENTIAL_EXFIL_DENY_PATTERNS

logger = logging.getLogger(__name__)


def _is_webhook_silence_response(content: Any) -> bool:
    """Whether an agent response means "deliberately say nothing".

    Webhook routes are autonomous background lanes: a subscription prompt tells
    the agent to answer with ``[SILENT]`` when a tick produced nothing worth a
    human's attention (a duplicate inbound, a stand-down because a sibling lane
    already replied, a routine close).  Nobody is waiting on the other end, so
    there is no reader for whom a "nothing happened" message is useful.

    The reason this is the loose autonomous rule rather than the live gateway's
    is what the two lanes optimise for.  In an interactive chat, swallowing a
    real answer because it happens to open with a marker is much worse than
    showing a stray marker, so ``is_intentional_silence_response`` demands the
    response be EXACTLY a marker.  A webhook run has the opposite payoff: the
    cost of a leaked non-story is a pointless notification on every tick, and
    models reliably add a sentence explaining why they stayed quiet — which
    under the strict rule flips the whole thing back to "deliver".  That is not
    a hypothetical: it is why a Helper support lane kept messaging its owner to
    report that it had nothing to report.

    So use the shared autonomous-lane matcher (also used by cron), which treats
    a marker on its own first or last line as silence while still delivering
    prose that merely mentions one mid-sentence.  Sharing the function keeps
    the two autonomous lanes from drifting apart, and keeps the interactive
    path untouched.
    """
    return is_autonomous_silence_response(content)

# Sentinel returned by _resolve_request_profile when a /p/<profile>/ prefix
# names a profile this gateway does not serve (→ 404). Distinct from None
# (no prefix / multiplexing off → handle as the default profile).
_PROFILE_REJECTED = object()

_BUILTIN_DELIVER_PLATFORMS = {
    "telegram", "discord", "slack", "signal", "sms", "whatsapp",
    "matrix", "mattermost", "homeassistant", "email", "dingtalk",
    "feishu", "wecom", "wecom_callback", "weixin", "bluebubbles",
    "qqbot", "yuanbao",
}

# Default bind host. ``None`` tells aiohttp/asyncio's ``create_server`` to bind
# BOTH address families (IPv4 + IPv6) — the portable dual-stack default.
#
# Why not "0.0.0.0" (the old default) or "::"?
#   - "0.0.0.0" binds IPv4 ONLY. On IPv6-only private networks — notably Fly.io
#     6PN, where an agent's ``<app>.internal`` name resolves to an ``fdaa:…``
#     IPv6 address — an IPv4-only listener is unreachable. That is exactly why
#     hosted-agent webhook routes were publicly unreachable: the edge router
#     reverse-proxies to ``<app>.internal:8644`` over 6PN (IPv6) but the adapter
#     was listening on 0.0.0.0 (v4 only) → connection refused.
#   - "::" is NOT a safe fix: on hosts where the kernel sets IPV6_V6ONLY=1
#     (verified on Fly machines), binding "::" yields an IPv6-ONLY socket, which
#     then breaks the IPv4 loopback health check (``curl 127.0.0.1:8644/health``)
#     and the AF_INET port-conflict probe in connect().
#   - ``None`` asks the event loop to create a listening socket per resolved
#     family, so both 127.0.0.1 (v4) and the 6PN fdaa (v6) are served regardless
#     of the bindv6only sysctl. Users can still pin a specific host via
#     ``platforms.webhook.extra.host``.
DEFAULT_HOST = None
DEFAULT_PORT = 8644
_INSECURE_NO_AUTH = "INSECURE_NO_AUTH"
_DYNAMIC_ROUTES_FILENAME = "webhook_subscriptions.json"
_RATE_WINDOW_SECONDS = 60.0

# Server-side terminal-command denial applied to EVERY webhook/relay-dispatched
# agent session (loki lanes, relay workers). These run autonomously with no human
# present to answer an approval prompt, so push / PR / merge must be physically
# impossible at the approval layer — prompt-only guards are insufficient and were
# the deep-infra-audit P0 (a dispatched agent could push or merge if its prompt
# guard was stripped). Enforced unconditionally in approval.check_all_command_guards
# (before the yolo/mode=off bypass). Routes may ADD patterns via a
# "deny_terminal_patterns" list in webhook_subscriptions.json.
DEFAULT_WEBHOOK_DENY_PATTERNS = [
    *CREDENTIAL_EXFIL_DENY_PATTERNS,
    r"\bgit\s+(?:-\S+\s+\S*\s*)*push\b",       # git push, git -C <dir> push, git push --force
    r"\bgit\s+(?:-\S+\s+\S*\s*)*(?:checkout|switch|branch|reset|restore)\b",  # ref-mutation: webhook lanes must NOT flap the operator HEAD / main checkout (t_0113eacc)
    r"\bgh\s+pr\s+(?:create|merge|ready)\b",   # open / merge / mark-ready a PR
    r"\bgh\s+workflow\s+run\b",                # trigger a CI workflow (can push/merge/deploy)
    r"\bgh\s+run\s+(?:rerun|cancel|delete)\b", # re-run / cancel / delete a CI run (list/view/watch stay allowed)
    r"\bgh\s+release\s+(?:create|delete|upload)\b",  # cut / delete / upload a release asset
    r"\bgh\s+repo\s+(?:create|delete|fork)\b", # create / delete / fork a repo
    r"\bhub\s+(?:pull-request|merge|push)\b",  # legacy hub CLI equivalents
]

# Hostnames/IP literals that only serve connections originating on the same
# machine. Anything else is treated as a public bind for safety-rail purposes.
_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "ip6-localhost",
    "ip6-loopback",
})

_AGENT_RUN_SEMAPHORE: Optional[asyncio.Semaphore] = None
_AGENT_RUN_SEMAPHORE_CAP: Optional[int] = None
_LIVE_SESSION_SCAN_FAILED = object()
_PROCESS_START = datetime.now()


def _hydration_future_skew_tolerance_seconds() -> int:
    """Allowed forward clock skew (seconds) for hydration liveness ``updated_at``.

    A SessionEntry with ``updated_at`` beyond ``now + tolerance`` cannot be a
    genuinely live entry that just updated slightly ahead of this process's
    clock — it indicates a bad clock (e.g. a WSL2 clock jump) stamping a dead
    entry into the future, which must not re-adopt a stale worktree lease.
    """
    raw = os.environ.get("HERMES_WEBHOOK_HYDRATION_FUTURE_SKEW_SECONDS", "").strip()
    if raw:
        try:
            value = int(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
    return 300


_HYDRATION_FUTURE_SKEW_TOLERANCE = timedelta(seconds=_hydration_future_skew_tolerance_seconds())


def _get_agent_run_semaphore(max_concurrent_agent_runs: int) -> asyncio.Semaphore:
    """Return the process-global inbound agent-run semaphore for webhook runs."""
    global _AGENT_RUN_SEMAPHORE, _AGENT_RUN_SEMAPHORE_CAP
    if (
        _AGENT_RUN_SEMAPHORE is None
        or _AGENT_RUN_SEMAPHORE_CAP != max_concurrent_agent_runs
    ):
        _AGENT_RUN_SEMAPHORE = asyncio.Semaphore(max_concurrent_agent_runs)
        _AGENT_RUN_SEMAPHORE_CAP = max_concurrent_agent_runs
    return _AGENT_RUN_SEMAPHORE




def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")

def _safe_ref_component(value: str, *, fallback: str = "delivery") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-./")
    return cleaned or fallback

def _is_loopback_host(host: Optional[str]) -> bool:
    """True when `host` binds only to the local machine.

    Covers IPv4 loopback, the standard `localhost` alias, IPv6 loopback in
    both bracketed and bare form, and the common Debian-style aliases. Any
    falsy value (empty string, None) is conservatively treated as non-loopback
    because an unset host usually means the platform-default public bind.
    """
    if not host:
        return False
    return host.strip().lower() in _LOOPBACK_HOSTS


def _hmac_str_equal(provided: str, expected: str) -> bool:
    """Timing-safe equality for two ``str`` values, tolerant of non-ASCII input.

    ``hmac.compare_digest`` raises ``TypeError`` when given a ``str`` that
    contains non-ASCII characters. The ``provided`` value here is an
    attacker-controlled signature/token header on a public, unauthenticated
    webhook endpoint, so a single non-ASCII byte would otherwise raise out of
    the request handler and return a 500 instead of rejecting the request.
    Comparing as UTF-8 bytes keeps the constant-time guarantee while making a
    hostile header fail closed with a clean rejection.
    """
    return hmac.compare_digest(provided.encode(), expected.encode())


def check_webhook_requirements() -> bool:
    """Check if webhook adapter dependencies are available."""
    return AIOHTTP_AVAILABLE


class WebhookAdapter(BasePlatformAdapter):
    """Generic webhook receiver that triggers agent runs from HTTP POSTs."""

    # No human is present to answer a "session restored — what next?" prompt:
    # webhook runs are event-triggered.  The startup auto-resume turn must
    # instruct the model to FINISH the interrupted work instead of emitting an
    # interactive acknowledgement that abandons the task (#57056).
    interactive_resume: bool = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEBHOOK)
        # ``host`` may be None (dual-stack default) or a user-pinned string.
        # A config value of empty string / null is normalised to None so it
        # also means "bind all families" rather than an invalid "" host.
        _cfg_host = config.extra.get("host", DEFAULT_HOST)
        self._host: Optional[str] = _cfg_host or None
        self._port: int = int(config.extra.get("port", DEFAULT_PORT))
        self._global_secret: str = config.extra.get("secret", "")
        self._static_routes: Dict[str, dict] = config.extra.get("routes", {})
        self._dynamic_routes: Dict[str, dict] = {}
        self._dynamic_routes_mtime: float = 0.0
        self._routes: Dict[str, dict] = dict(self._static_routes)
        self._runner = None
        # Routes already warned about legacy V1 body-only signatures
        # (once-per-route so a busy sender doesn't spam the log).
        self._v1_signature_warned: set[str] = set()

        # Delivery info keyed by session chat_id.
        #
        # Read by every send() invocation for the chat_id (status messages
        # AND the final response).  Cleaned up via TTL on each POST so the
        # dict stays bounded — see _prune_delivery_info().  Do NOT pop on
        # send(), or interim status messages (e.g. fallback notifications,
        # context-pressure warnings) will consume the entry before the
        # final response arrives, causing the response to silently fall
        # back to the "log" deliver type.
        self._delivery_info: Dict[str, dict] = {}
        self._delivery_info_created: Dict[str, float] = {}

        # Reference to gateway runner for cross-platform delivery (set externally)
        self.gateway_runner = None

        # Idempotency: TTL cache of recently processed delivery IDs.
        # Prevents duplicate agent runs when webhook providers retry.
        self._seen_deliveries: Dict[str, float] = {}
        self._idempotency_ttl: int = 3600  # 1 hour

        # Rate limiting: per-route timestamps in a fixed window.
        self._rate_counts: Dict[str, List[float]] = {}
        self._rate_limit: int = int(config.extra.get("rate_limit", 30))  # per minute

        # Agent-run backpressure: global in-process cap across all webhook
        # routes. This limits concurrent in-flight agent tasks, not request
        # arrival rate, and fails fast with HTTP 429 when saturated.
        self._max_concurrent_agent_runs: int = resolve_max_concurrent_agent_runs(
            {"gateway": {"max_concurrent_agent_runs": config.extra.get("max_concurrent_agent_runs")}}
        )
        self._agent_run_semaphore = _get_agent_run_semaphore(self._max_concurrent_agent_runs)
        self._run_finalizers: Dict[str, Callable[[], None]] = {}

        # Body size limit (auth-before-body pattern)
        self._max_body_bytes: int = int(
            config.extra.get("max_body_bytes", 1_048_576)
        )  # 1MB
        self._script_timeout_seconds: int = int(
            config.extra.get(
                "script_timeout_seconds",
                DEFAULT_SCRIPT_TIMEOUT_SECONDS,
            )
        )
        self._route_processor = WebhookRouteProcessor(
            script_timeout_seconds=self._script_timeout_seconds
        )

        # Phase 3: bind relayed (Platform.WEBHOOK) agent runs to a dedicated git
        # worktree so an orchestrator's "hands" work on a branch, not the live
        # tree. Gated OFF by default; rollback = unset HERMES_WEBHOOK_WORKTREE.
        self._wt_enabled: bool = _env_truthy("HERMES_WEBHOOK_WORKTREE")
        # F4 per-delivery broker sits under the existing master gate and is OFF
        # by default; switch-off must preserve today's singleton path exactly.
        self._per_delivery_wt_enabled: bool = (
            self._wt_enabled and _env_truthy("HERMES_WEBHOOK_PER_DELIVERY_WT")
        )
        self._wt_base_branch_ref: str = (
            os.environ.get("HERMES_WEBHOOK_BASE_BRANCH", "").strip() or "fork/main"
        )
        self._wt_base_branch: str = self._wt_base_branch_ref
        self._wt_branch: str = "relay/work"
        self._wt_path: Optional[str] = None   # set lazily by _ensure_relay_worktree()
        self._wt_init_failed: bool = False    # latched so we refuse, not retry-spam
        self._wt_broker = None
        self._wt_broker_lock = threading.Lock()
        self._lease_by_finalizer: Dict[str, dict] = {}
        self._runtime_cwds_by_finalizer: Dict[str, Any] = {}
        self._hydrated_adoption_sids: set[str] = set()

    def _build_session_key(self, source) -> str:
        """Return the webhook run/session correlation key for a source."""
        from gateway.session import build_session_key

        extra = getattr(self.config, "extra", {}) or {}
        return build_session_key(
            source,
            group_sessions_per_user=extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
        )

    def _run_finalizer_key(self, event: MessageEvent) -> str:
        """Return the key used to register and complete a webhook agent run."""
        return self._build_session_key(event.source)

    def _register_run_finalizer(self, event: MessageEvent, approval_key: Optional[str], lease_info: Optional[dict] = None) -> str:
        """Register once-only run cleanup keyed by the event's session key."""
        key = self._run_finalizer_key(event)

        def _finalizer() -> None:
            try:
                from tools.approval import (
                    clear_session,
                    clear_session_credential_taint,
                )
                if approval_key:
                    clear_session(approval_key)
                    # Drop any two-step credential-stage taint accrued during
                    # this dispatch so it cannot bleed into a reused key.
                    clear_session_credential_taint(approval_key)
            except Exception:
                logger.debug(
                    "[webhook] failed to clear deny_terminal_patterns for key=%s",
                    key,
                    exc_info=True,
                )
            finally:
                if self._agent_run_semaphore._value < self._max_concurrent_agent_runs:
                    self._agent_run_semaphore.release()
                else:
                    logger.warning(
                        "[webhook] skipped over-release for finalized run key=%s",
                        key,
                    )

        self._run_finalizers[key] = _finalizer
        if lease_info:
            self._lease_by_finalizer[key] = lease_info
        self._runtime_cwds_by_finalizer[key] = ()
        return key

    def _record_run_execution_cwds(self, key: str, cwds: Any) -> None:
        """Persist runtime-boundary cwd observations for finalize-time adoption audit."""
        if key not in self._run_finalizers:
            return
        self._runtime_cwds_by_finalizer[key] = cwds

    def _runtime_cwds_match_lease(self, lease_info: dict, cwds: tuple[str, ...]) -> bool:
        """True when every recorded subprocess cwd is inside/equal to the lease path."""
        if not cwds:
            return True
        lease_path = str(lease_info.get("path") or "")
        if not lease_path:
            return False
        try:
            lease_real = Path(lease_path).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        for cwd in cwds:
            try:
                cwd_real = Path(cwd).expanduser().resolve()
            except (OSError, RuntimeError):
                return False
            if not (cwd_real == lease_real or lease_real in cwd_real.parents):
                return False
        return True

    def _finalize_run(self, key: str) -> None:
        """Run and remove a registered webhook finalizer exactly once."""
        finalizer = self._run_finalizers.pop(key, None)
        lease_info = self._lease_by_finalizer.pop(key, None)
        runtime_cwds = tuple(self._runtime_cwds_by_finalizer.pop(key, ()))
        if finalizer is None:
            return
        try:
            if lease_info:
                self._complete_worktree_lease(lease_info, runtime_cwds=runtime_cwds)
        finally:
            finalizer()

    def _release_run_backpressure(self, event: MessageEvent) -> None:
        """Release webhook run backpressure after the spawned agent task ends.

        Called from the single ``on_processing_complete`` override below (which
        also closes the per-delivery session).  Kept as its own method because
        the release must happen even when session close fails — a leaked
        semaphore slot wedges the adapter at ``max_concurrent_agent_runs``.
        """
        self._finalize_run(self._run_finalizer_key(event))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Load agent-created subscriptions before validating
        self._reload_dynamic_routes()

        # Validate routes at startup — secret is required per route
        for name, route in self._routes.items():
            secret = route.get("secret", self._global_secret)
            if not secret:
                raise ValueError(
                    f"[webhook] Route '{name}' has no HMAC secret. "
                    f"Set 'secret' on the route or globally. "
                    f"For testing without auth, set secret to '{_INSECURE_NO_AUTH}'."
                )

            # Safety rail: refuse to start if INSECURE_NO_AUTH is combined with a
            # non-loopback bind. The escape hatch is for local testing only;
            # serving an unauthenticated route on a public interface is a
            # deployment-grade footgun we'd rather crash early than ship.
            if secret == _INSECURE_NO_AUTH and not _is_loopback_host(self._host):
                raise ValueError(
                    f"[webhook] Route '{name}' uses INSECURE_NO_AUTH secret "
                    f"but is bound to non-loopback host '{self._host}'. "
                    f"INSECURE_NO_AUTH is for local testing only. "
                    f"Refusing to start to prevent accidental exposure."
                )
            # deliver_only routes bypass the agent — the POST body becomes a
            # direct push notification via the configured delivery target.
            # Validate up-front so misconfiguration surfaces at startup rather
            # than on the first webhook POST.
            if route.get("deliver_only"):
                deliver = route.get("deliver", "log")
                if not deliver or deliver == "log":
                    raise ValueError(
                        f"[webhook] Route '{name}' has deliver_only=true but "
                        f"deliver is '{deliver}'. Direct delivery requires a "
                        f"real target (telegram, discord, slack, github_comment, etc.)."
                    )

        if self._per_delivery_wt_enabled:
            try:
                await asyncio.to_thread(self._get_per_delivery_broker)
            except Exception:
                logger.exception("[webhook] failed to hydrate per-delivery worktree broker")

        # client_max_size makes aiohttp enforce the cap on every read path,
        # including Transfer-Encoding: chunked bodies that carry no
        # Content-Length and would otherwise bypass the header check below.
        app = web.Application(client_max_size=self._max_body_bytes)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/webhooks/{route_name}", self._handle_webhook)
        # Multi-profile multiplexing: a /p/<profile>/webhooks/<route> prefix
        # routes the inbound event to that profile. The handler validates both
        # gateway multiplexing and the route's explicit profile binding.
        app.router.add_post(
            "/p/{profile}/webhooks/{route_name}",
            self._handle_webhook,
        )

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        # Do not probe only one address family before binding. With the
        # dual-stack default, an IPv6-only listener can already own this port
        # while 127.0.0.1 still looks free.
        #
        # SO_REUSEADDR is platform-dependent:
        #   - macOS (BSD semantics): two wildcard/specific sockets with
        #     SO_REUSEADDR can silently split traffic while both servers
        #     report success — so disable it there.
        #   - Linux: SO_REUSEADDR only permits rebinding past TIME_WAIT
        #     (a second live listener needs SO_REUSEPORT, which we never
        #     set). Disabling it would make a quick gateway restart fail
        #     to bind for up to ~60s — so keep the default (enabled).
        site = web.TCPSite(
            self._runner,
            self._host,
            self._port,
            reuse_address=False if sys.platform == "darwin" else None,
        )
        try:
            await site.start()
        except OSError as exc:
            await self._runner.cleanup()
            self._runner = None
            logger.error(
                "[webhook] Could not bind %s:%d: %s. "
                "Set a different host or port in config.yaml under "
                "platforms.webhook.extra.",
                self._host or "all IPv4+IPv6 interfaces",
                self._port,
                exc,
            )
            return False
        self._mark_connected()

        route_names = ", ".join(self._routes.keys()) or "(none configured)"
        logger.info(
            "[webhook] Listening on %s:%d — routes: %s",
            self._host or "* (all interfaces, IPv4+IPv6)",
            self._port,
            route_names,
        )
        return True

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()
        logger.info("[webhook] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver the agent's response to the configured destination.

        chat_id is ``webhook:{route}:{delivery_id}``.  The delivery info
        stored during webhook receipt is read with ``.get()`` (not popped)
        so that interim status messages emitted before the final response
        — fallback-model notifications, context-pressure warnings, etc. —
        do not consume the entry and silently downgrade the final response
        to the ``log`` deliver type.  TTL cleanup happens on POST.
        """
        if _is_webhook_silence_response(content):
            logger.info(
                "[webhook] Response for %s is a silence marker — not delivering", chat_id
            )
            return SendResult(success=True)

        delivery = self._delivery_info.get(chat_id, {})
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            logger.info("[webhook] Response for %s: %s", chat_id, content[:200])
            return SendResult(success=True)

        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)

        # Cross-platform delivery — any platform with a gateway adapter.
        # Check both built-in names and plugin-registered platforms.
        _is_known_platform = deliver_type in _BUILTIN_DELIVER_PLATFORMS
        if not _is_known_platform:
            try:
                from gateway.platform_registry import platform_registry
                _is_known_platform = platform_registry.is_registered(deliver_type)
            except Exception:
                pass
        if self.gateway_runner and _is_known_platform:
            return await self._deliver_cross_platform(
                deliver_type, content, delivery
            )

        logger.warning("[webhook] Unknown deliver type: %s", deliver_type)
        return SendResult(
            success=False, error=f"Unknown deliver type: {deliver_type}"
        )

    def _prune_delivery_info(self, now: float) -> None:
        """Drop delivery_info entries older than the idempotency TTL.

        Mirrors the cleanup pattern used for ``_seen_deliveries``.  Called
        on each POST so the dict size is bounded by ``rate_limit * TTL``
        even if many webhooks fire and never receive a final response.
        """
        cutoff = now - self._idempotency_ttl
        stale = [
            k
            for k, t in self._delivery_info_created.items()
            if t < cutoff
        ]
        for k in stale:
            self._delivery_info.pop(k, None)
            self._delivery_info_created.pop(k, None)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "webhook"}

    def toolsets_for_source(self, source) -> Optional[List[str]]:
        """Per-route toolset override.

        Webhook session chat_ids are ``webhook:{route}:{delivery_id}``.
        When the matching route config carries a ``toolsets`` list, that list
        replaces the platform-level ``platform_toolsets.webhook`` resolution
        for this run only. Routes without the key keep the platform default
        (the intentionally constrained webhook-safe toolset), so a single
        trusted route (e.g. a localhost monitoring push) can be granted
        ``terminal`` without widening every other webhook route.

        Set via ``platforms.webhook.extra.routes.<name>.toolsets`` in
        config.yaml or a ``toolsets`` key on a subscription in
        ``webhook_subscriptions.json`` (manual edit — deliberately NOT
        exposed through `hermes webhook subscribe`, so an agent-created
        subscription cannot self-grant elevated tools).
        """
        chat_id = str(getattr(source, "chat_id", "") or "")
        parts = chat_id.split(":", 2)
        if len(parts) < 2 or parts[0] != "webhook":
            return None
        route_config = self._routes.get(parts[1])
        if not isinstance(route_config, dict):
            return None
        toolsets = route_config.get("toolsets")
        if not isinstance(toolsets, list) or not toolsets:
            return None
        cleaned = [str(t).strip() for t in toolsets if str(t).strip()]
        return cleaned or None

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response({"status": "ok", "platform": "webhook"})

    def _reload_dynamic_routes(self) -> None:
        """Reload agent-created subscriptions from disk if the file changed."""
        from hermes_constants import get_hermes_home
        hermes_home = get_hermes_home()
        subs_path = hermes_home / _DYNAMIC_ROUTES_FILENAME
        if not subs_path.exists():
            if self._dynamic_routes:
                self._dynamic_routes = {}
                self._routes = dict(self._static_routes)
                logger.debug("[webhook] Dynamic subscriptions file removed, cleared dynamic routes")
            return
        try:
            mtime = subs_path.stat().st_mtime
            if mtime <= self._dynamic_routes_mtime:
                return  # No change
            data = json.loads(subs_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            # Merge: static routes take precedence over dynamic ones.
            # Reject any dynamic route whose effective secret is empty —
            # an empty secret would cause _handle_webhook to skip HMAC
            # validation entirely, letting unauthenticated callers in.
            new_dynamic: Dict[str, dict] = {}
            for k, v in data.items():
                if k in self._static_routes:
                    continue
                effective_secret = v.get("secret", self._global_secret)
                if not effective_secret:
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: 'secret' is "
                        "missing or empty. Set a valid HMAC secret, or use "
                        "'%s' to explicitly disable auth (testing only).",
                        k,
                        _INSECURE_NO_AUTH,
                    )
                    continue
                if (
                    effective_secret == _INSECURE_NO_AUTH
                    and not _is_loopback_host(self._host)
                ):
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: INSECURE_NO_AUTH "
                        "is only allowed on loopback hosts. Current host: '%s'.",
                        k,
                        self._host,
                    )
                    continue
                new_dynamic[k] = v
            self._dynamic_routes = new_dynamic
            self._routes = {**self._dynamic_routes, **self._static_routes}
            self._dynamic_routes_mtime = mtime
            logger.info(
                "[webhook] Reloaded %d dynamic route(s): %s",
                len(self._dynamic_routes),
                ", ".join(self._dynamic_routes.keys()) or "(none)",
            )
        except Exception as e:
            logger.error("[webhook] Failed to reload dynamic routes: %s", e)

    def _ensure_relay_worktree(self) -> Optional[str]:
        """Lazily create (or reuse) the persistent relay git worktree.

        Returns the absolute path, or None if it cannot be guaranteed — in
        which case the caller MUST refuse the run rather than fall through to
        the gateway's live cwd. gc-immune: lives OUTSIDE ~/.hermes/codex-wt/ so
        CodexGcWatcher never scans it; uses no port (no codex pool contention).
        """
        if self._wt_path is not None and Path(self._wt_path).is_dir():
            return self._wt_path
        if self._wt_init_failed:
            return None
        try:
            from hermes_constants import get_hermes_home
            hermes_home = Path(get_hermes_home())
            repo_root = os.environ.get(
                "HERMES_REPO_ROOT", str(Path(__file__).resolve().parents[2])
            )
            wt_dir = hermes_home / "relay-wt" / "relay"
            if wt_dir.is_dir():
                self._wt_path = str(wt_dir)
                return self._wt_path
            wt_dir.parent.mkdir(parents=True, exist_ok=True)
            # Idempotent: re-attach to the branch if it already exists from a prior boot.
            ref_check = subprocess.run(
                ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet",
                 f"refs/heads/{self._wt_branch}"],
                capture_output=True, text=True, timeout=25,
            )
            add_cmd = ["git", "-C", repo_root, "worktree", "add", str(wt_dir)]
            if ref_check.returncode == 0:
                add_cmd.append(self._wt_branch)
            else:
                add_cmd += ["-b", self._wt_branch, self._wt_base_branch]
            res = subprocess.run(add_cmd, capture_output=True, text=True, timeout=25)
            if res.returncode != 0 or not wt_dir.is_dir():
                logger.error(
                    "[webhook] relay worktree add failed rc=%s: %s",
                    res.returncode, (res.stderr or "").strip(),
                )
                self._wt_init_failed = True
                return None
            self._wt_path = str(wt_dir)
            logger.info(
                "[webhook] relay worktree ready at %s on branch %s (base %s)",
                self._wt_path, self._wt_branch, self._wt_base_branch,
            )
            return self._wt_path
        except Exception:
            logger.exception(
                "[webhook] relay worktree init crashed; refusing write-capable relayed runs"
            )
            self._wt_init_failed = True
            return None

    def _lease_ledger_path(self) -> Path:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / "state" / "loki" / "worktree-leases.jsonl"

    def _append_lease_ledger(self, event: str, lease_info: dict) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            "sid": lease_info.get("sid"),
            "delivery_id": lease_info.get("delivery_id"),
            "route": lease_info.get("route"),
            "path": lease_info.get("path"),
            "branch": lease_info.get("branch"),
            "base": lease_info.get("base"),
        }
        if lease_info.get("base_ref"):
            record["base_ref"] = lease_info.get("base_ref")
        if lease_info.get("reason"):
            record["reason"] = lease_info.get("reason")
        try:
            path = self._lease_ledger_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fd:
                fd.write(json.dumps(record, sort_keys=True) + "\n")
        except (OSError, IOError):
            logger.error(
                "[webhook] lease ledger append failed event=%s sid=%s delivery=%s",
                event,
                lease_info.get("sid"),
                lease_info.get("delivery_id"),
                exc_info=True,
            )

    @staticmethod
    def _alternate_profile_session_keys(session_key: str, entries: dict) -> list[str]:
        """Return profile-namespace alternates for a dispatch/session key."""
        parts = session_key.split(":", 2)
        if len(parts) != 3 or parts[0] != "agent":
            return []
        suffix = parts[2]
        return [
            key for key in entries
            if isinstance(key, str)
            and key != session_key
            and key.startswith("agent:")
            and key.split(":", 2)[-1] == suffix
        ]

    def _lookup_live_session_entry(self, session_key: str) -> Any | None:
        """Return the live SessionStore entry for ``session_key`` without creating one."""
        store = getattr(self, "_session_store", None)
        if store is None or not session_key:
            return None
        try:
            ensure_loaded = getattr(store, "_ensure_loaded", None)
            if callable(ensure_loaded):
                ensure_loaded()
            entries = getattr(store, "_entries", None)
            if isinstance(entries, dict):
                entry = entries.get(session_key)
                if entry is not None:
                    return entry
                # F2: mirror the gateway/run.py dual-key defensive read. The
                # webhook dispatch key is built in the legacy ``agent:main``
                # namespace, while a multiplexed gateway can store the live
                # SessionEntry under ``agent:<profile>``. Any live entry for
                # the same platform/chat suffix is authoritative for refusal;
                # treating it as fresh would silently miss a stale/mismatched
                # binding.
                for alternate_key in self._alternate_profile_session_keys(session_key, entries):
                    entry = entries.get(alternate_key)
                    if entry is not None:
                        return entry
                return None
            get_entry = getattr(store, "get", None)
            if callable(get_entry):
                return get_entry(session_key)
        except Exception:
            logger.exception(
                "[webhook] live session lookup failed for %s; refusing per-delivery adoption",
                session_key,
            )
            return object()
        return None

    @staticmethod
    def _session_entry_updated_at(entry: Any) -> datetime | None:
        """Return a SessionEntry.updated_at value usable for hydration liveness."""
        updated_at = getattr(entry, "updated_at", None)
        if isinstance(updated_at, datetime):
            return updated_at
        if isinstance(updated_at, str):
            try:
                return datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    @staticmethod
    def _naive_local_datetime(value: datetime) -> datetime:
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone().replace(tzinfo=None)
        return value

    def _session_entry_is_live_for_hydration(self, entry: Any) -> bool:
        """True when a SessionStore entry has been active since this process
        started AND is not future-dated beyond the allowed clock-skew window.

        Defense-in-depth on top of the ``_PROCESS_START`` liveness rail
        (t_8535d138): a bad clock (e.g. a WSL2 clock jump) can stamp a DEAD
        entry's ``updated_at`` far into the future, which would otherwise
        satisfy ``updated_at >= _PROCESS_START`` and be adopted as trusted.
        Anything beyond ``now + _HYDRATION_FUTURE_SKEW_TOLERANCE`` cannot be a
        genuinely live update and is treated as not-live, falling through to
        the same fail-closed stale-complete path as any other dead entry.
        """
        updated_at = self._session_entry_updated_at(entry)
        if updated_at is None:
            return False
        normalized = self._naive_local_datetime(updated_at)
        if normalized < self._naive_local_datetime(_PROCESS_START):
            return False
        future_ceiling = datetime.now() + _HYDRATION_FUTURE_SKEW_TOLERANCE
        if normalized > future_ceiling:
            return False
        return True

    def _live_session_entries(self) -> list[Any] | None | object:
        """Return post-process-start SessionStore entries, a fail-closed sentinel, or None.

        F1 fail-closed marker: scan exceptions return _LIVE_SESSION_SCAN_FAILED;
        hydration must refuse every candidate with reason hydrate_scan_failure.
        """
        store = getattr(self, "_session_store", None)
        if store is None:
            return None
        try:
            ensure_loaded = getattr(store, "_ensure_loaded", None)
            if callable(ensure_loaded):
                ensure_loaded()
            if not hasattr(store, "_entries"):
                return []
            entries = getattr(store, "_entries")
            if isinstance(entries, dict):
                return [
                    entry for entry in entries.values()
                    if self._session_entry_is_live_for_hydration(entry)
                ]
            logger.error(
                "[webhook] live session scan found untrusted entries type %s during per-delivery hydration; refusing adoption",
                type(entries).__name__,
            )
            return _LIVE_SESSION_SCAN_FAILED
        except Exception:
            logger.exception("[webhook] live session scan failed during per-delivery hydration")
            return _LIVE_SESSION_SCAN_FAILED

    @staticmethod
    def _same_worktree_path(left: str | None, right: str | None) -> bool:
        if not left or not right:
            return False
        try:
            return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
        except (OSError, RuntimeError):
            return os.path.abspath(left) == os.path.abspath(right)

    def _verify_per_delivery_adoption(self, *, session_key: str, worktree_path: str) -> bool:
        """True when target session is fresh or already bound to this exact worktree."""
        entry = self._lookup_live_session_entry(session_key)
        if entry is None:
            return True
        existing_worktree = getattr(entry, "worktree_path", None)
        return self._same_worktree_path(existing_worktree, worktree_path)

    def _refuse_worktree_lease(self, lease_info: dict, *, reason: str) -> None:
        """Release a lease as refused and record distinct ledger evidence."""
        refused = dict(lease_info)
        refused["reason"] = reason
        broker = self._wt_broker
        sid = str(refused.get("sid") or "")
        if broker is not None and sid:
            try:
                broker.release(sid)
            except Exception:
                logger.exception(
                    "[webhook] failed to release refused per-delivery lease sid=%s delivery=%s",
                    sid,
                    refused.get("delivery_id"),
                )
        self._append_lease_ledger("refused", refused)

    def _resolve_worktree_base_sha(self) -> str:
        repo_root = os.environ.get(
            "HERMES_REPO_ROOT", str(Path(__file__).resolve().parents[2])
        )
        res = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", self._wt_base_branch_ref],
            capture_output=True, text=True, timeout=25,
        )
        if res.returncode != 0:
            raise RuntimeError((res.stderr or "git rev-parse failed").strip())
        return res.stdout.strip()

    def _get_per_delivery_broker(self):
        if self._wt_broker is not None:
            return self._wt_broker
        with self._wt_broker_lock:  # F4 broker-singleton lock marker
            if self._wt_broker is None:
                from agent.worktree_broker import WorktreeBroker
                from hermes_constants import get_hermes_home
                hermes_home = Path(get_hermes_home())
                repo_root = Path(os.environ.get(
                    "HERMES_REPO_ROOT", str(Path(__file__).resolve().parents[2])
                ))
                existing = self._hydrate_per_delivery_sessions(hermes_home=hermes_home, repo_root=repo_root)
                self._wt_broker = WorktreeBroker(
                    repo_root=repo_root,
                    hermes_home=hermes_home,
                    existing_sessions=existing,
                    wt_dir_name="relay-wt/deliveries",
                    branch_prefix="loki",
                    ports_enabled=False,
                    max_active_leases=self._max_concurrent_agent_runs,
                )
        return self._wt_broker

    def _hydrated_lease_record(
        self,
        *,
        child: Path,
        branch: str,
        base_sha: str | None,
        reason: str,
    ) -> dict:
        return {
            "sid": child.name,
            "delivery_id": child.name,
            "route": "hydrate",
            "path": str(child),
            "branch": branch,
            "base": base_sha,
            "reason": reason,
        }

    @staticmethod
    def _hydrated_worktree_is_clean_for_removal(child: Path, base_sha: str | None) -> bool:
        """True when a stale hydrated worktree has no local work to harvest."""
        try:
            status = subprocess.run(
                ["git", "-C", str(child), "status", "--porcelain"],
                capture_output=True, text=True, check=False, timeout=25,
            )
        except subprocess.TimeoutExpired:
            logger.warning("[webhook] stale hydrated worktree status timed out for %s; retaining", child)
            return False
        if status.returncode != 0 or status.stdout.strip():
            return False
        if not base_sha:
            return False
        try:
            commits = subprocess.run(
                ["git", "-C", str(child), "rev-list", "--count", f"{base_sha}..HEAD"],
                capture_output=True, text=True, check=False, timeout=25,
            )
        except subprocess.TimeoutExpired:
            logger.warning("[webhook] stale hydrated worktree rev-list timed out for %s; retaining", child)
            return False
        if commits.returncode != 0:
            return False
        try:
            return int((commits.stdout or "0").strip() or "0") == 0
        except ValueError:
            return False

    def _complete_stale_hydrated_worktree(
        self,
        *,
        child: Path,
        branch: str,
        base_sha: str | None,
        reason: str,
        repo_root: Path,
    ) -> None:
        """Retire a stale restart candidate without keeping it as an active lease.

        Clean stale worktrees are removed. Dirty/non-clean trees are left on disk
        for operator harvest, but are deliberately not returned to the broker
        registry, so they cannot consume active lease capacity after restart.
        """
        record = self._hydrated_lease_record(
            child=child,
            branch=branch,
            base_sha=base_sha,
            reason=reason,
        )
        event = "awaiting-harvest"
        if self._hydrated_worktree_is_clean_for_removal(child, base_sha):
            try:
                rm_result = subprocess.run(
                    ["git", "-C", str(repo_root), "worktree", "remove", str(child)],
                    capture_output=True, text=True, check=False, timeout=25,
                )
            except subprocess.TimeoutExpired:
                logger.warning("[webhook] clean stale per-delivery worktree removal timed out for %s; retaining", child)
                rm_result = None
            if rm_result is not None and (rm_result.returncode == 0 or not child.exists()):
                event = "removed"
            elif rm_result is not None:
                logger.warning(
                    "[webhook] clean stale per-delivery worktree removal failed for %s: %s",
                    child,
                    (rm_result.stderr or "").strip(),
                )
        self._append_lease_ledger(event, record)

    def _hydrate_per_delivery_sessions(self, *, hermes_home: Path, repo_root: Path) -> dict[str, dict]:
        """Conservatively adopt only live-bound wh-* loki/* worktrees under relay-wt/deliveries."""
        root = hermes_home / "relay-wt" / "deliveries"
        if not root.is_dir():
            return {}
        res = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=25,
        )
        if res.returncode != 0:
            logger.warning("[webhook] per-delivery worktree hydration failed: %s", (res.stderr or "").strip())
            return {}
        entries: dict[str, dict[str, str]] = {}
        current: dict[str, str] = {}
        for line in res.stdout.splitlines() + [""]:
            if not line.strip():
                if current.get("worktree"):
                    entries[current["worktree"]] = dict(current)
                current = {}
                continue
            if line.startswith("worktree "):
                current["worktree"] = line.removeprefix("worktree ")
            elif line.startswith("branch "):
                current["branch"] = line.removeprefix("branch ").removeprefix("refs/heads/")
            elif line.startswith("HEAD "):
                current["base_sha"] = line.removeprefix("HEAD ")
        adopted: dict[str, dict] = {}
        live_entries = self._live_session_entries()
        for child in root.iterdir():
            if not child.is_dir() or not child.name.startswith("wh-"):
                continue
            meta = entries.get(str(child.resolve())) or entries.get(str(child))
            branch = (meta or {}).get("branch", "")
            base_sha = (meta or {}).get("base_sha")
            if not branch.startswith("loki/"):
                continue
            if live_entries is _LIVE_SESSION_SCAN_FAILED:
                self._append_lease_ledger(
                    "refused",
                    self._hydrated_lease_record(
                        child=child,
                        branch=branch,
                        base_sha=base_sha,
                        reason="hydrate_scan_failure",
                    ),
                )
                continue
            if live_entries is not None:
                matching = [
                    entry for entry in live_entries
                    if self._same_worktree_path(getattr(entry, "worktree_path", None), str(child))
                ]
                mismatched = [
                    entry for entry in live_entries
                    if str(getattr(entry, "worktree_path", "") or "") and not self._same_worktree_path(
                        getattr(entry, "worktree_path", None),
                        str(child),
                    )
                ]
                if not matching and len(mismatched) > 0:
                    self._append_lease_ledger(
                        "refused",
                        self._hydrated_lease_record(
                            child=child,
                            branch=branch,
                            base_sha=base_sha,
                            reason="hydrate_live_binding_mismatch",
                        ),
                    )
                    continue
                adopted_once = child.name in getattr(self, "_hydrated_adoption_sids", set())
                # Stale-completion fires whenever NO live session is bound to THIS
                # worktree — regardless of whether unrelated live sessions exist
                # elsewhere. The earlier `not live_entries` guard only triggered
                # when the SessionStore was completely empty, so on any non-idle
                # gateway a stale/leaked wh-* worktree fell through to unconditional
                # adoption (re-arming it as a trusted lease across restart — worse
                # than a capacity wedge). Mismatch (a live session bound to a
                # DIFFERENT path) is already handled disk-inert above and takes
                # priority; adoption below is reached only when `matching` is
                # non-empty (a live session is bound to exactly this worktree).
                if not matching and not adopted_once:
                    self._complete_stale_hydrated_worktree(
                        child=child,
                        branch=branch,
                        base_sha=base_sha,
                        reason="hydrate_no_live_session",
                        repo_root=repo_root,
                    )
                    continue
            adopted[child.name] = {
                "path": str(child),
                "branch": branch,
                "base_sha": base_sha,
            }
            self._hydrated_adoption_sids.add(child.name)
        return adopted

    def _allocate_per_delivery_worktree(self, route_name: str, delivery_id: str) -> dict:
        from agent.worktree_broker import BranchCollisionError, DiskPressureError, LeaseCapacityError, RepoStateError
        delivery_hash = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()[:12]
        short_delivery = _safe_ref_component(delivery_hash, fallback="delivery")
        route_component = _safe_ref_component(route_name, fallback="route")
        sid = f"wh-{route_component}-{short_delivery}"
        branch = f"loki/{route_component}/{short_delivery}"
        base_sha = self._resolve_worktree_base_sha()
        broker = self._get_per_delivery_broker()
        preexisting_lease = sid in getattr(broker, "_registry", {})
        release_on_failure = False
        try:
            wt = broker.allocate(
                sid,
                isa_slug=short_delivery,
                base_branch=base_sha,
                branch_name=branch,
                base_sha=base_sha,
                identity=delivery_id,
            )
            release_on_failure = not preexisting_lease
            lease = {
                "sid": sid,
                "delivery_id": delivery_id,
                "route": route_name,
                "path": str(wt.path),
                "branch": wt.branch,
                "base": base_sha,
                "base_ref": self._wt_base_branch_ref,
            }
            try:
                self._append_lease_ledger("leased", lease)
            except (OSError, IOError):
                logger.error(
                    "[webhook] lease ledger append failed event=leased sid=%s delivery=%s",
                    sid,
                    delivery_id,
                    exc_info=True,
                )
            return lease
        except (DiskPressureError, RepoStateError, BranchCollisionError, LeaseCapacityError):
            raise
        except RuntimeError:
            if release_on_failure:
                try:
                    broker.release(sid)
                except Exception:
                    logger.exception(
                        "[webhook] failed to release per-delivery worktree after post-allocate failure sid=%s",
                        sid,
                    )
            raise
        except Exception:
            if release_on_failure:
                try:
                    broker.release(sid)
                except Exception:
                    logger.exception(
                        "[webhook] failed to release per-delivery worktree after post-allocate failure sid=%s",
                        sid,
                    )
            raise

    def _complete_worktree_lease(self, lease_info: dict, *, runtime_cwds: tuple[str, ...] = ()) -> None:
        broker = self._wt_broker
        if broker is None:
            return
        sid = str(lease_info.get("sid") or "")
        if not sid:
            return
        if not self._runtime_cwds_match_lease(lease_info, runtime_cwds):
            failed = dict(lease_info)
            failed["reason"] = "adoption_failed_runtime_cwd_mismatch"
            try:
                broker._registry.pop(sid, None)
            except Exception:
                logger.debug("[webhook] failed to drop mismatched lease registry sid=%s", sid, exc_info=True)
            self._append_lease_ledger("adoption_failed", failed)
            return
        result = broker.complete_lease(sid, base_sha=lease_info.get("base"))
        try:
            self._append_lease_ledger("completed", lease_info)
            self._append_lease_ledger(result, lease_info)
        except (OSError, IOError):
            logger.error(
                "[webhook] lease ledger append failed during completion sid=%s delivery=%s",
                sid,
                lease_info.get("delivery_id"),
                exc_info=True,
            )

    def _resolve_request_profile(self, request: "web.Request"):
        """Resolve and validate a /p/<profile>/ webhook URL prefix.

        ``None`` means no prefix or multiplexing disabled; a string is a
        served profile; ``_PROFILE_REJECTED`` fails closed as HTTP 404.
        """
        profile = (request.match_info.get("profile") or "").strip()
        if not profile:
            return None
        runner = self.gateway_runner
        cfg = getattr(runner, "config", None)
        if not getattr(cfg, "multiplex_profiles", False):
            return None
        try:
            from hermes_cli.profiles import profiles_to_serve

            served = {
                name
                for name, _ in profiles_to_serve(
                    multiplex=True,
                    profile_allowlist=getattr(
                        cfg, "multiplex_profile_allowlist", None
                    ),
                )
            }
        except Exception:
            return _PROFILE_REJECTED
        if profile not in served:
            return _PROFILE_REJECTED
        return profile

    @staticmethod
    def _route_allows_profile(
        route_config: dict,
        request_profile: Optional[str],
    ) -> bool:
        """Return whether a route is bound to the URL-selected profile.

        Omitting ``profile`` keeps a route on the default profile. An explicit
        null, blank, or non-string value is malformed and fails closed.
        """
        if "profile" not in route_config:
            configured_profile = "default"
        else:
            configured_profile = route_config.get("profile")
        if not isinstance(configured_profile, str):
            return False
        configured_profile = configured_profile.strip()
        if not configured_profile:
            return False
        effective_profile = request_profile or "default"
        return configured_profile == effective_profile

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        """POST /webhooks/{route_name} — receive and process a webhook event."""
        # Hot-reload dynamic subscriptions on each request (mtime-gated, cheap)
        self._reload_dynamic_routes()

        route_name = request.match_info.get("route_name", "")
        route_config = self._routes.get(route_name)

        # Multi-profile: resolve and validate the URL prefix before revealing
        # whether the selected route exists.
        profile = self._resolve_request_profile(request)
        if profile is _PROFILE_REJECTED:
            return web.json_response(
                {"error": "Unknown or unconfigured profile"},
                status=404,
            )

        if not route_config:
            return web.json_response(
                {"error": f"Unknown route: {route_name}"}, status=404
            )

        if not self._route_allows_profile(route_config, profile):
            effective_profile = profile or "default"
            logger.warning(
                "[webhook] Route %s is not authorized for profile %r",
                route_name,
                effective_profile,
            )
            # Match the unknown-route response so callers cannot use profile
            # mismatches to enumerate route bindings.
            return web.json_response(
                {"error": f"Unknown route: {route_name}"}, status=404
            )

        # Disabled routes are kept in the subscriptions file (so the dashboard
        # can re-enable them) but reject incoming events.  Default-enabled:
        # only an explicit ``enabled: false`` turns a route off, matching the
        # mcp_servers ``enabled`` semantics.
        if route_config.get("enabled", True) is False:
            return web.json_response(
                {"error": f"Route disabled: {route_name}"}, status=403
            )

        # ── Auth-before-body ─────────────────────────────────────
        # Check Content-Length before reading the full payload.
        content_length = request.content_length or 0
        if content_length > self._max_body_bytes:
            return web.json_response(
                {"error": "Payload too large"}, status=413
            )

        # Read body (must be done before any validation)
        try:
            raw_body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            # aiohttp's client_max_size tripped — chunked or lying
            # Content-Length. Same 413 as the header check above.
            return web.json_response(
                {"error": "Payload too large"}, status=413
            )
        except Exception as e:
            logger.error("[webhook] Failed to read body: %s", e)
            return web.json_response({"error": "Bad request"}, status=400)
        if len(raw_body) > self._max_body_bytes:
            # Defense in depth: enforce the cap on the actual bytes read even
            # if the server-level limit was bypassed or misconfigured.
            return web.json_response(
                {"error": "Payload too large"}, status=413
            )

        # Validate HMAC signature FIRST (skip only for the explicit local-test
        # INSECURE_NO_AUTH mode). Missing/empty secrets must fail closed here,
        # not only during connect(), so direct handler reuse cannot turn a
        # network webhook route into an unauthenticated agent-dispatch surface.
        secret = route_config.get("secret", self._global_secret)
        if not secret:
            logger.error(
                "[webhook] Route %s has no HMAC secret; refusing request",
                route_name,
            )
            return web.json_response(
                {"error": "Webhook route is missing an HMAC secret"},
                status=403,
            )
        if secret != _INSECURE_NO_AUTH:
            if not self._validate_signature(request, raw_body, secret):
                logger.warning(
                    "[webhook] Invalid signature for route %s", route_name
                )
                return web.json_response(
                    {"error": "Invalid signature"}, status=401
                )

        # ── Rate limiting (after auth) ───────────────────────────
        now = time.time()
        window = self._rate_counts.setdefault(route_name, [])
        window[:] = [t for t in window if now - t < 60]
        if len(window) >= self._rate_limit:
            return web.json_response(
                {"error": "Rate limit exceeded"}, status=429
            )
        window.append(now)

        # Parse payload
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            # Try form-encoded as fallback
            try:
                import urllib.parse

                payload = dict(
                    urllib.parse.parse_qsl(raw_body.decode("utf-8"))
                )
            except Exception:
                return web.json_response(
                    {"error": "Cannot parse body"}, status=400
                )

        # Check event type filter
        event_type = (
            request.headers.get("X-GitHub-Event", "")
            or request.headers.get("X-GitLab-Event", "")
            or payload.get("event_type", "")
            or payload.get("type", "")
            or "unknown"
        )
        allowed_events = route_config.get("events", [])
        if allowed_events and event_type not in allowed_events:
            logger.debug(
                "[webhook] Ignoring event %s for route %s (allowed: %s)",
                event_type,
                route_name,
                allowed_events,
            )
            return web.json_response(
                {"status": "ignored", "event": event_type}
            )

        if not self._route_processor.route_filters_match(
            route_config, payload, event_type, request.headers
        ):
            logger.info(
                "[webhook] filtered event=%s route=%s",
                event_type,
                route_name,
            )
            return web.json_response(
                {
                    "status": "ignored",
                    "reason": "filter",
                    "route": route_name,
                }
            )

        if route_config.get("script"):
            # run_route_script shells out (subprocess.run, up to its timeout);
            # run it in a worker thread so it can't block the gateway event loop.
            keep, transformed_payload = await asyncio.to_thread(
                self._route_processor.run_route_script,
                route_config.get("script"),
                payload,
            )
            if not keep:
                logger.info(
                    "[webhook] script ignored event=%s route=%s",
                    event_type,
                    route_name,
                )
                return web.json_response(
                    {
                        "status": "ignored",
                        "reason": "script",
                        "route": route_name,
                    }
                )
            payload = transformed_payload or payload

        # Format prompt from template
        prompt_template = route_config.get("prompt", "")
        prompt = self._render_prompt(
            prompt_template, payload, event_type, route_name
        )

        # Inject skill content if configured.
        # We call build_skill_invocation_message() directly rather than
        # using /skill-name slash commands — the gateway's command parser
        # would intercept those and break the flow.
        skills = route_config.get("skills", [])
        if skills:
            try:
                from agent.skill_commands import (
                    build_skill_invocation_message,
                    get_skill_commands,
                )

                skill_cmds = get_skill_commands()
                for skill_name in skills:
                    cmd_key = f"/{skill_name}"
                    if cmd_key in skill_cmds:
                        skill_content = build_skill_invocation_message(
                            cmd_key, user_instruction=prompt
                        )
                        if skill_content:
                            prompt = skill_content
                            break  # Load the first matching skill
                    else:
                        logger.warning(
                            "[webhook] Skill '%s' not found", skill_name
                        )
            except Exception as e:
                logger.warning("[webhook] Skill loading failed: %s", e)

        # Build a unique delivery ID
        delivery_id = request.headers.get(
            "X-GitHub-Delivery",
            request.headers.get(
                "svix-id",
                request.headers.get("X-Request-ID", str(int(time.time() * 1000))),
            ),
        )

        # ── Idempotency ─────────────────────────────────────────
        # Skip duplicate deliveries (webhook retries).
        now = time.time()
        # Prune expired entries
        self._seen_deliveries = {
            k: v
            for k, v in self._seen_deliveries.items()
            if now - v < self._idempotency_ttl
        }
        if delivery_id in self._seen_deliveries:
            logger.info(
                "[webhook] Skipping duplicate delivery %s", delivery_id
            )
            return web.json_response(
                {"status": "duplicate", "delivery_id": delivery_id},
                status=200,
            )
        self._seen_deliveries[delivery_id] = now

        # ── Direct delivery mode (deliver_only) ─────────────────
        # Skip the agent entirely — the rendered prompt IS the message we
        # deliver.  Use case: external services (Supabase, monitoring,
        # cron jobs, other agents) that need to push a plain notification
        # to a user's chat with zero LLM cost.  Reuses the same HMAC auth,
        # rate limiting, idempotency, and template rendering as agent mode.
        if route_config.get("deliver_only"):
            delivery = {
                "deliver": route_config.get("deliver", "log"),
                "deliver_extra": self._render_delivery_extra(
                    route_config.get("deliver_extra", {}), payload
                ),
                "payload": payload,
            }
            logger.info(
                "[webhook] direct-deliver event=%s route=%s target=%s msg_len=%d delivery=%s",
                event_type,
                route_name,
                delivery["deliver"],
                len(prompt),
                delivery_id,
            )
            try:
                result = await self._direct_deliver(prompt, delivery)
            except Exception:
                logger.exception(
                    "[webhook] direct-deliver failed route=%s delivery=%s",
                    route_name,
                    delivery_id,
                )
                return web.json_response(
                    {"status": "error", "error": "Delivery failed", "delivery_id": delivery_id},
                    status=502,
                )

            if result.success:
                return web.json_response(
                    {
                        "status": "delivered",
                        "route": route_name,
                        "target": delivery["deliver"],
                        "delivery_id": delivery_id,
                    },
                    status=200,
                )
            # Delivery attempted but target rejected it — surface as 502
            # with a generic error (don't leak adapter-level detail).
            logger.warning(
                "[webhook] direct-deliver target rejected route=%s target=%s error=%s",
                route_name,
                delivery["deliver"],
                result.error,
            )
            return web.json_response(
                {"status": "error", "error": "Delivery failed", "delivery_id": delivery_id},
                status=502,
            )

        # Use delivery_id in session key so concurrent webhooks on the
        # same route get independent agent runs (not queued/interrupted).
        session_chat_id = f"webhook:{route_name}:{delivery_id}"

        # Store delivery info for send().  Read by every send() invocation
        # for this chat_id (interim status messages and the final response),
        # so we do NOT pop on send.  TTL-based cleanup keeps the dict bounded.
        deliver_config = {
            "deliver": route_config.get("deliver", "log"),
            "deliver_extra": self._render_delivery_extra(
                route_config.get("deliver_extra", {}), payload
            ),
        }
        self._delivery_info[session_chat_id] = deliver_config
        self._delivery_info_created[session_chat_id] = now
        self._prune_delivery_info(now)

        # Build source and event
        source = self.build_source(
            chat_id=session_chat_id,
            chat_name=f"webhook/{route_name}",
            chat_type="webhook",
            user_id=f"webhook:{route_name}",
            user_name=route_name,
        )

        def _register_approval_rails() -> Optional[str]:
            """Register push/PR/merge denial only for runs accepted for spawn."""
            # The key is computed with the SAME build_session_key call the dispatcher
            # uses (gateway/platforms/base.py), so the approval contextvar key at
            # tool-exec time matches this registration and the deny actually fires.
            # Cleared at true end-of-run from on_processing_complete.
            try:
                from tools.approval import register_session_deny_patterns
                _deny_patterns = list(DEFAULT_WEBHOOK_DENY_PATTERNS)
                _route_deny = route_config.get("deny_terminal_patterns")
                if isinstance(_route_deny, list):
                    _deny_patterns.extend(str(p) for p in _route_deny)
                _approval_key = self._build_session_key(source)
                register_session_deny_patterns(_approval_key, _deny_patterns)
                return _approval_key
            except Exception:
                # Fail loud but do not crash the dispatch — prompt-level guards remain
                # as defense-in-depth if server-side registration somehow fails.
                logger.exception(
                    "[webhook] failed to register deny_terminal_patterns for route=%s",
                    route_name,
                )
                return None

        # DISP-5: arm the unconditional push/PR/workflow floor for THIS dispatch.
        # mark_autonomous_dispatch() sets a contextvar that asyncio.create_task()
        # (below) copies into the run task, and _run_in_executor_with_context
        # preserves it across the executor-thread hop into tool-exec time. So the
        # floor in check_session_deny_patterns fails CLOSED on git push / gh pr /
        # gh workflow run even if the deny-list registration above was skipped or
        # the session key mismatched. Interactive Discord/Telegram sessions enter
        # via a different handler and never reach here, so they are unaffected.
        try:
            from tools.approval import mark_autonomous_dispatch
            mark_autonomous_dispatch(True)
        except Exception:
            logger.exception(
                "[webhook] failed to arm autonomous-dispatch floor route=%s",
                route_name,
            )

        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=delivery_id,
        )

        logger.info(
            "[webhook] %s event=%s route=%s prompt_len=%d delivery=%s",
            request.method,
            event_type,
            route_name,
            len(prompt),
            delivery_id,
        )

        # Non-blocking — return 202 Accepted immediately.  The per-delivery
        # session is closed by the ``on_processing_complete`` override below
        # once the agent run actually finishes (``handle_message`` itself is
        # fire-and-forget: it spawns ``_process_message_background`` and
        # returns before the run starts, so nothing can be closed here).
        #
        # Acquire capacity BEFORE spawning the background task. A saturated
        # semaphore means the gateway is already at its configured in-flight
        # agent-run ceiling, so reject with Retry-After instead of creating an
        # unbounded task that can starve the async event loop.
        if self._agent_run_semaphore.locked():
            retry_after = "30"
            logger.warning(
                "[webhook] rejecting run: max_concurrent_agent_runs reached (%d) route=%s delivery=%s",
                self._max_concurrent_agent_runs,
                route_name,
                delivery_id,
            )
            self._seen_deliveries.pop(delivery_id, None)
            return web.json_response(
                {
                    "status": "rate_limited",
                    "error": "max_concurrent_agent_runs_exhausted",
                    "retry_after": int(retry_after),
                    "delivery_id": delivery_id,
                },
                status=429,
                headers={"Retry-After": retry_after},
            )
        await self._agent_run_semaphore.acquire()

        # Phase 3/F4 worktree preflight must happen after capacity acquisition but
        # before task creation; release the slot if setup refuses the run.
        _wt_for_run: Optional[str] = None
        _lease_info: Optional[dict] = None
        if self._wt_enabled:
            if self._per_delivery_wt_enabled:
                try:
                    _lease_info = await asyncio.to_thread(
                        self._allocate_per_delivery_worktree, route_name, delivery_id
                    )
                    _wt_for_run = str(_lease_info["path"])
                    _session_key = self._build_session_key(source)
                    if not self._verify_per_delivery_adoption(
                        session_key=_session_key,
                        worktree_path=_wt_for_run,
                    ):
                        self._agent_run_semaphore.release()
                        self._seen_deliveries.pop(delivery_id, None)
                        self._refuse_worktree_lease(
                            _lease_info,
                            reason="adoption_mismatch",
                        )
                        logger.error(
                            "[webhook] per-delivery worktree adoption mismatch; refusing run route=%s delivery=%s session=%s worktree=%s",
                            route_name,
                            delivery_id,
                            _session_key,
                            _wt_for_run,
                        )
                        return web.json_response(
                            {"status": "error", "error": "worktree_unavailable",
                             "delivery_id": delivery_id},
                            status=503,
                        )
                except Exception as exc:
                    self._agent_run_semaphore.release()
                    self._seen_deliveries.pop(delivery_id, None)
                    from agent.worktree_broker import LeaseCapacityError
                    if isinstance(exc, LeaseCapacityError):
                        retry_after = "30"
                        return web.json_response(
                            {
                                "status": "rate_limited",
                                "error": "worktree_lease_capacity_exhausted",
                                "retry_after": int(retry_after),
                                "delivery_id": delivery_id,
                            },
                            status=429,
                            headers={"Retry-After": retry_after},
                        )
                    if _lease_info is not None:
                        self._refuse_worktree_lease(
                            _lease_info,
                            reason="post_allocation_exception",
                        )
                    logger.error(
                        "[webhook] per-delivery worktree unavailable; refusing run route=%s delivery=%s: %s",
                        route_name, delivery_id, exc,
                    )
                    return web.json_response(
                        {"status": "error", "error": "worktree_unavailable",
                         "delivery_id": delivery_id},
                        status=503,
                    )
            else:
                _wt_for_run = await asyncio.to_thread(self._ensure_relay_worktree)
                if _wt_for_run is None:
                    self._agent_run_semaphore.release()
                    self._seen_deliveries.pop(delivery_id, None)
                    logger.error(
                        "[webhook] relay worktree unavailable; refusing run route=%s delivery=%s",
                        route_name, delivery_id,
                    )
                    return web.json_response(
                        {"status": "error", "error": "worktree_unavailable",
                         "delivery_id": delivery_id},
                        status=503,
                    )

        async def _run_with_backpressure() -> None:
            # All pre-spawn refusal gates have passed; only now bind approval
            # rails to this accepted run, using the same key finalization clears.
            _approval_key = _register_approval_rails()
            _finalizer_key = self._register_run_finalizer(event, _approval_key, _lease_info)
            from agent.codex_session_context import (
                get_runtime_execution_cwd_recorder,
                reset_runtime_execution_cwds,
                restore_runtime_execution_cwds,
            )
            _cwd_audit_token = reset_runtime_execution_cwds()
            self._record_run_execution_cwds(_finalizer_key, get_runtime_execution_cwd_recorder())
            try:
                # Phase 3: when HERMES_WEBHOOK_WORKTREE is on, bind the run to the relay
                # git worktree so it works on a branch, not the live tree. If a worktree
                # can't be guaranteed, REFUSE (503) — NEVER fall through to the live cwd.
                if self._wt_enabled:
                    if self._per_delivery_wt_enabled and _wt_for_run is None:
                        self._seen_deliveries.pop(delivery_id, None)
                        raise RuntimeError("per-delivery worktree binding missing after verified preflight")
                    from agent.codex_session_context import (
                        set_active_worktree, reset_active_worktree,
                    )
                    _tok = set_active_worktree(_wt_for_run)
                    try:
                        await self.handle_message(event)
                    finally:
                        reset_active_worktree(_tok)
                else:
                    await self.handle_message(event)
            except Exception:
                self._finalize_run(_finalizer_key)
                raise
            finally:
                restore_runtime_execution_cwds(_cwd_audit_token)
                # handle_message returns at spawn; run-end finalization is owned by on_processing_complete.
                if _finalizer_key in self._run_finalizers and _finalizer_key not in self._session_tasks:
                    self._finalize_run(_finalizer_key)

        task = asyncio.create_task(_run_with_backpressure())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return web.json_response(
            {
                "status": "accepted",
                "route": route_name,
                "event": event_type,
                "delivery_id": delivery_id,
            },
            status=202,
        )

    async def on_processing_complete(
        self, event: "MessageEvent", outcome: Any
    ) -> None:
        """Close the per-delivery webhook session once its run finishes.

        A webhook delivery is one-shot: the ``delivery_id`` is baked into the
        session key, so the session will never receive a second turn.  Mirror
        the cron completion path (``cron/scheduler.py`` →
        ``end_session(..., "cron_complete")``) by marking the session ended
        when the run completes.  Without this, webhook sessions keep
        ``ended_at`` NULL forever; ``SessionDB.prune_sessions`` only reaps
        rows with ``ended_at`` set, so unclosed webhook sessions accumulate
        unbounded and drive state.db bloat (the ghost-session leak).

        This hook is the one seam that runs at the TRUE end of the run:
        ``BasePlatformAdapter._process_message_background`` fires it after the
        message handler returns, on the success, failure, and cancellation
        paths alike — so error runs are reaped too.  (``handle_message`` is
        fire-and-forget; wrapping IT closes before the run even starts.)
        ``end_session()`` is first-reason-wins and no-ops on an already-ended
        row, so this never clobbers a ``compression``/``agent_close`` reason.

        Fork: this is also where webhook run backpressure is released (the
        agent-run semaphore and any per-delivery worktree lease).  Both sides
        own this hook, so both run here — the release sits in ``finally`` so a
        failing session close can never wedge the adapter at
        ``max_concurrent_agent_runs``.
        """
        try:
            await self._end_webhook_session(event, event.source.chat_id)
        finally:
            self._release_run_backpressure(event)
            await super().on_processing_complete(event, outcome)

    async def _end_webhook_session(
        self, event: "MessageEvent", session_chat_id: str
    ) -> None:
        """Mark the per-delivery webhook session ended in state.db.

        Resolves the persisted ``session_id`` from the gateway session store
        using the SAME source the run was keyed on (so profile multiplexing
        and key construction match exactly), then closes it via the existing
        ``SessionDB.end_session`` API — never a hand-written UPDATE.
        """
        runner = self.gateway_runner
        if runner is None:
            return
        session_db = getattr(runner, "_session_db", None)
        store = getattr(runner, "session_store", None)
        if session_db is None or store is None:
            return
        try:
            key_fn = getattr(runner, "_session_key_for_source", None)
            if key_fn is None:
                return
            session_key = key_fn(event.source)
            # Resolve the persisted session_id via the store's public,
            # lock-held accessor (peek_session_id) rather than reaching into
            # the private _entries dict without the store lock. Fall back to
            # the private path only for older stores / test doubles that
            # predate the accessor.
            peek = getattr(store, "peek_session_id", None)
            if callable(peek):
                session_id = peek(session_key)
            else:
                if hasattr(store, "_ensure_loaded"):
                    try:
                        store._ensure_loaded()
                    except Exception:
                        pass
                entries = getattr(store, "_entries", {}) or {}
                entry = entries.get(session_key)
                session_id = getattr(entry, "session_id", None) if entry else None
            if not session_id:
                logger.debug(
                    "[webhook] No session_id to close for %s (key=%s)",
                    session_chat_id,
                    session_key,
                )
                return
            # AsyncSessionDB forwards end_session via asyncio.to_thread; a
            # plain SessionDB exposes it synchronously.  Handle both.
            _end = session_db.end_session
            result = _end(session_id, "webhook_complete")
            if asyncio.iscoroutine(result):
                await result
            logger.debug(
                "[webhook] Closed session %s for delivery %s",
                session_id,
                session_chat_id,
            )
        except Exception as e:
            logger.debug(
                "[webhook] Failed to close session for %s: %s",
                session_chat_id,
                e,
            )

    # ------------------------------------------------------------------
    # Signature validation
    # ------------------------------------------------------------------

    def _validate_signature(
        self, request: "web.Request", body: bytes, secret: str
    ) -> bool:
        """Validate webhook signature (GitHub, GitLab, Svix, generic HMAC-SHA256)."""
        def _header(name: str) -> str:
            return (
                request.headers.get(name, "")
                or request.headers.get(name.lower(), "")
                or request.headers.get(name.upper(), "")
            )

        # Svix / AgentMail:
        #   svix-id: msg_...
        #   svix-timestamp: unix seconds
        #   svix-signature: v1,<base64-hmac> [v1,<base64-hmac> ...]
        # Signed content is: "{id}.{timestamp}.{raw_body}".  Svix secrets
        # usually start with "whsec_" and the remainder is base64-encoded.
        svix_id = _header("svix-id")
        svix_timestamp = _header("svix-timestamp")
        svix_signature = _header("svix-signature")
        if svix_id or svix_timestamp or svix_signature:
            return self._validate_svix_signature(
                body=body,
                secret=secret,
                msg_id=svix_id,
                timestamp=svix_timestamp,
                signature_header=svix_signature,
            )

        # GitHub: X-Hub-Signature-256 = sha256=<hex>
        gh_sig = request.headers.get("X-Hub-Signature-256", "")
        if gh_sig:
            expected = "sha256=" + hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            return _hmac_str_equal(gh_sig, expected)

        # GitLab: X-Gitlab-Token = <plain secret>
        gl_token = request.headers.get("X-Gitlab-Token", "")
        if gl_token:
            return _hmac_str_equal(gl_token, secret)

        # Generic V2: X-Webhook-Signature-V2 = <hex HMAC-SHA256 of "<timestamp>.<body>">
        #             X-Webhook-Timestamp = <unix seconds> (required for V2)
        # Checked independently of (and before) legacy V1 below — a sender
        # that only ever sends V2 headers must still validate here; nesting
        # this inside `if generic_sig:` would silently skip V2-only senders.
        #
        # The presence of X-Webhook-Signature-V2 alone selects V2 mode and
        # commits to it — it must NOT fall through to the V1 branch just
        # because the timestamp is missing/malformed/expired. A sender
        # migrating to V2 typically sends both V1 and V2 headers together
        # for compatibility; if incomplete V2 fell through to V1, an
        # attacker who captured one such mixed request could strip the
        # X-Webhook-Timestamp header from a replay and have it validate
        # against the still-present, still-unprotected V1 signature instead
        # — silently downgrading a V2-protected request back to the replay
        # hole V2 exists to close.
        v2_sig = request.headers.get("X-Webhook-Signature-V2", "")
        if v2_sig:
            v2_timestamp = request.headers.get("X-Webhook-Timestamp", "")
            if not v2_timestamp:
                logger.warning(
                    "[webhook] Route '%s' sent X-Webhook-Signature-V2 with "
                    "no X-Webhook-Timestamp — rejecting rather than "
                    "falling back to legacy V1",
                    request.match_info.get("route_name", ""),
                )
                return False
            try:
                ts = int(v2_timestamp)
            except (TypeError, ValueError):
                return False
            if abs(int(time.time()) - ts) > 300:
                logger.warning(
                    "[webhook] Route '%s' generic HMAC V2 timestamp outside replay window",
                    request.match_info.get("route_name", ""),
                )
                return False
            signed_content = v2_timestamp.encode() + b"." + body
            expected_v2 = hmac.new(
                secret.encode(), signed_content, hashlib.sha256
            ).hexdigest()
            return _hmac_str_equal(v2_sig, expected_v2)

        # Generic V1 (legacy): X-Webhook-Signature = <hex HMAC-SHA256 of body>
        # (deprecated — no replay protection, since the signature only
        # covers the body: a captured (body, signature) pair replays
        # indefinitely with no timestamp binding it to a specific delivery.)
        # Only reachable when X-Webhook-Signature-V2 was not sent at all —
        # see the guard above.
        generic_sig = request.headers.get("X-Webhook-Signature", "")
        if generic_sig:
            expected = hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            route_name = request.match_info.get("route_name", "")
            if route_name not in self._v1_signature_warned:
                self._v1_signature_warned.add(route_name)
                logger.warning(
                    "[webhook] Route '%s' uses legacy body-only HMAC (no "
                    "timestamp), which is vulnerable to replay attacks. Add "
                    "an 'X-Webhook-Timestamp' header and switch to "
                    "'X-Webhook-Signature-V2' (HMAC-SHA256 of "
                    "'<timestamp>.<body>').",
                    route_name,
                )
            return _hmac_str_equal(generic_sig, expected)

        # No recognised signature header but secret is configured → reject
        logger.debug(
            "[webhook] Secret configured but no signature header found"
        )
        return False

    def _validate_svix_signature(
        self,
        body: bytes,
        secret: str,
        msg_id: str,
        timestamp: str,
        signature_header: str,
        tolerance_seconds: int = 300,
    ) -> bool:
        """Validate Svix-compatible signatures used by AgentMail webhooks."""
        if not (msg_id and timestamp and signature_header and secret):
            return False

        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - ts) > tolerance_seconds:
            logger.warning("[webhook] Svix signature timestamp outside replay window")
            return False

        if secret.startswith("whsec_"):
            encoded_secret = secret.removeprefix("whsec_")
            try:
                key = base64.b64decode(encoded_secret, validate=True)
            except (binascii.Error, ValueError):
                logger.debug("[webhook] Invalid whsec_ Svix signing secret")
                return False
        else:
            # Be permissive for providers that document Svix-style headers but
            # hand out raw shared secrets rather than whsec_ base64 secrets.
            logger.debug("[webhook] Validating Svix-style signature with raw secret")
            key = secret.encode()

        signed_content = msg_id.encode() + b"." + timestamp.encode() + b"." + body
        expected = base64.b64encode(
            hmac.new(key, signed_content, hashlib.sha256).digest()
        ).decode()

        # Svix can send multiple signatures separated by spaces during secret
        # rotation. Each entry is formatted as "vN,<base64>".
        for part in signature_header.split():
            try:
                version, signature = part.split(",", 1)
            except ValueError:
                continue
            if version == "v1" and _hmac_str_equal(signature, expected):
                return True
        return False

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def _render_prompt(
        self,
        template: str,
        payload: dict,
        event_type: str,
        route_name: str,
    ) -> str:
        """Render a prompt template with the webhook payload.

        Supports dot-notation access into nested dicts:
        ``{pull_request.title}`` → ``payload["pull_request"]["title"]``

        Special token ``{__raw__}`` dumps the entire payload as indented
        JSON (truncated to 4000 chars).  Useful for monitoring alerts or
        any webhook where the agent needs to see the full payload.
        """
        if not template:
            truncated = json.dumps(payload, indent=2)[:4000]
            return (
                f"Webhook event '{event_type}' on route "
                f"'{route_name}':\n\n```json\n{truncated}\n```"
            )

        def _resolve(match: re.Match) -> str:
            key = match.group(1)
            # Special token: dump the entire payload as JSON
            if key == "__raw__":
                return json.dumps(payload, indent=2)[:4000]
            if key == "event_type":
                return event_type
            value: Any = payload
            for part in key.split("."):
                if isinstance(value, dict):
                    value = value.get(part, f"{{{key}}}")
                else:
                    return f"{{{key}}}"
            if isinstance(value, (dict, list)):
                return json.dumps(value, indent=2)[:2000]
            return str(value)

        return re.sub(r"\{([a-zA-Z0-9_.]+)\}", _resolve, template)

    def _render_delivery_extra(
        self, extra: dict, payload: dict
    ) -> dict:
        """Render delivery_extra template values with payload data."""
        rendered: Dict[str, Any] = {}
        for key, value in extra.items():
            if isinstance(value, str):
                rendered[key] = self._render_prompt(value, payload, "", "")
            else:
                rendered[key] = value
        return rendered

    # ------------------------------------------------------------------
    # Response delivery
    # ------------------------------------------------------------------

    async def _direct_deliver(
        self, content: str, delivery: dict
    ) -> SendResult:
        """Deliver *content* directly without invoking the agent.

        Used by ``deliver_only`` routes: the rendered template becomes the
        literal message body, and we dispatch to the same delivery helpers
        that the agent-mode ``send()`` flow uses.  All target types that
        work in agent mode work here — Telegram, Discord, Slack, GitHub
        PR comments, etc.
        """
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            # Shouldn't reach here — startup validation rejects deliver_only
            # with deliver=log — but guard defensively.
            logger.info("[webhook] direct-deliver log-only: %s", content[:200])
            return SendResult(success=True)

        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)

        # Fall through to the cross-platform dispatcher, which validates the
        # target name and routes via the gateway runner.
        return await self._deliver_cross_platform(
            deliver_type, content, delivery
        )

    async def _deliver_github_comment(
        self, content: str, delivery: dict
    ) -> SendResult:
        """Post agent response as a GitHub PR/issue comment via ``gh`` CLI."""
        extra = delivery.get("deliver_extra", {})
        repo = extra.get("repo", "")
        pr_number = extra.get("pr_number", "")

        if not repo or not pr_number:
            logger.error(
                "[webhook] github_comment delivery missing repo or pr_number"
            )
            return SendResult(
                success=False, error="Missing repo or pr_number"
            )

        # --- Input validation (prevent CLI argument injection) ---
        # pr_number must be a positive integer.
        try:
            pr_int = int(pr_number)
            if pr_int <= 0:
                raise ValueError("non-positive")
        except (ValueError, TypeError):
            logger.error(
                "[webhook] invalid pr_number: %r", pr_number
            )
            return SendResult(
                success=False, error="Invalid pr_number"
            )

        # repo must match owner/name (alphanumeric, hyphens, underscores, dots).
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
            logger.error("[webhook] invalid repo format: %r", repo)
            return SendResult(
                success=False, error="Invalid repo format"
            )

        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "comment",
                    str(pr_int),
                    "--repo",
                    repo,
                    "--body",
                    content,
                ],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=30,
            )
            if result.returncode == 0:
                logger.info(
                    "[webhook] Posted comment on %s#%s", repo, pr_number
                )
                return SendResult(success=True)
            else:
                logger.error(
                    "[webhook] gh pr comment failed: %s", result.stderr
                )
                return SendResult(success=False, error=result.stderr)
        except FileNotFoundError:
            logger.error(
                "[webhook] 'gh' CLI not found — install GitHub CLI for "
                "github_comment delivery"
            )
            return SendResult(
                success=False, error="gh CLI not installed"
            )
        except Exception as e:
            logger.error("[webhook] github_comment delivery error: %s", e)
            return SendResult(success=False, error=str(e))

    async def _deliver_cross_platform(
        self, platform_name: str, content: str, delivery: dict
    ) -> SendResult:
        """Route response to another platform (telegram, discord, etc.)."""
        if not self.gateway_runner:
            return SendResult(
                success=False,
                error="No gateway runner for cross-platform delivery",
            )

        try:
            target_platform = Platform(platform_name)
        except ValueError:
            return SendResult(
                success=False, error=f"Unknown platform: {platform_name}"
            )

        # Default adapters first; multiplex may park Slack/etc. only on a
        # secondary profile (self._profile_adapters). Fall back so webhook
        # deliver:slack still works when default has slack disabled.
        adapter = self.gateway_runner.adapters.get(target_platform)
        if not adapter:
            for _prof, amap in (getattr(self.gateway_runner, "_profile_adapters", None) or {}).items():
                if not isinstance(amap, dict):
                    continue
                cand = amap.get(target_platform)
                if cand is not None:
                    adapter = cand
                    break
        if not adapter:
            return SendResult(
                success=False,
                error=f"Platform {platform_name} not connected",
            )

        # Use home channel if no specific chat_id in deliver_extra
        extra = delivery.get("deliver_extra", {})
        chat_id = extra.get("chat_id", "")
        if not chat_id:
            home = self.gateway_runner.config.get_home_channel(target_platform)
            if home:
                chat_id = home.chat_id
            else:
                return SendResult(
                    success=False,
                    error=f"No chat_id or home channel for {platform_name}",
                )

        # Pass thread_id from deliver_extra so Telegram forum topics work
        metadata = None
        thread_id = extra.get("message_thread_id") or extra.get("thread_id")
        if thread_id:
            metadata = {"thread_id": thread_id}

        return await adapter.send(chat_id, content, metadata=metadata)
