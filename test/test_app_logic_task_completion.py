import _bootstrap  # noqa: I001

from types import SimpleNamespace

import pytest

import desktop_qt_ui.app_logic as app_logic_module
from PyQt6.QtWidgets import QApplication

from desktop_qt_ui.app_logic import MainAppLogic
from services.i18n_service import I18nManager

_QT_APP = QApplication.instance() or QApplication([])
LOCALE_DIR = _bootstrap.ROOT / "desktop_qt_ui" / "locales"


class _Signal:
    def __init__(self):
        self.values = []

    def emit(self, *args):
        self.values.append(args)


class _StateManager:
    def __init__(self):
        self.translating = True
        self.status_message = None

    def set_translating(self, value):
        self.translating = bool(value)

    def set_status_message(self, message):
        self.status_message = message


@pytest.mark.parametrize(
    ("locale", "expected_fragment"),
    [
        ("zh_CN", "所有 2 个文件"),
        ("zh_TW", "所有 2 個檔案"),
        ("en_US", "All 2 files"),
        ("ja_JP", "2 個のファイル"),
        ("ko_KR", "2개 파일"),
        ("es_ES", "los 2 archivos"),
    ],
)
def test_all_existing_outputs_use_dedicated_overwrite_disabled_message(
    monkeypatch,
    locale,
    expected_fragment,
):
    warning_signal = _Signal()
    completed_signal = _Signal()
    state_manager = _StateManager()
    i18n = I18nManager(
        locale_dir=str(LOCALE_DIR),
        fallback_locale="zh_CN",
        config_language=locale,
    )

    logic = SimpleNamespace(
        current_task_id=7,
        current_worker=object(),
        saved_files_count=0,
        completed_output_sources={},
        _task_failures=[],
        _path_key=MainAppLogic._path_key,
        _record_task_failure_from_result=lambda _result: None,
        _record_task_failure=lambda *_args: None,
        _ui_log=lambda *_args: None,
        _t=i18n.translate,
        state_manager=state_manager,
        warning_dialog_requested=warning_signal,
        error_dialog_requested=_Signal(),
        task_completed=completed_signal,
        _cleanup_after_task=lambda: None,
    )
    monkeypatch.setattr(app_logic_module.QTimer, "singleShot", lambda *_args: None)
    results = [
        {
            "skipped": True,
            "original_path": rf"C:\batch\{name}.png",
            "skip_message": f"output exists: {name}.png",
        }
        for name in ("01", "02")
    ]

    MainAppLogic.on_task_finished(logic, results, 7)

    message = warning_signal.values[0][0]
    assert expected_fragment in message
    assert "{count}" not in message
    assert "all_existing_outputs_skipped_dialog" not in message
    assert "2" in state_manager.status_message
    assert "all_existing_outputs_skipped_status" not in state_manager.status_message
    assert "01.png" not in message
    assert not state_manager.translating
    assert completed_signal.values == [([],)]


@pytest.mark.parametrize("saved_count", [0, 1])
def test_retry_skips_are_reported_without_overwrite_advice(monkeypatch, saved_count):
    i18n = I18nManager(locale_dir=str(LOCALE_DIR), config_language="zh_CN")
    logic = SimpleNamespace(
        current_task_id=7, current_worker=object(), saved_files_count=0,
        completed_output_sources={}, _task_failures=[],
        _path_key=MainAppLogic._path_key,
        _record_task_failure_from_result=lambda _result: pytest.fail("a skip is not a failed result"),
        _record_task_failure=lambda *_args: pytest.fail("unexpected failure"),
        _ui_log=lambda *_args: None, _t=i18n.translate,
        state_manager=_StateManager(), warning_dialog_requested=_Signal(),
        error_dialog_requested=_Signal(), task_completed=_Signal(),
        _cleanup_after_task=lambda: None,
    )
    monkeypatch.setattr(app_logic_module.QTimer, "singleShot", lambda *_args: None)
    results = [{
        'success': False, 'skipped': True, 'original_path': 'bad.png',
        'skip_reason': 'retry_exhausted', 'skip_message': 'Translation failed: timeout',
    }]
    if saved_count:
        results.append({'success': True, 'original_path': 'good.png', 'output_path': 'out.png'})

    MainAppLogic.on_task_finished(logic, results, 7)

    assert '重试耗尽，已跳过 1 个文件' in logic.task_skip_summary
    assert 'bad.png' in logic.task_skip_summary
    assert 'timeout' in logic.task_skip_summary
    assert '覆盖' not in logic.task_skip_summary
    assert not logic.error_dialog_requested.values
    assert logic.saved_files_count == saved_count
    if not saved_count:
        assert logic.warning_dialog_requested.values == [(logic.task_skip_summary,)]


@pytest.mark.parametrize('prompt_enabled', [True, False])
def test_completion_dialog_includes_skips_with_editor_prompt_enabled_or_disabled(monkeypatch, prompt_enabled):
    from PyQt6.QtWidgets import QMessageBox
    from ui import main_window

    messages = []

    def show_dialog(_window, _title, _subtitle, message, **_kwargs):
        messages.append(message)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(main_window, 'show_error_dialog', show_dialog)
    warning = _Signal()
    window = SimpleNamespace(
        app_logic=SimpleNamespace(task_skip_summary='Skipped bad.png: timeout', warning_dialog_requested=warning),
        _request_main_file_snapshot=lambda: None,
        _should_prompt_open_results_in_editor=lambda: prompt_enabled,
        isMinimized=lambda: False,
        _t=lambda text, **kwargs: text.format(**kwargs),
    )
    main_window.MainWindow.on_task_completed(window, ['good.png'])
    if prompt_enabled:
        assert 'Skipped bad.png: timeout' in messages[0]
        assert '1 files saved' in messages[0]
    else:
        assert warning.values == [('Skipped bad.png: timeout',)]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

