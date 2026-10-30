# -*- coding: utf-8 -*-
"""Обновлятор — ядро приложения.

Здесь живёт вся «неграфическая» логика: конфигурация, запуск внешних
процессов (winget/pip), парсинг списка обновлений, определение VPN и
движок обновлений, работающий в фоновом потоке и шлющий события в UI.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import history

APP_NAME = "Обновлятор"
APP_VERSION = "1.7.4"


def _resolve_base_dir() -> Path:
    """Папка для config.json/logs/history.

    В обычном запуске — рядом со скриптом. В собранном .exe (PyInstaller
    onefile __file__ указывает на временную папку распаковки!) — рядом
    с exe, если туда можно писать, иначе %LOCALAPPDATA%\\Обновлятор.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        try:
            probe = exe_dir / ".obnovlyator-write-test"
            probe.touch()
            probe.unlink()
            return exe_dir
        except OSError:
            pass
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
        try:
            local.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return local
    return Path(__file__).resolve().parent


BASE_DIR = _resolve_base_dir()
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

log = logging.getLogger("obnovlyator")


def setup_logging() -> None:
    """Логи в файл logs/obnovlyator.log с ротацией по суткам (14 дней)."""
    if any(isinstance(h, TimedRotatingFileHandler) for h in log.handlers):
        return
    try:
        LOG_DIR.mkdir(exist_ok=True)
        handler = TimedRotatingFileHandler(
            LOG_DIR / "obnovlyator.log", when="midnight",
            backupCount=14, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    except OSError:
        logging.basicConfig(level=logging.INFO)


# --------------------------------------------------------------------------
# Запуск внешних процессов
# --------------------------------------------------------------------------

def decode_bytes(raw: bytes) -> str:
    """Декодирует вывод внешней программы (winget/pip/tasklist).

    Порядок подобран так, чтобы не получить «кракозябры» ни с UTF-8,
    ни с русскими OEM/ANSI-кодировками.
    """
    for enc in ("utf-8", "cp1251", "cp866"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def kill_tree(proc: subprocess.Popen) -> None:
    """Убивает процесс вместе с дочерними (установщики порождают потомков)."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15,
        )
    except Exception:
        log.debug("taskkill не сработал, пробую proc.kill()", exc_info=True)
    try:
        proc.kill()
    except Exception:
        pass


def cancel_all(procs: list[subprocess.Popen]) -> None:
    for p in list(procs):
        try:
            kill_tree(p)
        except Exception:
            pass


def run_cmd(
    args,
    *,
    timeout: float = 120,
    shell: bool = False,
    cancel_event: threading.Event | None = None,
    on_line=None,
    procs: list[subprocess.Popen] | None = None,
) -> tuple[int | None, str]:
    """Запускает процесс и читает вывод построчно (учитывая \\r и \\n).

    Возвращает (код возврата, весь вывод). Код None означает, что процесс
    был убит по таймауту или отмене — вызывающий различает их по cancel_event.
    Ни при каких условиях не бросает исключений, кроме неудачного старта.
    """
    log.debug("CMD: %r", args)
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=shell,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, OSError) as e:
        raise RuntimeError(f"Не удалось запустить «{args if isinstance(args, str) else args[0]}»: {e}") from e

    if procs is not None:
        procs.append(proc)

    lines: list[str] = []
    chunks: queue.Queue = queue.Queue()
    deadline = time.monotonic() + timeout
    cancelled = timed_out = False
    _EOF = object()  # маркер завершения потока чтения (не путать с паузой в выводе)

    def _reader() -> None:
        try:
            while True:
                piece = proc.stdout.read1(8192)  # type: ignore[union-attr]
                if not piece:
                    break
                chunks.put(piece)
        except Exception:
            pass
        finally:
            chunks.put(_EOF)

    threading.Thread(target=_reader, daemon=True, name="cmd-reader").start()

    def _feed(raw: bytes) -> None:
        line = decode_bytes(raw).strip()
        if not line:
            return
        lines.append(line)
        if on_line:
            try:
                on_line(line)
            except Exception:
                log.exception("on_line упал")

    pending = b""
    try:
        while True:
            try:
                piece = chunks.get(timeout=0.2)
            except queue.Empty:
                piece = None  # данных сейчас нет — проверяем таймаут/отмену и ждём дальше
            if piece is _EOF:  # поток чтения закончился — вывод исчерпан
                break
            if piece is not None:
                pending += piece
                parts = re.split(rb"[\r\n]+", pending)
                pending = parts.pop()
                for raw_line in parts:
                    _feed(raw_line)
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if time.monotonic() > deadline:
                timed_out = True
                break

        if pending.strip():
            _feed(pending)

        if cancelled or timed_out:
            kill_tree(proc)
            end = time.monotonic() + 5
            while proc.poll() is None and time.monotonic() < end:
                time.sleep(0.1)
            return None, "\n".join(lines)

        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            rc = proc.poll()
        return rc, "\n".join(lines)
    finally:
        if procs is not None:
            try:
                procs.remove(proc)
            except ValueError:
                pass


# --------------------------------------------------------------------------
# Пакеты и парсинг
# --------------------------------------------------------------------------

@dataclass
class PackageUpdate:
    source: str        # 'winget' | 'pip'
    pkg_id: str        # winget Id или имя pip-пакета
    name: str
    version: str
    available: str

    @property
    def key(self) -> str:
        return f"{self.source}:{self.pkg_id}"


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def parse_winget_table(text: str) -> list[PackageUpdate]:
    """Разбирает таблицу `winget upgrade`.

    Колонки вычисляются по позициям слов в строке заголовка — это не зависит
    от языка системы (у русской Windows заголовок «Имя ИД Версия …»).
    """
    lines = text.splitlines()
    dash_i = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if len(stripped) >= 10 and set(stripped) <= {"-"}:
            dash_i = i
            break
    if dash_i < 1:
        return []

    header = lines[dash_i - 1]
    starts = [m.start() for m in re.finditer(r"\S+", header)]
    if not 3 <= len(starts) <= 6:
        return []

    packages: list[PackageUpdate] = []
    for line in lines[dash_i + 1:]:
        if not line.strip():
            break
        cols = []
        for j, start in enumerate(starts):
            end = starts[j + 1] if j + 1 < len(starts) else len(line)
            cols.append(line[start:end].strip() if start < len(line) else "")
        while len(cols) < 5:
            cols.append("")
        name, pkg_id, version, available, source = cols[:5]
        # Идентификатор — «якорь» строки: без валидного Id считаем таблицу
        # законченной (ниже идут итоговые строки вроде «Доступны обновления: N»)
        if not pkg_id or pkg_id == "..." or not _ID_RE.match(pkg_id):
            break
        packages.append(PackageUpdate(
            source=source or "winget",
            pkg_id=pkg_id,
            name=name or pkg_id,
            version=version or "?",
            available=available or "?",
        ))
    return packages


def parse_pip_outdated(text: str) -> list[PackageUpdate]:
    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError:
        log.warning("pip вернул не-JSON вывод, пропускаю")
        return []
    packages = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("name"):
            packages.append(PackageUpdate(
                source="pip",
                pkg_id=str(item["name"]).lower(),
                name=str(item["name"]),
                version=str(item.get("version") or "?"),
                available=str(item.get("latest_version") or "?"),
            ))
    return packages


# --------------------------------------------------------------------------
# Запрет сна и проверка свободного места
# --------------------------------------------------------------------------

class NoSleep:
    """Запрещает Windows уходить в сон, пока выполняется блок with.

    Используется на время больших операций (обновление пакетов, очистки):
    ES_CONTINUOUS | ES_SYSTEM_REQUIRED — счётчик простоя системы не растёт.
    """

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __enter__(self) -> "NoSleep":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(
                self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
        except Exception:
            log.debug("SetThreadExecutionState недоступен", exc_info=True)
        return self

    def __exit__(self, *_exc) -> None:
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
        except Exception:
            pass


def disk_free_gb(path: str = "C:\\") -> float:
    """Свободное место на диске в ГБ (0 — не удалось определить)."""
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except Exception:
        return 0.0


def enough_space(cfg: "Config", path: str = "C:\\") -> bool:
    """True, если места больше cfg.min_free_gb (0 ГБ = проверка отключена)."""
    limit = float(getattr(cfg, "min_free_gb", 0.0) or 0.0)
    if limit <= 0:
        return True
    free = disk_free_gb(path)
    return not free or free >= limit


# --------------------------------------------------------------------------
# Экспорт/импорт настроек (перенос на другой ПК)
# --------------------------------------------------------------------------

def export_settings(cfg: "Config", dest) -> None:
    """Сохраняет ВСЕ настройки (кроме приватных — их тут и нет) в JSON."""
    data = dataclasses.asdict(cfg)
    Path(dest).write_text(
        json.dumps({"_app": APP_NAME, "_schema": 2, **data},
                   ensure_ascii=False, indent=2), encoding="utf-8")


def import_settings(src) -> "Config":
    """Читает JSON настроек и возвращает новый Config с проверкой типов
    (неизвестные поля игнорируются, битые значения заменяются дефолтами)."""
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("файл настроек повреждён")
    cfg = Config()
    for f in dataclasses.fields(Config):
        if f.name in data and not f.name.startswith("_"):
            try:
                setattr(cfg, f.name, _coerce(data[f.name], getattr(cfg, f.name)))
            except Exception:
                log.warning("Импорт: поле %s не распознано — оставляю значение по умолчанию", f.name)
    if cfg.update_channel not in ("stable", "beta", "nightly"):
        cfg.update_channel = "stable"
    return cfg


# --------------------------------------------------------------------------
# winget / pip
# --------------------------------------------------------------------------

def winget_exe() -> str | None:
    found = shutil.which("winget")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "winget.exe"
    return str(local) if local.exists() else None


def scan_winget(*, cancel_event: threading.Event | None = None,
                procs: list | None = None) -> list[PackageUpdate]:
    exe = winget_exe()
    if not exe:
        raise RuntimeError(
            "winget не найден. Установите «Установщик приложений» (App Installer) из Microsoft Store."
        )
    args = [exe, "upgrade", "--include-unknown",
            "--accept-source-agreements", "--disable-interactivity"]
    rc, out = run_cmd(args, timeout=300, cancel_event=cancel_event, procs=procs)
    if rc is None:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        raise RuntimeError("winget не успел завершить сканирование (таймаут 300 с)")
    packages = parse_winget_table(out)
    if not packages and rc not in (0,):
        tail = "\n".join(out.splitlines()[-5:])
        raise RuntimeError(f"winget вернул код {rc}. {tail}")
    return packages


def scan_pip(*, cancel_event: threading.Event | None = None,
             procs: list | None = None) -> list[PackageUpdate]:
    args = [sys.executable, "-m", "pip", "list", "--outdated",
            "--format=json", "--disable-pip-version-check"]
    rc, out = run_cmd(args, timeout=600, cancel_event=cancel_event, procs=procs)
    if rc is None:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        raise RuntimeError("pip не успел завершиться (таймаут 600 с)")
    if rc != 0:
        raise RuntimeError(f"pip list --outdated завершился с кодом {rc}")
    return parse_pip_outdated(out)


def npm_exe() -> str | None:
    return shutil.which("npm") or shutil.which("npm.cmd")


def scan_npm(*, cancel_event: threading.Event | None = None,
             procs: list | None = None) -> list[PackageUpdate]:
    """Устаревшие глобальные npm-пакеты. npm outdated возвращает код 1,
    когда есть устаревшие — это норма, а не ошибка."""
    exe = npm_exe()
    if not exe:
        raise RuntimeError("npm не найден (Node.js не установлен?)")
    rc, out = run_cmd([exe, "outdated", "-g", "--json"],
                      timeout=600, cancel_event=cancel_event, procs=procs)
    if rc is None:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        raise RuntimeError("npm не успел завершиться (таймаут 600 с)")
    if rc not in (0, 1):
        tail = "\n".join(out.splitlines()[-3:])
        raise RuntimeError(f"npm outdated завершился с кодом {rc}. {tail}")
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return []
    packages = []
    for name, info in data.items():
        if isinstance(info, dict):
            packages.append(PackageUpdate(
                source="npm", pkg_id=name, name=name,
                version=str(info.get("current") or "?"),
                available=str(info.get("latest") or "?")))
    return packages


def upgrade_command(cfg: "Config", pkg: PackageUpdate) -> list | str:
    if pkg.source == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade",
                pkg.pkg_id, "--disable-pip-version-check"]
    if pkg.source == "npm":
        exe = npm_exe() or "npm"
        return [exe, "install", "-g", f"{pkg.pkg_id}@{pkg.available}"]
    exe = winget_exe() or "winget"
    cmd = [exe, "upgrade", "--id", pkg.pkg_id, "--exact", "--silent",
           "--accept-package-agreements", "--accept-source-agreements",
           "--disable-interactivity"]
    if cfg.winget_extra_args:
        cmd += cfg.winget_extra_args.split()
    return cmd


# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------

def _coerce(value, default):
    """Приводит значение из JSON к типу поля конфига, иначе — значение по умолчанию."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    if isinstance(default, float):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    if isinstance(default, list):
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        if isinstance(value, str):
            return [x.strip() for x in value.replace(";", ",").split(",") if x.strip()]
        return default
    if isinstance(default, dict):
        return value if isinstance(value, dict) else default
    return str(value) if value is not None else default


@dataclass
class Config:
    # VPN
    vpn_processes: list = field(default_factory=lambda: [
        "wireguard", "openvpn", "amnezia", "v2ray", "sing-box", "xray",
        "outline", "nordvpn", "protonvpn", "windscribe", "surfshark",
        "hidemyname", "planetvpn", "adguardvpn",
    ])
    vpn_adapters: list = field(default_factory=lambda: [
        "wireguard", "openvpn", "amnezia", "sing-box", "xray", "tun", "tap", "vpn",
    ])
    vpn_connect_cmd: str = ""        # команда включения VPN (напр. путь к .exe с аргументами)
    vpn_disconnect_cmd: str = ""     # команда выключения VPN
    vpn_disconnect_after: bool = False  # отключать VPN, если подключили его сами
    vpn_home_country: str = "RU"     # ваша страна без VPN
    vpn_ip_url: str = ""             # напр. http://ip-api.com/json — доп. проверка по IP

    # Обновления
    timeout_sec: int = 1800          # таймаут на один пакет
    retries: int = 1                 # повторных попыток после неудачи
    winget_extra_args: str = ""      # доп. аргументы winget, напр. --scope user
    update_pip: bool = False         # обновлять ли pip-пакеты
    update_npm: bool = False         # обновлять ли глобальные npm-пакеты
    auto_interval_hours: float = 0.0 # автопроверка каждые N часов (0 = выкл)
    auto_update_all: bool = False    # при автопроверке сразу обновлять всё
    start_with_windows: bool = False # автозапуск приложения

    # Категории пакетов: {"winget:AIMP.AIMP": "vpn"}
    categories: dict = field(default_factory=dict)
    # Игнорированные вручную: {ключ: версия} — скрыты из списка до новой версии
    ignored_updates: dict = field(default_factory=dict)
    # Пакеты, чьё обновление упало с ошибкой: {ключ: версия, которая не скачалась}.
    # Пока не выйдет другая версия, такие пакеты не предлагаются к обновлению.
    failed_updates: dict = field(default_factory=dict)

    light_theme: bool = False       # (устарело) оставлено для совместимости старых конфигов
    theme_mode: str = "dark"        # dark | light | system
    minimize_to_tray: bool = True   # закрывать окно = свернуть в трей
    notify_enabled: bool = True     # всплывающие уведомления Windows
    restore_point: bool = False     # точка восстановления перед обновлением
    vpn_kill_switch: bool = True    # прерывать загрузку VPN-пакета, если VPN отключился

    # Самообновление приложения (механизм см. selfupdate.py; пока репозиторий
    # не задан в selfupdate.py, проверки просто пишут в журнал «отключено»)
    selfupdate_enabled: bool = True
    update_channel: str = "stable"        # stable | beta | nightly
    selfupdate_check_hours: float = 6.0   # фоновая проверка каждые N часов (0 = вручную)
    selfupdate_skipped: list = field(default_factory=list)  # версии «пропустить»
    app_hash: str = ""               # эталонный хеш файлов приложения (проверки)
    night_mode: bool = False         # автообновление «всё» только ночью (23:00–07:00)
    perf_mode: bool = False          # режим слабых ПК: меньше анимаций и фона
    per_monitor_dpi: bool = False    # per-monitor DPI v2 (на некоторых
                                     # ноутбуках с масштабированием даёт лаги)

    # Правила обновления пакетов
    # Чёрный список: эти пакеты никогда не обновляются (критичные для работы)
    blocked_packages: list = field(default_factory=list)   # например ["numpy"]
    # Закрепление версий: {"numpy": "1.26.4"} — пакет НЕ обновляется, пока pin
    # не уберут или не изменят вручную; любые версии выше закреплённой тоже
    # пропускаются (pin = «держать как есть»)
    pinned_packages: dict = field(default_factory=dict)
    # Не начинать обновление/очистку, если на системном диске меньше этого места (ГБ)
    min_free_gb: float = 2.0

    # ------------------------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if not CONFIG_PATH.exists():
            cfg.save()
            return cfg
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config.json должен содержать объект {}")
        except Exception:
            broken = CONFIG_PATH.with_suffix(".json.broken")
            try:
                CONFIG_PATH.replace(broken)
            except OSError:
                pass
            log.exception("config.json повреждён — использую значения по умолчанию")
            cfg.save()
            return cfg
        for f in dataclasses.fields(cls):
            if f.name in data:
                try:
                    setattr(cfg, f.name, _coerce(data[f.name], getattr(cfg, f.name)))
                except Exception:
                    log.warning("Поле конфига %s не распознано, использую значение по умолчанию", f.name)
        # миграция со старого булева light_theme на theme_mode
        if "theme_mode" not in data and data.get("light_theme"):
            cfg.theme_mode = "light"
        if cfg.theme_mode not in ("dark", "light", "system"):
            cfg.theme_mode = "dark"
        if cfg.update_channel not in ("stable", "beta", "nightly"):
            cfg.update_channel = "stable"
        return cfg

    def save(self) -> None:
        try:
            if CONFIG_PATH.exists():
                shutil.copy2(CONFIG_PATH, CONFIG_PATH.with_suffix(".json.bak"))
            tmp = CONFIG_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(CONFIG_PATH)
        except OSError:
            log.exception("Не удалось сохранить config.json")

    # ------------------------------------------------------------------
    def category_of(self, key: str) -> str:
        return "vpn" if self.categories.get(key) == "vpn" else "normal"

    def set_category(self, key: str, category: str) -> None:
        if category == "vpn":
            self.categories[key] = "vpn"
        else:
            self.categories.pop(key, None)

    # ------------------------------------------------------------------
    # Правила обновления пакетов (чёрный список и закрепление версий)
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_name(name: str) -> str:
        return name.strip().lower().replace("_", "-")

    def _name_matches(self, rule_name: str, pkg: "PackageUpdate") -> bool:
        """Сопоставление правила с пакетом: точное имя ИЛИ подстрока.

        Нужно, потому что у winget ID вида «Python.Numpy», а пользователь
        пишет просто «numpy». Для pip/npm совпадение обычно точное.
        """
        rn = self._norm_name(rule_name)
        if not rn:
            return False
        return (rn == self._norm_name(pkg.pkg_id)
                or rn == self._norm_name(pkg.name)
                or rn in self._norm_name(pkg.pkg_id)
                or rn in self._norm_name(pkg.name))

    def package_rule(self, pkg: "PackageUpdate") -> str:
        """Возвращает правило для пакета: 'update' | 'blocked' | 'pinned'.

        blocked: пакет из blocked_packages — никогда не трогать.
        pinned:  пакет из pinned_packages — держим на указанной версии;
                 любые обновления пропускаются, пока pin не уберут/не изменят.
        """
        for b in self.blocked_packages:
            if self._name_matches(b, pkg):
                return "blocked"
        for p in self.pinned_packages:
            if self._name_matches(p, pkg):
                return "pinned"
        return "update"

    def rule_reason(self, pkg: "PackageUpdate") -> str:
        """Человекочитаемая причина запрета обновления."""
        rule = self.package_rule(pkg)
        if rule == "blocked":
            for b in self.blocked_packages:
                if self._name_matches(b, pkg):
                    return f"в чёрном списке ({b})"
        if rule == "pinned":
            for p, ver in self.pinned_packages.items():
                if self._name_matches(p, pkg):
                    return f"закреплён на версии {ver}"
        return ""


# --------------------------------------------------------------------------
# Автозапуск Windows
# --------------------------------------------------------------------------

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "Obnovlyator"


def _pythonw() -> str:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe)


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_VALUE)
        return True
    except OSError:
        return False


