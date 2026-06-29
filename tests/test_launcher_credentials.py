"""機能選択画面（LauncherWindow）のログイン情報保存・自動入力の動的テスト。"""
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
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestLauncherCredentials(unittest.TestCase):
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

        import app.credential_store as cs

        self._cs_patch = mock.patch.object(cs, "keyring", None)
        self._cs_patch.start()
        self.addCleanup(self._cs_patch.stop)
        self.cs = cs
        cs.clear_saved_credentials()

    def _make_window(self):
        from app.launcher_window import LauncherWindow

        # config.env 由来の kintone 移行読み込みは無効化して隔離する。
        patcher = mock.patch(
            "app.launcher_window.dotenv_values", return_value={}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        win = LauncherWindow()
        self.addCleanup(win.deleteLater)
        return win

    def test_olap_login_success_saves_olap_credentials(self) -> None:
        win = self._make_window()
        win._olap_id.setText("olap-id")
        win._olap_password.setText("olap-pw")
        with mock.patch.object(win, "_load_config_or_warn", return_value=object()), \
             mock.patch.object(win, "_verify_olap_login", return_value=True):
            self.assertTrue(win._authorize_olap())
        saved = self.cs.load_saved_credentials()
        self.assertEqual(saved.olap_login_id, "olap-id")
        self.assertEqual(saved.olap_password, "olap-pw")

    def test_olap_login_failure_does_not_save(self) -> None:
        win = self._make_window()
        win._olap_id.setText("olap-id")
        win._olap_password.setText("olap-pw")
        with mock.patch.object(win, "_load_config_or_warn", return_value=object()), \
             mock.patch.object(win, "_verify_olap_login", return_value=False):
            self.assertFalse(win._authorize_olap())
        saved = self.cs.load_saved_credentials()
        self.assertEqual(saved.olap_login_id, "")
        self.assertEqual(saved.olap_password, "")

    def test_kintone_both_success_saves_both(self) -> None:
        win = self._make_window()
        win._olap_id.setText("olap-id")
        win._olap_password.setText("olap-pw")
        win._kintone_id.setText("k-id")
        win._kintone_password.setText("k-pw")
        with mock.patch.object(win, "_load_config_or_warn", return_value=object()), \
             mock.patch.object(win, "_verify_olap_login", return_value=True), \
             mock.patch.object(win, "_verify_kintone_connection", return_value=True):
            self.assertTrue(win._authorize_kintone())
        saved = self.cs.load_saved_credentials()
        self.assertEqual(saved.olap_login_id, "olap-id")
        self.assertEqual(saved.olap_password, "olap-pw")
        self.assertEqual(saved.kintone_login_id, "k-id")
        self.assertEqual(saved.kintone_password, "k-pw")

    def test_kintone_connection_failure_does_not_save_kintone(self) -> None:
        win = self._make_window()
        win._olap_id.setText("olap-id")
        win._olap_password.setText("olap-pw")
        win._kintone_id.setText("k-id")
        win._kintone_password.setText("k-pw")
        with mock.patch.object(win, "_load_config_or_warn", return_value=object()), \
             mock.patch.object(win, "_verify_olap_login", return_value=True), \
             mock.patch.object(win, "_verify_kintone_connection", return_value=False):
            self.assertFalse(win._authorize_kintone())
        saved = self.cs.load_saved_credentials()
        self.assertEqual(saved.kintone_login_id, "")
        self.assertEqual(saved.kintone_password, "")

    def test_saved_olap_credentials_autofilled_on_startup(self) -> None:
        self.cs.save_olap_credentials("saved-olap", "saved-olap-pw")
        win = self._make_window()
        self.assertEqual(win._olap_id.text(), "saved-olap")
        self.assertEqual(win._olap_password.text(), "saved-olap-pw")

    def test_saved_kintone_credentials_autofilled_on_startup(self) -> None:
        self.cs.save_kintone_credentials("saved-k", "saved-k-pw")
        win = self._make_window()
        self.assertEqual(win._kintone_id.text(), "saved-k")
        self.assertEqual(win._kintone_password.text(), "saved-k-pw")

    def test_buttons_reevaluated_after_loading_credentials(self) -> None:
        # OLAP のみ保存 → 伝票ボタン有効・kintoneボタン無効。
        self.cs.save_olap_credentials("saved-olap", "saved-olap-pw")
        win = self._make_window()
        self.assertTrue(win._voucher_btn.isEnabled())
        self.assertFalse(win._kintone_btn.isEnabled())

    def test_kintone_button_enabled_when_both_saved(self) -> None:
        self.cs.save_olap_credentials("saved-olap", "saved-olap-pw")
        self.cs.save_kintone_credentials("saved-k", "saved-k-pw")
        win = self._make_window()
        self.assertTrue(win._voucher_btn.isEnabled())
        self.assertTrue(win._kintone_btn.isEnabled())

    def test_password_fields_remain_masked(self) -> None:
        from PySide6.QtWidgets import QLineEdit

        win = self._make_window()
        self.assertEqual(win._olap_password.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(win._kintone_password.echoMode(), QLineEdit.EchoMode.Password)


if __name__ == "__main__":
    unittest.main()
