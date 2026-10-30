# -*- coding: utf-8 -*-
"""Самообновление Обновлятора: проверка релизов, загрузка, подмена exe, откат.

КАК ЭТО РАБОТАЕТ
================
Windows запрещает удалять/перезаписывать запущенный .exe, но разрешает его
ПЕРЕИМЕНОВАТЬ. На этом держится вся подмена (внешний хелпер не нужен):

    1. Фоновая проверка: GET api.github.com/repos/<owner>/<repo>/releases
    2. Из релиза берётся ассет manifest.json и проверяется ed25519-подпись
       (публичный ключ зашит здесь, приватный только у разработчика).
    3. Манифест содержит sha256 нового exe -> скачивание в staging
       (с докачкой после обрыва), потом сверка хеша.
    4. Подмена: бэкап текущего exe -> rename(запущенный -> .old) ->
       os.replace(новый на место старого) -> запуск нового с --post-update.
    5. Новый exe при старте сверяет свой хеш; после успешного поднятия GUI
       main.py/App вызывает finalize_update() (тост, чистка бэкапов).
    6. Если новый exe падает при запуске 2 раза подряд -> автоматический
       откат на бэкап + запись в историю.

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!!                    НАСТРОЙКИ ПОКА НЕ ЗАПОЛНЕНЫ                     !!!
!!!                                                                     !!!
!!! Чтобы самообновление заработало, нужно сделать ОДНОРАЗОВО:          !!!
!!!                                                                     !!!
!!!  1) Создать ПУБЛИЧНЫЙ репозиторий на GitHub (например               !!!
!!!     "ivanov/obnovlyator") — он будет сервером обновлений.           !!!
!!!  2) Вписать его ниже в SELFUPDATE_REPO в формате "owner/repo".      !!!
!!!  3) Выполнить:  python build_release.py --make-keys-only            !!!
!!!     и вписать напечатанный ПУБЛИЧНЫЙ ключ в SELFUPDATE_PUBLIC_KEY.  !!!
!!!     Приватный ключ (release_keys/private.hex) НИКОМУ не показывать  !!!
!!!     и не выкладывать в репозиторий!                                 !!!
!!!  4) Публиковать релизы: python build_release.py --version 1.6 ...   !!!
!!!     (скрипт сам соберёт exe, посчитает sha256, подпишет манифест    !!!
!!!     и напечатает готовую команду gh release create)                 !!!
!!!                                                                     !!!
!!! Пока SELFUPDATE_REPO пуст, всё приложение работает как раньше,      !!!
!!! а проверка обновлений просто пишет в журнал «отключено».            !!!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
"""
from __future__ import annotations

# ============================================================================
#  >>> НАСТРОЙКИ САМООБНОВЛЕНИЯ — ЗАПОЛНИТЬ, КОГДА ПОЯВИТСЯ РЕПОЗИТОРИЙ <<<
# ============================================================================

# TODO(GITHUB): впишите "owner/repo" своего публичного репозитория, например
#   SELFUPDATE_REPO = "ivanov/obnovlyator"
# Пока ПУСТО — самообновление отключено (приложение работает как раньше).
SELFUPDATE_REPO = "bolysevandrej86-arch/obnovlyator"

# Имя exe-ассета на GitHub Releases. Должно совпадать с тем, как файл
# загружается в релиз (build_release.py делает это автоматически).
SELFUPDATE_ASSET = "Obnovlyator.exe"

# TODO(GITHUB): публичный ключ ed25519 из release_keys/public.hex
# (создаётся командой: python build_release.py --make-keys-only).
# Пока ПУСТО — подпись манифеста не проверяется, но sha256 всё равно
# проверяется. Перед первой публикацией ОБЯЗАТЕЛЬНО вставьте ключ.
SELFUPDATE_PUBLIC_KEY = "a51fc0aff1576763649431c85350bfcc72d3781b43038eeeef72b91f6f8eea0e"

