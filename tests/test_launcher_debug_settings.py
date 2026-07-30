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
        self.assertTrue(dialog.update_kintone_group.isHidden())

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

    def test_update_kintone_fields_are_visible_only_in_debug_mode(self) -> None:
        dialog = self._dialog()
        self.assertTrue(dialog.update_kintone_group.isHidden())

        with mock.patch(
            "app.launcher_window.QInputDialog.getText",
            return_value=("admin", True),
        ):
            dialog.debug_visible.setChecked(True)
        self.assertFalse(dialog.update_kintone_group.isHidden())

    def test_accept_saves_update_kintone_debug_settings(self) -> None:
        import app.credential_store as credential_store

        self.settings.setValue("ui/debug_visible", "1")
        dialog = self._dialog()
        dialog.update_kintone_app_id.setText("１２３")
        dialog.update_kintone_api_token.setText(" debug-token ")
        with mock.patch.object(
            credential_store, "keyring", None
        ), mock.patch(
            "app.launcher_window.save_update_debug_kintone_api_token"
        ) as save_token:
            dialog.accept()

        self.assertEqual(
            self.settings.value("update/debug_kintone_app_id"), "123"
        )
        save_token.assert_called_once_with("debug-token")

    def test_accept_rejects_incomplete_update_kintone_settings(self) -> None:
        self.settings.setValue("ui/debug_visible", "1")
        dialog = self._dialog()
        dialog.update_kintone_app_id.setText("123")
        dialog.update_kintone_api_token.clear()
        with mock.patch(
            "app.launcher_window.QMessageBox.warning"
        ) as warning:
            dialog.accept()

        warning.assert_called_once()
        self.assertEqual(dialog.result(), 0)

    def test_accept_rejects_invalid_update_kintone_app_id(self) -> None:
        self.settings.setValue("ui/debug_visible", "1")
        dialog = self._dialog()
        dialog.update_kintone_app_id.setText("12.5")
        dialog.update_kintone_api_token.setText("debug-token")
        with mock.patch(
            "app.launcher_window.QMessageBox.warning"
        ) as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertEqual(dialog.result(), 0)

    def test_api_token_is_masked_by_default(self) -> None:
        from PySide6.QtWidgets import QLineEdit

        dialog = self._dialog()
        self.assertEqual(
            dialog.update_kintone_api_token.echoMode(),
            QLineEdit.EchoMode.Password,
        )

    def test_debug_off_accept_preserves_saved_override(self) -> None:
        self.settings.setValue("update/debug_kintone_app_id", "999")
        dialog = self._dialog()
        dialog.update_kintone_app_id.setText("changed-but-hidden")
        dialog.update_kintone_api_token.setText("changed-but-hidden")
        with mock.patch(
            "app.launcher_window.save_update_debug_kintone_api_token"
        ) as save_token:
            dialog.accept()
        self.assertEqual(
            self.settings.value("update/debug_kintone_app_id"), "999"
        )
        save_token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
