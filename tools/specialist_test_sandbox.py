"""Child entry point for specialist pytest execution after namespace setup."""
from __future__ import annotations
import ctypes
import errno
import json
import os
import sys
from pathlib import Path
from tools.specialist_test_tool import _landlock_preexec


def _install_process_seccomp() -> None:
    """Deny child creation, exec, signalling and ptrace after pytest imports."""
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    allow = 0x7FFF0000
    errno_action = 0x00050000 | errno.EPERM
    ctx = lib.seccomp_init(allow)
    if not ctx:
        os._exit(126)
    try:
        for name in (
            b"clone", b"clone3", b"fork", b"vfork", b"execve", b"execveat",
            b"kill", b"tkill", b"tgkill", b"pidfd_send_signal", b"ptrace",
            # No network or host IPC of any address family. A private network
            # namespace does not isolate AF_UNIX sockets, so deny socket
            # creation and connection syscalls in the verifier process.
            b"socket", b"socketpair", b"connect", b"bind", b"listen",
            b"accept", b"accept4", b"sendto", b"sendmsg", b"sendmmsg",
            b"recvfrom", b"recvmsg", b"recvmmsg", b"shutdown",
            # Landlock ABI 3 does not mediate metadata-only mutations such as
            # chmod/chown/xattr/utime. Deny those syscalls for verifier code.
            b"chmod", b"fchmod", b"fchmodat", b"fchmodat2", b"chown", b"fchown", b"lchown",
            b"fchownat", b"setxattr", b"lsetxattr", b"fsetxattr",
            b"removexattr", b"lremovexattr", b"fremovexattr", b"utime",
            b"utimes", b"futimesat", b"utimensat",
        ):
            number = lib.seccomp_syscall_resolve_name(name)
            if number >= 0 and lib.seccomp_rule_add(ctx, errno_action, number, 0) != 0:
                os._exit(126)
        if lib.seccomp_load(ctx) != 0:
            os._exit(126)
    finally:
        lib.seccomp_release(ctx)


def main() -> int:
    sys.dont_write_bytecode = True
    import pytest
    payload=json.loads(os.environ.pop("HERMES_SPECIALIST_TEST_PAYLOAD"))
    roots=[Path(p) for p in payload["read_roots"]]
    writable=[Path(p) for p in payload["write_roots"]]
    # Apply Landlock in this already-unshared process, then exec pytest so the
    # restrictions survive without blocking unshare's namespace setup.
    _landlock_preexec(roots,writable)()
    _install_process_seccomp()
    return int(pytest.main(["-q","-p","no:cacheprovider","-p","no:logging",*payload["targets"]]))

if __name__ == "__main__":
    raise SystemExit(main())
