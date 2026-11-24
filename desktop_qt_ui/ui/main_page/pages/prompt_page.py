from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon as FIF,
    HeaderCardWidget,
    ListWidget,
    PrimaryPushButton,
    PushButton,
    TitleLabel,
    ToolButton,
)

from ui.secondary_pages.prompt_preview import PromptPreviewPanel
from ui.widgets.hover_hint import install_hover_hint
from ui.widgets.toggle_switch import ToggleSwitch

_BUILTIN_PROMPT_UI_SPECS = (
    {
        "stem": "system_prompt_hq",
        "title_key": "Builtin Base Translation Prompt",
        "desc_key": "desc_builtin_system_prompt_hq",
        "config_key": "translator.use_builtin_system_prompt",
    },
    {
        "stem": "system_prompt_line_break",
        "title_key": "AI Line Break Prompt",
        "desc_key": "desc_builtin_system_prompt_line_break",
        "config_key": "render.disable_auto_wrap",
    },
    {
        "stem": "glossary_extraction_prompt",
        "title_key": "Glossary Extraction Prompt",
        "desc_key": "desc_builtin_glossary_extraction_prompt",
        "config_key": "translator.extract_glossary",
    },
    {
        "stem": "system_prompt_hq_format",
        "title_key": "Output Format Prompt",
        "desc_key": "desc_builtin_system_prompt_hq_format",
        "config_key": None,
    },
)


