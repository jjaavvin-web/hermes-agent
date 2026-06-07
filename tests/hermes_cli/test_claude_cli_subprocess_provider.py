from hermes_cli import runtime_provider as rp


def test_resolve_runtime_provider_claude_cli_subprocess(monkeypatch):
    """The Claude CLI provider is a subprocess transport, not an HTTP/API-key route."""

    def fail_load_pool(provider):
        raise AssertionError(f"claude-cli-subprocess must not load credential pool: {provider}")

    monkeypatch.setattr(rp, "load_pool", fail_load_pool)

    resolved = rp.resolve_runtime_provider(requested="claude-cli-subprocess")

    assert resolved == {
        "provider": "claude-cli-subprocess",
        "api_mode": "claude_cli_subprocess",
        "base_url": "",
        "api_key": "***",
        "source": "claude-cli-subprocess",
        "requested_provider": "claude-cli-subprocess",
    }


def test_parse_api_mode_accepts_claude_cli_subprocess():
    assert "claude_cli_subprocess" in rp._VALID_API_MODES
    assert rp._parse_api_mode("claude_cli_subprocess") == "claude_cli_subprocess"


def test_cli_runtime_credentials_accepts_claude_cli_subprocess_empty_base(monkeypatch):
    """Non-HTTP subprocess runtimes intentionally have no base URL/API key route."""
    from cli import HermesCLI
    from hermes_cli import runtime_provider as rp

    cli = HermesCLI.__new__(HermesCLI)
    cli.requested_provider = "claude-cli-subprocess"
    cli._explicit_api_key = None
    cli._explicit_base_url = None
    cli._fallback_model = []
    cli.api_mode = "chat_completions"
    cli.provider = ""
    cli.acp_command = None
    cli.acp_args = []
    cli.api_key = ""
    cli.base_url = ""
    cli.agent = None
    cli.model = "claude-opus-4.8"
    cli._active_agent_route_signature = None
    cli._normalize_model_for_provider = lambda provider: False

    monkeypatch.setattr(
        rp,
        "resolve_runtime_provider",
        lambda **kwargs: {
            "provider": "claude-cli-subprocess",
            "api_mode": "claude_cli_subprocess",
            "base_url": "",
            "api_key": "***",
            "source": "claude-cli-subprocess",
            "requested_provider": "claude-cli-subprocess",
        },
    )

    assert cli._ensure_runtime_credentials() is True
    assert cli.provider == "claude-cli-subprocess"
    assert cli.api_mode == "claude_cli_subprocess"
    assert cli.base_url == ""
    assert cli.api_key == "***"
