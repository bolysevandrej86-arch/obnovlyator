# -*- coding: utf-8 -*-
"""Очистка: установочные файлы после обновлений, системные кэши, браузеры,
компоненты Windows; отчёты по корзине и старым файлам в «Загрузках».

Все операции безопасные: занятые файлы пропускаются, ошибки собираются,
ничего вне указанных папок не трогается. Отчёты только показывают размеры.
"""
from __future__ import annotations

import os
import shutil
import string
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from core import CREATE_NO_WINDOW, run_cmd


@dataclass
class CleanupTarget:
    id: str
    name: str
    desc: str
    kind: str = "folder"        # folder | pip | dism
    path: str = ""
    exclude: tuple = ()         # подпапки верхнего уровня, которые не трогаем
    only_dirs: tuple = ()       # чистить только подкаталоги с этими именами (кэши браузеров)
    process_guard: str = ""     # если этот процесс запущен — очистка отменяется
    min_age_sec: int = 0        # удалять только файлы/папки старше этого возраста
    admin_only: bool = False
    show_exists: bool = False   # цель показывается, только если папка существует


def _temp_dir() -> Path:
    return Path(os.environ.get("TEMP") or os.environ.get("TMP")
                or Path.home() / "AppData" / "Local" / "Temp")


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


_BROWSER_CACHE_DIRS = ("cache", "code cache", "gpucache", "shadercache",
                       "graphitedawn resources cache", "service worker")


def get_targets() -> list[CleanupTarget]:
    temp = _temp_dir()
    local = _local()
    windir = os.environ.get("WINDIR", r"C:\Windows")
    targets = [
        CleanupTarget(
            id="winget", name="Установщики winget",
            desc="Инсталляторы, скачанные при обновлении программ "
                 f"({temp / 'WinGet'})",
            path=str(temp / "WinGet"), min_age_sec=3600,
        ),
        CleanupTarget(
            id="temp", name="Временные файлы",
            desc=f"Папка {temp} (кроме WinGet), только файлы старше суток",
            path=str(temp), exclude=("winget",), min_age_sec=24 * 3600,
        ),
        CleanupTarget(
            id="pip", name="Кэш pip", kind="pip",
            desc="Скачанные wheel-пакеты Python",
        ),
        CleanupTarget(
            id="wu", name="Кэш Центра обновления Windows",
            desc="Уже установленные обновления из SoftwareDistribution\\Download "
                 "(требуются права администратора)",
            path=os.path.join(windir, "SoftwareDistribution", "Download"),
            admin_only=True,
        ),
        CleanupTarget(
            id="dism", name="Компоненты Windows (WinSxS)",
            desc="Очистка устаревших компонентов через DISM. Занимает до получаса, "
                 "освобождённое место проявляется постепенно. Размер заранее не оценить.",
            kind="dism", admin_only=True,
        ),
    ]
    # Кэши браузеров — добавляем, только если браузер установлен
    browsers = [
        ("chrome", "Кэш Google Chrome", local / "Google" / "Chrome" / "User Data",
         "chrome.exe"),
        ("edge", "Кэш Microsoft Edge", local / "Microsoft" / "Edge" / "User Data",
         "msedge.exe"),
        ("firefox", "Кэш Mozilla Firefox", local / "Mozilla" / "Firefox" / "Profiles",
         "firefox.exe"),
    ]
    for bid, bname, bpath, guard in browsers:
        if bpath.exists():
            targets.append(CleanupTarget(
                id=bid, name=bname,
                desc=f"Кэш страниц и картинок ({bpath}). Браузер сам всё пересоздаст.",
                path=str(bpath), only_dirs=_BROWSER_CACHE_DIRS,
                process_guard=guard,
            ))
    return targets


def fmt_size(n: float) -> str:
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def _dir_size(path: str, cancel_event=None, exclude: tuple = ()) -> int:
    total = 0
    base = Path(path)
    if not base.exists():
        return 0
    excl = {e.lower() for e in exclude}
    for root, dirs, files in os.walk(base, topdown=True, onerror=lambda e: None):
        if cancel_event is not None and cancel_event.is_set():
            return total
        if root == str(base):
            dirs[:] = [d for d in dirs if d.lower() not in excl]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _iter_cache_dirs(base: Path, names: frozenset, cancel_event=None):
    """Генерирует пути каталогов, чьё имя совпадает с names (в любом месте базы)."""
    for root, dirs, _files in os.walk(base, topdown=True, onerror=lambda e: None):
        if cancel_event is not None and cancel_event.is_set():
            return
        matched = [d for d in dirs if d.lower() in names]
        for m in matched:
            yield Path(root) / m
            dirs.remove(m)  # внутрь не спускаться — уже отдали целиком


def _pip_cache_dir() -> str:
    try:
        rc, out = run_cmd([sys.executable, "-m", "pip", "cache", "dir"], timeout=30)
        if rc == 0:
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line and Path(line).exists():
                    return line
    except RuntimeError:
        pass
    return ""


def dir_size(path: str, cancel_event=None) -> int:
    """Публичная обёртка для измерения произвольной папки."""
    return _dir_size(path, cancel_event)


def measure(target: CleanupTarget, cancel_event=None) -> int | None:
    """Размер цели в байтах; None — размер оценить нельзя (DISM)."""
    if target.kind == "dism":
        return None
    if target.kind == "pip":
        return _dir_size(_pip_cache_dir(), cancel_event)
    base = Path(target.path)
    if not base.exists():
        return 0
    if target.only_dirs:
        names = frozenset(n.lower() for n in target.only_dirs)
        return sum(_dir_size(str(p), cancel_event)
                   for p in _iter_cache_dirs(base, names, cancel_event))
    return _dir_size(target.path, cancel_event, exclude=target.exclude)


