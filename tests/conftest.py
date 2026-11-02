# -*- coding: utf-8 -*-
"""Общая обстановка для тестов Обновлятора.

- путь репозитория в sys.path (pytest запускается из корня, но на всякий);
- изолированный %LOCALAPPDATA% — SelfUpdatePaths/selfupdate не трогают
  реальные данные машины; ставится ДО импорта selfupdate тестами;
- тишина: тосты и запись в историю перехватываются фикстурами.
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="obnovlyator_tests_lad_")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _quiet_selfupdate(monkeypatch):
    """Никаких тостов и записей в реальную history.jsonl во время тестов."""
    import selfupdate
    monkeypatch.setattr(selfupdate, "notify_selfupdate", lambda *a, **k: None)
    monkeypatch.setattr(selfupdate, "record_history", lambda **kw: None)


@pytest.fixture()
def updater():
    """Чистый SelfUpdater с временными путями и пустым state-файлом."""
    import selfupdate
    up = selfupdate.SelfUpdater(current_version="1.7.5.5", repo="x/y")
    up.mark_skip = lambda v: getattr(up, "_skipped", []).append(v)
    up._skipped = []
    up.paths.state_file.unlink(missing_ok=True)
    return up


def write_state(up, **kw):
    import selfupdate
    base = {"phase": "verifying", "from": "1.7.5.4", "to": "1.7.5.5",
            "expected_sha256": "", "backup": "X:/nope/backup.exe",
            "fail_count": 0, "ts": 0.0}
    base.update(kw)
    selfupdate._write_json_atomic(up.paths.state_file, base)
    return base