def set_autostart(enable: bool) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    cmd = f'"{_pythonw()}" "{BASE_DIR / "main.py"}"'
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, _RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        log.exception("Не удалось изменить автозапуск в реестре")
        return False


# --------------------------------------------------------------------------
# Один экземпляр приложения и версия Windows
# --------------------------------------------------------------------------

_mutex_handle = None


def acquire_single_instance() -> bool:
    """Именованный мьютекс Windows: True — мы первые, False — уже запущено.
    Освобождается автоматически при завершении процесса.

    Делает несколько попыток с паузой: после самообновления новый exe может
    стартовать в момент, когда старый процесс ещё умирает и держит мьютекс —
    без повторов пользователь видел бы «уже запущен» после каждого обновления.
    """
    global _mutex_handle
    try:
        import ctypes
        for attempt in range(12):            # суммарно ~4,5 секунды
            handle = ctypes.windll.kernel32.CreateMutexW(
                None, False, "ObnovlyatorSingletonMutex")
            _mutex_handle = handle
            if ctypes.windll.kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
                return True
            if attempt < 11:
                time.sleep(0.4)
        return False
    except Exception:
        log.warning("Не удалось создать мьютекс одиночного запуска", exc_info=True)
        return True  # не мешаем запуску из-за собственной ошибки


