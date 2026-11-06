# -*- coding: utf-8 -*-
"""1.7.6: Store/UWP-обновления — парсер MSIX, upgrade_command, store_update_scan."""
import pytest

import core
import diag
from core import Config, PackageUpdate

# колонки как в реальном выводе winget: позиции слов заголовка задают срезы
COL_NAME, COL_ID, COL_VER, COL_AVAIL = 0, 30, 100, 116
_HEADER = ("Имя".ljust(COL_ID) + "ИД".ljust(COL_VER - COL_ID)
           + "Версия".ljust(COL_AVAIL - COL_VER) + "Доступно")
_DASH = "-" * len(_HEADER)


def _row(name, pkg_id, version="", available=""):
    return (name.ljust(COL_ID) + pkg_id.ljust(COL_VER - COL_ID)
            + version.ljust(COL_AVAIL - COL_VER) + available)


# --- parse_winget_table: MSIX-строки ----------------------------------------
def test_parse_msix_row_with_versions():
    table = "\n".join([
        _HEADER, _DASH,
        _row("Paint", "MSIX\\Microsoft.Paint_11.2609.401.0_x64__8wekyb3d8bbwe",
             "11.2608.2.0", "11.2609.401.0"),
        _row("Git", "Git.Git", "2.56.0", "2.57.0"),
    ])
    pkgs = core.parse_winget_table(table)
    assert [p.source for p in pkgs] == ["msstore", "winget"]
    ms = pkgs[0]
    assert ms.pkg_id.startswith("MSIX\\")
    assert ms.name == "Paint" and ms.available == "11.2609.401.0"


def test_parse_msix_row_without_versions_skipped():
    # таблица без колонок версий (каталог всего установленного) —
    # MSIX-строки не превращаются в карточки
    text = "\n".join([
        "Имя                                          ИД",
        "-" * 78,
        "Blender         MSIX\\BlenderFoundation.Blender_5.2.3.0_x64__ppwjx1n5r4v9t",
        "Яндекс.Музыка   MSIX\\A025C540.Yandex.Music_4.54.8517.0_x64__vfvw9svesycw6",
    ])
    assert core.parse_winget_table(text) == []


def test_parse_garbage_row_after_msix_stops_cleanly():
    table = "\n".join([
        _HEADER, _DASH,
        _row("Paint", "MSIX\\X_1.0_x64__h", "1.0", "1.1"),
        "Итоговая строка без валидного идентификатора",
    ])
    pkgs = core.parse_winget_table(table)
    assert len(pkgs) == 1 and pkgs[0].source == "msstore"


# --- upgrade_command для msstore ----------------------------------------------
def test_upgrade_command_msstore(monkeypatch):
    monkeypatch.setattr(core, "winget_exe", lambda: "winget.exe")
    pkg = PackageUpdate(source="msstore",
                        pkg_id="MSIX\\Microsoft.Paint_11.0.0_x64__8wekyb3d8bbwe",
                        name="Paint", version="11.0.0", available="11.1.0")
    cmd = core.upgrade_command(Config(), pkg)
    assert cmd[0] == "winget.exe" and cmd[1] == "upgrade"
    assert "--id" in cmd and "--silent" in cmd
    assert "--exact" not in cmd     # полный MSIX-идентификатор и так уникален


# --- store_update_scan ----------------------------------------------------------
def test_store_scan_needs_admin(monkeypatch):
    monkeypatch.setattr(diag, "is_admin_elevated", lambda: False)
    res = diag.store_update_scan()
    assert res["status"] == "info" and "администратора" in res["text"]


def test_store_scan_ok(monkeypatch):
    monkeypatch.setattr(diag, "is_admin_elevated", lambda: True)
    monkeypatch.setattr(diag, "_ps", lambda s, timeout=0: (0, "SCAN_OK"))
    res = diag.store_update_scan()
    assert res["status"] == "ok" and "Store" in res["text"]


def test_store_scan_error(monkeypatch):
    monkeypatch.setattr(diag, "is_admin_elevated", lambda: True)
    monkeypatch.setattr(diag, "_ps", lambda s, timeout=0: (1, "SCAN_ERR -2147024891"))
    res = diag.store_update_scan()
    assert res["status"] == "error" and "SCAN_ERR" in res["text"]


# --- конфиг ----------------------------------------------------------------------
def test_update_store_default_on():
    assert Config().update_store is True
