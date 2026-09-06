from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml


def _load_authority_lint():
    path = Path("/home/josep/.hermes/scripts/config_canon/authority_lint.py")
    spec = importlib.util.spec_from_file_location("authority_lint_live", path)
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
    authority_lint = _load_authority_lint()
    matrix_path = Path("/home/josep/.hermes/scripts/config_canon/authority-matrix.yaml")
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
