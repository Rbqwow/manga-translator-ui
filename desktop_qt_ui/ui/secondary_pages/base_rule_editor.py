"""Base visual YAML rule list editor for three-group (common/horizontal/vertical) rules."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

import yaml
from PyQt6.QtCore import QItemSelectionModel, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontDatabase, QSyntaxHighlighter, QTextCharFormat
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    FluentIcon as FIF,
    LineEdit as FluentLineEdit,
    IndeterminateProgressBar,
    PlainTextEdit,
    PopUpAniStackedWidget,
    PushButton,
    SegmentedWidget,
    SimpleCardWidget,
    TableWidget,
    ToolButton,
)
from ui.secondary_pages.themed_message_box import themed_question, themed_warning
from qfluentwidgets.components.widgets.table_view import TableItemDelegate

_RULE_READ_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rule-reader")


def _rule_file_signature(path):
    stat = os.stat(path)
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class _RuleTableDelegate(TableItemDelegate):
    """Paint status symbols as icons, avoiding expensive font fallback for ✓/✗."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if index.column() in (0, 3) and option.text in ("✓", "✗"):
            option.icon = (FIF.ACCEPT if option.text == "✓" else FIF.CLOSE).icon()
            option.text = ""
            option.decorationSize = QSize(14, 14)
            option.decorationAlignment = Qt.AlignmentFlag.AlignCenter
            option.features &= ~option.ViewItemFeature.HasDisplay
            option.features |= option.ViewItemFeature.HasDecoration


def _fixed_width_font(size: int = 11) -> QFont:
    try:
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    except Exception:
        font = QFont("Consolas")
    font.setPointSize(size)
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


class YamlHighlighter(QSyntaxHighlighter):
    """简单的 YAML 语法高亮"""

    def highlightBlock(self, text: str):
        if text.lstrip().startswith("#"):
            fmt = QTextCharFormat()
            fmt.setFontItalic(True)
            self.setFormat(0, len(text), fmt)
            return

        colon_idx = text.find(":")
        if colon_idx > 0 and not text.lstrip().startswith("-"):
            fmt = QTextCharFormat()
            fmt.setFontWeight(QFont.Weight.Bold)
            self.setFormat(0, colon_idx, fmt)


class _UniqueKeyYamlLoader(yaml.SafeLoader):
    """自定义 SafeLoader，检测并拒绝直接声明的重复映射键，同时保留 YAML 合并覆盖（<<: *anchor）的合法语义。"""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen_keys = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=deep)
                if key in seen_keys:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key '{key}'",
                        key_node.start_mark,
                    )
                seen_keys.add(key)
        return super().construct_mapping(node, deep=deep)


