"""Constrained pytest runner for specialist Kanban workers."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from hermes_cli.worker_authority import authorize_current_worker
from tools.registry import registry


def _landlock_preexec(read_roots: list[Path], write_roots: list[Path]):
    """Build a child hook that fails closed into a Landlock filesystem ruleset."""

    def apply() -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        create, add, restrict = 444, 445, 446
        pr_set_no_new_privs = 38
        execute, read_file, read_dir = 1 << 0, 1 << 2, 1 << 3
        # Landlock ABI 3 supports every filesystem right from bit 0 through
        # TRUNCATE (bit 14). Handle the complete mask so omitted MAKE_* rights
        # cannot silently remain allowed outside writable roots.
        write_access = sum(1 << bit for bit in range(1, 15) if bit not in {2, 3})
        handled = execute | read_file | read_dir | write_access

        class Ruleset(ctypes.Structure):
            _fields_ = [("handled_access_fs", ctypes.c_uint64)]

        class PathBeneath(ctypes.Structure):
            _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

        fd = libc.syscall(create, ctypes.byref(Ruleset(handled)), ctypes.sizeof(Ruleset), 0)
        if fd < 0:
            os._exit(126)
        try:
            rules = [(path, execute | read_file | read_dir) for path in read_roots]
            rules += [(path, handled) for path in write_roots]
            for path, access in rules:
                if not path.exists():
                    continue
                try:
                    parent_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                except OSError:
                    os._exit(126)
                try:
                    attr = PathBeneath(access, parent_fd)
                    if libc.syscall(add, fd, 1, ctypes.byref(attr), 0) < 0:
                        os._exit(126)
                finally:
                    os.close(parent_fd)
            if libc.prctl(pr_set_no_new_privs, 1, 0, 0, 0) != 0:
                os._exit(126)
            if libc.syscall(restrict, fd, 0) < 0:
                os._exit(126)
        finally:
            os.close(fd)

    return apply


def _clean_test_env(temp: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.update(
        HOME=str(temp),
        TMPDIR=str(temp),
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    return env


def _handle_specialist_test(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    decision = authorize_current_worker("specialist_test", args)
    if not decision.allowed:
        return {"error": decision.reason, "exit_code": None}
    root = Path(os.environ["HERMES_KANBAN_WORKSPACE"]).resolve(strict=True)
    timeout = min(max(int(args.get("timeout") or 300), 1), 600)
    unshare = shutil.which("unshare")
    if not unshare:
        return {"error": "specialist test denied: network namespace unavailable", "exit_code": None}

    with tempfile.TemporaryDirectory(prefix="hermes-specialist-test-") as temp_name:
        temp = Path(temp_name)
        # Both roles test an immutable disposable copy. Pytest assertion
        # rewriting can emit .pyc files despite a nominal no-bytecode env; the
        # original assigned workspace therefore must never be the pytest cwd.
        if any(path.is_symlink() for path in root.rglob("*")):
            return {"error": "specialist test denied: workspace symlinks are not allowed", "exit_code": None}
        run_root = temp / "workspace"
        shutil.copytree(root, run_root, symlinks=False)

        targets: list[str] = []
        for item in args["targets"]:
            source = (root / item).resolve(strict=True)
            if not source.is_file():
                return {"error": "specialist test denied: targets must be existing files", "exit_code": None}
            relative = source.relative_to(root)
            targets.append(str((run_root / relative).resolve(strict=True)))

        runtime_roots = {
            Path(sys.executable).resolve().parent,
            Path(sys.prefix).resolve(),
            Path(sys.base_prefix).resolve(),
            Path("/dev"),

            Path("/usr/bin"),
            Path("/bin"),
            Path("/usr/lib"),
            Path("/usr/lib64"),
            Path("/lib"),
            Path("/lib64"),
        }
        env = _clean_test_env(temp)
        env["HERMES_SPECIALIST_TEST_PAYLOAD"] = __import__("json").dumps({
            "read_roots": [str(path) for path in sorted(runtime_roots)],
            "write_roots": [str(run_root), str(temp)],
            "targets": targets,
        })
        proc = subprocess.run(
            [
                unshare, "-Urnpf", "--mount-proc", "--kill-child=SIGKILL", "--",
                sys.executable, "-I", "-c",
                (
                    "import sys;"
                    f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r});"
                    "from tools.specialist_test_sandbox import main;"
                    "raise SystemExit(main())"
                ),
            ],
            cwd=str(run_root),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    output = (proc.stdout + proc.stderr)[-50_000:]
    return {"output": output, "exit_code": proc.returncode}


SPECIALIST_TEST_SCHEMA = {
    "name": "specialist_test",
    "description": "Run pytest against explicit existing test files inside the assigned workspace using a clean environment, network namespace, and Landlock filesystem ruleset.",
    "parameters": {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Existing test files relative to the assigned workspace; directories, options, and escaping paths are denied.",
            },
            "timeout": {"type": "integer", "minimum": 1, "maximum": 600, "default": 300},
        },
        "required": ["targets"],
    },
}


registry.register(
    name="specialist_test",
    toolset="specialist_test",
    schema=SPECIALIST_TEST_SCHEMA,
    handler=_handle_specialist_test,
    emoji="🧪",
    max_result_size_chars=50_000,
)
