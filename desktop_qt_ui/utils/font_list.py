"""Qt 字体数据库共享 helper。

系统字体和应用字体按具体 family/style 展示，同一物理字体的多个入口合并。
项目 ``fonts/`` 中的文件注册进 Qt，编辑器保存明确的家族和样式。

注册统一走 ``text_render.register_font_file``：家族名以 ``[`` 开头的字体
（如 "[工具箱]xxx-简繁"）会被 Qt 的 "Family [Foundry]" 语法解析成空家族名，
QFont 匹配固定落到同一字体；注册层会自动改写为去掉方括号的内存副本。
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import unicodedata
import weakref
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache

from manga_translator.rendering.text_render import (
    font_registry_revision,
    qt_family_is_ambiguous,
    register_font_file,
    registered_font_files,
    strip_qt_foundry_brackets,
    unregister_font_file,
)
from PyQt6.QtCore import (
    QAbstractListModel,
    QEasingCurve,
    QEvent,
    QLocale,
    QModelIndex,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSignalBlocker,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QRawFont,
    QPalette,
    QRegion,
    QWheelEvent,
)
from PyQt6.QtWidgets import QListView, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, IndeterminateProgressBar, LineEdit, MenuAnimationType, isDarkTheme
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.menu import (
    IndicatorMenuItemDelegate,
    MenuAnimationManager,
)
from qfluentwidgets.components.widgets.scroll_bar import SmoothScrollDelegate

from ui.widgets.wheel_filter import TopLevelComboBox, _stop_popup_animation

from .resource_helper import resource_path

logger = logging.getLogger("manga_translator")

FONT_FILE_EXTENSIONS = (".ttf", ".otf", ".ttc")
FONT_STYLE_SEPARATOR = "::"
_REGISTERED_FONT_FAMILIES: dict[str, list[str]] = {}
_ORIGINAL_FONT_DISPLAY_NAMES: dict[str, str] = {}
_FONT_SEARCH_PLACEHOLDERS = {
    "zh_CN": "搜索字体…",
    "zh_TW": "搜尋字型…",
    "ja_JP": "フォントを検索…",
    "ko_KR": "글꼴 검색…",
    "es_ES": "Buscar fuentes…",
    "en_US": "Search fonts…",
}
_SYSTEM_FONTS_ENABLED = True
_FONT_COMBO_INSTANCES: weakref.WeakSet = weakref.WeakSet()
_FONT_DIRECTORY_SIGNATURE: tuple | None = None
_FONT_FILE_SIGNATURES: dict[str, tuple] = {}
_FONT_REGISTRY_REVISION = -1
_FONT_DATABASE_APP = None
_FONT_FILE_LIST_CACHE: tuple[tuple[str, str], ...] = ()
_FONT_FAMILY_CACHE: dict[bool, tuple[str, ...]] = {}
_FONT_CATALOG_SERVICE = None
_FONT_LOADING_TEXTS = {
    "zh_CN": "正在加载字体…",
    "zh_TW": "正在載入字型…",
    "ja_JP": "フォントを読み込み中…",
    "ko_KR": "글꼴 불러오는 중…",
    "es_ES": "Cargando fuentes…",
    "en_US": "Loading fonts…",
}
_FONT_ERROR_TEXTS = {
    "zh_CN": "字体加载失败，请关闭后重试",
    "zh_TW": "字型載入失敗，請關閉後重試",
    "ja_JP": "読み込み失敗。閉じて再試行してください",
    "ko_KR": "불러오기 실패. 닫고 다시 시도하세요",
    "es_ES": "Error al cargar. Cierre y reintente",
    "en_US": "Loading failed. Close and retry",
}


def _clear_font_catalog_caches(invalidate_service=True) -> None:
    if invalidate_service and _FONT_CATALOG_SERVICE is not None:
        _FONT_CATALOG_SERVICE.invalidate()
    _FONT_FAMILY_CACHE.clear()
    _font_name_records.cache_clear()
    _font_family_name_records.cache_clear()
    _font_candidate_key.cache_clear()
    _resolved_font_identity.cache_clear()
    localized_font_family.cache_clear()
    _font_styles.cache_clear()
    _list_font_style_entries_cached.cache_clear()
    _resolve_catalog_value.cache_clear()
    _cached_qfont_for_value.cache_clear()


def fonts_directory() -> str:
    """字体目录绝对路径（打包后位于 app.exe 同级）。"""
    return resource_path("fonts")


def list_font_files() -> list[tuple[str, str]]:
    """Refresh file registrations, including deletion and in-place replacement."""
    global _FONT_DIRECTORY_SIGNATURE, _FONT_FILE_LIST_CACHE
    global _FONT_REGISTRY_REVISION, _FONT_DATABASE_APP
    fonts_dir = fonts_directory()
    signatures = {}
    font_files = []

    def scan_error(error):
        if not isinstance(error, FileNotFoundError):
            raise error

    try:
        for directory, _subdirectories, filenames in os.walk(fonts_dir, onerror=scan_error):
            for filename in filenames:
                if not filename.lower().endswith(FONT_FILE_EXTENSIONS):
                    continue
                path = os.path.normcase(os.path.abspath(os.path.join(directory, filename)))
                try:
                    stat = os.stat(path)
                except FileNotFoundError:
                    continue  # A file may disappear while the directory is scanned.
                signatures[path] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                relative_path = os.path.relpath(os.path.join(directory, filename), fonts_dir)
                font_files.append((os.path.splitext(filename)[0], relative_path))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Failed to scan font directory: %s", exc)
        return list(_FONT_FILE_LIST_CACHE)
    signature = tuple(sorted(signatures.items()))
    files_changed = signature != _FONT_DIRECTORY_SIGNATURE
    if files_changed:
        _FONT_FILE_LIST_CACHE = tuple(
            sorted(font_files, key=lambda item: (item[0].casefold(), item[1]))
        )
        _FONT_DIRECTORY_SIGNATURE = signature

    app = QGuiApplication.instance()
    if app is not None:
        if _FONT_DATABASE_APP is not app:
            app.fontDatabaseChanged.connect(_clear_font_catalog_caches)
            _FONT_DATABASE_APP = app
        for path in set(_REGISTERED_FONT_FAMILIES) - signatures.keys():
            if unregister_font_file(path):
                _REGISTERED_FONT_FAMILIES.pop(path, None)
                _FONT_FILE_SIGNATURES.pop(path, None)
        for _stem, filename in _FONT_FILE_LIST_CACHE:
            path = os.path.normcase(os.path.abspath(os.path.join(fonts_dir, filename)))
            if (
                _FONT_FILE_SIGNATURES.get(path) == signatures[path]
                and _REGISTERED_FONT_FAMILIES.get(path)
            ):
                continue
            _REGISTERED_FONT_FAMILIES[path] = register_font_file(path)
            _FONT_FILE_SIGNATURES[path] = signatures[path]
        # The renderer also registers files outside this directory. Refresh
        # those registrations so replacements/deletions invalidate both the
        # parent Qt database and the subprocess catalog before taking a snapshot.
        for path in registered_font_files():
            if os.path.normcase(os.path.abspath(path)) not in signatures:
                register_font_file(path)
        revision = font_registry_revision()
        if files_changed or revision != _FONT_REGISTRY_REVISION:
            _ORIGINAL_FONT_DISPLAY_NAMES.clear()
            for path in registered_font_files():
                _remember_original_font_names(path)
            _clear_font_catalog_caches()
            _FONT_REGISTRY_REVISION = revision
    return list(_FONT_FILE_LIST_CACHE)


def list_font_families(include_system: bool | None = None) -> list[str]:
    """Return the scalable project and optional system font families."""
    if QGuiApplication.instance() is None:
        return []
    list_font_files()
    if include_system is None:
        include_system = _SYSTEM_FONTS_ENABLED
    include_system = bool(include_system)
    cached = _FONT_FAMILY_CACHE.get(include_system)
    if cached is not None:
        return list(cached)
    families = {
        name
        for name in QFontDatabase.families()
        if name and not qt_family_is_ambiguous(name) and QFontDatabase.isScalable(name)
    }
    if not include_system:
        project_families = {
            family
            for path in registered_font_files(include_system=False)
            for family in register_font_file(path)
        }
        families.intersection_update(project_families)
    result = tuple(sorted(families, key=str.casefold))
    _FONT_FAMILY_CACHE[include_system] = result
    return list(result)


def _search_key(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def _remember_original_font_names(path: str) -> None:
    """Remember bracketed names that registration sanitizes for safe Qt use."""
    if not path.lower().endswith((".ttf", ".otf")):
        return
    try:
        from fontTools.ttLib import TTFont

        font = TTFont(path, lazy=True)
        try:
            for record in font["name"].names:
                if record.nameID not in (1, 16, 21):
                    continue
                try:
                    original = record.toUnicode().strip()
                except UnicodeDecodeError:
                    continue
                sanitized = strip_qt_foundry_brackets(original)
                if original and sanitized != original:
                    _ORIGINAL_FONT_DISPLAY_NAMES.setdefault(
                        _search_key(sanitized), original
                    )
        finally:
            font.close()
    except Exception as exc:
        logger.debug("Failed to read original font name for %s: %s", path, exc)


def _original_font_display_name(name: str) -> str:
    return _ORIGINAL_FONT_DISPLAY_NAMES.get(_search_key(name), name)


@lru_cache(maxsize=None)
def _font_name_records(family: str, style: str = "") -> tuple[tuple[int, str, str], ...]:
    """Return ``(name id, language tag, value)`` records from Qt's font face."""
    records: list[tuple[int, str, str]] = []
    try:
        from fontTools.ttLib import TTFont, newTable
        from fontTools.ttLib.tables._n_a_m_e import _MAC_LANGUAGES, _WINDOWS_LANGUAGES

        data = bytes(_raw_font_for_selection(family, style).fontTable("name"))
        if data:
            table = newTable("name")
            table.decompile(data, TTFont())
            for record in table.names:
                if record.nameID not in (1, 2, 4, 6, 16, 17, 21, 22):
                    continue
                try:
                    value = record.toUnicode().strip()
                except UnicodeDecodeError:
                    continue
                if not value:
                    continue
                if record.platformID == 3:
                    language = _WINDOWS_LANGUAGES.get(record.langID, "")
                elif record.platformID == 1:
                    language = _MAC_LANGUAGES.get(record.langID, "")
                elif record.platformID == 0 and record.langID >= 0x8000:
                    tags = getattr(table, "langTagRecord", ())
                    tag_index = record.langID - 0x8000
                    language = (
                        tags[tag_index].toUnicode() if tag_index < len(tags) else ""
                    )
                else:
                    language = ""
                records.append((record.nameID, language, value))
    except Exception as exc:
        logger.debug("Failed to read localized font name for %s: %s", family, exc)
    return tuple(dict.fromkeys(records))


