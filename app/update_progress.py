from __future__ import annotations

import inspect
import logging
import threading
import traceback
import weakref
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QDialog, QLabel, QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout, QWidget,
)

from app import update_client

_LOGGER = logging.getLogger("tks_to_kintone_app")
_ACTIVE_UPDATE_THREADS: set[QThread] = set()


class UpdateState(str, Enum):
    IDLE = "IDLE"
    DOWNLOADING = "DOWNLOADING"
    VERIFYING_SIZE = "VERIFYING_SIZE"
    VERIFYING_SHA256 = "VERIFYING_SHA256"
    VERIFYING_PE = "VERIFYING_PE"
    PUBLISHING_INSTALLER = "PUBLISHING_INSTALLER"
    WAITING_DOWNLOAD_THREAD_FINISHED = "WAITING_DOWNLOAD_THREAD_FINISHED"
    LAUNCHING_INSTALLER = "LAUNCHING_INSTALLER"
    WAITING_INSTALLER_CONFIRMATION = "WAITING_INSTALLER_CONFIRMATION"
    SHUTDOWN_COMMITTED = "SHUTDOWN_COMMITTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_ACTIVE_STATES = {
    UpdateState.DOWNLOADING, UpdateState.VERIFYING_SIZE,
    UpdateState.VERIFYING_SHA256, UpdateState.VERIFYING_PE,
    UpdateState.PUBLISHING_INSTALLER, UpdateState.WAITING_DOWNLOAD_THREAD_FINISHED,
    UpdateState.LAUNCHING_INSTALLER, UpdateState.WAITING_INSTALLER_CONFIRMATION,
}


def _thread_state(thread: QThread) -> tuple[int, str, str, str]:
    app = QApplication.instance()
    return (id(thread), str(thread.isRunning()).lower(),
            str(thread.isFinished()).lower(),
            str(app is not None and QThread.currentThread() is app.thread()).lower())


def _log_thread_event(event: str, thread: QThread, **fields: object) -> None:
    thread_id, running, finished, is_gui = _thread_state(thread)
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    _LOGGER.info(
        "event=%s thread_object_id=%s isRunning=%s isFinished=%s python_thread=%s "
        "gui_thread=%s%s%s", event, thread_id, running, finished,
        threading.current_thread().name, is_gui, " " if suffix else "", suffix,
    )


class UpdateWorker(QObject):
    stage_changed = Signal(str, str)
    download_progress = Signal(int, int)
    installer_ready = Signal(str)
    failed = Signal(str, str)
    cancelled = Signal()
    terminal = Signal(str)

    def __init__(self, info: update_client.UpdateInfo, update_dir: Path) -> None:
        super().__init__()
        self._info = info
        self._update_dir = update_dir
        self._cancel_event = threading.Event()
        self._cancel_allowed = True

    def request_cancel(self) -> None:
        if self._cancel_allowed:
            self._cancel_event.set()

    @Slot()
    def run(self) -> None:
        result, error_stage = "failure", "worker_start"
        try:
            _LOGGER.info("event=update_worker_received source=%s app_id=%s record_id=%s sha256_length=%s",
                         self._info.connection_source, self._info.app_id,
                         self._info.record_id, len(self._info.sha256))

            def stage(name: str, message: str) -> None:
                nonlocal error_stage
                error_stage = name
                if name != "download":
                    self._cancel_allowed = False
                self.stage_changed.emit(name, message)

            path = update_client.prepare_installer(
                self._info, self._update_dir,
                progress_callback=self.download_progress.emit,
                stage_callback=stage, cancel_check=self._cancel_event.is_set,
            )
            self.installer_path = path
            result = "success"
            self.installer_ready.emit(str(path))
            _LOGGER.info("event=update_download_worker_result_emitted result=success")
        except update_client.UpdateCancelled:
            result = "cancelled"
            self.cancelled.emit()
            _LOGGER.info("event=update_download_worker_result_emitted result=cancelled")
        except Exception as exc:  # noqa: BLE001
            self.public_error = str(exc)
            self.diagnostic = getattr(exc, "diagnostic", f"{type(exc).__name__}: {exc}")
            _LOGGER.error("event=update_failed error_type=%s error_stage=%s error_message=%s traceback=%s",
                          type(exc).__name__, error_stage,
                          update_client._safe_error_message(exc),
                          update_client._redact_sensitive_text(traceback.format_exc()))
            self.failed.emit(self.public_error, self.diagnostic)
            _LOGGER.info("event=update_download_worker_result_emitted result=failure")
        finally:
            _LOGGER.info("event=update_download_worker_terminal_emitted result=%s", result)
            self.terminal.emit(result)