def release_single_instance() -> None:
    """Освобождает мьютекс заранее — нужно перед перезапуском от администратора,
    чтобы новый экземпляр не упёрся в «уже запущено»."""
    global _mutex_handle
    if _mutex_handle:
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        except Exception:
            log.debug("Не удалось закрыть мьютекс", exc_info=True)
        _mutex_handle = None


_SHOW_EVENT_NAME = "ObnovlyatorShowWindowEvent"


def signal_show_window() -> bool:
    """Просит уже запущенный экземпляр показать окно. True — сигнал дошёл."""
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenEventW(
            0x0002, False, _SHOW_EVENT_NAME)  # EVENT_MODIFY_STATE
        if not handle:
            return False
        ctypes.windll.kernel32.SetEvent(handle)
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except Exception:
        log.debug("signal_show_window не сработал", exc_info=True)
        return False


def start_show_window_watcher(callback) -> None:
    """Слушает событие «второй экземпляр просит показать окно».
    callback вызывается в фоновом потоке — внутри маршаллить в UI через after."""
    import ctypes
    import threading

    def loop() -> None:
        handle = ctypes.windll.kernel32.CreateEventW(
            None, False, False, _SHOW_EVENT_NAME)
        WAIT_OBJECT_0 = 0
        INFINITE = 0xFFFFFFFF
        while True:
            if ctypes.windll.kernel32.WaitForSingleObject(handle, INFINITE) == WAIT_OBJECT_0:
                try:
                    callback()
                except Exception:
                    log.exception("callback показа окна упал")

    threading.Thread(target=loop, daemon=True, name="show-window").start()