@lru_cache(maxsize=None)
def _font_family_name_records(
    family: str, style: str = ""
) -> tuple[tuple[int, str, str], ...]:
    return tuple(
        record for record in _font_name_records(family, style)
        if record[0] in (1, 16, 21)
    )


def _raw_font_for_selection(family: str, style: str = "") -> QRawFont:
    font = QFontDatabase.font(family, style, 12) if style else QFont(family, 12)
    return QRawFont.fromFont(font)


# Compare inexpensive metadata before reading large outline/layout tables. Equal
# names or equal weight/style attributes alone never establish face identity.
_FONT_METADATA_TABLES = ("name", "head", "OS/2", "maxp")
_FONT_RENDER_TABLES = (
    "cmap", "hhea", "hmtx", "loca", "glyf", "CFF ", "CFF2", "post",
    "vhea", "vmtx", "VORG", "cvt ", "fpgm", "prep", "gasp",
    "GSUB", "GPOS", "GDEF", "BASE", "JSTF", "MATH", "kern",
    "COLR", "CPAL", "CBDT", "CBLC", "EBDT", "EBLC", "EBSC", "sbix", "SVG ",
    "fvar", "gvar", "avar", "cvar", "HVAR", "VVAR", "MVAR", "STAT",
    "morx", "mort", "kerx", "ankr", "trak", "bsln", "just", "lcar", "opbd", "prop", "feat",
    "Silf", "Glat", "Gloc", "Feat", "Sill",
)


def _font_tables_digest(raw: QRawFont, tags: tuple[str, ...]) -> bytes:
    digest = hashlib.sha256()
    for tag in tags:
        data = bytes(raw.fontTable(tag))
        digest.update(tag.encode("ascii"))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.digest()


@lru_cache(maxsize=None)
def _font_candidate_key(family: str, style: str = "") -> tuple:
    try:
        raw = _raw_font_for_selection(family, style)
        if not raw.isValid() or not raw.fontTable("name") or not raw.fontTable("head"):
            return ()
        # QRawFont does not expose resolved variation coordinates. Never merge
        # variable-font instances on the strength of their shared SFNT tables.
        if raw.fontTable("fvar"):
            return ()
        # A failed Qt match may silently return the default font. Such entries
        # must not be deduplicated with that unrelated fallback family.
        names = {
            name.casefold()
            for _id, _lang, name in _font_family_name_records(family, style)
        }
        if family.casefold() not in names:
            return ()
        # QRawFont.weight() can report the requested 400 even when a legacy
        # family selects a physical Heavy face. The file's OS/2 is in the digest.
        return (_font_tables_digest(raw, _FONT_METADATA_TABLES), raw.style().value)
    except Exception as exc:
        logger.debug("Failed to read font identity for %s (%s): %s", family, style, exc)
        return ()


