"""Proposal-only gateway tool; no generic config write or approval argument."""
import json
import threading

from tools.registry import registry

_callbacks = {}
_lock = threading.RLock()


def register_gateway(session_key, callback):
    with _lock:
        _callbacks[session_key] = callback


def unregister_gateway(session_key):
    with _lock:
        _callbacks.pop(session_key, None)


def propose_tool(args):
    from tools.approval import get_current_session_key
    with _lock:
        callback = _callbacks.get(get_current_session_key())
    if callback is None:
        return json.dumps({'status': 'blocked', 'message': 'No native Discord proposal channel in this turn.'})
    if set(args) != {'server', 'include'}:
        return json.dumps({'status': 'blocked', 'message': 'Only server and include are accepted.'})
    from gateway.mcp_include_operation import validate_names
    try:
        validate_names(args['server'], args['include'])
        result = callback(args['server'], list(args['include']))
        return json.dumps(result)
    except Exception:
        return json.dumps({'status': 'blocked', 'message': 'Proposal failed; no apply was requested. Check pending preview before retrying.'})


SCHEMA = {
    'name': 'mcp_include_propose',
    'description': 'Propose one existing MCP server exact tool include-list change to the native Discord human. One button confirmation covers saving that field, all-server MCP reconnect and registration verification. This only proposes: never claim applied from its return. No shell, credentials, other config keys or blanket approvals. Requires existing explicit include list. Five-minute process-local preview; expiry/restart requires fresh proposal.',
    'parameters': {'type': 'object', 'properties': {
        'server': {'type': 'string'},
        'include': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 64}},
        'required': ['server', 'include'], 'additionalProperties': False}}

registry.register(name='mcp_include_propose', toolset='mcp_include', schema=SCHEMA,
                  handler=lambda args, **kw: propose_tool(args), emoji='🔧')
