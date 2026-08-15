"""Shared fixtures for gateway tests.

The ``_ensure_telegram_mock`` helper guarantees that a minimal mock of
the ``telegram`` package is registered in :data:`sys.modules` **before**
any test file triggers ``from plugins.platforms.telegram.adapter import ...``.

Without this, ``pytest-xdist`` workers that happen to collect
``test_telegram_caption_merge.py`` (bare top-level import, no per-file
mock) first will cache ``ChatType = None`` from the production
ImportError fallback, causing 30+ downstream test failures wherever
``ChatType.GROUP`` / ``ChatType.SUPERGROUP`` is accessed.

Individual test files may still call their own ``_ensure_telegram_mock``
— it short-circuits when the mock is already present.

Plugin-adapter anti-pattern guard
---------------------------------
Tests for platform plugins (``plugins/platforms/<name>/adapter.py``)
must load the adapter via
:func:`tests.gateway._plugin_adapter_loader.load_plugin_adapter`, not by
adding the plugin directory to ``sys.path`` and doing a bare
``from adapter import ...``. The guard at the bottom of this file
scans test module ASTs at collection time and fails collection with a
pointer to the helper if the anti-pattern is detected.

Rationale: every plugin ships its own ``adapter.py``, and two tests each
inserting their plugin dir on ``sys.path[0]`` race for
``sys.modules["adapter"]`` in the same xdist worker. Whichever collects
first wins; the other fails with ``ImportError``, and the polluted
``sys.path`` cascades into unrelated tests. See PR #17764 for the
incident.
"""

import ast
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="session", autouse=True)
def _bind_lark_sdk_globals_when_installed():
    """Bind the feishu adapter's lark SDK globals once per test session.

    The adapter defers ``import lark_oapi`` to first use
    (``_load_lark_oapi`` — called from connect()/probe_bot()/standalone
    send), so the request-builder globals (``CreateMessageRequestBody``
    etc.) stay ``None`` at module import time. Feishu tests across many
    files inject a mock ``_client`` and skip connect() entirely, then call
    send paths that reference those globals. Bind them eagerly when the
    SDK is installed; when it isn't, the affected tests already skip via
    their own ``skipUnless`` guards.
    """
    try:
        import lark_oapi  # noqa: F401
    except ImportError:
        yield
        return
    try:
        from plugins.platforms.feishu.adapter import _load_lark_oapi

        _load_lark_oapi()
    except Exception:
        pass  # adapter not importable in this environment — tests will skip
    yield


def make_async_session_db(sync_mock=None):
    """Wrap a sync mock SessionDB in AsyncSessionDB so gateway code that awaits
    the facade works in tests. Returns (facade, sync_mock); configure return
    values and assert calls on sync_mock."""
    from hermes_state import AsyncSessionDB
    sync_mock = sync_mock if sync_mock is not None else MagicMock()
    return AsyncSessionDB(sync_mock), sync_mock


_UPDATE_MARKER_TEST_MODULES = {
    "test_update_command.py",
    "test_update_streaming.py",
}
_UPDATE_MARKER_NAMES = (
    ".update_pending.json",
    ".update_pending.claimed.json",
    ".update_output.txt",
    ".update_exit_code",
    ".update_prompt.json",
    ".update_response",
)


def _marker_fingerprint(home: Path) -> dict[str, tuple[bool, int | None, int | None]]:
    """Stat-only fingerprint for update marker files under ``home``."""
    result: dict[str, tuple[bool, int | None, int | None]] = {}
    for name in _UPDATE_MARKER_NAMES:
        path = home / name
        try:
            st = path.stat()
        except FileNotFoundError:
            result[name] = (False, None, None)
        else:
            result[name] = (True, st.st_mtime_ns, st.st_size)
    return result