class InstallerLaunchWorker(QObject):
    stage_changed = Signal(str, str)
    confirmed = Signal()
    failed = Signal(str, str)
    terminal = Signal(str)

    def __init__(self, installer_path: Path, app_exe_path: Path) -> None:
        super().__init__()
        self._installer_path = installer_path
        self._app_exe_path = app_exe_path

    @Slot()
    def run(self) -> None:
        result = "failure"
        try:
            self.stage_changed.emit("waiting_for_elevation", "管理者の確認を待っています")
            update_client.start_installer_for_update(self._installer_path, self._app_exe_path)
            # start_installer_for_update returns only after ShellExecuteExW, a live
            # process, and this attempt's non-empty Inno Setup log are confirmed.
            self.confirmed.emit()
            result = "success"
        except update_client.ElevationCancelled as exc:
            self.public_error = "管理者の確認がキャンセルされました。更新は開始されていません。"
            self.diagnostic = str(exc)
            self.failed.emit(self.public_error, self.diagnostic)
        except Exception as exc:  # noqa: BLE001
            self.public_error = str(exc)
            self.diagnostic = f"{type(exc).__name__}: {exc}"
            self.failed.emit(self.public_error, self.diagnostic)
        finally:
            _LOGGER.info("event=update_installer_launch_worker_finished")
            self.terminal.emit(result)


class UpdateProgressDialog(QDialog):
    """Display-only update UI. It never owns the controller or workers."""

    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None, *, debug_display: bool = False) -> None:
        super().__init__(parent)
        self._controller_ref: weakref.ReferenceType[UpdateController] | None = None
        self._debug_display = debug_display
        self._failure_close_enabled = False
        self.setWindowTitle("TksToKintone アップデート")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(480)
        self.status_label = QLabel("更新ファイルを準備しています")
        self.progress_bar = QProgressBar(); self.progress_bar.setRange(0, 0)
        self.bytes_label = QLabel(""); self.bytes_label.setVisible(False)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True)
        self.details.setVisible(False); self.details.setMaximumHeight(110)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self._button_clicked)
        layout = QVBoxLayout(self)
        for widget in (self.status_label, self.progress_bar, self.bytes_label, self.details):
            layout.addWidget(widget)
        layout.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignRight)

    def bind_controller(self, controller: UpdateController) -> None:
        self._controller_ref = weakref.ref(controller)

    def _state_name(self) -> str:
        controller = self._controller_ref() if self._controller_ref else None
        return controller.state.value if controller else UpdateState.IDLE.value

    def _log_action(self, action: str) -> None:
        caller = inspect.stack()[2].function
        _LOGGER.info("event=update_dialog_action action=%s state=%s caller=%s",
                     action, self._state_name(), caller)

    def _button_clicked(self) -> None:
        if self._failure_close_enabled:
            self.close()
        else:
            self.cancel_button.setEnabled(False)
            self.status_label.setText("更新をキャンセルしています")
            self.cancel_requested.emit()

    @Slot(int, int)
    def show_progress(self, received: int, total: int) -> None:
        if total > 0:
            percent = min(100, int(received * 100 / total))
            self.progress_bar.setRange(0, 100); self.progress_bar.setValue(percent)
            self.status_label.setText(f"ダウンロード中... {percent}%")
            self.bytes_label.setText(f"{_format_bytes(received)} / {_format_bytes(total)}")
        else:
            self.progress_bar.setRange(0, 0)
            self.status_label.setText("ダウンロード中")
            self.bytes_label.setText(f"{_format_bytes(received)} / 全容量不明")
        self.bytes_label.setVisible(True)

    @Slot(str)
    def show_stage(self, message: str) -> None:
        self.status_label.setText(message)
        self.progress_bar.setRange(0, 100); self.progress_bar.setValue(100)
        self.bytes_label.setVisible(False)

    def show_failure(self, message: str, diagnostic: str) -> None:
        self.status_label.setText(message)
        self.details.setPlainText(diagnostic if self._debug_display else "")
        self.details.setVisible(self._debug_display and bool(diagnostic))
        self.cancel_button.setText("閉じる"); self.cancel_button.setEnabled(True)
        self._failure_close_enabled = True
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True); self.show()

    def lock_cancellation(self) -> None:
        self.cancel_button.setEnabled(False)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False); self.show()

    def close(self) -> bool:
        self._log_action("close")
        return super().close()

    def accept(self) -> None:
        self._log_action("accept")
        if self._failure_close_enabled: super().accept()

    def reject(self) -> None:
        self._log_action("reject")
        if self._failure_close_enabled: super().reject()

    def hide(self) -> None:
        self._log_action("hide")
        if self._failure_close_enabled: super().hide()

    def deleteLater(self) -> None:
        self._log_action("deleteLater")
        super().deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self._failure_close_enabled:
            event.ignore()
            return
        super().closeEvent(event)


