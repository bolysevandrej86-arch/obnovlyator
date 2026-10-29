# -*- coding: utf-8 -*-
"""Обновлятор — графический интерфейс (CustomTkinter).

Все обращения к движку — только через очередь событий: движок работает
в фоновом потоке, UI-поток лишь рисует. Ни одна ошибка фоновых задач
не может уронить окно.
"""
from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core import (APP_NAME, APP_VERSION, BASE_DIR, Config, Engine, NoSleep,
                  PackageUpdate, VpnManager, autostart_enabled, enough_space,
                  explain_error, export_settings, import_settings, log,
                  release_single_instance, run_cmd, set_autostart, winget_exe)
import cleanup as cleanup_mod
import bloat as bloat_mod
import history as history_mod
import tray as tray_mod

# Цвета задаются парами (для светлой темы, для тёмной) — CTk сам подставляет
# нужный и перерисовывает виджеты при смене темы.
ACCENT = "#3b82f6"
GREEN = "#16a34a"
RED = "#dc2626"
PURPLE = "#7c3aed"

SIDEBAR_BG = ("#e9e9ef", "#1b1b20")
CARD_BG = ("#f6f6fa", "#232329")
PILL_BG = ("#dcdce3", "#26262c")
SEP_COLOR = ("#c9c9d1", "#2e2e35")
BTN_GRAY = ("#a9a9ba", "#2e2e35")
BTN_GRAY_HOVER = ("#9696a9", "#3f3f46")
BTN_BORDER = ("#8e8ea0", "#3a3a42")
BTN_TEXT = ("#20202a", "#e4e4e7")
NAV_TEXT = ("#2a2a31", "#e4e4e7")
SRC_BADGE_BG = ("#d2d2da", "#3f3f46")
SRC_BADGE_FG = ("#3a3a42", "#d4d4d8")
MUTED = ("#5b5b66", "#8b8b94")
FAINT = ("#7b7b85", "#6b6b74")
VER_FG = ("#3f3f46", "#c4c4cc")
LOG_BG = ("#ffffff", "#191920")
LOG_FG = ("#3a3a42", "#d4d4d8")

STATUS_STYLE = {
    "idle": ("ожидание", ("#d4d4da", "#3f3f46"), ("#3a3a42", "#e4e4e7")),
    "run": ("обновляется", ("#93c5fd", "#1d4ed8"), ("#1e3a8a", "#eff6ff")),
    "ok": ("готово", ("#86efac", "#166534"), ("#14532d", "#dcfce7")),
    "error": ("ошибка", ("#fca5a5", "#991b1b"), ("#7f1d1d", "#fee2e2")),
    "skip": ("пропуск", ("#fcd34d", "#92400e"), ("#78350f", "#fef3c7")),
    "failed_before": ("ошибка ранее", ("#fcd34d", "#92400e"), ("#78350f", "#fef3c7")),
    "hidden": ("скрыто", ("#c7d2fe", "#3730a3"), ("#312e81", "#e0e7ff")),
    "cancelled": ("отменено", ("#d4d4da", "#52525b"), ("#3a3a42", "#e4e4e7")),
}
LOG_COLORS = {
    "info": ("#3a3a42", "#d4d4d8"),
    "ok": ("#15803d", "#4ade80"),
    "warning": ("#b45309", "#fbbf24"),
    "error": ("#b91c1c", "#f87171"),
    "debug": ("#8b8b94", "#71717a"),
}


