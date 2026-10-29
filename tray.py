# -*- coding: utf-8 -*-
"""Системный трей и всплывающие уведомления Windows.

Зависимости необязательные: если pystray/winotify не установлены,
приложение работает как раньше, просто без трея и тостов.
"""
from __future__ import annotations

from core import APP_NAME, APP_VERSION, log

APP_ID = "Обновлятор"

_icon_image_cache = None


def _make_icon_image():
    """Рисует значок: тёмный круг с синей стрелкой-обновлением."""
    global _icon_image_cache
    if _icon_image_cache is not None:
        return _icon_image_cache
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([2, 2, size - 2, size - 2], radius=14,
                           fill=(27, 27, 32, 255))
    # стрелка-обновление: дуга с наконечником
    draw.arc([14, 14, 50, 50], start=300, end=200, fill=(59, 130, 246, 255), width=7)
    draw.polygon([(44, 12), (58, 24), (38, 28)], fill=(59, 130, 246, 255))
    _icon_image_cache = img
    return img


def start_tray(app) -> bool:
    """Запускает иконку в трее. False — pystray недоступен."""
    try:
        import pystray
    except ImportError:
        log.info("pystray не установлен — трей отключён (pip install pystray)")
        return False
    image = _make_icon_image()
    if image is None:
        return False

    def open_window():
        app.after(0, app.tray_open_window)

    def check_updates():
        app.after(0, app.tray_check_updates)

    def update_all():
        app.after(0, app.tray_update_all)

    def selfupdate_check():
        app.after(0, app.tray_check_selfupdate)

    def quit_app():
        app.after(0, app.tray_quit)

    menu = pystray.Menu(
        pystray.MenuItem("Открыть", open_window, default=True),
        pystray.MenuItem("Проверить обновления программ", check_updates),
        pystray.MenuItem("Обновить всё", update_all),
        pystray.MenuItem("Обновить сам Обновлятор", selfupdate_check),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", quit_app),
    )
    icon = pystray.Icon(APP_ID, image, f"{APP_NAME} v{APP_VERSION}", menu)

    import threading

    def run():
        try:
            icon.run_detached()
        except Exception:
            log.warning("Не удалось запустить трей", exc_info=True)

    threading.Thread(target=run, daemon=True, name="tray").start()
    app._tray_icon = icon
    return True


def stop_tray(app) -> None:
    icon = getattr(app, "_tray_icon", None)
    if icon is not None:
        try:
            icon.stop()
        except Exception:
            pass
        app._tray_icon = None


def notify(title: str, msg: str) -> None:
    """Тост Windows; молча пропускается, если winotify нет или он не сработал."""
    try:
        from winotify import Notification
        n = Notification(app_id=APP_ID, title=title[:64], msg=msg[:250], duration="short")
        n.show()
    except Exception:
        log.debug("Уведомление не отправлено", exc_info=True)