@lru_cache(maxsize=None)
def _resolved_font_identity(family: str, style: str = "") -> tuple:
    candidate = _font_candidate_key(family, style)
    if not candidate:
        return ()
    try:
        raw = _raw_font_for_selection(family, style)
        if not any(raw.fontTable(tag) for tag in ("glyf", "CFF ", "CFF2")):
            return ()
        return (*candidate, _font_tables_digest(raw, _FONT_RENDER_TABLES))
    except Exception as exc:
        logger.debug("Failed to compare font contents for %s (%s): %s", family, style, exc)
        return ()


def _language_score(language: str, locale_code: str) -> int:
    language = str(language or "").replace("_", "-").casefold()
    locale_code = str(locale_code or "").replace("_", "-").casefold()
    locale_language = locale_code.split("-", 1)[0]
    if language == locale_code:
        return 5
    if locale_code.startswith("zh-cn") and language in {
        "zh",
        "zh-cn",
        "zh-hans",
        "zh-sg",
    }:
        return 5
    if locale_code.startswith("zh-tw") and language in {
        "zh-tw",
        "zh-hant",
        "zh-hk",
        "zh-mo",
    }:
        return 5
    if language.split("-", 1)[0] == locale_language:
        return 4
    if language.split("-", 1)[0] == "en":
        return 2
    return 1 if not language else 0


@lru_cache(maxsize=None)
def localized_font_family(
    family: str, locale_code: str, style: str = ""
) -> tuple[str, tuple[str, ...]]:
    """Return the localized display family and all searchable aliases."""
    records = _font_family_name_records(family, style)
    if not records:
        return family, (family,)

    family_key = _search_key(family)
    matching_name_ids = {
        name_id
        for name_id, _language, value in records
        if _search_key(value) == family_key
    }
    if not matching_name_ids:
        return family, (family,)
    candidates = [
        record
        for record in records
        if record[0] in matching_name_ids
    ]
    name_id_score = {16: 3, 21: 2, 1: 1}
    best = max(
        candidates,
        key=lambda record: (
            _language_score(record[1], locale_code),
            name_id_score.get(record[0], 0),
        ),
    )
    candidate_names = [record[2] for record in candidates]
    aliases = tuple(
        dict.fromkeys(
            [
                family,
                *candidate_names,
                *(_original_font_display_name(name) for name in candidate_names),
            ]
        )
    )
    return _original_font_display_name(best[2]), aliases


@lru_cache(maxsize=None)
def _font_styles(family: str) -> list[str]:
    """Return all Qt styles in stable display order, without inferring a default."""
    try:
        styles = [str(style) for style in QFontDatabase.styles(family) if str(style)]
    except Exception:
        styles = []
    if not styles:
        return [""]
    return sorted(
        styles,
        key=lambda style: (
            style.casefold() not in {"regular", "normal"},
            style.casefold(),
        ),
    )


def font_value(family: str, style: str = "") -> str:
    """Serialize a Qt selection, preserving every explicit style including Regular."""
    if not style:
        return family
    return f"{family}{FONT_STYLE_SEPARATOR}{style}"


def split_font_value(value: str) -> tuple[str, str]:
    """Return the family and optional style represented by ``font_value``."""
    family, separator, style = str(value or "").rpartition(FONT_STYLE_SEPARATOR)
    if separator and family and style:
        return family, style
    return str(value or ""), ""


@dataclass(frozen=True)
class _FontCatalogEntry:
    display: str
    value: str
    search_aliases: tuple[str, ...]
    selection_aliases: tuple[str, ...]
    candidate_key: tuple


def _canonical_font_rank(entry: _FontCatalogEntry) -> tuple:
    family, style = split_font_value(entry.value)
    records = _font_family_name_records(family, style)
    priority = min(
        (
            {16: 0, 21: 1, 1: 2}[name_id]
            for name_id, _language, name in records
            if name.casefold() == family.casefold()
        ),
        default=3,
    )
    return (
        priority, not family.isascii(), len(family),
        family.casefold(), style.casefold(), entry.value,
    )


@lru_cache(maxsize=None)
def _list_font_style_entries_cached(
    locale_code: str,
    include_system: bool,
) -> tuple[_FontCatalogEntry, ...]:
    return _build_font_style_entries(locale_code, list_font_families(include_system))


def _build_font_style_entries(locale_code, families, cancel=None, progress=None):
    """Merge equivalent physical faces only after enumerating every style.

    Search names are deliberately separate from the verified Qt selectors used
    to restore saved values. Neither translated names nor a shared weight imply
    that a saved selector can be redirected to another entry.
    """
    candidates: dict[tuple, list[_FontCatalogEntry]] = {}
    # Stable denominator for both passes; see the merge-pass progress call.
    total_steps = max(len(families), 1) * 2
    for number, family in enumerate(families):
        if cancel is not None and cancel.is_set():
            return ()
        if cancel is not None and number % 10 == 0 and cancel.wait(0.001):
            return ()
        styles = _font_styles(family)
        for style in styles:
            display, family_aliases = localized_font_family(family, locale_code, style)
            value = font_value(family, style)
            search_aliases = tuple(dict.fromkeys(
                (
                    value, *family_aliases,
                    *(name for _id, _language, name in _font_name_records(family, style)),
                    *(f"{alias} {style}" for alias in family_aliases if style),
                )
            ))
            candidate = _font_candidate_key(family, style)
            entry = _FontCatalogEntry(
                f"{display} - {style}" if style and len(styles) > 1 else display,
                value, search_aliases, (value,), candidate,
            )
            key = ("candidate", candidate) if candidate else ("entry", value)
            candidates.setdefault(key, []).append(entry)
        if progress is not None:
            progress(number + 1, total_steps)

    merged = []
    for number, entries in enumerate(candidates.values()):
        if cancel is not None and number % 10 == 0 and cancel.wait(0.001):
            return ()
        groups: dict[tuple, list[_FontCatalogEntry]] = {}
        for entry in entries:
            if cancel is not None and cancel.is_set():
                return ()
            identity = (
                _resolved_font_identity(*split_font_value(entry.value))
                if len(entries) > 1 else ()
            )
            key = ("face", identity) if identity else ("entry", entry.value)
            groups.setdefault(key, []).append(entry)
        for group in groups.values():
            canonical = min(group, key=_canonical_font_rank)
            merged.append(_FontCatalogEntry(
                canonical.display, canonical.value,
                tuple(sorted({alias for item in group for alias in item.search_aliases})),
                tuple(sorted({alias for item in group for alias in item.selection_aliases})),
                canonical.candidate_key,
            ))
        if progress is not None:
            # The merge pass has fewer steps than the enumeration pass, so a
            # per-pass denominator made the reported percentage jump backwards
            # when `candidates` was smaller than `families`. One stable total
            # keeps the counter monotonic.
            progress(len(families) + number + 1, total_steps)

    counts = Counter(_search_key(entry.display) for entry in merged)
    return tuple(sorted((
        _FontCatalogEntry(
            f"{entry.display} ({entry.value})"
            if counts[_search_key(entry.display)] > 1 else entry.display,
            entry.value, entry.search_aliases, entry.selection_aliases, entry.candidate_key,
        )
        for entry in merged
    ), key=lambda entry: (_search_key(entry.display), entry.value)))