# Канал по умолчанию (пользователь может сменить в Настройках):
#   stable = обычные релизы; beta = prereleases; nightly = теги nightly-*
DEFAULT_CHANNEL = "stable"
# ============================================================================

import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("obnovlyator.selfupdate")

APP_DIR_NAME = "Обновлятор"
IS_WINDOWS = (sys.platform == "win32")
MB = 1024 * 1024

GH_API = "https://api.github.com"
GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "Obnovlyator-selfupdate/1.0",   # GitHub API требует User-Agent
}


# ============================================================================
#  Исключения
# ============================================================================

class UpdateError(Exception):
    """Базовая ошибка самообновления."""


class NetworkError(UpdateError):
    """Сеть недоступна / GitHub не отвечает / лимит запросов."""


class VerifyError(UpdateError):
    """Подпись или хеш не сошлись — файл НЕ будет запущен."""


class ApplyError(UpdateError):
    """Не удалось применить обновление (нет прав, файл занят и т.п.)."""


# ============================================================================
#  Пути: staging/бэкапы — в %LOCALAPPDATA%\Обновлятор\selfupdate
# ============================================================================

class SelfUpdatePaths:
    """Все временные файлы обновления — отдельно от config.json приложения."""

    def __init__(self) -> None:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        self.root = Path(base) / APP_DIR_NAME / "selfupdate"
        self.staging = self.root / "staging"
        self.backup = self.root / "backup"
        for d in (self.root, self.staging, self.backup):
            d.mkdir(parents=True, exist_ok=True)
        self.state_file = self.root / "update_state.json"

    def exe(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve()
        return Path(sys.argv[0]).resolve() if sys.argv[0] else Path.cwd()

    def old_marker(self) -> Path:
        return self.exe().with_name(self.exe().name + ".old")

    def staged_exe(self, version: str) -> Path:
        return self.staging / f"Obnovlyator_{version}.exe"

    def backup_path(self, version: str) -> Path:
        ts = time.strftime("%Y%m%d_%H%M%S")
        return self.backup / f"Obnovlyator_{version}_{ts}.exe.bak"


# ============================================================================
#  Утилиты
# ============================================================================

class NoSleep:
    """Запрещает Windows уходить в сон, пока идёт обновление (ES_CONTINUOUS)."""

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __enter__(self) -> "NoSleep":
        if IS_WINDOWS:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
            except Exception:                                   # noqa: BLE001
                log.warning("SetThreadExecutionState недоступен", exc_info=True)
        return self

    def __exit__(self, *_exc) -> None:
        if IS_WINDOWS:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
            except Exception:                                   # noqa: BLE001
                pass


def sha256_file(path: Path, chunk: int = 4 * MB) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _ver_chunks(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")[:4]
    return tuple(int(x) for x in nums) + (0,) * (4 - len(nums))


def version_newer(candidate: str, current: str) -> bool:
    """True, если candidate строго новее current (числовые куски версий)."""
    return _ver_chunks(candidate) > _ver_chunks(current)


def free_space_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(str(path)).free
    except Exception:                                           # noqa: BLE001
        return 0


def dir_is_writable(path: Path) -> bool:
    probe = path / f".selfupdate_probe_{os.getpid()}.tmp"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except Exception:                                           # noqa: BLE001
        return False


def canonical_manifest_bytes(data: dict) -> bytes:
    """Канонический вид манифеста для подписи/проверки (стабильный порядок)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def notify_selfupdate(title: str, msg: str) -> None:
    """Тост; winotify подключается мягко, без падений."""
    try:
        from winotify import Notification                       # type: ignore
        Notification(app_id="Обновлятор", title=title[:64],
                     msg=msg[:250], duration="short").show()
    except Exception:                                           # noqa: BLE001
        log.debug("Тост не отправлен", exc_info=True)


def record_history(**fields) -> None:
    """Зеркалим ключевые события самообновления в общую историю приложения."""
    try:
        import history                                          # noqa: PLC0415
        history.append("selfupdate", **fields)
    except Exception:                                           # noqa: BLE001
        log.debug("history недоступен", exc_info=True)


# ============================================================================
#  Верификация манифеста
# ============================================================================

def verify_manifest(data: dict, repo: str, public_key_hex: str) -> dict:
    """Структурная и криптографическая проверка манифеста.

    1. Обязательные поля; 2. sha256 — hex 64; 3. URL только из нашего
    репозитория (allowlist); 4. ed25519-подпись (если задан ключ).
    Любое несоответствие -> VerifyError: файл скачан не будет.
    """
    for field in ("version", "channel", "sha256", "size", "url"):
        if field not in data or data[field] in ("", None):
            raise VerifyError(f"В манифесте отсутствует поле '{field}'")
    sha = str(data["sha256"]).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise VerifyError("Некорректный sha256 в манифесте")
    url = str(data["url"])
    if not url.startswith("https://"):
        raise VerifyError("URL обновления не https")
    allowed = (
        f"https://github.com/{repo}/releases/download/",
        f"https://api.github.com/repos/{repo}/releases/assets/",
        "https://objects.githubusercontent.com/",   # фактический хост ассетов
    )
    if not url.startswith(allowed):
        raise VerifyError(f"URL обновления вне доверенного источника: {url}")

    if public_key_hex:
        sig = data.pop("signature", None)
        if not sig:
            raise VerifyError("Манифест не подписан")
        try:
            from nacl.exceptions import BadSignatureError       # type: ignore
            from nacl.signing import VerifyKey                  # type: ignore
            VerifyKey(bytes.fromhex(public_key_hex)).verify(
                canonical_manifest_bytes(data), bytes.fromhex(str(sig)))
        except ImportError as e:
            raise VerifyError(f"Манифест подписан, но PyNaCl не работает "
                              f"в этой сборке: {e!r}") from e
        except (BadSignatureError, ValueError) as e:
            raise VerifyError("Подпись манифеста НЕВЕРНА — возможна подмена") from e
    data["sha256"] = sha
    return data


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    channel: str
    sha256: str
    size: int
    url: str
    notes: str = ""
    critical: bool = False
    published_at: str = ""
    release_name: str = ""


# ============================================================================
#  Клиент GitHub Releases
# ============================================================================

def _get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=GH_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) < 5:
                log.warning("GitHub API: почти исчерпан лимит запросов")
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0":
            raise NetworkError("Лимит GitHub API исчерпан, повторите позже") from e
        raise NetworkError(f"GitHub вернул HTTP {e.code}") from e
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            ConnectionError) as e:
        raise NetworkError(f"Сеть недоступна: {e}") from e


class _Paused(Exception):
    """Внутренний сигнал паузы загрузки."""


# ============================================================================
#  Основной класс
# ============================================================================

class SelfUpdater:
    """Полный цикл самообновления для PyInstaller onefile.

    Использование:
        up = create_updater(cfg)
        up.handle_startup_state()        # первым делом при старте (main.py)
        info = up.check_for_updates()    # None = обновлений нет
        staged = up.download(info, progress, pause_check)
        up.apply(info, staged)           # подмена + перезапуск, не возвращается
        up.finalize_update()             # после успешного поднятия GUI
    """

    MAX_DOWNLOAD_RETRIES = 6
    BACKUPS_TO_KEEP = 3

    def __init__(self, current_version: str, channel: str = DEFAULT_CHANNEL,
                 skip_versions: Optional[set] = None,
                 repo: str = "", public_key: str = "",
                 mark_skip=None) -> None:
        self.current_version = current_version
        self.channel = channel if channel in ("stable", "beta", "nightly") else "stable"
        self.skip_versions = set(skip_versions or ())
        self.mark_skip = mark_skip     # вызывается при откате на битую версию
        self.repo = repo.strip().strip("/")
        self.public_key = (public_key or "").strip()
        self.paths = SelfUpdatePaths()

    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        """False, пока SELFUPDATE_REPO не заполнен (см. комментарий вверху)."""
        return bool(self.repo)

    # ------------------------------------------------------------------
    #  ПРОВЕРКА
    # ------------------------------------------------------------------
    def check_for_updates(self, ignore_skip: bool = False) -> Optional[UpdateInfo]:
        if not self.configured:
            log.info("Самообновление отключено: SELFUPDATE_REPO не заполнен "
                     "(см. selfupdate.py) — это нормально до публикации релизов")
            return None
        releases = _get_json(f"{GH_API}/repos/{self.repo}/releases?per_page=20")
        if not isinstance(releases, list):
            raise NetworkError("Неожиданный ответ GitHub API")
        net_error = ""                  # последний сбой загрузки манифеста
        for rel in releases:
            if rel.get("draft") or not self._channel_match(rel):
                continue
            assets = rel.get("assets") or []
            manifest_url = next((a.get("browser_download_url") for a in assets
                                 if a.get("name") == "manifest.json"), None)
            if not manifest_url:
                log.info("Релиз %s без manifest.json — пропускаю", rel.get("tag_name"))
                continue
            try:
                data = _get_json(manifest_url)
                data = verify_manifest(data, self.repo, self.public_key)
            except VerifyError:
                raise                       # подпись/структура — не глотаем
            except NetworkError as e:
                # этот релиз сейчас не читается — пробуем старее, но помним
                # сбой: если обновлений не найдено, честно сообщим о сети,
                # а не «обновлений нет»
                net_error = str(e)
                continue
            if data.get("channel") != self.channel:
                continue
            if not version_newer(str(data["version"]), self.current_version):
                return None                 # первый релиз канала — уже проверен
            if (str(data["version"]) in self.skip_versions
                    and not ignore_skip):
                log.info("Версия %s помечена «пропустить»", data["version"])
                return None
            exe_url = next((a.get("browser_download_url") for a in assets
                            if a.get("name") == SELFUPDATE_ASSET), None)
            url = str(data["url"]) if data.get("url") else exe_url
            if not url:
                continue
            size = int(data.get("size") or 0)
            if size <= 0:
                size = next((int(a.get("size") or 0) for a in assets
                             if a.get("name") == SELFUPDATE_ASSET), 0)
            record_history(status="found", to_version=str(data["version"]),
                           channel=self.channel)
            return UpdateInfo(
                version=str(data["version"]),
                channel=self.channel,
                sha256=str(data["sha256"]),
                size=size,
                url=url,
                notes=str(data.get("notes") or rel.get("body") or ""),
                critical=bool(data.get("critical")),
                published_at=str(data.get("published_at")
                                 or rel.get("published_at") or ""),
                release_name=str(rel.get("name") or rel.get("tag_name") or ""),
            )
        if net_error:
            raise NetworkError(f"Не удалось скачать манифест релиза: {net_error}")
        return None

    def _channel_match(self, rel: dict) -> bool:
        """stable = обычные релизы; beta = prereleases; nightly = тег nightly-*."""
        tag = (rel.get("tag_name") or "").lower()
        if self.channel == "nightly":
            return tag.startswith("nightly") or "nightly" in tag
        if self.channel == "beta":
            return bool(rel.get("prerelease")) or tag.startswith("beta")
        return not rel.get("prerelease") and not tag.startswith(("beta", "nightly"))

    # ------------------------------------------------------------------
    #  ЗАГРУЗКА (докачка, повторы, запрет сна, пауза)
    # ------------------------------------------------------------------
    def download(self, info: UpdateInfo,
                 progress: Optional[Callable[[dict], None]] = None,
                 pause_check: Optional[Callable[[], bool]] = None) -> Path:
        need = info.size * 2 + 64 * MB
        free = free_space_bytes(self.paths.staging)
        if free and free < need:
            raise ApplyError(f"Недостаточно места на диске: нужно ~{need / MB:.0f} МБ, "
                             f"свободно {free / MB:.0f} МБ")
        part = self.paths.staged_exe(info.version).with_suffix(".exe.part")
        final = self.paths.staged_exe(info.version)
        if final.exists():
            final.unlink()
        record_history(status="downloading", to_version=info.version)
        try:
            with NoSleep():
                self._download_resumable(info, part, progress, pause_check)
        except Exception as e:
            record_history(status="download_error", detail=str(e)[:200])
            raise
        real = sha256_file(part)
        if real != info.sha256.lower():
            part.unlink(missing_ok=True)
            record_history(status="download_error", detail="sha256 mismatch")
            raise VerifyError("SHA-256 скачанного файла не совпал — файл удалён")
        part.rename(final)
        record_history(status="downloaded", to_version=info.version)
        return final

    def _download_resumable(self, info: UpdateInfo, part: Path,
                            progress: Optional[Callable[[dict], None]],
                            pause_check: Optional[Callable[[], bool]]) -> None:
        backoff = 2.0
        last_paused_toast = 0.0
        for attempt in range(1, self.MAX_DOWNLOAD_RETRIES + 1):
            offset = part.stat().st_size if part.exists() else 0
            if offset > info.size:
                part.unlink()
                offset = 0
            headers = dict(GH_HEADERS)
            if offset:
                headers["Range"] = f"bytes={offset}-"
            req = urllib.request.Request(info.url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    # Сервер без поддержки Range отдаёт 200 на запрос докачки —
                    # тогда пишем заново, иначе получится битый файл.
                    if offset and getattr(resp, "status", 200) != 206:
                        log.info("Сервер не поддерживает докачку — начинаю заново")
                        offset = 0
                    total = info.size or (offset + int(
                        resp.headers.get("Content-Length") or 0))
                    done = offset
                    last_t, last_b = time.monotonic(), done
                    speed = 0.0
                    mode = "ab" if offset else "wb"
                    with part.open(mode) as f:
                        while True:
                            if pause_check and pause_check():
                                now = time.monotonic()
                                if now - last_paused_toast > 30:
                                    notify_selfupdate(
                                        "Обновлятор",
                                        "Загрузка обновления приостановлена: "
                                        "ожидание восстановления сети")
                                    last_paused_toast = now
                                f.flush()
                                time.sleep(3.0)
                                raise _Paused()
                            chunk = resp.read(256 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            now = time.monotonic()
                            if now - last_t >= 0.5:
                                speed = (done - last_b) / (now - last_t)
                                last_t, last_b = now, done
                            if progress:
                                progress({"done": done, "total": total,
                                          "percent": min(100.0, done * 100.0 / max(total, 1)),
                                          "speed_bps": speed})
                if part.stat().st_size == info.size:
                    return
                log.warning("Загрузка оборвалась: %s / %s байт",
                            part.stat().st_size, info.size)
            except _Paused:
                backoff = 2.0
                continue
            except (urllib.error.URLError, urllib.error.HTTPError,
                    socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                if isinstance(e, urllib.error.HTTPError):
                    if e.code == 416:
                        part.unlink(missing_ok=True)
                        log.warning("Сброшена повреждённая докачка (.part)")
                        continue
                    if e.code not in (429, 500, 502, 503, 504):
                        raise NetworkError(f"HTTP {e.code} при загрузке") from e
                log.warning("Попытка %d/%d не удалась: %s",
                            attempt, self.MAX_DOWNLOAD_RETRIES, e)
            if attempt < self.MAX_DOWNLOAD_RETRIES:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
        raise NetworkError("Не удалось докачать файл за отведённое число попыток")

    # ------------------------------------------------------------------
    #  ПОДМЕНА ФАЙЛА + ПЕРЕЗАПУСК
    # ------------------------------------------------------------------
    def apply(self, info: UpdateInfo, staged: Path) -> None:
        """Подменяет запущенный exe и запускает новый. При успехе НЕ
        ВОЗВРАЩАЕТСЯ (завершает процесс — заранее закройте окно/трей)."""
        if not getattr(sys, "frozen", False):
            raise ApplyError("Самообновление работает только в собранном .exe "
                             "(python main.py обновить сам себя не может)")
        target = self.paths.exe()
        if not dir_is_writable(target.parent):
            raise ApplyError(
                f"Нет прав на запись в {target.parent}. Не держите приложение "
                "в Program Files — рекомендуем папку пользователя, например "
                "%LOCALAPPDATA%\\Programs\\Обновлятор")
        if not staged.exists():
            raise ApplyError("Скачанный файл не найден")

        backup = self.paths.backup_path(self.current_version)
        shutil.copy2(target, backup)

        state = {
            "phase": "verifying",
            "from": self.current_version,
            "to": info.version,
            "expected_sha256": info.sha256.lower(),
            "backup": str(backup),
            "fail_count": 0,
            "ts": time.time(),
        }
        _write_json_atomic(self.paths.state_file, state)
        record_history(status="installing", from_version=self.current_version,
                       to_version=info.version)

        old_marker = self.paths.old_marker()
        if old_marker.exists():
            try:
                old_marker.unlink()
            except OSError:
                pass
        os.rename(target, old_marker)                # разрешено даже запущенному
        try:
            tmp_new = target.with_name(target.name + ".new")
            shutil.copy2(staged, tmp_new)            # копия на тот же том
            os.replace(tmp_new, target)              # атомарная замена
        except Exception as e:
            try:
                os.rename(old_marker, target)
            except OSError:
                pass
            record_history(status="install_error", detail=str(e)[:200])
            raise ApplyError(f"Не удалось заменить exe: {e}") from e

        try:
            subprocess.Popen(
                [str(target), "--post-update"],
                cwd=str(target.parent), close_fds=True,
                creationflags=0x00000008 if IS_WINDOWS else 0,  # DETACHED_PROCESS
            )
        except Exception as e:
            try:
                os.rename(target, self.paths.staged_exe(info.version))
                os.rename(old_marker, target)
            except OSError:
                pass
            record_history(status="install_error", detail=str(e)[:200])
            raise ApplyError(f"Не удалось запустить обновлённый exe: {e}") from e

        record_history(status="restarting", to_version=info.version)
        time.sleep(0.3)
        os._exit(0)

    # ------------------------------------------------------------------
    #  ЗАПУСК ПОСЛЕ ОБНОВЛЕНИЯ / ЗАЩИТА ОТ ЦИКЛА ПАДЕНИЙ
    # ------------------------------------------------------------------
    def handle_startup_state(self) -> Optional[dict]:
        """Вызывать ПЕРВЫМ ДЕЛОМ при старте (до создания GUI!).

        - чистит мусор прошлых обновлений (.old, .part);
        - если это первый запуск после подмены — сверяет свой хеш;
        - если приложение падало после обновления 2 раза — автооткат.
        """
        self._cleanup_leftovers()
        state = _read_json(self.paths.state_file)
        if not state or state.get("phase") != "verifying":
            return state

        real = sha256_file(self.paths.exe())
        if state.get("expected_sha256") and real != state["expected_sha256"]:
            self._restore_backup(state, "хеш запущенного exe не совпал")
            return None
        fail = int(state.get("fail_count") or 0)
        if fail >= 2:
            self._restore_backup(state, "приложение падало при запуске после обновления")
            return None
        state["fail_count"] = fail + 1
        _write_json_atomic(self.paths.state_file, state)
        return state

    def finalize_update(self) -> None:
        """Вызвать ПОСЛЕ успешного поднятия GUI — фиксирует успех обновления."""
        state = _read_json(self.paths.state_file) or {}
        to_ver = state.get("to") or self.current_version
        from_ver = state.get("from") or "?"
        try:
            self.paths.state_file.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self.paths.old_marker().unlink(missing_ok=True)
        except OSError:
            pass
        self._prune_backups()
        record_history(status="ok", from_version=from_ver, to_version=to_ver)
        notify_selfupdate("Обновлятор", f"Обновление до {to_ver} завершено успешно")

    # ------------------------------------------------------------------
    def _restore_backup(self, state: dict, reason: str) -> None:
        backup = Path(str(state.get("backup") or ""))
        to_ver, from_ver = str(state.get("to") or "?"), str(state.get("from") or "?")
        if backup.exists():
            target = self.paths.exe()
            marker = self.paths.old_marker()
            try:
                if target.exists():
                    os.rename(target, marker)
                shutil.copy2(backup, target)
                subprocess.Popen([str(target)], cwd=str(target.parent),
                                 close_fds=True,
                                 creationflags=0x00000008 if IS_WINDOWS else 0)
                self.paths.state_file.unlink(missing_ok=True)
                record_history(status="rolled_back", from_version=to_ver,
                               to_version=from_ver, detail=reason)
                if self.mark_skip:
                    try:
                        self.mark_skip(to_ver)
                    except Exception:
                        log.debug("mark_skip не сработал", exc_info=True)
                notify_selfupdate("Обновлятор", f"Обновление до {to_ver} не удалось, "
                                                f"выполнен откат на {from_ver}")
                time.sleep(0.3)
                os._exit(1)
            except OSError as e:
                record_history(status="rollback_error", detail=str(e)[:200])
        else:
            record_history(status="rollback_missing_backup", detail=reason)
        self.paths.state_file.unlink(missing_ok=True)

    def _prune_backups(self) -> None:
        baks = sorted(self.paths.backup.glob("*.exe.bak"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in baks[self.BACKUPS_TO_KEEP:]:
            try:
                old.unlink()
            except OSError:
                pass

    def _cleanup_leftovers(self) -> None:
        for p in self.paths.staging.glob("*.part"):
            try:
                p.unlink()
            except OSError:
                pass
        # отработанные staging-экзешники старше двух недель — тоже мусор
        cutoff = time.time() - 14 * 86400
        for p in self.paths.staging.glob("*.exe"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
        marker = self.paths.old_marker()
        if marker.exists():
            try:
                marker.unlink()
            except OSError:
                log.info("Старый exe ещё занят — удалю при следующем запуске")


# ============================================================================
#  Атомарный JSON + фабрика
# ============================================================================

def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def create_updater(cfg, current_version: str) -> Optional[SelfUpdater]:
    """Собирает SelfUpdater из Config приложения.

    cfg — core.Config (нужны поля update_channel и skip-версии из
    ignored_updates самого приложения не подходят — используются свои
    selfupdate_skipped). Возвращает None, если самообновление выключено.
    """
    enabled = bool(getattr(cfg, "selfupdate_enabled", True))
    if not enabled:
        return None
    channel = getattr(cfg, "update_channel", DEFAULT_CHANNEL)
    skipped = set(getattr(cfg, "selfupdate_skipped", []) or [])
    def _mark_skip(version: str) -> None:
        """При автооткате помечаем битую версию — чтобы авто-проверки
        не зацикливались на ней. Ручная проверка игнорирует пометку."""
        try:
            cfg.selfupdate_skipped = sorted(
                set(getattr(cfg, "selfupdate_skipped", []) or []) | {version})
            cfg.save()
        except Exception:
            pass

    return SelfUpdater(
        current_version=current_version,
        channel=channel,
        skip_versions=skipped,
        repo=SELFUPDATE_REPO,
        public_key=SELFUPDATE_PUBLIC_KEY,
        mark_skip=_mark_skip,
    )
