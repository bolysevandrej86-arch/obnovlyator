# -*- coding: utf-8 -*-
"""Поиск «бесполезных» программ: известный предустановленный мусор,
триалы и магазинные приложения, которые редко кому нужны.

Список — только кандидаты с пояснением, решение об удалении за пользователем.
Ничего само не удаляется.
"""
from __future__ import annotations

import winreg
from dataclasses import dataclass

from core import run_cmd


def run_ps(command: str, *, timeout: float = 120):
    """PowerShell-команда с объединённым выводом и скрытым окном."""
    return run_cmd(["powershell", "-NoProfile", "-Command", command], timeout=timeout)

UNINSTALL_PATHS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)

# (фрагмент имени программы без учёта регистра, чем она подозрительна)
BLOAT_PATTERNS = [
    ("candy crush", "казуальная игра с рекламой"),
    ("soda saga", "казуальная игра с рекламой"),
    ("farmville", "казуальная игра с рекламой"),
    ("wildtangent", "предустановленные игры OEM"),
    ("solitaire collection", "пасьянсы Microsoft"),
    ("microsoft mahjong", "игра Microsoft"),
    ("bing news", "лента новостей Bing"),
    ("bing weather", "погода Bing"),
    ("msn news", "лента новостей MSN"),
    ("msn weather", "погода MSN"),
    ("get started", "вводная обучалка Windows"),
    ("начало работы", "вводная обучалка Windows"),
    ("советы", "советы и подсказки Windows"),
    ("tips", "советы и подсказки Windows"),
    ("feedback hub", "центр отзывов Microsoft"),
    ("центр отзывов", "центр отзывов Microsoft"),
    ("mixed reality", "портал смешанной реальности (устарел)"),
    ("смешанной реальности", "портал смешанной реальности (устарел)"),
    ("3d viewer", "просмотр 3D-объектов"),
    ("просмотр 3d", "просмотр 3D-объектов"),
    ("print 3d", "печать 3D"),
    ("печать 3d", "печать 3D"),
    ("cortana", "помощник Cortana"),
    ("кортана", "помощник Cortana"),
    ("yourphone", "связь с телефоном"),
    ("phone link", "связь с телефоном"),
    ("связь с телефоном", "связь с телефоном"),
    ("paid wi-fi", "платный Wi-Fi"),
    ("oneconnect", "платный Wi-Fi"),
    ("mcafee", "антивирус-триал, навязывается при покупке ПК"),
    ("norton", "антивирус, часто триал от OEM"),
    ("ask toolbar", "рекламная панель в браузер"),
    ("tiktok", "магазинное приложение (если не пользуетесь)"),
    ("instagram", "магазинное приложение (если не пользуетесь)"),
    ("facebook", "магазинное приложение (если не пользуетесь)"),
    ("twitter", "магазинное приложение (если не пользуетесь)"),
    ("netflix", "стриминг (если не пользуетесь)"),
    ("spotify", "стриминг (если не пользуетесь)"),
    ("advertising screen", "рекламный экран блокировки OEM"),
    ("wild games", "предустановленные игры OEM"),
]


@dataclass
class InstalledApp:
    name: str
    version: str
    publisher: str
    install_date: str      # YYYYMMDD как в реестре, или ""
    size_kb: int           # EstimatedSize из реестра, 0 — неизвестно
    uninstall_string: str
    reason: str = ""       # непустой = подозрительная


def match_bloat(name: str) -> str:
    low = name.lower()
    for pattern, reason in BLOAT_PATTERNS:
        if pattern in low:
            return reason
    return ""


def list_installed() -> list[InstalledApp]:
    """Все установленные программы из разделов Uninstall (3 куста)."""
    apps: list[InstalledApp] = []
    seen: set[tuple[str, str]] = set()
    for hive, path in UNINSTALL_PATHS:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            i = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, sub_name) as key:
                        def rd(value_name: str) -> str:
                            try:
                                value, _ = winreg.QueryValueEx(key, value_name)
                                return str(value)
                            except OSError:
                                return ""

                        display = rd("DisplayName").strip()
                        if not display or display.lower().startswith("kb"):
                            continue  # обновления Windows — не программы
                        if rd("SystemComponent") == "1":
                            continue
                        try:
                            size_kb = int(rd("EstimatedSize") or 0)
                        except ValueError:
                            size_kb = 0
                        app = InstalledApp(
                            name=display,
                            version=rd("DisplayVersion"),
                            publisher=rd("Publisher"),
                            install_date=rd("InstallDate"),
                            size_kb=size_kb,
                            uninstall_string=rd("UninstallString"),
                        )
                        key_pair = (app.name.lower(), app.version)
                        if key_pair in seen:
                            continue
                        seen.add(key_pair)
                        app.reason = match_bloat(app.name)
                        apps.append(app)
                except OSError:
                    continue
    apps.sort(key=lambda a: a.name.lower())
    return apps


def find_bloat() -> list[InstalledApp]:
    return [app for app in list_installed() if app.reason]


def uninstall_command(app: InstalledApp) -> list[str] | None:
    """Команда тихого удаления через winget; None — winget недоступен."""
    from core import winget_exe
    exe = winget_exe()
    if not exe:
        return None
    return [exe, "uninstall", "--name", app.name, "--exact", "--silent",
            "--accept-source-agreements", "--disable-interactivity"]


