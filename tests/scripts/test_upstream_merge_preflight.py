"""Fast unit tests for scripts/upstream_merge_preflight.py.

Covers only the pure AST logic (check 3's duplicate-definition sweep and
check 4's config-schema key-diff) via tmp_path-written source files. The
subprocess-driven checks (CLI-boot smoke, web route-manifest drift) are
exercised by running the real script end-to-end, not by unit tests here —
they need a real interpreter boot / FastAPI import and are intentionally
out of scope for this fast suite.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load("upstream_merge_preflight")


def _write(tmp_path: Path, filename: str, source: str) -> str:
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# check 3 — find_duplicate_defs
# ---------------------------------------------------------------------------


def test_find_duplicate_defs_flags_module_level_duplicate_function(tmp_path):
    source = _write(
        tmp_path,
        "module_dupe.py",
        "def handle():\n    return 1\n\n\ndef handle():\n    return 2\n",
    )

    dupes = preflight.find_duplicate_defs(source)

    assert len(dupes) == 1
    assert dupes[0].scope == ""
    assert dupes[0].name == "handle"
    assert dupes[0].lines == (1, 5)


def test_find_duplicate_defs_flags_duplicate_method_in_class(tmp_path):
    source = _write(
        tmp_path,
        "class_dupe.py",
        (
            "class Worker:\n"
            "    def run(self):\n"
            "        return 'first'\n"
            "\n"
            "    def other(self):\n"
            "        return 'unrelated'\n"
            "\n"
            "    def run(self):\n"
            "        return 'second'\n"
        ),
    )

    dupes = preflight.find_duplicate_defs(source)

    assert len(dupes) == 1
    assert dupes[0].scope == "Worker"
    assert dupes[0].name == "run"


def test_find_duplicate_defs_ignores_single_definitions(tmp_path):
    source = _write(
        tmp_path,
        "clean.py",
        "def a():\n    pass\n\n\ndef b():\n    pass\n",
    )

    assert preflight.find_duplicate_defs(source) == []


def test_find_duplicate_defs_excludes_underscore_name(tmp_path):
    source = _write(
        tmp_path,
        "underscore.py",
        "def _():\n    pass\n\n\ndef _():\n    pass\n",
    )

    assert preflight.find_duplicate_defs(source) == []


def test_find_duplicate_defs_excludes_property_setter_pair(tmp_path):
    source = _write(
        tmp_path,
        "prop.py",
        (
            "class Config:\n"
            "    @property\n"
            "    def value(self):\n"
            "        return self._value\n"
            "\n"
            "    @value.setter\n"
            "    def value(self, new_value):\n"
            "        self._value = new_value\n"
        ),
    )

    assert preflight.find_duplicate_defs(source) == []


def test_find_duplicate_defs_excludes_property_setter_deleter_trio(tmp_path):
    source = _write(
        tmp_path,
        "prop_trio.py",
        (
            "class Config:\n"
            "    @property\n"
            "    def value(self):\n"
            "        return self._value\n"
            "\n"
            "    @value.setter\n"
            "    def value(self, new_value):\n"
            "        self._value = new_value\n"
            "\n"
            "    @value.deleter\n"
            "    def value(self):\n"
            "        del self._value\n"
        ),
    )

    assert preflight.find_duplicate_defs(source) == []


def test_find_duplicate_defs_still_flags_when_decorator_missing_on_one_def(tmp_path):
    """A near-miss: one of the two defs is NOT actually decorated — real bug."""
    source = _write(
        tmp_path,
        "fake_prop.py",
        (
            "class Config:\n"
            "    @property\n"
            "    def value(self):\n"
            "        return self._value\n"
            "\n"
            "    def value(self, new_value):\n"  # missing @value.setter — a real collision
            "        self._value = new_value\n"
        ),
    )

    dupes = preflight.find_duplicate_defs(source)

    assert len(dupes) == 1
    assert dupes[0].name == "value"


def test_find_duplicate_defs_treats_if_else_branches_as_separate(tmp_path):
    """Conditional definitions in if/else are a common, legitimate pattern —
    they live in different statement lists (If.body vs If.orelse) and must
    not be flagged as a same-scope collision."""
    source = _write(
        tmp_path,
        "conditional.py",
        (
            "import sys\n"
            "\n"
            "if sys.platform == 'win32':\n"
            "    def normalize_path(p):\n"
            "        return p.replace('/', chr(92))\n"
            "else:\n"
            "    def normalize_path(p):\n"
            "        return p\n"
        ),
    )

    assert preflight.find_duplicate_defs(source) == []


def test_find_duplicate_defs_flags_duplicate_within_same_if_block(tmp_path):
    source = _write(
        tmp_path,
        "if_block_dupe.py",
        (
            "if True:\n"
            "    def helper():\n"
            "        return 1\n"
            "    def helper():\n"
            "        return 2\n"
        ),
    )

    dupes = preflight.find_duplicate_defs(source)

    assert len(dupes) == 1
    assert dupes[0].name == "helper"


def test_find_duplicate_defs_nested_function_scopes_are_independent(tmp_path):
    """Same name reused in an outer function and its own nested function body
    is not a collision — they're different scopes."""
    source = _write(
        tmp_path,
        "nested.py",
        (
            "def outer():\n"
            "    def inner():\n"
            "        return 1\n"
            "    return inner\n"
            "\n"
            "\n"
            "def inner():\n"
            "    return 2\n"
        ),
    )

    assert preflight.find_duplicate_defs(source) == []


