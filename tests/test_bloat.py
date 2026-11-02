# -*- coding: utf-8 -*-
"""bloat: склейка дубликатов, защита удаления, живой ARP."""
from bloat import InstalledApp, merge_duplicates, protection_reason


def _app(name, **kw):
    base = dict(version="", publisher="", install_date="", size_kb=0,
                uninstall_string="")
    base.update(kw)
    return InstalledApp(name=name, **base)


# --- merge_duplicates --------------------------------------------------------
def test_merge_dedupes_by_name():
    apps = [
        _app("Приложение X", version="1.0", size_kb=100),
        _app("приложение x", version="2.0", size_kb=200,
             uninstall_string="MsiExec.exe /X{G}"),
    ]
    out = merge_duplicates(apps)
    assert len(out) == 1
    assert out[0].version == "2.0"          # выигрывает более полная запись
    assert out[0].uninstall_string


def test_merge_backfills_empty_fields():
    apps = [
        _app("App Y", size_kb=500, uninstall_string="un.exe"),   # без версии
        _app("App Y", version="3.1", publisher="Вендор"),
    ]
    out = merge_duplicates(apps)
    assert len(out) == 1
    assert out[0].version == "3.1"
    assert out[0].publisher == "Вендор"
    assert out[0].uninstall_string == "un.exe"   # поля не теряются
    assert out[0].size_kb == 500


def test_merge_keeps_distinct_apps():
    apps = [_app("A"), _app("B")]
    assert len(merge_duplicates(apps)) == 2


# --- protection_reason -------------------------------------------------------
def test_protection_flags_security():
    assert protection_reason("Kaspersky Total Security")
    assert protection_reason("Microsoft Defender Antivirus Service")
    assert protection_reason("AVG Internet Security")


def test_protection_flags_runtimes_and_drivers():
    assert protection_reason("Microsoft Visual C++ 2015 Redistributable (x64)")
    assert protection_reason("Intel(R) Wireless Bluetooth(R)")
    assert protection_reason("NVIDIA Graphics Driver 566.36")
    assert protection_reason(".NET Framework 4.8.1")


def test_protection_allows_regular_apps():
    assert protection_reason("7-Zip 24.08") == ""
    assert protection_reason("Git") == ""
    assert protection_reason("Яндекс Музыка") == ""


# --- живой реестр ------------------------------------------------------------
def test_live_arp_unique_names():
    from bloat import list_installed
    apps = list_installed()
    names = [a.name.lower() for a in apps]
    assert len(names) == len(set(names)), "дубликаты имён после склейки"