def _font_catalog_worker_command():
    if getattr(sys, "frozen", False):
        return [sys.executable, "--font-catalog-worker"]
    worker = os.path.join(os.path.dirname(os.path.dirname(__file__)), "font_catalog_worker.py")
    return [sys.executable, "-u", worker]


def _load_font_catalog(locale, include_system, font_paths, cancel, progress):
    """Run Qt/fontTools in a separate process so they cannot stall GUI painting."""
    if cancel.is_set():
        return ()
    request = {"locale": locale, "include_system": include_system, "font_paths": font_paths}
    # A file for stderr avoids a full pipe deadlocking a fontTools warning burst.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            _font_catalog_worker_command(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            text=True, encoding="utf-8", bufsize=1,
            # Pin the working directory: the worker resolves project-relative
            # font paths, and a packaged app can inherit an unrelated cwd.
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        finished = threading.Event()

        def watch_cancel():
            while not finished.wait(0.05):
                if cancel.is_set():
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    return

        watcher = threading.Thread(target=watch_cancel, name="font-catalog-cancel", daemon=True)
        watcher.start()
        entries = None
        try:
            process.stdin.write(json.dumps(request, ensure_ascii=False))
            process.stdin.close()
            for line in process.stdout:
                if cancel.is_set():
                    break
                message = json.loads(line)
                if "progress" in message:
                    progress(*message["progress"])
                if "entries" in message:
                    entries = tuple(
                        _FontCatalogEntry(
                            item["display"], item["value"], tuple(item["search_aliases"]),
                            tuple(item["selection_aliases"]), (),
                        ) for item in message["entries"]
                    )
            if cancel.is_set():
                return ()
            code = process.wait()
            if code or entries is None:
                errors.seek(max(0, errors.seek(0, os.SEEK_END) - 4096))
                detail = errors.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(detail or f"Font catalog worker exited with code {code}")
            return entries
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            finished.set()
            watcher.join(timeout=0.2)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()


class _FontCatalogService(QObject):
    """Share subprocess results and marshal progress/results onto the GUI thread."""

    ready = pyqtSignal(object, object, str)
    progress = pyqtSignal(object, int, int)
    invalidated = pyqtSignal(int)
    _completed = pyqtSignal(object, int, object, str)

    def __init__(self, app):
        super().__init__(app)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="font-catalog")
        self._generation = 0
        self._cache = {}
        self._pending = {}
        self._closed = False
        self._invalidation_timer = QTimer(self)
        self._invalidation_timer.setSingleShot(True)
        self._invalidation_timer.timeout.connect(lambda: self.invalidated.emit(self._generation))
        self._completed.connect(self._on_completed, type=Qt.ConnectionType.QueuedConnection)
        app.aboutToQuit.connect(self.shutdown)

    def invalidate(self):
        self._generation += 1
        self._cache.clear()
        for cancel, future in self._pending.values():
            cancel.set()
            future.cancel()
        self._pending.clear()
        if not self._closed:
            self._invalidation_timer.start(0)

    def request(self, locale, include_system):
        # Project registrations stay on the GUI thread. The worker never mutates
        # the renderer's registry or hands a QRawFont to another thread.
        list_font_files()
        key = (locale, bool(include_system), self._generation)
        if self._closed:
            return key
        if key in self._cache:
            QTimer.singleShot(0, lambda: self.ready.emit(key, self._cache.get(key), ""))
            return key
        if key in self._pending:
            return key
        font_paths = registered_font_files(include_system=include_system)
        cancel = threading.Event()
        generation = self._generation

        def build():
            def report(done, total):
                if not cancel.is_set():
                    self.progress.emit(key, done, total)
            return _load_font_catalog(locale, include_system, font_paths, cancel, report)

        future = self._executor.submit(build)
        self._pending[key] = (cancel, future)

        def done(completed):
            if cancel.is_set():
                return
            try:
                entries, error = completed.result(), ""
            except Exception as exc:
                entries, error = (), str(exc)
                logger.exception("Failed to load font catalog")
            try:
                self._completed.emit(key, generation, entries, error)
            except RuntimeError:
                pass  # Application was destroyed while a job was finishing.

        future.add_done_callback(done)
        return key

    def _on_completed(self, key, generation, entries, error):
        self._pending.pop(key, None)
        if self._closed or generation != self._generation:
            return
        if not error:
            self._cache[key] = entries
        self.ready.emit(key, entries, error)

    def shutdown(self):
        self._closed = True
        self._invalidation_timer.stop()
        self.invalidate()
        self._executor.shutdown(wait=False, cancel_futures=True)


def _font_catalog_service():
    global _FONT_CATALOG_SERVICE
    if _FONT_CATALOG_SERVICE is None:
        _FONT_CATALOG_SERVICE = _FontCatalogService(QGuiApplication.instance())
    return _FONT_CATALOG_SERVICE


def _selection_key(value: str) -> str:
    # Qt names are case insensitive; search-only NFKC folding must not collapse
    # distinct persisted font names (e.g. full-width and ASCII characters).
    return str(value or "").casefold()


def _left_mouse_button_down():
    if QGuiApplication.mouseButtons() & Qt.MouseButton.LeftButton:
        return True
    if sys.platform == "win32":
        # WindowDeactivate can precede Qt's MouseButtonPress on Windows.
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
    return False


@lru_cache(maxsize=4096)
def _resolve_catalog_value(value: str, locale_code: str, include_system: bool) -> str:
    entries = _list_font_style_entries_cached(locale_code, include_system)
    key = _selection_key(value)
    exact = {
        entry.value for entry in entries
        if any(_selection_key(alias) == key for alias in entry.selection_aliases)
    }
    if exact:
        return next(iter(exact)) if len(exact) == 1 else value

    # Resolve legacy bare-family values through Qt's actual default, never the
    # first enumerated/sorted style. Unavailable or invalid selectors stay intact.
    family, style = split_font_value(value)
    families = {_selection_key(name): name for name in list_font_families(include_system)}
    family = families.get(_selection_key(family)) or families.get(
        _selection_key(strip_qt_foundry_brackets(family))
    )
    if not family or (style and style not in _font_styles(family)):
        return value
    candidate = _font_candidate_key(family, style)
    if not candidate:
        return value
    matches = [entry for entry in entries if entry.candidate_key == candidate]
    if not matches:
        return value
    identity = _resolved_font_identity(family, style)
    if not identity:
        return value
    values = {
        entry.value for entry in matches
        if _resolved_font_identity(*split_font_value(entry.value)) == identity
    }
    return next(iter(values)) if len(values) == 1 else value


@lru_cache(maxsize=4096)
def _cached_qfont_for_value(value: str) -> QFont:
    family, style = split_font_value(value)
    return QFontDatabase.font(family, style, 12) if style else QFont(family)


def qfont_for_value(value: str) -> QFont:
    """Build a preview font from a persisted family/style selection."""
    return QFont(_cached_qfont_for_value(str(value or "")))