class PackageCard(ctk.CTkFrame):
    """Карточка одного обновления: чекбокс, название, версии, бейджи, статус."""

    def __init__(self, master, pkg: PackageUpdate, app: "App"):
        super().__init__(master, corner_radius=10, fg_color=CARD_BG)
        self.pkg = pkg
        self.app = app

        self.grid_columnconfigure(1, weight=1)
        self.var = tk.BooleanVar(value=pkg.key in app.selected)
        self.var.trace_add("write", self._on_check)

        chk = ctk.CTkCheckBox(self, text="", variable=self.var, width=26,
                              checkbox_width=20, checkbox_height=20, corner_radius=6)
        chk.grid(row=0, column=0, rowspan=2, padx=(12, 6), pady=10)

        name = ctk.CTkLabel(self, text=pkg.name, anchor="w",
                            font=ctk.CTkFont(size=13, weight="bold"))
        name.grid(row=0, column=1, sticky="ew")
        pid = ctk.CTkLabel(self, text=pkg.pkg_id, anchor="w",
                           font=ctk.CTkFont(size=10), text_color=MUTED)
        pid.grid(row=1, column=1, sticky="ew")

        ctk.CTkLabel(self, text=f"{pkg.version}  →  {pkg.available}",
                     font=ctk.CTkFont(family="Consolas", size=11),
                     text_color=VER_FG).grid(row=0, column=2, rowspan=2, padx=8)

        badges = ctk.CTkFrame(self, fg_color="transparent")
        badges.grid(row=0, column=3, rowspan=2, padx=4)
        ctk.CTkLabel(badges, text=pkg.source, font=ctk.CTkFont(size=10),
                     fg_color=SRC_BADGE_BG, text_color=SRC_BADGE_FG,
                     corner_radius=6).pack(side="left", padx=2)
        self.vpn_badge = ctk.CTkLabel(badges, text="VPN", font=ctk.CTkFont(size=10, weight="bold"),
                                      fg_color=PURPLE, text_color="#ede9fe", corner_radius=6)
        if app.cfg.category_of(pkg.key) == "vpn":
            self.vpn_badge.pack(side="left", padx=2)

        self.status_lbl = ctk.CTkLabel(self, text="ожидание", width=110, corner_radius=8,
                                       font=ctk.CTkFont(size=11))
        self.status_lbl.grid(row=0, column=4, rowspan=2, padx=(4, 12))
        self.set_status("idle")

        widgets = [self, chk, name, pid, self.status_lbl]
        widgets += list(badges.winfo_children())
        for w in widgets:
            w.bind("<Button-3>", self._menu)

    # ------------------------------------------------------------------
    def _on_check(self, *_):
        if self.var.get():
            self.app.selected.add(self.pkg.key)
        else:
            self.app.selected.discard(self.pkg.key)
        self.app.update_counts()

    def set_status(self, status: str) -> None:
        text, bg, fg = STATUS_STYLE.get(status, STATUS_STYLE["idle"])
        self.status_lbl.configure(text=text, fg_color=bg, text_color=fg)

    def set_vpn_category(self, is_vpn: bool) -> None:
        if is_vpn:
            self.vpn_badge.pack(side="left", padx=2)
        else:
            self.vpn_badge.pack_forget()

    def _menu(self, event):
        self.app.show_pkg_menu(self.pkg, event)
        return "break"


class CleanupCard(ctk.CTkFrame):
    """Карточка одной цели очистки: чекбокс, описание, размер."""

    def __init__(self, master, target, app: "App"):
        super().__init__(master, corner_radius=10, fg_color=CARD_BG)
        self.target = target
        self.app = app
        self.var = tk.BooleanVar(value=True)
        self.var.trace_add("write", lambda *_: app.update_cleanup_total())

        self.grid_columnconfigure(1, weight=1)
        ctk.CTkCheckBox(self, text="", variable=self.var, width=26,
                        checkbox_width=20, checkbox_height=20,
                        corner_radius=6).grid(row=0, column=0, rowspan=2,
                                              padx=(12, 6), pady=10)
        ctk.CTkLabel(self, text=target.name, anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=1, sticky="ew")
        ctk.CTkLabel(self, text=target.desc, anchor="w", wraplength=380, justify="left",
                     font=ctk.CTkFont(size=10), text_color=MUTED).grid(row=1, column=1, sticky="ew")
        self.size_lbl = ctk.CTkLabel(self, text="—", width=90, anchor="e",
                                     font=ctk.CTkFont(family="Consolas", size=12),
                                     text_color=VER_FG)
        self.size_lbl.grid(row=0, column=2, rowspan=2, padx=(6, 12))
        if target.admin_only:
            ctk.CTkLabel(self, text="admin", font=ctk.CTkFont(size=9, weight="bold"),
                         fg_color=("#b45309", "#92400e"), text_color="#fef3c7",
                         corner_radius=6).grid(row=0, column=3, padx=(0, 12), sticky="n")

    def set_size(self, size) -> None:
        self.size_lbl.configure(text="—" if size is None else cleanup_mod.fmt_size(size))


