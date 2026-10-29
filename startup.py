# -*- coding: utf-8 -*-
"""Менеджер автозагрузки: программы, стартующие вместе с Windows.

Источники:
  - разделы реестра Run (HKCU + HKLM, включая WOW6432Node для HKLM);
  - папки автозагрузки (личная и общая).

Отключение — ОБРАТИМО и никогда не удаляет: записи реестра переносятся
в наше хранилище RunDisabled (и возвращаются обратно), ярлыки папок
переименовываются в *.disabled.
"""
from __future__ import annotations

import os
import winreg
from pathlib import Path

from bloat import match_bloat

_DISABLED_KEY = r"Software\Microsoft\Windows\CurrentVersion\RunDisabled_Obnovlyator"
_RUN_PATHS = (
    (winreg.HKEY_CURRENT_USER, winreg.KEY_SET_VALUE | winreg.KEY_READ, False,
     r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU"),
    (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ, False,
     r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM"),
    (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ, True,
     r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "HKLM"),
)


def _startup_dirs() -> list[tuple[Path, bool]]:
    """(папка, общая?) — личная и общая автозагрузка."""
    dirs = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append((Path(appdata) / "Microsoft" / "Windows"
                     / "Start Menu" / "Programs" / "Startup", False))
    pd = os.environ.get("ProgramData")
    if pd:
        dirs.append((Path(pd) / "Microsoft" / "Windows"
                     / "Start Menu" / "Programs" / "StartUp", True))
    return [(d, common) for d, common in dirs if d.exists()]


def list_entries() -> list[dict]:
    """Все записи автозагрузки.

    Каждая: {id, name, command, source, writable, common, disabled_store,
             suspicious} — id уникален и стабилен между сканами.
    """
    entries: list[dict] = []

    # --- реестр: активные -------------------------------------------
    for hive, _mode, wow, path, who in _RUN_PATHS:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, value, _type = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not name or not str(value).strip():
                    continue
                writable = hive == winreg.HKEY_CURRENT_USER
                entries.append({
                    "id": f"reg:{who}:{path}:{name}",
                    "name": name,
                    "command": str(value),
                    "source": f"Реестр {who} (Run)",
                    "writable": writable,
                    "disabled_store": False,
                    "kind": "reg",
                    "hive": hive,
                    "reg_path": path,
                    "reg_name": name,
                })

    # --- реестр: отложенные нами -------------------------------------
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _DISABLED_KEY)
    except OSError:
        key = None
    if key is not None:
        with key:
            i = 0
            while True:
                try:
                    name, value, _type = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not name:
                    continue
                entries.append({
                    "id": f"regdis:{name}",
                    "name": name,
                    "command": str(value),
                    "source": "Реестр HKCU (отключено Обновлятором)",
                    "writable": True,
                    "disabled_store": True,
                    "kind": "regdis",
                    "reg_name": name,
                })

    # --- папки автозагрузки -------------------------------------------
    for d, common in _startup_dirs():
        try:
            files = list(d.iterdir())
        except OSError:
            continue
        who = "Общая" if common else "Личная"
        for f in files:
            if f.suffix.lower() not in (".lnk", ".exe", ".bat", ".url"):
                continue
            disabled = f.name.lower().endswith(".disabled")
            name = f.stem[:-9] if disabled else f.stem
            entries.append({
                "id": f"dir:{f}",
                "name": name,
                "command": str(f),
                "source": f"Папка автозагрузки ({who})",
                "writable": True,
                "disabled_store": disabled,
                "kind": "dir",
                "path": str(f),
                "common": common,
            })

    for e in entries:
        e["suspicious"] = match_bloat(e["name"]) or match_bloat(e["command"])
    entries.sort(key=lambda x: (x["disabled_store"], x["name"].lower()))
    return entries


def disable_entry(entry: dict) -> tuple[bool, str]:
    """Отключает запись (обратимо). HKLM — только от администратора."""
    if entry["kind"] == "reg":
        if not entry["writable"]:
            return False, "запись в HKLM — нужны права администратора"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, entry["reg_path"],
                                0, winreg.KEY_ALL_ACCESS) as src:
                value, vtype = winreg.QueryValueEx(src, entry["reg_name"])
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _DISABLED_KEY) as dst:
                winreg.SetValueEx(dst, entry["reg_name"], 0, vtype, value)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, entry["reg_path"],
                                0, winreg.KEY_SET_VALUE) as src:
                winreg.DeleteValue(src, entry["reg_name"])
            return True, "отключена (вернуть можно там же)"
        except OSError as e:
            return False, str(e)
    if entry["kind"] == "dir":
        try:
            p = Path(entry["path"])
            p.rename(p.with_name(p.name + ".disabled"))
            return True, "отключена"
        except OSError as e:
            return False, str(e)
    return False, "уже отключена"


def enable_entry(entry: dict) -> tuple[bool, str]:
    """Возвращает ранее отключённую запись на место."""
    if entry["kind"] == "regdis":
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _DISABLED_KEY,
                                0, winreg.KEY_ALL_ACCESS) as src:
                value, vtype = winreg.QueryValueEx(src, entry["reg_name"])
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE) as dst:
                winreg.SetValueEx(dst, entry["reg_name"], 0, vtype, value)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _DISABLED_KEY,
                                0, winreg.KEY_ALL_ACCESS) as src:
                winreg.DeleteValue(src, entry["reg_name"])
            return True, "включена"
        except OSError as e:
            return False, str(e)
    if entry["kind"] == "dir":
        try:
            p = Path(entry["path"])
            if p.name.lower().endswith(".disabled"):
                p.rename(p.with_name(p.name[:-9]))
            return True, "включена"
        except OSError as e:
            return False, str(e)
    return False, "запись активна"
