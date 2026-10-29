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
import diag as diag_mod
import history as history_mod
import selfupdate
import selfupdate_ui
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


class HistoryChart(ctk.CTkLabel):
    """Сглаженный столбчатый график «по дням».

    tk.Canvas не умеет антиалиасинг — на нём всё в «пикселях». Поэтому
    рисуем в PIL с трёхкратным суперсэмплингом и показываем как CTkImage:
    линии и скруглённые столбцы получаются гладкими, шрифт — чётким.
    """

    SS = 3  # суперсэмплинг

    def __init__(self, master, height: int = 130, **kw) -> None:
        super().__init__(master, text="", height=height, **kw)
        self._series: list[tuple[str, int]] = []
        self._suffix = ""
        self._last_size = (0, 0)
        self._resize_after = None
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, _e=None) -> None:
        """Ресайз окна генерирует десятки событий Configure подряд —
        PIL-рендер на каждый из них даёт видимые лаги, поэтому с дебаунсом."""
        if self._resize_after:
            self.after_cancel(self._resize_after)
        self._resize_after = self.after(120, self._resize_now)

    def _resize_now(self) -> None:
        self._resize_after = None
        self._last_size = (0, 0)   # размер точно менялся — рисуем заново
        self.redraw()

    def show(self, series: list[tuple[str, int]], suffix: str = "") -> None:
        self._series = series
        self._suffix = suffix
        self.redraw(force=True)

    # ------------------------------------------------------------------
    def redraw(self, force: bool = False) -> None:
        try:
            self._render(force)
        except Exception:
            pass  # график не должен ронять интерфейс

    def _render(self, force: bool = False) -> None:
        from PIL import Image, ImageDraw, ImageFont

        w = max(self.winfo_width(), 10)
        h = max(self.winfo_height(), 10)
        if not force and (w, h) == self._last_size:
            return  # размер не менялся — не перерисовываем
        self._last_size = (w, h)

        dark = str(ctk.get_appearance_mode()).lower() == "dark"
        fg = "#e4e4e7" if dark else "#3a3a42"
        mut = "#8b8b94" if dark else "#7b7b85"
        grid = (70, 80, 100, 90) if dark else (180, 184, 194, 160)
        bar_top, bar_bottom = (96, 165, 250), (37, 99, 235)
        bg = (0, 0, 0, 0)
        ss = self.SS
        W, H = w * ss, h * ss

        img = Image.new("RGBA", (W, H), bg)
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("segoeui.ttf", 9 * ss)
        except OSError:
            font = ImageFont.load_default()

        pad_l, pad_r, pad_t, pad_b = 30 * ss, 8 * ss, 10 * ss, 20 * ss
        series = self._series
        plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b

        # сетка + подписи оси Y
        maxv = max((v for _, v in series), default=0) or 1
        for i in range(1, 4):
            y = pad_t + plot_h * (1 - i / 4)
            d.line([(pad_l, y), (W - pad_r, y)], fill=grid, width=ss)
            d.text((pad_l - 6 * ss, y), str(round(maxv * i / 4)), fill=mut,
                   font=font, anchor="rm")

        n = len(series)
        if n:
            slot = plot_w / max(n, 1)
            bw = min(18 * ss, max(4 * ss, slot * 0.55))
            radius = min(3 * ss, bw // 2)
            for i, (label, v) in enumerate(series):
                x = pad_l + slot * i + slot / 2
                if v > 0:
                    bh = max(2 * ss, plot_h * (v / maxv))
                    top = H - pad_b - bh
                    # столбец с мягким градиентом сверху вниз
                    bar_img = Image.new("RGBA", (int(bw), int(bh)))
                    bd = ImageDraw.Draw(bar_img)
                    for yy in range(int(bh)):
                        t = yy / max(bh - 1, 1)
                        color = tuple(
                            int(bar_top[c] + (bar_bottom[c] - bar_top[c]) * t)
                            for c in range(3)) + (255,)
                        bd.line([(0, yy), (int(bw), yy)], fill=color)
                    mask = Image.new("L", (int(bw), int(bh)), 0)
                    md = ImageDraw.Draw(mask)
                    md.rounded_rectangle([0, 0, int(bw) - 1, int(bh) - 1],
                                         radius=radius, fill=255)
                    img.paste(bar_img, (int(x - bw / 2), int(top)), mask)
                    if n <= 14 or i % 2 == 0:
                        d.text((x, top - 6 * ss), f"{v}{self._suffix}",
                               fill=fg, font=font, anchor="ms")
                if n <= 16 or i % 2 == 0:
                    d.text((x, H - pad_b + 4 * ss), label, fill=mut,
                           font=font, anchor="ma")

        # ось X
        d.line([(pad_l, H - pad_b), (W - pad_r, H - pad_b)], fill=grid, width=ss)

        img = img.resize((w, h), Image.LANCZOS)
        self.configure(image=ctk.CTkImage(light_image=img, dark_image=img,
                                          size=(w, h)), text="")




class SettingsWindow(ctk.CTkToplevel):
    """Окно настроек: VPN, автообновление, автозапуск, параметры winget."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.cfg = app.cfg
        self.title("Настройки — Обновлятор")
        self.geometry("660x700")
        self.transient(app)
        self.after(150, self.grab_set)

        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=14, pady=(14, 0))

        # --- VPN --------------------------------------------------------
        self._section(scroll, "VPN")
        self.e_proc = self._row(scroll, "Процессы VPN-клиентов (через запятую)",
                                ", ".join(self.cfg.vpn_processes))
        self.e_adapter = self._row(scroll, "Имена/части имён адаптеров (через запятую)",
                                   ", ".join(self.cfg.vpn_adapters))
        self.e_connect = self._row(scroll, "Команда подключения VPN (запускается как cmd)",
                                   self.cfg.vpn_connect_cmd,
                                   hint="например: \"C:\\VPN\\client.exe\" /connect")
        self.e_disconnect = self._row(scroll, "Команда отключения VPN",
                                      self.cfg.vpn_disconnect_cmd)
        self.e_ipurl = self._row(scroll, "URL проверки страны по IP (необязательно)",
                                 self.cfg.vpn_ip_url, hint="например: https://ipinfo.io/json (только https)")
        self.e_country = self._row(scroll, "Домашняя страна (код, напр. RU)",
                                   self.cfg.vpn_home_country)
        self.sw_disc_after = self._switch(scroll, "Отключать VPN, если подключали его сами",
                                          self.cfg.vpn_disconnect_after)

        # --- Обновления --------------------------------------------------
        self._section(scroll, "Обновления")
        self.e_timeout = self._row(scroll, "Таймаут одного пакета, секунд",
                                   str(self.cfg.timeout_sec))
        self.e_retries = self._row(scroll, "Повторных попыток после ошибки",
                                   str(self.cfg.retries))
        self.e_extra = self._row(scroll, "Доп. аргументы winget",
                                 self.cfg.winget_extra_args, hint="например: --scope user")
        self.sw_pip = self._switch(scroll, "Также обновлять Python-пакеты (pip)",
                                   self.cfg.update_pip)
        self.sw_npm = self._switch(scroll, "Также обновлять глобальные npm-пакеты (Node.js)",
                                   self.cfg.update_npm)
        self.e_interval = self._row(scroll, "Автопроверка каждые N часов (0 — выключена)",
                                    str(self.cfg.auto_interval_hours))
        self.sw_autoall = self._switch(scroll, "При автопроверке сразу обновлять всё",
                                       self.cfg.auto_update_all)
        self.sw_restore = self._switch(
            scroll, "Точка восстановления перед обновлением (нужны права администратора)",
            self.cfg.restore_point)
        self.sw_killswitch = self._switch(
            scroll, "Прерывать загрузку VPN-пакета, если VPN отключился",
            self.cfg.vpn_kill_switch)
        self.sw_autostart = self._switch(scroll, "Запускать приложение при входе в Windows",
                                         autostart_enabled())

        # --- Проверка VPN -------------------------------------------------
        self._section(scroll, "Проверка VPN")
        row = ctk.CTkFrame(scroll, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkButton(row, text="Проверить статус VPN", width=180,
                      command=self._check_vpn).pack(side="left")
        self.vpn_result = ctk.CTkLabel(row, text="", anchor="w",
                                       font=ctk.CTkFont(size=11), text_color=MUTED)
        self.vpn_result.pack(side="left", padx=10, fill="x", expand=True)

        # --- Окно и тема ----------------------------------------------------
        self._section(scroll, "Окно и тема")
        theme_row = ctk.CTkFrame(scroll, fg_color="transparent")
        theme_row.pack(fill="x", pady=2)
        ctk.CTkLabel(theme_row, text="Тема", font=ctk.CTkFont(size=11),
                     text_color=MUTED).pack(side="left", padx=(0, 10))
        self.theme_menu = ctk.CTkOptionMenu(
            theme_row, width=170, values=["Тёмная", "Светлая", "Как в системе"],
            command=lambda v: self.app.apply_theme_mode(
                {"Тёмная": "dark", "Светлая": "light", "Как в системе": "system"}[v]))
        self.theme_menu.set({"dark": "Тёмная", "light": "Светлая",
                             "system": "Как в системе"}[self.cfg.theme_mode])
        self.theme_menu.pack(side="left")
        self.sw_tray = self._switch(scroll, "Сворачивать в трей при закрытии окна "
                                           "(обновления продолжаются)",
                                    self.cfg.minimize_to_tray)
        self.sw_notify = self._switch(scroll, "Всплывающие уведомления Windows",
                                      self.cfg.notify_enabled)

        # --- Самообновление Обновлятора -----------------------------------
        self._section(scroll, "Самообновление Обновлятора")
        self.sw_selfupd = self._switch(
            scroll, "Проверять обновления самого Обновлятора",
            self.cfg.selfupdate_enabled)
        ch_row = ctk.CTkFrame(scroll, fg_color="transparent")
        ch_row.pack(fill="x", pady=2)
        ctk.CTkLabel(ch_row, text="Канал обновлений", font=ctk.CTkFont(size=11),
                     text_color=MUTED).pack(side="left", padx=(0, 10))
        self.channel_menu = ctk.CTkOptionMenu(
            ch_row, width=220,
            values=["stable", "beta", "nightly"])
        self.channel_menu.set(self.cfg.update_channel
                              if self.cfg.update_channel in ("stable", "beta", "nightly")
                              else "stable")
        self.channel_menu.pack(side="left")
        self.e_selfupd_hours = self._row(
            scroll, "Фоновая проверка каждые N часов (0 — только вручную)",
            str(self.cfg.selfupdate_check_hours))
        # Подсказка о текущем состоянии подключения репозитория:
        if selfupdate.SelfUpdater("", repo=selfupdate.SELFUPDATE_REPO,
                                  public_key=selfupdate.SELFUPDATE_PUBLIC_KEY).configured:
            hint = (f"Источник обновлений: github.com/{selfupdate.SELFUPDATE_REPO} "
                    "(настроен в selfupdate.py)")
            hint_color = GREEN
        else:
            hint = ("⚠ Источник обновлений пока не настроен: заполните "
                    "SELFUPDATE_REPO в selfupdate.py (см. комментарий вверху файла)")
            hint_color = ("#b45309", "#fbbf24")
        ctk.CTkLabel(scroll, text=hint, anchor="w", wraplength=560, justify="left",
                     font=ctk.CTkFont(size=10), text_color=hint_color).pack(fill="x", pady=(2, 0))

        # --- Правила пакетов -----------------------------------------------
        self._section(scroll, "Правила пакетов")
        self.e_blocked = self._row(
            scroll, "Чёрный список (никогда не обновлять, через запятую)",
            ", ".join(self.cfg.blocked_packages),
            hint="например: numpy, nodejs — критичные для работы пакеты")
        self.e_pins = self._row(
            scroll, "Закрепление версий (формат: имя=версия, через запятую)",
            ", ".join(f"{k}={v}" for k, v in self.cfg.pinned_packages.items()),
            hint="например: numpy=1.26.4 — пакет держится на этой версии")
        self.e_mingb = self._row(
            scroll, "Не начинать обновление/очистку, если на диске меньше (ГБ)",
            str(self.cfg.min_free_gb))

        # --- Перенос настроек ------------------------------------------------
        self._section(scroll, "Экспорт/импорт настроек")
        st_row = ctk.CTkFrame(scroll, fg_color="transparent")
        st_row.pack(fill="x", pady=2)
        ctk.CTkButton(st_row, text="📤  Экспортировать настройки", width=210,
                      command=self._export_settings).pack(side="left")
        ctk.CTkButton(st_row, text="📥  Импортировать настройки", width=210,
                      command=self._import_settings).pack(side="left", padx=8)

        # --- Кнопки -------------------------------------------------------
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=14, pady=12)
        self.err_lbl = ctk.CTkLabel(bottom, text="", text_color=("#b91c1c", "#f87171"))
        self.err_lbl.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(bottom, text="Отмена", width=100, fg_color=BTN_GRAY,
                      hover_color=BTN_GRAY_HOVER,
                      command=self.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bottom, text="Сохранить", width=130,
                      command=self._save).pack(side="right")

    # ------------------------------------------------------------------
    def _section(self, master, title: str) -> None:
        ctk.CTkLabel(master, text=title, font=ctk.CTkFont(size=14, weight="bold"),
                     anchor="w").pack(fill="x", pady=(14, 4))

    def _row(self, master, label: str, value: str, hint: str | None = None) -> ctk.CTkEntry:
        ctk.CTkLabel(master, text=label, anchor="w",
                     font=ctk.CTkFont(size=11), text_color=MUTED).pack(fill="x", pady=(6, 1))
        entry = ctk.CTkEntry(master, width=400)
        entry.pack(fill="x")
        if value:
            entry.insert(0, value)
        if hint:
            ctk.CTkLabel(master, text=hint, anchor="w", font=ctk.CTkFont(size=10),
                         text_color=FAINT).pack(fill="x")
        return entry

    def _switch(self, master, label: str, value: bool) -> ctk.CTkSwitch:
        var = tk.BooleanVar(value=value)
        switch = ctk.CTkSwitch(master, text=label, variable=var, onvalue=True, offvalue=False)
        switch.pack(fill="x", pady=6)
        switch.var = var
        return switch

    def _entry(self, e: ctk.CTkEntry) -> str:
        return e.get().strip()

    def _int(self, e: ctk.CTkEntry, default: int, minimum: int) -> int:
        try:
            return max(minimum, int(float(self._entry(e).replace(",", "."))))
        except ValueError:
            return default

    def _float(self, e: ctk.CTkEntry, default: float, minimum: float) -> float:
        try:
            return max(minimum, float(self._entry(e).replace(",", ".")))
        except ValueError:
            return default

    def _check_vpn(self) -> None:
        self.vpn_result.configure(text="проверяю…", text_color=MUTED)

        def work():
            active, reason = VpnManager(self.cfg).status()
            text = {True: "активен", False: "не активен", None: "неопределён"}[active]
            color = {True: ("#15803d", "#4ade80"),
                     False: MUTED,
                     None: ("#b45309", "#fbbf24")}[active]
            self.after(0, lambda: self.vpn_result.configure(text=f"{text} — {reason}", text_color=color))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    def _export_settings(self) -> None:
        """Сохранить все настройки в JSON — для переноса на другой ПК."""
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".json",
            initialfile=f"obnovlyator_settings_{time.strftime('%Y%m%d')}.json",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            export_settings(self.cfg, path)
            self.vpn_result.configure(text=f"Настройки сохранены: {path}",
                                      text_color=("#15803d", "#4ade80"))
        except OSError as e:
            messagebox.showerror("Экспорт настроек", f"Не удалось сохранить:\n{e}",
                                 parent=self)

    def _import_settings(self) -> None:
        """Загрузить настройки из JSON. Применяются сразу; часть — после перезапуска."""
        path = filedialog.askopenfilename(
            parent=self, filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            new_cfg = import_settings(path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            messagebox.showerror("Импорт настроек",
                                 f"Файл повреждён или не читается:\n{e}", parent=self)
            return
        # Переносим поля в текущий конфиг и сохраняем
        from dataclasses import fields as dfields
        for f in dfields(Config):
            setattr(self.cfg, f.name, getattr(new_cfg, f.name))
        self.cfg.save()
        self.app.apply_theme_mode(self.cfg.theme_mode)
        if self.cfg.start_with_windows != autostart_enabled():
            set_autostart(self.cfg.start_with_windows)
        self.vpn_result.configure(text="Настройки импортированы и применены "
                                      "(часть — после перезапуска)",
                                  text_color=("#15803d", "#4ade80"))

    def destroy(self):
        self.app._settings_win = None
        super().destroy()

    def _save(self) -> None:
        cfg = self.cfg
        cfg.vpn_processes = [x for x in self._entry(self.e_proc).replace(";", ",").split(",") if x.strip()]
        cfg.vpn_adapters = [x for x in self._entry(self.e_adapter).replace(";", ",").split(",") if x.strip()]
        cfg.vpn_connect_cmd = self._entry(self.e_connect)
        cfg.vpn_disconnect_cmd = self._entry(self.e_disconnect)
        cfg.vpn_ip_url = self._entry(self.e_ipurl)
        cfg.vpn_home_country = self._entry(self.e_country).upper()
        cfg.vpn_disconnect_after = bool(self.sw_disc_after.var.get())
        cfg.timeout_sec = self._int(self.e_timeout, 1800, 60)
        cfg.retries = self._int(self.e_retries, 1, 0)
        cfg.winget_extra_args = self._entry(self.e_extra)
        cfg.update_pip = bool(self.sw_pip.var.get())
        cfg.update_npm = bool(self.sw_npm.var.get())
        cfg.auto_interval_hours = self._float(self.e_interval, 0.0, 0.0)
        cfg.auto_update_all = bool(self.sw_autoall.var.get())
        cfg.restore_point = bool(self.sw_restore.var.get())
        cfg.vpn_kill_switch = bool(self.sw_killswitch.var.get())
        cfg.start_with_windows = bool(self.sw_autostart.var.get())
        cfg.minimize_to_tray = bool(self.sw_tray.var.get())
        cfg.notify_enabled = bool(self.sw_notify.var.get())

        # самообновление приложения
        cfg.selfupdate_enabled = bool(self.sw_selfupd.var.get())
        channel = self.channel_menu.get()
        cfg.update_channel = channel if channel in ("stable", "beta", "nightly") else "stable"
        cfg.selfupdate_check_hours = min(168.0, self._float(self.e_selfupd_hours, 6.0, 0.0))

        # правила пакетов
        cfg.blocked_packages = [x.strip() for x in
                                self._entry(self.e_blocked).replace(";", ",").split(",")
                                if x.strip()]
        pins = {}
        for chunk in self._entry(self.e_pins).replace(";", ",").split(","):
            chunk = chunk.strip()
            if "=" in chunk:
                name, _, ver = chunk.partition("=")
                if name.strip() and ver.strip():
                    pins[name.strip()] = ver.strip()
        cfg.pinned_packages = pins
        cfg.min_free_gb = min(200.0, self._float(self.e_mingb, 2.0, 0.0))

        if cfg.minimize_to_tray and getattr(self.app, "_tray_icon", None) is None:
            tray_mod.start_tray(self.app)
        elif not cfg.minimize_to_tray and getattr(self.app, "_tray_icon", None) is not None:
            tray_mod.stop_tray(self.app)

        if cfg.start_with_windows and not set_autostart(True):
            self.err_lbl.configure(text="Не удалось включить автозапуск (реестр недоступен)")
            return
        if not cfg.start_with_windows and autostart_enabled() and not set_autostart(False):
            self.err_lbl.configure(text="Не удалось отключить автозапуск (реестр недоступен)")
            return

        cfg.save()
        self.app.log("Настройки сохранены", "ok")
        self.destroy()


def _force_segoe_font() -> None:
    """Тема CTk ссылается на шрифт Roboto, которого в Windows нет — Tk
    подставляет что придётся, текст выглядит рыхло. Принудительно ставим
    системный Segoe UI до создания любых виджетов."""
    try:
        block = ctk.ThemeManager.theme.get("CTkFont", {})
        if isinstance(block, dict):
            if isinstance(block.get("family"), str):
                block["family"] = "Segoe UI"
            for value in block.values():
                if isinstance(value, dict) and isinstance(value.get("family"), str):
                    value["family"] = "Segoe UI"
    except Exception:
        pass


class App(ctk.CTk):
    def __init__(self, is_admin: bool = True):
        super().__init__()
        self.cfg = Config.load()
        ctk.set_appearance_mode("system" if self.cfg.theme_mode == "system"
                                else self.cfg.theme_mode)
        ctk.set_default_color_theme("blue")
        _force_segoe_font()

        self.title(f"{APP_NAME} — автообновление программ")
        self.geometry("1150x740")
        self.minsize(960, 600)

        self.q: queue.Queue = queue.Queue()
        self.engine = Engine(self.cfg, self.q)
        self.is_admin = is_admin
        self.cards: dict[str, PackageCard] = {}
        self.selected: set[str] = set()
        self.hidden_keys: set[str] = set()
        self._closing = False
        self._auto_stop = threading.Event()
        self._auto_pending = False
        self._current_view = "updates"
        self._tool_busy = False          # занят ли раздел Очистки/Мусора
        self._theme_busy = False
        self._theme_pending = None
        self._search_after_id = None
        self._tags_mode = None           # тема, под которую окрашены теги журнала
        self.cleanup_cards: dict[str, CleanupCard] = {}
        self._cleanup_sizes: dict[str, int | None] = {}
        self.bloat_cards: dict[str, BloatCard] = {}
        self.appx_cards: list[AppxCard] = []
        self.task_rows: list[TaskRow] = []
        self.residual_rows: list[ResidualRow] = []
        self.report_rows: dict[str, ReportRow] = {}
        self._tray_icon = None
        self._settings_win = None
        self._quitting = False
        self.sort_var = tk.StringVar(value="По умолчанию")

        # --- самообновление приложения (см. selfupdate.py)
        self.selfup = None                 # SelfUpdater, создаётся в _selfupdate_init
        self._selfupdate_pending = None    # (info, staged) — установить при выходе
        self._selfupdate_checking = False

        self._build()
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<F5>", lambda e: self._on_refresh())
        self.bind_all("<Control-a>", lambda e: self._on_select_all())
        self.bind_all("<Control-A>", lambda e: self._on_select_all())
        self.after(100, self._poll)
        self.after(400, self._check_vpn_async)
        self.after(1500, self._selfupdate_init)   # фиксация успеха обновления + первая проверка
        threading.Thread(target=self._auto_loop, daemon=True, name="auto").start()
        if self.cfg.minimize_to_tray:
            try:
                tray_mod.start_tray(self)
            except Exception:
                log.warning("Трей не запустился", exc_info=True)

        self.log(f"{APP_NAME} v{APP_VERSION} запущен", "ok")
        if not is_admin:
            self.log("Приложение без прав администратора: часть обновлений может "
                     "запросить UAC или завершиться ошибкой.", "warning")
        self.log("Подсказка: правый клик по пакету — назначить обновление через VPN.", "info")

    # ======================================================================
    # Построение интерфейса
    # ======================================================================
    def _build(self) -> None:
        side = ctk.CTkFrame(self, width=236, corner_radius=0, fg_color=SIDEBAR_BG)
        side.grid(row=0, column=0, sticky="nsw")
        side.pack_propagate(False)

        ctk.CTkLabel(side, text="⚡ Обновлятор",
                     font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(18, 0))
        ctk.CTkLabel(side, text="автообновление программ", font=ctk.CTkFont(size=11),
                     text_color=MUTED).pack(pady=(0, 12))

        pill = ctk.CTkFrame(side, fg_color=PILL_BG, corner_radius=8)
        pill.pack(fill="x", padx=14, pady=3)
        self.vpn_dot = ctk.CTkLabel(pill, text="●", width=16,
                                    font=ctk.CTkFont(size=14), text_color="#71717a")
        self.vpn_dot.pack(side="left", padx=(10, 4), pady=7)
        self.vpn_text = ctk.CTkLabel(pill, text="VPN: проверяю…", anchor="w",
                                     font=ctk.CTkFont(size=11), wraplength=160,
                                     justify="left")
        self.vpn_text.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=7)

        self.admin_lbl = ctk.CTkLabel(
            side,
            text="✓ права администратора" if self.is_admin else "⚠ без прав администратора",
            font=ctk.CTkFont(size=11),
            text_color=("#15803d", "#4ade80") if self.is_admin else ("#b45309", "#fbbf24"),
        )
        self.admin_lbl.pack(pady=(2, 6))

        if not self.is_admin:
            ctk.CTkButton(side, text="🔑  Перезапустить от администратора", height=30,
                          fg_color=("#b45309", "#92400e"),
                          hover_color=("#92400e", "#78350f"),
                          command=self.restart_as_admin).pack(fill="x", padx=14, pady=(0, 6))

        ctk.CTkFrame(side, height=1, fg_color=SEP_COLOR).pack(fill="x", padx=14, pady=6)

        # --- навигация по разделам ----------------------------------------
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        for view_id, label in (("updates", "📦  Обновления"),
                               ("cleanup", "🧹  Очистка"),
                               ("bloat", "🗑  Мусор"),
                               ("history", "🕘  История"),
                               ("faq", "❔  FAQ"),
                               ("checks", "🩺  Проверки")):
            btn = ctk.CTkButton(side, text=label, height=32, anchor="w",
                                fg_color="transparent",
                                text_color=NAV_TEXT,
                                hover_color=BTN_GRAY_HOVER,
                                command=lambda v=view_id: self.show_view(v))
            btn.pack(fill="x", padx=14, pady=2)
            self.nav_buttons[view_id] = btn

        ctk.CTkFrame(side, height=1, fg_color=SEP_COLOR).pack(fill="x", padx=14, pady=6)

        self.btn_scan = ctk.CTkButton(side, text="🔄  Проверить обновления", height=38,
                                      anchor="w", command=self.on_scan)
        self.btn_scan.pack(fill="x", padx=14, pady=(8, 4))
        self.btn_upd_sel = ctk.CTkButton(side, text="⬆  Обновить выбранное", height=38,
                                         anchor="w", fg_color=ACCENT, hover_color="#2563eb",
                                         state="disabled", command=self.on_update_selected)
        self.btn_upd_sel.pack(fill="x", padx=14, pady=4)
        self.btn_upd_all = ctk.CTkButton(side, text="🚀  Обновить всё", height=38, anchor="w",
                                         fg_color=GREEN, hover_color="#15803d",
                                         state="disabled", command=self.on_update_all)
        self.btn_upd_all.pack(fill="x", padx=14, pady=4)
        self.btn_cancel = ctk.CTkButton(side, text="⏹  Остановить", height=34, anchor="w",
                                        fg_color=("#dc2626", "#7f1d1d"),
                                        hover_color=("#b91c1c", "#991b1b"),
                                        state="disabled", command=self.on_cancel)
        self.btn_cancel.pack(fill="x", padx=14, pady=(4, 8))

        self.progress = ctk.CTkProgressBar(side, height=8, progress_color=ACCENT)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=14, pady=(4, 2))
        self.phase_lbl = ctk.CTkLabel(side, text="", anchor="w", justify="left",
                                      font=ctk.CTkFont(size=11), wraplength=200,
                                      text_color=MUTED)
        self.phase_lbl.pack(fill="x", padx=14, pady=(0, 10))

        ctk.CTkFrame(side, height=1, fg_color=SEP_COLOR).pack(fill="x", padx=14, pady=6)

        self.theme_sw_var = tk.BooleanVar(value=self.cfg.theme_mode == "light")
        ctk.CTkSwitch(side, text="Светлая тема", font=ctk.CTkFont(size=12),
                      variable=self.theme_sw_var,
                      command=self._toggle_theme).pack(fill="x", padx=14, pady=4)

        ctk.CTkButton(side, text="⚙  Настройки", height=32, anchor="w",
                      fg_color=BTN_GRAY, hover_color=BTN_GRAY_HOVER,
                      command=self.open_settings).pack(fill="x", padx=14, pady=(6, 4))
        self.btn_log = ctk.CTkButton(side, text="📃  Журнал", height=32, anchor="w",
                                     fg_color=BTN_GRAY, hover_color=BTN_GRAY_HOVER,
                                     command=self._toggle_log)
        self.btn_log.pack(fill="x", padx=14, pady=4)

        ctk.CTkLabel(side, text=f"v{APP_VERSION}", font=ctk.CTkFont(size=10),
                     text_color=FAINT).pack(side="bottom", pady=8)

        # --- основная область -------------------------------------------
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)

        self.views = ctk.CTkFrame(main, fg_color="transparent")
        self.views.grid(row=0, column=0, sticky="nsew")

        # --- раздел «Обновления» -----------------------------------------
        self.updates_view = ctk.CTkFrame(self.views, fg_color="transparent")
        self.updates_view.grid(row=0, column=0, sticky="nsew")
        self.updates_view.grid_rowconfigure(1, weight=1)
        self.updates_view.grid_columnconfigure(0, weight=1)

        topbar = ctk.CTkFrame(self.updates_view, fg_color="transparent")
        topbar.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        self.tabs = ctk.CTkSegmentedButton(topbar, values=["Все", "Без VPN", "С VPN"],
                                           command=self.apply_filter)
        self.tabs.set("Все")
        self.tabs.pack(side="left")
        self.count_lbl = ctk.CTkLabel(topbar, text="Нажмите «Проверить обновления»",
                                      text_color=MUTED)
        self.count_lbl.pack(side="left", padx=14)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._search_debounced)
        search = ctk.CTkEntry(topbar, width=200, placeholder_text="🔍  Поиск…",
                              textvariable=self.search_var)
        search.pack(side="right")
        ctk.CTkOptionMenu(topbar, width=130, values=["По умолчанию", "Имя А→Я", "Имя Я→А"],
                          variable=self.sort_var, command=self.apply_filter).pack(side="right", padx=8)

        self.list_frame = ctk.CTkScrollableFrame(self.updates_view, fg_color="transparent")
        self.list_frame.grid(row=1, column=0, sticky="nsew", padx=10)
        self.empty_lbl = ctk.CTkLabel(self.list_frame, text="Нажмите «Проверить обновления»",
                                      text_color=FAINT, font=ctk.CTkFont(size=14))
        self.empty_lbl.pack(pady=60)
        self.hidden_lbl = ctk.CTkLabel(self.list_frame, text="", anchor="w",
                                       text_color=MUTED, font=ctk.CTkFont(size=11))

        self._build_cleanup_view()
        self._build_bloat_view()
        self._build_history_view()
        self._build_faq_view()
        self._build_checks_view()

        self.log_frame = ctk.CTkFrame(main, height=190)
        self.log_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 12))
        self.log_frame.grid_propagate(False)
        log_header = ctk.CTkFrame(self.log_frame, fg_color="transparent")
        log_header.pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(log_header, text="Журнал", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=MUTED).pack(side="left")
        ctk.CTkButton(log_header, text="Очистить", width=80, height=24,
                      fg_color=BTN_GRAY, hover_color=BTN_GRAY_HOVER,
                      command=self._clear_log).pack(side="right")
        self.log_box = ctk.CTkTextbox(self.log_frame, font=ctk.CTkFont(family="Consolas", size=11),
                                      fg_color=LOG_BG, text_color=LOG_FG, wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_box.configure(state="disabled")
        self._log_open = True
        self._apply_log_tags()
        self.show_view("updates")

    # ======================================================================
    # Раздел «Очистка»
    # ======================================================================
    def _build_cleanup_view(self) -> None:
        self.cleanup_view = ctk.CTkFrame(self.views, fg_color="transparent")
        self.cleanup_view.grid(row=0, column=0, sticky="nsew")
        self.cleanup_view.grid_rowconfigure(1, weight=1)
        self.cleanup_view.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self.cleanup_view, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        ctk.CTkLabel(header, text="Очистка установщиков и кэшей",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        self.btn_measure = ctk.CTkButton(header, text="📏  Обновить размеры", width=170,
                                         fg_color=BTN_GRAY, hover_color=BTN_GRAY_HOVER,
                                         border_width=1, border_color=BTN_BORDER,
                                         command=self.on_cleanup_measure)
        self.btn_measure.pack(side="right")

        scroll = ctk.CTkScrollableFrame(self.cleanup_view, fg_color="transparent")
        scroll.grid(row=1, column=0, sticky="nsew", padx=10)
        for target in cleanup_mod.get_targets():
            card = CleanupCard(scroll, target, self)
            card.pack(fill="x", padx=8, pady=4)
            self.cleanup_cards[target.id] = card
            self._cleanup_sizes[target.id] = None

        ctk.CTkLabel(scroll, text="Отчёты (только просмотр; удаление — по кнопке)",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=MUTED).pack(fill="x", padx=8, pady=(14, 4))
        self.report_rows["recycle"] = ReportRow(
            scroll, "recycle", "Корзина",
            "Суммарно на всех дисках; очистка — с подтверждением",
            [("🧹  Очистить корзину", self.on_empty_recycle, ("#dc2626", "#7f1d1d"))])
        self.report_rows["recycle"].pack(fill="x", padx=8, pady=4)
        self.report_rows["downloads"] = ReportRow(
            scroll, "downloads", "Старые файлы в «Загрузках»",
            "Файлы старше 30 дней; приложение их не удаляет — только показывает",
            [("📂  Открыть папку", self.on_open_downloads, (BTN_GRAY, BTN_GRAY_HOVER))])
        self.report_rows["downloads"].pack(fill="x", padx=8, pady=4)

        bottom = ctk.CTkFrame(self.cleanup_view, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=16, pady=(6, 12))
        self.cleanup_total_lbl = ctk.CTkLabel(bottom, text="", text_color=MUTED)
        self.cleanup_total_lbl.pack(side="left")
        self.btn_cleanup_run = ctk.CTkButton(bottom, text="🧹  Очистить выбранное", width=190,
                                             fg_color=GREEN, hover_color="#15803d",
                                             command=self.on_cleanup_run)
        self.btn_cleanup_run.pack(side="right")

    def on_cleanup_measure(self) -> None:
        if self._tool_busy or self.engine.busy:
            self.log("Дождитесь завершения текущей операции", "warning")
            return
        self._tool_busy = True
        self.log("Считаю размеры кэшей…", "info")

        def work():
            for target in cleanup_mod.get_targets():
                size = cleanup_mod.measure(target, cancel_event=self._auto_stop)
                if self._closing:
                    return
                self.q.put({"type": "cleanup_size", "id": target.id, "size": size})
            self.q.put({"type": "report", "kind": "recycle",
                        "size": cleanup_mod.recycle_size(self._auto_stop)})
            count, total, _folder = cleanup_mod.downloads_report(30)
            self.q.put({"type": "report", "kind": "downloads",
                        "count": count, "bytes": total})
            self.q.put({"type": "tool_busy", "value": False})

        threading.Thread(target=work, daemon=True, name="cleanup-measure").start()

    def on_empty_recycle(self) -> None:
        if self._tool_busy or self.engine.busy:
            self.log("Дождитесь завершения текущей операции", "warning")
            return
        if not messagebox.askyesno(APP_NAME, "Безвозвратно очистить корзину на всех дисках?"):
            return
        self._tool_busy = True

        def work():
            ok, msg = cleanup_mod.empty_recycle_bin()
            size = cleanup_mod.recycle_size(self._auto_stop)
            self.q.put({"type": "log", "text": f"Корзина: {msg}", "level": "ok" if ok else "error"})
            self.q.put({"type": "report", "kind": "recycle", "size": size})
            self.q.put({"type": "tool_busy", "value": False})

        threading.Thread(target=work, daemon=True, name="recycle").start()

    def on_open_downloads(self) -> None:
        _count, _total, folder = cleanup_mod.downloads_report(30)
        if folder:
            cleanup_mod.open_in_explorer(folder)

    def on_cleanup_run(self) -> None:
        if self._tool_busy or self.engine.busy:
            self.log("Дождитесь завершения текущей операции", "warning")
            return
        chosen = [card.target for card in self.cleanup_cards.values() if card.var.get()]
        if not chosen:
            messagebox.showinfo(APP_NAME, "Отметьте хотя бы одну цель очистки.")
            return
        # Защита от дефицита места: очистка при заполненном диске может
        # помешать системе; при меньшем минимума — предупреждаем, но не блокируем
        # (очистка сама освобождает место).
        if not enough_space(self.cfg):
            self.log(f"На диске C: меньше {self.cfg.min_free_gb:g} ГБ — "
                     f"очистка всё равно выполнится (она освобождает место)", "warning")
        known = [self._cleanup_sizes.get(t.id) for t in chosen]
        estimate = sum(s for s in known if s)
        msg = "Удалить файлы: " + ", ".join(t.name for t in chosen) + "?"
        if estimate:
            msg = f"Удалить ~{cleanup_mod.fmt_size(estimate)}?\n\n" + msg
        if not messagebox.askyesno(APP_NAME, msg):
            return
        self._tool_busy = True
        is_admin = self.is_admin

        def work():
            # Пока идёт очистка — Windows не уходит в спящий режим
            with NoSleep():
                for target in chosen:
                    if self._closing:
                        return
                    freed, errors = cleanup_mod.clean(target, self._auto_stop, is_admin)
                    for err in errors:
                        self.q.put({"type": "log", "text": f"Очистка «{target.name}»: {err}",
                                    "level": "warning"})
                    if isinstance(freed, int) and freed > 0:
                        history_mod.append("cleanup", name=target.name, freed=freed)
                    size = cleanup_mod.measure(target, self._auto_stop)
                    self.q.put({"type": "cleanup_cleaned", "id": target.id,
                                "name": target.name, "freed": freed, "size": size})
            self.q.put({"type": "tool_busy", "value": False})

        threading.Thread(target=work, daemon=True, name="cleanup-run").start()

    def update_cleanup_total(self) -> None:
        total = 0
        any_known = False
        for tid, card in self.cleanup_cards.items():
            if card.var.get():
                size = self._cleanup_sizes.get(tid)
                if size:
                    total += size
                    any_known = True
        self.cleanup_total_lbl.configure(
            text=f"Выбрано ~{cleanup_mod.fmt_size(total)}" if any_known else "")

    # ======================================================================
    # Раздел «Мусор» (бесполезные программы)
    # ======================================================================
    def _build_bloat_view(self) -> None:
        self.bloat_view = ctk.CTkFrame(self.views, fg_color="transparent")
        self.bloat_view.grid(row=0, column=0, sticky="nsew")
        self.bloat_view.grid_rowconfigure(1, weight=1)
        self.bloat_view.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self.bloat_view, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        box = ctk.CTkFrame(header, fg_color="transparent")
        box.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(box, text="Бесполезные программы",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(box, text="Кандидаты на удаление: предустановленный мусор, триалы, "
                               "магазинные приложения. Ничего не удаляется само.",
                     font=ctk.CTkFont(size=11), text_color=MUTED,
                     wraplength=560, justify="left").pack(anchor="w")
        self.btn_bloat_scan = ctk.CTkButton(header, text="🔍  Искать", width=140,
                                            fg_color=BTN_GRAY, hover_color=BTN_GRAY_HOVER,
                                            border_width=1, border_color=BTN_BORDER,
                                            command=self.on_bloat_scan)
        self.btn_bloat_scan.pack(side="right", padx=(8, 0))

        self.bloat_scroll = ctk.CTkScrollableFrame(self.bloat_view, fg_color="transparent")
        self.bloat_scroll.grid(row=1, column=0, sticky="nsew", padx=10)
        self.bloat_empty = ctk.CTkLabel(
            self.bloat_scroll,
            text="Нажмите «Искать» — проверю установленные программы\nпо списку известного мусора",
            text_color=FAINT, font=ctk.CTkFont(size=13), justify="center")
        self.bloat_empty.pack(pady=60)

    def on_bloat_scan(self) -> None:
        if self._tool_busy or self.engine.busy:
            self.log("Дождитесь завершения текущей операции", "warning")
            return
        self._tool_busy = True
        self.btn_bloat_scan.configure(state="disabled")
        self.log("Полная проверка: программы, магазинные пакеты, задачи, остатки…", "info")

        def work():
            import dataclasses
            arp = [dataclasses.asdict(a) for a in bloat_mod.find_bloat()]
            appx = bloat_mod.find_appx_bloat()
            tasks = bloat_mod.list_tasks()
            residuals = bloat_mod.find_residuals(cancel_event=self._auto_stop)
            for item in residuals:  # размеры только для кандидатов — их немного
                item["size"] = cleanup_mod.dir_size(item["path"], self._auto_stop)
            self.q.put({"type": "bloat_full", "arp": arp, "appx": appx,
                        "tasks": tasks, "residuals": residuals})
            self.q.put({"type": "tool_busy", "value": False})

        threading.Thread(target=work, daemon=True, name="bloat-scan").start()

    def on_uninstall_appx(self, card: "AppxCard") -> None:
        if self._tool_busy or self.engine.busy:
            self.log("Дождитесь завершения текущей операции", "warning")
            return
        info = card.info
        if not messagebox.askyesno(APP_NAME,
                                   f"Удалить магазинное приложение «{info['name']}»?\n\n"
                                   f"{info['reason']}"):
            return
        self._tool_busy = True
        card.set_state("run")

        def work():
            ok, msg = bloat_mod.uninstall_appx(info["fullname"])
            self.q.put({"type": "appx_state", "card": card, "state": "ok" if ok else "error"})
            self.q.put({"type": "log", "text": f"«{info['name']}»: " +
                        ("удалено" if ok else f"ошибка удаления ({msg})"),
                        "level": "ok" if ok else "error"})
            if ok:
                history_mod.append("uninstall", name=info["name"], ok=True)
            self.q.put({"type": "tool_busy", "value": False})

        threading.Thread(target=work, daemon=True, name="appx-uninstall").start()

    def on_toggle_task(self, row: "TaskRow", enable: bool) -> None:
        if self._tool_busy or self.engine.busy:
            self.log("Дождитесь завершения текущей операции", "warning")
            return
        self._tool_busy = True

        def work():
            ok, msg = bloat_mod.set_task_enabled(row.task["path"], row.task["name"], enable)
            new_state = "Ready" if enable else "Disabled"
            self.q.put({"type": "task_state", "row": row,
                        "state": new_state if ok else row.task["state"]})
            self.q.put({"type": "log",
                        "text": f"Задача «{row.task['name']}»: " +
                                ("включена" if enable else "отключена") if ok else
                                f"Задача «{row.task['name']}»: ошибка ({msg})",
                        "level": "ok" if ok else "error"})
            self.q.put({"type": "tool_busy", "value": False})

        threading.Thread(target=work, daemon=True, name="task-toggle").start()

    def on_delete_residual(self, row: "ResidualRow") -> None:
        if self._tool_busy or self.engine.busy:
            self.log("Дождитесь завершения текущей операции", "warning")
            return
        path = row.info["path"]
        if not messagebox.askyesno(
                APP_NAME, f"Удалить папку?\n\n{path}\n\n"
                          f"Убедитесь, что она вам не нужна — восстановить нельзя."):
            return
        self._tool_busy = True

        def work():
            import shutil
            from pathlib import Path
            ok = False
            err = ""
            try:
                shutil.rmtree(Path(path))
                ok = True
            except OSError as e:
                err = e.strerror or str(e)
            self.q.put({"type": "residual_deleted", "row": row, "ok": ok})
            self.q.put({"type": "log",
                        "text": f"«{row.info['name']}»: " + ("папка удалена" if ok
                                else f"не удалось удалить ({err})"),
                        "level": "ok" if ok else "error"})
            self.q.put({"type": "tool_busy", "value": False})

        threading.Thread(target=work, daemon=True, name="residual-delete").start()

    def on_uninstall_bloat(self, app_inst) -> None:
        if self._tool_busy or self.engine.busy:
            self.log("Дождитесь завершения текущей операции", "warning")
            return
        if not messagebox.askyesno(
                APP_NAME, f"Удалить «{app_inst.name}»?\n\n"
                          f"{app_inst.reason}\n\nДействие необратимо."):
            return
        cmd = bloat_mod.uninstall_command(app_inst)
        if cmd is None:
            messagebox.showerror(APP_NAME, "winget не найден — удалите программу "
                                           "через Параметры → Приложения.")
            return
        self._tool_busy = True
        key = f"{app_inst.name}|{app_inst.version}"
        self.q.put({"type": "bloat_state", "key": key, "state": "run"})
        self.log(f"Удаляю «{app_inst.name}»…", "info")

        def work():
            try:
                rc, out = run_cmd(cmd, timeout=900)
            except RuntimeError as e:
                rc, out = None, str(e)
            state = "ok" if rc == 0 else "error"
            self.q.put({"type": "bloat_state", "key": key, "state": state})
            tail = "; ".join(out.splitlines()[-2:]) if rc != 0 and out else ""
            self.q.put({"type": "log",
                        "text": f"«{app_inst.name}»: " + ("удалено" if rc == 0 else
                                f"ошибка удаления (код {rc})" + (f". {tail}" if tail else "")) +
                                ("" if rc == 0 else ". Попробуйте через Параметры → Приложения"),
                        "level": "ok" if rc == 0 else "error"})
            self.q.put({"type": "tool_busy", "value": False})

        threading.Thread(target=work, daemon=True, name="bloat-uninstall").start()

    # ======================================================================
    # Разделы и перезапуск
    # ======================================================================
    def _section_header(self, master, text: str) -> None:
        ctk.CTkLabel(master, text=text, font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=MUTED).pack(fill="x", padx=8, pady=(14, 4))

    def _rebuild_bloat(self, sections: dict) -> None:
        for card in self.bloat_cards.values():
            card.destroy()
        self.bloat_cards.clear()
        for row in self.appx_cards + self.task_rows + self.residual_rows:
            row.destroy()
        self.appx_cards, self.task_rows, self.residual_rows = [], [], []
        self.bloat_empty.pack_forget()

        arp, appx = sections.get("arp", []), sections.get("appx", [])
        tasks, residuals = sections.get("tasks", []), sections.get("residuals", [])

        if arp:
            self._section_header(self.bloat_scroll,
                                 f"Программы ({len(arp)})")
            for a in arp:
                card = BloatCard(self.bloat_scroll, bloat_mod.InstalledApp(**a), self)
                card.pack(fill="x", padx=8, pady=4)
                self.bloat_cards[f"{a['name']}|{a['version']}"] = card
        if appx:
            self._section_header(self.bloat_scroll, f"Магазинные приложения ({len(appx)})")
            for info in appx:
                card = AppxCard(self.bloat_scroll, info, self)
                card.pack(fill="x", padx=8, pady=4)
                self.appx_cards.append(card)
        if tasks:
            self._section_header(self.bloat_scroll,
                                 f"Задачи планировщика вне Microsoft ({len(tasks)})")
            for t in tasks:
                row = TaskRow(self.bloat_scroll, t, self)
                row.pack(fill="x", padx=8, pady=4)
                self.task_rows.append(row)
        if residuals:
            self._section_header(self.bloat_scroll,
                                 f"Возможные остатки удалённых программ ({len(residuals)}) "
                                 "— проверьте перед удалением")
            for info in residuals:
                row = ResidualRow(self.bloat_scroll, info, self)
                row.pack(fill="x", padx=8, pady=4)
                row.set_size(info.get("size"))
                self.residual_rows.append(row)

        if not (arp or appx or tasks or residuals):
            self.bloat_empty.configure(text="Мусора не найдено — всё чисто 🎉")
            self.bloat_empty.pack(pady=60)
        found = len(arp) + len(appx)
        self.log(f"Проверка завершена: программ-кандидатов {found}, "
                 f"задач {len(tasks)}, остатков {len(residuals)}",
                 "info" if found or tasks or residuals else "ok")

    def _refresh_tool_buttons(self) -> None:
        state = "disabled" if self._tool_busy else "normal"
        self.btn_measure.configure(state=state)
        self.btn_cleanup_run.configure(state=state)
        self.btn_bloat_scan.configure(state=state)

    def show_view(self, view_id: str) -> None:
        self._current_view = view_id
        for vid, btn in self.nav_buttons.items():
            active = vid == view_id
            btn.configure(fg_color=ACCENT if active else "transparent",
                          text_color="#ffffff" if active else NAV_TEXT,
                          hover_color="#2563eb" if active else BTN_GRAY_HOVER)
        in_updates = view_id == "updates"
        if not self.engine.busy:
            self.btn_scan.configure(state="normal" if in_updates else "disabled")
            state = "normal" if in_updates and self.cards else "disabled"
            self.btn_upd_sel.configure(state=state)
            self.btn_upd_all.configure(state=state)
        view = {"updates": self.updates_view, "cleanup": self.cleanup_view,
                "bloat": self.bloat_view, "history": self.history_view,
                "faq": self.faq_view, "checks": self.checks_view}[view_id]
        view.tkraise()
        if view_id == "history":
            self._refresh_history()

    def restart_as_admin(self) -> None:
        """Перезапускает приложение с повышением прав через UAC."""
        if self.is_admin:
            return
        if self.engine.busy or self._tool_busy:
            if not messagebox.askyesno(APP_NAME,
                                       "Операция ещё выполняется.\nПрервать и перезапустить?"):
                return
        try:
            import ctypes
            if getattr(sys, "frozen", False):
                params = "--takeover"          # exe сам себе точка входа
            else:
                params = f'"{BASE_DIR / "main.py"}" --takeover'
            # отпускаем мьютекс ДО запуска нового экземпляра, иначе новый
            # стартует быстрее, чем умирает старый процесс, и увидит
            # «приложение уже запущено»
            release_single_instance()
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                                     params, None, 1)
            if rc > 32:
                self.log("Перезапускаю с правами администратора…", "info")
                self._closing = True
                self._auto_stop.set()
                self.engine.cancel()
                self.destroy()
            else:
                self.log("Перезапуск отменён или не разрешён", "warning")
                release_single_instance()  # мьютекс снова наш
        except Exception as e:
            self.log(f"Не удалось перезапустить: {e}", "error")
            release_single_instance()

    # ======================================================================
    # Раздел «История»
    # ======================================================================
    def _build_faq_view(self) -> None:
        """Раздел «FAQ»: частые вопросы по приложению и настройкам."""
        self.faq_view = ctk.CTkFrame(self.views, fg_color="transparent")
        self.faq_view.grid(row=0, column=0, sticky="nsew")
        self.faq_view.grid_rowconfigure(1, weight=1)
        self.faq_view.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self.faq_view, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        ctk.CTkLabel(header, text="Частые вопросы",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(header, text="Коротко о том, что чаще всего спрашивают. "
                                  "Подробности — в README.md рядом с приложением.",
                     font=ctk.CTkFont(size=11), text_color=MUTED).pack(anchor="w")

        scroll = ctk.CTkScrollableFrame(self.faq_view, fg_color="transparent")
        scroll.grid(row=1, column=0, sticky="nsew", padx=10)

        faq = [
            ("Обновления ставятся, но часть пакетов помечена «ошибка ранее» — что это?",
             "Так помечаются пакеты, чья загрузка не удалась. Чтобы не гонять "
             "заведомо падающие загрузки по кругу, они снимаются с выбора и "
             "пропускаются при «Обновить всё» и автообновлении. Как только у "
             "пакета выйдет новая версия — он вернётся в список сам. Вернуть "
             "вручную: правый клик по карточке → «Снова предлагать обновление». "
             "Точную причину сбоя смотрите в журнале: там пишется расшифровка "
             "ошибки (нет прав, источник недоступен и т.п.)."),
            ("Что делает «Игнорировать эту версию»?",
             "Правый клик по пакету → «Игнорировать эту версию» прячет его из "
             "списка до выхода новой версии — удобно, когда новая версия не "
             "нужна. Скрытые пакеты видны внизу списка; вернуть: правый клик → "
             "«Вернуть в список»."),
            ("Как назначить пакету обновление через VPN?",
             "Правый клик по пакету → «Обновлять через VPN». Такие пакеты "
             "обновляются во вторую очередь: приложение проверяет VPN (процесс "
             "клиента, сетевой адаптер или страну по IP), при необходимости "
             "запускает его командой из настроек, обновляет и — по желанию — "
             "отключает обратно. Если VPN отключился посреди загрузки, она "
             "прерывается, чтобы трафик не пошёл мимо VPN."),
            ("Нужны ли права администратора?",
             "Без них тоже работает: Windows сама покажет UAC там, где "
             "установщику нужно повышение. Для полностью тихих обновлений и "
             "очистки WinSxS/кэша обновлений запустите от администратора — "
             "кнопка «🔑 Перезапустить от администратора» в сайдбаре."),
            ("Почему второй запуск пишет «уже запущено»?",
             "Приложение работает в одном экземпляре, чтобы два обновления не "
             "дернулись за одним списком. Если окно не видно — приложение "
             "свёрнуто в трей: повторный запуск просто показывает его окно. "
             "Выйти полностью: правый клик по иконке в трее → «Выход»."),
            ("Что безопасно чистить в разделе «Очистка»?",
             "Все цели безопасны: установщики winget, временные файлы старше "
             "суток (занятые пропускаются), кэши браузеров и pip, кэш уже "
             "установленных обновлений Windows. Кэш браузера чистится только "
             "при закрытом браузере. Отчёты (корзина, старые файлы в "
             "«Загрузках») ничего не удаляют сами."),
            ("Куда пишутся настройки, история и журналы?",
             "Рядом с приложением: config.json (настройки, переживает любые "
             "сбои — битый файл заменяется копией .bak), history.jsonl "
             "(история действий), logs\\obnovlyator.log (полный журнал с "
             "ротацией по суткам). У exe-сборки всё лежит рядом с exe."),
            ("Как работает автообновление по расписанию?",
             "Настройки → «Автопроверка каждые N часов». Фоновый таймер "
             "проверяет обновления; если включено «сразу обновлять всё» — "
             "ставит их без вашего участия. Окно при этом может быть свёрнуто "
             "в трей, результат придёт уведомлением. Точка восстановления "
             "перед автообновлением создаётся, если включена в настройках."),
            ("Приложение умеет обновлять само себя?",
             "Да, но пока требуется настройка: публичный репозиторий GitHub и "
             "ключ подписи (см. selfupdate.py — там пошаговая инструкция). "
             "Пока репозиторий не указан, кнопка «Обновить сам Обновлятор» в "
             "трее честно сообщает, что обновлений нет."),
            ("Почему поиск пакетов медленный в первый раз?",
             "winget при первом запуске обновляет свои источники — это разовая "
             "задержка в несколько секунд. Последующие сканы быстрее."),
        ]
        for q, a in faq:
            block = ctk.CTkFrame(scroll, corner_radius=10, fg_color=CARD_BG)
            block.pack(fill="x", padx=8, pady=4)
            ctk.CTkLabel(block, text=q, anchor="w", justify="left",
                         font=ctk.CTkFont(size=13, weight="bold"),
                         wraplength=820).pack(fill="x", padx=(12, 12), pady=(10, 2))
            ctk.CTkLabel(block, text=a, anchor="w", justify="left",
                         font=ctk.CTkFont(size=11), text_color=MUTED,
                         wraplength=820).pack(fill="x", padx=(12, 12), pady=(0, 10))

    def _build_checks_view(self) -> None:
        """Раздел «Проверки»: здоровье системы, драйверы, диски, целостность."""
        self.checks_view = ctk.CTkFrame(self.views, fg_color="transparent")
        self.checks_view.grid(row=0, column=0, sticky="nsew")
        self.checks_view.grid_rowconfigure(1, weight=1)
        self.checks_view.grid_columnconfigure(0, weight=1)
        self.check_rows: dict[str, dict] = {}
        self._checks_busy = False
        self._checks_queue: list[str] = []

        header = ctk.CTkFrame(self.checks_view, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        ctk.CTkLabel(header, text="Проверки системы",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(header, text="Целостность Windows и Обновлятора, драйверы, "
                                  "диски. Ничего не исправляет и не удаляет — "
                                  "только диагностика.",
                     font=ctk.CTkFont(size=11), text_color=MUTED).pack(anchor="w")

        scroll = ctk.CTkScrollableFrame(self.checks_view, fg_color="transparent")
        scroll.grid(row=1, column=0, sticky="nsew", padx=10)

        admin_note = "нужны права администратора (кнопка 🔑 в сайдбаре)"
        rows = (
            ("integrity", "Целостность Обновлятора",
             "Сверяет файлы приложения с эталоном, записанным при первом "
             "запуске. Мгновенно.",
             [("▶  Проверить", lambda: self._run_check("integrity", 1.0))]),
            ("devices", "Драйверы: проблемные устройства",
             "Ищет устройства с ошибками (как в диспетчере устройств). Быстро.",
             [("▶  Проверить", lambda: self._run_check("devices", 1.0))]),
            ("drivers", "Драйверы: обновления",
             "Опрашивает Windows Update о новых драйверах. Долго: 1–5 минут.",
             [("▶  Искать", lambda: self._run_check("drivers", 1.0))]),
            ("disks", "Диски: SMART и место",
             "Статус здоровья физических дисков и свободное место на томах. "
             "Быстро.",
             [("▶  Проверить", lambda: self._run_check("disks", 1.0))]),
            ("dism", "Система: хранилище компонентов",
             "Быстрая проверка DISM. " + admin_note + ".",
             [("▶  Проверить", lambda: self._run_check("dism", 1.0))]),
            ("sfc", "Система: целостность файлов (полная)",
             "SFC /verifyonly — только доклад, ничего не чинит. "
             "Долго: 5–15 минут. " + admin_note + ".",
             [("▶  Запустить SFC", lambda: self._run_check("sfc", 1.0))]),
            ("chkdsk", "Диск: файловая система",
             "chkdsk /scan системного диска — только чтение. Долго: 5–20 "
             "минут. " + admin_note + ".",
             [("▶  Проверить", lambda: self._run_check("chkdsk", 1.0))]),
        )
        for key, title, desc, buttons in rows:
            card = ctk.CTkFrame(scroll, corner_radius=10, fg_color=CARD_BG)
            card.pack(fill="x", padx=8, pady=4)
            card.grid_columnconfigure(0, weight=1)
            left = ctk.CTkFrame(card, fg_color="transparent")
            left.grid(row=0, column=0, sticky="ew", padx=(12, 6), pady=10)
            ctk.CTkLabel(left, text=title, anchor="w", justify="left",
                         font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x")
            ctk.CTkLabel(left, text=desc, anchor="w", justify="left",
                         font=ctk.CTkFont(size=10), text_color=MUTED,
                         wraplength=460).pack(fill="x")
            btns = ctk.CTkFrame(card, fg_color="transparent")
            btns.grid(row=0, column=1, rowspan=2, padx=4)
            widgets = []
            for text, cmd in buttons:
                b = ctk.CTkButton(btns, text=text, width=130, height=32,
                                  fg_color=BTN_GRAY, hover_color=BTN_GRAY_HOVER,
                                  border_width=1, border_color=BTN_BORDER,
                                  text_color=BTN_TEXT, command=cmd)
                b.pack(pady=3)
                widgets.append(b)
            result = ctk.CTkLabel(card, text="", anchor="w", justify="left",
                                  font=ctk.CTkFont(size=11), wraplength=860)
            result.grid(row=1, column=0, columnspan=2, sticky="ew",
                        padx=(12, 12), pady=(0, 10))
            self.check_rows[key] = {"card": card, "result": result,
                                    "buttons": widgets, "status_lbl": None}
            # цветной итог поверх карточки
            badge = ctk.CTkLabel(card, text="", width=110, corner_radius=8,
                                 font=ctk.CTkFont(size=11), fg_color="transparent")
            badge.place(relx=1.0, x=-160, y=14, anchor="ne")
            self.check_rows[key]["status_lbl"] = badge

        # целостность — мгновенная, прогоним сразу при открытии раздела
        self.after(400, lambda: self._run_check("integrity", 1.0, quiet=True))

    CHECK_COLORS = {
        "ok": ("✔", ("#86efac", "#166534"), ("#14532d", "#dcfce7")),
        "warn": ("⚠", ("#fcd34d", "#92400e"), ("#78350f", "#fef3c7")),
        "error": ("✖", ("#fca5a5", "#991b1b"), ("#7f1d1d", "#fee2e2")),
        "info": ("ℹ", ("#93c5fd", "#1d4ed8"), ("#1e3a8a", "#eff6ff")),
        "run": ("…", ("#93c5fd", "#1d4ed8"), ("#1e3a8a", "#eff6ff")),
    }

    def _run_check(self, key: str, _dummy: float = 1.0, quiet: bool = False) -> None:
        """Запускает одну проверку в фоновом потоке. quiet — не мешать журналу."""
        if self._checks_busy:
            return
        if self.engine.busy:
            self.log("Дождитесь завершения обновлений", "warning")
            return
        fns = {
            "integrity": lambda: diag_mod.app_integrity(self.cfg),
            "devices": diag_mod.problem_devices,
            "drivers": diag_mod.driver_updates_wu,
            "disks": diag_mod.disk_health,
            "dism": lambda: diag_mod.dism_checkhealth(self.is_admin),
            "sfc": diag_mod.sfc_verifyonly,
            "chkdsk": diag_mod.chkdsk_scan,
        }
        fn = fns.get(key)
        if not fn:
            return
        self._checks_busy = True
        row = self.check_rows[key]
        for r in self.check_rows.values():
            for b in r["buttons"]:
                b.configure(state="disabled")
        row["result"].configure(text="выполняется…" if key != "integrity" else "…")
        icon, bg, fg = self.CHECK_COLORS["run"]
        row["status_lbl"].configure(text=icon, fg_color=bg, text_color=fg)
        if not quiet:
            self.log(f"Проверка «{key}» запущена", "info")

        def work():
            try:
                res = fn()
            except Exception as e:                     # noqa: BLE001
                log.exception("Проверка %s упала", key)
                res = {"status": "error", "text": f"Внутренняя ошибка проверки: {e!r}"}
            self.q.put({"type": "check_done", "key": key, "res": res,
                        "quiet": quiet})
            self.q.put({"type": "checks_busy", "value": False})

        threading.Thread(target=work, daemon=True, name=f"check-{key}").start()

    def _checks_done(self, key: str, res: dict, quiet: bool) -> None:
        row = self.check_rows[key]
        status = res.get("status", "info")
        icon, bg, fg = self.CHECK_COLORS.get(status, self.CHECK_COLORS["info"])
        row["status_lbl"].configure(text=icon, fg_color=bg, text_color=fg)
        details = "\n".join("• " + d for d in res.get("details", [])[:12])
        text = res.get("text", "")
        kwargs = {"text": text + ("\n" + details if details else "")}
        if status == "error":
            kwargs["text_color"] = fg
        row["result"].configure(**kwargs)
        for r in self.check_rows.values():
            for b in r["buttons"]:
                b.configure(state="normal")
        if not quiet:
            self.log(f"Проверка «{key}»: {text.splitlines()[0]}", "info" if status in ("ok", "info") else status)
        nxt = self._checks_queue.pop(0) if self._checks_queue else None
        if nxt:
            self.after(150, lambda: self._run_check(nxt, 1.0))

    def _build_history_view(self) -> None:
        self.history_view = ctk.CTkFrame(self.views, fg_color="transparent")
        self.history_view.grid(row=0, column=0, sticky="nsew")
        self.history_view.grid_rowconfigure(2, weight=1)
        self.history_view.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self.history_view, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        btnrow = ctk.CTkFrame(header, fg_color="transparent")
        btnrow.pack(fill="x")
        ctk.CTkButton(btnrow, text="💾  Сохранить список программ (бэкап)", height=30,
                      fg_color=BTN_GRAY, hover_color=BTN_GRAY_HOVER,
                      border_width=1, border_color=BTN_BORDER, text_color=BTN_TEXT,
                      command=self.on_bundle_export).pack(side="left")
        ctk.CTkButton(btnrow, text="📥  Установить из бэкапа", height=30,
                      fg_color=BTN_GRAY, hover_color=BTN_GRAY_HOVER,
                      border_width=1, border_color=BTN_BORDER, text_color=BTN_TEXT,
                      command=self.on_bundle_import).pack(side="left", padx=8)
        ctk.CTkButton(btnrow, text="📄  Экспорт истории txt", height=30,
                      fg_color=BTN_GRAY, hover_color=BTN_GRAY_HOVER,
                      border_width=1, border_color=BTN_BORDER, text_color=BTN_TEXT,
                      command=lambda: self.on_history_export("txt")).pack(side="left", padx=8)
        ctk.CTkButton(btnrow, text="📊  csv", height=30,
                      fg_color=BTN_GRAY, hover_color=BTN_GRAY_HOVER,
                      border_width=1, border_color=BTN_BORDER, text_color=BTN_TEXT,
                      command=lambda: self.on_history_export("csv")).pack(side="left")
        self.stats_lbl = ctk.CTkLabel(header, text="Собираю статистику…", anchor="w",
                                      font=ctk.CTkFont(size=13), text_color=MUTED,
                                      wraplength=900, justify="left")
        self.stats_lbl.pack(fill="x", pady=(6, 0))

        # --- график активности по дням (14 дней, чистый tk.Canvas без matplotlib)
        self.history_chart = HistoryChart(self.history_view, height=130)
        self.history_chart.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 2))

        self.history_scroll = ctk.CTkScrollableFrame(self.history_view,
                                                     fg_color="transparent")
        self.history_scroll.grid(row=2, column=0, sticky="nsew", padx=10)
        self.history_empty = ctk.CTkLabel(
            self.history_scroll,
            text="История пока пуста.\nЗдесь появятся обновления, очистки и удаления.",
            text_color=FAINT, font=ctk.CTkFont(size=13), justify="center")
        self.history_empty.pack(pady=60)

    def on_history_export(self, fmt: str) -> None:
        """Экспорт всей истории в txt или csv."""
        default = f"obnovlyator_history_{time.strftime('%Y%m%d')}.{fmt}"
        path = filedialog.asksaveasfilename(
            defaultextension=f".{fmt}", initialfile=default,
            filetypes=[(fmt.upper(), f"*.{fmt}")])
        if not path:
            return
        try:
            n = history_mod.export_csv(path) if fmt == "csv" \
                else history_mod.export_txt(path)
            self.log(f"История экспортирована ({n} записей): {path}", "ok")
            if self.cfg.notify_enabled:
                tray_mod.notify(APP_NAME, f"История экспортирована: {n} записей")
        except OSError as e:
            messagebox.showerror("Экспорт", f"Не удалось сохранить файл:\n{e}")

    def _refresh_history(self) -> None:
        s = history_mod.stats(30)
        self.stats_lbl.configure(
            text=f"За 30 дней: обновлено {s['ok30']}, ошибок {s['err30']}, "
                 f"освобождено {cleanup_mod.fmt_size(s['cleaned30'])}, "
                 f"удалено программ {s['removed30']}\n"
                 f"За всё время: обновлено {s['ok_all']}, ошибок {s['err_all']}, "
                 f"освобождено {cleanup_mod.fmt_size(s['cleaned_all'])}, "
                 f"удалено программ {s['removed_all']}, "
                 f"обновлений приложения {s['selfupd_all']}"
                 + (f", откатов {s['selfupd_rolled']}" if s['selfupd_rolled'] else ""))
        try:
            self.history_chart.show(history_mod.daily_series(14))
        except Exception:                                       # noqa: BLE001
            log.debug("График истории не обновился", exc_info=True)
        for w in self.history_scroll.winfo_children():
            if w is not self.history_empty:  # заглушку не уничтожаем — используем снова
                w.destroy()
        rows = history_mod.read(limit=150)
        rows.reverse()  # свежие сверху
        if not rows:
            self.history_empty.configure(text="История пока пуста.\n"
                                              "Здесь появятся обновления, очистки и удаления.")
            self.history_empty.pack(pady=60)
            return
        for row in rows:
            self._history_row(row).pack(fill="x", padx=8, pady=2)

    def _history_row(self, row: dict) -> ctk.CTkFrame:
        kind = row.get("kind")
        if kind == "update":
            status = row.get("status", "")
            icon, color = {"ok": ("✔", ("#15803d", "#4ade80")),
                           "error": ("✖", ("#b91c1c", "#f87171")),
                           "cancelled": ("⏹", ("#6b6b74", "#a1a1aa")),
                           "skip": ("⤼", ("#b45309", "#fbbf24"))}.get(
                status, ("•", MUTED))
            text = f"{icon} {row.get('name', '?')} — {status}"
            extra = f"{row.get('version', '')} → {row.get('available', '')}"
        elif kind == "cleanup":
            icon, color = "🧹", ("#2563eb", "#60a5fa")
            text = f"🧹 Очистка: {row.get('name', '?')}"
            extra = f"освобождено {cleanup_mod.fmt_size(row.get('freed') or 0)}"
        elif kind == "uninstall":
            icon, color = "🗑", ("#9333ea", "#c084fc")
            text = f"🗑 Удалено: {row.get('name', '?')}"
            extra = ""
        elif kind == "selfupdate":
            # события самообновления самого Обновлятора (selfupdate.py)
            status = row.get("status", "")
            human = {
                "found": ("🔍", "найдено обновление приложения"),
                "downloading": ("⬇", "загрузка обновления…"),
                "downloaded": ("✔", "загрузка завершена"),
                "download_error": ("✖", "ошибка загрузки"),
                "installing": ("⚙", "установка"),
                "restarting": ("↻", "перезапуск"),
                "ok": ("✔", "обновление успешно"),
                "rolled_back": ("⚠", "ОТКАТ на предыдущую версию"),
                "rollback_error": ("⚠", "ошибка отката"),
            }.get(status, ("•", status or "обновление"))
            icon = human[0]
            color = (("#15803d", "#4ade80") if status in ("ok", "downloaded")
                     else ("#b45309", "#fbbf24") if "error" in status or "back" in status
                     else ("#2563eb", "#60a5fa"))
            text = f"⬆ Обновлятор: {human[1]}"
            ver = " → ".join(str(row.get(k)) for k in ("from_version", "to_version")
                             if row.get(k))
            extra = ver + (f"  ({row.get('detail')})" if status in (
                "download_error", "rolled_back", "rollback_error") and row.get("detail") else "")
        else:
            icon, color = "•", MUTED
            text, extra = str(row.get("kind", "?")), ""
        frame = ctk.CTkFrame(self.history_scroll, corner_radius=8, fg_color=CARD_BG)
        ts = ctk.CTkLabel(frame, text=str(row.get("ts", "")), width=140, anchor="w",
                          font=ctk.CTkFont(family="Consolas", size=10),
                          text_color=MUTED)
        ts.pack(side="left", padx=(12, 4), pady=8)
        main = ctk.CTkLabel(frame, text=text, anchor="w", text_color=color,
                            font=ctk.CTkFont(size=12))
        main.pack(side="left", padx=2)
        if extra:
            ctk.CTkLabel(frame, text=extra, anchor="e",
                         font=ctk.CTkFont(family="Consolas", size=10),
                         text_color=MUTED).pack(side="right", padx=12)
        return frame

    # ======================================================================
    # UX: хоткеи, сведения о пакете
    # ======================================================================
    def _on_refresh(self) -> None:
        action = {"updates": self.on_scan, "cleanup": self.on_cleanup_measure,
                  "bloat": self.on_bloat_scan, "history": self._refresh_history,
                  "faq": lambda: None, "checks": self.on_checks_quick}
        action.get(self._current_view, self.on_scan)()

    def on_checks_quick(self) -> None:
        """Быстрая тройка проверок подряд: устройства → диски → целостность."""
        self._checks_queue = ["disks", "integrity"]
        self._run_check("devices", 1.0)

    def _on_select_all(self) -> None:
        if self.engine.busy or self._tool_busy:
            return
        if self._current_view == "updates" and self.cards:
            for key, card in self.cards.items():
                if key in self.hidden_keys:
                    continue
                if self.cfg.failed_updates.get(key) != card.pkg.available:
                    card.var.set(True)
        elif self._current_view == "cleanup":
            for card in self.cleanup_cards.values():
                card.var.set(True)

    def _on_pkg_info(self, pkg: PackageUpdate) -> None:
        if pkg.source == "pip":
            self._show_pkg_info_dialog(f"{pkg.name}\n\npip-пакет: {pkg.pkg_id}\n"
                                       f"{pkg.version} → {pkg.available}")
            return
        self.log(f"Получаю сведения о {pkg.name}…", "debug")

        def work():
            text = f"{pkg.name} ({pkg.pkg_id})\n\nСведения недоступны: winget не найден."
            if winget_exe():
                try:
                    _rc, out = run_cmd([winget_exe(), "show", "--id", pkg.pkg_id, "--exact",
                                        "--disable-interactivity",
                                        "--accept-source-agreements"], timeout=90)
                    text = out.strip() or "Сведения не получены."
                except RuntimeError as e:
                    text = f"Сведения недоступны: {e}"
            self.q.put({"type": "pkg_info", "text": text})

        threading.Thread(target=work, daemon=True, name="pkg-info").start()

    def _show_pkg_info_dialog(self, text: str) -> None:
        win = ctk.CTkToplevel(self)
        win.title("О пакете — Обновлятор")
        win.geometry("640x520")
        win.transient(self)
        box = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Consolas", size=12), wrap="word")
        box.pack(fill="both", expand=True, padx=10, pady=10)
        box.insert("1.0", text)
        box.configure(state="disabled")
        ctk.CTkButton(win, text="Закрыть", width=110,
                      command=win.destroy).pack(pady=(0, 10))

    # ======================================================================
    # Бэкап списка программ (winget export/import)
    # ======================================================================
    @staticmethod
    def _bundle_tmp_path() -> Path:
        """ASCII-путь для winget: он не умеет писать/читать файлы в каталогах
        с кириллицей (0x80070003), поэтому работаем через ProgramData и
        перемещаем файл питоном."""
        base = Path(os.environ.get("ProgramData") or Path.home()) / "Obnovlyator"
        base.mkdir(parents=True, exist_ok=True)
        return base / "bundle-export.json"

    def on_bundle_export(self) -> None:
        if self._tool_busy or self.engine.busy:
            self.log("Дождитесь завершения текущей операции", "warning")
            return
        default = f"Обновлятор-бэкап-{time.strftime('%Y-%m-%d')}.json"
        path = filedialog.asksaveasfilename(
            title="Куда сохранить список программ",
            defaultextension=".json", initialfile=default,
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        path = str(Path(path).expanduser())  # нормализуем слеши под Windows
        parent = Path(path).parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                messagebox.showerror(APP_NAME, f"Папка недоступна:\n{parent}")
                return
        self._tool_busy = True

        def work():
            exe = winget_exe()
            tmp = self._bundle_tmp_path()
            if not exe:
                self.q.put({"type": "log", "text": "winget не найден", "level": "error"})
            else:
                self.q.put({"type": "log", "text": f"Сохраняю список программ в {path}…",
                            "level": "info"})
                tmp.unlink(missing_ok=True)
                rc, out = run_cmd([exe, "export", "-o", str(tmp),
                                   "--accept-source-agreements"], timeout=600)
                ok = rc == 0 and tmp.exists()
                if ok:
                    try:
                        shutil.move(str(tmp), path)  # питон не боится кириллицы
                    except OSError as e:
                        ok = False
                        self.q.put({"type": "log",
                                    "text": f"Не удалось переместить файл в {path}: {e}. "
                                            f"Список лежит в {tmp}",
                                    "level": "error"})
                if ok:
                    history_mod.append("bundle", action="export", path=path)
                tail = "; ".join(
                    l for l in out.strip().splitlines()[-2:]
                    if "недоступен из любого источника" not in l) if out.strip() else ""
                if ok:
                    self.q.put({"type": "log", "text": "Бэкап списка: готов ✔",
                                "level": "ok"})
                else:
                    self.q.put({"type": "log",
                                "text": "Бэкап списка: ошибка — "
                                        + (f"{explain_error(rc, tail)}"
                                           + (f". {tail}" if tail else "")),
                                "level": "error"})
            self.q.put({"type": "tool_busy", "value": False})

        threading.Thread(target=work, daemon=True, name="bundle-export").start()

    def on_bundle_import(self) -> None:
        if self._tool_busy or self.engine.busy:
            self.log("Дождитесь завершения текущей операции", "warning")
            return
        path = filedialog.askopenfilename(
            title="Файл бэкапа (winget export)",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")])
        if not path:
            return
        if not Path(path).exists():
            messagebox.showerror(APP_NAME, f"Файл не найден:\n{path}")
            return
        if not messagebox.askyesno(
                APP_NAME,
                "Установить программы из файла бэкапа?\n\n"
                f"{path}\n\n"
                "winget начнёт установку всех пакетов из списка — "
                "это может занять долго."):
            return
        self._tool_busy = True

        def work():
            exe = winget_exe()
            tmp = self._bundle_tmp_path()
            if not exe:
                self.q.put({"type": "log", "text": "winget не найден", "level": "error"})
            else:
                try:
                    shutil.copyfile(path, tmp)  # кириллический путь winget не читает
                except OSError as e:
                    self.q.put({"type": "log", "text": f"Не удалось скопировать файл "
                                f"бэкапа во временную папку: {e}", "level": "error"})
                    self.q.put({"type": "tool_busy", "value": False})
                    return
                self.q.put({"type": "log", "text": "Устанавливаю программы из бэкапа…",
                            "level": "info"})
                rc, out = run_cmd([exe, "import", "-i", str(tmp),
                                   "--accept-package-agreements",
                                   "--accept-source-agreements",
                                   "--disable-interactivity"], timeout=7200,
                                  on_line=lambda l: self.q.put(
                                      {"type": "log", "text": l, "level": "debug"}))
                self.q.put({"type": "log",
                            "text": "Установка из бэкапа: " +
                                    ("завершена ✔" if rc == 0 else f"код {rc}"),
                            "level": "ok" if rc == 0 else "warning"})
                if rc == 0:
                    history_mod.append("bundle", action="import", path=path)
            self.q.put({"type": "tool_busy", "value": False})

        threading.Thread(target=work, daemon=True, name="bundle-import").start()

    # ======================================================================
    # Трей и окно
    # ======================================================================
    def _is_hidden(self) -> bool:
        try:
            return self.state() in ("withdrawn", "iconic")
        except Exception:
            return False

    def _notify_if_hidden(self, title: str, msg: str) -> None:
        if self.cfg.notify_enabled and not self._closing and self._is_hidden():
            tray_mod.notify(title, msg)

    # ======================================================================
    # Самообновление самого Обновлятора (механизм — selfupdate.py)
    # ======================================================================
    def _selfupdate_init(self) -> None:
        """Вызывается через 1,5 с после старта: GUI успешно поднялся."""
        if self._closing:
            return
        try:
            self.selfup = selfupdate.create_updater(self.cfg, APP_VERSION)
            if self.selfup is None:
                return
            # Обновление, начатое в прошлый раз, завершилось успешно — фиксируем.
            self.selfup.finalize_update()
            # эталон целостности теперь — обновлённые файлы
            self.cfg.app_hash = ""
            diag_mod.app_integrity(self.cfg)
            # Первая фоновая проверка через 6 с, дальше — по интервалу настроек
            self.after(6000, self._selfupdate_check, False)
            self._selfupdate_schedule_next()
        except Exception:                                       # noqa: BLE001
            log.warning("Инициализация самообновления не удалась", exc_info=True)

    def _selfupdate_schedule_next(self) -> None:
        hours = float(self.cfg.selfupdate_check_hours or 0)
        if hours > 0:
            self.after(int(hours * 3600 * 1000), self._selfupdate_check, False)

    def tray_check_selfupdate(self) -> None:
        if self.selfup is None:          # клик по трее до инициализации (первые 1,5 с)
            self.selfup = selfupdate.create_updater(self.cfg, APP_VERSION)
        self._selfupdate_check(manual=True)

    def _selfupdate_check(self, manual: bool = False) -> None:
        """Фоновая проверка обновлений самого приложения."""
        if self._closing or self._selfupdate_checking or self.selfup is None:
            return
        self._selfupdate_checking = True

        def work() -> None:
            info = None
            verify_failed = ""          # подпись не сошлась — это важно
            net_text = ""               # тихая ошибка сети
            try:
                info = self.selfup.check_for_updates()
            except selfupdate.VerifyError as e:
                verify_failed = str(e)
            except selfupdate.NetworkError as e:
                net_text = str(e)
            except Exception:                                   # noqa: BLE001
                log.warning("Ошибка проверки самообновления", exc_info=True)
            finally:
                self._selfupdate_checking = False

            if verify_failed:
                self.after(0, lambda e=verify_failed: messagebox.showerror(
                    "Безопасность",
                    f"Манифест обновления не прошёл проверку подписи:\n{e}\n\n"
                    "Обновление ЗАБЛОКИРОВАНО. Сообщите разработчику."))
            elif info is not None:
                self.after(0, lambda: self._selfupdate_show_dialog(info))
            elif net_text and manual:
                self.after(0, lambda e=net_text: self.log(
                    f"Самообновление: сеть недоступна ({e})", "warning"))
            elif manual:
                self.after(0, lambda: self.log(
                    "Обновлятор: у вас последняя версия", "ok"))

        threading.Thread(target=work, daemon=True, name="selfupdate-check").start()

    def _selfupdate_show_dialog(self, info) -> None:
        if self._closing:
            return
        self._notify_if_hidden(APP_NAME, f"Доступна версия {info.version} — "
                                         "откройте окно для обновления")
        selfupdate_ui.SelfUpdateDialog(
            self, self.selfup, info,
            on_skip=self._selfupdate_skip,
            on_ready=self._selfupdate_ready,
        )

    def _selfupdate_skip(self, version: str) -> None:
        skipped = set(self.cfg.selfupdate_skipped or [])
        skipped.add(version)
        self.cfg.selfupdate_skipped = sorted(skipped)
        self.cfg.save()
        self.log(f"Версия {version} помечена «пропустить»", "info")

    def _selfupdate_ready(self, info, staged) -> None:
        """Файл скачан и проверен. Спрашиваем: сейчас или при выходе."""
        if messagebox.askyesno(
                "Обновление Обновлятора",
                f"Версия {info.version} готова к установке.\n"
                "Перезапустить Обновлятор сейчас?\n\n"
                "(«Нет» — установится при выходе из программы)"):
            self._selfupdate_apply(info, staged)
        else:
            self._selfupdate_pending = (info, staged)
            self.log(f"Обновление до {info.version} установится при выходе", "info")

    def _selfupdate_apply(self, info, staged) -> None:
        banner = None
        try:
            self.log(f"Перезапуск для установки {info.version}…", "info")
            self._quitting = True
            self._closing = True
            self._auto_stop.set()
            self.engine.cancel()
            tray_mod.stop_tray(self)
            # плашка «Обновляем, подождите, пожалуйста» — висит, пока идёт
            # подмена exe (процесс завершится сам, баннер умрёт вместе с ним)
            banner = ctk.CTkToplevel(self)
            banner.overrideredirect(True)
            banner.attributes("-topmost", True)
            w, h = 420, 150
            banner.geometry(f"{w}x{h}+{(self.winfo_screenwidth() - w) // 2} "
                            f"+{(self.winfo_screenheight() - h) // 2}")
            banner.configure(fg_color=SIDEBAR_BG)
            ctk.CTkLabel(banner, text="🔄  Обновляем, подождите,\n"
                          "пожалуйста…",
                          font=ctk.CTkFont(size=15, weight="bold"),
                          text_color="#e4e4e7", justify="center").pack(expand=True)
            banner.update()
            # updater.apply сам запустит новый exe и завершит этот процесс
            self.selfup.apply(info, staged)
        except selfupdate.UpdateError as e:
            if banner is not None:
                try:
                    banner.destroy()
                except Exception:
                    pass
            self._closing = False
            self._quitting = False
            messagebox.showerror("Обновление Обновлятора",
                                 f"Не удалось применить обновление:\n{e}")

    def _redraw_history_chart(self) -> None:
        """Перерисовка графика истории после смены темы (принудительно)."""
        chart = getattr(self, "history_chart", None)
        if chart is not None and chart.winfo_exists():
            self.after(300, lambda: chart.redraw(force=True))

    def tray_open_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def tray_check_updates(self) -> None:
        self.tray_open_window()
        self.on_scan()

    def tray_update_all(self) -> None:
        self.tray_open_window()
        if self.cards:
            self.on_update_all()
        else:
            self._auto_pending = True
            self.on_scan()

    def tray_quit(self) -> None:
        self._quitting = True
        self._on_close()

    def _quit(self) -> None:
        # если пользователь обещал установить обновление при выходе — предлагаем
        if self._selfupdate_pending and not self._closing:
            info, staged = self._selfupdate_pending
            if messagebox.askyesno(
                    "Обновление Обновлятора",
                    f"Установить обновление до {info.version} перед выходом?"):
                self._selfupdate_apply(info, staged)   # сам завершит процесс
                return
            self._selfupdate_pending = None
        self._quitting = True
        self._closing = True
        self._auto_stop.set()
        self.engine.cancel()
        tray_mod.stop_tray(self)
        self.destroy()

    def apply_theme_mode(self, mode: str) -> None:
        """Тема: dark | light | system.

        Перекраска сериализована: пока одна перекраска идёт, новые запросы
        не наслаиваются (иначе окно мигает несколько раз, а ползунок
        «залипает» в промежуточном положении). Последний запрошенный режим
        применяется после завершения текущей перекраски.
        """
        mode = mode if mode in ("dark", "light", "system") else "dark"
        if self._theme_busy:
            self._theme_pending = mode
            return
        self._theme_busy = True
        try:
            self.cfg.theme_mode = mode
            ctk.set_appearance_mode("system" if mode == "system" else mode)
            self._tags_mode = None
            self.update_idletasks()          # один синхронный проход перекраски
            self._redraw_history_chart()     # график истории перекрашиваем следом
        finally:
            self._theme_busy = False
            self._sync_theme_switch()
            self.after_idle(self._after_theme_paint)

    def _after_theme_paint(self) -> None:
        """Всё, что не обязано блокировать перекраску, — после неё."""
        self._apply_log_tags()
        try:
            self.cfg.save()
        except Exception:
            pass
        pending = getattr(self, "_theme_pending", None)
        if pending:
            self._theme_pending = None
            if pending != self.cfg.theme_mode:
                self.apply_theme_mode(pending)

    def _sync_theme_switch(self) -> None:
        """Ползунок в сайдбаре всегда отражает фактическую тему — даже после
        серии быстрых щелчков или смены темы из окна настроек."""
        self.theme_sw_var.set(self.cfg.theme_mode == "light")

    def _save_config_later(self) -> None:
        try:
            self.cfg.save()
        except Exception:
            pass

    # ======================================================================
    # Очередь событий движка
    # ======================================================================
    def _poll(self) -> None:
        if self._closing:
            return
        try:
            while True:
                ev = self.q.get_nowait()
                try:
                    self._handle(ev)
                except Exception:  # одно неудачное событие не должно убить UI-цикл
                    import traceback
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _handle(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "log":
            self.log(ev["text"], ev.get("level", "info"))
        elif kind == "phase":
            self.phase_lbl.configure(text=ev.get("text", ""))
        elif kind == "progress":
            total = max(1, ev.get("total", 1))
            self.progress.set(min(1.0, ev.get("done", 0) / total))
        elif kind == "pkg":
            card = self.cards.get(ev["key"])
            if card:
                card.set_status(ev["status"])
            if ev["status"] == "error":
                self._mark_failed(ev["key"])
            elif ev["status"] == "ok":
                self._clear_failed(ev["key"], notify=False)
        elif kind == "scanned":
            self._rebuild(ev["packages"])
            self._notify_if_hidden("Обновлятор",
                                   f"Найдено обновлений: {len(ev['packages'])}")
        elif kind == "vpn":
            self.set_vpn_pill(ev.get("active"), ev.get("reason", ""))
        elif kind == "busy":
            self._set_busy(bool(ev.get("value")))
        elif kind == "auto_trigger":
            self._on_auto_trigger()
        elif kind == "tool_busy":
            self._tool_busy = bool(ev.get("value"))
            self._refresh_tool_buttons()
        elif kind == "check_done":
            self._checks_done(ev["key"], ev["res"], bool(ev.get("quiet")))
        elif kind == "checks_busy":
            self._checks_busy = bool(ev.get("value"))
            if not self._checks_busy:
                for r in self.check_rows.values():
                    for b in r["buttons"]:
                        b.configure(state="normal")
        elif kind == "cleanup_size":
            self._cleanup_sizes[ev["id"]] = ev["size"]
            card = self.cleanup_cards.get(ev["id"])
            if card:
                card.set_size(ev["size"])
            self.update_cleanup_total()
        elif kind == "cleanup_cleaned":
            freed = ev.get("freed", 0)
            if freed is None:  # DISM: размер не измеряется
                self.log(f"Очистка «{ev['name']}»: выполнена — место освободится "
                         f"постепенно", "ok")
            elif freed:
                self.log(f"Очистка «{ev['name']}»: освобождено "
                         f"{cleanup_mod.fmt_size(freed)}", "ok")
            else:
                self.log(f"Очистка «{ev['name']}»: освобождать было нечего", "info")
            self._cleanup_sizes[ev["id"]] = ev.get("size")
            card = self.cleanup_cards.get(ev["id"])
            if card:
                card.set_size(ev.get("size"))
            self.update_cleanup_total()
        elif kind == "report":
            if ev["kind"] == "recycle":
                self.report_rows["recycle"].set_value(cleanup_mod.fmt_size(ev["size"]))
            else:
                self.report_rows["downloads"].set_value(
                    f"{ev['count']} шт · {cleanup_mod.fmt_size(ev['bytes'])}")
        elif kind == "bloat_full":
            self._rebuild_bloat(ev)
        elif kind == "bloat_state":
            card = self.bloat_cards.get(ev["key"])
            if card:
                card.set_state(ev["state"])
        elif kind == "appx_state":
            ev["card"].set_state(ev["state"])
        elif kind == "task_state":
            ev["row"].set_state(ev["state"])
        elif kind == "residual_deleted":
            if ev.get("ok"):
                ev["row"].destroy()
                if ev["row"] in self.residual_rows:
                    self.residual_rows.remove(ev["row"])
        elif kind == "pkg_info":
            self._show_pkg_info_dialog(ev.get("text", ""))
        elif kind == "finished":
            if ev.get("ok") and not ev.get("cancelled"):
                self.log("Через 3 с перезапрошу список обновлений…", "debug")
                self.after(3000, self._rescan_if_idle)
            self._notify_if_hidden(
                "Обновлятор",
                f"Готово: обновлено {ev.get('ok', 0)}, ошибок {ev.get('fail', 0)}"
                + (", остановлено" if ev.get("cancelled") else ""))

    def _rescan_if_idle(self) -> None:
        if not self._closing and not self.engine.busy:
            self.on_scan()

    # ======================================================================
    # Действия
    # ======================================================================
    def on_scan(self) -> None:
        if self.engine.busy:
            return
        if self._tool_busy:
            self.log("Дождитесь завершения операции в разделе «Очистка/Мусор»", "warning")
            return
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self.engine.start_scan()

    def on_update_selected(self) -> None:
        if self.engine.busy:
            return
        keys = [key for key, card in self.cards.items() if card.var.get()]
        if not keys:
            messagebox.showwarning(APP_NAME, "Отметьте хотя бы один пакет галочкой.")
            return
        self._start_update(keys)

    def on_update_all(self) -> None:
        if self.engine.busy:
            return
        if not self.cards:
            messagebox.showinfo(APP_NAME, "Сначала проверьте обновления.")
            return
        # пакеты с ошибкой на этой версии и скрытые вручную — не предлагаем
        for key, card in self.cards.items():
            if key in self.hidden_keys:
                continue
            if self.cfg.failed_updates.get(key) == card.pkg.available:
                continue
            card.var.set(True)
        keys = [key for key, card in self.cards.items() if card.var.get()]
        if not keys:
            messagebox.showinfo(
                APP_NAME,
                "Все пакеты уже помечены как ошибочные.\n"
                "Правый клик по карточке → «Снова предлагать обновление».")
            return
        self._start_update(keys)

    def _start_update(self, keys: list[str]) -> None:
        if self._tool_busy:
            self.log("Дождитесь завершения операции в разделе «Очистка/Мусор»", "warning")
            return
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self.engine.start_update(keys)

    def on_cancel(self) -> None:
        self.btn_cancel.configure(state="disabled")
        self.log("Останавливаю…", "warning")
        self.engine.cancel()

    def open_settings(self) -> None:
        self._settings_win = SettingsWindow(self)

    def show_pkg_menu(self, pkg: PackageUpdate, event) -> None:
        is_vpn = self.cfg.category_of(pkg.key) == "vpn"
        menu = tk.Menu(self, tearoff=0, font=("Segoe UI", 10))
        menu.add_command(label=("✓ " if is_vpn else "") + "Обновлять через VPN",
                         command=lambda: self._set_category(pkg, "vpn"))
        menu.add_command(label=("✓ " if not is_vpn else "") + "Обычное обновление (без VPN)",
                         command=lambda: self._set_category(pkg, "normal"))
        menu.add_separator()
        if pkg.key in self.hidden_keys:
            menu.add_command(label="Вернуть в список",
                             command=lambda: self._unignore(pkg.key))
        else:
            menu.add_command(label="Игнорировать эту версию",
                             command=lambda: self._ignore(pkg.key))
        if not self.engine.busy and self.engine.packages:
            menu.add_command(label="Обновить только этот",
                             command=lambda: self._start_update([pkg.key]))
        if pkg.key in self.cfg.failed_updates:
            menu.add_command(label="Снова предлагать обновление",
                             command=lambda: self._clear_failed(pkg.key))
        menu.add_command(label="ℹ  О пакете",
                         command=lambda: self._on_pkg_info(pkg))
        menu.add_command(label="Копировать ID",
                         command=lambda: self._copy(pkg.pkg_id))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_category(self, pkg: PackageUpdate, category: str) -> None:
        self.cfg.set_category(pkg.key, category)
        self.cfg.save()
        card = self.cards.get(pkg.key)
        if card:
            card.set_vpn_category(category == "vpn")
        self.apply_filter()
        self.log(f"{pkg.name}: категория — {'через VPN' if category == 'vpn' else 'без VPN'}", "info")

    def _copy(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    # -- «не предлагать скачивать» после ошибки ----------------------------
    def _mark_failed(self, key: str) -> None:
        """Пакет не смог обновиться: запоминаем версию и убираем из предложений."""
        pkg = self.engine.packages.get(key)
        if not pkg or not pkg.available:
            return
        if self.cfg.failed_updates.get(key) == pkg.available:
            return  # уже записано, не спамим журналом
        self.cfg.failed_updates[key] = pkg.available
        self.cfg.save()
        card = self.cards.get(key)
        if card:
            card.var.set(False)
        self.log(f"«{pkg.name}»: не скачался — больше не предлагается "
                 f"(правый клик по карточке → «Снова предлагать обновление»)", "warning")

    def _clear_failed(self, key: str, notify: bool = True) -> None:
        """Снимает пометку «ошибка»: вручную или потому что обновление удалось."""
        if key not in self.cfg.failed_updates:
            return
        del self.cfg.failed_updates[key]
        self.cfg.save()
        card = self.cards.get(key)
        if card:
            card.set_status("idle")
            card.var.set(True)
        if notify:
            name = self.engine.packages[key].name if key in self.engine.packages else key
            self.log(f"«{name}»: снова предлагается к обновлению", "info")

    # -- ручное игнорирование версии ---------------------------------------
    def _ignore(self, key: str) -> None:
        pkg = (self.engine.packages.get(key)
               or (self.cards[key].pkg if key in self.cards else None))
        if not pkg:
            return
        self.cfg.ignored_updates[key] = pkg.available
        self.cfg.save()
        self.hidden_keys.add(key)
        self.selected.discard(key)
        card = self.cards.get(key)
        if card:
            card.var.set(False)
            card.set_status("hidden")
        self.apply_filter()
        self.log(f"«{pkg.name}» {pkg.available}: скрыто до выхода новой версии "
                 f"(правый клик → «Вернуть в список»)", "info")

    def _unignore(self, key: str) -> None:
        if key not in self.cfg.ignored_updates:
            return
        del self.cfg.ignored_updates[key]
        self.cfg.save()
        self.hidden_keys.discard(key)
        card = self.cards.get(key)
        if card:
            card.set_status("idle")
            card.var.set(True)
        self.apply_filter()
        name = self.engine.packages[key].name if key in self.engine.packages else key
        self.log(f"«{name}»: возвращено в список обновлений", "info")

    def _toggle_theme(self) -> None:
        if self._theme_busy:
            return  # перекраска уже идёт; в конце ползунок синхронизируется
        self.apply_theme_mode("light" if self.theme_sw_var.get() else "dark")

    def _toggle_log(self) -> None:
        if self._log_open:
            self.log_frame.grid_remove()
            self._log_open = False
        else:
            self.log_frame.grid()
            self._log_open = True

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ======================================================================
    # Состояние
    # ======================================================================
    def _set_busy(self, busy: bool) -> None:
        in_updates = self._current_view == "updates"
        has_packages = bool(self.cards)
        self.btn_scan.configure(state="normal" if not busy and in_updates else "disabled")
        state = "normal" if not busy and in_updates and has_packages else "disabled"
        self.btn_upd_sel.configure(state=state)
        self.btn_upd_all.configure(state=state)
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        self._update_title(f"{APP_NAME} — обновление…" if busy else None)
        if not busy:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.set(0)
            self.phase_lbl.configure(text="")

    def _rebuild(self, packages: list[dict]) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)

        for card in self.cards.values():
            card.destroy()
        self.cards.clear()

        fresh_keys = {f"{d['source']}:{d['pkg_id']}" for d in packages}
        self.selected &= fresh_keys
        failed_before: set[str] = set()
        self.hidden_keys: set[str] = set()
        records_changed = False
        for d in packages:
            pkg = PackageUpdate(**d)
            recorded = self.cfg.failed_updates.get(pkg.key)
            if recorded is not None and recorded != pkg.available:
                # вышла новая версия — прошлый неудачный скачок больше не актуален
                del self.cfg.failed_updates[pkg.key]
                records_changed = True
                recorded = None
            ignored = self.cfg.ignored_updates.get(pkg.key)
            if ignored is not None and ignored != pkg.available:
                del self.cfg.ignored_updates[pkg.key]  # новая версия — возвращаем
                records_changed = True
                ignored = None
            if ignored is not None:
                self.selected.discard(pkg.key)
                self.hidden_keys.add(pkg.key)
                failed_before.discard(pkg.key)
            elif recorded is not None:
                self.selected.discard(pkg.key)  # ранее не скачался — не выбираем
                failed_before.add(pkg.key)
            else:
                self.selected.add(pkg.key)      # по умолчанию выбрано всё
            self.cards[pkg.key] = PackageCard(self.list_frame, pkg, self)
            if pkg.key in self.hidden_keys:
                self.cards[pkg.key].set_status("hidden")
            elif pkg.key in failed_before:
                self.cards[pkg.key].set_status("failed_before")
        if records_changed:
            self.cfg.save()

        self.apply_filter()
        self._update_title()
        self._check_vpn_async()
        if self._auto_pending:
            self._auto_pending = False
            if self.cfg.auto_update_all and self.cards:
                self.log("Автообновление: обновляю всё найденное", "info")
                self.after(300, self.on_update_all)

    def _search_debounced(self, *_):
        """Фильтр срабатывает через 150 мс после последнего нажатия —
        при длинных списках ввод не подтормаживает."""
        if self._search_after_id:
            self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(150, self.apply_filter)

    def apply_filter(self, *_):
        self._search_after_id = None
        tab = self.tabs.get()
        query = self.search_var.get().strip().lower()
        order = list(self.cards.items())
        if self.sort_var.get() == "Имя А→Я":
            order.sort(key=lambda kv: kv[1].pkg.name.lower())
        elif self.sort_var.get() == "Имя Я→А":
            order.sort(key=lambda kv: kv[1].pkg.name.lower(), reverse=True)
        hidden_here = []
        for key, card in order:
            is_hidden = key in self.hidden_keys
            ok = tab == "Все" or (tab == "С VPN") == (self.cfg.category_of(key) == "vpn")
            if query and query not in card.pkg.name.lower() and query not in card.pkg.pkg_id.lower():
                ok = False
            if not ok:
                card.pack_forget()
                continue
            if is_hidden:
                hidden_here.append(card)
            else:
                card.pack(fill="x", padx=8, pady=4)
        self.hidden_lbl.pack_forget()
        if hidden_here:
            self.hidden_lbl.configure(
                text=f"Скрытые вами ({len(hidden_here)}) — правый клик по карточке → "
                     f"«Вернуть в список»")
            self.hidden_lbl.pack(fill="x", padx=8, pady=(12, 0))
            for card in hidden_here:
                card.pack(fill="x", padx=8, pady=4)
        self.empty_lbl.pack_forget()
        visible = len(self.cards) - len(self.hidden_keys)
        if not self.cards or (visible == 0 and not hidden_here):
            self.empty_lbl.configure(
                text="Обновлений нет — всё актуально 🎉" if self.engine.packages
                else "Нажмите «Проверить обновления»")
            self.empty_lbl.pack(pady=60)
        self.update_counts()

    def update_counts(self) -> None:
        if not self.engine.packages:
            return
        visible = len(self.cards) - len(self.hidden_keys)
        self.count_lbl.configure(
            text=f"Обновлений: {visible}  •  выбрано: {len(self.selected)}")

    def _update_title(self, text: str | None = None) -> None:
        """Индикатор состояния в заголовке окна — видно на панели задач."""
        if text is not None:
            self.title(text)
        elif self.engine.busy:
            self.title(f"{APP_NAME} — обновление…")
        elif self.engine.packages:
            n = len(self.cards) - len(self.hidden_keys)
            self.title(f"{APP_NAME} — {n} обновл." if n else f"{APP_NAME} — всё актуально")
        else:
            self.title(f"{APP_NAME} — автообновление программ")

    def set_vpn_pill(self, active, reason: str = "") -> None:
        if active is True:
            self.vpn_dot.configure(text_color=("#15803d", "#4ade80"))
            self.vpn_text.configure(text=f"VPN: активен\n{reason}" if reason else "VPN: активен")
        elif active is None:
            self.vpn_dot.configure(text_color=("#b45309", "#fbbf24"))
            self.vpn_text.configure(text=f"VPN: неопределён\n{reason}".strip())
        else:
            self.vpn_dot.configure(text_color=("#6b6b74", "#71717a"))
            self.vpn_text.configure(text=f"VPN: не активен\n{reason}".strip())

    def _check_vpn_async(self) -> None:
        cfg = self.cfg

        def work():
            active, reason = VpnManager(cfg).status()
            self.q.put({"type": "vpn", "active": active, "reason": reason})

        threading.Thread(target=work, daemon=True).start()

    # ======================================================================
    # Журнал
    # ======================================================================
    @staticmethod
    def _resolve(color):
        """Пара (светлый, тёмный) -> конкретный цвет под текущую тему."""
        if isinstance(color, (tuple, list)):
            return color[1] if str(ctk.get_appearance_mode()).lower() == "dark" else color[0]
        return color

    def _apply_log_tags(self) -> None:
        """Перекрашивает теги журнала под активную тему (tk-теги сами не умеют).
        Применяется один раз на тему — вызовы на каждую строку журнала дешёвые."""
        mode = str(ctk.get_appearance_mode()).lower()
        if self._tags_mode == mode:
            return
        self._tags_mode = mode
        for tag, color in LOG_COLORS.items():
            fg = self._resolve(color)
            try:
                self.log_box.tag_config(tag, foreground=fg)
            except Exception:
                try:
                    self.log_box._textbox.tag_config(tag, foreground=fg)
                except Exception:
                    pass

    def log(self, text: str, level: str = "info") -> None:
        tag = level if level in LOG_COLORS else "info"
        self.log_box.configure(state="normal")
        self._apply_log_tags()
        # потолок журнала: во время обновлений winget выдаёт сотни строк,
        # без обрезки текстовое поле постепенно начинает тормозить
        try:
            count = int(self.log_box.index("end-1c").split(".")[0])
            if count > 600:
                self.log_box.delete("1.0", f"{count - 400}.0")
        except Exception:
            pass
        stamp = time.strftime("%H:%M:%S")
        self.log_box.insert("end", f"[{stamp}] {text}\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ======================================================================
    # Автопроверка по расписанию
    # ======================================================================
    def _auto_loop(self) -> None:
        while not self._auto_stop.is_set():
            interval = float(self.cfg.auto_interval_hours or 0)
            if interval <= 0:
                self._auto_stop.wait(60)
                continue
            exited = self._auto_stop.wait(interval * 3600)
            if exited or self._closing:
                return
            if not self.engine.busy:
                self.q.put({"type": "auto_trigger"})

    def _on_auto_trigger(self) -> None:
        if self.engine.busy or self._closing:
            return
        self.log(f"Автопроверка по расписанию ({self.cfg.auto_interval_hours:g} ч)", "info")
        if self.cards:
            if self.cfg.auto_update_all:
                self.on_update_all()
            else:
                self.on_scan()
        else:
            self._auto_pending = True
            self.on_scan()

    # ======================================================================
    # Завершение
    # ======================================================================
    def _on_close(self) -> None:
        if self._quitting:
            self._quit()
            return
        # окно закрывается в трей — обновления продолжаются
        if self.cfg.minimize_to_tray and self._tray_icon is not None:
            if self.engine.busy:
                self.log("Окно скрыто в трей — обновление продолжается", "info")
                self._notify_if_hidden(APP_NAME, "Свернуто в трей — обновление продолжается")
            self.withdraw()
            return
        if self.engine.busy:
            if not messagebox.askyesno(APP_NAME,
                                       "Обновление ещё выполняется.\nПрервать и выйти?"):
                return
        self._quit()