def populate_font_combo(
    combo, current: str | None = None, locale_code: str = "en_US", *, entries=None
) -> None:
    """清空并填充字体下拉框：显示本地化名称，userData 保留 family/style。

    ``current`` 传当前 font value 时选中对应条目；
    条目不在列表里则追加一条（userData 保留原值）再选中。
    """
    combo.clear()
    combo._font_search_terms = {}
    include_system = getattr(combo, "_include_system_fonts", _SYSTEM_FONTS_ENABLED)
    if entries is None:
        list_font_files()
        entries = _list_font_style_entries_cached(locale_code, include_system)
    for entry in entries:
        display, value, aliases = entry.display, entry.value, entry.search_aliases
        combo.addItem(display, userData=value)
        combo._font_search_terms[value] = _search_key(" ".join((display, *aliases)))
    # Saved bare families and unavailable styles remain explicit selections.
    # Resolving them by hashing font tables here would block the GUI again.
    selections = {
        _selection_key(alias): entry.value
        for entry in entries for alias in entry.selection_aliases
    }
    combo._font_selection_aliases = selections
    if not current:
        return
    current = selections.get(_selection_key(current), current)
    for index in range(combo.count()):
        item_data = combo.itemData(index)
        if item_data == current:
            combo.setCurrentIndex(index)
            return
    family, style = split_font_value(current)
    display, aliases = _original_font_display_name(family), (family,)
    if style:
        display = f"{display} - {style}"
    combo.addItem(display, userData=current)
    combo._font_search_terms[current] = _search_key(" ".join((display, *aliases)))
    combo.setCurrentIndex(combo.count() - 1)


class _FontMenuModel(QAbstractListModel):
    """Full font catalog model with precomputed searchable metadata."""

    SearchRole = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self._entries = tuple(entries)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._entries)

    def set_entries(self, entries):
        self.beginResetModel()
        self._entries = tuple(entries)
        self.endResetModel()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        display, value, search_terms = self._entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return display
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == self.SearchRole:
            return search_terms
        if role == Qt.ItemDataRole.SizeHintRole:
            width = (
                self.parent().fontMetrics().horizontalAdvance(display)
                if self.parent()
                else len(display) * 8
            )
            return QSize(40 + width, 33)
        return None


class _FontFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._query = ""

    def set_filter(self, text: str):
        query = _search_key(text.strip())
        if query == self._query:
            return
        self._query = query
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        if not self._query:
            return True
        model = self.sourceModel()
        index = model.index(source_row, 0, source_parent)
        return self._query in str(model.data(index, _FontMenuModel.SearchRole) or "")


class _FontMenuDelegate(IndicatorMenuItemDelegate):
    """Resolve and paint preview fonts only for rows requested by the viewport."""

    def paint(self, painter, option, index):
        value = index.model().data(index, Qt.ItemDataRole.UserRole)
        option.font = _cached_qfont_for_value(str(value or ""))
        super().paint(painter, option, index)

    def sizeHint(self, option, index):
        hint = index.model().data(index, Qt.ItemDataRole.SizeHintRole)
        return QSize(max(hint.width(), 1), 33)


class _FontMenuScrollDelegate(SmoothScrollDelegate):
    """Preserve Fluent mouse-wheel scrolling and add native touchpad deltas."""

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.Wheel:
            return super().eventFilter(obj, event)

        pixel_y = event.pixelDelta().y()
        if pixel_y:
            bar = self.parent().verticalScrollBar()
            target = max(bar.minimum(), min(bar.maximum(), bar.value() - pixel_y))
            if target == bar.value():
                return False
            bar.setValue(target)
            event.accept()
            return True

        if event.angleDelta().isNull():
            return False
        return super().eventFilter(obj, event)


