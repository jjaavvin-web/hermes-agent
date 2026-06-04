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
