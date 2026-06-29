from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class LauncherDebugSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            self._tmp.name,
        )
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        self.settings = QSettings("Manekiya", "TksToKintone")
        self.settings.clear()
        self.settings.sync()

    def _dialog(self):
        from app.launcher_window import LauncherSettingsDialog

        dialog = LauncherSettingsDialog(self.settings)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_debug_visible_off_to_on_requires_admin_password(self) -> None:
        dialog = self._dialog()
        with mock.patch("app.launcher_window.QInputDialog.getText", return_value=("admin", True)):
            dialog.debug_visible.setChecked(True)
        self.assertTrue(dialog.debug_visible.isChecked())

    def test_debug_visible_wrong_password_does_not_turn_on(self) -> None:
        dialog = self._dialog()
        with mock.patch("app.launcher_window.QInputDialog.getText", return_value=("wrong", True)):
            dialog.debug_visible.setChecked(True)
        self.assertFalse(dialog.debug_visible.isChecked())

    def test_debug_visible_cancel_does_not_turn_on(self) -> None:
        dialog = self._dialog()
        with mock.patch("app.launcher_window.QInputDialog.getText", return_value=("", False)):
            dialog.debug_visible.setChecked(True)
        self.assertFalse(dialog.debug_visible.isChecked())

    def test_debug_visible_on_to_off_does_not_require_password(self) -> None:
        self.settings.setValue("ui/debug_visible", "1")
        dialog = self._dialog()
        with mock.patch("app.launcher_window.QInputDialog.getText") as get_text:
            dialog.debug_visible.setChecked(False)
        get_text.assert_not_called()
        self.assertFalse(dialog.debug_visible.isChecked())


if __name__ == "__main__":
    unittest.main()
