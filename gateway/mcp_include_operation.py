"""One native MCP include-list operation. No shell, generic config key or grant API.

Consent is a gateway-owned slash-confirm callback, not a model argument. Intent
is process-local: expiry/restart requires re-proposal; execution is never replayed.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import re
from contextlib import contextmanager

import yaml

from hermes_cli import config
from utils import atomic_roundtrip_yaml_update


class PreflightError(ValueError):
    pass


def operation_lock(runner):
    lock = getattr(runner, '_mcp_operation_lock', None)
    if lock is None:
        lock = asyncio.Lock()
        runner._mcp_operation_lock = lock
    return lock


def validate_names(server, include):
    if not isinstance(server, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', server):
        raise PreflightError('Invalid server name.')
    if not isinstance(include, list) or not 1 <= len(include) <= 64:
        raise PreflightError('Use a non-empty list of at most 64 exact tool names.')
    if any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', n) for n in include):
        raise PreflightError('Exact tool names only; no wildcard or config path.')
    if len(set(include)) != len(include):
        raise PreflightError('Duplicate tool names.')


def snapshot(server, include):
    validate_names(server, include)
    from hermes_cli import managed_scope
    if config.is_managed() or managed_scope.is_key_managed(f'mcp_servers.{server}.tools.include'):
        raise PreflightError('Administrator-managed config cannot be changed by this operation.')
    path = config.get_config_path()
    if path.is_symlink() or not path.is_file():
        raise PreflightError('Operation requires an existing regular config file.')
    raw = path.read_bytes()
    # Aliased maps could change a second server through a single-key mutation.
    if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(raw)):
        raise PreflightError('Aliased config requires operator editing; no mutation.')
    data = config.require_readable_config_before_write(path)
    servers = data.get('mcp_servers')
    target = servers.get(server) if isinstance(servers, dict) else None
    if not isinstance(target, dict) or target.get('enabled', True) is not True:
        raise PreflightError('Existing enabled server required.')
    from tools import mcp_tool
    with mcp_tool._lock:
        if target.get('lazy') or server in mcp_tool._lazy_server_configs:
            raise PreflightError('Lazy MCP targets are not supported by this operation.')
    # The existing CLI writer honors literal dotted keys. Reject any ambiguity
    # before offering a preview; the writer also validates staged exact state.
    parts = ['mcp_servers', server, 'tools', 'include']
    node = data
    for index, key in enumerate(parts[:-1]):
        if any('.'.join(parts[index:index + size]) in node for size in range(2, len(parts) - index + 1)):
            raise PreflightError('Ambiguous dotted config keys require operator editing.')
        node = node.get(key, {})
        if not isinstance(node, dict):
            raise PreflightError('Nested config shape required.')
    tools = target.get('tools')
    old = tools.get('include') if isinstance(tools, dict) else None
    if not isinstance(old, list) or not all(isinstance(n, str) for n in old):
        raise PreflightError('Existing explicit include list required; no implicit access expansion.')
    if old:
        validate_names(server, old)
    return path, raw, data, tuple(old)


@contextmanager
def strict_config_lock(path):
    # Same lock file as the existing CLI writer, but no best-effort fallback.
    import fcntl
    with config._CONFIG_LOCK:
        fd = os.open(str(path.with_name('config.yaml.lock')), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PreflightError('Config writer busy; nothing changed.') from exc
            yield
        finally:
            os.close(fd)


def runtime_names(server):
    """Read the actual post-reload registered names, not the success string."""
    from tools import mcp_tool
    with mcp_tool._lock:
        active = mcp_tool._servers.get(server)
        if active is None or active.session is None:
            return set()
        from tools.registry import registry
        entries = {entry.name: entry for entry in registry.get_all_entries()}
        registered = {name for name in active._registered_tool_names
                      if name in entries and entries[name].toolset == f'mcp-{server}'}
        return {tool.name for tool in active._tools
                if mcp_tool.mcp_prefixed_tool_name(server, tool.name) in registered}


def connection_failures(data):
    from tools import mcp_tool
    with mcp_tool._lock:
        return [name for name, cfg in data['mcp_servers'].items()
                if isinstance(cfg, dict) and cfg.get('enabled', True) is True
                and not cfg.get('lazy') and name not in mcp_tool._lazy_server_configs
                and (name not in mcp_tool._servers or mcp_tool._servers[name].session is None)]


async def propose(runner, event, server, include):
    from gateway.config import Platform
    if (event.source.platform != Platform.DISCORD or not event.source.user_id
            or event.internal or not event.allow_gateway_control):
        return {'status': 'blocked', 'message': 'Native Discord human confirmation required.'}
    if getattr(getattr(runner, 'config', None), 'multiplex_profiles', False):
        return {'status': 'blocked', 'message': 'Multiplex-profile operations are not supported in this slice.'}
    if not runner._resume_caller_is_admin(event.source):
        return {'status': 'blocked', 'message': 'An explicitly configured gateway admin must own this config operation.'}
    try:
        path, raw, before_data, old = snapshot(server, include)
        # Prove the native writer route and exclusivity BEFORE offering consent.
        with strict_config_lock(path):
            pass
        if operation_lock(runner).locked():
            raise PreflightError('MCP reload busy; nothing changed.')
    except Exception as exc:
        message = str(exc) if isinstance(exc, PreflightError) else 'Config preflight failed; nothing changed.'
        return {'status': 'blocked', 'message': message}
    target_path = str(path.resolve())
    before_hash = hashlib.sha256(raw).hexdigest()
    desired = tuple(include)  # Caller cannot change a pending payload in place.
    expected_data = copy.deepcopy(before_data)
    expected_data['mcp_servers'][server]['tools']['include'] = list(desired)

    async def apply(choice):
        if choice != 'once':
            return 'Cancelled; nothing changed. This operation cannot grant always-allow.'
        if not runner._resume_caller_is_admin(event.source):
            return 'Blocked: admin authority no longer valid; nothing changed.'
        lock = operation_lock(runner)
        if lock.locked():
            return 'Blocked: MCP reload busy; nothing changed. Re-propose when idle.'
        async with lock:
            try:
                current_path, current_raw, _, _ = snapshot(server, list(desired))
                if str(current_path.resolve()) != target_path or hashlib.sha256(current_raw).hexdigest() != before_hash:
                    raise PreflightError('Config changed since preview; nothing changed. Re-propose for a fresh preview.')
                with strict_config_lock(current_path):
                    if current_path.read_bytes() != raw:
                        raise PreflightError('Config changed while acquiring writer; nothing changed.')
                    # Native comment-preserving, atomic single-field writer.
                    atomic_roundtrip_yaml_update(current_path, f'mcp_servers.{server}.tools.include', list(desired), expected_state=expected_data)
                saved_raw = current_path.read_bytes()
            except PreflightError as exc:
                return f'Blocked: {exc}'
            except Exception:
                return 'PARTIAL/UNKNOWN: config write did not complete cleanly. Inspect saved state before retry; no automatic replay.'
            try:
                await runner._execute_mcp_reload_unlocked(event, strict=True)
                saved = config.require_readable_config_before_write(current_path)
                actual = saved['mcp_servers'][server]['tools']['include']
                failed = connection_failures(saved)
                if failed:
                    return 'PARTIAL: include saved; MCP servers not connected: ' + ', '.join(sorted(failed)) + '. No automatic replay.'
                available = runtime_names(server)
                if (current_path.read_bytes() != saved_raw or saved != expected_data or actual != list(desired)
                        or available != set(desired)):
                    return 'PARTIAL: include list saved; runtime/config not verified. Inspect saved state and connections; no automatic retry or rollback.'
            except Exception:
                return 'PARTIAL: include list saved; MCP reload/readback failed. Inspect connections before retry; no automatic rollback.'
            return f'VERIFIED: {server} include list saved and registered tools match. MCP reloaded once; enabled eager connections and unrelated config checked. Tool behavior still requires normal-tool acceptance.'

    message = (f'MCP tool access: `{server}`\nBefore: {list(old)}\nAfter: {list(desired)}\n'
               'Approve once to save only this include list, reconnect all MCP servers, '
               'refresh cached tool lists, and verify registration. This interrupts MCP connections and invalidates prompt caches. '
               'Failure stops with saved-state readback; no automatic rollback or replay. No credentials, server command, '
               'approval policy or other-server settings change. Preview expires in 5 minutes; expiry does nothing.')
    if len(message) > 3000:
        return {'status': 'blocked', 'message': 'Preview too large; reduce the exact include list.'}
    fallback = await runner._request_slash_confirm(
        event=event, command='mcp-include', title='MCP tool access — one operation',
        message=message, handler=apply, owner_user_id=str(event.source.user_id))
    if fallback:
        return {'status': 'blocked', 'message': fallback, 'applied': False}
    return {'status': 'awaiting_confirmation', 'message': message,
            'applied': False, 'instruction': 'Wait for the native human confirmation; do not simulate it or run another apply route.'}
