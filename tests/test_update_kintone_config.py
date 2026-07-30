from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings

from app.update_kintone_config import (
    normalize_update_kintone_api_token,
    normalize_update_kintone_app_id,
    resolve_update_kintone_config,
)


class UpdateKintoneConfigTest(unittest.TestCase):
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

    def _resolve(self):
        return resolve_update_kintone_config(
            settings=self.settings,
            production_app_id="250",
            production_api_token="production-token",
        )

    def test_debug_disabled_always_uses_production(self) -> None:
        self.settings.setValue("update/debug_kintone_app_id", "999")
        with mock.patch(
            "app.update_kintone_config.load_update_debug_kintone_api_token",
            return_value="debug-token",
        ):
            config = self._resolve()
        self.assertEqual(config.source, "production")
        self.assertEqual(config.app_id, "250")

    def test_complete_debug_override_is_used(self) -> None:
        self.settings.setValue("ui/debug_visible", "1")
        self.settings.setValue("update/debug_kintone_app_id", "999")
        with mock.patch(
            "app.update_kintone_config.load_update_debug_kintone_api_token",
            return_value="debug-token",
        ):
            config = self._resolve()
        self.assertEqual(config.source, "debug_override")
        self.assertEqual(config.app_id, "999")
        self.assertEqual(config.api_token, "debug-token")

    def test_both_debug_values_empty_use_production(self) -> None:
        self.settings.setValue("ui/debug_visible", "1")
        with mock.patch(
            "app.update_kintone_config.load_update_debug_kintone_api_token",
            return_value="",
        ):
            config = self._resolve()
        self.assertEqual(config.source, "production")

    def test_incomplete_debug_override_is_configuration_error(self) -> None:
        self.settings.setValue("ui/debug_visible", "1")
        self.settings.setValue("update/debug_kintone_app_id", "999")
        with mock.patch(
            "app.update_kintone_config.load_update_debug_kintone_api_token",
            return_value="",
        ):
            with self.assertRaises(ValueError):
                self._resolve()

    def test_invalid_stored_debug_override_is_configuration_error(self) -> None:
        self.settings.setValue("ui/debug_visible", "1")
        self.settings.setValue("update/debug_kintone_app_id", "not-a-number")
        with mock.patch(
            "app.update_kintone_config.load_update_debug_kintone_api_token",
            return_value="secret-that-must-not-leak",
        ):
            with self.assertRaises(ValueError) as raised:
                self._resolve()
        self.assertNotIn("secret-that-must-not-leak", str(raised.exception))

    def test_normalizers_reject_invalid_values_without_echoing_secret(self) -> None:
        self.assertEqual(normalize_update_kintone_app_id("１２３"), "123")
        with self.assertRaises(ValueError):
            normalize_update_kintone_app_id("1.5")
        secret = "secret\nvalue"
        with self.assertRaises(ValueError) as raised:
            normalize_update_kintone_api_token(secret)
        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
