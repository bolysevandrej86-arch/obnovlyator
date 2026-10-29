# -*- coding: utf-8 -*-
"""История действий Обновлятора: JSONL-файл + сводная статистика.

Каждая запись — одна строка JSON: {"ts": "...", "kind": "update",
"name": "...", ...}. Файл читается человеком и легко чистится.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

def _history_path() -> Path:
    """Рядом с приложением. В собранном .exe __file__ указывает на временную
    папку распаковки — берём BASE_DIR из core (отложенно: core импортирует
    этот модуль на старте, прямой импорт дал бы цикл)."""
    try:
        from core import BASE_DIR
        return BASE_DIR / "history.jsonl"
    except Exception:
        return Path(__file__).resolve().parent / "history.jsonl"


_LOCK = threading.Lock()


def append(kind: str, **fields) -> None:
    record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **fields}
    try:
        with _LOCK:
            with open(_history_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        from core import log
        log.warning("Не удалось записать в history.jsonl", exc_info=True)


def read(limit: int | None = None) -> list[dict]:
    """Все записи (новые в конце); limit — последние N."""
    if not _history_path().exists():
        return []
    rows: list[dict] = []
    try:
        with open(_history_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-limit:] if limit else rows


def _within_days(ts: str, days: int) -> bool:
    try:
        t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        return time.time() - t <= days * 86400
    except (ValueError, OverflowError):
        return False


def stats(days: int = 30) -> dict:
    """Сводка: {'ok30': N, 'err30': N, 'cleaned30': bytes, 'removed30': N,
    'ok_all': N, 'err_all': N, 'cleaned_all': bytes, 'removed_all': N,
    'selfupd_all': N, 'selfupd_rolled': N}."""
    out = {k: 0 for k in ("ok30", "err30", "cleaned30", "removed30",
                          "ok_all", "err_all", "cleaned_all", "removed_all",
                          "selfupd_all", "selfupd_rolled")}
    for row in read():
        recent = _within_days(str(row.get("ts", "")), days)
        kind = row.get("kind")
        status = row.get("status")
        freed = row.get("freed") or 0
        if kind == "update":
            if status == "ok":
                out["ok_all"] += 1
                if recent:
                    out["ok30"] += 1
            elif status == "error":
                out["err_all"] += 1
                if recent:
                    out["err30"] += 1
        elif kind == "cleanup" and isinstance(freed, (int, float)):
            out["cleaned_all"] += freed
            if recent:
                out["cleaned30"] += freed
        elif kind == "uninstall" and row.get("ok"):
            out["removed_all"] += 1
            if recent:
                out["removed30"] += 1
        elif kind == "selfupdate":
            # статус ok = успешное самообновление; rolled_back = откат
            if status == "ok":
                out["selfupd_all"] += 1
            elif status in ("rolled_back", "rollback_error"):
                out["selfupd_rolled"] += 1
    return out


def daily_series(days: int = 14) -> list[tuple[str, int]]:
    """Число успешных обновлений пакетов по дням за последние `days` дней.
    Используется графиком в разделе «История»."""
    counts: dict[str, int] = {}
    for row in read():
        if row.get("kind") != "update" or row.get("status") != "ok":
            continue
        day = str(row.get("ts", ""))[:10]        # 'YYYY-MM-DD'
        if day:
            counts[day] = counts.get(day, 0) + 1
    out = []
    for i in range(days - 1, -1, -1):
        key = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        out.append((key[5:].replace("-", "."), counts.get(key, 0)))
    return out


def export_txt(dest) -> int:
    """Вся история читабельным текстом. Возвращает число записей."""
    rows = read()
    s = stats(30)
    with open(dest, "w", encoding="utf-8") as f:
        f.write("ИСТОРИЯ ОБНОВЛЯТОРА\n" + "=" * 64 + "\n")
        f.write(f"За 30 дней: обновлено {s['ok30']}, ошибок {s['err30']}, "
                f"освобождено {s['cleaned30'] / 1024 ** 3:.2f} ГБ, "
                f"удалено программ {s['removed30']}\n")
        f.write(f"За всё время: обновлено {s['ok_all']}, ошибок {s['err_all']}, "
                f"освобождено {s['cleaned_all'] / 1024 ** 3:.2f} ГБ, "
                f"удалено программ {s['removed_all']}, "
                f"обновлений приложения {s['selfupd_all']}\n")
        f.write("=" * 64 + "\n\n")
        for r in rows:
            f.write(f"[{r.get('ts', '')}] {r.get('kind', '?'):10} ")
            f.write("  ".join(f"{k}={v}" for k, v in r.items()
                              if k not in ("ts", "kind")))
            f.write("\n")
    return len(rows)


def export_csv(dest) -> int:
    """Вся история в CSV с ; разделителем (открывается в Excel)."""
    import csv
    rows = read()
    keys = ["ts", "kind", "name", "key", "source", "version", "available",
            "status", "freed", "detail"]
    with open(dest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(keys + ["extra"])
        for r in rows:
            base = [r.get(k, "") for k in keys]
            extra = "  ".join(f"{k}={v}" for k, v in r.items() if k not in keys)
            w.writerow(base + [extra])
    return len(rows)