def _process_running(substring: str) -> bool:
    try:
        _, out = run_cmd(["tasklist", "/FO", "CSV", "/NH"], timeout=25)
        low = out.lower()
        return substring.lower() in low
    except RuntimeError:
        return False


def clean(target: CleanupTarget, cancel_event=None,
          is_admin: bool = False) -> tuple[int | None, list[str]]:
    """Очищает цель. Возвращает (освобождено байт | None, ошибки).
    None — «операция выполнена, размер неизвестен» (DISM).
    Занятые файлы пропускаются — не ошибка; остаток покажет измерение."""
    errors: list[str] = []

    if target.kind == "pip":
        try:
            rc, out = run_cmd([sys.executable, "-m", "pip", "cache", "purge"], timeout=120)
            if rc != 0:
                errors.append(f"pip cache purge: код {rc}")
        except RuntimeError as e:
            errors.append(str(e))
        return 0, errors

    if target.kind == "dism":
        if not is_admin:
            return None, ["нужны права администратора (кнопка перезапуска — в сайдбаре)"]
        try:
            rc, out = run_cmd(
                ["Dism.exe", "/Online", "/Cleanup-Image", "/StartComponentCleanup"],
                timeout=3600, cancel_event=cancel_event)
        except RuntimeError as e:
            return None, [str(e)]
        if rc == 0:
            return None, []
        tail = out.strip().splitlines()[-1] if out.strip() else ""
        return None, [f"DISM завершился с кодом {rc}" + (f": {tail}" if tail else "")]

    if target.admin_only and not is_admin:
        return 0, ["нужны права администратора (кнопка перезапуска — в сайдбаре)"]

    if target.process_guard and _process_running(target.process_guard):
        return 0, [f"процесс {target.process_guard} запущен — закройте программу и повторите"]

    base = Path(target.path)
    if not base.exists():
        return 0, []
    excl = {e.lower() for e in target.exclude}
    cutoff = time.time() - target.min_age_sec if target.min_age_sec else 0
    freed = 0

    def _remove_file(path: Path, respect_age: bool) -> int:
        try:
            if respect_age and cutoff and path.stat().st_mtime > cutoff:
                return 0  # слишком свежий — не трогаем
            size = path.stat().st_size
            path.unlink()
            return size
        except OSError:
            return 0

    if target.only_dirs:
        # удаляем содержимое каталогов-кэшей, сами каталоги оставляем
        names = frozenset(n.lower() for n in target.only_dirs)
        for cache_dir in list(_iter_cache_dirs(base, names, cancel_event)):
            if cancel_event is not None and cancel_event.is_set():
                break
            before = _dir_size(str(cache_dir))
            for child in list(cache_dir.iterdir()):
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                except OSError:
                    pass
            after = _dir_size(str(cache_dir))
            freed += max(0, before - after)
        return freed, errors

    for entry in list(base.iterdir()):
        if cancel_event is not None and cancel_event.is_set():
            break
        if entry.name.lower() in excl:
            continue
        try:
            if entry.is_file():
                freed += _remove_file(entry, respect_age=True)
            elif entry.is_dir():
                before = _dir_size(str(entry))
                shutil.rmtree(entry, ignore_errors=True)
                if entry.exists():  # что-то внутри залочено
                    freed += before - _dir_size(str(entry))
                else:
                    freed += before
        except OSError as e:
            errors.append(f"{entry.name}: {e.strerror or e}")
    return freed, errors


# --------------------------------------------------------------------------
# Отчёты: корзина и старые файлы в «Загрузках»
# --------------------------------------------------------------------------

def recycle_size(cancel_event=None) -> int:
    """Размер корзины на всех дисках (что доступно без администратора)."""
    total = 0
    for letter in string.ascii_uppercase:
        drive_root = Path(f"{letter}:\\$Recycle.Bin")
        if drive_root.exists():
            total += _dir_size(str(drive_root), cancel_event)
            if cancel_event is not None and cancel_event.is_set():
                break
    return total


def empty_recycle_bin() -> tuple[bool, str]:
    try:
        rc, out = run_cmd(
            ["powershell", "-NoProfile", "-Command",
             "try { Clear-RecycleBin -Force -ErrorAction Stop; 'OK' } "
             "catch { 'ERR: ' + $_.Exception.Message }"],
            timeout=120)
    except RuntimeError as e:
        return False, str(e)
    if "OK" in out:
        return True, "корзина очищена"
    tail = out.strip().splitlines()[-1] if out.strip() else f"код {rc}"
    return False, tail


def downloads_report(days: int = 30) -> tuple[int, int, str]:
    """(количество старых файлов, их суммарный размер, папка).
    Учитываются только файлы верхнего уровня «Загрузок» старше days дней."""
    folder = Path.home() / "Downloads"
    if not folder.exists():
        folder = Path.home() / "Загрузки"
    if not folder.exists():
        return 0, 0, ""
    cutoff = time.time() - days * 86400
    count = 0
    total = 0
    try:
        for entry in folder.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    count += 1
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return count, total, str(folder)


def open_in_explorer(path: str) -> None:
    try:
        subprocess.Popen(["explorer", path], creationflags=CREATE_NO_WINDOW)
    except OSError:
        pass
