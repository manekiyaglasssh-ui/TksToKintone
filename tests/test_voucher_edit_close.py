"""rembg削除後の指図書編集・保存終了回帰テスト。"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QCloseEvent, QImage
    from PySide6.QtWidgets import QApplication, QMainWindow
    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditClose(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous = os.environ.get("TKS_TO_KINTONE_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        self.app.setProperty("update_shutdown_committed", False)
        if self._previous is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous
        self._tmp.cleanup()

    def _window(self, parent=None):
        from app.voucher_edit_window import VoucherEditWindow
        win = VoucherEditWindow("CLOSE", b"", parent=parent)
        self.addCleanup(win.deleteLater)
        return win

    @staticmethod
    def _png() -> bytes:
        from app.voucher_edit_window import qimage_to_png_bytes
        image = QImage(6, 4, QImage.Format.Format_ARGB32)
        image.fill(0xFFFFFFFF)
        return qimage_to_png_bytes(image)

    def test_threshold_processing_then_discard_close(self) -> None:
        parent = QMainWindow()
        self.addCleanup(parent.deleteLater)
        win = self._window(parent)
        win.add_image(self._png(), rect=QRectF(10, 10, 30, 20), select=True)
        win._on_threshold_transparent()
        event = QCloseEvent()
        with mock.patch.object(win, "_prompt_unsaved_changes", return_value="discard"):
            win.closeEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertIsNotNone(parent.metaObject())

    def test_save_and_close_persists_without_thread_wait(self) -> None:
        win = self._window()
        win.add_image(self._png(), rect=QRectF(10, 10, 30, 20), select=True)
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        self.assertFalse(win.is_dirty())
        self.assertFalse(hasattr(win, "_blocking_image_threads"))
        self.assertFalse(hasattr(win, "_warmup_threads"))

    def test_source_has_no_background_worker_or_warmup(self) -> None:
        from pathlib import Path
        source = Path("app/voucher_edit_window.py").read_text(encoding="utf-8")
        for token in ("RembgWarmupWorker", "BackgroundRemovalWorker", "_start_rembg_warmup"):
            self.assertNotIn(token, source)

    def test_committed_update_shutdown_never_prompts_unsaved_changes_again(self) -> None:
        from app.gui import UPDATE_SHUTDOWN_COMMITTED_PROPERTY

        win = self._window()
        self.app.setProperty(UPDATE_SHUTDOWN_COMMITTED_PROPERTY, True)
        event = QCloseEvent()
        with mock.patch.object(win, "is_dirty", return_value=True), mock.patch.object(
            win, "_prompt_unsaved_changes"
        ) as prompt:
            win.closeEvent(event)
        prompt.assert_not_called()
        self.assertTrue(event.isAccepted())


if __name__ == "__main__":
    unittest.main()