def cleanup_stale_mei(max_age_hours: float = 6.0) -> tuple[int, int]:
    """Удаляет осиротевшие _MEI* папки PyInstaller из %TEMP% (наш главный
    симптом — предупреждение «Failed to remove temporary directory» после
    самообновления). Трогает только папки старше max_age_hours и те, что
    реально удаляются; занятые пропускает. Возвращает (папок, байт)."""
    temp = Path(os.environ.get("TEMP") or os.environ.get("TMP")
                or Path.home() / "AppData" / "Local" / "Temp")
    cutoff = time.time() - max_age_hours * 3600
    dirs = removed = freed = 0
    try:
        candidates = list(temp.glob("_MEI*"))
    except OSError:
        return 0, 0
    for d in candidates:
        if not d.is_dir() or d.name == "_MEI":
            continue
        try:
            if d.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        try:
            for f in d.rglob("*"):
                try:
                    if f.is_file():
                        freed += f.stat().st_size
                except OSError:
                    pass
            dirs += 1
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue
    if removed or freed:
        log.info("Убрано осиротевших _MEI-папок: %s (%.1f МБ)", dirs, freed / 1048576)
    return dirs, freed


def windows_build_ok() -> tuple[bool, str]:
    """winget доступен начиная с Windows 10 сборки 17763 (1809)."""
    try:
        version = sys.getwindowsversion()  # type: ignore[attr-defined]
        if version.build < 17763:
            return False, (f"у вас Windows сборка {version.build}, а winget "
                           f"работает только на Windows 10 1809+ (сборка 17763)")
        return True, ""
    except Exception:
        return True, ""  # не смогли определить — не мешаем