@pytest.fixture(autouse=True)
def _isolate_update_marker_home(request, tmp_path, monkeypatch):
    """Keep update-marker tests from touching the operator's real Hermes home.

    ``gateway.run`` snapshots ``_hermes_home`` at import time, while
    ``hermes_cli.main._gateway_prompt()`` resolves paths through
    ``HERMES_HOME`` / ``get_hermes_home()`` at call time.  The update-marker
    suites exercise both paths, so isolate both resolvers and assert the real
    platform-default home did not gain or mutate marker artifacts.
    """
    module_name = Path(str(request.fspath)).name
    if module_name not in _UPDATE_MARKER_TEST_MODULES:
        yield
        return

    isolated_home = tmp_path / "isolated-hermes-home"
    isolated_home.mkdir()
    real_home = Path.home() / ".hermes"
    before = _marker_fingerprint(real_home)

    monkeypatch.setenv("HERMES_HOME", str(isolated_home))
    monkeypatch.setattr("gateway.run._hermes_home", isolated_home, raising=False)

    yield

    after = _marker_fingerprint(real_home)
    assert after == before, (
        "update-marker test mutated marker artifacts in the real Hermes home: "
        f"before={before!r} after={after!r}"
    )


#: The operator's REAL codex-reaper state dir, captured at conftest import —
#: i.e. before any fixture can monkeypatch ``HOME``.
_REAL_CODEX_REAPER_DIR = Path.home() / ".hermes" / "state" / "codex-reaper"

#: Opt-in, because a *live* gateway legitimately appends to this ledger on its
#: hourly tick (``CodexGcWatcher.poll_interval_sec`` defaults to 3600 s).  On a
#: developer box running hermes-gateway.service that would fail a random test
#: roughly once per hour of suite runtime — a new flake source, which is a bad
#: trade for a backstop.  In CI no gateway runs, so it is pure signal: set
#: ``HERMES_TEST_GUARD_REAL_HERMES_HOME=1`` there.
#:
#: This is defence-in-depth only.  The real containment is that
#: ``codex_session_reaper`` / ``codex_gc_watcher`` / ``codex_registry_gc``
#: resolve their home via ``get_hermes_home()``, which honours the per-test
#: ``HERMES_HOME`` that ``tests/conftest.py::_hermetic_environment`` pins for
#: every test.  See ``tests/gateway/test_codex_reaper_ledger_isolation.py``.
_GUARD_REAL_HOME_ENV = "HERMES_TEST_GUARD_REAL_HERMES_HOME"

#: Bound at import, before any test can monkeypatch them.  ``Path.iterdir`` and
#: ``Path.stat`` dispatch through ``os.listdir`` / ``os.stat`` at call time, and
#: several tests in this package replace those globally (e.g. a ``/proc`` walk
#: patched to raise ``OSError``).  A guard routed through the patched hooks fails
#: in two directions: it errors the *victim* test at teardown (a false red, and
#: attributed to the wrong test), and a patch returning a fixed listing would
#: make it silently blind (a false green — the worse half).  It must observe the
#: real filesystem, so it holds the real callables.
_REAL_LISTDIR = os.listdir
_REAL_STAT = os.stat


def _codex_reaper_state_fingerprint(
    directory: Path | None = None,
) -> dict[str, tuple[int, int]]:
    """Stat-only fingerprint (name -> size, mtime_ns) of the real reaper state.

    Deliberately stat-only: the ledger contains real session/thread ids, so this
    guard must never read, log, or diff record *contents*.

    ``directory`` defaults to the operator's real dir and exists so the guard's
    own regression test can point it at a tmpdir without monkeypatching module
    state that the guard is itself using at teardown.
    """
    result: dict[str, tuple[int, int]] = {}
    root = str(_REAL_CODEX_REAPER_DIR if directory is None else directory)
    try:
        names = sorted(_REAL_LISTDIR(root))
    except OSError:
        return result
    for name in names:
        try:
            st = _REAL_STAT(os.path.join(root, name))
        except OSError:
            continue
        result[name] = (st.st_size, st.st_mtime_ns)
    return result