class UpdateController(QObject):
    """Long-lived owner of the complete update transaction."""

    state_changed = Signal(str, str)
    finished = Signal(bool)

    def __init__(self, info: update_client.UpdateInfo, update_dir: Path,
                 app_exe_path: Path, parent: QObject,
                 pre_install_check: Callable[[], bool] | None = None,
                 debug_display: bool = False) -> None:
        super().__init__(parent)
        self.state = UpdateState.IDLE
        self._pre_install_check = pre_install_check or (lambda: True)
        self._app_exe_path = app_exe_path
        self._verified_installer_path: Path | None = None
        self._pending_result: str | None = None
        self._pending_failure: tuple[str, str] = ("更新に失敗しました。", "")
        widget_parent = parent if isinstance(parent, QWidget) else None
        self.progress_dialog = UpdateProgressDialog(widget_parent, debug_display=debug_display)
        self.progress_dialog.bind_controller(self)
        self.progress_dialog.cancel_requested.connect(self.cancel)
        self.download_thread: QThread | None = QThread()
        self.download_worker: UpdateWorker | None = UpdateWorker(info, update_dir)
        self.installer_thread: QThread | None = None
        self.installer_worker: InstallerLaunchWorker | None = None
        thread, worker = self.download_thread, self.download_worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.stage_changed.connect(self._on_download_stage)
        worker.download_progress.connect(self.progress_dialog.show_progress)
        worker.installer_ready.connect(self._save_installer_result)
        worker.failed.connect(self._save_failure)
        worker.terminal.connect(self._on_download_terminal)
        worker.terminal.connect(worker.deleteLater, Qt.ConnectionType.DirectConnection)
        worker.terminal.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(self._on_download_thread_finished)
        _log_thread_event("update_download_thread_created", thread)

    def _transition(self, new: UpdateState) -> None:
        old = self.state
        if old == new: return
        self.state = new
        _LOGGER.info("event=update_state_changed from=%s to=%s", old.value, new.value)
        self.state_changed.emit(old.value, new.value)

    def start(self) -> None:
        if self.state != UpdateState.IDLE or self.download_thread is None: return
        self._transition(UpdateState.DOWNLOADING)
        self.progress_dialog.show()
        _ACTIVE_UPDATE_THREADS.add(self.download_thread)
        self.download_thread.start()

    @Slot(str, str)
    def _on_download_stage(self, stage: str, message: str) -> None:
        mapping = {"verify_file": UpdateState.VERIFYING_SIZE,
                   "verify_sha256": UpdateState.VERIFYING_SHA256,
                   "verify_pe": UpdateState.VERIFYING_PE,
                   "installer_ready": UpdateState.PUBLISHING_INSTALLER}
        if stage in mapping:
            self._transition(mapping[stage])
            self.progress_dialog.lock_cancellation()
        self.progress_dialog.show_stage(message) if stage != "download" else None

    @Slot(str)
    def _save_installer_result(self, path: str) -> None:
        self._verified_installer_path = Path(path)

    @Slot(str, str)
    def _save_failure(self, message: str, diagnostic: str) -> None:
        self._pending_failure = (message, diagnostic)

    @Slot(str)
    def _on_download_terminal(self, result: str) -> None:
        self._pending_result = result
        thread = self.download_thread
        if result == "success": self._transition(UpdateState.WAITING_DOWNLOAD_THREAD_FINISHED)
        if thread is not None:
            _log_thread_event("update_download_thread_quit_requested", thread)

    @Slot()
    def _on_download_thread_finished(self) -> None:
        thread = self.download_thread
        if thread is None or thread.isRunning(): return
        _log_thread_event("update_download_thread_finished", thread)
        _ACTIVE_UPDATE_THREADS.discard(thread); thread.deleteLater()
        self.download_worker = None; self.download_thread = None
        if self._pending_result == "success" and self._verified_installer_path:
            QTimer.singleShot(0, self._start_installer_worker)
        elif self._pending_result == "cancelled":
            self._finish_failure(UpdateState.CANCELLED, "更新をキャンセルしました。", "")
        else:
            self._finish_failure(UpdateState.FAILED, *self._pending_failure)

    @Slot()
    def _start_installer_worker(self) -> None:
        if self.download_thread is not None or not self._verified_installer_path: return
        try: can_exit = bool(self._pre_install_check())
        except Exception:  # pragma: no cover
            _LOGGER.exception("event=update_preflight_failed"); can_exit = False
        if not can_exit:
            update_client.discard_prepared_installer(self._verified_installer_path)
            self._finish_failure(UpdateState.CANCELLED, "更新をキャンセルしました。", "")
            return
        self._transition(UpdateState.LAUNCHING_INSTALLER)
        self.progress_dialog.show_stage("インストーラー起動中")
        thread = QThread()
        worker = InstallerLaunchWorker(self._verified_installer_path, self._app_exe_path)
        self.installer_thread, self.installer_worker = thread, worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.stage_changed.connect(lambda _stage, msg: self.progress_dialog.show_stage(msg))
        worker.confirmed.connect(self._on_installer_confirmed)
        worker.failed.connect(self._save_failure)
        worker.terminal.connect(self._on_installer_terminal)
        worker.terminal.connect(worker.deleteLater, Qt.ConnectionType.DirectConnection)
        worker.terminal.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(self._on_installer_thread_finished)
        _ACTIVE_UPDATE_THREADS.add(thread)
        _log_thread_event("update_installer_worker_created", thread)
        thread.start()

    @Slot()
    def _on_installer_confirmed(self) -> None:
        self._transition(UpdateState.WAITING_INSTALLER_CONFIRMATION)
        self.progress_dialog.show_stage("更新を開始します")

    @Slot(str)
    def _on_installer_terminal(self, result: str) -> None:
        self._pending_result = result

    @Slot()
    def _on_installer_thread_finished(self) -> None:
        thread = self.installer_thread
        if thread is None or thread.isRunning(): return
        _ACTIVE_UPDATE_THREADS.discard(thread); thread.deleteLater()
        self.installer_worker = None; self.installer_thread = None
        if self._pending_result == "success" and self.state == UpdateState.WAITING_INSTALLER_CONFIRMATION:
            self._commit_shutdown()
        else:
            if self._verified_installer_path: update_client.discard_prepared_installer(self._verified_installer_path)
            self._finish_failure(UpdateState.FAILED, *self._pending_failure)

    def _commit_shutdown(self) -> None:
        self._transition(UpdateState.SHUTDOWN_COMMITTED)
        self.finished.emit(True)
        from app.gui import quit_app_for_update
        quit_app_for_update()

    def _finish_failure(self, state: UpdateState, message: str, diagnostic: str) -> None:
        if self.download_thread is not None or self.installer_thread is not None: return
        self._transition(state)
        self.progress_dialog.show_failure(message, diagnostic)
        self.finished.emit(False)

    @Slot()
    def cancel(self) -> None:
        if self.state == UpdateState.DOWNLOADING and self.download_worker is not None:
            self.download_worker.request_cancel()

    @property
    def active_thread_count(self) -> int:
        return sum(t is not None and t.isRunning() for t in
                   (self.download_thread, self.installer_thread))


