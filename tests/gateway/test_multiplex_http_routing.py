"""Phase 1: HTTP-inbound /p/<profile>/ routing for the webhook adapter."""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.profile_routing import ProfileRouteRejected
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class TestSessionSourceProfileField:
    def test_profile_roundtrips(self):
        s = SessionSource(
            platform=Platform.WEBHOOK if hasattr(Platform, "WEBHOOK") else Platform.TELEGRAM,
            chat_id="c1",
            chat_type="webhook",
            profile="coder",
        )
        restored = SessionSource.from_dict(s.to_dict())
        assert restored.profile == "coder"


class TestWebhookProfileResolution:
    """_resolve_request_profile validates the /p/<profile>/ prefix."""

    def _adapter(
        self,
        multiplex: bool,
        served=("default", "coder"),
        allowlist=None,
    ):
        from gateway.platforms.webhook import WebhookAdapter, _PROFILE_REJECTED

        class _FakeReq:
            def __init__(self, profile):
                self.match_info = {"profile": profile} if profile is not None else {}

        cfg = GatewayConfig(
            multiplex_profiles=multiplex,
            multiplex_profile_allowlist=allowlist,
        )

        class _Runner:
            config = cfg

        # Construct minimally; we only call _resolve_request_profile.
        adapter = WebhookAdapter.__new__(WebhookAdapter)
        adapter.gateway_runner = _Runner()
        return adapter, _FakeReq, _PROFILE_REJECTED, served

    def test_no_prefix_returns_none(self):
        adapter, Req, _REJ, _ = self._adapter(multiplex=True)
        assert adapter._resolve_request_profile(Req(None)) is None

    def test_unserved_prefix_is_rejected(self, monkeypatch):
        adapter, Req, rejected, served = self._adapter(
            multiplex=True,
            served=("default", "worker"),
            allowlist=["worker"],
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex, profile_allowlist=None: [
                (name, f"/profiles/{name}") for name in served
            ],
        )

        assert adapter._resolve_request_profile(Req("worker")) == "worker"
        assert adapter._resolve_request_profile(Req("restricted")) is rejected


class TestNamedProfileRuntimeBinding:
    @pytest.mark.asyncio
    async def test_primary_webhook_handler_enters_selected_profile_scope(
        self, monkeypatch
    ):
        """The shared primary HTTP adapter must not force named routes to default."""
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        selected_home = Path("/profiles/worker")
        entered = []

        @contextmanager
        def _record_scope(profile_home):
            entered.append(Path(profile_home))
            yield

        monkeypatch.setattr(gateway_run, "_profile_runtime_scope", _record_scope)
        runner._resolve_profile_home_for_source = lambda source: selected_home

        seen = {}

        async def _handle(event):
            seen["profile"] = event.source.profile
            return "ok"

        runner._handle_message = _handle
        source = SessionSource(
            platform=Platform.WEBHOOK,
            chat_id="webhook:route:delivery",
            chat_type="webhook",
            profile="worker",
        )
        event = type("Event", (), {"source": source})()

        result = await runner._make_default_profile_message_handler()(event)

        assert result == "ok"
        assert seen["profile"] == "worker"
        assert entered == [selected_home]

    @pytest.mark.asyncio
    async def test_named_webhook_profile_disappearing_before_dispatch_fails_closed(
        self, monkeypatch
    ):
        """A validated named URL must never fall back to the default profile."""
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        runner._handle_message = MagicMock()
        source = SessionSource(
            platform=Platform.WEBHOOK,
            chat_id="webhook:route:delivery",
            chat_type="webhook",
            profile="worker",
        )
        event = type("Event", (), {"source": source})()

        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir",
            MagicMock(return_value=Path("/profiles/worker")),
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", MagicMock(return_value=False)
        )

        with pytest.raises(ProfileRouteRejected, match="worker"):
            await runner._make_default_profile_message_handler()(event)

        runner._handle_message.assert_not_called()

    def test_named_profile_home_channel_resolves_under_exact_profile_scope(
        self, monkeypatch
    ):
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        selected_home = Path("/profiles/worker")
        entered = []

        @contextmanager
        def _record_scope(profile_home):
            entered.append(Path(profile_home))
            yield

        worker_channel = MagicMock(chat_id="worker-home")
        worker_config = MagicMock()
        worker_config.get_home_channel.return_value = worker_channel
        monkeypatch.setattr(gateway_run, "_profile_runtime_scope", _record_scope)
        monkeypatch.setattr(
            gateway_run,
            "_multiplex_profile_homes",
            lambda config: [("default", Path("/default")), ("worker", selected_home)],
        )
        monkeypatch.setattr(
            "gateway.config.load_gateway_config", lambda: worker_config
        )

        resolved = runner._home_channel_for_profile(Platform.TELEGRAM, "worker")

        assert resolved is worker_channel
        assert entered == [selected_home]
        worker_config.get_home_channel.assert_called_once_with(Platform.TELEGRAM)


