"""Direct tests for GatewayAuthorizationMixin.

These tests construct a bare GatewayRunner the same way the existing
config-driven access-policy tests do: no network, no DB, just the mixin state
that the authorization ladder reads.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


_AUTH_ENV_VARS = (
    "TELEGRAM_ALLOWED_USERS",
    "DISCORD_ALLOWED_USERS",
    "WHATSAPP_ALLOWED_USERS",
    "WHATSAPP_CLOUD_ALLOWED_USERS",
    "SLACK_ALLOWED_USERS",
    "SIGNAL_ALLOWED_USERS",
    "EMAIL_ALLOWED_USERS",
    "SMS_ALLOWED_USERS",
    "MATTERMOST_ALLOWED_USERS",
    "MATRIX_ALLOWED_USERS",
    "DINGTALK_ALLOWED_USERS",
    "FEISHU_ALLOWED_USERS",
    "WECOM_ALLOWED_USERS",
    "WECOM_CALLBACK_ALLOWED_USERS",
    "WEIXIN_ALLOWED_USERS",
    "BLUEBUBBLES_ALLOWED_USERS",
    "QQ_ALLOWED_USERS",
    "YUANBAO_ALLOWED_USERS",
    "GATEWAY_ALLOWED_USERS",
    "TELEGRAM_ALLOW_ALL_USERS",
    "DISCORD_ALLOW_ALL_USERS",
    "WHATSAPP_ALLOW_ALL_USERS",
    "WHATSAPP_CLOUD_ALLOW_ALL_USERS",
    "SLACK_ALLOW_ALL_USERS",
    "SIGNAL_ALLOW_ALL_USERS",
    "EMAIL_ALLOW_ALL_USERS",
    "SMS_ALLOW_ALL_USERS",
    "MATTERMOST_ALLOW_ALL_USERS",
    "MATRIX_ALLOW_ALL_USERS",
    "DINGTALK_ALLOW_ALL_USERS",
    "FEISHU_ALLOW_ALL_USERS",
    "WECOM_ALLOW_ALL_USERS",
    "WECOM_CALLBACK_ALLOW_ALL_USERS",
    "WEIXIN_ALLOW_ALL_USERS",
    "BLUEBUBBLES_ALLOW_ALL_USERS",
    "QQ_ALLOW_ALL_USERS",
    "YUANBAO_ALLOW_ALL_USERS",
    "GATEWAY_ALLOW_ALL_USERS",
    "TELEGRAM_GROUP_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_CHATS",
    "QQ_GROUP_ALLOWED_USERS",
    "DISCORD_ALLOW_BOTS",
    "FEISHU_ALLOW_BOTS",
)


def _clear_auth_env(monkeypatch) -> None:
    for key in _AUTH_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch):
    _clear_auth_env(monkeypatch)


def _make_runner(
    *,
    config: GatewayConfig | None = None,
    adapters: dict[Platform, SimpleNamespace] | None = None,
):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = adapters or {}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    return runner


def _adapter(**attrs):
    return SimpleNamespace(send=AsyncMock(), **attrs)


def _source(
    platform: Platform,
    *,
    chat_type: str = "dm",
    user_id: str | None = "some-user",
    chat_id: str = "some-chat",
    is_bot: bool = False,
    role_authorized=False,
) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_name="tester" if user_id else None,
        is_bot=is_bot,
        role_authorized=role_authorized,
    )


def _config(platform: Platform, extra: dict | None = None) -> GatewayConfig:
    return GatewayConfig(platforms={platform: PlatformConfig(enabled=True, extra=extra or {})})


def test_system_event_platforms_bypass_user_authorization():
    runner = _make_runner()

    assert runner._is_user_authorized(_source(Platform.HOMEASSISTANT, user_id=None)) is True
    assert runner._is_user_authorized(_source(Platform.WEBHOOK, user_id=None)) is True


@pytest.mark.parametrize("allowed_chats", ["some-chat", "*"])
def test_telegram_group_chat_allowlist_runs_before_no_user_id_guard(allowed_chats, monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", allowed_chats)

    assert (
        runner._is_user_authorized(
            _source(Platform.TELEGRAM, chat_type="group", user_id=None, chat_id="some-chat")
        )
        is True
    )


@pytest.mark.parametrize("chat_type", ["group", "forum", "channel"])
def test_telegram_chat_scoped_allowlist_covers_group_forum_and_channel(chat_type, monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "some-chat")

    assert (
        runner._is_user_authorized(
            _source(Platform.TELEGRAM, chat_type=chat_type, user_id=None, chat_id="some-chat")
        )
        is True
    )


def test_telegram_group_chat_allowlist_miss_without_user_id_denies(monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "other-chat")

    assert (
        runner._is_user_authorized(
            _source(Platform.TELEGRAM, chat_type="group", user_id=None, chat_id="some-chat")
        )
        is False
    )


@pytest.mark.parametrize("value", ["true", "1", "yes"])
@pytest.mark.parametrize(
    ("platform", "env_key"),
    [(Platform.DISCORD, "DISCORD_ALLOW_ALL_USERS"), (Platform.TELEGRAM, "TELEGRAM_ALLOW_ALL_USERS")],
)
def test_per_platform_allow_all_truthy_values_authorize(platform, env_key, value, monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv(env_key, value)

    assert runner._is_user_authorized(_source(platform)) is True


@pytest.mark.parametrize("value", ["false", "no", "0", ""])
@pytest.mark.parametrize(
    ("platform", "env_key"),
    [(Platform.DISCORD, "DISCORD_ALLOW_ALL_USERS"), (Platform.TELEGRAM, "TELEGRAM_ALLOW_ALL_USERS")],
)
def test_per_platform_allow_all_falsey_values_do_not_authorize(platform, env_key, value, monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv(env_key, value)

    assert runner._is_user_authorized(_source(platform)) is False


def test_role_authorized_real_bool_bypasses_allowlists():
    runner = _make_runner()

    assert runner._is_user_authorized(_source(Platform.DISCORD, role_authorized=True)) is True


@pytest.mark.parametrize("role_authorized", [False, 1, MagicMock()])
def test_role_authorized_requires_exact_true_bool(role_authorized):
    runner = _make_runner()

    assert runner._is_user_authorized(_source(Platform.DISCORD, role_authorized=role_authorized)) is False


@pytest.mark.parametrize("value", ["mentions", "all"])
def test_discord_allow_bots_mentions_or_all_authorizes_bot(value, monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", value)

    assert runner._is_user_authorized(_source(Platform.DISCORD, is_bot=True)) is True


@pytest.mark.parametrize("value", [None, "none"])
def test_discord_allow_bots_unset_or_none_does_not_authorize_bot(value, monkeypatch):
    runner = _make_runner()
    if value is not None:
        monkeypatch.setenv("DISCORD_ALLOW_BOTS", value)

    assert runner._is_user_authorized(_source(Platform.DISCORD, is_bot=True)) is False


def test_allow_bots_unsupported_platform_does_not_authorize_bot(monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "all")

    assert runner._is_user_authorized(_source(Platform.TELEGRAM, is_bot=True)) is False


def test_pairing_store_approved_user_authorizes():
    runner = _make_runner()
    runner.pairing_store.is_approved.return_value = True

    assert runner._is_user_authorized(_source(Platform.DISCORD, user_id="paired-user")) is True
    runner.pairing_store.is_approved.assert_called_with("discord", "paired-user")


def test_pairing_store_false_has_no_authorization_effect():
    runner = _make_runner()
    runner.pairing_store.is_approved.return_value = False

    assert runner._is_user_authorized(_source(Platform.DISCORD, user_id="unpaired-user")) is False


def test_platform_env_allowlist_membership_match_and_miss(monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "alice,bob")

    assert runner._is_user_authorized(_source(Platform.TELEGRAM, user_id="alice")) is True
    assert runner._is_user_authorized(_source(Platform.TELEGRAM, user_id="carol")) is False


def test_platform_env_allowlist_star_authorizes_any_user(monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")

    assert runner._is_user_authorized(_source(Platform.TELEGRAM, user_id="carol")) is True


def test_global_gateway_allowed_users_match_and_miss(monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "alice,bob")

    assert runner._is_user_authorized(_source(Platform.DISCORD, user_id="alice")) is True
    assert runner._is_user_authorized(_source(Platform.DISCORD, user_id="carol")) is False


@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_global_allow_all_truthy_without_allowlists_authorizes(value, monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", value)

    assert runner._is_user_authorized(_source(Platform.DISCORD)) is True


def test_global_allow_all_unset_falls_to_default_deny():
    runner = _make_runner()

    assert runner._is_user_authorized(_source(Platform.DISCORD)) is False


def test_own_policy_dm_allowlist_authorizes_without_env_allowlists():
    runner = _make_runner(
        config=_config(Platform.WECOM, {"dm_policy": "allowlist"}),
        adapters={Platform.WECOM: _adapter(enforces_own_access_policy=True)},
    )

    assert runner._is_user_authorized(_source(Platform.WECOM)) is True


def test_own_policy_open_dm_does_not_authorize_without_env_allowlists():
    runner = _make_runner(
        config=_config(Platform.WECOM, {"dm_policy": "open"}),
        adapters={Platform.WECOM: _adapter(enforces_own_access_policy=True)},
    )

    assert runner._is_user_authorized(_source(Platform.WECOM)) is False


def test_own_policy_group_sender_allowlist_authorizes_without_env_allowlists():
    runner = _make_runner(
        config=_config(Platform.WECOM, {"group_policy": "open"}),
        adapters={
            Platform.WECOM: _adapter(
                enforces_own_access_policy=True,
                _groups={"some-chat": {"allow_from": "alice"}},
            )
        },
    )

    assert runner._is_user_authorized(_source(Platform.WECOM, chat_type="group")) is True


def test_default_deny_when_nothing_matches():
    runner = _make_runner(config=None, adapters={})
    runner.pairing_store.is_approved.return_value = False

    assert runner._is_user_authorized(_source(Platform.DISCORD, user_id="ordinary-user")) is False


def test_no_user_id_guard_denies_dm_without_chat_scoped_match():
    runner = _make_runner()

    assert runner._is_user_authorized(_source(Platform.TELEGRAM, user_id=None, chat_type="dm")) is False


def test_adapter_enforces_own_access_policy_cases():
    runner = _make_runner(adapters={Platform.WECOM: _adapter(enforces_own_access_policy=True)})
    runner_without_flag = _make_runner(adapters={Platform.WECOM: _adapter()})
    runner_false_flag = _make_runner(adapters={Platform.WECOM: _adapter(enforces_own_access_policy=False)})

    assert runner._adapter_enforces_own_access_policy(None) is False
    assert _make_runner(adapters={})._adapter_enforces_own_access_policy(Platform.WECOM) is False
    assert runner._adapter_enforces_own_access_policy(Platform.WECOM) is True
    assert runner_false_flag._adapter_enforces_own_access_policy(Platform.WECOM) is False
    assert runner_without_flag._adapter_enforces_own_access_policy(Platform.WECOM) is False


def test_adapter_dm_policy_prefers_live_adapter_then_config_then_default():
    runner = _make_runner(
        config=_config(Platform.WECOM, {"dm_policy": " disabled "}),
        adapters={Platform.WECOM: _adapter(_dm_policy=" AllowList ")},
    )
    config_runner = _make_runner(config=_config(Platform.WECOM, {"dm_policy": " Disabled "}))
    empty_runner = _make_runner(config=GatewayConfig())

    assert runner._adapter_dm_policy(None) == ""
    assert runner._adapter_dm_policy(Platform.WECOM) == "allowlist"
    assert config_runner._adapter_dm_policy(Platform.WECOM) == "disabled"
    assert empty_runner._adapter_dm_policy(Platform.WECOM) == ""


def test_adapter_group_policy_prefers_live_adapter_then_config_then_default():
    runner = _make_runner(
        config=_config(Platform.WECOM, {"group_policy": " disabled "}),
        adapters={Platform.WECOM: _adapter(_group_policy=" AllowList ")},
    )
    config_runner = _make_runner(config=_config(Platform.WECOM, {"group_policy": " Disabled "}))
    empty_runner = _make_runner(config=GatewayConfig())

    assert runner._adapter_group_policy(None) == ""
    assert runner._adapter_group_policy(Platform.WECOM) == "allowlist"
    assert config_runner._adapter_group_policy(Platform.WECOM) == "disabled"
    assert empty_runner._adapter_group_policy(Platform.WECOM) == ""


@pytest.mark.parametrize("allow_from", ["alice", ["alice"], ("alice",), {"alice"}])
def test_adapter_group_has_sender_allowlist_accepts_non_empty_string_or_collection(allow_from):
    runner = _make_runner(
        adapters={Platform.WECOM: _adapter(_groups={"some-chat": {"allow_from": allow_from}})}
    )

    assert runner._adapter_group_has_sender_allowlist(Platform.WECOM, "some-chat") is True


@pytest.mark.parametrize("group_cfg", [{"allow_from": ""}, {"allow_from": []}, {}, {"allowFrom": ""}])
def test_adapter_group_has_sender_allowlist_rejects_empty_or_absent_allow_from(group_cfg):
    runner = _make_runner(adapters={Platform.WECOM: _adapter(_groups={"some-chat": group_cfg})})

    assert runner._adapter_group_has_sender_allowlist(Platform.WECOM, "some-chat") is False


def test_adapter_group_has_sender_allowlist_case_insensitive_key_and_star_fallback():
    case_runner = _make_runner(
        adapters={Platform.WECOM: _adapter(_groups={"Some-Chat": {"allow_from": "alice"}})}
    )
    star_runner = _make_runner(
        adapters={Platform.WECOM: _adapter(_groups={"*": {"allow_from": ["alice"]}})}
    )

    assert case_runner._adapter_group_has_sender_allowlist(Platform.WECOM, "some-chat") is True
    assert star_runner._adapter_group_has_sender_allowlist(Platform.WECOM, "missing-chat") is True


def test_adapter_group_has_sender_allowlist_config_fallback_and_none_guards():
    runner = _make_runner(
        config=_config(Platform.WECOM, {"groups": {"some-chat": {"allow_from": "alice"}}})
    )

    assert runner._adapter_group_has_sender_allowlist(None, "some-chat") is False
    assert runner._adapter_group_has_sender_allowlist(Platform.WECOM, None) is False
    assert runner._adapter_group_has_sender_allowlist(Platform.WECOM, "some-chat") is True


def test_unauthorized_dm_behavior_clean_env_no_config_pairs():
    runner = _make_runner(config=None)

    assert runner._get_unauthorized_dm_behavior(Platform.TELEGRAM) == "pair"


@pytest.mark.parametrize("env_key", ["TELEGRAM_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS"])
def test_unauthorized_dm_behavior_allowlist_env_ignores(env_key, monkeypatch):
    runner = _make_runner(config=None)
    monkeypatch.setenv(env_key, "alice")

    assert runner._get_unauthorized_dm_behavior(Platform.TELEGRAM) == "ignore"


def test_unauthorized_dm_behavior_group_allowlist_env_ignores(monkeypatch):
    runner = _make_runner(config=None)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "some-chat")

    assert runner._get_unauthorized_dm_behavior(Platform.TELEGRAM) == "ignore"


def test_unauthorized_dm_behavior_per_platform_override_wins_over_global_and_policy():
    runner = _make_runner(
        config=GatewayConfig(
            unauthorized_dm_behavior="ignore",
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    extra={"unauthorized_dm_behavior": "pair", "dm_policy": "allowlist"},
                )
            },
        )
    )

    assert runner._get_unauthorized_dm_behavior(Platform.TELEGRAM) == "pair"


def test_unauthorized_dm_behavior_global_override_wins_without_platform_override():
    runner = _make_runner(config=GatewayConfig(unauthorized_dm_behavior="ignore"))

    assert runner._get_unauthorized_dm_behavior(Platform.TELEGRAM) == "ignore"


@pytest.mark.parametrize(
    ("dm_policy", "expected"),
    [("pairing", "pair"), ("allowlist", "ignore"), ("disabled", "ignore")],
)
def test_unauthorized_dm_behavior_follows_dm_policy_when_no_explicit_override(dm_policy, expected):
    runner = _make_runner(config=_config(Platform.WECOM, {"dm_policy": dm_policy}))

    assert runner._get_unauthorized_dm_behavior(Platform.WECOM) == expected