# --------------------------------------------------------------------------
# Магазинные (UWP) приложения
# --------------------------------------------------------------------------

def find_appx_bloat() -> list[dict]:
    """Мусорные пакеты Microsoft Store (их нет в списке «Программы»).
    Возвращает [{name, fullname, reason}]."""
    ps = ("Get-AppxPackage | ForEach-Object { \"$($_.Name)|$($_.PackageFullName)\" }")
    try:
        rc, out = run_ps(ps, timeout=120)
    except RuntimeError:
        return []
    result = []
    for line in out.splitlines():
        line = line.strip().strip('"')
        if "|" not in line:
            continue
        name, fullname = line.split("|", 1)
        reason = match_bloat(name)
        if reason:
            result.append({"name": name, "fullname": fullname, "reason": reason})
    return result


def uninstall_appx(fullname: str) -> tuple[bool, str]:
    try:
        rc, out = run_ps(f"Remove-AppxPackage -Package '{fullname}'", timeout=180)
    except RuntimeError as e:
        return False, str(e)
    return rc == 0, (out.strip().splitlines()[-1] if out.strip() else f"код {rc}")


# --------------------------------------------------------------------------
# Задачи планировщика (не Microsoft)
# --------------------------------------------------------------------------

def list_tasks() -> list[dict]:
    """Все задачи планировщика вне \\Microsoft\\ (OEM-аналитика, апдейтеры и т.п.)."""
    ps = ("Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' } | "
          "ForEach-Object { \"$($_.TaskPath)|$($_.TaskName)|$($_.State)\" }")
    try:
        rc, out = run_ps(ps, timeout=120)
    except RuntimeError:
        return []
    tasks = []
    for line in out.splitlines():
        line = line.strip().strip('"')
        parts = line.split("|")
        if len(parts) == 3 and parts[1].strip():
            tasks.append({"path": parts[0], "name": parts[1], "state": parts[2]})
    return tasks


def set_task_enabled(task_path: str, task_name: str, enable: bool) -> tuple[bool, str]:
    """Включение/отключение задачи: schtasks /Change /TN <path+name> /ENABLE|/DISABLE."""
    full = task_path + task_name
    flag = "/ENABLE" if enable else "/DISABLE"
    try:
        rc, out = run_cmd(["schtasks", "/Change", "/TN", full, flag], timeout=60)
    except RuntimeError as e:
        return False, str(e)
    return rc == 0, (out.strip().splitlines()[-1] if out.strip() else f"код {rc}")


# --------------------------------------------------------------------------
# Возможные остатки удалённых программ
# --------------------------------------------------------------------------

# Системные каталоги, которые никогда не являются «хвостами»
_RESIDUAL_WHITELIST = {
    "common files", "microsoft", "windows apps", "windows defender", "windows nt",
    "internet explorer", "windows media player", "modifiable windows apps",
    "reference assemblies", "windows powershell", "shared libraries",
    "frontendassets", "packages", "temp", "tmp", "crashdumps",
    "connected devices platform", "comms", "holders", "microsoft games",
    "squirreltemp", "desktopideplevelcache", "chromium", "patch", "windows",
    "system32", "drivers", "winget", "pip",
    "windows mail", "windows photo viewer", "windows sidebar",
    "windowspowershell", "msbuild", "dotnet", "package management",
    "packagemanagement", "uninstall information", "windows defender advanced threat protection",
    "modifiablewindowsapps", "softwaredistribution", "usoshared", "whesvc",
    "документы", "documents", "главное меню", "start menu", "шаблоны", "templates",
    "обои", "desktop", "мicrosoft", "componentoeminf",
}


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def find_residuals(cancel_event=None) -> list[dict]:
    """Каталоги в Program Files / ProgramData, чьи имена не совпадают ни с одной
    установленной программой. Сознательно НЕ трогаем LOCALAPPDATA/APPDATA —
    там лежат данные и сохранения даже удалённых программ.
    Эвристика: всё, что менялось за 14 дней, считается «живым».
    Возвращает [{path, name}]."""
    import os
    import time
    from pathlib import Path

    roots = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        r = os.environ.get(var)
        if r and Path(r).exists():
            roots.append(Path(r))

    installed_blob = " ".join(
        _norm(f"{a.name} {a.publisher}") for a in list_installed())
    cutoff = time.time() - 14 * 86400
    found: list[dict] = []
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if cancel_event is not None and cancel_event.is_set():
                return found
            if not entry.is_dir():
                continue
            low = entry.name.lower()
            if (low in _RESIDUAL_WHITELIST or low.startswith(("windows", "microsoft"))
                    or low.startswith(".")):
                continue
            normed = _norm(entry.name)
            if len(normed) < 4:
                continue
            if normed in installed_blob:
                continue
            try:
                if entry.stat().st_mtime > cutoff:
                    continue  # недавно менялась — каталог живой
            except OSError:
                continue
            found.append({"path": str(entry), "name": entry.name})
    return found


def open_in_explorer(path: str) -> None:
    from core import CREATE_NO_WINDOW
    import subprocess
    try:
        subprocess.Popen(["explorer", path], creationflags=CREATE_NO_WINDOW)
    except OSError:
        pass
