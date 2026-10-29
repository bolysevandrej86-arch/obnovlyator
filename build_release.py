# -*- coding: utf-8 -*-
"""Релизный пайплайн Обновлятора: сборка -> sha256 -> подписанный манифест.

Одна команда делает всё:
    python build_release.py --version 1.6 --repo ivanov/obnovlyator --notes "Что нового"

Шаги:
    1. (одноразово) python build_release.py --make-keys-only
       -> release_keys/private.hex + public.hex.
       ПРИВАТНЫЙ КЛЮЧ НЕ ПОПАДАЕТ в репозиторий и в exe! Добавьте
       release_keys/ в .gitignore.
    2. Публичный ключ вставить в selfupdate.py -> SELFUPDATE_PUBLIC_KEY.
    3. Эта команда: чистит dist/, собирает PyInstaller onefile (те же флаги,
       что в build_exe.bat, БЕЗ UPX и с version-файлом), считает SHA-256,
       генерирует manifest.json, подписывает его ed25519 и проверяет тем же
       алгоритмом, что и клиент (selfupdate.verify_manifest).
    4. Печатает готовую команду gh release create.

Пока репозитория нет: можно запускать --make-keys-only и тренировочные сборки,
но опубликовать релиз получится только после заполнения selfupdate.py
(SELFUPDATE_REPO / SELFUPDATE_PUBLIC_KEY) — см. комментарий вверху того файла.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS_DIR = HERE / "release_keys"
APP = "Obnovlyator"


# ============================================================================
#  Ключи ed25519
# ============================================================================

def make_keys(force: bool = False) -> tuple:
    KEYS_DIR.mkdir(exist_ok=True)
    priv_path, pub_path = KEYS_DIR / "private.hex", KEYS_DIR / "public.hex"
    if priv_path.exists() and not force:
        print(f"[ключи] использую существующие: {pub_path}")
        return pub_path.read_text().strip(), priv_path.read_text().strip()
    try:
        from nacl.signing import SigningKey
    except ImportError:
        sys.exit("Нужен PyNaCl:  pip install pynacl")
    signing = SigningKey.generate()
    priv_path.write_text(signing.encode().hex())
    pub_hex = signing.verify_key.encode().hex()
    pub_path.write_text(pub_hex)
    print(f"[ключи] СОЗДАНЫ новые ключи: {pub_path}")
    print("        1) Вставьте ПУБЛИЧНЫЙ ключ в selfupdate.py -> SELFUPDATE_PUBLIC_KEY")
    print("        2) Добавьте release_keys/ в .gitignore — приватный ключ = ваша подпись")
    return pub_hex, priv_path.read_text().strip()


def sign_manifest(manifest: dict, priv_hex: str) -> dict:
    """Канонический JSON без поля signature -> ed25519 -> добавляем 'signature'."""
    from nacl.signing import SigningKey
    payload = {k: v for k, v in manifest.items() if k != "signature"}
    msg = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    out = dict(payload)
    out["signature"] = SigningKey(bytes.fromhex(priv_hex)).sign(msg).signature.hex()
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ============================================================================
#  Сборка PyInstaller (флаги как в build_exe.bat, но без UPX и с версией)
# ============================================================================

def make_version_info(version: str) -> str:
    """Метаданные exe: версия видна в Проводнике (Свойства -> Подробно)."""
    v = [int(x) for x in (version.split(".") + ["0", "0", "0", "0"])[:4]]
    vn = ",".join(map(str, v))
    vs = ".".join(map(str, v))
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({vn}), prodvers=({vn}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', ''),
        StringStruct('FileDescription', 'Обновлятор'),
        StringStruct('FileVersion', '{vs}'),
        StringStruct('ProductName', 'Обновлятор'),
        StringStruct('ProductVersion', '{vs}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ])
"""


def run_pyinstaller(version: str, icon: str) -> Path:
    for d in (HERE / "dist", HERE / "build"):
        shutil.rmtree(d, ignore_errors=True)
    vf = HERE / "version_info.txt"
    vf.write_text(make_version_info(version), encoding="utf-8")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed", "--clean",
        "--name", APP, "--icon", icon,
        "--version-file", str(vf),
        # UPX НЕ используем специально: упаковка повышает число ложных
        # срабатываний антивирусов и ломает верификацию подписи Windows.
        "--collect-all", "customtkinter", "--collect-all", "darkdetect",
        "--collect-all", "pystray", "--collect-all", "winotify",
        str(HERE / "main.py"),
    ]
    print("[сборка]", " ".join(cmd))
    if subprocess.run(cmd, cwd=str(HERE)).returncode != 0:
        sys.exit("PyInstaller завершился с ошибкой")
    exe = HERE / "dist" / f"{APP}.exe"
    if not exe.exists():
        sys.exit(f"Не найден результат сборки: {exe}")
    return exe