class BaseYamlRuleEditorPanel(CardWidget):
    """通用 YAML 规则编辑面板基类，支持通用/横排/竖排三分组与表格/源码双模式。"""

    data_changed = pyqtSignal()
    _load_ready = pyqtSignal(int, object, str)
    _switch_save_ready = pyqtSignal(int, object, str)
    _AUTOSAVE_DELAY_MS = 600
    COL_ENABLED, COL_PATTERN, COL_MIDDLE, COL_REGEX, COL_COMMENT = range(5)
    COL_COUNT = 5
    _YES, _NO = "✓", "✗"

    GROUPS = (
        ("common", "Common (Always)"),
        ("horizontal", "Horizontal"),
        ("vertical", "Vertical"),
    )

    @classmethod
    def _safe_load_yaml(cls, raw_text: str) -> dict:
        """统一 YAML 解析：确保根节点为 dict，空内容返回空字典，拒绝重复键与非 dict 根节点"""
        raw = yaml.load(raw_text, Loader=_UniqueKeyYamlLoader)
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ValueError("YAML root must be a dict")
        return raw

    @classmethod
    def _validate_rule_data(cls, data: dict) -> None:
        for group, _label in cls.GROUPS:
            rules = data.get(group, [])
            if not isinstance(rules, list):
                raise ValueError(f"Rule group '{group}' must be a list")
            for row, rule in enumerate(rules, 1):
                if not isinstance(rule, dict):
                    raise ValueError(f"{group}[{row}]: rule must be a mapping")
                try:
                    cls._validate_rule(rule)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{group}[{row}]: {exc}") from exc

    @classmethod
    def _validate_rule(cls, rule: dict) -> None:
        """Validate subclass data before constructing widgets or saving YAML."""

    def __init__(self, t_func: Optional[Callable] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._t = t_func or (lambda value, **kwargs: value.format(**kwargs) if kwargs else value)
        self._file_path = self._get_file_path()
        self._modified = False
        self._raw_mode = False
        self._mode_route = "table_view"
        self._current_group = "common"
        self._loading = False
        self._load_error = ""
        self._failed_raw_content = None
        self._load_generation = 0
        self._load_future = None
        # Kept apart from `_load_future`: a refresh may supersede a pending
        # read, but it must never cancel the write that persists edits the user
        # already made while switching from source view back to the table.
        self._save_future = None
        self._edit_revision = 0
        self._load_revision = 0
        self._switching_to_table = False
        self._saving_table_switch = False
        self._last_loaded_signature = None
        self._raw_content = ""
        self._pending_rows = None
        self._pending_stack = None
        self._pending_tables = {}
        self._populate_timer = QTimer(self)
        self._populate_timer.timeout.connect(self._populate_next_rows)
        self._load_ready.connect(self._on_load_ready, type=Qt.ConnectionType.QueuedConnection)
        self._switch_save_ready.connect(self._on_switch_save_ready, type=Qt.ConnectionType.QueuedConnection)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_auto_save_timeout)

        self._setup_ui()
        self._init_backward_compatibility_aliases()
        self._load_data()

    # ─── 抽象与定制接口（子类实现） ───

    def _get_file_path(self) -> str:
        raise NotImplementedError

    def _get_header_labels(self) -> List[str]:
        raise NotImplementedError

    def _get_filter_placeholder(self) -> str:
        return self._t("Type to filter...")

    def _get_restore_confirm_message(self) -> str:
        return self._t("Restore rules to built-in defaults? Current custom rules will be overwritten.")

    def _do_restore_default(self) -> None:
        raise NotImplementedError

    @staticmethod
    def _invalidate_cache_for_path(path: str) -> None:
        """Invalidate runtime data without accessing widgets (also used by workers)."""
        pass

    def _create_rule_template(self) -> dict:
        return {
            "enabled": True,
            "pattern": "",
            "regex": False,
            "comment": "",
        }

    def _populate_row(self, table: TableWidget, row: int, rule: dict) -> None:
        raise NotImplementedError

    def _extract_row_data(self, table: TableWidget, row: int) -> Optional[dict]:
        raise NotImplementedError

    def _get_row_filter_haystack(self, table: TableWidget, row: int) -> str:
        items = []
        for col in range(table.columnCount()):
            item = table.item(row, col)
            if item and item.text():
                items.append(item.text())
        return " ".join(items).lower()

    def _on_table_double_click(self, table: TableWidget, row: int, column: int) -> None:
        pass

    def _init_extra_toolbar_buttons_before_toggles(self, bar: QHBoxLayout) -> None:
        pass

    def _init_extra_toolbar_buttons_after_toggles(self, bar: QHBoxLayout) -> None:
        pass

    def _init_extra_filter_widgets(self, filter_layout: QHBoxLayout) -> None:
        pass

    def _on_selection_changed_extra(self, selected_rows: List[int]) -> None:
        pass

    def _refresh_ui_texts_extra(self) -> None:
        pass

    def _apply_theme_extra(self) -> None:
        pass

    # ─── UI 构建 ───

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # 1. 顶部工具栏
        toolbar = SimpleCardWidget(self)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 8, 10, 8)
        toolbar_layout.setSpacing(8)

        self.add_button = PushButton(self._t("Add Rule"), icon=FIF.ADD)
        self.delete_button = PushButton(self._t("Delete"), icon=FIF.DELETE)
        self.up_button = ToolButton(FIF.UP, self)
        self.up_button.setToolTip(self._t("Move Up"))
        self.down_button = ToolButton(FIF.DOWN, self)
        self.down_button.setToolTip(self._t("Move Down"))

        self.select_all_button = PushButton(self._t("Select All"), icon=FIF.CHECKBOX)

        toolbar_layout.addWidget(self.add_button)
        toolbar_layout.addWidget(self.delete_button)
        toolbar_layout.addWidget(self.up_button)
        toolbar_layout.addWidget(self.down_button)
        toolbar_layout.addWidget(self.select_all_button)

        self._init_extra_toolbar_buttons_before_toggles(toolbar_layout)

        self.toggle_enabled_button = PushButton(self._t("Enable"), icon=FIF.ACCEPT)
        self.toggle_regex_button = PushButton(self._t("Regex"), icon=FIF.CODE)
        toolbar_layout.addWidget(self.toggle_enabled_button)
        toolbar_layout.addWidget(self.toggle_regex_button)

        self._init_extra_toolbar_buttons_after_toggles(toolbar_layout)

        toolbar_layout.addStretch()
        self.restore_button = PushButton(self._t("Restore Default"), icon=FIF.SYNC)
        toolbar_layout.addWidget(self.restore_button)
        layout.addWidget(toolbar)
        self._toolbar = toolbar

        # 2. 搜索过滤栏
        filter_card = SimpleCardWidget(self)
        filter_layout = QHBoxLayout(filter_card)
        filter_layout.setContentsMargins(10, 8, 10, 8)
        filter_layout.setSpacing(8)

        self.filter_label = CaptionLabel(self._t("Filter:"))
        self.search = FluentLineEdit()
        self.search.setPlaceholderText(self._get_filter_placeholder())
        self.search.setClearButtonEnabled(True)

        filter_layout.addWidget(self.filter_label)
        filter_layout.addWidget(self.search, 1)
        self._init_extra_filter_widgets(filter_layout)
        layout.addWidget(filter_card)
        self.filter_card = filter_card

        # 3. 双模式切换容器
        self.mode_segment = SegmentedWidget(self)
        self.mode_stack = PopUpAniStackedWidget(self)
        self._mode_pages: Dict[str, QWidget] = {}

        # 模式1: 表格模式
        table_container = SimpleCardWidget(self.mode_stack)
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(10, 10, 10, 10)
        table_layout.setSpacing(8)

        self.group_segment = SegmentedWidget(table_container)
        self.group_stack = PopUpAniStackedWidget(table_container)
        self.tables: Dict[str, TableWidget] = {}

        for group_key, group_label in self.GROUPS:
            table = self._create_table()
            self.tables[group_key] = table
            self.group_stack.addWidget(table)
            self.group_segment.addItem(
                group_key,
                self._t(group_label),
                onClick=lambda checked=False, k=group_key: self._set_group(k),
            )

        table_layout.addWidget(self.group_segment)
        table_layout.addWidget(self.group_stack, 1)
        self.mode_stack.addWidget(table_container)
        self._mode_pages["table_view"] = table_container

        # 模式2: 原始 YAML 源码模式
        raw_container = SimpleCardWidget(self.mode_stack)
        raw_layout = QVBoxLayout(raw_container)
        raw_layout.setContentsMargins(10, 10, 10, 10)
        raw_layout.setSpacing(8)

        self.raw_hint = CaptionLabel(
            self._t("Edit raw YAML content directly. Changes are saved automatically.")
        )
        self.raw_hint.setWordWrap(True)
        self.raw_editor = PlainTextEdit()
        self.raw_editor.setFont(_fixed_width_font(10))
        self.raw_editor.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)
        self.highlighter = YamlHighlighter(self.raw_editor.document())

        raw_layout.addWidget(self.raw_hint)
        raw_layout.addWidget(self.raw_editor, 1)
        self.mode_stack.addWidget(raw_container)
        self._mode_pages["raw_edit"] = raw_container

        self.mode_segment.addItem(
            "table_view",
            self._t("Table View"),
            onClick=lambda checked=False: self._set_mode("table_view"),
        )
        self.mode_segment.addItem(
            "raw_edit",
            self._t("Raw Edit"),
            onClick=lambda checked=False: self._set_mode("raw_edit"),
        )
        layout.addWidget(self.mode_segment)
        layout.addWidget(self.mode_stack, 1)

        # 4. 底部状态栏
        self.status = CaptionLabel("")
        self._loading_bar = IndeterminateProgressBar(self, start=False)
        self._loading_bar.hide()
        layout.addWidget(self._loading_bar)
        layout.addWidget(self.status)

        self._set_group("common")
        self._show_mode_page("table_view")
        self.mode_segment.setCurrentItem("table_view")

        # 信号连接
        self.add_button.clicked.connect(self._on_add_rule)
        self.delete_button.clicked.connect(self._on_delete_rule)
        self.up_button.clicked.connect(lambda: self._on_move_rule(-1))
        self.down_button.clicked.connect(lambda: self._on_move_rule(1))
        self.select_all_button.clicked.connect(self._on_select_all)
        self.toggle_enabled_button.clicked.connect(self._on_toggle_enabled)
        self.toggle_regex_button.clicked.connect(self._on_toggle_regex)
        self.restore_button.clicked.connect(self._on_restore_default)
        self.search.textChanged.connect(self._on_search_changed)
        self.raw_editor.textChanged.connect(self._on_raw_changed)

    def _init_backward_compatibility_aliases(self) -> None:
        """为保持历史调用/访问兼容提供的属性别名"""
        self._add_button = self.add_button
        self._delete_button = self.delete_button
        self._move_up_button = self.up_button
        self._move_down_button = self.down_button
        self._select_all_button = self.select_all_button
        self._toggle_enabled_button = self.toggle_enabled_button
        self._toggle_regex_button = self.toggle_regex_button
        self._restore_default_button = self.restore_button
        self._search_input = self.search
        self._search_label = self.filter_label
        self._tables = self.tables
        self._raw_editor = self.raw_editor
        self._status_label = self.status
        self._auto_save_timer = self._timer
        self._filter_row = self.filter_card
        self._mode_segmented = self.mode_segment
        self._mode_stack = self.mode_stack
        self._group_segmented = self.group_segment
        self._group_stack = self.group_stack

    # ─── 表格创建与基本事件 ───

    def _create_table(self) -> TableWidget:
        table = TableWidget()
        table.setItemDelegate(_RuleTableDelegate(table))
        table.setColumnCount(self.COL_COUNT)
        table.setHorizontalHeaderLabels(self._get_header_labels())
        table.setSelectionBehavior(TableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(TableWidget.SelectionMode.ExtendedSelection)
        header = table.horizontalHeader()
        header.setSectionResizeMode(self.COL_ENABLED, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_PATTERN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_MIDDLE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_REGEX, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_COMMENT, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(self.COL_ENABLED, 55)
        table.setColumnWidth(self.COL_REGEX, 55)

        table.cellChanged.connect(self._on_cell_changed)
        table.cellDoubleClicked.connect(self._handle_cell_double_clicked)
        table.itemSelectionChanged.connect(self._on_selection_changed)
        return table

    def _current_table(self) -> TableWidget:
        return self.tables.get(self._current_group, self.tables["common"])

    def _current_group_key(self) -> str:
        return self._current_group

    def _set_group(self, group_key: str) -> None:
        if group_key not in self.tables:
            return
        self._current_group = group_key
        self.group_stack.setCurrentWidget(self.tables[group_key])
        self.group_segment.setCurrentItem(group_key)
        self._apply_filter(self._current_table(), self.search.text())
        self._on_selection_changed()

    def _show_mode_page(self, mode_key: str) -> None:
        page = self._mode_pages.get(mode_key)
        if page:
            self.mode_stack.setCurrentWidget(page)

    def _is_raw_mode(self) -> bool:
        return self._raw_mode

    def _set_table_controls_enabled(self, enabled: bool) -> None:
        for btn in (
            self.add_button,
            self.delete_button,
            self.up_button,
            self.down_button,
            self.select_all_button,
            self.toggle_enabled_button,
            self.toggle_regex_button,
        ):
            btn.setEnabled(enabled)
        self.filter_card.setVisible(enabled)

    def _set_mode(self, route_key: str) -> None:
        raw = (route_key in ("raw_edit", "raw", True))
        target_route = "raw_edit" if raw else "table_view"
        if target_route == self._mode_route:
            self.mode_segment.setCurrentItem(target_route)
            return

        if raw:
            self._enter_raw_mode()
        else:
            self._enter_table_mode()

    def _enter_raw_mode(self) -> None:
        if self._loading:
            return
        if self._modified:
            self._save_current_content(show_errors=False)
        yaml_content = self._raw_content if not self._modified else self._tables_to_yaml()
        if self._load_error and self._failed_raw_content is not None:
            yaml_content = self._failed_raw_content
        self.raw_editor.blockSignals(True)
        self.raw_editor.setPlainText(yaml_content)
        self.raw_editor.blockSignals(False)

        self._raw_mode = True
        self._mode_route = "raw_edit"
        self._show_mode_page("raw_edit")
        self.mode_segment.setCurrentItem("raw_edit")
        self._set_table_controls_enabled(False)
        self._update_status()

    def _enter_table_mode(self) -> None:
        if self._loading or not self._raw_mode:
            return
        # Keep the source visible until parsing, staged rows and any save succeed.
        self.mode_segment.setCurrentItem("raw_edit")
        self._timer.stop()
        self._load_data(raw_text=self.raw_editor.toPlainText())

    # ─── 数据存取与同步 ───

    def _set_loading(self, loading):
        self._loading = loading
        self._toolbar.setEnabled(not loading)
        self.mode_segment.setEnabled(not loading)
        self.group_segment.setEnabled(not loading)
        self.filter_card.setEnabled(not loading)
        self.raw_editor.setReadOnly(loading)
        for table in self.tables.values():
            table.setEnabled(not loading and not self._load_error)
        if self._load_error:
            self._set_table_controls_enabled(False)
        elif not loading:
            self._set_table_controls_enabled(not self._raw_mode)
        self._loading_bar.setVisible(loading)
        if loading:
            self._loading_bar.start()
            self.status.setText(self._t("web_loading") + "…")
        else:
            self._loading_bar.stop()

    def _discard_pending_tables(self):
        self._populate_timer.stop()
        self._pending_rows = None
        if self._pending_stack is not None:
            self._pending_stack.hide()
            self._pending_stack.deleteLater()
        self._pending_stack = None
        self._pending_tables = {}

    def _report_load_error(self, error, raw_content=None):
        self._discard_pending_tables()
        self._timer.stop()
        self._switching_to_table = False
        self._saving_table_switch = False
        self._load_error = error
        self._failed_raw_content = raw_content
        self._set_loading(False)
        self._update_status()

    def _prepare_pending_tables(self, data, *, validated=False):
        if not validated:
            self._validate_rule_data(data)
        self._discard_pending_tables()
        stack = PopUpAniStackedWidget(self.group_stack.parentWidget())
        self._pending_stack = stack
        stack.hide()
        stack.setAnimationEnabled(False)
        for group, _label in self.GROUPS:
            table = self._create_table()
            self._pending_tables[group] = table
            stack.addWidget(table)
            table.horizontalHeader().restoreState(self.tables[group].horizontalHeader().saveState())
            table.blockSignals(True)
            table.setUpdatesEnabled(False)
            table.setRowCount(len(data.get(group, [])))
        self._pending_rows = iter(
            (self._pending_tables[group], row, rule)
            for group, _label in self.GROUPS
            for row, rule in enumerate(data.get(group, []))
        )

    def _commit_pending_tables(self):
        old_stack = self.group_stack
        new_stack = self._pending_stack
        old_stack.parentWidget().layout().replaceWidget(old_stack, new_stack)
        self.tables.update(self._pending_tables)
        self.group_stack = self._group_stack = new_stack
        for table in self.tables.values():
            table.blockSignals(False)
            table.setUpdatesEnabled(True)
        new_stack.setCurrentWidget(self._current_table())
        new_stack.setAnimationEnabled(True)
        new_stack.show()
        old_stack.hide()
        old_stack.deleteLater()
        self._pending_stack = None
        self._pending_tables = {}
        self._pending_rows = None
        self._load_error = ""
        self._failed_raw_content = None

    def _load_data(self, force=False, *, raw_text=None) -> None:
        if self._loading and (not force or self._saving_table_switch):
            return
        self._load_generation += 1
        generation = self._load_generation
        if self._load_future is not None:
            self._load_future.cancel()
        # A pending switch-save is deliberately left running: its result is
        # dropped by the generation check, but the file still gets written. The
        # flag is cleared here so a superseded save cannot leave the panel
        # stuck in its "saving" state.
        self._saving_table_switch = False
        self._discard_pending_tables()
        if force:
            self._modified = False
        self._switching_to_table = raw_text is not None
        self._load_revision = self._edit_revision
        self._set_loading(True)
        path = self._file_path
        previous = None if force or self._load_error else self._last_loaded_signature
        parse = type(self)._safe_load_yaml
        validate = type(self)._validate_rule_data

        def read():
            if raw_text is None:
                signature = _rule_file_signature(path)
                if signature == previous:
                    return (None, None, signature), ""
                with open(path, "r", encoding="utf-8") as stream:
                    raw = stream.read()
            else:
                raw, signature = raw_text, previous
            try:
                data = parse(raw)
                validate(data)
            except Exception as exc:
                return (raw, None, signature), str(exc)
            return (raw, data, signature), ""

        self._submit_rule_job(read, "_load_ready", generation)

    def _submit_rule_job(self, work, signal_name, generation, *, saving=False):
        reference = weakref.ref(self)

        def done(future):
            if future.cancelled():
                return
            try:
                result, error = future.result()
            except Exception as exc:
                result, error = None, str(exc)
            panel = reference()
            if panel is not None:
                try:
                    getattr(panel, signal_name).emit(generation, result, error)
                except RuntimeError:
                    pass

        future = _RULE_READ_EXECUTOR.submit(work)
        if saving:
            self._save_future = future
        else:
            self._load_future = future
        future.add_done_callback(done)

    def _discard_obsolete_load(self):
        if self._edit_revision == self._load_revision and (
            self._switching_to_table or not self._modified
        ):
            return False
        self._discard_pending_tables()
        self._switching_to_table = False
        self._set_loading(False)
        self._update_status()
        if self._modified:
            self._timer.start(self._AUTOSAVE_DELAY_MS)
        return True

    def _on_load_ready(self, generation, result, error):
        if generation != self._load_generation:
            return
        if self._discard_obsolete_load():
            return
        if error:
            self._report_load_error(error, result[0] if result else None)
            return
        raw, data, signature = result
        if data is None:
            self._set_loading(False)
            self._update_status()
            return
        self._pending_content = (raw, signature)
        try:
            self._prepare_pending_tables(data, validated=True)
        except Exception as exc:
            self._report_load_error(str(exc), raw)
            return
        self._populate_timer.start(0)

    def _populate_next_rows(self):
        if self._discard_obsolete_load():
            return
        deadline = time.perf_counter() + 0.008
        for _ in range(40):
            try:
                table, row, rule = next(self._pending_rows)
            except StopIteration:
                self._populate_timer.stop()
                if self._switching_to_table and self._modified:
                    self._save_table_switch()
                else:
                    self._finish_table_population()
                return
            try:
                self._populate_row(table, row, rule)
            except Exception as exc:
                self._report_load_error(str(exc), self._pending_content[0])
                return
            if time.perf_counter() >= deadline:
                break

    def _save_table_switch(self):
        self._saving_table_switch = True
        path, raw = self._file_path, self._pending_content[0]
        write = type(self)._write_rule_snapshot

        def save():
            return write(path, raw), ""

        self._submit_rule_job(
            save, "_switch_save_ready", self._load_generation, saving=True,
        )

    def _on_switch_save_ready(self, generation, result, error):
        if generation != self._load_generation:
            return
        self._saving_table_switch = False
        if self._discard_obsolete_load():
            return
        if error:
            self._report_load_error(error, self._pending_content[0])
            return
        self._pending_content = result
        self._finish_table_population()

    def _finish_table_population(self):
        self._commit_pending_tables()
        self._raw_content, self._last_loaded_signature = self._pending_content
        if self._switching_to_table:
            self._switching_to_table = False
            self._raw_mode = False
            self._mode_route = "table_view"
            self._show_mode_page("table_view")
            self.mode_segment.setCurrentItem("table_view")
        elif self._raw_mode:
            self.raw_editor.blockSignals(True)
            self.raw_editor.setPlainText(self._raw_content)
            self.raw_editor.blockSignals(False)
        self._timer.stop()
        self._modified = False
        self._set_loading(False)
        self._apply_filter(self._current_table(), self.search.text())
        self._update_status()

    def _insert_rule_to_table(self, table: TableWidget, rule: dict, row: Optional[int] = None) -> int:
        r = table.rowCount() if row is None else row
        table.insertRow(r)
        self._populate_row(table, r, rule)
        return r

    def _table_data(self) -> dict:
        data = {}
        for group_key, table in self.tables.items():
            rules = []
            for row in range(table.rowCount()):
                item = self._extract_row_data(table, row)
                if item is not None:
                    rules.append(item)
            data[group_key] = rules
        return data

    def _tables_to_yaml(self) -> str:
        return yaml.dump(self._table_data(), allow_unicode=True, default_flow_style=False, sort_keys=False)

    def _on_cell_changed(self, row: int, col: int) -> None:
        self._mark_modified()

    def _on_raw_changed(self) -> None:
        self._mark_modified()

    def _mark_modified(self) -> None:
        if self._load_error and not self._raw_mode:
            self._update_status()
            return
        self._modified = True
        self._edit_revision += 1
        self.status.setText(self._t("Saving..."))
        self._timer.start(self._AUTOSAVE_DELAY_MS)
        self._update_status()
        self.data_changed.emit()

    def _on_auto_save_timeout(self) -> None:
        if self._modified:
            self._save_current_content(show_errors=False)

    def _save_current_content(self, show_errors: bool = False) -> bool:
        if self._loading or (self._load_error and not self._raw_mode):
            self._update_status()
            return False
        if self._raw_mode:
            return self._save_raw_content(self.raw_editor.toPlainText(), show_errors=show_errors)
        return self._write_content(self._tables_to_yaml(), show_errors=show_errors)

    def _save_raw_content(self, raw_text: str, show_errors: bool = False) -> bool:
        try:
            self._validate_rule_data(self._safe_load_yaml(raw_text))
        except Exception as exc:
            msg = self._t("YAML syntax error, changes not saved.")
            if show_errors:
                themed_warning(self, self._t("Save Error"), msg + f"\n\n{exc}")
            else:
                self.status.setText(msg)
            return False
        return self._write_content(raw_text, show_errors=show_errors)

    @classmethod
    def _write_rule_snapshot(cls, path: str, content: str):
        """Persist a validated snapshot atomically; no Qt objects on this path."""
        content = content.rstrip() + "\n"
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=directory,
                prefix=".rules-", suffix=".tmp", delete=False,
            ) as stream:
                temp_path = stream.name
                stream.write(content)
            os.replace(temp_path, path)
        finally:
            if temp_path is not None and os.path.exists(temp_path):
                os.remove(temp_path)
        cls._invalidate_cache_for_path(path)
        return content, _rule_file_signature(path)

    def _write_content(self, content: str, show_errors: bool = False) -> bool:
        try:
            self._raw_content, self._last_loaded_signature = self._write_rule_snapshot(self._file_path, content)
            self._timer.stop()
            self._modified = False
            self._load_error = ""
            self._failed_raw_content = None
            self.status.setText(self._t("All changes saved"))
            return True
        except Exception as exc:
            msg = f"{self._t('Save error')}: {exc}"
            if show_errors:
                themed_warning(self, self._t("Save Error"), msg)
            else:
                self.status.setText(msg)
            return False

    # ─── 操作逻辑（增、删、移、改） ───

    def _get_selected_rows(self) -> List[int]:
        table = self._current_table()
        return sorted({idx.row() for idx in table.selectedIndexes() if not table.isRowHidden(idx.row())})

    def _on_selection_changed(self) -> None:
        rows = self._get_selected_rows()
        table = self._current_table()
        visible_rows = [r for r in range(table.rowCount()) if not table.isRowHidden(r)]

        if visible_rows and set(visible_rows).issubset(set(rows)):
            self.select_all_button.setText(self._t("Deselect All"))
            self.select_all_button.setIcon(FIF.CLEAR_SELECTION)
        else:
            self.select_all_button.setText(self._t("Select All"))
            self.select_all_button.setIcon(FIF.CHECKBOX)

        if not rows:
            self.toggle_enabled_button.setText(self._t("Enable"))
            self.toggle_regex_button.setText(self._t("Regex"))
            self._on_selection_changed_extra([])
            self._update_status()
            return

        enabled_count = sum(
            1 for r in rows
            if table.item(r, self.COL_ENABLED) and table.item(r, self.COL_ENABLED).text() == self._YES
        )
        regex_count = sum(
            1 for r in rows
            if table.item(r, self.COL_REGEX) and table.item(r, self.COL_REGEX).text() == self._YES
        )

        if enabled_count > len(rows) // 2:
            self.toggle_enabled_button.setText(self._t("Disable"))
        else:
            self.toggle_enabled_button.setText(self._t("Enable"))

        if regex_count > len(rows) // 2:
            self.toggle_regex_button.setText(self._t("Cancel Regex"))
        else:
            self.toggle_regex_button.setText(self._t("Regex"))

        self._on_selection_changed_extra(rows)
        self._update_status()

    def _on_select_all(self) -> None:
        """切换全部选中 / 取消全选（只作用于当前可见行）"""
        if self._is_raw_mode():
            return
        table = self._current_table()
        visible_rows = [r for r in range(table.rowCount()) if not table.isRowHidden(r)]
        if not visible_rows:
            return

        selected_rows = self._get_selected_rows()
        if set(visible_rows).issubset(set(selected_rows)):
            table.clearSelection()
        else:
            table.blockSignals(True)
            table.clearSelection()
            sm = table.selectionModel()
            if sm is not None:
                for r in visible_rows:
                    sm.select(
                        table.model().index(r, 0),
                        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                    )
            else:
                for r in visible_rows:
                    for c in range(table.columnCount()):
                        item = table.item(r, c)
                        if item:
                            item.setSelected(True)
            table.blockSignals(False)

        self._on_selection_changed()

    def _on_add_rule(self) -> None:
        if self._raw_mode:
            return
        table = self._current_table()
        rule = self._create_rule_template()
        table.blockSignals(True)
        row = self._insert_rule_to_table(table, rule)
        table.blockSignals(False)
        table.selectRow(row)
        pattern_item = table.item(row, self.COL_PATTERN)
        if pattern_item:
            table.editItem(pattern_item)
        self._mark_modified()

    def _on_delete_rule(self) -> None:
        if self._raw_mode:
            return
        table = self._current_table()
        rows = sorted(self._get_selected_rows(), reverse=True)
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        if not rows:
            return
        table.blockSignals(True)
        for r in rows:
            table.removeRow(r)
        table.blockSignals(False)
        self._mark_modified()
        self._on_selection_changed()

    def _on_move_rule(self, delta: int) -> None:
        if self._raw_mode:
            return
        table = self._current_table()
        row = table.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= table.rowCount():
            return

        data_row = self._extract_row_data(table, row)
        data_target = self._extract_row_data(table, target)
        if data_row is None or data_target is None:
            return

        table.blockSignals(True)
        table.removeRow(max(row, target))
        table.removeRow(min(row, target))
        low = min(row, target)
        ordered = (data_row, data_target) if row < target else (data_target, data_row)
        self._insert_rule_to_table(table, ordered[1], low)
        self._insert_rule_to_table(table, ordered[0], low + 1)
        table.blockSignals(False)

        table.selectRow(target)
        self._mark_modified()

    def _on_toggle_enabled(self) -> None:
        if self._raw_mode:
            return
        rows = self._get_selected_rows()
        if not rows and self._current_table().currentRow() >= 0:
            rows = [self._current_table().currentRow()]
        if not rows:
            return
        table = self._current_table()
        target = self._YES if self.toggle_enabled_button.text() == self._t("Enable") else self._NO

        table.blockSignals(True)
        for r in rows:
            item = table.item(r, self.COL_ENABLED)
            if item:
                item.setText(target)
                self._set_row_dimmed(table, r, target == self._NO)
        table.blockSignals(False)
        self._mark_modified()
        self._on_selection_changed()

    def _on_toggle_regex(self) -> None:
        if self._raw_mode:
            return
        rows = self._get_selected_rows()
        if not rows and self._current_table().currentRow() >= 0:
            rows = [self._current_table().currentRow()]
        if not rows:
            return
        table = self._current_table()
        target = self._YES if self.toggle_regex_button.text() == self._t("Regex") else self._NO

        table.blockSignals(True)
        for r in rows:
            item = table.item(r, self.COL_REGEX)
            if item:
                item.setText(target)
        table.blockSignals(False)
        self._mark_modified()
        self._on_selection_changed()

    def _handle_cell_double_clicked(self, row: int, col: int) -> None:
        table = self._current_table()
        if col in (self.COL_ENABLED, self.COL_REGEX):
            item = table.item(row, col)
            if item:
                table.blockSignals(True)
                new_val = self._NO if item.text() == self._YES else self._YES
                item.setText(new_val)
                if col == self.COL_ENABLED:
                    self._set_row_dimmed(table, row, new_val == self._NO)
                table.blockSignals(False)
                self._mark_modified()
                self._on_selection_changed()
        else:
            self._on_table_double_click(table, row, col)

    def _set_row_dimmed(self, table: TableWidget, row: int, dimmed: bool) -> None:
        pass

    def _on_search_changed(self, text: str) -> None:
        if self._raw_mode:
            return
        self._apply_filter(self._current_table(), text)
        self._on_selection_changed()

    def _apply_filter(self, table: TableWidget, query: str) -> None:
        q = (query or "").strip().lower()
        for r in range(table.rowCount()):
            if not q:
                table.setRowHidden(r, False)
            else:
                haystack = self._get_row_filter_haystack(table, r)
                table.setRowHidden(r, bool(q not in haystack))

    def _on_restore_default(self) -> None:
        reply = themed_question(
            self,
            self._t("Restore Default"),
            self._get_restore_confirm_message(),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._timer.stop()
            self._do_restore_default()
            self._load_data(force=True)
            self._apply_filter(self._current_table(), self.search.text())
            self.status.setText(self._t("Defaults restored"))
            self.data_changed.emit()
        except Exception as exc:
            self.status.setText(f"{self._t('Restore default failed')}: {exc}")

    def _update_status(self) -> None:
        if self._loading:
            return
        if self._load_error:
            self.status.setText(f"{self._t('Load error')}: {self._load_error}")
            return
        group_key = self._current_group
        table = self.tables.get(group_key)
        if not table:
            return
        total = table.rowCount()
        enabled = sum(
            1 for r in range(total)
            if table.item(r, self.COL_ENABLED) and table.item(r, self.COL_ENABLED).text() == self._YES
        )
        selected_count = len(self._get_selected_rows())
        selected_info = ""
        if selected_count > 0:
            selected_fmt = self._t("{count} selected")
            selected_info = f" ({selected_fmt.replace('{count}', str(selected_count))})"
        modified_mark = " ●" if self._modified else ""
        mode = self._t("Raw Edit") if self._raw_mode else self._t("Table View")
        self.status.setText(
            f"{group_key}: {enabled}/{total} {self._t('enabled')}{selected_info}{modified_mark}  [{mode}]"
        )

    # ─── 公共接口 ───

    def refresh(self) -> None:
        if not self._modified:
            self._load_data()

    def apply_theme(self) -> None:
        if hasattr(self, "highlighter"):
            self.highlighter.rehighlight()
        self._apply_theme_extra()
        self.update()

    def refresh_ui_texts(self) -> None:
        self.add_button.setText(self._t("Add Rule"))
        self.delete_button.setText(self._t("Delete"))
        self.up_button.setToolTip(self._t("Move Up"))
        self.down_button.setToolTip(self._t("Move Down"))
        self.restore_button.setText(self._t("Restore Default"))
        self.filter_label.setText(self._t("Filter:"))
        self.search.setPlaceholderText(self._get_filter_placeholder())
        self.raw_hint.setText(
            self._t("Edit raw YAML content directly. Changes are saved automatically.")
        )
        self.mode_segment.setItemText("table_view", self._t("Table View"))
        self.mode_segment.setItemText("raw_edit", self._t("Raw Edit"))
        self.mode_segment.setCurrentItem("raw_edit" if self._raw_mode else "table_view")
        for key, label in self.GROUPS:
            self.group_segment.setItemText(key, self._t(label))
        header_labels = self._get_header_labels()
        for table in self.tables.values():
            table.setHorizontalHeaderLabels(header_labels)
        self._on_selection_changed()
        self._refresh_ui_texts_extra()
        self._update_status()
