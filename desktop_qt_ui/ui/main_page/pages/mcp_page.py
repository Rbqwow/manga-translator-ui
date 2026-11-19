"""MCP Service management Fluent UI page and live log viewer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, QUrl, pyqtSlot
from PyQt6.QtGui import QDesktopServices, QDoubleValidator, QIntValidator, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon as FIF,
    IconWidget,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    TitleLabel,
    isDarkTheme,
)

from services import get_config_service, get_mcp_service
from services.log_service import get_log_service
from ui.theme import monospace_font
from ui.widgets.toggle_switch import ToggleSwitch
from ui.widgets.wheel_filter import NoWheelComboBox, install_wheel_filter


class McpPagePanel(QWidget):
    """Fluent page hosting MCP server control, strategy settings, progress, and live logs."""

    _MAX_LOG_LINES = 1000

    def __init__(
        self,
        *,
        controller: Any,
        t_func: Callable[..., str],
        switch_page: Optional[Callable[[str], None]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.controller = controller
        self._t = t_func
        self._switch_page = switch_page
        self.config_service = get_config_service()
        self.mcp_service = get_mcp_service()
        self.log_service = get_log_service()

        self._syncing = False
        self._log_entries: List[Dict[str, Any]] = []
        self._auto_scroll = True

        self._build_ui()
        self._connect_signals()
        self._backfill_logs()
        self.sync_from_config()
        self.refresh_status()
        self.apply_theme()
        install_wheel_filter(self)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)

        header_card = CardWidget(self)
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(18, 14, 18, 14)
        header_layout.setSpacing(4)
        self.title_label = TitleLabel(self._t("MCP Service"), header_card)
        self.subtitle_label = BodyLabel(
            self._t("MCP Page Subtitle"),
            header_card,
        )
        self.subtitle_label.setWordWrap(True)
        header_layout.addWidget(self.title_label)
        header_layout.addWidget(self.subtitle_label)
        outer.addWidget(header_card)

        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(ScrollArea.Shape.NoFrame)
        scroll.enableTransparentBackground()

        container = QWidget(scroll)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 6, 0)
        container_layout.setSpacing(12)
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

        # 1. Service control & client integration card
        self._build_control_card(container_layout)
        # 2. MCP translation mode & strategy card
        self._build_strategy_card(container_layout)
        # 3. Active job & progress card
        self._build_progress_card(container_layout)
        # 4. Live logs card
        self._build_logs_card(container_layout)

    def _create_card_header(self, card: CardWidget, layout: QVBoxLayout, icon: Any, title_text: str) -> StrongBodyLabel:
        row = QHBoxLayout()
        row.setSpacing(10)
        icon_widget = IconWidget(icon, card)
        icon_widget.setFixedSize(20, 20)
        label = StrongBodyLabel(title_text, card)
        row.addWidget(icon_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(label, 1)
        layout.addLayout(row)
        return label

    def _build_control_card(self, parent_layout: QVBoxLayout) -> None:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        self.control_card_title = self._create_card_header(
            card, layout, FIF.CONNECT, self._t("MCP Control Card Title")
        )

        # Status & endpoint row
        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.status_dot = QFrame(card)
        self.status_dot.setFixedSize(10, 10)
        self.status_label = BodyLabel(self._t("MCP Status Stopped"), card)
        self.endpoint_label = CaptionLabel("http://127.0.0.1:8765/mcp", card)
        self.endpoint_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_url_button = PushButton(self._t("MCP Copy URL"), card)
        self.copy_url_button.setIcon(FIF.COPY)
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label)
        status_row.addWidget(self.endpoint_label, 1)
        status_row.addWidget(self.copy_url_button)
        layout.addLayout(status_row)

        # Port + Start/Stop + Auto-start
        ctrl_grid = QGridLayout()
        ctrl_grid.setHorizontalSpacing(12)
        ctrl_grid.setVerticalSpacing(10)

        self.port_label = BodyLabel(self._t("MCP Port Label"), card)
        self.port_edit = LineEdit(card)
        self.port_edit.setFixedWidth(110)
        self.port_edit.setValidator(QIntValidator(1024, 65535, self.port_edit))
        self.toggle_server_button = PrimaryPushButton(self._t("MCP Start Service"), card)
        self.toggle_server_button.setIcon(FIF.PLAY)

        ctrl_grid.addWidget(self.port_label, 0, 0)
        ctrl_grid.addWidget(self.port_edit, 0, 1)
        ctrl_grid.addWidget(self.toggle_server_button, 0, 2)
        ctrl_grid.setColumnStretch(3, 1)

        self.auto_start_label = BodyLabel(self._t("MCP Auto Start Label"), card)
        self.auto_start_hint = CaptionLabel(self._t("MCP Auto Start Hint"), card)
        self.auto_start_hint.setWordWrap(True)
        auto_col = QVBoxLayout()
        auto_col.setSpacing(2)
        auto_col.addWidget(self.auto_start_label)
        auto_col.addWidget(self.auto_start_hint)
        self.auto_start_switch = ToggleSwitch(card)

        ctrl_grid.addLayout(auto_col, 1, 0, 1, 3)
        ctrl_grid.addWidget(self.auto_start_switch, 1, 4, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(ctrl_grid)

        # Target client selector
        client_row = QHBoxLayout()
        client_row.setSpacing(10)
        self.client_label = BodyLabel(self._t("MCP Target Client Label"), card)
        self.client_combo = NoWheelComboBox(card)
        self.client_combo.setMinimumWidth(260)
        client_row.addWidget(self.client_label)
        client_row.addWidget(self.client_combo)
        client_row.addStretch(1)
        layout.addLayout(client_row)

        # Quick client setup buttons
        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        self.copy_config_button = PushButton(self._t("MCP Copy Config"), card)
        self.copy_config_button.setIcon(FIF.COPY)
        self.write_config_button = PushButton(self._t("MCP Write Client Config"), card)
        self.write_config_button.setIcon(FIF.SAVE)
        self.install_skill_button = PushButton(self._t("MCP Install Skill"), card)
        self.install_skill_button.setIcon(FIF.ROBOT)
        actions_row.addWidget(self.copy_config_button)
        actions_row.addWidget(self.write_config_button)
        actions_row.addWidget(self.install_skill_button)
        actions_row.addStretch(1)
        layout.addLayout(actions_row)

        self._populate_client_combo()
        self._update_client_action_controls()

        self.integration_feedback_label = CaptionLabel("", card)
        self.integration_feedback_label.setWordWrap(True)
        self.integration_feedback_label.hide()
        layout.addWidget(self.integration_feedback_label)

        # Dynamic yellow 0-API conflict banner
        self.api_conflict_banner = QFrame(card)
        self.api_conflict_banner.setObjectName("mcp_api_conflict_banner")
        banner_layout = QHBoxLayout(self.api_conflict_banner)
        banner_layout.setContentsMargins(12, 10, 12, 10)
        banner_layout.setSpacing(10)
        self.api_conflict_icon = IconWidget(FIF.INFO, self.api_conflict_banner)
        self.api_conflict_icon.setFixedSize(18, 18)
        self.api_conflict_label = BodyLabel("", self.api_conflict_banner)
        self.api_conflict_label.setWordWrap(True)
        self.go_settings_button = PushButton(self._t("MCP Go To Settings"), self.api_conflict_banner)
        self.go_settings_button.setIcon(FIF.SETTING)
        banner_layout.addWidget(self.api_conflict_icon, 0, Qt.AlignmentFlag.AlignTop)
        banner_layout.addWidget(self.api_conflict_label, 1)
        banner_layout.addWidget(self.go_settings_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.api_conflict_banner.hide()
        layout.addWidget(self.api_conflict_banner)

        parent_layout.addWidget(card)

    def _build_strategy_card(self, parent_layout: QVBoxLayout) -> None:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        self.strategy_card_title = self._create_card_header(
            card, layout, FIF.SETTING, self._t("MCP Strategy Card Title")
        )

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)

        def _add_row(row: int, title_key: str, hint_key: str, control: QWidget):
            col = QVBoxLayout()
            col.setSpacing(2)
            t_lbl = BodyLabel(self._t(title_key), card)
            h_lbl = CaptionLabel(self._t(hint_key), card)
            h_lbl.setWordWrap(True)
            col.addWidget(t_lbl)
            col.addWidget(h_lbl)
            grid.addLayout(col, row, 0)
            grid.addWidget(control, row, 1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return t_lbl, h_lbl

        self.mode_combo = NoWheelComboBox(card)
        self.mode_combo.setMinimumWidth(240)
        self.mode_title, self.mode_hint = _add_row(
            0, "MCP Mode Label", "MCP Mode Hint", self.mode_combo
        )

        self.on_demand_switch = ToggleSwitch(card)
        self.on_demand_title, self.on_demand_hint = _add_row(
            1, "MCP On Demand Image Label", "MCP On Demand Image Hint", self.on_demand_switch
        )

        self.delivery_combo = NoWheelComboBox(card)
        self.delivery_combo.setMinimumWidth(240)
        self.delivery_title, self.delivery_hint = _add_row(
            2, "MCP Image Delivery Label", "MCP Image Delivery Hint", self.delivery_combo
        )

        self.preview_size_combo = NoWheelComboBox(card)
        self.preview_size_combo.setMinimumWidth(140)
        for px in (768, 1024, 1280):
            self.preview_size_combo.addItem(f"{px} px", userData=px)
        self.preview_size_title, self.preview_size_hint = _add_row(
            3, "MCP Preview Max Size Label", "MCP Preview Max Size Hint", self.preview_size_combo
        )

        self.preview_format_combo = NoWheelComboBox(card)
        self.preview_format_combo.setMinimumWidth(140)
        for fmt_key, label in (("jpeg", "JPEG"), ("webp", "WebP"), ("png", "PNG")):
            self.preview_format_combo.addItem(label, userData=fmt_key)
        self.preview_format_title, self.preview_format_hint = _add_row(
            4, "MCP Preview Format Label", "MCP Preview Format Hint", self.preview_format_combo
        )

        self.preview_quality_edit = LineEdit(card)
        self.preview_quality_edit.setFixedWidth(100)
        self.preview_quality_edit.setValidator(QIntValidator(1, 100, self.preview_quality_edit))
        self.preview_quality_title, self.preview_quality_hint = _add_row(
            5, "MCP Preview Quality Label", "MCP Preview Quality Hint", self.preview_quality_edit
        )

        self.budget_mb_edit = LineEdit(card)
        self.budget_mb_edit.setFixedWidth(100)
        budget_validator = QDoubleValidator(0.1, 50.0, 2, self.budget_mb_edit)
        budget_validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self.budget_mb_edit.setValidator(budget_validator)
        self.budget_title, self.budget_hint = _add_row(
            6, "MCP Inline Budget Label", "MCP Inline Budget Hint", self.budget_mb_edit
        )

        self.parallel_batches_combo = NoWheelComboBox(card)
        self.parallel_batches_combo.setMinimumWidth(140)
        for n in (1, 2, 3, 4):
            self.parallel_batches_combo.addItem(str(n), userData=n)
        self.parallel_title, self.parallel_hint = _add_row(
            7, "MCP Max Parallel Batches Label", "MCP Max Parallel Batches Hint", self.parallel_batches_combo
        )

        self.lease_timeout_edit = LineEdit(card)
        self.lease_timeout_edit.setFixedWidth(100)
        self.lease_timeout_edit.setValidator(QIntValidator(30, 3600, self.lease_timeout_edit))
        self.lease_title, self.lease_hint = _add_row(
            8, "MCP Lease Timeout Label", "MCP Lease Timeout Hint", self.lease_timeout_edit
        )

        self.idle_ttl_edit = LineEdit(card)
        self.idle_ttl_edit.setFixedWidth(100)
        self.idle_ttl_edit.setValidator(QIntValidator(0, 86400, self.idle_ttl_edit))
        self.idle_ttl_title, self.idle_ttl_hint = _add_row(
            9, "MCP Idle TTL Label", "MCP Idle TTL Hint", self.idle_ttl_edit
        )

        self.use_gui_files_switch = ToggleSwitch(card)
        self.use_gui_files_title, self.use_gui_files_hint = _add_row(
            10, "MCP Use GUI Files Label", "MCP Use GUI Files Hint", self.use_gui_files_switch
        )

        self._populate_strategy_combo_texts()
        layout.addLayout(grid)
        parent_layout.addWidget(card)

    def _build_progress_card(self, parent_layout: QVBoxLayout) -> None:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)

        self.progress_card_title = self._create_card_header(
            card, layout, FIF.SYNC, self._t("MCP Progress Card Title")
        )

        meta_row = QHBoxLayout()
        meta_row.setSpacing(14)
        self.job_id_label = CaptionLabel(self._t("MCP Job ID Value", job_id="-"), card)
        self.job_mode_label = CaptionLabel(self._t("MCP Effective Mode Value", mode="-"), card)
        self.ondemand_count_label = CaptionLabel(self._t("MCP On Demand Count Value", count=0), card)
        meta_row.addWidget(self.job_id_label)
        meta_row.addWidget(self.job_mode_label)
        meta_row.addWidget(self.ondemand_count_label)
        meta_row.addStretch(1)
        layout.addLayout(meta_row)

        prog_header = QHBoxLayout()
        prog_header.setSpacing(8)
        self.job_status_label = BodyLabel(self._t("MCP Job Idle"), card)
        self.job_status_label.setWordWrap(True)
        self.job_count_label = CaptionLabel("0/0 (0%)", card)
        self.job_count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        prog_header.addWidget(self.job_status_label, 1)
        prog_header.addWidget(self.job_count_label)
        layout.addLayout(prog_header)

        self.job_progress_bar = ProgressBar(card)
        self.job_progress_bar.setMinimum(0)
        self.job_progress_bar.setMaximum(100)
        self.job_progress_bar.setValue(0)
        self.job_progress_bar.setFixedHeight(6)
        layout.addWidget(self.job_progress_bar)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.stop_job_button = PushButton(self._t("MCP Stop Job"), card)
        self.stop_job_button.setEnabled(False)
        self.open_output_button = PushButton(self._t("MCP Open Output Dir"), card)
        self.open_output_button.setIcon(FIF.FOLDER)
        self.unload_models_button = PushButton(self._t("MCP Unload Models"), card)
        self.unload_models_button.setIcon(FIF.DELETE)
        btn_row.addWidget(self.stop_job_button)
        btn_row.addWidget(self.open_output_button)
        btn_row.addWidget(self.unload_models_button)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        parent_layout.addWidget(card)

    def _build_logs_card(self, parent_layout: QVBoxLayout) -> None:
        card = CardWidget(self)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)

        self.logs_card_title = self._create_card_header(
            card, layout, FIF.DOCUMENT, self._t("MCP Logs Card Title")
        )

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.log_filter_label = CaptionLabel(self._t("MCP Log Filter Label"), card)
        self.log_filter_combo = NoWheelComboBox(card)
        self.log_filter_combo.setMinimumWidth(140)
        self.auto_scroll_label = CaptionLabel(self._t("MCP Log Auto Scroll"), card)
        self.auto_scroll_switch = ToggleSwitch(card, checked=True)
        self.clear_logs_button = PushButton(self._t("MCP Log Clear"), card)
        self.open_logs_dir_button = PushButton(self._t("Open log folder"), card)
        self.open_logs_dir_button.setIcon(FIF.FOLDER)

        toolbar.addWidget(self.log_filter_label)
        toolbar.addWidget(self.log_filter_combo)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.auto_scroll_label)
        toolbar.addWidget(self.auto_scroll_switch)
        toolbar.addStretch(1)
        toolbar.addWidget(self.clear_logs_button)
        toolbar.addWidget(self.open_logs_dir_button)
        layout.addLayout(toolbar)

        self._populate_log_filter_combo()

        self.log_view = PlainTextEdit(card)
        self.log_view.setReadOnly(True)
        self.log_view.setFont(monospace_font(10))
        self.log_view.setMinimumHeight(240)
        self.log_view.setMaximumBlockCount(self._MAX_LOG_LINES)
        self.log_view.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log_view)

        parent_layout.addWidget(card)

    def _populate_strategy_combo_texts(self) -> None:
        prev_mode = self.mode_combo.currentData()
        self.mode_combo.blockSignals(True)
        try:
            self.mode_combo.clear()
            self.mode_combo.addItem(self._t("MCP Mode HQ"), userData="hq")
            self.mode_combo.addItem(self._t("MCP Mode Text"), userData="text")
            idx = self.mode_combo.findData(prev_mode)
            if idx >= 0:
                self.mode_combo.setCurrentIndex(idx)
        finally:
            self.mode_combo.blockSignals(False)

        prev_delivery = self.delivery_combo.currentData()
        self.delivery_combo.blockSignals(True)
        try:
            self.delivery_combo.clear()
            self.delivery_combo.addItem(self._t("MCP Delivery Auto"), userData="auto")
            self.delivery_combo.addItem(self._t("MCP Delivery Inline"), userData="mcp_image")
            self.delivery_combo.addItem(self._t("MCP Delivery File Path"), userData="file_path")
            idx = self.delivery_combo.findData(prev_delivery)
            if idx >= 0:
                self.delivery_combo.setCurrentIndex(idx)
        finally:
            self.delivery_combo.blockSignals(False)

    def _client_profiles(self) -> List[Any]:
        if self.mcp_service is not None and hasattr(self.mcp_service, "list_mcp_client_profiles"):
            return list(self.mcp_service.list_mcp_client_profiles())
        from services.mcp_service import MCP_CLIENT_PROFILES

        return list(MCP_CLIENT_PROFILES.values())

    def _client_display_label(self, profile: Any) -> str:
        key = getattr(profile, "key", "")
        if key == "generic":
            return self._t("MCP Client Generic")
        if key == "stdio_bridge":
            return self._t("MCP Client Stdio Bridge")
        return str(getattr(profile, "display_name", key))

    def _populate_client_combo(self) -> None:
        prev_client = self.client_combo.currentData() or "claude_code"
        self.client_combo.blockSignals(True)
        try:
            self.client_combo.clear()
            for profile in self._client_profiles():
                self.client_combo.addItem(self._client_display_label(profile), userData=profile.key)
            idx = self.client_combo.findData(prev_client)
            if idx < 0 and self.client_combo.count() > 0:
                idx = 0
            if idx >= 0:
                self.client_combo.setCurrentIndex(idx)
        finally:
            self.client_combo.blockSignals(False)

    def _selected_client_profile(self) -> Optional[Any]:
        client_key = self.client_combo.currentData()
        if not client_key:
            return None
        for profile in self._client_profiles():
            if profile.key == client_key:
                return profile
        return None

    def _update_client_action_controls(self) -> None:
        profile = self._selected_client_profile()
        can_write = bool(profile and getattr(profile, "supports_write", False))
        self.write_config_button.setEnabled(can_write)
        if can_write:
            self.write_config_button.setToolTip("")
        else:
            self.write_config_button.setToolTip(self._t("MCP Write Config Copy Only Hint"))

    def _populate_log_filter_combo(self) -> None:
        prev_filter = self.log_filter_combo.currentData() or "all"
        self.log_filter_combo.blockSignals(True)
        try:
            self.log_filter_combo.clear()
            self.log_filter_combo.addItem(self._t("MCP Log Filter All"), userData="all")
            self.log_filter_combo.addItem(self._t("MCP Log Filter MCP"), userData="mcp")
            self.log_filter_combo.addItem(self._t("MCP Log Filter Warnings"), userData="warning")
            idx = self.log_filter_combo.findData(prev_filter)
            if idx >= 0:
                self.log_filter_combo.setCurrentIndex(idx)
        finally:
            self.log_filter_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------
    def _connect_signals(self) -> None:
        self.toggle_server_button.clicked.connect(self._on_toggle_server_clicked)
        self.copy_url_button.clicked.connect(self._on_copy_url_clicked)
        self.port_edit.editingFinished.connect(self._on_port_edited)
        self.auto_start_switch.checkedChanged.connect(
            lambda checked: self._save_mcp_setting("auto_start", bool(checked))
        )
        self.client_combo.currentIndexChanged.connect(self._on_client_changed)
        self.copy_config_button.clicked.connect(self._on_copy_config_clicked)
        self.write_config_button.clicked.connect(self._on_write_config_clicked)
        self.install_skill_button.clicked.connect(self._on_install_skill_clicked)
        self.go_settings_button.clicked.connect(self._on_go_settings_clicked)

        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.on_demand_switch.checkedChanged.connect(
            lambda checked: self._save_mcp_setting("allow_on_demand_image", bool(checked))
        )
        self.delivery_combo.currentIndexChanged.connect(
            lambda _i: self._save_mcp_setting("image_delivery", self.delivery_combo.currentData())
        )
        self.preview_size_combo.currentIndexChanged.connect(
            lambda _i: self._save_mcp_setting("preview_max_size", int(self.preview_size_combo.currentData()))
        )
        self.preview_format_combo.currentIndexChanged.connect(self._on_preview_format_changed)
        self.preview_quality_edit.editingFinished.connect(self._on_preview_quality_edited)
        self.budget_mb_edit.editingFinished.connect(self._on_budget_edited)
        self.parallel_batches_combo.currentIndexChanged.connect(
            lambda _i: self._save_mcp_setting("max_parallel_batches", int(self.parallel_batches_combo.currentData()))
        )
        self.lease_timeout_edit.editingFinished.connect(self._on_lease_timeout_edited)
        self.idle_ttl_edit.editingFinished.connect(self._on_idle_ttl_edited)
        self.use_gui_files_switch.checkedChanged.connect(
            lambda checked: self._save_mcp_setting("use_gui_files_by_default", bool(checked))
        )

        self.stop_job_button.clicked.connect(self._on_stop_job_clicked)
        self.open_output_button.clicked.connect(self.controller.open_output_folder)
        self.unload_models_button.clicked.connect(self._on_unload_models_clicked)

        self.log_filter_combo.currentIndexChanged.connect(lambda _i: self._render_filtered_logs())
        self.auto_scroll_switch.checkedChanged.connect(self._on_auto_scroll_toggled)
        self.clear_logs_button.clicked.connect(self._on_clear_logs_clicked)
        self.open_logs_dir_button.clicked.connect(self._on_open_logs_dir_clicked)

        if self.mcp_service is not None:
            self.mcp_service.server_state_changed.connect(
                self._on_server_state_changed, type=Qt.ConnectionType.QueuedConnection
            )
            self.mcp_service.job_progress_changed.connect(
                self._on_job_progress_changed, type=Qt.ConnectionType.QueuedConnection
            )
            self.mcp_service.log_record_received.connect(
                self._on_log_record_received, type=Qt.ConnectionType.QueuedConnection
            )
            self.mcp_service.api_conflicts_changed.connect(
                self._on_api_conflicts_changed, type=Qt.ConnectionType.QueuedConnection
            )

    # ------------------------------------------------------------------
    # Config synchronization & persistence
    # ------------------------------------------------------------------
    def _save_mcp_setting(self, key: str, value: Any) -> None:
        if self._syncing or value is None:
            return
        self.controller.update_single_config(f"mcp.{key}", value)
        if key == "port":
            self._update_server_status_widgets()

    def _on_port_edited(self) -> None:
        if self._syncing:
            return
        text = self.port_edit.text().strip()
        try:
            port = max(1024, min(65535, int(text)))
        except ValueError:
            port = 8765
        self.port_edit.setText(str(port))
        self._save_mcp_setting("port", port)

    def _on_mode_changed(self, _index: int) -> None:
        mode = self.mode_combo.currentData() or "hq"
        self._update_on_demand_controls(mode)
        self._save_mcp_setting("translation_mode", mode)

    def _update_on_demand_controls(self, mode: str) -> None:
        is_hq = str(mode) == "hq"
        self.on_demand_switch.setEnabled(not is_hq)
        if is_hq:
            self.on_demand_hint.setText(self._t("MCP On Demand Disabled In HQ Hint"))
        else:
            self.on_demand_hint.setText(self._t("MCP On Demand Image Hint"))

    def _on_preview_format_changed(self, _index: int) -> None:
        fmt = self.preview_format_combo.currentData() or "jpeg"
        self.preview_quality_edit.setEnabled(fmt != "png")
        self._save_mcp_setting("preview_format", fmt)

    def _on_preview_quality_edited(self) -> None:
        if self._syncing:
            return
        try:
            quality = max(1, min(100, int(self.preview_quality_edit.text().strip())))
        except ValueError:
            quality = 82
        self.preview_quality_edit.setText(str(quality))
        self._save_mcp_setting("preview_quality", quality)

    def _on_budget_edited(self) -> None:
        if self._syncing:
            return
        try:
            budget = max(0.1, min(50.0, round(float(self.budget_mb_edit.text().strip()), 2)))
        except ValueError:
            budget = 2.0
        self.budget_mb_edit.setText(str(budget))
        self._save_mcp_setting("inline_image_budget_mb", budget)

    def _on_lease_timeout_edited(self) -> None:
        if self._syncing:
            return
        try:
            sec = max(30, min(3600, int(self.lease_timeout_edit.text().strip())))
        except ValueError:
            sec = 180
        self.lease_timeout_edit.setText(str(sec))
        self._save_mcp_setting("lease_timeout_seconds", sec)

    def _on_idle_ttl_edited(self) -> None:
        if self._syncing:
            return
        try:
            sec = max(0, min(86400, int(self.idle_ttl_edit.text().strip())))
        except ValueError:
            sec = 300
        self.idle_ttl_edit.setText(str(sec))
        self._save_mcp_setting("models_idle_ttl", sec)

    def sync_from_config(self, _config_dict: Optional[dict] = None) -> None:
        if not self.config_service:
            return
        cfg = self.config_service.get_config()
        mcp_cfg = getattr(cfg, "mcp", None)
        if mcp_cfg is None:
            return

        self._syncing = True
        try:
            self.port_edit.setText(str(mcp_cfg.port))
            self.auto_start_switch.setCheckedSilently(bool(mcp_cfg.auto_start))

            mode_idx = self.mode_combo.findData(mcp_cfg.translation_mode)
            if mode_idx >= 0:
                self.mode_combo.setCurrentIndex(mode_idx)
            self.on_demand_switch.setCheckedSilently(bool(mcp_cfg.allow_on_demand_image))
            self._update_on_demand_controls(mcp_cfg.translation_mode)

            del_idx = self.delivery_combo.findData(mcp_cfg.image_delivery)
            if del_idx >= 0:
                self.delivery_combo.setCurrentIndex(del_idx)

            size_idx = self.preview_size_combo.findData(int(mcp_cfg.preview_max_size))
            if size_idx < 0:
                self.preview_size_combo.addItem(f"{mcp_cfg.preview_max_size} px", userData=int(mcp_cfg.preview_max_size))
                size_idx = self.preview_size_combo.findData(int(mcp_cfg.preview_max_size))
            if size_idx >= 0:
                self.preview_size_combo.setCurrentIndex(size_idx)

            fmt_idx = self.preview_format_combo.findData(mcp_cfg.preview_format)
            if fmt_idx >= 0:
                self.preview_format_combo.setCurrentIndex(fmt_idx)
            self.preview_quality_edit.setText(str(mcp_cfg.preview_quality))
            self.preview_quality_edit.setEnabled(mcp_cfg.preview_format != "png")

            self.budget_mb_edit.setText(str(mcp_cfg.inline_image_budget_mb))

            par_idx = self.parallel_batches_combo.findData(int(mcp_cfg.max_parallel_batches))
            if par_idx >= 0:
                self.parallel_batches_combo.setCurrentIndex(par_idx)

            self.lease_timeout_edit.setText(str(mcp_cfg.lease_timeout_seconds))
            self.idle_ttl_edit.setText(str(mcp_cfg.models_idle_ttl))
            self.use_gui_files_switch.setCheckedSilently(bool(mcp_cfg.use_gui_files_by_default))
        finally:
            self._syncing = False

    # ------------------------------------------------------------------
    # Actions & callbacks
    # ------------------------------------------------------------------
    def _show_toast(self, title: str, content: str, *, error: bool = False) -> None:
        self.integration_feedback_label.setText(content)
        self.integration_feedback_label.show()
        try:
            fn = InfoBar.error if error else InfoBar.success
            fn(
                title=title,
                content=content,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=4000,
                parent=self.window() or self,
            )
        except Exception:
            pass

    def _current_port(self) -> int:
        try:
            return max(1024, min(65535, int(self.port_edit.text().strip())))
        except ValueError:
            return 8765

    def _on_toggle_server_clicked(self) -> None:
        if self.mcp_service is None:
            return
        if self.mcp_service.is_server_running():
            ok, url, err = self.mcp_service.stop_server()
            if not ok and err:
                self._show_toast(self._t("Error"), err, error=True)
            else:
                self._update_server_status_widgets(running=False, url=url)
        else:
            port = self._current_port()
            self._save_mcp_setting("port", port)
            ok, url, err = self.mcp_service.start_server(port=port)
            if not ok and err:
                self._show_toast(self._t("Error"), err, error=True)
            else:
                self._update_server_status_widgets(running=True, url=url)

    def _on_copy_url_clicked(self) -> None:
        url = self.endpoint_label.text().strip()
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(url)
        self._show_toast(self._t("MCP Service"), self._t("MCP URL Copied", url=url))

    def _on_client_changed(self, _index: int) -> None:
        self._update_client_action_controls()

    def _on_copy_config_clicked(self) -> None:
        if self.mcp_service is None:
            return
        profile = self._selected_client_profile()
        client_key = profile.key if profile is not None else "generic"
        client_label = self._client_display_label(profile) if profile is not None else client_key
        snippet = self.mcp_service.get_mcp_client_config_snippet(
            client=client_key,
            port=self._current_port(),
        )
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(snippet)
        self._show_toast(self._t("MCP Service"), self._t("MCP Config Copied", client=client_label))

    def _on_write_config_clicked(self) -> None:
        if self.mcp_service is None:
            return
        profile = self._selected_client_profile()
        if profile is None:
            return
        if not getattr(profile, "supports_write", False):
            self._show_toast(self._t("MCP Service"), self._t("MCP Write Config Copy Only Hint"))
            return

        custom_path: Optional[str] = None
        if getattr(profile, "requires_custom_config_path", False):
            default_dir = Path(getattr(self.mcp_service, "root_dir", os.getcwd())) / ".vscode" / "mcp.json"
            selected, _ = QFileDialog.getSaveFileName(
                self,
                self._t("MCP Select Config File Title"),
                str(default_dir),
                "JSON (*.json);;All Files (*)",
                options=QFileDialog.Option.DontConfirmOverwrite,
            )
            if not selected:
                return
            custom_path = selected

        try:
            path = self.mcp_service.install_mcp_config(
                profile.key,
                config_path=custom_path,
                port=self._current_port(),
            )
            self._show_toast(
                self._t("MCP Service"),
                self._t("MCP Config Written", client=self._client_display_label(profile), path=path),
            )
        except Exception as exc:
            self._show_toast(self._t("Error"), str(exc), error=True)

    def _on_install_skill_clicked(self) -> None:
        if self.mcp_service is None:
            return
        profile = self._selected_client_profile()
        target = profile.key if profile is not None else "agents_standard"
        try:
            path = self.mcp_service.install_skill(target=target)
            self._show_toast(self._t("MCP Service"), self._t("MCP Skill Installed", path=path))
        except Exception as exc:
            self._show_toast(self._t("Error"), str(exc), error=True)

    def _on_go_settings_clicked(self) -> None:
        if callable(self._switch_page):
            self._switch_page("settings")

    def _on_stop_job_clicked(self) -> None:
        if self.mcp_service is not None:
            self.mcp_service.cancel_active_job()
            self.refresh_status()

    def _on_unload_models_clicked(self) -> None:
        if self.mcp_service is None:
            return
        self.unload_models_button.setEnabled(False)

        def _done(_res):
            from PyQt6.QtCore import QTimer

            QTimer.singleShot(
                0,
                lambda: (
                    self.unload_models_button.setEnabled(True),
                    self._show_toast(self._t("MCP Service"), self._t("MCP Models Unloaded")),
                ),
            )

        def _err(msg: str):
            from PyQt6.QtCore import QTimer

            QTimer.singleShot(
                0,
                lambda: (
                    self.unload_models_button.setEnabled(True),
                    self._show_toast(self._t("Error"), msg, error=True),
                ),
            )

        self.mcp_service.unload_models_async(on_done=_done, on_error=_err)

    # ------------------------------------------------------------------
    # Status, conflicts & log updates
    # ------------------------------------------------------------------
    def _update_server_status_widgets(
        self,
        *,
        running: Optional[bool] = None,
        url: Optional[str] = None,
        error: str = "",
    ) -> None:
        is_running = (
            bool(running)
            if running is not None
            else bool(self.mcp_service and self.mcp_service.is_server_running())
        )
        endpoint = url or (
            self.mcp_service.endpoint_url()
            if self.mcp_service
            else f"http://127.0.0.1:{self._current_port()}/mcp"
        )
        self.endpoint_label.setText(endpoint)

        if is_running:
            self.status_dot.setStyleSheet("background-color: #2ecc71; border-radius: 5px;")
            self.status_label.setText(self._t("MCP Status Running"))
            self.toggle_server_button.setText(self._t("MCP Stop Service"))
            self.toggle_server_button.setIcon(FIF.PAUSE if hasattr(FIF, "PAUSE") else FIF.CLOSE)
            self.port_edit.setEnabled(False)
        else:
            self.status_dot.setStyleSheet("background-color: #95a5a6; border-radius: 5px;")
            self.status_label.setText(
                f"{self._t('MCP Status Stopped')} ({error})" if error else self._t("MCP Status Stopped")
            )
            self.toggle_server_button.setText(self._t("MCP Start Service"))
            self.toggle_server_button.setIcon(FIF.PLAY)
            self.port_edit.setEnabled(True)

    @pyqtSlot(bool, str, str)
    def _on_server_state_changed(self, running: bool, url: str, error: str) -> None:
        self._update_server_status_widgets(running=running, url=url, error=error)

    @pyqtSlot(list)
    def _on_api_conflicts_changed(self, conflicts: list) -> None:
        if not conflicts:
            self.api_conflict_banner.hide()
            return
        lines = [f"{item.get('section')}.{item.get('setting')} = {item.get('value')}" for item in conflicts]
        self.api_conflict_label.setText(
            self._t("MCP API Conflict Warning", items=", ".join(lines))
        )
        self.api_conflict_banner.show()

    @pyqtSlot(dict)
    def _on_job_progress_changed(self, status_dict: dict) -> None:
        if not status_dict:
            return
        job_id = status_dict.get("job_id") or "-"
        mode = status_dict.get("effective_mode") or "-"
        on_demand = int(status_dict.get("on_demand_image_requests") or 0)
        self.job_id_label.setText(self._t("MCP Job ID Value", job_id=job_id))
        self.job_mode_label.setText(self._t("MCP Effective Mode Value", mode=mode))
        self.ondemand_count_label.setText(self._t("MCP On Demand Count Value", count=on_demand))

        total = int(status_dict.get("total_pages") or 0)
        skipped = int(status_dict.get("skipped_pages") or 0)
        rendered = int(status_dict.get("rendered_pages") or 0)
        failed = int(status_dict.get("failed_pages") or 0)
        done_count = min(total, skipped + rendered + failed)
        pct = int((done_count / total) * 100) if total > 0 else 0

        self.job_progress_bar.setMaximum(max(total, 1))
        self.job_progress_bar.setValue(done_count if total > 0 else 0)
        self.job_count_label.setText(f"{done_count}/{total} ({pct}%)")

        status = str(status_dict.get("status") or "idle")
        preprocessed = int(status_dict.get("preprocessed_pages") or 0)
        committed = int(status_dict.get("committed_pages") or 0)
        pending = int(status_dict.get("pending_pages") or 0)
        self.job_status_label.setText(
            self._t(
                "MCP Job Status Summary",
                status=status,
                preprocessed=preprocessed,
                committed=committed,
                pending=pending,
                rendered=rendered,
                skipped=skipped,
                failed=failed,
            )
        )
        self.stop_job_button.setEnabled(status == "running")

    def refresh_status(self) -> None:
        self.sync_from_config()
        self._update_server_status_widgets()
        if self.mcp_service is not None:
            self._on_api_conflicts_changed(self.mcp_service.get_api_backend_conflicts())
            job_status = self.mcp_service.get_current_job_status()
            if job_status is not None:
                self._on_job_progress_changed(job_status)

    # ------------------------------------------------------------------
    # Live log panel
    # ------------------------------------------------------------------
    def _backfill_logs(self) -> None:
        if self.log_service is None:
            return
        self._log_entries = list(self.log_service.get_recent_logs(limit=self._MAX_LOG_LINES))
        self._render_filtered_logs()

    @staticmethod
    def _format_log_line(entry: Dict[str, Any]) -> str:
        ts = str(entry.get("timestamp") or "")
        if "T" in ts:
            ts = ts.split("T", 1)[1][:8]
        level = str(entry.get("level") or "INFO")
        module = str(entry.get("module") or "")
        message = str(entry.get("message") or "")
        line = f"[{ts}] {level:<7} [{module}] {message}"
        exc = entry.get("exception")
        if exc:
            line += f"\n{exc}"
        return line

    def _entry_matches_filter(self, entry: Dict[str, Any]) -> bool:
        mode = self.log_filter_combo.currentData() or "all"
        if mode == "mcp":
            module = str(entry.get("module") or "").lower()
            message = str(entry.get("message") or "")
            return "mcp" in module or "[MCP]" in message
        if mode == "warning":
            level = str(entry.get("level") or "").upper()
            return level in {"WARNING", "ERROR", "CRITICAL"}
        return True

    def _render_filtered_logs(self) -> None:
        lines = [
            self._format_log_line(entry)
            for entry in self._log_entries
            if self._entry_matches_filter(entry)
        ]
        self.log_view.setPlainText("\n".join(lines[-self._MAX_LOG_LINES :]))
        if self._auto_scroll:
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    @pyqtSlot(dict)
    def _on_log_record_received(self, entry: dict) -> None:
        self._log_entries.append(entry)
        if len(self._log_entries) > self._MAX_LOG_LINES:
            self._log_entries = self._log_entries[-self._MAX_LOG_LINES :]
        if not self._entry_matches_filter(entry):
            return
        self.log_view.appendPlainText(self._format_log_line(entry))
        if self._auto_scroll:
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _on_auto_scroll_toggled(self, checked: bool) -> None:
        self._auto_scroll = bool(checked)
        if self._auto_scroll:
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _on_clear_logs_clicked(self) -> None:
        self._log_entries.clear()
        self.log_view.clear()

    def _on_open_logs_dir_clicked(self) -> None:
        if self.mcp_service is None:
            return
        target = self.mcp_service.get_log_directory()
        os.makedirs(target, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    # ------------------------------------------------------------------
    # Theme & i18n refresh
    # ------------------------------------------------------------------
    def apply_theme(self) -> None:
        dark = isDarkTheme()
        if dark:
            self.api_conflict_banner.setStyleSheet(
                "#mcp_api_conflict_banner { background-color: rgba(243, 156, 18, 0.18); "
                "border: 1px solid rgba(243, 156, 18, 0.55); border-radius: 6px; }"
            )
        else:
            self.api_conflict_banner.setStyleSheet(
                "#mcp_api_conflict_banner { background-color: rgba(255, 193, 7, 0.18); "
                "border: 1px solid rgba(243, 156, 18, 0.65); border-radius: 6px; }"
            )

    def refresh_ui_texts(self) -> None:
        self.title_label.setText(self._t("MCP Service"))
        self.subtitle_label.setText(self._t("MCP Page Subtitle"))
        self.control_card_title.setText(self._t("MCP Control Card Title"))
        self.copy_url_button.setText(self._t("MCP Copy URL"))
        self.port_label.setText(self._t("MCP Port Label"))
        self.auto_start_label.setText(self._t("MCP Auto Start Label"))
        self.auto_start_hint.setText(self._t("MCP Auto Start Hint"))
        self.client_label.setText(self._t("MCP Target Client Label"))
        self._populate_client_combo()
        self.copy_config_button.setText(self._t("MCP Copy Config"))
        self.write_config_button.setText(self._t("MCP Write Client Config"))
        self.install_skill_button.setText(self._t("MCP Install Skill"))
        self._update_client_action_controls()
        self.go_settings_button.setText(self._t("MCP Go To Settings"))

        self.strategy_card_title.setText(self._t("MCP Strategy Card Title"))
        self.mode_title.setText(self._t("MCP Mode Label"))
        self.mode_hint.setText(self._t("MCP Mode Hint"))
        self.on_demand_title.setText(self._t("MCP On Demand Image Label"))
        self.delivery_title.setText(self._t("MCP Image Delivery Label"))
        self.delivery_hint.setText(self._t("MCP Image Delivery Hint"))
        self.preview_size_title.setText(self._t("MCP Preview Max Size Label"))
        self.preview_size_hint.setText(self._t("MCP Preview Max Size Hint"))
        self.preview_format_title.setText(self._t("MCP Preview Format Label"))
        self.preview_format_hint.setText(self._t("MCP Preview Format Hint"))
        self.preview_quality_title.setText(self._t("MCP Preview Quality Label"))
        self.preview_quality_hint.setText(self._t("MCP Preview Quality Hint"))
        self.budget_title.setText(self._t("MCP Inline Budget Label"))
        self.budget_hint.setText(self._t("MCP Inline Budget Hint"))
        self.parallel_title.setText(self._t("MCP Max Parallel Batches Label"))
        self.parallel_hint.setText(self._t("MCP Max Parallel Batches Hint"))
        self.lease_title.setText(self._t("MCP Lease Timeout Label"))
        self.lease_hint.setText(self._t("MCP Lease Timeout Hint"))
        self.idle_ttl_title.setText(self._t("MCP Idle TTL Label"))
        self.idle_ttl_hint.setText(self._t("MCP Idle TTL Hint"))
        self.use_gui_files_title.setText(self._t("MCP Use GUI Files Label"))
        self.use_gui_files_hint.setText(self._t("MCP Use GUI Files Hint"))

        self._populate_strategy_combo_texts()
        self._populate_log_filter_combo()
        self._update_on_demand_controls(self.mode_combo.currentData() or "hq")

        self.progress_card_title.setText(self._t("MCP Progress Card Title"))
        self.stop_job_button.setText(self._t("MCP Stop Job"))
        self.open_output_button.setText(self._t("MCP Open Output Dir"))
        self.unload_models_button.setText(self._t("MCP Unload Models"))

        self.logs_card_title.setText(self._t("MCP Logs Card Title"))
        self.log_filter_label.setText(self._t("MCP Log Filter Label"))
        self.auto_scroll_label.setText(self._t("MCP Log Auto Scroll"))
        self.clear_logs_button.setText(self._t("MCP Log Clear"))
        self.open_logs_dir_button.setText(self._t("Open log folder"))

        self.refresh_status()

    def shutdown(self) -> None:
        """Disconnect McpService signals cleanly before window teardown."""
        if self.mcp_service is None:
            return
        for sig, slot in (
            (self.mcp_service.server_state_changed, self._on_server_state_changed),
            (self.mcp_service.job_progress_changed, self._on_job_progress_changed),
            (self.mcp_service.log_record_received, self._on_log_record_received),
            (self.mcp_service.api_conflicts_changed, self._on_api_conflicts_changed),
        ):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass


def create_mcp_page(self) -> QWidget:
    """Create the MCP Service Fluent page and bind it to MainView."""
    panel = McpPagePanel(
        controller=self.controller,
        t_func=self._t,
        switch_page=self._switch_content_page,
    )
    self.mcp_panel = panel
    return panel