class ReportRow(ctk.CTkFrame):
    """Строка отчёта в разделе Очистки (корзина, старые загрузки) — только просмотр."""

    def __init__(self, master, rid: str, title: str, subtitle: str, buttons: list):
        super().__init__(master, corner_radius=10, fg_color=CARD_BG)
        self.rid = rid
        self.grid_columnconfigure(0, weight=1)
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=0, column=0, sticky="ew", padx=(12, 6), pady=10)
        ctk.CTkLabel(left, text=title, anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x")
        ctk.CTkLabel(left, text=subtitle, anchor="w", font=ctk.CTkFont(size=10),
                     text_color=MUTED).pack(fill="x")
        self.value_lbl = ctk.CTkLabel(self, text="—", width=120, anchor="e",
                                      font=ctk.CTkFont(family="Consolas", size=12),
                                      text_color=VER_FG)
        self.value_lbl.grid(row=0, column=1, rowspan=2, padx=4)
        col = 2
        for text, command, colors in buttons:
            ctk.CTkButton(self, text=text, width=110, height=30,
                          fg_color=colors[0], hover_color=colors[1],
                          command=command).grid(row=0, column=col, padx=4)
            col += 1

    def set_value(self, text: str) -> None:
        self.value_lbl.configure(text=text)


class AppxCard(ctk.CTkFrame):
    """Магазинное (UWP) приложение-кандидат на удаление."""

    def __init__(self, master, info: dict, app: "App"):
        super().__init__(master, corner_radius=10, fg_color=CARD_BG)
        self.info = info
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=info["name"], anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=(12, 6), pady=10)
        ctk.CTkLabel(self, text=info["reason"], font=ctk.CTkFont(size=10),
                     fg_color=("#fcd34d", "#92400e"),
                     text_color=("#78350f", "#fef3c7"),
                     corner_radius=6, wraplength=200).grid(row=0, column=1, padx=4)
        self.state_lbl = ctk.CTkLabel(self, text="", width=92, corner_radius=8,
                                      font=ctk.CTkFont(size=11))
        self.state_lbl.grid(row=0, column=2, padx=4)
        ctk.CTkButton(self, text="🗑  Удалить", width=96, height=30,
                      fg_color=("#dc2626", "#7f1d1d"), hover_color=("#b91c1c", "#991b1b"),
                      command=self._uninstall).grid(row=0, column=3, padx=(4, 12))

    def _uninstall(self):
        self.app.on_uninstall_appx(self)

    def set_state(self, state: str) -> None:
        styles = {"run": ("удаляется…", ("#93c5fd", "#1d4ed8"), ("#1e3a8a", "#eff6ff")),
                  "ok": ("удалено", ("#86efac", "#166534"), ("#14532d", "#dcfce7")),
                  "error": ("ошибка", ("#fca5a5", "#991b1b"), ("#7f1d1d", "#fee2e2"))}
        text, bg, fg = styles[state]
        self.state_lbl.configure(text=text, fg_color=bg, text_color=fg)


TASK_STATE_RU = {"Ready": "Готова", "Running": "Выполняется", "Disabled": "Отключена"}


