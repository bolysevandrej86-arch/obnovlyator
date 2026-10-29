# -*- coding: utf-8 -*-
"""Проверки здоровья системы: целостность Windows, драйверы, диски,
целостность самого Обновлятора.

Каждая проверка возвращает словарь:
    {"status": "ok"|"warn"|"error"|"info", "text": "...", "details": [...]}

status — итог для цветной метки; details — построчные результаты для карточки.
Долгие проверки (SFC, chkdsk, поиск драйверов) выполняются в фоновом потоке
вызывающим кодом; здесь только синхронная логика.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from core import CREATE_NO_WINDOW, run_cmd


def _ps(script: str, *, timeout: float, admin_note: str = "") -> tuple[int, str]:
    """PowerShell-обёртка.

    Скрипт передаётся через -EncodedCommand (base64 UTF-16LE): обычный
    -Command с кавычками внутри ломается при передаче (EmptyPipeElement),
    а вывод принудительно переводим в UTF-8 — иначе русский текст и
    консольных утилит (sfc/dism/chkdsk) приходит в OEM-кодировке и
    превращается в кракозябры.
    """
    import base64
    full = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            + "[Console]::InputEncoding=[System.Text.Encoding]::UTF8; "
            + "$ProgressPreference='SilentlyContinue'; "
            + script)
    encoded = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
    return run_cmd(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                   timeout=timeout)


def _clixml_errors(out: str) -> list[str]:
    """Достаёт человекочитаемые сообщения из CLIXML-вывода PowerShell."""
    import html
    import re
    msgs = []
    for m in re.findall(r'<S S="Error">(.*?)</S>', out or ""):
        m = m.replace("_x000D__x000A_", " ").replace("_x000A_", " ")
        m = html.unescape(html.unescape(m))
        m = re.sub(r"\s+", " ", m).strip()
        if m and not m.startswith("+") and "CategoryInfo" not in m                 and "FullyQualifiedErrorId" not in m:
            msgs.append(m)
    return msgs


def _last_lines(out: str, n: int = 4) -> list[str]:
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    return lines[-n:]


# ---------------------------------------------------------------------------
# Целостность Windows
# ---------------------------------------------------------------------------

def dism_checkhealth(is_admin: bool) -> dict:
    """Быстрая проверка хранилища компонентов (DISM /CheckHealth), ~1 минута."""
    if not is_admin:
        return {"status": "info", "text": "Нужны права администратора "
                "(кнопка перезапуска — в сайдбаре)."}
    rc, out = _ps("& { Dism.exe /Online /Cleanup-Image /CheckHealth | Out-String }",
                  timeout=600)
    details = _last_lines(out, 4)
    if rc == 0:
        low = (out or "").lower()
        if "repairable" in low or "восстановлен" in low:
            return {"status": "warn", "text": "Хранилище компонентов помечено как "
                    "повреждаемое — рекомендуется «полная проверка» (SFC).",
                    "details": details}
        return {"status": "ok", "text": "Хранилище компонентов в порядке.",
                "details": details}
    return {"status": "error", "text": f"DISM завершился с кодом {rc}.",
            "details": details}


def sfc_verifyonly(cancel_event=None, on_note=None) -> dict:
    """Полная проверка целостности системных файлов (sfc /verifyonly).
    Долгая: обычно 5–15 минут. Изменений не вносит — только докладывает."""
    if not is_admin_elevated():
        return {"status": "info", "text": "Нужны права администратора "
                "(кнопка перезапуска — в сайдбаре)."}
    if on_note:
        on_note("Идёт полная проверка системных файлов — 5–15 минут, "
                "окно можно свернуть.")
    # sfc.exe при перенаправлении пишет UTF-16LE, PowerShell его искажает —
    # вывод уходит в файл и читается с явной кодировкой Unicode
    rc, out = _ps(
        "& { $f = Join-Path $env:TEMP 'obnovlyator_sfc.txt'; "
        "cmd /c (\"sfc /verifyonly > \" + $f + \" 2>&1\"); "
        "Get-Content -LiteralPath $f -Encoding Unicode | Out-String; "
        "Remove-Item -LiteralPath $f -ErrorAction SilentlyContinue }",
        timeout=2700)
    low = (out or "").lower()
    details = _last_lines(out, 5)
    if rc == 0 and ("не обнаружено нарушений целостности" in low
                    or "did not find any integrity violations" in low):
        return {"status": "ok", "text": "Нарушений целостности системных "
                "файлов не найдено.", "details": details}
    if "not" in low and ("perform" in low or "выполнить" in low):
        return {"status": "error", "text": "SFC не смог выполниться "
                "(нужны права администратора или безопасный режим).",
                "details": details}
    if rc == 0:
        return {"status": "warn", "text": "SFC завершился, но нашёл записи — "
                "смотрите детали. Нарушения можно чинить: sfc /scannow "
                "от администратора.", "details": details}
    return {"status": "error", "text": f"SFC завершился с кодом {rc}.",
            "details": details}


def is_admin_elevated() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Драйверы
# ---------------------------------------------------------------------------

def problem_devices() -> dict:
    """Устройства с ошибками в диспетчере устройств (быстрая проверка)."""
    rc, out = _ps(
        "& { Get-CimInstance Win32_PnPEntity | Where-Object "
        "{ $_.ConfigManagerErrorCode -ne 0 } | ForEach-Object "
        "{ \"$($_.Name)|$($_.ConfigManagerErrorCode)\" } | Out-String }",
        timeout=180)
    devices = []
    for line in (out or "").splitlines():
        line = line.strip().strip('"')
        if "|" not in line:
            continue
        name, _, code = line.rpartition("|")
        if name.strip():
            devices.append((name.strip(), code.strip()))
    if devices:
        meanings = {
            "1": "устройство настроено неверно",
            "10": "не запускается",
            "18": "требуется переустановка драйверов",
            "22": "отключено",
            "28": "драйверы не установлены",
            "31": "не работает нормально",
            "43": "устройство остановлено из-за неполадок",
            "45": "устройство не подключено (не используется)",
        }
        details = [f"{name} — {meanings.get(code, 'код ' + code)}"
                   for name, code in devices[:12]]
        return {"status": "warn", "text": f"Проблемных устройств: {len(devices)}.",
                "details": details}
    return {"status": "ok", "text": "Проблемных устройств не найдено — все "
            "драйверы работают без ошибок."}


def driver_install(cancel_event=None, on_note=None, is_admin: bool = False) -> dict:
    """УСТАНОВКА найденных обновлений драйверов через Windows Update.
    Долго (10–40 минут), требуются права администратора."""
    if not is_admin:
        return {"status": "info", "text": "Установка драйверов требует прав "
                "администратора (кнопка 🔑 в сайдбаре)."}
    if on_note:
        on_note("Ищу драйверы и устанавливаю — 10–40 минут, "
                "компьютер лучше не выключать.")
    script = (
        "& { $ErrorActionPreference='Stop'; "
        "$s = New-Object -ComObject Microsoft.Update.Session; "
        "$searcher = $s.CreateUpdateSearcher(); "
        "$r = $searcher.Search(\"IsInstalled=0 and Type='Driver'\"); "
        "if ($r.Updates.Count -eq 0) { 'NO_DRIVERS' } else { "
        "$inst = $s.CreateUpdateInstaller(); "
        "$inst.Updates = $r.Updates; "
        "foreach ($u in $r.Updates) { $u.Accept() | Out-Null }; "
        "$res = $inst.Install(); "
        "for ($i=0; $i -lt $res.Updates.Count; $i++) { "
        "$code = $res.GetUpdateResult($i).ResultCode; "
        "(\"RESULT|\" + $r.Updates.Item($i).Title + \"|\" + $code) } } }")
    rc, out = _ps(script, timeout=3600)
    if "NO_DRIVERS" in (out or ""):
        return {"status": "ok", "text": "Windows Update не предлагает "
                "обновлений драйверов — устанавливать нечего."}
    results = []
    for line in (out or "").splitlines():
        if not line.startswith("RESULT|"):
            continue
        _, title, code = line.split("|", 2)
        mark = {"2": "установлено", "3": "установлено с ошибками",
                "4": "ОШИБКА", "5": "отменено"}.get(code.strip(), "код " + code)
        results.append(f"{title} — {mark}")
    ok_n = sum(1 for r in results if "установлено" in r)
    if not results and rc != 0:
        tail = "; ".join(_last_lines(out, 2))
        return {"status": "error", "text": f"Установка не удалась (код {rc}). {tail}"}
    status = "ok" if ok_n == len(results) and results else ("warn" if results else "error")
    text = (f"Установлено драйверов: {ok_n} из {len(results)}. "
            if results else "Установка завершилась без результата. ")
    if results:
        text += "Перезагрузите компьютер, если Windows попросит."
    return {"status": status, "text": text, "details": results}


def driver_updates_wu(cancel_event=None, on_note=None) -> dict:
    """Поиск обновлений драйверов через Windows Update (1–5 минут)."""
    if on_note:
        on_note("Опрашиваю Windows Update — это может занять несколько минут.")
    script = (
        "& { $s = New-Object -ComObject Microsoft.Update.Session; "
        "$searcher = $s.CreateUpdateSearcher(); "
        "$r = $searcher.Search(\"IsInstalled=0 and Type='Driver'\"); "
        "if ($r.Updates.Count -eq 0) { 'NO_DRIVERS' } "
        "else { foreach ($u in $r.Updates) { $u.Title } } }")
    rc, out = _ps(script, timeout=900)
    if "NO_DRIVERS" in (out or ""):
        return {"status": "ok", "text": "Windows Update не предлагает "
                "обновлений драйверов."}
    junk = ("#< CLIXML", "<Objs", "<?x", "</Objs", "<S ", "<MS ", "&", "$",
            "if ", "else ")
    titles = [l.strip().strip('"') for l in (out or "").splitlines()
              if l.strip() and not l.strip().startswith(junk)]
    if not titles and rc != 0:
        tail = "; ".join(_last_lines(out, 2))
        return {"status": "error", "text": f"Поиск не удался (код {rc}). {tail}"}
    details = titles[:12]
    return {"status": "info", "text": f"Доступно обновлений драйверов: "
            f"{len(titles)}. Ставятся через Центр обновления Windows "
            "(Параметры → Центр обновления → Дополнительные параметры).",
            "details": details}


# ---------------------------------------------------------------------------
# Диски
# ---------------------------------------------------------------------------

def disk_health() -> dict:
    """SMART-статус физических дисков + свободное место (быстрая)."""
    rc, out = _ps(
        "& { Get-PhysicalDisk | ForEach-Object "
        "{ \"DISK|$($_.FriendlyName)|$($_.HealthStatus)|$([math]::Round($_.Size/1GB))GB\" }; "
        "Get-Volume | Where-Object DriveLetter | ForEach-Object "
        "{ \"VOL|$($_.DriveLetter): | свободно $([math]::Round($_.SizeRemaining/1GB,1)) ГБ "
        "из $([math]::Round($_.Size/1GB,1)) ГБ\" } | Out-String }",
        timeout=180)
    disk_rows, vol_rows, warnings = [], [], []
    for line in (out or "").splitlines():
        line = line.strip().strip('"')
        parts = line.split("|")
        if parts[0] == "DISK" and len(parts) >= 4:
            disk_rows.append(f"{parts[1]} — {parts[3]}")
            if parts[2].lower() != "healthy":
                warnings.append(f"Диск {parts[1]}: статус {parts[2]}")
        elif parts[0] == "VOL" and len(parts) >= 3:
            vol_rows.append("".join(parts[1:]))
    if not disk_rows and not vol_rows:
        tail = "; ".join(_last_lines(out, 2))
        return {"status": "error", "text": f"Не удалось опросить диски. {tail}"}
    details = ([f"Диск: {r}" for r in disk_rows] +
               [f"Том: {r}" for r in vol_rows[:8]])
    if warnings:
        return {"status": "error", "text": " ".join(warnings)
                + " Срочно скопируйте важное и проверьте диск!", "details": details}
    return {"status": "ok", "text": "Все диски healthy, критичного дефицита "
            "места проверьте по списку томов.", "details": details}


def chkdsk_scan(cancel_event=None, on_note=None) -> dict:
    """Онлайн-проверка файловой системы системного диска (chkdsk /scan —
    только чтение, ошибок не чинит). Долгая: 5–20 минут."""
    if not is_admin_elevated():
        return {"status": "info", "text": "Нужны права администратора "
                "(кнопка перезапуска — в сайдбаре)."}
    drive = Path(os.environ.get("WINDIR", r"C:\Windows")).drive or "C:"
    if on_note:
        on_note(f"Проверяю файловую систему {drive} — 5–20 минут, "
                "окно можно свернуть.")
    rc, out = _ps(f"chkdsk {drive} /scan | Out-String", timeout=2400)
    low = (out or "").lower()
    details = _last_lines(out, 5)
    if rc is None:
        return {"status": "error", "text": "chkdsk был прерван.", "details": details}
    if rc != 0:
        return {"status": "error", "text": f"chkdsk завершился с кодом {rc}.",
                "details": details}
    bad_markers = ("ошибки", "errors found", "damaged", "поврежденных секторах")
    if "проблем не найдено" in low or "no problems" in low \
            or "0 байт в поврежденных секторах" in low \
            or "0 кб в поврежденных секторах" in low \
            or "0 kb in bad sectors" in low:
        return {"status": "ok", "text": f"Файловая система {drive} в порядке, "
                "ошибок не найдено.", "details": details}
    return {"status": "warn", "text": f"{drive}: chkdsk нашёл возможные "
            "ошибки — смотрите детали.", "details": details}


def restore_points_list() -> dict:
    """Последние точки восстановления системы."""
    if not is_admin_elevated():
        return {"status": "info", "text": "Нужны права администратора "
                "(кнопка 🔑 в сайдбаре)."}
    rc, out = _ps(
        "& { Get-ComputerRestorePoint | ForEach-Object "
        "{ \"POINT|\" + $_.SequenceNumber + \"|\" + $_.Description + \"|\" "
        "+ $_.ConvertToDateTime($_.CreationTime) } | Out-String }", timeout=180)
    points = []
    for line in (out or "").splitlines():
        line = line.strip().strip('"')
        if not line.startswith("POINT|"):
            continue
        parts = line.split("|", 3)
        if len(parts) == 4:
            points.append(f"№{parts[1]} — {parts[2]} ({parts[3].split('.')[0]})")
    if not points:
        return {"status": "info", "text": "Точек восстановления нет. Создайте "
                "первую кнопкой «Создать точку»."}
    return {"status": "ok", "text": f"Точек восстановления: {len(points)} "
            f"(показаны последние {min(len(points), 10)}).",
            "details": points[:10]}


def restore_point_create() -> dict:
    """Создаёт точку восстановления системы."""
    if not is_admin_elevated():
        return {"status": "info", "text": "Нужны права администратора "
                "(кнопка 🔑 в сайдбаре)."}
    from core import create_restore_point
    ok, msg = create_restore_point("Обновлятор: ручная точка")
    text = msg + ("" if ok else ". Если защита системы выключена — включите: "
                  "Параметры → Система → О системе → Защита системы.")
    return {"status": "ok" if ok else "warn", "text": text}


def restore_open_ui() -> dict:
    """Открывает штатный мастер восстановления (rstrui.exe)."""
    try:
        subprocess.Popen(["rstrui.exe"], creationflags=CREATE_NO_WINDOW)
        return {"status": "ok", "text": "Мастер восстановления открыт: выберите "
                "точку и следуйте указаниям мастера."}
    except OSError as e:
        return {"status": "error", "text": f"Не удалось открыть rstrui: {e}"}


# ---------------------------------------------------------------------------
# Целостность самого Обновлятора
# ---------------------------------------------------------------------------

_CORE_FILES = ("core.py", "ui.py", "main.py", "cleanup.py", "bloat.py",
               "history.py", "tray.py", "selfupdate.py", "selfupdate_ui.py",
               "diag.py")


def _own_hashes() -> tuple[str, str]:
    """(что проверяем, sha256). В exe — сам exe; из исходников — набор .py."""
    if getattr(sys, "frozen", False):
        target = Path(sys.executable)
        return f"exe «{target.name}»", _hash_file(target)
    base = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    counted = 0
    for name in _CORE_FILES:
        p = base / name
        if p.exists():
            digest.update(p.read_bytes())
            counted += 1
    return f"исходники ({counted} файлов)", digest.hexdigest()


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def app_integrity(cfg) -> dict:
    """Сверяет файлы приложения с эталоном, записанным при первом запуске.
    Эталон обновляется после успешного самообновления."""
    what, current = _own_hashes()
    stored = (getattr(cfg, "app_hash", "") or "").lower()
    short = f"{current[:16]}…"
    if not stored:
        cfg.app_hash = current
        cfg.save()
        return {"status": "ok", "text": f"Первичная проверка: эталон {what} "
                f"записан.\nSHA256: {short}"}
    if stored == current.lower():
        return {"status": "ok", "text": f"Целостность {what} подтверждена — "
                f"файлы совпадают с эталоном.\nSHA256: {short}"}
    return {"status": "warn",
            "text": f"Файлы {what} отличаются от эталона!\nЕсли вы обновляли "
            f"приложение вручную — это нормально, эталон обновится после "
            f"следующего запуска проверки.\nSHA256 сейчас: {short}",
            "details": ["Эталон: " + stored[:16] + "…",
                        "Сейчас: " + current[:16] + "…"]}