class _FontMenuListView(QListView):
    """QListView replacement retaining MenuActionListWidget sizing semantics."""

    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self._itemHeight = 33
        self._maxVisibleItems = -1
        self._font_model = _FontMenuModel(entries, self)
        self._filter_model = _FontFilterProxyModel(self)
        self._filter_model.setSourceModel(self._font_model)
        self.setModel(self._filter_model)
        self.setObjectName("comboListWidget")
        self.setViewportMargins(0, 2, 0, 6)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setDragEnabled(False)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(self.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setUniformItemSizes(True)
        self.setItemDelegate(_FontMenuDelegate(self))
        self.scrollDelegate = _FontMenuScrollDelegate(self)
        self._natural_width = 1

    def recalculateNaturalWidth(self):
        self._natural_width = max(
            (
                40 + self.fontMetrics().horizontalAdvance(entry[0])
                for entry in self._font_model._entries
            ),
            default=1,
        )

    def setItemHeight(self, height: int):
        self._itemHeight = int(height)
        self.adjustSize()

    def setMaxVisibleItems(self, num: int):
        self._maxVisibleItems = int(num)
        self.adjustSize()

    def maxVisibleItems(self):
        return self._maxVisibleItems

    def itemsHeight(self):
        rows = self._filter_model.rowCount()
        if self._maxVisibleItems > 0:
            rows = min(rows, self._maxVisibleItems)
        margins = self.viewportMargins()
        return rows * self._itemHeight + margins.top() + margins.bottom()

    def heightForAnimation(self, pos, aniType):
        manager = MenuAnimationManager.make(self, aniType)
        _width, available_height = manager.availableViewSize(pos)
        return min(self.itemsHeight(), available_height)

    def adjustSize(self, pos=None, aniType=MenuAnimationType.NONE):
        manager = MenuAnimationManager.make(self, aniType)
        available_width, available_height = manager.availableViewSize(pos)
        margins = self.viewportMargins()
        width = max(
            min(
                available_width,
                self._natural_width + margins.left() + margins.right() + 2,
            ),
            self.minimumWidth(),
        )
        # MenuActionListWidget adds three pixels after its viewport margins;
        # retaining that allowance avoids a one-pixel-short final row.
        height = min(available_height, self.itemsHeight() + 3)
        if self._maxVisibleItems > 0:
            height = min(
                height,
                self._maxVisibleItems * self._itemHeight
                + margins.top()
                + margins.bottom()
                + 3,
            )
        self.setFixedSize(max(width, 1), max(height, 1))

    def setCurrentRow(self, source_row: int):
        source_index = self._font_model.index(source_row, 0)
        index = self._filter_model.mapFromSource(source_index)
        if not index.isValid():
            self.clearSelection()
            self.setCurrentIndex(QModelIndex())
            return
        self.setCurrentIndex(index)
        self.selectionModel().select(
            index, self.selectionModel().SelectionFlag.ClearAndSelect
        )

    def set_filter(self, text: str):
        self._filter_model.set_filter(text)

    def source_row(self, index: QModelIndex) -> int:
        if not index.isValid():
            return -1
        return self._filter_model.mapToSource(index).row()


class _FontMenuRevealAnimation(QObject):
    """Fluent-duration reveal that keeps the search field anchored."""

    def __init__(self, menu):
        super().__init__(menu)
        self.ani = QPropertyAnimation(menu, b"revealProgress", menu)
        self.ani.setDuration(250)
        self.ani.setEasingCurve(QEasingCurve.Type.OutQuad)


class _FontComboBoxMenu(ComboBoxMenu):
    """Fluent searchable menu backed by a full model and virtualized view."""

    _WINDOW_FLAGS = (
        Qt.WindowType.Tool
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.NoDropShadowWindowHint
    )

    fontHovered = pyqtSignal(str)
    fontSelected = pyqtSignal(int)
    _STATUS_HEIGHT = 76

    def __init__(self, font_entries, placeholder, parent=None):
        self._font_entries = tuple(font_entries)
        self._was_activated = False
        self._anchor = None
        self._anchor_watchers = ()
        self._opens_upward: bool | None = None
        self._content_opens_upward: bool | None = None
        self._application_filter_installed = False
        self._reveal_progress = 1.0
        super().__init__(parent)
        old_view = self.view
        self.hBoxLayout.removeWidget(old_view)
        old_view.hide()
        old_view.deleteLater()
        self.view = _FontMenuListView(self._font_entries, self)
        # ComboBoxMenu changes the base list from AlwaysOff to AsNeeded after
        # construction. Reapply that step because this virtualized view replaces
        # the one initialized by ComboBoxMenu.
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setShadowEffect()
        self._container = QWidget(self)
        # RoundMenu itself is translucent. Give both the status and the input
        # an opaque surface so disabled controls never expose text behind them.
        palette = self._container.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#2d2d2d" if isDarkTheme() else "#ffffff"))
        self._container.setPalette(palette)
        self._container.setAutoFillBackground(True)
        self._content_layout = QVBoxLayout(self._container)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(6)
        self.search_edit = LineEdit(self._container)
        self.search_edit.setPlaceholderText(placeholder)
        self.search_edit.setClearButtonEnabled(True)
        self._search_placeholder = placeholder
        self._status_panel = QWidget(self._container)
        self._status_panel.setFixedHeight(self._STATUS_HEIGHT)
        status_layout = QVBoxLayout(self._status_panel)
        status_layout.setContentsMargins(12, 8, 12, 12)
        self.status_label = CaptionLabel(self._status_panel)
        self.status_label.setWordWrap(True)
        self._loading_bar = IndeterminateProgressBar(self._status_panel, start=False)
        self._loading_bar.setFixedHeight(3)
        status_layout.addWidget(self.status_label, 1)
        status_layout.addWidget(self._loading_bar)
        self._status_panel.hide()
        self._content_layout.addWidget(self.search_edit)
        self._content_layout.addWidget(self._status_panel)
        self._content_layout.addWidget(self.view)
        self.hBoxLayout.addWidget(self._container, 1)
        self.search_edit.textChanged.connect(self._filter_items)
        self.view.entered.connect(self._on_index_entered)
        self.view.clicked.connect(self._on_index_clicked)
        self._apply_view_style()

    def _apply_view_style(self):
        self.view.setStyleSheet(
            self.styleSheet().replace("MenuActionListWidget", "_FontMenuListView")
        )
        # The popup follows the combo width; measuring every installed font's
        # display name here can trigger another global glyph-fallback scan.
        self.view.adjustSize()

    def set_loading(self, text, *, error=False):
        self.status_label.setText(text)
        self.status_label.setToolTip(text)
        self._status_panel.show()
        self.search_edit.setEnabled(False)
        self.view.setEnabled(False)
        self.view.hide()
        self.view._font_model.set_entries(())
        if error:
            self._loading_bar.stop()
            self._loading_bar.hide()
        else:
            self._loading_bar.show()
            self._loading_bar.start()
        self._layout_for_anchor()

    def set_entries(self, entries, current_row):
        _stop_popup_animation(self)
        self.clearMask()
        self._loading_bar.stop()
        self._loading_bar.hide()
        self._status_panel.hide()
        self.search_edit.setEnabled(True)
        self.search_edit.setPlaceholderText(self._search_placeholder)
        self.view.setEnabled(True)
        self._font_entries = tuple(entries)
        self.view._font_model.set_entries(self._font_entries)
        self.view.show()
        self.view.setCurrentRow(current_row)
        self._layout_for_anchor()
        self._focus_search_if_active()

    def _focus_search_if_active(self) -> None:
        # Loading disables the search field, so the initial focus request may
        # have been ignored. Only retry while this popup still owns focus.
        if self.isVisible() and QGuiApplication.focusWindow() is self.windowHandle():
            self.search_edit.setFocus(Qt.FocusReason.PopupFocusReason)

    def bind_to_anchor(self, anchor) -> None:
        """Keep this independent tool window attached to its combo box."""
        self._anchor = anchor
        watchers = []
        widget = anchor
        while widget is not None:
            if widget not in watchers:
                watchers.append(widget)
                widget.installEventFilter(self)
            widget = widget.parentWidget()
        self._anchor_watchers = tuple(watchers)
        application = QGuiApplication.instance()
        if application is not None:
            application.installEventFilter(self)
            self._application_filter_installed = True
        self._layout_for_anchor()

    def _layout_for_anchor(self) -> None:
        """Keep the entire popup above/below the combo, including loading text."""
        anchor = self._anchor
        if anchor is None:
            return
        try:
            anchor_top_left = anchor.mapToGlobal(QPoint())
            anchor_center = anchor.mapToGlobal(anchor.rect().center())
            anchor_width = anchor.width()
            anchor_height = anchor.height()
        except RuntimeError:
            return

        screen = (
            QGuiApplication.screenAt(anchor_center) or QGuiApplication.primaryScreen()
        )
        if screen is None:
            return
        available = screen.availableGeometry()
        margins = self.layout().contentsMargins()
        spacing = self._content_layout.spacing()
        loading = not self._status_panel.isHidden()
        desired_view_height = self._STATUS_HEIGHT if loading else max(40, self.view.itemsHeight() + 3)
        chrome_height = anchor_height + spacing + margins.top() + margins.bottom()
        gap = 4
        available_below = max(0, available.bottom() + 1 - anchor_top_left.y() - anchor_height - gap)
        available_above = max(0, anchor_top_left.y() - available.top() - gap)
        # Loading and the final list can have very different heights. Recompute
        # direction for the current body, including when the anchor moves.
        below_height = min(desired_view_height + chrome_height, available_below)
        above_height = min(desired_view_height + chrome_height, available_above)
        opens_upward = above_height > below_height
        if self._opens_upward != opens_upward:
            _stop_popup_animation(self)
            self.clearMask()
        self._opens_upward = opens_upward
        maximum_view_height = max(1, (available_above if self._opens_upward else available_below) - chrome_height)
        view_height = max(1, min(desired_view_height, maximum_view_height))

        # The original combo menu makes its inner content inherit the combo
        # width. Do not let long font names or platform size hints widen it.
        content_width = max(1, min(anchor_width, available.width() - margins.left() - margins.right()))
        self.search_edit.setFixedSize(content_width, anchor_height)
        self._status_panel.setFixedSize(content_width, view_height if loading else self._STATUS_HEIGHT)
        self.view.setFixedSize(content_width, view_height)
        self._content_layout.activate()
        self.layout().activate()
        self.adjustSize()

        x = anchor_top_left.x() - margins.left()
        if self._opens_upward:
            y = anchor_top_left.y() - self.height() - gap
        else:
            y = anchor_top_left.y() + anchor_height + gap
        x = max(available.left(), min(x, available.right() - self.width() + 1))
        y = max(available.top(), min(y, available.bottom() - self.height() + 1))
        self.move(x, y)

    def _follow_anchor(self) -> None:
        if self._anchor is None or not self.isVisible():
            return
        _stop_popup_animation(self)
        self.clearMask()
        self._layout_for_anchor()

    def _detach_anchor(self) -> None:
        if self._application_filter_installed:
            application = QGuiApplication.instance()
            if application is not None:
                application.removeEventFilter(self)
            self._application_filter_installed = False
        for widget in self._anchor_watchers:
            try:
                widget.removeEventFilter(self)
            except RuntimeError:
                pass
        self._anchor_watchers = ()
        self._anchor = None
        self._opens_upward = None
        self._content_opens_upward = None

    def _on_index_entered(self, index):
        row = self.view.source_row(index)
        if 0 <= row < len(self._font_entries):
            self.fontHovered.emit(self._font_entries[row][1])
        else:
            self.fontHovered.emit("")

    def _on_index_clicked(self, index):
        row = self.view.source_row(index)
        if not 0 <= row < len(self._font_entries):
            return
        # Commit before closing: WA_DeleteOnClose may destroy this menu while
        # closeEvent is running, so no signal may safely follow _hideMenu().
        self.fontSelected.emit(row)
        self._hideMenu(False)

    def leaveEvent(self, event):
        self.fontHovered.emit("")
        super().leaveEvent(event)

    def _filter_items(self, text: str) -> None:
        self.view.set_filter(text)
        self.view.scrollToTop()

    def adjustSize(self):
        if not hasattr(self, "_container"):
            return super().adjustSize()
        margins = self.layout().contentsMargins()
        hint = self._container.sizeHint()
        self.setFixedSize(
            hint.width() + margins.left() + margins.right(),
            hint.height() + margins.top() + margins.bottom(),
        )

    def _set_reveal_progress(self, progress: float) -> None:
        self._reveal_progress = max(0.0, min(1.0, float(progress)))
        if self._reveal_progress >= 1.0:
            self.clearMask()
            return
        height = max(1, round(self.height() * self._reveal_progress))
        top = self.height() - height if self._opens_upward else 0
        visible = QRect(0, top, self.width(), height)
        self.setMask(QRegion(visible))

    def _get_reveal_progress(self) -> float:
        return self._reveal_progress

    revealProgress = pyqtProperty(
        float, fget=_get_reveal_progress, fset=_set_reveal_progress
    )

    def show_for_anchor(self) -> None:
        self.aniManager = _FontMenuRevealAnimation(self)
        animation = self.aniManager.ani
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.finished.connect(self.clearMask)
        self._set_reveal_progress(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        animation.start()
        QTimer.singleShot(0, self._focus_search_if_active)

    @staticmethod
    def _belongs_to(obj, root) -> bool:
        while obj is not None:
            if obj is root:
                return True
            try:
                obj = obj.parent()
            except RuntimeError:
                return False
        return False

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonPress and self.isVisible():
            global_position = getattr(event, "globalPosition", lambda: None)()
            point = global_position.toPoint() if global_position is not None else None
            inside_menu = point is not None and self.frameGeometry().contains(point)
            inside_anchor = False
            if point is not None and self._anchor is not None:
                try:
                    anchor_rect = self._anchor.rect()
                    anchor_rect.moveTopLeft(self._anchor.mapToGlobal(QPoint()))
                    inside_anchor = anchor_rect.contains(point)
                except RuntimeError:
                    pass
            if (
                not inside_menu
                and not inside_anchor
                and not self._belongs_to(watched, self)
                and not self._belongs_to(watched, self._anchor)
            ):
                self._hideMenu(True)
        if watched in self._anchor_watchers and event.type() in {
            QEvent.Type.Move,
            QEvent.Type.Resize,
            QEvent.Type.Show,
        }:
            self._follow_anchor()
        return super().eventFilter(watched, event)

    def event(self, e):
        result = super().event(e)
        if e.type() == QEvent.Type.WindowActivate:
            self._was_activated = True
        elif e.type() == QEvent.Type.WindowDeactivate and self._was_activated:
            self._hideMenu(True)
        return result

    def mousePressEvent(self, e):
        pass

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self._hideMenu(True)
            return
        super().keyPressEvent(e)

    def closeEvent(self, event):
        self._loading_bar.stop()
        self._detach_anchor()
        _stop_popup_animation(self)
        super().closeEvent(event)


class FontComboBox(TopLevelComboBox):
    """Fluent font selector; enumerate the full catalog only when opened."""

    currentFontChanged = pyqtSignal(QFont)
    fontPreviewChanged = pyqtSignal(str)

    def __init__(self, parent=None, locale_getter: Callable[[], str] | None = None):
        self._locale_getter = locale_getter
        self._cached_locale_code: str | None = None
        self._include_system_fonts = _SYSTEM_FONTS_ENABLED
        self._font_search_terms: dict[str, str] = {}
        self._catalog_loaded = False
        self._catalog_key = None
        self._catalog_service = None
        self._font_selection_aliases = {}
        super().__init__(parent)
        self._close_popup_on_release = False
        self._popup_close_click_timer = QTimer(self)
        self._popup_close_click_timer.setInterval(20)
        self._popup_close_click_timer.timeout.connect(self._clear_finished_close_click)
        _FONT_COMBO_INSTANCES.add(self)
        self.currentIndexChanged.connect(self._emit_current_font_changed)
        self.refresh(QFont().family())

    def _createComboMenu(self):
        locale_code = self._locale_code()
        entries = [
            (
                item.text,
                str(item.userData or item.text),
                self._font_search_terms.get(str(item.userData), _search_key(item.text)),
            )
            for item in self.items
        ]
        menu = _FontComboBoxMenu(
            entries,
            _FONT_SEARCH_PLACEHOLDERS.get(
                locale_code, _FONT_SEARCH_PLACEHOLDERS["en_US"]
            ),
            self._popup_parent(),
        )
        menu.fontHovered.connect(self.fontPreviewChanged)
        menu.fontSelected.connect(self._on_menu_font_selected)
        menu.closedSignal.connect(lambda: self.fontPreviewChanged.emit(""))
        menu.adjustSize()
        return menu

    def _showComboMenu(self):
        menu = self._createComboMenu()
        menu.setMaxVisibleItems(self.maxVisibleItems())
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu
        menu.view.setCurrentRow(self.currentIndex())
        menu.bind_to_anchor(self)
        self._request_catalog()
        menu.show_for_anchor()

    def _menu_entries(self):
        return [
            (item.text, str(item.userData or item.text),
             self._font_search_terms.get(str(item.userData), _search_key(item.text)))
            for item in self.items
        ]

    def _request_catalog(self):
        if self._catalog_service is None:
            self._catalog_service = _font_catalog_service()
            self._catalog_service.ready.connect(self._on_catalog_ready)
            self._catalog_service.progress.connect(self._on_catalog_progress)
            self._catalog_service.invalidated.connect(self._on_catalog_invalidated)
        self._catalog_loaded = False
        menu = self.dropMenu
        if menu is not None:
            menu.set_loading(_FONT_LOADING_TEXTS.get(self._locale_code(), _FONT_LOADING_TEXTS["en_US"]))
        self._catalog_key = self._catalog_service.request(self._locale_code(), self._include_system_fonts)

    def _on_catalog_progress(self, key, done, total):
        if key == self._catalog_key and self.dropMenu is not None:
            text = _FONT_LOADING_TEXTS.get(self._locale_code(), _FONT_LOADING_TEXTS["en_US"])
            self.dropMenu.status_label.setText(f"{text} {done}/{total}")

    def _on_catalog_ready(self, key, entries, error):
        if key != self._catalog_key or entries is None:
            return
        if error:
            if self.dropMenu is not None:
                self.dropMenu.set_loading(
                    _FONT_ERROR_TEXTS.get(self._locale_code(), _FONT_ERROR_TEXTS["en_US"]), error=True,
                )
            return
        blocker = QSignalBlocker(self)
        try:
            current = self.currentFamily()
            populate_font_combo(self, current, self._locale_code(), entries=entries)
            if not current:
                self.setCurrentIndex(-1)
            self._catalog_loaded = True
        finally:
            del blocker
        if self.dropMenu is not None:
            self.dropMenu.set_entries(self._menu_entries(), self.currentIndex())

    def _on_catalog_invalidated(self, generation):
        # A request may already use the latest generation before this coalesced
        # notification reaches the GUI. Do not cancel that fresh subscription.
        if self._catalog_key is not None and self._catalog_key[2] == generation:
            return
        reload_pending = self._catalog_key is not None and not self._catalog_loaded
        self._catalog_loaded = False
        self._catalog_key = None
        self._font_selection_aliases = {}
        if self.dropMenu is not None or reload_pending:
            self._request_catalog()

    def _onDropMenuClosed(self):
        # Never retain a deleted WA_DeleteOnClose menu for a worker callback.
        self.dropMenu = None
        if _left_mouse_button_down() and self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self._remember_popup_close_click()

    def _remember_popup_close_click(self):
        # Preserve the gesture, not the soon-to-be-deleted popup wrapper.
        self._close_popup_on_release = True
        self._popup_close_click_timer.start()

    def _clear_finished_close_click(self):
        if not _left_mouse_button_down():
            self._close_popup_on_release = False
            self._popup_close_click_timer.stop()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.dropMenu is not None:
            self._remember_popup_close_click()
        super().mousePressEvent(event)

    def _toggleComboMenu(self):
        if self._close_popup_on_release:
            self._close_popup_on_release = False
            self._popup_close_click_timer.stop()
            self._closeComboMenu()
            return
        super()._toggleComboMenu()

    def _on_menu_font_selected(self, row: int):
        if 0 <= row < self.count():
            self._onItemClicked(row)

    def refresh(
        self,
        current_family: str | None = None,
        force: bool = False,
        *,
        load_catalog: bool = False,
    ) -> None:
        if force:
            _clear_font_catalog_caches()
            self._catalog_loaded = False
            self._catalog_key = None
        family = (
            self.currentFamily()
            if current_family is None
            else str(current_family or "")
        )
        blocker = QSignalBlocker(self)
        try:
            if not self._catalog_loaded:
                # Hidden editor/settings controls only need their saved value.
                # Register project fonts for previewing that value, but avoid
                # enumerating and hashing every installed font on startup.
                list_font_files()
                self.clear()
                self._font_search_terms = {}
                self._font_selection_aliases = {}
                self.setCurrentFamily(family)
            else:
                self.setCurrentFamily(family)
            if not family:
                self.setCurrentIndex(-1)
        finally:
            del blocker
        if load_catalog or self.dropMenu is not None:
            self._request_catalog()

    def refresh_ui_texts(self) -> None:
        self._cached_locale_code = None
        self._catalog_loaded = False
        self._catalog_key = None
        self.refresh()

    def set_include_system_fonts(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._include_system_fonts == enabled:
            return
        current = self.currentFamily()
        self._include_system_fonts = enabled
        self._catalog_loaded = False
        self._catalog_key = None
        self.refresh(current)

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Font selection is explicit: scrolling never changes the family."""
        event.ignore()

    def _locale_code(self) -> str:
        if self._cached_locale_code is not None:
            return self._cached_locale_code
        locale_code = ""
        if self._locale_getter is not None:
            try:
                locale_code = str(self._locale_getter() or "")
            except RuntimeError:
                pass
        self._cached_locale_code = locale_code or QLocale.system().name()
        return self._cached_locale_code

    def currentFamily(self) -> str:
        return str(self.currentData() or self.currentText() or "")

    def setCurrentIndex(self, index):
        # Fluent's base combo ignores -1, while font fields use it to mean
        # inherit/default. Loading a catalog must not choose a font implicitly.
        if index == -1:
            if self._currentIndex != -1:
                self._currentIndex = -1
                self.setText("")
                self.currentTextChanged.emit("")
                self.currentIndexChanged.emit(-1)
            return
        super().setCurrentIndex(index)

    def setCurrentFamily(self, family: str) -> None:
        value = str(family or "")
        if not value:
            self.setCurrentIndex(-1)
            return
        value = self._font_selection_aliases.get(_selection_key(value), value)
        index = self.findData(value)
        if index < 0:
            family_name, style = split_font_value(value)
            display, aliases = _original_font_display_name(family_name), (family_name,)
            if style:
                display = f"{display} - {style}"
            self.addItem(display, userData=value)
            self._font_search_terms[value] = _search_key(
                " ".join((display, *aliases, style))
            )
            index = self.count() - 1
        self.setCurrentIndex(index)

    def currentFont(self) -> QFont:
        return qfont_for_value(self.currentFamily())

    def setCurrentFont(self, font: QFont) -> None:
        self.setCurrentFamily(font_value(font.family(), font.styleName()))

    def _emit_current_font_changed(self, _index: int) -> None:
        self.currentFontChanged.emit(self.currentFont())


def set_system_fonts_enabled(enabled: bool) -> None:
    """Update the shared font source policy and refresh existing selectors."""
    global _SYSTEM_FONTS_ENABLED
    enabled = bool(enabled)
    _SYSTEM_FONTS_ENABLED = enabled
    for combo in list(_FONT_COMBO_INSTANCES):
        try:
            combo.set_include_system_fonts(enabled)
        except RuntimeError:
            continue