# ---------------------------------------------------------------------------
# check 3 — find_merge_introduced_duplicates (base comparison)
# ---------------------------------------------------------------------------


def test_merge_introduced_duplicates_filters_pre_existing_at_base(tmp_path):
    base_source = _write(
        tmp_path,
        "base.py",
        "def handle():\n    return 1\n\n\ndef handle():\n    return 2\n",
    )
    head_source = base_source  # unchanged by the merge

    new_dupes = preflight.find_merge_introduced_duplicates(head_source, base_source)

    assert new_dupes == []


def test_merge_introduced_duplicates_reports_new_duplicate_not_at_base(tmp_path):
    base_source = _write(tmp_path, "base_clean.py", "def handle():\n    return 1\n")
    head_source = _write(
        tmp_path,
        "head_dupe.py",
        "def handle():\n    return 1\n\n\ndef handle():\n    return 2\n",
    )

    new_dupes = preflight.find_merge_introduced_duplicates(head_source, base_source)

    assert len(new_dupes) == 1
    assert new_dupes[0].name == "handle"


def test_merge_introduced_duplicates_new_file_reports_everything(tmp_path):
    head_source = _write(
        tmp_path,
        "new_file.py",
        "def handle():\n    return 1\n\n\ndef handle():\n    return 2\n",
    )

    new_dupes = preflight.find_merge_introduced_duplicates(head_source, None)

    assert len(new_dupes) == 1


def test_merge_introduced_duplicates_unparseable_base_reports_everything(tmp_path):
    head_source = _write(
        tmp_path,
        "head_ok.py",
        "def handle():\n    return 1\n\n\ndef handle():\n    return 2\n",
    )
    broken_base_source = "def handle(:\n    pass\n"  # deliberately unparseable

    new_dupes = preflight.find_merge_introduced_duplicates(head_source, broken_base_source)

    assert len(new_dupes) == 1


# ---------------------------------------------------------------------------
# check 4 — extract_config_key_paths
# ---------------------------------------------------------------------------


def test_extract_config_key_paths_flat_dict(tmp_path):
    source = _write(
        tmp_path,
        "flat_config.py",
        "DEFAULT_CONFIG = {\n    'model': '',\n    'toolsets': ['hermes-cli'],\n}\n",
    )

    paths = preflight.extract_config_key_paths(source)

    assert paths == {"model", "toolsets"}


