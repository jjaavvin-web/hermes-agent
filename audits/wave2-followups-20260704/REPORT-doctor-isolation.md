# T1 summary — doctor test isolation from live state.db

- **Task:** Stop `tests/hermes_cli/test_doctor.py` from letting `run_doctor()` probe the live `~/.hermes/state.db` when tests only patched `os.environ["HERMES_HOME"]`.
- **Changed files:** `tests/hermes_cli/test_doctor.py` only, plus this required report artifact.
- **Production source:** `hermes_cli/doctor.py` was read-only reference only; no production rewrite.
- **Result:** Added a RED documentation test proving `monkeypatch.setenv("HERMES_HOME", ...)` does **not** redirect imported `doctor_mod.HERMES_HOME`; added a shared `_isolate_doctor_paths()` helper that patches `doctor_mod.HERMES_HOME`, `PROJECT_ROOT`, and `_DHH`; updated the GitHub-token doctor tests to use that helper; added an autouse SQLite guard that fails if any doctor test opens the real `~/.hermes/state.db`.
- **Verification:** Focused RED/GREEN subset passed (`5 passed`), whole file passed (`68 passed`, no timeout banners), and Ruff passed.
- **Kanban:** No `HERMES_KANBAN_TASK`, `HERMES_KANBAN_TASK_ID`, or board env was present in this webhook lane, so no Kanban card/comment target was available. Diff/evidence is captured here instead.
- **SOUL/MVMS law:** Applied MOTHERSHIP SOUL “MVMS recall + write-back law (loki lanes)” and “Handoff law”; MVMS completion write-back remains required before final closeout.

## Recall

MVMS recall found relevant state-hygiene/test-isolation lessons:

- `ee75eaed-262e-4e60-8c6c-0501b4d4fd09` — lecture-pipeline tests touched real production state when HOME was not isolated.
- `28698a30-b703-470e-9858-ad286a17f86c` — dynamic verification must be run before committing.
- `65464428-23d7-457b-b22e-59fb17ee17ea` — pair focused test/ruff gates with hard rails and evidence.

## RED evidence

Command:

```bash
PY="venv/bin/python"; [ -x "$PY" ] || PY=python3
"$PY" - <<'PY'
import os
from pathlib import Path
import tempfile
import hermes_cli.doctor as doctor_mod
before = doctor_mod.HERMES_HOME
with tempfile.TemporaryDirectory() as d:
    target = Path(d) / '.hermes'
    target.mkdir()
    os.environ['HERMES_HOME'] = str(target)
    print(f'imported_constant={before}')
    print(f'env_target={target}')
    assert doctor_mod.HERMES_HOME == target, (
        'RED: monkeypatch.setenv/os.environ alone does not redirect '
        'import-time doctor_mod.HERMES_HOME'
    )
PY
```

Output:

```text
imported_constant=/home/josep/.hermes
env_target=/tmp/tmphu7ngyjv/.hermes
Traceback (most recent call last):
  File "<stdin>", line 12, in <module>
AssertionError: RED: monkeypatch.setenv/os.environ alone does not redirect import-time doctor_mod.HERMES_HOME
```

## GREEN implementation evidence

### Focused regression subset

Command:

```bash
PY="venv/bin/python"; [ -x "$PY" ] || PY=python3
"$PY" -m pytest \
  tests/hermes_cli/test_doctor.py::test_setenv_alone_does_not_redirect_imported_doctor_home \
  tests/hermes_cli/test_doctor.py::test_isolate_doctor_paths_updates_all_import_time_doctor_constants \
  tests/hermes_cli/test_doctor.py::TestGitHubTokenCheck \
  -q -o 'addopts='
```

Output:

```text
.....                                                                    [100%]
5 passed, 1 warning in 3.24s
```

### Whole file

Command:

```bash
PY="venv/bin/python"; [ -x "$PY" ] || PY=python3
"$PY" -m pytest tests/hermes_cli/test_doctor.py -q -o 'addopts='
```

Output:

```text
....................................................................     [100%]
68 passed, 1 warning in 24.53s
```

No timeout banners appeared.

### Ruff

Command:

```bash
PY="venv/bin/python"; [ -x "$PY" ] || PY=python3
"$PY" -m ruff check tests/hermes_cli/test_doctor.py
```

Output:

```text
All checks passed!
```

## Diff summary

- Added `_LIVE_STATE_DB` and an autouse `_fail_if_doctor_probes_live_state_db` fixture that wraps `sqlite3.connect` and fails if the normalized database path is the real user `~/.hermes/state.db`.
- Added `_isolate_doctor_paths()` test helper to set env plus patch `doctor_mod.HERMES_HOME`, `doctor_mod.PROJECT_ROOT`, and `doctor_mod._DHH` together.
- Added RED documentation test for env-only non-redirection.
- Added GREEN isolation test proving all three import-time doctor constants are redirected.
- Updated `TestGitHubTokenCheck` tests to use `doctor_mod.run_doctor()` after direct constant isolation instead of importing `run_doctor` while only setting `HERMES_HOME` in the process environment.

## Gates preserved

- No `hermes_cli/doctor.py` edits.
- No gateway/dashboard/service restart.
- No provider/config/credential/security mutation.
- No git reset/checkout/switch/branch command executed.
- No push/PR/merge.

## Remaining risk / next gate

Only local lane commit is in scope. Push/PR/merge remains an explicit human/Fable gate.
