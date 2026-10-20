import _bootstrap  # noqa: F401

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fontTools.ttLib import TTFont
from PyQt6.QtCore import QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from utils import font_list
from manga_translator.rendering.text_render import _fonts


_APP = None


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate()


@pytest.fixture
def isolated_font_catalog(monkeypatch, tmp_path):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    project_fonts = tmp_path / "fonts"
    project_fonts.mkdir()
    monkeypatch.setattr(font_list, "fonts_directory", lambda: str(project_fonts))
    monkeypatch.setattr(font_list, "_REGISTERED_FONT_FAMILIES", {})
    monkeypatch.setattr(font_list, "_FONT_FILE_SIGNATURES", {})
    monkeypatch.setattr(font_list, "_FONT_DIRECTORY_SIGNATURE", None)
    monkeypatch.setattr(font_list, "_FONT_FILE_LIST_CACHE", ())
    monkeypatch.setattr(font_list, "_FONT_REGISTRY_REVISION", -1)
    service = font_list._FontCatalogService(_APP)
    monkeypatch.setattr(font_list, "_FONT_CATALOG_SERVICE", service)
    font_list._clear_font_catalog_caches()
    original_paths = set(_fonts.registered_font_files())
    results = {}
    service.ready.connect(lambda key, entries, error: results.update({key: (entries, error)}))

    def catalog(include_system=False):
        key = service.request("en_US", include_system)
        wait_for(lambda: key in results, timeout=30)
        entries, error = results.pop(key)
        assert not error
        return {
            font_list.split_font_value(alias)[0]
            for entry in entries for alias in entry.selection_aliases
        }

    yield project_fonts, catalog
    service.shutdown()
    for path in set(_fonts.registered_font_files()) - original_paths:
        _fonts.unregister_font_file(path)
    font_list._clear_font_catalog_caches()


def write_test_font(path, family):
    path.parent.mkdir(parents=True, exist_ok=True)
    with TTFont(_bootstrap.ROOT / "fonts" / "anime_ace_3.ttf") as font:
        for record in font["name"].names:
            if record.nameID in (1, 3, 4, 6, 16, 21):
                record.string = family.encode(record.getEncoding())
        font.save(path)


@pytest.mark.parametrize("include_system", [False, True])
def test_real_catalog_includes_nested_and_renderer_registered_fonts(
    isolated_font_catalog, tmp_path, include_system,
):
    project_fonts, catalog = isolated_font_catalog
    nested_family = "Catalog Nested Test Font"
    external_family = "Catalog External Test Font"
    write_test_font(project_fonts / "nested" / "project.ttf", nested_family)
    external = tmp_path / "external.ttf"
    write_test_font(external, external_family)
    assert _fonts.register_font_file(str(external)) == [external_family]

    families = catalog(include_system)
    assert {nested_family, external_family} <= families
    assert {nested_family, external_family} <= set(font_list.list_font_families(include_system))


def test_real_catalog_tracks_replacement_deletion_and_registration_changes(
    isolated_font_catalog, tmp_path,
):
    project_fonts, catalog = isolated_font_catalog
    nested = project_fonts / "nested" / "project.ttf"
    external = tmp_path / "external.ttf"
    original_family = "Catalog Original Test Font"
    replacement_family = "Catalog Replacement Test Font"
    external_family = "Catalog Extra Test Font"
    write_test_font(nested, original_family)
    write_test_font(external, external_family)
    _fonts.register_font_file(str(external))
    assert {original_family, external_family} <= catalog()

    write_test_font(nested, replacement_family)
    external.unlink()
    families = catalog()
    assert replacement_family in families
    assert original_family not in families
    assert external_family not in families

    nested.unlink()
    write_test_font(external, external_family)
    _fonts.register_font_file(str(external))
    families = catalog()
    assert external_family in families
    assert replacement_family not in families
    _fonts.unregister_font_file(str(external))
    assert external_family not in catalog()


def test_real_catalog_excludes_headless_system_registrations_when_disabled(
    isolated_font_catalog, monkeypatch, tmp_path,
):
    _project_fonts, catalog = isolated_font_catalog
    system_directory = tmp_path / "system-fonts"
    system_font = system_directory / "system.ttf"
    original_family = "Catalog System Test Font"
    replacement_family = "Catalog Replaced System Test Font"
    write_test_font(system_font, original_family)
    _fonts.register_font_file(str(system_font))
    assert original_family in catalog(False)
    monkeypatch.setattr(_fonts, "_system_fonts_registered", False)
    monkeypatch.setattr(_fonts, "_system_font_dirs", lambda: [str(system_directory)])
    assert _fonts._register_system_fonts()
    assert original_family in catalog(True)
    assert original_family not in catalog(False)
    assert original_family not in font_list.list_font_families(False)

    write_test_font(system_font, replacement_family)
    assert replacement_family not in catalog(False)
    families = catalog(True)
    assert replacement_family in families
    assert original_family not in families


def test_font_worker_entry_exits_without_starting_ui_services():
    result = subprocess.run(
        [sys.executable, "-m", "desktop_qt_ui.main", "--font-catalog-worker"],
        input=json.dumps({"locale": "en_US", "include_system": False, "font_paths": []}),
        capture_output=True, text=True, encoding="utf-8", cwd=_bootstrap.ROOT,
        timeout=30, check=True,
    )
    assert json.loads(result.stdout) == {"entries": []}
    assert "UI log file" not in result.stdout + result.stderr


def test_catalog_cpu_work_is_in_another_process_and_does_not_block_gui(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    script = """
import json, os, sys, time
json.load(sys.stdin)
until = time.monotonic() + 0.4
while time.monotonic() < until:
    sum(range(5000))
print(json.dumps({'entries': [{'display': str(os.getpid()), 'value': 'family',
    'search_aliases': ['family'], 'selection_aliases': ['family']}]}), flush=True)
"""
    monkeypatch.setattr(font_list, "_font_catalog_worker_command", lambda: [sys.executable, "-u", "-c", script])
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(10)
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            future = executor.submit(font_list._load_font_catalog, "en_US", True, (), threading.Event(), lambda *args: None)
            wait_for(future.done)
            entries = future.result()
            assert int(entries[0].display) != os.getpid()
            assert len(ticks) >= 10
        finally:
            timer.stop()


def test_catalog_cancellation_terminates_worker_process(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    script = "import json,sys,time; json.load(sys.stdin); print(json.dumps({'progress':[1,2]}), flush=True); time.sleep(30)"
    monkeypatch.setattr(font_list, "_font_catalog_worker_command", lambda: [sys.executable, "-u", "-c", script])
    cancel, started = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(font_list._load_font_catalog, "en_US", True, (), cancel, lambda *args: started.set())
        try:
            wait_for(started.is_set)
        finally:
            cancel.set()
        wait_for(future.done)
        assert future.result() == ()
