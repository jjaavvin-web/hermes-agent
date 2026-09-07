"""Local fixture tests: no production config, Discord traffic or MCP processes."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from tools import slash_confirm


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    from gateway import mcp_include_operation as op
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from gateway.run import GatewayRunner
    from hermes_cli import config
    path = tmp_path / 'config.yaml'
    cfg = {'approvals': {'mode': 'smart', 'mcp_reload_confirm': False},
           'mcp_servers': {'mvms': {'command': 'fixture-reader', 'tools': {'include': ['status']}},
                           'writer': {'command': 'fixture-writer', 'tools': {'include': ['record']}}}}
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(config, 'get_config_path', lambda: path)
    monkeypatch.setattr(op, 'runtime_names', lambda server: {'status', 'get'})
    monkeypatch.setattr(op, 'connection_failures', lambda data: [])
    runner = object.__new__(GatewayRunner)
    runner._session_key_for_source = lambda source: 'discord:room:thread'
    runner._resume_caller_is_admin = lambda source: source.user_id == 'human'
    runner._adapter_for_source = lambda source: SimpleNamespace(send_slash_confirm=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner._thread_metadata_for_source = lambda *args: None
    runner._reply_anchor_for_event = lambda event: None
    runner._execute_mcp_reload_unlocked = AsyncMock(return_value='reload fixture')
    source = SessionSource(platform=Platform.DISCORD, user_id='human', chat_id='room', thread_id='thread')
    event = MessageEvent(text='request', source=source, message_id='message')
    slash_confirm._pending.clear()
    yield SimpleNamespace(op=op, path=path, cfg=cfg, runner=runner, event=event)
    slash_confirm._pending.clear()


async def propose(f):
    result = await f.op.propose(f.runner, f.event, 'mvms', ['status', 'get'])
    pending = slash_confirm.get_pending('discord:room:thread')
    return result, pending


async def approve(p, **kw):
    return await slash_confirm.resolve('discord:room:thread', p['confirm_id'],
                                       kw.pop('choice', 'once'), actor_user_id=kw.pop('actor', 'human'), **kw)


@pytest.mark.asyncio
async def test_one_confirmation_apply_readback_duplicate(fixture):
    f = fixture
    before = f.path.read_bytes()
    result, p = await propose(f)
    assert p and 'awaiting' in result['status']
    assert f.path.read_bytes() == before
    assert 'all MCP' in result['message'] and 'status' in result['message']
    output = await approve(p)
    assert 'VERIFIED' in output
    expected = copy.deepcopy(f.cfg)
    expected['mcp_servers']['mvms']['tools']['include'] = ['status', 'get']
    assert yaml.safe_load(f.path.read_text()) == expected
    assert f.runner._execute_mcp_reload_unlocked.await_count == 1
    assert await approve(p) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('choice,actor', [('cancel', 'human'), ('always', 'human'), ('once', 'other'), ('anything', 'human')])
async def test_denial_identity_and_no_global_allow(fixture, choice, actor):
    f = fixture
    before = f.path.read_bytes()
    _, p = await propose(f)
    await approve(p, choice=choice, actor=actor)
    assert f.path.read_bytes() == before
    f.runner._execute_mcp_reload_unlocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_thread_id_and_supersession(fixture):
    f = fixture
    _, old = await propose(f)
    _, new = await propose(f)
    assert old['confirm_id'] != new['confirm_id']
    assert await approve(old) is None
    assert await slash_confirm.resolve('other-thread', new['confirm_id'], 'once', actor_user_id='human') is None
    f.runner._execute_mcp_reload_unlocked.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('elapsed,applies', [(120, True), (301, False)])
async def test_delayed_confirmation(fixture, elapsed, applies):
    _, p = await propose(fixture)
    slash_confirm._pending['discord:room:thread']['created_at'] = time.time() - elapsed
    result = await approve(p)
    assert fixture.runner._execute_mcp_reload_unlocked.await_count == int(applies)
    if not applies:
        assert result is None


@pytest.mark.asyncio
async def test_config_drift_before_first_mutation(fixture):
    f = fixture
    _, p = await propose(f)
    changed = copy.deepcopy(f.cfg)
    changed['approvals']['mode'] = 'ask'
    f.path.write_text(yaml.safe_dump(changed))
    before = f.path.read_bytes()
    result = await approve(p)
    assert 'changed' in result.lower()
    assert f.path.read_bytes() == before
    f.runner._execute_mcp_reload_unlocked.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('server,names', [('missing', ['get']), ('mvms.tools', ['get']), ('mvms', ['*']), ('mvms', ['get', 'get']), ('mvms', []), ('mvms', 'get')])
async def test_invalid_scope_preflight(fixture, server, names):
    f = fixture
    before = f.path.read_bytes()
    result = await f.op.propose(f.runner, f.event, server, names)
    assert result['status'] == 'blocked'
    assert slash_confirm.get_pending('discord:room:thread') is None
    assert f.path.read_bytes() == before


@pytest.mark.asyncio
async def test_missing_runtime_tool_is_honest_partial(fixture, monkeypatch):
    f = fixture
    _, p = await propose(f)
    monkeypatch.setattr(f.op, 'runtime_names', lambda name: {'status'})
    result = await approve(p)
    assert 'PARTIAL' in result and 'not verified' in result
    assert yaml.safe_load(f.path.read_text())['mcp_servers']['mvms']['tools']['include'] == ['status', 'get']
    assert f.runner._execute_mcp_reload_unlocked.await_count == 1


@pytest.mark.asyncio
async def test_failure_does_not_replay_or_overwrite(fixture):
    f = fixture
    _, p = await propose(f)
    f.runner._execute_mcp_reload_unlocked.side_effect = RuntimeError('fixture failure')
    result = await approve(p)
    assert 'PARTIAL' in result
    assert await approve(p) is None
    assert f.runner._execute_mcp_reload_unlocked.await_count == 1


@pytest.mark.asyncio
async def test_busy_reload_blocks_before_config_write(fixture):
    f = fixture
    _, p = await propose(f)
    lock = f.op.operation_lock(f.runner)
    before = f.path.read_bytes()
    async with lock:
        result = await approve(p)
    assert 'busy' in result.lower()
    assert f.path.read_bytes() == before


def test_raw_config_guard_retained(fixture, monkeypatch):
    from tools import file_tools
    monkeypatch.setattr(file_tools, '_get_hermes_config_resolved', lambda: str(fixture.path))
    assert 'Refusing to write to Hermes config' in file_tools._check_sensitive_path(str(fixture.path))


def test_proposal_tool_has_no_authorization_or_apply_argument():
    from tools import mcp_include_tool as tool
    assert set(tool.SCHEMA['parameters']['properties']) == {'server', 'include'}
    assert json.loads(tool.propose_tool({'server': 'mvms', 'include': ['get']}))['status'] == 'blocked'




@pytest.mark.asyncio
async def test_admin_denied_and_revoked(fixture):
    f = fixture
    before = f.path.read_bytes()
    f.runner._resume_caller_is_admin = lambda source: False
    result, p = await propose(f)
    assert result['status'] == 'blocked' and p is None
    f.runner._resume_caller_is_admin = lambda source: True
    _, p = await propose(f)
    f.runner._resume_caller_is_admin = lambda source: False
    assert 'authority' in await approve(p)
    assert f.path.read_bytes() == before


@pytest.mark.asyncio
async def test_strict_reload_propagates_failure(fixture, monkeypatch):
    from tools import mcp_tool
    f = fixture
    del f.runner._execute_mcp_reload_unlocked
    monkeypatch.setattr(mcp_tool, 'shutdown_mcp_servers', lambda: None)
    def fail():
        raise RuntimeError('fixture discovery failure')
    monkeypatch.setattr(mcp_tool, 'discover_mcp_tools', fail)
    _, p = await propose(f)
    result = await approve(p)
    assert 'PARTIAL' in result and 'VERIFIED' not in result
    assert yaml.safe_load(f.path.read_text())['mcp_servers']['mvms']['tools']['include'] == ['status', 'get']


def test_real_gateway_admin_check_fails_closed(fixture):
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig, PlatformConfig, Platform
    f = fixture
    del f.runner._resume_caller_is_admin
    f.runner.config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True)})
    assert not f.runner._resume_caller_is_admin(f.event.source)




@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['managed', 'relay', 'internal'])
async def test_admin_managed_and_non_native_events_blocked(fixture, monkeypatch, case):
    from hermes_cli import managed_scope
    f = fixture
    before = f.path.read_bytes()
    if case == 'managed':
        monkeypatch.setattr(managed_scope, 'is_key_managed', lambda key: True)
    elif case == 'relay':
        f.event.allow_gateway_control = False
    else:
        f.event.internal = True
    result, p = await propose(f)
    assert result['status'] == 'blocked' and p is None
    assert f.path.read_bytes() == before




@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['root-dot', 'server-dot', 'tools-dot', 'lazy-config', 'lazy-runtime'])
async def test_review_rejected_shapes(fixture, monkeypatch, shape):
    f = fixture
    d = yaml.safe_load(f.path.read_text())
    if shape == 'root-dot':
        d['mcp_servers.mvms.tools.include'] = ['unrelated']
    elif shape == 'server-dot':
        d['mcp_servers']['mvms.tools.include'] = ['unrelated']
    elif shape == 'tools-dot':
        d['mcp_servers']['mvms']['tools.include'] = ['unrelated']
    elif shape == 'lazy-config':
        d['mcp_servers']['mvms']['lazy'] = True
    else:
        from tools import mcp_tool
        monkeypatch.setattr(mcp_tool, '_lazy_server_configs', {'mvms': {}})
    f.path.write_text(yaml.safe_dump(d))
    before = f.path.read_bytes()
    result, p = await propose(f)
    assert result['status'] == 'blocked' and p is None
    assert f.path.read_bytes() == before
    f.runner._execute_mcp_reload_unlocked.assert_not_awaited()


def test_staged_writer_mismatch_does_not_replace(fixture):
    from utils import atomic_roundtrip_yaml_update
    f = fixture
    before = f.path.read_bytes()
    with pytest.raises(ValueError, match='Staged config'):
        atomic_roundtrip_yaml_update(f.path, 'mcp_servers.mvms.tools.include', ['get'], expected_state={})
    assert f.path.read_bytes() == before


@pytest.mark.asyncio
async def test_other_server_failure_reports_partial(fixture, monkeypatch):
    f = fixture
    from tools import mcp_tool
    import importlib
    checker = importlib.reload(f.op).connection_failures
    monkeypatch.setattr(f.op, 'connection_failures', checker)
    monkeypatch.setattr(mcp_tool, '_servers', {'mvms': SimpleNamespace(session=object())})
    monkeypatch.setattr(mcp_tool, '_lazy_server_configs', {})
    _, p = await propose(f)
    result = await approve(p)
    assert 'PARTIAL' in result and 'writer' in result and 'VERIFIED' not in result


def test_registry_owner_and_missing_entry(fixture, monkeypatch):
    from tools import mcp_tool
    from tools.registry import registry
    import importlib
    runtime_names = importlib.reload(fixture.op).runtime_names
    active = SimpleNamespace(session=object(), _tools=[SimpleNamespace(name='get')], _registered_tool_names=['mcp__mvms__get'])
    monkeypatch.setattr(mcp_tool, '_servers', {'mvms': active})
    monkeypatch.setattr(registry, '_tools', {})
    assert runtime_names('mvms') == set()
    registry.register(name='mcp__mvms__get', toolset='wrong-owner', schema={'name': 'mcp__mvms__get'}, handler=lambda args: '{}')
    assert runtime_names('mvms') == set()
    registry._tools.clear()
    registry.register(name='mcp__mvms__get', toolset='mcp-mvms', schema={'name': 'mcp__mvms__get'}, handler=lambda args: '{}')
    assert runtime_names('mvms') == {'get'}




@pytest.mark.asyncio
async def test_real_registry_cached_agent_filtering(fixture, monkeypatch):
    from tools import mcp_tool, tool_search
    from tools.registry import registry
    # Exercise registry filtering directly, not progressive-disclosure presentation.
    monkeypatch.setattr(tool_search, 'load_config', lambda: SimpleNamespace(enabled='off'))
    f = fixture
    monkeypatch.setattr(registry, '_tools', {})
    registry.register(name='mcp__mvms__get', toolset='mcp-mvms', schema={'name': 'mcp__mvms__get', 'parameters': {'type': 'object'}}, handler=lambda args: '{}')
    agent = SimpleNamespace(tools=[], valid_tool_names=set(), enabled_toolsets=['mcp-mvms'], disabled_toolsets=[])
    mcp_tool.refresh_agent_mcp_tools(agent)
    assert 'mcp__mvms__get' in agent.valid_tool_names
    agent.disabled_toolsets = ['mcp-mvms']
    mcp_tool.refresh_agent_mcp_tools(agent)
    assert 'mcp__mvms__get' not in agent.valid_tool_names


@pytest.mark.asyncio
async def test_gateway_wrapper_default_and_explicit_origin(fixture):
    f = fixture
    f.runner.config = SimpleNamespace(multiplex_profiles=False)
    f.runner._run_agent_inner = AsyncMock(return_value={})
    await f.runner._run_agent('fixture', '', [], f.event.source, 'fixture-session')
    assert f.runner._run_agent_inner.await_args.kwargs['mcp_control_allowed'] is False
    await f.runner._run_agent('fixture', '', [], f.event.source, 'fixture-session', mcp_control_allowed=True)
    assert f.runner._run_agent_inner.await_args.kwargs['mcp_control_allowed'] is True


def test_tool_is_explicit_discord_only():
    from toolsets import TOOLSETS
    assert 'mcp_include_propose' in TOOLSETS['hermes-discord']['tools']
    assert 'mcp_include_propose' not in TOOLSETS['hermes-api-server']['tools']


@pytest.mark.asyncio
async def test_button_delivery_failure_is_no_mutation(fixture):
    f = fixture
    f.runner._adapter_for_source = lambda source: None
    before = f.path.read_bytes()
    result, p = await propose(f)
    assert result['status'] == 'blocked' and p is None
    assert f.path.read_bytes() == before


@pytest.mark.asyncio
async def test_actual_gateway_reload_and_registry_readback(fixture, monkeypatch):
    from gateway.run import GatewayRunner
    from tools import mcp_tool
    f = fixture
    del f.runner._execute_mcp_reload_unlocked
    monkeypatch.setattr(f.op, 'runtime_names', __import__('importlib').reload(f.op).runtime_names)
    active = SimpleNamespace(session=object(), _tools=[SimpleNamespace(name=n) for n in ['status', 'get']],
                             _registered_tool_names=['mcp__mvms__status', 'mcp__mvms__get'])
    from tools.registry import registry
    monkeypatch.setattr(registry, '_tools', dict(registry._tools))
    calls = []
    def shutdown():
        calls.append('shutdown')
        mcp_tool._servers.clear()
    def discover():
        calls.append('discover')
        assert yaml.safe_load(f.path.read_text())['mcp_servers']['mvms']['tools']['include'] == ['status', 'get']
        mcp_tool._servers['mvms'] = active
        mcp_tool._servers['writer'] = SimpleNamespace(session=object())
        from tools.registry import registry
        for name in active._registered_tool_names:
            registry.register(name=name, toolset='mcp-mvms', schema={'name': name, 'parameters': {'type': 'object'}}, handler=lambda args, **kwargs: '{}')
        return list(active._registered_tool_names)
    monkeypatch.setattr(mcp_tool, '_servers', {})
    monkeypatch.setattr(mcp_tool, 'shutdown_mcp_servers', shutdown)
    monkeypatch.setattr(mcp_tool, 'discover_mcp_tools', discover)
    _, p = await propose(f)
    result = await approve(p)
    assert 'VERIFIED' in result
    assert calls == ['shutdown', 'discover']


@pytest.mark.asyncio
async def test_discord_button_owner_bot_and_choice(fixture):
    from plugins.platforms.discord.adapter import SlashConfirmView
    f = fixture
    _, p = await propose(f)
    # Real adapter class (fixture interaction, no network).
    ordinary = SlashConfirmView('discord:unscoped', 'fixture-confirm', {'human'})
    assert [child.label for child in ordinary.children] == ['Approve Once', 'Always Approve', 'Cancel']
    view = SlashConfirmView('discord:room:thread', p['confirm_id'], {'human', 'other'})
    good = SimpleNamespace(user=SimpleNamespace(id='human', bot=False, display_name='Human'))
    other = SimpleNamespace(user=SimpleNamespace(id='other', bot=False))
    bot = SimpleNamespace(user=SimpleNamespace(id='human', bot=True))
    assert view._check_auth(good)
    assert not view._check_auth(other)
    assert not view._check_auth(bot)
    assert view.timeout == 300
    assert all(getattr(child, 'label', None) != 'Always Approve' for child in view.children)
    assert [child.label for child in view.children] == ['Approve Once', 'Cancel']
    good.message = SimpleNamespace(embeds=[])
    good.response = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
    good.followup = SimpleNamespace(send=AsyncMock())
    await view.children[0].callback(good)
    assert f.runner._execute_mcp_reload_unlocked.await_count == 1
    assert 'VERIFIED' in good.followup.send.await_args.args[0]
    assert all(child.disabled for child in view.children)


@pytest.mark.asyncio
async def test_proposal_tool_bridge_does_not_authorize(fixture):
    from tools import mcp_include_tool as tool
    from tools.approval import set_current_session_key, reset_current_session_key
    f = fixture
    loop = asyncio.get_running_loop()
    def bridge(server, include):
        return asyncio.run_coroutine_threadsafe(f.op.propose(f.runner, f.event, server, include), loop).result(5)
    token = set_current_session_key('bridge-test')
    tool.register_gateway('bridge-test', bridge)
    try:
        before = f.path.read_bytes()
        result = json.loads(await asyncio.to_thread(tool.propose_tool, {'server': 'mvms', 'include': ['status', 'get']}))
        assert result['status'] == 'awaiting_confirmation'
        assert f.path.read_bytes() == before
    finally:
        tool.unregister_gateway('bridge-test')
        reset_current_session_key(token)


@pytest.mark.asyncio
async def test_payload_copy_and_actual_lock_conflict(fixture):
    import fcntl
    f = fixture
    names = ['status', 'get']
    await f.op.propose(f.runner, f.event, 'mvms', names)
    p = slash_confirm.get_pending('discord:room:thread')
    names.append('record')
    with open(f.path.with_name('config.yaml.lock'), 'a+') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = f.path.read_bytes()
        result = await approve(p)
        assert 'busy' in result
        assert f.path.read_bytes() == before
    _, p = await propose(f)
    assert 'VERIFIED' in await approve(p)


@pytest.mark.asyncio
async def test_aliased_config_rejected(fixture):
    f = fixture
    f.path.write_text('mcp_servers:\n  mvms: &shared\n    command: fixture\n    tools:\n      include: [status]\n  writer: *shared\n')
    before = f.path.read_bytes()
    result, p = await propose(f)
    assert result['status'] == 'blocked' and p is None
    assert f.path.read_bytes() == before

