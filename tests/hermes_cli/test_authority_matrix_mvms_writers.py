from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

# Host artifacts: this test checks josep's live config canon, which is not
# part of the repo.  The lint script itself hard-codes /home/josep roots, so
# the location cannot be re-derived from HERMES_HOME.
CONFIG_CANON_DIR = Path("/home/josep/.hermes/scripts/config_canon")
AUTHORITY_LINT_PATH = CONFIG_CANON_DIR / "authority_lint.py"


def _load_authority_lint():
    spec = importlib.util.spec_from_file_location("authority_lint_live", AUTHORITY_LINT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_authority_matrix_covers_all_static_mvms_writer_sources_on_disk() -> None:
    """Regression: any static MVMS writer source must have a matrix row.

    This mirrors authority_lint coverage, but keeps a focused pytest in the
    dashboard trust-layer suite so a new unregistered writer cannot leave the
    capstone chip green by accident.
    """
    if not AUTHORITY_LINT_PATH.exists():
        pytest.skip("host artifact ~/.hermes/scripts/config_canon/authority_lint.py absent")
    authority_lint = _load_authority_lint()
    matrix_path = CONFIG_CANON_DIR / "authority-matrix.yaml"
    matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    registered = {
        row.get("principal_name")
        for row in authority_lint.principal_rows(matrix)
        if row.get("principal_type") == "mvms_writer"
    }

    static_writers = authority_lint.discover_mvms_writer_principals(authority_lint.DEFAULT_WRITER_ROOTS)
    expected = set(static_writers)

    assert expected, "static MVMS-writer scan returned no writers; lint coverage is blind"
    missing = sorted(expected - registered)
    assert missing == []