# ============================================================================
#  main
# ============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="Сборка релиза Обновлятора")
    ap.add_argument("--version", help="версия релиза, например 1.6")
    ap.add_argument("--channel", default="stable", choices=["stable", "beta", "nightly"])
    ap.add_argument("--notes", default="", help="заметки релиза")
    ap.add_argument("--notes-file", help="файл с заметками (перекрывает --notes)")
    ap.add_argument("--repo", default="", help="owner/repo на GitHub")
    ap.add_argument("--icon", default=str(HERE / "app.ico"))
    ap.add_argument("--critical", action="store_true",
                    help="критическое обновление (нельзя пропустить)")
    ap.add_argument("--skip-build", action="store_true",
                    help="не собирать exe, взять готовый dist/Obnovlyator.exe")
    ap.add_argument("--make-keys-only", action="store_true",
                    help="только создать ключи и выйти")
    args = ap.parse_args()

    pub_hex, priv_hex = make_keys()
    if args.make_keys_only:
        print("\nПУБЛИЧНЫЙ КЛЮЧ (вставьте в selfupdate.py):\n" + pub_hex)
        return

    if not args.version:
        sys.exit("Укажите --version, например: --version 1.6")
    version = args.version.strip().lstrip("v")
    repo = args.repo.strip().strip("/")
    if not repo:
        print("[внимание] --repo не задан: манифест будет создан, но URL в нём "
              "нельзя сформировать. Укажите --repo owner/repo (или пока просто "
              "потренируйтесь — репозиторий ещё не создан, см. selfupdate.py).")

    exe = (HERE / "dist" / f"{APP}.exe") if args.skip_build else \
        run_pyinstaller(version, args.icon)
    if not exe.exists():
        sys.exit(f"Готовый exe не найден: {exe}")

    notes = Path(args.notes_file).read_text(encoding="utf-8") \
        if args.notes_file else args.notes
    sha = sha256_file(exe)
    manifest = {
        "app_name": "Обновлятор",
        "version": version,
        "channel": args.channel,
        "file": f"{APP}.exe",
        "sha256": sha,
        "size": exe.stat().st_size,
        "url": (f"https://github.com/{repo}/releases/download/v{version}/{APP}.exe"
                if repo else ""),
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "notes": notes,
        "critical": bool(args.critical),
        "min_supported": "",
    }
    if not repo:
        manifest.pop("url")             # без URL верификация не пройдёт — и не должна

    signed = sign_manifest(manifest, priv_hex) if repo else manifest

    out_dir = HERE / "release_out" / version
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exe, out_dir / f"{APP}.exe")
    (out_dir / "manifest.json").write_text(
        json.dumps(signed, ensure_ascii=False, indent=2), encoding="utf-8")

    # Самопроверка: манифест обязан проходить верификацию клиента
    if repo:
        sys.path.insert(0, str(HERE))
        import selfupdate
        selfupdate.verify_manifest(
            json.loads((out_dir / "manifest.json").read_text(encoding="utf-8")),
            repo, pub_hex)
        print("[проверка] манифест подписан и проходит верификацию клиента — OK")

    print("\n" + "=" * 70)
    print(f"Готово: {out_dir}")
    print(f"  {APP}.exe      sha256={sha[:16]}... "
          f"({manifest['size'] / 1024 / 1024:.1f} МБ)")
    print(f"  manifest.json  {'подписан ed25519' if repo else 'БЕЗ подписи (нет repo)'}")
    if repo:
        print("\nПубликация (нужен одноразовый 'gh auth login'):")
        prerelease = " --prerelease" if args.channel in ("beta", "nightly") else ""
        print(f'  gh release create v{version} '
              f'"{out_dir / (APP + ".exe")}" "{out_dir / "manifest.json"}" '
              f'--title "Версия {version}" --notes "{notes or "обновление"}"'
              + prerelease)
        print("\nПосле публикации клиенты получат обновление автоматически "
              "(проверка раз в N часов + кнопка в трее).")
    else:
        print("\nРепозиторий не задан: для реальной публикации повторите с --repo.")


if __name__ == "__main__":
    main()