# --------------------------------------------------------------------------
# VPN
# --------------------------------------------------------------------------

class VpnManager:
    """Определяет активность VPN по трём независимым признакам.

    Достаточно любого: процесс VPN-клиента, сетевой адаптер или «чужая»
    страна по IP (если задан vpn_ip_url). Возвращает (True/False/None, причина),
    где None — «не удалось определить».
    """

    def __init__(self, cfg: Config, procs: list | None = None):
        self.cfg = cfg
        self.procs = procs if procs is not None else []

    def status(self) -> tuple[bool | None, str]:
        # 1. Процессы VPN-клиентов
        try:
            _, out = run_cmd(["tasklist", "/FO", "CSV", "/NH"],
                             timeout=25, procs=self.procs)
            for line in out.splitlines():
                first = line.split('","')[0].strip().strip('"').lower()
                for name in self.cfg.vpn_processes:
                    if name and name.lower() in first:
                        return True, f"процесс «{first}»"
        except RuntimeError:
            log.warning("Не удалось получить список процессов", exc_info=True)

        # 2. Активные сетевые адаптеры
        try:
            _, out = run_cmd(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { $_.Name }"],
                timeout=40, procs=self.procs,
            )
            for line in out.splitlines():
                adapter = line.strip()
                low = adapter.lower()
                for pattern in self.cfg.vpn_adapters:
                    if pattern and pattern.lower() in low:
                        return True, f"адаптер «{adapter}»"
        except RuntimeError:
            log.warning("Не удалось получить список адаптеров", exc_info=True)

        # 3. Страна по внешнему IP
        if (self.cfg.vpn_ip_url or "").strip():
            country = self._ip_country()
            if country:
                home = (self.cfg.vpn_home_country or "").strip().upper()
                if not home or country.upper() != home:
                    return True, f"IP-страна: {country}"
                return False, f"IP-страна: {country} (домашняя)"

        return False, "признаков VPN не найдено"

    def _ip_country(self) -> str | None:
        url = self.cfg.vpn_ip_url.strip()
        if url.lower().startswith("http:"):
            log.warning("vpn_ip_url должен использовать https:// — проверка по IP пропущена")
            return None
        try:
            req = urllib.request.Request(url,
                                         headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            log.warning("Не удалось определить страну по IP", exc_info=True)
            return None
        if isinstance(data, dict):
            for key in ("country", "countryCode", "country_code", "cc"):
                if data.get(key):
                    return str(data[key])
        return None

    def connect(self) -> tuple[bool, str]:
        cmd = (self.cfg.vpn_connect_cmd or "").strip()
        if not cmd:
            return False, "команда подключения не задана"
        try:
            rc, _ = run_cmd(cmd, shell=True, timeout=90, procs=self.procs)
        except RuntimeError as e:
            return False, str(e)
        if rc is None or rc == 0:
            return True, "команда подключения выполнена"
        return False, f"команда подключения вернула код {rc}"

    def disconnect(self) -> tuple[bool, str]:
        cmd = (self.cfg.vpn_disconnect_cmd or "").strip()
        if not cmd:
            return False, "команда отключения не задана"
        try:
            rc, _ = run_cmd(cmd, shell=True, timeout=60, procs=self.procs)
        except RuntimeError as e:
            return False, str(e)
        return rc == 0, f"код {rc}"

    def wait_active(self, timeout: float = 120,
                    cancel_event: threading.Event | None = None) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cancel_event is not None and cancel_event.is_set():
                return False
            active, _ = self.status()
            if active:
                return True
            time.sleep(3)
        return False


# --------------------------------------------------------------------------
# Движок
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Точка восстановления
# --------------------------------------------------------------------------

def create_restore_point(description: str) -> tuple[bool, str]:
    """Checkpoint-Computer. False — не создана (не админ, отключена защита и т.п.),
    но это не блокирует обновления — движок только пишет в журнал."""
    args = ["powershell", "-NoProfile", "-Command",
            f"Checkpoint-Computer -Description '{description}' "
            f"-RestorePointType MODIFY_SETTINGS"]
    try:
        rc, out = run_cmd(args, timeout=600)
    except RuntimeError as e:
        return False, str(e)
    if rc == 0:
        return True, "точка восстановления создана"
    low = out.lower()
    # система не даёт создавать чаще раза в сутки — это не ошибка
    if "in progress" in low or "24" in low or "restore point" in low and "not" in low:
        return True, "точка за последние 24 часа уже создавалась — пропуск"
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    return False, f"код {rc}" + (f": {tail}" if tail else "") + \
        (" (нужны права администратора и включённая защита системы)" if rc else "")


# --------------------------------------------------------------------------
# Расшифровка ошибок обновления
# --------------------------------------------------------------------------

# (подстрока из вывода, человеческое объяснение)
_ERROR_HINTS = [
    ("hash mismatch", "файл повреждён или изменён — попробуйте позже"),
    ("no applicable installer", "нет установщика под вашу версию Windows"),
    ("installer failed exit code", "сам установщик сообщил об ошибке"),
    ("requires elevation", "нужны права администратора"),
    ("access is denied", "нет доступа (запустите от администратора)"),
    ("0x80190194", "источник вернул 404 — пакет пропал из источника"),
    ("0x80190193", "источник вернул 403 — доступ запрещён (попробуйте VPN)"),
    ("0x80072ee2", "таймаут сети"),
    ("0x80072efd", "нет соединения с источником (попробуйте VPN)"),
    ("0x80072ee7", "не удалось определить адрес источника (DNS)"),
    ("0x80070005", "нет прав — запустите от администратора"),
    ("0x80070002", "файл не найден"),
    ("0x80d03805", "загрузка заблокирована Windows"),
    ("already installed", "версия уже установлена — возможно, нужен перезапуск"),
    ("pending", "требуется перезагрузка для завершения установки"),
]


def explain_error(rc: int | None, out: str = "") -> str:
    """Человеческое объяснение ошибки обновления по коду и хвосту вывода."""
    low = (out or "").lower()
    for needle, hint in _ERROR_HINTS:
        if needle in low:
            return hint
    if rc is None:
        return "процесс был прерван или не ответил вовремя"
    code = rc & 0xFFFFFFFF
    if code == 0:
        return "успешно"
    hi = code >> 16
    if hi == 0x8A15:
        return "ошибка winget или установщика пакета"
    if hi == 0x8007:
        return f"системная ошибка Windows ({hex(code)})"
    if hi == 0x8019:
        return "HTTP-ошибка источника загрузки (попробуйте VPN)"
    if hi == 0x80D0:
        return "ошибка службы загрузки Windows"
    return f"код {hex(code)}"


class Cancelled(Exception):
    """Операция прервана пользователем."""


_LOG_LEVELS = {
    "info": "info", "ok": "info", "warning": "warning", "warn": "warning",
    "error": "error", "debug": "debug",
}


class Engine:
    """Фоновый движок. Все результаты — события-словари в очередь для UI."""

    def __init__(self, cfg: Config, events: queue.Queue):
        self.cfg = cfg
        self.events = events
        self.packages: dict[str, PackageUpdate] = {}
        self.package_logs: dict[str, str] = {}   # последний вывод установки
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._busy = False

    # -- служебное -------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._busy

    def emit(self, **event) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            pass

    def log(self, text: str, level: str = "info") -> None:
        getattr(log, _LOG_LEVELS.get(level, "info"), log.info)(text)
        self.emit(type="log", text=text, level=level)

    def cancel(self) -> None:
        self._cancel.set()
        cancel_all(self._procs)

    def _spawn(self, fn) -> None:
        if self._busy:
            self.log("Движок занят — дождитесь завершения текущей операции", "warning")
            return
        self._cancel.clear()

        def wrap() -> None:
            self._busy = True
            self.emit(type="busy", value=True)
            try:
                fn()
            except Cancelled:
                self.log("Остановлено пользователем", "warning")
                self.emit(type="finished", ok=0, fail=0, skip=0, cancelled=True)
            except RuntimeError as e:
                self.log(str(e), "error")
                self.emit(type="finished", ok=0, fail=0, skip=0, cancelled=False)
            except Exception as e:  # ни одна ошибка движка не должна уронить UI
                log.exception("Неожиданная ошибка движка")
                self.log(f"Неожиданная ошибка: {e!r}", "error")
                self.emit(type="finished", ok=0, fail=0, skip=0, cancelled=False)
            finally:
                self._busy = False
                self.emit(type="busy", value=False)

        self._thread = threading.Thread(target=wrap, daemon=True, name="engine")
        self._thread.start()

    def _run(self, args, *, timeout: float, shell: bool = False, on_line=None):
        rc, out = run_cmd(args, timeout=timeout, shell=shell, on_line=on_line,
                          cancel_event=self._cancel, procs=self._procs)
        if rc is None and not self._cancel.is_set():
            shown = args if isinstance(args, str) else " ".join(map(str, args))
            raise RuntimeError(f"Превышен таймаут ({int(timeout)} с): {shown}")
        return rc, out

    # -- сканирование ----------------------------------------------------
    def start_scan(self, include_pip: bool | None = None) -> None:
        if include_pip is None:
            include_pip = self.cfg.update_pip
        self._spawn(lambda: self._scan_worker(include_pip))

    def _scan_worker(self, include_pip: bool) -> None:
        self.emit(type="phase", text="Сканирование winget…")
        packages = scan_winget(cancel_event=self._cancel, procs=self._procs)
        if include_pip:
            self.emit(type="phase", text="Сканирование pip…")
            try:
                packages += scan_pip(cancel_event=self._cancel, procs=self._procs)
            except RuntimeError as e:
                self.log(f"pip: {e}", "warning")
        if self.cfg.update_npm:
            self.emit(type="phase", text="Сканирование npm…")
            try:
                packages += scan_npm(cancel_event=self._cancel, procs=self._procs)
            except RuntimeError as e:
                self.log(f"npm: {e}", "warning")

        # Правила пакетов: чёрный список и закрепление версий (Настройки ->
        # «Правила пакетов»). Заблокированные/закреплённые не попадают в список.
        kept: list[PackageUpdate] = []
        for p in packages:
            rule = self.cfg.package_rule(p)
            if rule == "update":
                kept.append(p)
            else:
                reason = self.cfg.rule_reason(p)
                self.log(f"Пропущено по правилу: {p.name} ({reason})", "debug")
        if len(kept) != len(packages):
            self.log(f"По правилам пропущено пакетов: {len(packages) - len(kept)}", "info")
        packages = kept

        self.packages = {p.key: p for p in packages}
        via_vpn = sum(1 for p in packages if self.cfg.category_of(p.key) == "vpn")
        self.log(f"Найдено обновлений: {len(packages)} (из них через VPN: {via_vpn})",
                 "info" if packages else "ok")
        self.emit(type="scanned", packages=[asdict(p) for p in packages])
        self.emit(type="phase", text="")

    # -- обновление ------------------------------------------------------
    def start_update(self, keys: list[str]) -> None:
        keys = [k for k in keys if k in self.packages]
        if not keys:
            self.log("Не выбран ни один пакет", "warning")
            return
        self._spawn(lambda: self._update_worker(list(keys)))

    def _update_worker(self, keys: list[str]) -> None:
        # Проверка свободного места: при дефиците обновления не начинаем,
        # чтобы не довести диск до нуля посреди установки.
        if not enough_space(self.cfg):
            self.log(f"На диске C: меньше {self.cfg.min_free_gb:g} ГБ свободно — "
                     f"обновления отложены (Настройки → Правила пакетов). "
                     f"Свободно {disk_free_gb():.1f} ГБ", "error")
            self.emit(type="finished", ok=0, fail=0, skip=0, cancelled=False)
            return

        selected = [self.packages[k] for k in keys]
        normal = [p for p in selected if self.cfg.category_of(p.key) != "vpn"]
        via_vpn = [p for p in selected if self.cfg.category_of(p.key) == "vpn"]
        ok = fail = skip = 0
        done = 0
        total = len(selected)
        self.emit(type="progress", done=0, total=total)

        # Пока идут обновления — Windows не уходит в спящий режим
        with NoSleep():
            self._update_loop(selected, normal, via_vpn)
        # после with — обычное завершение уже сделано в _update_loop через emit

    def _update_loop(self, selected, normal, via_vpn) -> None:
        ok = fail = skip = 0
        done = 0
        total = len(selected)
        self.emit(type="progress", done=0, total=total)

        if self.cfg.restore_point:
            self.emit(type="phase", text="Точка восстановления…")
            self.log("Создаю точку восстановления…", "info")
            rp_ok, msg = create_restore_point(f"{APP_NAME}: перед обновлениями")
            self.log(("✔ " if rp_ok else "⚠ ") + msg, "ok" if rp_ok else "warning")

        def apply(status: str) -> None:
            nonlocal ok, fail, skip, done
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            elif status == "cancelled":
                raise Cancelled()
            else:
                fail += 1
            done += 1
            self.emit(type="progress", done=done, total=total)

        for i, pkg in enumerate(normal, 1):
            self.emit(type="phase", text=f"Обновление без VPN ({i}/{len(normal)})…")
            apply(self._upgrade_one(pkg))

        if via_vpn:
            self.emit(type="phase", text="Проверка VPN…")
            vpn = VpnManager(self.cfg, procs=self._procs)
            active, reason = vpn.status()
            self.emit(type="vpn", active=active, reason=reason)
            connected_here = False

            if active is True:
                self.log(f"VPN активен ({reason})")
            elif (self.cfg.vpn_connect_cmd or "").strip():
                self.log(f"VPN не активен ({reason}) — подключаю…", "warning")
                conn_ok, msg = vpn.connect()
                if conn_ok and vpn.wait_active(timeout=120, cancel_event=self._cancel):
                    connected_here = True
                    self.log("VPN подключён", "ok")
                elif self._cancel.is_set():
                    raise Cancelled()
                else:
                    self.log(f"Не удалось подключить VPN: {msg}", "error")
            else:
                self.log("VPN не активен, команда подключения не задана (Настройки → VPN)", "error")

            if active is True or connected_here:
                for i, pkg in enumerate(via_vpn, 1):
                    self.emit(type="phase", text=f"Обновление через VPN ({i}/{len(via_vpn)})…")
                    apply(self._upgrade_one(pkg))
            else:
                for pkg in via_vpn:
                    self.emit(type="pkg", key=pkg.key, status="skip")
                    apply("skip")
                    history.append("update", name=pkg.name, key=pkg.key, source=pkg.source,
                                   version=pkg.version, available=pkg.available, status="skip")
                self.log(f"Пропущено без VPN: {len(via_vpn)} шт.", "warning")

            if connected_here and self.cfg.vpn_disconnect_after:
                self.emit(type="phase", text="Отключение VPN…")
                disc_ok, msg = vpn.disconnect()
                self.log("VPN отключён" if disc_ok
                         else f"Не удалось отключить VPN ({msg})",
                         "info" if disc_ok else "warning")

        self.emit(type="phase", text="")
        cancelled = self._cancel.is_set()
        self.log(f"Готово. Обновлено: {ok}, ошибки: {fail}, пропущено: {skip}"
                 + (" (остановлено)" if cancelled else ""),
                 "ok" if fail == 0 and not cancelled else "warning")
        self.emit(type="finished", ok=ok, fail=fail, skip=skip, cancelled=cancelled)

    def _upgrade_one(self, pkg: PackageUpdate) -> str:
        """Обновляет один пакет: ретраи, kill-switch VPN, запись в историю.
        Возвращает ok|error|cancelled."""
        self.emit(type="pkg", key=pkg.key, status="run")
        self.log(f"Обновляю {pkg.name} ({pkg.pkg_id}): {pkg.version} → {pkg.available}")
        cmd = upgrade_command(self.cfg, pkg)
        attempts = max(1, int(self.cfg.retries) + 1)
        last_rc = None
        last_out = ""
        vpn_dropped = {"flag": False}

        # kill-switch: если VPN пропал во время загрузки VPN-пакета — убить процесс
        watch_stop = threading.Event()
        if self.cfg.category_of(pkg.key) == "vpn" and self.cfg.vpn_kill_switch:
            def watch():
                vpn = VpnManager(self.cfg, procs=self._procs)
                while not watch_stop.wait(5.0):
                    try:
                        active, _ = vpn.status()
                    except Exception:
                        continue
                    if active is False:
                        vpn_dropped["flag"] = True
                        cancel_all(self._procs)
                        return
            threading.Thread(target=watch, daemon=True, name="vpn-watch").start()

        def record(status: str) -> None:
            history.append("update", name=pkg.name, key=pkg.key, source=pkg.source,
                           version=pkg.version, available=pkg.available, status=status)

        try:
            for attempt in range(1, attempts + 1):
                rc, out = self._run(cmd, timeout=max(60, int(self.cfg.timeout_sec)),
                                    on_line=lambda line: self.log(line, "debug"))
                last_rc, last_out = rc, out
                self.package_logs[pkg.key] = out[-20000:]   # для «Показать лог»
                if rc == 0:
                    self.emit(type="pkg", key=pkg.key, status="ok")
                    self.log(f"{pkg.name}: обновлено ✔", "ok")
                    record("ok")
                    return "ok"
                if rc is None and vpn_dropped["flag"]:
                    self.emit(type="pkg", key=pkg.key, status="error")
                    self.log(f"{pkg.name}: VPN отключился во время загрузки — процесс прерван, "
                             f"чтобы трафик не пошёл мимо VPN. Включите VPN и повторите.", "error")
                    record("error")
                    return "error"
                if rc is None and self._cancel.is_set():
                    self.emit(type="pkg", key=pkg.key, status="cancelled")
                    record("cancelled")
                    return "cancelled"
                if attempt < attempts:
                    self.log(f"{pkg.name}: попытка {attempt} не удалась "
                             f"({explain_error(rc, out)}), повторяю…", "warning")
                    time.sleep(2)
        finally:
            watch_stop.set()

        tail = "; ".join((last_out or "").splitlines()[-2:])
        self.emit(type="pkg", key=pkg.key, status="error")
        self.log(f"{pkg.name}: ошибка — {explain_error(last_rc, last_out)}"
                 + (f". {tail}" if tail else ""), "error")
        record("error")
        return "error"
