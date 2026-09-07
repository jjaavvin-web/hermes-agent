"""Regression tests for C-mcp-inherit (kanban t_883970c1).

Delegated children must inherit READ-ONLY MCP servers only, by default.
Writer-capable MCP (e.g. mvms-writer, Notion write tools) must reach a
child ONLY via the explicit config escape hatch
(``delegation.writer_mcp_allowed_toolsets``) -- never via a model-supplied
``toolsets``/``enabled_toolsets`` request, and never merely because the
parent/mothership happens to have it enabled.

See the approved scoping decision at
~/.hermes/audits/wave2-20260706/F7-mcp-inherit/SCOPING-PROPOSAL.md.
"""

import threading
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import _build_child_agent


def _make_mock_parent(enabled_toolsets, platform="webhook"):
    """Mock MOTHERSHIP/parent agent with writer + read-only MCP enabled."""
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = platform
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    # A *list* (not tuple) so we can assert it wasn't mutated in place.
    parent.enabled_toolsets = list(enabled_toolsets)
    return parent


# Config fixtures shared across tests: mvms-writer is a declared writer MCP,
# mvms and context7 are declared read-only MCP. Declaring authority
# explicitly keeps these tests independent of what (if anything) is live in
# the real tools.registry singleton in this process.
_MCP_SERVERS_CONFIG = {
    "mvms-writer": {"authority": "write"},
    "mvms": {"authority": "read"},
    "context7": {"authority": "read"},
}


def _delegation_cfg(**overrides):
    cfg = {}
    cfg.update(overrides)
    return cfg


def _full_cfg(**delegation_overrides):
    return {
        "mcp_servers": _MCP_SERVERS_CONFIG,
        "delegation": _delegation_cfg(**delegation_overrides),
    }


class TestWriterMcpStrippedByDefault(unittest.TestCase):
    """Regression 1: writer MCP must never reach a delegated child by
    default, whether via an explicit ``toolsets`` request (model-controlled)
    or via the no-explicit-toolsets "inherit everything the parent has"
    branch."""

    @patch("tools.delegate_tool._load_config", return_value=_delegation_cfg())
    @patch(
        "tools.delegate_tool._load_full_delegation_config",
        return_value=_full_cfg(),
    )
    def test_explicit_child_request_for_writer_mcp_is_stripped(
        self, mock_full_cfg, mock_cfg
    ):
        """Parent has mvms-writer enabled; child explicitly requests it via
        toolsets (stand-in for a model-supplied enabled_toolsets ask).
        Default config must strip it regardless."""
        parent = _make_mock_parent(["web", "mcp-mvms-writer"])

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="do something",
                context=None,
                toolsets=["web", "mcp-mvms-writer"],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        got = MockAgent.call_args[1]["enabled_toolsets"]
        self.assertNotIn(
            "mcp-mvms-writer",
            got,
            "Writer-capable MCP leaked to a delegated child via an explicit "
            "child toolsets request under default config",
        )
        self.assertIn("web", got)

    @patch("tools.delegate_tool._load_config", return_value=_delegation_cfg())
    @patch(
        "tools.delegate_tool._load_full_delegation_config",
        return_value=_full_cfg(),
    )
    def test_no_explicit_toolsets_branch_also_strips_writer_mcp(
        self, mock_full_cfg, mock_cfg
    ):
        """No toolsets passed at all (the real delegate_task path always
        does this) -- child inherits parent.enabled_toolsets wholesale.
        Writer MCP must still be stripped."""
        parent = _make_mock_parent(["web", "browser", "mcp-mvms-writer"])

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="do something",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        got = MockAgent.call_args[1]["enabled_toolsets"]
        self.assertNotIn("mcp-mvms-writer", got)
        self.assertIn("web", got)
        self.assertIn("browser", got)