def start_update(parent: QWidget, info: update_client.UpdateInfo,
                 app_exe_path: Path) -> UpdateController:
    """Start asynchronously and retain the controller on the application window."""
    from app.gui import prepare_app_for_update
    from PySide6.QtCore import QSettings
    raw = QSettings("Manekiya", "TksToKintone").value("ui/debug_visible", "0")
    debug = raw if isinstance(raw, bool) else str(raw).strip().lower() in {"1", "true", "yes", "on"}
    controller = UpdateController(info, update_client.default_update_dir(), app_exe_path,
                                  parent, prepare_app_for_update, bool(debug))
    setattr(parent, "_update_controller", controller)
    controller.start()
    return controller


def run_update_dialog(parent: QWidget, info: update_client.UpdateInfo,
                      app_exe_path: Path) -> bool:
    """Compatibility wrapper: start the asynchronous controller; never exec a dialog."""
    start_update(parent, info, app_exe_path)
    return True


def _format_bytes(value: int) -> str:
    amount = float(max(value, 0))
    for unit in ("bytes", "KB", "MB", "GB"):
        if amount < 1024.0 or unit == "GB":
            return f"{amount:.1f} {unit}" if unit != "bytes" else f"{int(amount)} bytes"
        amount /= 1024.0
    return f"{amount:.1f} GB"
