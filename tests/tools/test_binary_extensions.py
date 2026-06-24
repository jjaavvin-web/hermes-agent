"""Tests for the binary-extension text/binary gate."""

import pytest

from tools.binary_extensions import BINARY_EXTENSIONS, has_binary_extension


@pytest.mark.parametrize("path", ["photo.png", "archive.zip", "x.tar.gz"])
def test_common_binary_extensions_are_detected(path: str) -> None:
    assert has_binary_extension(path) is True


@pytest.mark.parametrize("path", ["a.PNG", "foo.PyC", r"C:\x.EXE"])
def test_binary_extension_check_is_case_insensitive(path: str) -> None:
    assert has_binary_extension(path) is True


@pytest.mark.parametrize("path", ["README", "/no/ext/here"])
def test_paths_without_dots_are_not_binary(path: str) -> None:
    assert has_binary_extension(path) is False


@pytest.mark.parametrize(
    "path",
    [
        ".gitignore",
        "archive.",
        "dir.png/file",
        "weird.txt",
        "code.py",
        "notes.md",
    ],
)
def test_text_and_path_edge_cases_are_not_binary(path: str) -> None:
    assert has_binary_extension(path) is False


def test_binary_extensions_frozenset_invariants() -> None:
    assert isinstance(BINARY_EXTENSIONS, frozenset)
    assert ".png" in BINARY_EXTENSIONS
    assert ".txt" not in BINARY_EXTENSIONS
    assert ".pdf" not in BINARY_EXTENSIONS
    assert all(extension.startswith(".") for extension in BINARY_EXTENSIONS)
    assert all(extension == extension.lower() for extension in BINARY_EXTENSIONS)
