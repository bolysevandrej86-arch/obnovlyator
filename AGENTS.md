# AGENTS.md — Обновлятор (памятка для ИИ-ассистента)

Прочитай это перед любыми правками. Проект живой, у него есть
установленный на машине пользователя exe — осторожнее с файлами.

## Что это

Windows-приложение на Python 3.10+ / CustomTkinter: автообновление программ
через winget (+pip/npm), VPN-категории пакетов, очистка системы, менеджер
автозагрузки, диагностика (SFC/DISM/chkdsk/драйверы/SMART), самообновление
через GitHub Releases. Автор репозитория: bolysevandrej86-arch/obnovlyator.

## Карта модулей

- `core.py` — ядро: Config, движок Engine (фоновый поток + очередь событий),
  winget/pip/npm сканеры, VpnManager, точки восстановления, `explain_error`,
  мьютекс одиночного запуска, `cleanup_stale_mei`, frozen-пути (`BASE_DIR`).
- `ui.py` — всё интерфейсное (App, карточки, окна). События движка → очередь
  `app.q` → `_poll`/`_handle` в главном потоке. Никогда не трогай виджеты
  из фоновых потоков — только `q.put` / `after`.
- `cleanup.py` — очистка (winget-установщики, %TEMP%, pip, WU, DISM, браузеры,
  отчёты: корзина, старые загрузки).
- `bloat.py` — мусор: ARP-реестр, UWP (Appx), задачи планировщика, остатки.
- `startup.py` — автозагрузка (Run HKCU/HKLM, папки). Отключение ОБРАТИМО:
  HKCU-записи переезжают в ключ `RunDisabled_Obnovlyator`, ярлыки → `.disabled`.
- `diag.py` — проверки: SFC/DISM/chkdsk, проблемные устройства, драйверы
  (поиск + установка через COM Windows Update), SMART, целостность
  приложения (`cfg.app_hash`), восстановление системы (точка/список/rstrui).
- `history.py` — история действий (JSONL) + статистика.
- `tray.py` — pystray + winotify. Menu-колбэки кладут события в `app.q`
  (НЕ `app.after` — он из чужого потока потоконебезопасен).
- `selfupdate.py` / `selfupdate_ui.py` — самообновление: GitHub Releases,
  манифест с sha256 + ed25519-подпись, докачка, подмена exe через rename
  (`.old`), авт откат после 2 падений. `SELFUPDATE_REPO` /
  `SELFUPDATE_PUBLIC_KEY` заданы; приватный ключ — `release_keys/`
  (в .gitignore, НИКОГДА не публиковать).
- `build_release.py` — релизный конвейер: сборка в `release_build/`
  (НИКОГДА не в `dist/` — там установленный у пользователя exe!), подпись
  манифеста, печать команды `gh release create`.
- `diag`/`cleanup`/`bloat` возвращают `{"status": ok|warn|error|info,
  "text", "details": []}`.

## Критические правила (насосанные кровью)

1. **Пути в frozen-режиме**: `__file__` у onefile указывает на временную
   распаковку. Для файлов данных — только `core.BASE_DIR`. `history.py`
   берёт BASE_DIR лениво (цикл импортов с core запрещён).
2. **PowerShell только через `diag._ps`** — она использует
   `-EncodedCommand` (base64 UTF-16LE): обычный `-Command` с кавычками
   ломается (EmptyPipeElement), вывод форсируется в UTF-8 (иначе
   кракозябры), CLIXML-шум фильтруется. В PowerShell 5.1 ЗАПРЕЩЁН пайп
   после `if/else` — выливай результаты через foreach.
3. **sfc.exe** пишет UTF-16LE в перенаправление — вывод через файл +
   `Get-Content -Encoding Unicode`.
4. **winget не умеет пути с кириллицей** (0x80070003) — бэкапы идут через
   ASCII-посредник `C:\ProgramData\Obnovlyator\` + перемещение питоном.
5. **Один экземпляр**: именованный мьютекс. Повторный запуск показывает
   окно первого (именованный Event `ObnovlyatorShowWindowEvent`).
   Перезапуск от админа передаёт `--takeover` и отпускает мьютекс ДО
   ShellExecuteW. Убитый пользователем экземпляр = осиротевшие `_MEI*`,
   их чистит `cleanup_stale_mei` на старте.
6. **Тема**: перекраска сериализована (`_theme_busy`/`_theme_pending`),
   после неё снимается висящий grab и синхронизируется ползунок. Смена
   темы из настроек закрывает окно настроек (grab!). Это чинит «кнопки
   перестали нажиматься» — не откатывай.
7. **События трея** — через `app.q` (было через `app.after` из потока
   pystray — «приложение не закрывается»).
8. **`dist/` — установленная у пользователя копия**. Релизы собираются
   в `release_build/` → `release_out/`. Не пересобирай «в dist», если
   пользователь не просил явно.
9. **Смена версии** — только `core.APP_VERSION` (одно место).
10. Права: HKLM/DISM/SFC/chkdsk/WU-установка требуют админа — проверки
    отвечают вежливой подсказкой про кнопку 🔑, а не падают.

## Тестирование

- `python main.py --selftest [--no-net]` — ядро (config, winget, VPN).
- `python main.py --smoke` — окно собирается скрыто и закрывается.
- `python main.py --check-selfupdate` — проверка канала самообновления.
- Интеграционные тесты — python -c c `App(is_admin=False); app.withdraw()`
  и накачкой событий через `app.q` / `app._handle`. Осторожно: голый
  `while: app.update()` без sleep иногда голодает по таймерам в CTk —
  добавляй `time.sleep(0.01)` или используй mainloop.
- UI-клик в живом приложении: CustomTkinter невидим для UI Automation —
  использовать computer-use скриншоты + координаты.

## Релиз

1. Версию поднять в `core.APP_VERSION`.
2. `python build_release.py --version X.Y.Z --repo
   bolysevandrej86-arch/obnovlyator --notes "..."` — соберёт в
   `release_out/X.Y.Z/`, подпишет манифест, напечатает команду
   `gh release create`.
3. Выполнить команду `gh release create` (gh установлен,
   `gh auth login` выполнен пользователем).
4. `git add -A; git commit; git push` (release_out/ в .gitignore).
5. dist/ не трогать без явной просьбы пользователя.

## Уроки самообновления (1.7.5)

- **Частичная распаковка _MEI** (антивирус/мало места на C:) ломала старт
  после самообновления: `set_default_color_theme("blue")` не находил
  blue.json. Тема УЖЕ загружена при импорте customtkinter — обёртка
  try/except в App.__init__ обязательна, не убирай.
- **Автооткат помечает битую версию** в `cfg.selfupdate_skipped`
  (create_updater → mark_skip) — иначе авто-проверка зацикливается на
  падающем релизе. Ручная проверка (`ignore_skip=True`) пометку игнорирует.
- **Слабое железо**: `psutil` (RAM ≤ 6 ГБ или ≤ 4 ядер) автоматически
  включает облегчения (`app._perf`) — как и cfg.perf_mode.
- DPI: по умолчанию system-aware (1); per-monitor v2 — только через
  `per_monitor_dpi: true` (на scaled-ноутбуках v2 давал «пустые разделы»).

## Известные особенности пользователя

- Windows 11, русский язык, диск D:, Python 3.13, репозиторий публичный.
- Пользователь тестирует сборки сам: его dist-exe может быть на
  предыдущей версии; обновление прилетает само через самообновление.
- VPN: xray.exe (периодически включён/выключен) — GitHub API без него
  бывает недоступен, проверки должны честно сообщать о сети.