def test_extract_config_key_paths_nested_dict(tmp_path):
    source = _write(
        tmp_path,
        "nested_config.py",
        (
            "DEFAULT_CONFIG = {\n"
            "    'database': {\n"
            "        'journal_mode': 'wal',\n"
            "        'wal_autocheckpoint': None,\n"
            "    },\n"
            "    'agent': {\n"
            "        'max_turns': 500,\n"
            "        'agent_cache': {\n"
            "            'size': 8,\n"
            "        },\n"
            "    },\n"
            "}\n"
        ),
    )

    paths = preflight.extract_config_key_paths(source)

    assert paths == {
        "database",
        "database.journal_mode",
        "database.wal_autocheckpoint",
        "agent",
        "agent.max_turns",
        "agent.agent_cache",
        "agent.agent_cache.size",
    }


def test_extract_config_key_paths_ignores_unrelated_dict_assignment(tmp_path):
    source = _write(
        tmp_path,
        "unrelated_dict.py",
        "OTHER_TABLE = {'x': 1}\n\nDEFAULT_CONFIG = {\n    'model': '',\n}\n",
    )

    assert preflight.extract_config_key_paths(source) == {"model"}


def test_extract_config_key_paths_missing_assignment_raises(tmp_path):
    source = _write(tmp_path, "missing.py", "SOMETHING_ELSE = {'x': 1}\n")

    try:
        preflight.extract_config_key_paths(source)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a missing DEFAULT_CONFIG assignment")


# ---------------------------------------------------------------------------
# check 4 — diff_config_key_paths
# ---------------------------------------------------------------------------


def test_diff_config_key_paths_reports_dropped_and_added_separately():
    base_paths = {"model", "database.journal_mode", "agent.max_turns"}
    head_paths = {"model", "agent.max_turns", "agent.gateway_timeout"}

    dropped, added = preflight.diff_config_key_paths(base_paths, head_paths)

    assert dropped == ["database.journal_mode"]
    assert added == ["agent.gateway_timeout"]


def test_diff_config_key_paths_no_drift_is_empty():
    paths = {"model", "database.journal_mode"}

    dropped, added = preflight.diff_config_key_paths(paths, paths)

    assert dropped == []
    assert added == []


def test_check_config_schema_drift_end_to_end_reflects_dropped_and_added(tmp_path, monkeypatch):
    """Exercise check_config_schema_drift's FAIL-on-dropped / INFO-on-added
    wiring against real (small) git history, without touching the repo's
    own git state — this stays fast (a two-commit throwaway repo)."""
    import subprocess

    repo = tmp_path / "throwaway_repo"
    (repo / "hermes_cli").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    config_path = repo / "hermes_cli" / "config_defaults.py"
    config_path.write_text(
        "DEFAULT_CONFIG = {\n    'model': '',\n    'database': {'journal_mode': 'wal'},\n}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()

    # HEAD drops 'database.journal_mode' and adds 'toolsets'.
    config_path.write_text(
        "DEFAULT_CONFIG = {\n    'model': '',\n    'toolsets': [],\n}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "head"], cwd=repo, check=True)

    result = preflight.check_config_schema_drift(repo, base_rev)

    assert result.passed is False
    assert any("DROPPED database.journal_mode" in line for line in result.extra_lines)
    assert any("INFO added toolsets" in line for line in result.extra_lines)


def test_check_duplicate_definitions_end_to_end_only_reports_new(tmp_path):
    """Same throwaway-repo pattern for check 3's git-wired entry point:
    a duplicate present at both base and head is not reported, a
    merge-introduced one is."""
    import subprocess

    repo = tmp_path / "throwaway_repo_dupes"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    mod_path = repo / "mod.py"
    mod_path.write_text("def helper():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()

    # HEAD introduces a real duplicate paste.
    mod_path.write_text(
        "def helper():\n    return 1\n\n\ndef helper():\n    return 2\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "head"], cwd=repo, check=True)

    result = preflight.check_duplicate_definitions(repo, base_rev)

    assert result.passed is False
    assert any("mod.py" in line and "helper" in line for line in result.extra_lines)
