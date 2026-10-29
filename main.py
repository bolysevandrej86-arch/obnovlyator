# -*- coding: utf-8 -*-
"""Обновлятор — точка входа.

Запуск GUI:            python main.py
Самопроверка ядра:     python main.py --selftest [--no-net]
Проверка сборки UI:    python main.py --smoke
Обновления приложения: python main.py --check-selfupdate
Флаги самообновления:  --post-update (внутренний, ставится автоматически),
                       --minimized (старт свёрнутым в трей)
"""
import sys


def _selftest(no_net: bool) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    from core import (CONFIG_PATH, Config, VpnManager, autostart_enabled,
                      setup_logging, scan_winget, winget_exe)
    setup_logging()
    ok = True

    print("[1] Конфигурация")
    try:
        cfg = Config.load()
        cfg.save()
        print(f"    OK: {CONFIG_PATH}")
    except Exception as e:
        print(f"    FAIL: {e!r}")
        return 1

    print(f"[2] winget: {winget_exe() or 'НЕ НАЙДЕН'}")
    if not winget_exe():
        ok = False

    if not no_net:
        print("[3] Сканирование winget (реальное)")
        try:
            packages = scan_winget()
            for p in packages:
                print(f"    - {p.name} [{p.pkg_id}] {p.version} -> {p.available} ({p.source})")
            print(f"    найдено: {len(packages)}")
        except Exception as e:
            print(f"    FAIL: {e}")
            ok = False

        print("[4] Определение VPN")
        try:
            active, reason = VpnManager(cfg).status()
            print(f"    активен={active} ({reason})")
        except Exception as e:
            print(f"    FAIL: {e!r}")
            ok = False

        print(f"[5] Автозапуск Windows: {'включён' if autostart_enabled() else 'выключен'}")

    print("Самопроверка:", "ПРОЙДЕНА" if ok else "ЕСТЬ ПРОБЛЕМЫ")
    return 0 if ok else 1


def _smoke() -> int:
    """Собирает окно, ничего не показывая, и сразу закрывает —
    проверка того, что интерфейс конструируется без ошибок."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    from ui import App
    app = App(is_admin=True)
    app.withdraw()
    app.after(1500, app.destroy)
    app.mainloop()
    print("UI собрался без ошибок.")
    return 0


def main() -> None:
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest("--no-net" in argv))
    if "--smoke" in argv:
        sys.exit(_smoke())
    if "--check-selfupdate" in argv:
        # Проверить обновления самого Обновлятора и выйти (для отладки/CLI)
        from core import Config, setup_logging, APP_VERSION
        import selfupdate
        setup_logging()
        up = selfupdate.create_updater(Config.load(), APP_VERSION)
        try:
            info = up.check_for_updates() if up else None
        except Exception as e:
            print(f"Ошибка проверки: {e}")
            return
        if info is None:
            print("Обновлений нет (или репозиторий ещё не настроен — см. selfupdate.py)")
        else:
            print(f"Доступна версия {info.version} ({info.channel}): {info.url}")
        return

    import ctypes
    import time
    from core import (APP_VERSION, Config, acquire_single_instance,
                      release_single_instance, setup_logging,
                      signal_show_window, start_show_window_watcher,
                      windows_build_ok)

    # Чёткий рендер на экранах с масштабированием: без этого Windows
    # растягивает картинку 96 DPI — текст и линии выглядят «пиксельными».
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    setup_logging()

    # ШАГ БЕЗОПАСНОСТИ №1: ещё до GUI проверяем состояние прошлого обновления.
    # Если после подмены exe приложение падало 2 раза — оно само откатится
    # на бэкап и перезапустится (см. selfupdate.handle_startup_state).
    import selfupdate
    _up = selfupdate.create_updater(Config.load(), APP_VERSION)
    if _up is not None:
        _up.handle_startup_state()   # может откатить и завершить процесс

    # одиночный экземпляр. --takeover ставит перезапуск от администратора:
    # старый процесс уже закрывается, новый дожидается освобождения мьютекса.
    if not acquire_single_instance():
        if "--takeover" in sys.argv:
            deadline = time.time() + 15
            while time.time() < deadline:
                time.sleep(0.2)
                if acquire_single_instance():
                    break
            else:
                signal_show_window()
                ctypes.windll.user32.MessageBoxW(
                    0, "Прошлый экземпляр не завершился за 15 секунд.\n"
                       "Закройте его через трей и попробуйте снова.",
                    "Обновлятор", 0x30)
                sys.exit(0)
        else:
            # приложение уже работает (возможно, свёрнуто в трей) —
            # просто показываем его окно вместо заглушки
            if not signal_show_window():
                ctypes.windll.user32.MessageBoxW(
                    0, "Обновлятор уже запущен.\nИщите его иконку в трее (возле часов).",
                    "Обновлятор", 0x40)
            sys.exit(0)

    ok_build, msg = windows_build_ok()
    if not ok_build:
        ctypes.windll.user32.MessageBoxW(
            0, f"Внимание: {msg}\n\nОбновления winget работать не будут.",
            "Обновлятор", 0x30)  # MB_ICONWARNING

    from ui import App
    app = App(is_admin=bool(ctypes.windll.shell32.IsUserAnAdmin()))
    splash = None
    if "--post-update" in argv:
        # короткая плашка «завершаем обновление» — висит, пока GUI поднимается
        try:
            import tkinter as _tk
            splash = _tk.Tk()
            splash.overrideredirect(True)
            splash.attributes("-topmost", True)
            w, h = 420, 130
            splash.geometry(f"{w}x{h}+{(splash.winfo_screenwidth() - w) // 2} "
                            f"+{(splash.winfo_screenheight() - h) // 2}")
            splash.configure(bg="#1b1b20")
            _tk.Label(splash, text="🔄  Завершаем обновление\nОбновлятора…",
                      bg="#1b1b20", fg="#e4e4e7", justify="center",
                      font=("Segoe UI", 13, "bold")).pack(expand=True)
            splash.update()
        except Exception:
            splash = None
    if "--minimized" in argv:
        # флаг используется автозапуском: старт свёрнутым в трей
        app.withdraw()
    start_show_window_watcher(lambda: app.after(0, app.tray_open_window))
    if splash is not None:
        splash.after(2500, splash.destroy)   # держим пару секунд для верности
        splash.mainloop()                    # свой мини-цикл для отрисовки
    app.mainloop()


if __name__ == "__main__":
    main()