class TestReadOnlyMcpStillInherited(unittest.TestCase):
    """Regression 2: read-only MCP servers must still reach children under
    the new default -- this item restricts WRITE authority, not all MCP."""

    @patch("tools.delegate_tool._load_config", return_value=_delegation_cfg())
    @patch(
        "tools.delegate_tool._load_full_delegation_config",
        return_value=_full_cfg(),
    )
    def test_explicit_toolsets_branch_preserves_read_only_mcp(
        self, mock_full_cfg, mock_cfg
    ):
        parent = _make_mock_parent(["web", "mcp-mvms"])

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="research",
                context=None,
                toolsets=["web"],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        got = MockAgent.call_args[1]["enabled_toolsets"]
        self.assertIn(
            "mcp-mvms",
            got,
            "Read-only MCP toolset was incorrectly stripped from a "
            "delegated child",
        )

    @patch("tools.delegate_tool._load_config", return_value=_delegation_cfg())
    @patch(
        "tools.delegate_tool._load_full_delegation_config",
        return_value=_full_cfg(),
    )
    def test_no_explicit_toolsets_branch_preserves_read_only_mcp(
        self, mock_full_cfg, mock_cfg
    ):
        parent = _make_mock_parent(["web", "mcp-mvms", "mcp-context7"])

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="research",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        got = MockAgent.call_args[1]["enabled_toolsets"]
        self.assertIn("mcp-mvms", got)
        self.assertIn("mcp-context7", got)


class TestWriterMcpEscapeHatch(unittest.TestCase):
    """Regression 3: the config escape hatch
    (delegation.writer_mcp_allowed_toolsets) is the ONLY way a child gets a
    writer MCP server; the model's own request is never sufficient alone
    (both are true simultaneously in this test)."""

    @patch(
        "tools.delegate_tool._load_config",
        return_value=_delegation_cfg(
            writer_mcp_allowed_toolsets=["mvms-writer"]
        ),
    )
    @patch(
        "tools.delegate_tool._load_full_delegation_config",
        return_value=_full_cfg(writer_mcp_allowed_toolsets=["mvms-writer"]),
    )
    def test_escape_hatch_grants_named_writer_mcp(self, mock_full_cfg, mock_cfg):
        parent = _make_mock_parent(["web", "mcp-mvms-writer"])

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="close out completion record",
                context=None,
                toolsets=["web", "mcp-mvms-writer"],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        got = MockAgent.call_args[1]["enabled_toolsets"]
        self.assertIn(
            "mcp-mvms-writer",
            got,
            "Config escape hatch failed to grant the named writer MCP "
            "server to a delegated child",
        )

    @patch("tools.delegate_tool._load_config", return_value=_delegation_cfg())
    @patch(
        "tools.delegate_tool._load_full_delegation_config",
        return_value=_full_cfg(),
    )
    def test_child_request_alone_without_config_grant_is_not_sufficient(
        self, mock_full_cfg, mock_cfg
    ):
        """Same model request as above, but WITHOUT the config grant --
        must be stripped. Proves the model's ask is never sufficient by
        itself."""
        parent = _make_mock_parent(["web", "mcp-mvms-writer"])

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="close out completion record",
                context=None,
                toolsets=["web", "mcp-mvms-writer"],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        got = MockAgent.call_args[1]["enabled_toolsets"]
        self.assertNotIn("mcp-mvms-writer", got)


class TestMothershipUnaffected(unittest.TestCase):
    """Regression 4: MOTHERSHIP/main-loop writer MCP access is preserved
    unchanged -- the restriction keys on delegate-CHILD construction, not on
    the parent/platform session itself."""

    @patch("tools.delegate_tool._load_config", return_value=_delegation_cfg())
    @patch(
        "tools.delegate_tool._load_full_delegation_config",
        return_value=_full_cfg(),
    )
    def test_parent_enabled_toolsets_not_mutated_by_child_construction(
        self, mock_full_cfg, mock_cfg
    ):
        """The mothership's own enabled_toolsets (which may legitimately
        include mvms-writer for closeout work) must be untouched after
        spawning a restricted child -- only the derived child list is
        filtered."""
        parent = _make_mock_parent(
            ["web", "mcp-mvms-writer"], platform="webhook"
        )
        original = list(parent.enabled_toolsets)

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="do something",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        self.assertEqual(
            parent.enabled_toolsets,
            original,
            "Mothership/parent enabled_toolsets was mutated by delegated "
            "child construction -- main-loop writer MCP access must be "
            "preserved unchanged",
        )
        self.assertIn("mcp-mvms-writer", parent.enabled_toolsets)

        # The CHILD built from this same parent must still be restricted.
        child_got = MockAgent.call_args[1]["enabled_toolsets"]
        self.assertNotIn("mcp-mvms-writer", child_got)


if __name__ == "__main__":
    unittest.main()