def create_prompt_page(self) -> QWidget:
    page = QWidget()
    page_layout = QVBoxLayout(page)
    page_layout.setContentsMargins(18, 16, 18, 14)
    page_layout.setSpacing(12)

    self.prompt_page_title_label = TitleLabel(self._t("Prompt Management"))
    self.prompt_page_subtitle_label = BodyLabel(
        self._t("Manage and apply prompt files for translation")
    )
    self.prompt_page_subtitle_label.setWordWrap(True)
    page_layout.addWidget(self.prompt_page_title_label)
    page_layout.addWidget(self.prompt_page_subtitle_label)

    prompt_splitter = QSplitter(Qt.Orientation.Horizontal)

    left_panel = QWidget(prompt_splitter)
    left_layout = QVBoxLayout(left_panel)
    left_layout.setContentsMargins(0, 0, 0, 0)
    left_layout.setSpacing(10)

    self.prompt_card = HeaderCardWidget(self._t("Prompt List"))
    prompt_card_layout = QVBoxLayout()
    prompt_card_layout.setContentsMargins(0, 0, 0, 0)
    prompt_card_layout.setSpacing(10)
    self.prompt_card.viewLayout.addLayout(prompt_card_layout)

    button_row_layout = QHBoxLayout()
    button_row_layout.setSpacing(8)
    self.prompt_new_button = PushButton(self._t("New"))
    self.prompt_copy_button = PushButton(self._t("Copy"))
    self.prompt_rename_button = PushButton(self._t("Rename"))
    self.prompt_delete_button = PushButton(self._t("Delete"))
    self.prompt_refresh_button = PushButton(self._t("Refresh"))
    self.prompt_open_dir_button = PushButton(self._t("Open Directory"))
    self.prompt_apply_button = PrimaryPushButton(self._t("Apply Selected Prompt"))
    self.prompt_new_button.setIcon(FIF.ADD)
    self.prompt_copy_button.setIcon(FIF.COPY)
    self.prompt_rename_button.setIcon(FIF.EDIT)
    self.prompt_delete_button.setIcon(FIF.DELETE)
    self.prompt_refresh_button.setIcon(FIF.SYNC)
    self.prompt_open_dir_button.setIcon(FIF.FOLDER)
    self.prompt_apply_button.setIcon(FIF.ACCEPT)
    button_row_layout.addWidget(self.prompt_new_button)
    button_row_layout.addWidget(self.prompt_copy_button)
    button_row_layout.addWidget(self.prompt_rename_button)
    button_row_layout.addWidget(self.prompt_delete_button)
    button_row_layout.addWidget(self.prompt_refresh_button)
    button_row_layout.addWidget(self.prompt_open_dir_button)
    button_row_layout.addWidget(self.prompt_apply_button)
    button_row_layout.addStretch()
    prompt_card_layout.addLayout(button_row_layout)

    self.prompt_list_widget = ListWidget()
    prompt_card_layout.addWidget(self.prompt_list_widget)

    self.prompt_status_label = BodyLabel("")
    self.prompt_status_label.setWordWrap(True)
    prompt_card_layout.addWidget(self.prompt_status_label)

    left_layout.addWidget(self.prompt_card, 3)

    self.builtin_prompt_card = HeaderCardWidget(self._t("Built-in Prompts"))
    builtin_layout = QVBoxLayout()
    builtin_layout.setContentsMargins(0, 0, 0, 0)
    builtin_layout.setSpacing(8)
    self.builtin_prompt_card.viewLayout.addLayout(builtin_layout)

    self.builtin_prompt_hint_label = CaptionLabel(self._t("Builtin Prompts Priority Hint"))
    self.builtin_prompt_hint_label.setWordWrap(True)
    builtin_layout.addWidget(self.builtin_prompt_hint_label)

    self.builtin_prompt_rows = {}
    self.builtin_prompt_toggles = {}
    for spec in _BUILTIN_PROMPT_UI_SPECS:
        stem = spec["stem"]
        config_key = spec["config_key"]
        row_widget = QWidget(self.builtin_prompt_card)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(8)

        title_label = BodyLabel(self._t(spec["title_key"]), row_widget)
        title_label.setWordWrap(True)
        install_hover_hint(title_label, self._t(spec["desc_key"]))
        install_hover_hint(row_widget, self._t(spec["desc_key"]))

        preview_btn = ToolButton(FIF.VIEW, row_widget)
        preview_btn.setFixedSize(28, 28)
        install_hover_hint(preview_btn, self._t("Preview"))
        preview_btn.clicked.connect(lambda checked=False, s=stem: self._preview_builtin_prompt(s))

        edit_btn = ToolButton(FIF.EDIT, row_widget)
        edit_btn.setFixedSize(28, 28)
        install_hover_hint(edit_btn, self._t("Edit"))
        edit_btn.clicked.connect(lambda checked=False, s=stem: self._edit_builtin_prompt(s))

        row_layout.addWidget(title_label, 1)
        row_layout.addWidget(preview_btn, 0)
        row_layout.addWidget(edit_btn, 0)

        toggle = None
        required_label = None
        if config_key is not None:
            toggle = ToggleSwitch(row_widget)
            install_hover_hint(toggle, self._t(spec["desc_key"]))
            toggle.checkedChanged.connect(
                lambda checked, k=config_key: self._on_builtin_prompt_toggle_changed(k, bool(checked))
            )
            self.builtin_prompt_toggles[config_key] = toggle
            row_layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        else:
            required_label = CaptionLabel(self._t("Required"), row_widget)
            install_hover_hint(required_label, self._t("Builtin Format Prompt Required Hint"))
            row_layout.addWidget(required_label, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.builtin_prompt_rows[stem] = {
            "row": row_widget,
            "title_label": title_label,
            "preview_button": preview_btn,
            "edit_button": edit_btn,
            "toggle": toggle,
            "required_label": required_label,
            "title_key": spec["title_key"],
            "desc_key": spec["desc_key"],
        }
        builtin_layout.addWidget(row_widget)

    left_layout.addWidget(self.builtin_prompt_card, 0)
    prompt_splitter.addWidget(left_panel)

    # MainView 是纯逻辑 QObject，不能当控件父级；面板随 addWidget 进布局后自动认领父级
    self.prompt_preview_panel = PromptPreviewPanel(t_func=self._t)
    prompt_splitter.addWidget(self.prompt_preview_panel)

    prompt_splitter.setStretchFactor(0, 2)
    prompt_splitter.setStretchFactor(1, 3)
    prompt_splitter.setSizes([380, 560])
    prompt_splitter.setCollapsible(0, False)
    prompt_splitter.setCollapsible(1, False)

    page_layout.addWidget(prompt_splitter, 1)

    self.prompt_new_button.clicked.connect(self._create_new_prompt)
    self.prompt_copy_button.clicked.connect(self._copy_selected_prompt)
    self.prompt_rename_button.clicked.connect(self._rename_selected_prompt)
    self.prompt_delete_button.clicked.connect(self._delete_selected_prompt)
    self.prompt_refresh_button.clicked.connect(self._refresh_prompt_manager)
    self.prompt_open_dir_button.clicked.connect(self.controller.open_dict_directory)
    self.prompt_apply_button.clicked.connect(self._apply_selected_prompt)
    self.prompt_list_widget.itemDoubleClicked.connect(lambda _: self._apply_selected_prompt())
    self.prompt_list_widget.currentItemChanged.connect(self._on_prompt_selection_changed)
    self.prompt_preview_panel.edit_requested.connect(self._open_prompt_editor)
    return page