@pytest.fixture(autouse=True)
def _guard_real_codex_reaper_state():
    """Fail the test that writes the operator's live reap ledger / tombstone archive.

    History: ``test_codex_gc_watcher.py``'s ``_FakeDispatcher`` sets no
    ``hermes_home`` and its broker is a ``MagicMock``, so the home-resolution
    probe fell through to ``Path.home() / ".hermes"`` and every ``_tick()``
    appended real JSONL to the operator's live ledger — ~1.8k records, on a file
    already unbounded at ~12.9 MB.  The callsite now uses ``get_hermes_home()``;
    this catches any future reintroduction *by any route*, including one that
    bypasses the callsites entirely.
    """
    if os.environ.get(_GUARD_REAL_HOME_ENV, "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        yield
        return

    before = _codex_reaper_state_fingerprint()
    yield
    after = _codex_reaper_state_fingerprint()
    if after == before:
        return
    changed = sorted(set(before) | set(after))
    delta = {
        name: (before.get(name, (0, 0))[0], after.get(name, (0, 0))[0])
        for name in changed
        if before.get(name) != after.get(name)
    }
    raise AssertionError(
        "this test mutated the operator's REAL codex-reaper state at "
        f"{_REAL_CODEX_REAPER_DIR} (file -> size before/after: {delta}). "
        "Either a home-resolution fallback escaped the per-test HERMES_HOME, "
        "or a live hermes-gateway ticked mid-run. To tell them apart: the test "
        "fixtures write records with a null worktree_path, a live tick does not."
    )


class _FakeEnumMember(str):
    """A python-telegram-bot-faithful stand-in for a ``StrEnum`` member.

    PTB constants (``ParseMode``, ``ChatType``) are ``StrEnum`` members:
    ``str(x)`` and equality give the *value* (``"supergroup"``) while
    ``repr(x)`` shows the qualified *member name*
    (``<ChatType.SUPERGROUP>``). Test stubs that pick only one of those
    shapes break the other consumer: plain strings fail assertions like
    ``"MARKDOWN_V2" in repr(parse_mode)``, while auto-generated MagicMock
    attributes fail the adapter's ``str(chat.type)`` normalization
    (``adapter.py`` ``_build_message_event``). This class satisfies both,
    so every telegram test sees the same semantics regardless of which
    file's mock installed first.
    """

    _qualname: str

    def __new__(cls, enum_name: str, member_name: str, value: str):
        obj = str.__new__(cls, value)
        obj._qualname = f"{enum_name}.{member_name}"
        return obj

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<{self._qualname}: {str.__repr__(self)}>"


def _fake_str_enum(enum_name: str, **members: str):
    """Build a ``SimpleNamespace``-like enum of :class:`_FakeEnumMember`."""
    from types import SimpleNamespace

    return SimpleNamespace(
        **{name: _FakeEnumMember(enum_name, name, value) for name, value in members.items()}
    )


def _ensure_telegram_mock() -> None:
    """Install a comprehensive telegram mock in sys.modules.

    Idempotent — skips when the real library is already imported.
    Uses ``sys.modules[name] = mod`` (overwrite) instead of
    ``setdefault`` so it wins even if a partial/broken import
    already cached a module with ``ChatType = None``.
    """
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return  # Real library is installed — nothing to mock

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    # One shared PTB-faithful enum namespace per constant, attached to BOTH
    # access paths: ``sys.modules["telegram.constants"]`` is registered as
    # the root mock below, so ``from telegram.constants import ParseMode``
    # resolves ``mod.ParseMode`` — while config/docs-style access reads
    # ``telegram.constants.ParseMode``. Binding the same object to both
    # keeps every consumer comparing against identical members.
    _parse_mode = _fake_str_enum(
        "ParseMode", MARKDOWN="Markdown", MARKDOWN_V2="MarkdownV2", HTML="HTML"
    )
    _chat_type = _fake_str_enum(
        "ChatType",
        PRIVATE="private",
        GROUP="group",
        SUPERGROUP="supergroup",
        CHANNEL="channel",
    )
    mod.ParseMode = _parse_mode
    mod.constants.ParseMode = _parse_mode
    mod.ChatType = _chat_type
    mod.constants.ChatType = _chat_type

    # Mirror PTB's exception hierarchy: BadRequest is a semantic API error,
    # but inherits from NetworkError in python-telegram-bot 22.x.
    mod.error.TelegramError = type("TelegramError", (Exception,), {})
    mod.error.NetworkError = type("NetworkError", (mod.error.TelegramError,), {})
    mod.error.TimedOut = type("TimedOut", (mod.error.NetworkError,), {})
    mod.error.BadRequest = type("BadRequest", (mod.error.NetworkError,), {})
    mod.error.Forbidden = type("Forbidden", (mod.error.TelegramError,), {})
    mod.error.InvalidToken = type("InvalidToken", (mod.error.TelegramError,), {})

    class RetryAfter(mod.error.TelegramError):
        def __init__(self, retry_after=1):
            self.retry_after = retry_after

    mod.error.RetryAfter = RetryAfter
    mod.error.Conflict = type("Conflict", (mod.error.TelegramError,), {})

    # Update.ALL_TYPES used in start_polling()
    mod.Update.ALL_TYPES = []

    for name in (
        "telegram",
        "telegram.ext",
        "telegram.constants",
        "telegram.request",
    ):
        sys.modules[name] = mod
    sys.modules["telegram.error"] = mod.error


def _ensure_discord_mock() -> None:
    """Install a comprehensive discord mock in sys.modules.

    Idempotent — skips when the real library is already imported.
    Uses ``sys.modules[name] = mod`` (overwrite) instead of
    ``setdefault`` so it wins even if a partial/broken import already
    cached the module.

    This mock is comprehensive — it includes **all** attributes needed by
    every gateway discord test file.  Individual test files should call
    this function (it short-circuits when already present) rather than
    maintaining their own mock setup.
    """
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return  # Real library is installed — nothing to mock

    from types import SimpleNamespace

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    discord_mod.Message = type("Message", (), {})

    # Embed: accept the kwargs production code / tests use
    # (title, description, color). MagicMock auto-attributes work too,
    # but some tests construct and inspect .title/.description directly.
    class _FakeEmbed:
        def __init__(self, *, title=None, description=None, color=None, **_):
            self.title = title
            self.description = description
            self.color = color
            self.fields = []
            self.footer = None
        def add_field(self, *, name=None, value=None, inline=False, **_):
            self.fields.append({"name": name, "value": value, "inline": inline})
            return self
        def set_footer(self, *, text=None, icon_url=None, **_):
            self.footer = {"text": text, "icon_url": icon_url}
            return self
    discord_mod.Embed = _FakeEmbed

    # ui.View / ui.Select / ui.Button: real classes (not MagicMock) so
    # tests that subclass ModelPickerView / iterate .children / clear
    # items work.
    class _FakeView:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.children = []
        def add_item(self, item):
            self.children.append(item)
        def clear_items(self):
            self.children.clear()

    class _FakeSelect:
        def __init__(self, *, placeholder=None, options=None, custom_id=None, **_):
            self.placeholder = placeholder
            self.options = options or []
            self.custom_id = custom_id
            self.callback = None
            self.disabled = False

    class _FakeButton:
        def __init__(self, *, label=None, style=None, custom_id=None, emoji=None,
                     url=None, disabled=False, row=None, sku_id=None, **_):
            self.label = label
            self.style = style
            self.custom_id = custom_id
            self.emoji = emoji
            self.url = url
            self.disabled = disabled
            self.row = row
            self.sku_id = sku_id
            self.callback = None

    class _FakeSelectOption:
        def __init__(self, *, label=None, value=None, description=None, **_):
            self.label = label
            self.value = value
            self.description = description
    discord_mod.SelectOption = _FakeSelectOption

    # AudioSource: real class so VoiceMixer(discord.AudioSource) can subclass
    # it cleanly in tests.  MagicMock auto-attributes would make is_opus()
    # return a Mock instead of False, breaking 9 TestVoiceMixerCore tests.
    class _FakeAudioSource:
        def is_opus(self):
            return False
        def read(self):
            return b"\x00" * 3840  # one silent stereo s16 frame
        def cleanup(self):
            pass
    discord_mod.AudioSource = _FakeAudioSource

    discord_mod.ui = SimpleNamespace(
        View=_FakeView,
        Select=_FakeSelect,
        Button=_FakeButton,
        button=lambda *a, **k: (lambda fn: fn),
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1, primary=2, secondary=2, danger=3,
        green=1, grey=2, blurple=2, red=3,
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: 1, green=lambda: 2, blue=lambda: 3,
        red=lambda: 4, purple=lambda: 5, greyple=lambda: 6,
        gold=lambda: 7,
    )

    # app_commands — needed by _register_slash_commands auto-registration
    class _FakeGroup:
        def __init__(self, *, name, description, parent=None):
            self.name = name
            self.description = description
            self.parent = parent
            self._children: dict = {}
            if parent is not None:
                parent.add_command(self)

        def add_command(self, cmd):
            self._children[cmd.name] = cmd

    class _FakeCommand:
        def __init__(self, *, name, description, callback, parent=None):
            self.name = name
            self.description = description
            self.callback = callback
            self.parent = parent

    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
        Group=_FakeGroup,
        Command=_FakeCommand,
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    for name in ("discord", "discord.ext", "discord.ext.commands"):
        sys.modules[name] = discord_mod
    sys.modules["discord.ext"] = ext_mod
    sys.modules["discord.ext.commands"] = commands_mod


# Run at collection time — before any test file's module-level imports.
_ensure_telegram_mock()
_ensure_discord_mock()


# ---------------------------------------------------------------------------
# Plugin-adapter anti-pattern guard
# ---------------------------------------------------------------------------

_GATEWAY_DIR = Path(__file__).resolve().parent
_GUARD_HINT = (
    "Plugin adapter tests must use "
    "``from tests.gateway._plugin_adapter_loader import load_plugin_adapter`` "
    "and call ``load_plugin_adapter('<plugin_name>')`` instead of inserting "
    "``plugins/platforms/<name>/`` on sys.path and doing a bare ``import "
    "adapter`` / ``from adapter import ...``. See the 'Plugin-adapter "
    "anti-pattern guard' docstring in tests/gateway/conftest.py."
)


def _scan_for_plugin_adapter_antipattern(source: str) -> list[str]:
    """Return a list of offending-line descriptions, or [] if clean.

    Flags two things:
    1. ``sys.path.insert(..., <something mentioning 'plugins/platforms'>)``
    2. ``import adapter`` or ``from adapter import ...`` at module level.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # Let pytest surface the real syntax error.

    offenses: list[str] = []

    for node in ast.walk(tree):
        # sys.path.insert(0, ".../plugins/platforms/...")
        if isinstance(node, ast.Call):
            func = node.func
            target_name: str | None = None
            if isinstance(func, ast.Attribute):
                # sys.path.insert / sys.path.append
                if (
                    isinstance(func.value, ast.Attribute)
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "sys"
                    and func.value.attr == "path"
                    and func.attr in {"insert", "append", "extend"}
                ):
                    target_name = f"sys.path.{func.attr}"

            if target_name is not None:
                call_src = ast.unparse(node)
                # Match both the string-literal form
                # ``.../plugins/platforms/...`` and the Path-operator form
                # ``Path(...) / 'plugins' / 'platforms' / ...`` that
                # plugin tests typically use.
                _src_no_ws = "".join(call_src.split())
                if (
                    "plugins/platforms" in call_src
                    or "plugins\\platforms" in call_src
                    or "'plugins'/'platforms'" in _src_no_ws
                    or '"plugins"/"platforms"' in _src_no_ws
                ):
                    offenses.append(
                        f"line {node.lineno}: {target_name}(...) points into "
                        f"plugins/platforms/"
                    )

    # Bare `import adapter` / `from adapter import ...` anywhere (module level
    # OR inside functions — both are symptoms of the same pattern).
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "adapter":
                    offenses.append(
                        f"line {node.lineno}: ``import adapter`` "
                        f"(bare — resolves to whichever plugin's adapter.py "
                        f"is first on sys.path)"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module == "adapter" and node.level == 0:
                offenses.append(
                    f"line {node.lineno}: ``from adapter import ...`` "
                    f"(bare — resolves to whichever plugin's adapter.py "
                    f"is first on sys.path)"
                )

    return offenses


def _fingerprint_gateway_tests() -> str:
    """Return a short fingerprint that changes when any gateway test file changes.

    Uses (mtime, size) pairs instead of content hashing — fast to compute
    (stat-only, no reads) and sufficient for cache invalidation across
    per-file subprocess runs.
    """
    import hashlib

    h = hashlib.sha256()
    for path in sorted(_GATEWAY_DIR.rglob("test_*.py")):
        try:
            st = path.stat()
            h.update(f"{path.name}:{st.st_mtime_ns}:{st.st_size}".encode())
        except OSError:
            h.update(f"{path.name}:missing".encode())
    return h.hexdigest()[:16]


def _run_adapter_antipattern_scan() -> list[str]:
    """Scan gateway test files for the plugin-adapter anti-pattern.

    Returns a list of violation strings (empty if clean).
    """
    violations: list[str] = []
    for path in _GATEWAY_DIR.rglob("test_*.py"):
        if path.name in {"_plugin_adapter_loader.py", "conftest.py"}:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Fast string pre-filter: skip files that can't possibly violate.
        # A violating file MUST contain both (a) an adapter/plugins/platforms
        # reference AND (b) either sys.path manipulation or a bare adapter import.
        if "adapter" not in source and "plugins/platforms" not in source:
            continue
        if not (
            "sys.path" in source
            or "import adapter" in source
            or "from adapter import" in source
        ):
            continue
        offenses = _scan_for_plugin_adapter_antipattern(source)
        if offenses:
            violations.append(
                f"  {path.relative_to(_GATEWAY_DIR.parent.parent)}:\n    "
                + "\n    ".join(offenses)
            )
    return violations


def pytest_configure(config):
    """Reject plugin-adapter tests that use the sys.path anti-pattern.

    Runs once per pytest session on the controller, BEFORE any xdist
    worker is spawned. If any file under ``tests/gateway/`` matches the
    anti-pattern, we fail the whole session with a clear message —
    before a polluted ``sys.path`` can cascade across workers.

    **Performance**: in the per-file subprocess isolation model (no xdist),
    every subprocess is a "controller" — so the naive scan would run 257
    times, each costing ~1s of AST walking.  We avoid this with two
    strategies:

    1. **Tight string pre-filter**: a file can only violate if it contains
       *both* an adapter/plugins/platforms reference *and* a sys.path
       manipulation or bare ``import adapter``.  This drops ~95% of files
       from needing AST parsing.
    2. **File-locked cache**: the scan result is cached in
       ``.pytest-cache/gw-adapter-guard-<fingerprint>`` keyed on a
       fingerprint of the gateway test file mtimes/sizes.  Concurrent
       subprocesses acquire a lock; only the first performs the scan;
       the rest wait and read the cached result.
    """
    # Only run on the xdist controller (or in non-xdist runs). Skip on
    # worker subprocesses so we don't scan the filesystem N times.
    if hasattr(config, "workerinput"):
        return

    fp = _fingerprint_gateway_tests()
    cache_dir = Path.cwd() / ".pytest-cache"
    cache_file = cache_dir / f"gw-adapter-guard-{fp}"
    lock_file = cache_dir / f".gw-adapter-guard-{fp}.lock"

    cache_dir.mkdir(parents=True, exist_ok=True)

    # Evict stale cache entries from previous fingerprints (best-effort).
    try:
        for old in cache_dir.glob("gw-adapter-guard-*"):
            if old.name != f"gw-adapter-guard-{fp}":
                old.unlink(missing_ok=True)
        for old in cache_dir.glob(".gw-adapter-guard-*.lock"):
            if old.name != f".gw-adapter-guard-{fp}.lock":
                old.unlink(missing_ok=True)
    except OSError:
        pass  # Non-critical; old files are harmless.

    # Use filelock to ensure only one process scans at a time.
    # Concurrent subprocesses all hit pytest_configure simultaneously;
    # without a lock they'd all find no cache and all run the scan.
    try:
        from filelock import FileLock
        lock = FileLock(str(lock_file), timeout=120)
    except ImportError:
        # Fallback: no locking (still correct, just slower under contention).

        class _NoLock:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
        lock = _NoLock()

    with lock:
        if cache_file.exists():
            cached = cache_file.read_text(encoding="utf-8")
            if cached == "clean":
                return
            raise pytest.UsageError(cached)

        # Slow path: this process is the first to acquire the lock.
        violations = _run_adapter_antipattern_scan()

        if violations:
            msg = (
                "Plugin-adapter-import anti-pattern detected in gateway tests:\n"
                + "\n".join(violations)
                + "\n\n"
                + _GUARD_HINT
            )
            cache_file.write_text(msg, encoding="utf-8")
            raise pytest.UsageError(msg)
        else:
            cache_file.write_text("clean", encoding="utf-8")

