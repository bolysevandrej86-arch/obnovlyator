# -*- coding: utf-8 -*-
"""Диалог самообновления Обновлятора в стиле CustomTkinter.

Два этапа:
  1. «Доступна версия X» — заметки, размер, канал, кнопки Обновить/Позже/Пропустить.
  2. Прогресс загрузки (проценты, скорость) + «Установить сейчас».

Все тяжёлые операции — в фоновых потоках; UI обновляется только через
self.after(0, ...) из главного потока (правило ui.py).
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox
from typing import Callable, Optional

import customtkinter as ctk

import selfupdate
from selfupdate import UpdateError

# Палитра как в ui.py: (для светлой, для тёмной)
ACCENT = "#3b82f6"
GREEN = "#16a34a"
RED = "#dc2626"
CARD_BG = ("#f6f6fa", "#232329")
PILL_BG = ("#dcdce3", "#26262c")
MUTED = ("#5b5b66", "#8b8b94")
FAINT = ("#7b7b85", "#6b6b74")
BTN_GRAY = ("#a9a9ba", "#2e2e35")
BTN_GRAY_HOVER = ("#9696a9", "#3f3f46")
BTN_TEXT = ("#20202a", "#e4e4e7")


def _fmt_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{int(n)} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ГБ"


class SelfUpdateDialog(ctk.CTkToplevel):
    """Модальное окно обновления самого приложения."""

    def __init__(self, app, updater: selfupdate.SelfUpdater, info,
                 on_skip: Optional[Callable[[str]], None] = None,
                 on_ready: Optional[Callable[[object, object], None]] = None):
        super().__init__(app)
        self.app = app
        self.updater = updater
        self.info = info
        self.on_skip = on_skip
        self.on_ready = on_ready
        self._downloading = False
        self._closed = False
        self._last_speed = 0.0

        self.title("Обновление Обновлятора")
        self.geometry("560x420")
        self.resizable(False, False)
        self.transient(app)
        self.after(150, self.grab_set)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # --- шапка
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(18, 4))
        title = f"Доступна версия {info.version}"
        if info.critical:
            title += "   [КРИТИЧЕСКОЕ]"
        ctk.CTkLabel(head, text=title, anchor="w",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(fill="x")
        meta = f"Канал: {info.channel}   ·   Размер: ~{_fmt_size(info.size)}"
        if info.published_at:
            meta += f"   ·   {str(info.published_at)[:10]}"
        ctk.CTkLabel(head, text=meta, anchor="w", font=ctk.CTkFont(size=11),
                     text_color=MUTED).pack(fill="x", pady=(2, 0))

        # --- заметки релиза
        if info.notes:
            box = ctk.CTkTextbox(self, height=150, fg_color=CARD_BG,
                                 font=ctk.CTkFont(size=12))
            box.pack(fill="x", padx=20, pady=10)
            box.insert("1.0", str(info.notes).strip())
            box.configure(state="disabled")

        # --- прогресс (этап 2, скрыт)
        self.prog_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.bar = ctk.CTkProgressBar(self.prog_frame, height=14,
                                      progress_color=ACCENT)
        self.bar.set(0)
        self.bar.pack(fill="x", padx=20, pady=(6, 2))
        self.lbl_prog = ctk.CTkLabel(self.prog_frame, text="", anchor="w",
                                     font=ctk.CTkFont(size=11), text_color=MUTED)
        self.lbl_prog.pack(fill="x", padx=20, pady=(0, 4))

        # --- кнопки
        btns = ctk.CTkFrame(self, fg_color="transparent")
        self.btns = btns
        btns.pack(fill="x", padx=20, pady=(6, 18))
        self.btn_update = ctk.CTkButton(btns, text="⬇  Обновить", width=150,
                                        height=34, fg_color=GREEN,
                                        hover_color="#15803d",
                                        command=self._start_download)
        self.btn_update.pack(side="right")
        self.btn_skip = ctk.CTkButton(btns, text="Пропустить версию", width=150,
                                      height=34, fg_color=BTN_GRAY,
                                      hover_color=BTN_GRAY_HOVER,
                                      text_color=BTN_TEXT,
                                      command=self._on_skip)
        self.btn_skip.pack(side="right", padx=8)
        self.btn_later = ctk.CTkButton(btns, text="Позже", width=110, height=34,
                                       fg_color=BTN_GRAY,
                                       hover_color=BTN_GRAY_HOVER,
                                       text_color=BTN_TEXT,
                                       command=self._on_later)
        self.btn_later.pack(side="left")

        if info.critical:
            # критические обновления можно только отложить, не пропустить
            self.btn_skip.configure(state="disabled")

    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        if self._downloading:
            if not messagebox.askyesno(
                    "Загрузка идёт", "Прервать загрузку обновления и закрыть?",
                    parent=self):
                return
        self._finish()

    def _finish(self) -> None:
        self._closed = True
        try:
            self.grab_release()
        except Exception:                                       # noqa: BLE001
            pass
        self.destroy()

    def _on_skip(self) -> None:
        if self.on_skip:
            self.on_skip(self.info.version)
        self._finish()

    def _on_later(self) -> None:
        self._finish()

    # ------------------------------------------------------------------
    def _start_download(self) -> None:
        self._downloading = True
        self.btn_update.configure(state="disabled")
        self.btn_skip.configure(state="disabled")
        self.btn_later.configure(text="Скрыть", command=self._on_close)
        self.prog_frame.pack(fill="x", before=self.btns)

        def progress(d: dict) -> None:
            self.after(0, lambda d=d: self._progress_ui(d))

        def worker() -> None:
            try:
                staged = self.updater.download(self.info, progress)
                if self._closed:
                    return
                self.after(0, lambda: self._download_done(staged))
            except UpdateError as e:
                if self._closed:
                    return
                self.after(0, lambda e=e: self._download_error(str(e)))
            except Exception:                                   # noqa: BLE001
                if self._closed:
                    return
                self.after(0, lambda: self._download_error("Непредвиденная ошибка"))

        threading.Thread(target=worker, daemon=True,
                         name="selfupdate-download").start()

    def _progress_ui(self, d: dict) -> None:
        if self._closed:
            return
        try:
            self.bar.set(min(1.0, d.get("percent", 0.0) / 100.0))
            speed = d.get("speed_bps") or 0.0
            self.lbl_prog.configure(
                text=f"{_fmt_size(d.get('done', 0))} / {_fmt_size(d.get('total', 0))}"
                     f"   —   {_fmt_size(speed)}/с")
        except Exception:                                       # noqa: BLE001
            pass

    def _download_error(self, msg: str) -> None:
        self._downloading = False
        messagebox.showerror(
            "Ошибка обновления",
            f"Не удалось скачать обновление:\n{msg}\n\n"
            "Текущая версия продолжает работать.", parent=self)
        try:
            self.btn_update.configure(state="normal")
            self.btn_skip.configure(state="normal" if not self.info.critical
                                    else "disabled")
            self.btn_later.configure(text="Позже", command=self._on_later)
            self.prog_frame.pack_forget()
            self.bar.set(0)
        except Exception:                                       # noqa: BLE001
            pass

    def _download_done(self, staged) -> None:
        self._downloading = False
        self.bar.set(1.0)
        self.lbl_prog.configure(text="Загрузка завершена, целостность проверена")
        self.btn_later.configure(text="Готово", state="disabled")
        self.btn_update.configure(text="Установить сейчас", state="normal",
                                  command=lambda: self._install_now(staged))
        # Короткая пауза, затем установка: окончательное подтверждение
        # задаёт главное окно (сейчас или при выходе).
        self.after(1200, lambda: self._install_now(staged)
                   if not self._closed else None)

    def _install_now(self, staged) -> None:
        if self.on_ready:
            self._closed = True
            try:
                self.grab_release()
                self.destroy()
            except Exception:                                   # noqa: BLE001
                pass
            self.on_ready(self.info, staged)
        else:
            self.updater.apply(self.info, staged)
