"""Wait for a durable editor save without stopping Qt's event loop."""

from PyQt6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QThread, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QWidget


class _SaveInputGuard(QObject):
    # Block editing and window closure even during the short delay before the
    # progress dialog appears. Paint, timer and worker result events still run.
    _blocked_events = frozenset({
        QEvent.Type.KeyPress,
        QEvent.Type.KeyRelease,
        QEvent.Type.InputMethod,
        QEvent.Type.Shortcut,
        QEvent.Type.ShortcutOverride,
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick,
        QEvent.Type.MouseMove,
        QEvent.Type.Wheel,
        QEvent.Type.TouchBegin,
        QEvent.Type.TouchUpdate,
        QEvent.Type.TouchEnd,
        QEvent.Type.TabletPress,
        QEvent.Type.TabletMove,
        QEvent.Type.TabletRelease,
    })

    def __init__(self, owner):
        super().__init__()
        self.protected_windows = [owner]

    def eventFilter(self, watched, event):
        if event.type() in self._blocked_events or (
            event.type() == QEvent.Type.Close and watched in self.protected_windows
        ):
            event.ignore()
            return True
        return False


def wait_for_editor_save(future, parent=None, *, flush_results=False):
    """Keep the bool save API: return only after writing, while Qt stays live."""
    app = QApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        return future.result()

    def result():
        value = future.result()
        if flush_results:
            # A FIFO barrier completes after prior worker callbacks have posted
            # their signals, but before Qt necessarily installs their artifacts.
            QCoreApplication.sendPostedEvents(None, QEvent.Type.MetaCall)
        return value

    if future.done():
        return result()

    loop = QEventLoop()
    owner = parent.window() if isinstance(parent, QWidget) else QApplication.activeWindow()
    guard = _SaveInputGuard(owner)
    app.installEventFilter(guard)
    dialog = None

    def show_progress():
        nonlocal dialog
        if future.done():
            return
        from services.i18n_service import _
        from ui.secondary_pages.themed_progress_dialog import create_progress_dialog

        message = _("Saving...")
        dialog = create_progress_dialog(parent, message, message)
        guard.protected_windows.append(dialog)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.show()

    delay = QTimer()
    delay.setSingleShot(True)
    delay.timeout.connect(show_progress)
    delay.start(200)
    poll = QTimer()
    poll.setInterval(10)
    poll.timeout.connect(lambda: loop.quit() if future.done() else None)
    poll.start()
    try:
        if not future.done():
            loop.exec()
        return result()
    finally:
        delay.stop()
        poll.stop()
        app.removeEventFilter(guard)
        if dialog is not None:
            dialog.accept()
            dialog.deleteLater()
