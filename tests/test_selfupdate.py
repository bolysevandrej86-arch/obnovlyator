# -*- coding: utf-8 -*-
"""State-автомат самообновления: booted/py_started/no_boot, финализация."""
import hashlib

import selfupdate
from tests.conftest import write_state


# --- version_newer ---------------------------------------------------------
def test_version_newer():
    assert selfupdate.version_newer("1.7.5.1", "1.7.3")
    assert selfupdate.version_newer("1.7.5.2", "1.7.5.1")
    assert not selfupdate.version_newer("1.7.5", "1.7.5.1")
    assert not selfupdate.version_newer("1.7.5.1", "1.7.5.1")


# --- handle_startup_state --------------------------------------------------
def test_first_start_marks_py_started(updater):
    write_state(updater)
    s = updater.handle_startup_state()
    assert s["py_started"] is True and s["fail_count"] == 0
    assert not s.get("no_boot")


def test_booted_instance_resets_counters(updater):
    write_state(updater, py_started=True, booted=True, fail_count=1)
    s = updater.handle_startup_state()
    assert s["fail_count"] == 0 and s["no_boot"] == 0 and not s["booted"]


def test_py_fail_rolls_back_after_3(updater):
    write_state(updater)
    updater.handle_startup_state()
    write_state(updater, py_started=True, fail_count=0)
    assert updater.handle_startup_state()["fail_count"] == 1
    write_state(updater, py_started=True, fail_count=1)
    assert updater.handle_startup_state()["fail_count"] == 2
    assert updater.paths.state_file.exists()      # порог ещё не достигнут
    write_state(updater, py_started=True, fail_count=2)
    updater.handle_startup_state()
    assert not updater.paths.state_file.exists()  # откат
    assert updater._skipped == ["1.7.5.5"]        # битая версия помечена


def test_no_boot_rolls_back_after_4_without_skip(updater):
    write_state(updater, no_boot=1)
    assert updater.handle_startup_state()["no_boot"] == 2
    write_state(updater, no_boot=2)
    assert updater.handle_startup_state()["no_boot"] == 3
    write_state(updater, no_boot=3)
    updater.handle_startup_state()
    assert not updater.paths.state_file.exists()
    assert updater._skipped == [], "no_boot-откат НЕ помечает версию"


# --- mark_booted / finalize_update ----------------------------------------
def test_mark_booted(updater):
    write_state(updater)
    updater.mark_booted()
    assert selfupdate._read_json(updater.paths.state_file)["booted"] is True


def test_finalize_without_state_is_silent(updater):
    updater.finalize_update()          # не должно упасть и ничего не записать
    assert not updater.paths.state_file.exists()


def test_finalize_records_once(updater, monkeypatch):
    import selfupdate
    hist, toasts = [], []
    monkeypatch.setattr(selfupdate, "record_history", lambda **kw: hist.append(kw))
    monkeypatch.setattr(selfupdate, "notify_selfupdate",
                        lambda t, m: toasts.append(m))
    write_state(updater)
    updater.finalize_update()
    assert len(hist) == 1 and hist[0]["status"] == "ok"
    assert hist[0]["to_version"] == "1.7.5.5"
    assert len(toasts) == 1 and "1.7.5.5" in toasts[0]
    assert not updater.paths.state_file.exists()
    updater.finalize_update()          # повтор — тишина
    assert len(hist) == 1 and len(toasts) == 1


# --- download: staged reuse ------------------------------------------------
def test_download_reuses_valid_staged(updater, tmp_path):
    data = b"Obnovlyator-fake-binary-" * 1000
    final = updater.paths.staged_exe("9.9.9")
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(data)
    info = selfupdate.UpdateInfo(
        version="9.9.9", channel="stable",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data), url="https://example.invalid/x.exe")
    assert updater.download(info) == final
    assert final.read_bytes() == data
    final.unlink(missing_ok=True)
