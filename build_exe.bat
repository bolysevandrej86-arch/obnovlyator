@echo off
cd /d "%~dp0"
echo [1/3] Generating icon...
python _make_icon.py
echo [2/3] Building dist\Obnovlyator.exe (onefile, 2-5 min)...
python -m PyInstaller --noconfirm --onefile --windowed --name Obnovlyator ^
  --icon app.ico ^
  --collect-all customtkinter --collect-all darkdetect ^
  --collect-all pystray --collect-all winotify ^
  main.py
if errorlevel 1 (
  echo BUILD FAILED
  pause
  exit /b 1
)
echo [3/3] Done: dist\Obnovlyator.exe
pause

rem ============================================================
rem  Это ДЕВ-сборка для проверки изменений.
rem  Для публикации релиза с самообновлением используйте:
rem    python build_release.py --version 1.6 --repo owner/repo --notes "..."
rem  (соберёт exe, посчитает sha256, подпишет манифест и напечатает
rem   готовую команду gh release create)
rem ============================================================
