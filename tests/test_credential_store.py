"""ログイン情報保存（credential_store）と機能選択画面の保存/自動入力の動的テスト。"""
from __future__ import annotations

import logging
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
class TestCredentialStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # QSettings の保存先をテスト用一時ディレクトリへ隔離する。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            self._tmp.name,
        )
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        import app.credential_store as cs

        # keyring 未導入相当（QSettings フォールバック）でテストする。
        self._cs_patch = mock.patch.object(cs, "keyring", None)
        self._cs_patch.start()
        self.addCleanup(self._cs_patch.stop)
        self.cs = cs
        cs.clear_saved_credentials()

    def test_save_and_load_olap(self) -> None:
        self.cs.save_olap_credentials("olap-id", "olap-pw")
        saved = self.cs.load_saved_credentials()
        self.assertEqual(saved.olap_login_id, "olap-id")
        self.assertEqual(saved.olap_password, "olap-pw")

    def test_save_and_load_kintone(self) -> None:
        self.cs.save_kintone_credentials("k-id", "k-pw")
        saved = self.cs.load_saved_credentials()
        self.assertEqual(saved.kintone_login_id, "k-id")
        self.assertEqual(saved.kintone_password, "k-pw")

    def test_empty_does_not_overwrite_existing(self) -> None:
        self.cs.save_olap_credentials("olap-id", "olap-pw")
        self.cs.save_olap_credentials("", "")
        saved = self.cs.load_saved_credentials()
        self.assertEqual(saved.olap_login_id, "olap-id")
        self.assertEqual(saved.olap_password, "olap-pw")

    def test_clear_removes_all(self) -> None:
        self.cs.save_olap_credentials("olap-id", "olap-pw")
        self.cs.save_kintone_credentials("k-id", "k-pw")
        self.cs.clear_saved_credentials()
        saved = self.cs.load_saved_credentials()
        self.assertEqual(saved.olap_login_id, "")
        self.assertEqual(saved.olap_password, "")
        self.assertEqual(saved.kintone_login_id, "")
        self.assertEqual(saved.kintone_password, "")

    def test_save_load_and_clear_update_debug_api_token(self) -> None:
        self.cs.save_update_debug_kintone_api_token("update-secret")
        self.assertEqual(
            self.cs.load_update_debug_kintone_api_token(), "update-secret"
        )

        self.cs.save_update_debug_kintone_api_token("")
        self.assertEqual(self.cs.load_update_debug_kintone_api_token(), "")

    def test_load_failure_returns_empty(self) -> None:
        with mock.patch.object(self.cs, "_settings", side_effect=RuntimeError("boom")):
            saved = self.cs.load_saved_credentials()
        self.assertEqual(saved.olap_login_id, "")
        self.assertEqual(saved.olap_password, "")

    def test_password_not_logged(self) -> None:
        secret = "S3cretValue!"
        with self.assertLogs("tks_to_kintone_app", level="DEBUG") as captured:
            self.cs.save_olap_credentials("olap-id", secret)
            self.cs.save_kintone_credentials("k-id", secret)
            self.cs.load_saved_credentials()
            # ログが1件も無いと assertLogs が失敗するため、明示的に1行出す。
            logging.getLogger("tks_to_kintone_app").info("credential save done")
        for line in captured.output:
            self.assertNotIn(secret, line)

    def test_save_failure_does_not_raise(self) -> None:
        with mock.patch.object(self.cs, "_store_password", side_effect=RuntimeError("boom")):
            # 例外を送出しないこと（戻り値なし・正常終了）。
            self.cs.save_olap_credentials("olap-id", "olap-pw")


if __name__ == "__main__":
    unittest.main()