class TaskRow(ctk.CTkFrame):
    """Задача планировщика вне \\Microsoft\\ с кнопкой включения/отключения."""

    def __init__(self, master, task: dict, app: "App"):
        super().__init__(master, corner_radius=10, fg_color=CARD_BG)
        self.task = task
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=task["name"], anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=(12, 6), pady=(10, 0))
        if (task["path"] or "").strip() not in ("", "\\"):
            ctk.CTkLabel(self, text=task["path"], anchor="w",
                         font=ctk.CTkFont(size=10), text_color=MUTED).grid(
                row=1, column=0, sticky="ew", padx=(12, 6), pady=(0, 10))
        state_colors = {"Ready": ("#86efac", "#166534"), "Running": ("#93c5fd", "#1d4ed8")}
        enabled = task["state"] != "Disabled"
        self.state_lbl = ctk.CTkLabel(
            self, text=TASK_STATE_RU.get(task["state"], task["state"]),
            width=110, corner_radius=8, font=ctk.CTkFont(size=11),
            fg_color=state_colors.get(task["state"], ("#d4d4da", "#52525b")),
            text_color=("#3a3a42", "#e4e4e7"))
        self.state_lbl.grid(row=0, column=1, rowspan=2, padx=4)
        self.toggle_btn = ctk.CTkButton(
            self, text="Отключить" if enabled else "Включить", width=100, height=30,
            fg_color=BTN_GRAY, hover_color=BTN_GRAY_HOVER,
            border_width=1, border_color=BTN_BORDER, text_color=BTN_TEXT,
            command=self._toggle)
        self.toggle_btn.grid(row=0, column=2, rowspan=2, padx=(4, 12))

    def _toggle(self):
        enable = self.task["state"] == "Disabled"
        self.toggle_btn.configure(state="disabled")
        self.app.on_toggle_task(self, enable)

    def set_state(self, state: str) -> None:
        self.task["state"] = state
        enabled = state != "Disabled"
        self.state_lbl.configure(
            text=TASK_STATE_RU.get(state, state),
            fg_color=("#86efac", "#166534") if enabled else ("#d4d4da", "#52525b"))
        self.toggle_btn.configure(text="Отключить" if enabled else "Включить",
                                  state="normal")


class ResidualRow(ctk.CTkFrame):
    """Возможный «хвост» удалённой программы: папка без владельца."""

    def __init__(self, master, info: dict, app: "App"):
        super().__init__(master, corner_radius=10, fg_color=CARD_BG)
        self.info = info
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=info["name"], anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=(12, 6), pady=(10, 0))
        ctk.CTkLabel(self, text=info["path"], anchor="w", wraplength=520, justify="left",
                     font=ctk.CTkFont(size=10), text_color=MUTED).grid(
            row=1, column=0, sticky="ew", padx=(12, 6), pady=(0, 10))
        self.size_lbl = ctk.CTkLabel(self, text="—", width=90, anchor="e",
                                     font=ctk.CTkFont(family="Consolas", size=11),
                                     text_color=VER_FG)
        self.size_lbl.grid(row=0, column=1, rowspan=2, padx=4)
        ctk.CTkButton(self, text="📂", width=36, height=30, fg_color=BTN_GRAY,
                      hover_color=BTN_GRAY_HOVER, text_color=BTN_TEXT,
                      command=lambda: bloat_mod.open_in_explorer(info["path"])
                      ).grid(row=0, column=2, rowspan=2, padx=2)
        ctk.CTkButton(self, text="🗑", width=36, height=30, fg_color=("#dc2626", "#7f1d1d"),
                      hover_color=("#b91c1c", "#991b1b"),
                      command=self._delete).grid(row=0, column=3, rowspan=2, padx=(2, 12))

    def set_size(self, size) -> None:
        self.size_lbl.configure(text="—" if size is None else cleanup_mod.fmt_size(size))

    def _delete(self):
        self.app.on_delete_residual(self)


