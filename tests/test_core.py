# -*- coding: utf-8 -*-
"""core: команды удаления, python_args, кэш скана Engine."""
import queue
import sys
import time

import pytest

import core
from core import Config, Engine, PackageUpdate


# --- winget_uninstall_command ------------------------------------------------
def test_uninstall_command_by_id(monkeypatch):
    monkeypatch.setattr(core, "winget_exe", lambda: "winget.exe")
    cmd = core.winget_uninstall_command(pkg_id="Git.Git")
    assert cmd[1] == "uninstall" and "--id" in cmd and "Git.Git" in cmd
    assert "--exact" in cmd and "--silent" in cmd


def test_uninstall_command_by_name(monkeypatch):
    monkeypatch.setattr(core, "winget_exe", lambda: "winget.exe")
    cmd = core.winget_uninstall_command(name="7-Zip")
    assert "--name" in cmd and "7-Zip" in cmd and "--exact" in cmd


def test_uninstall_command_without_winget(monkeypatch):
    monkeypatch.setattr(core, "winget_exe", lambda: None)
    assert core.winget_uninstall_command(name="x") is None


# --- python_args ---------------------------------------------------------------
def test_python_args_dev_mode():
    assert core.python_args("-m", "pip") == [sys.executable, "-m", "pip"]


def test_python_args_frozen_prefers_real_python(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(core.shutil, "which",
                        lambda n: r"C:\Python313\python.exe"
                        if n == "python.exe" else None)
    assert core.python_args("-m", "pip") == [r"C:\Python313\python.exe", "-m", "pip"]


def test_python_args_frozen_skips_store_stub(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def fake_which(name):
        if name == "python.exe":
            return r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\python.exe"
        return None
    monkeypatch.setattr(core.shutil, "which", fake_which)
    assert core.python_args() is None       # заглушка Store — не python


def test_python_args_frozen_py_launcher(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(core.shutil, "which",
                        lambda n: r"C:\Windows\py.exe" if n == "py.exe" else None)
    assert core.python_args("-m", "pip") == [r"C:\Windows\py.exe", "-3", "-m", "pip"]


# --- кэш скана Engine -----------------------------------------------------------
def _engine():
    return Engine(Config(), queue.Queue())


def _pkg(key_id="Git.Git"):
    return PackageUpdate(source="winget", pkg_id=key_id, name="Git",
                         version="2.56", available="2.57")


def test_scan_cache_hit_does_not_spawn_thread():
    eng = _engine()
    eng._last_scan = (time.time(), [_pkg()])
    eng.start_scan(force=False)
    evs = _drain(eng.events)
    kinds = {e["type"] for e in evs}
    assert {"scanned", "phase"} <= kinds
    assert "winget:Git.Git" in eng.packages
    assert eng._thread is None, "кэш не должен запускать winget"


def test_scan_cache_stale_spawns_real_scan():
    eng = _engine()
    eng._last_scan = (time.time() - 10 * core.Engine.SCAN_CACHE_SEC, [_pkg()])
    eng.cfg.update_pip = False
    eng.start_scan(force=False)
    assert eng._thread is not None
    eng.cancel()


def test_scan_force_bypasses_cache():
    eng = _engine()
    eng._last_scan = (time.time(), [_pkg()])
    eng.cfg.update_pip = False
    eng.start_scan(force=True)
    assert eng._thread is not None
    eng.cancel()


def _drain(q, limit=8):
    evs = []
    for _ in range(limit):
        try:
            evs.append(q.get(timeout=2))
        except queue.Empty:
            break
    return evs
