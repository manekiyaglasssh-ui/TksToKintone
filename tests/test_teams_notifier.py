from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False


class TeamsPayloadTest(unittest.TestCase):
    def test_build_kintone_order_url(self) -> None:
        from app.teams_notifier import KINTONE_TARGET_PROD, KINTONE_TARGET_TEST, build_kintone_order_url

        self.assertEqual(
            build_kintone_order_url("1405113", target=KINTONE_TARGET_PROD),
            "https://manekiya.cybozu.com/k/211/"
            "?view=20&q=f8257622%20%3D%20%221405113%22"
            "#sort_0=f8256572&order_0=desc&size=20",
        )
        self.assertEqual(
            build_kintone_order_url("1402816", target=KINTONE_TARGET_TEST),
            "https://manekiya.cybozu.com/k/255/"
            "?view=20&q=f8257622%20%3D%20%221402816%22"
            "#sort_0=f8256572&order_0=desc&size=20",
        )

    def test_build_kintone_order_url_preserves_leading_zero_and_encodes(self) -> None:
        from app.teams_notifier import build_kintone_order_url

        url = build_kintone_order_url("001405113")
        self.assertIn("%22001405113%22", url)
        self.assertIn("%20%3D%20", url)
        self.assertNotIn('f8257622 = "001405113"', url)
        self.assertNotIn("新規", url)
        self.assertNotIn("更新", url)

    def test_payload_is_adaptive_card_with_order_links_only(self) -> None:
        from app.teams_notifier import KINTONE_TARGET_TEST, build_teams_order_links_payload

        payload = build_teams_order_links_payload(
            [{"order_no": "1405113", "label": "新規"}, {"order_no": "1402088", "label": "更新"}],
            target=KINTONE_TARGET_TEST,
        )
        self.assertEqual(payload["type"], "message")
        attachments = payload["attachments"]
        self.assertEqual(len(attachments), 1)
        content = attachments[0]["content"]
        self.assertEqual(content["type"], "AdaptiveCard")
        body = content["body"]
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["type"], "TextBlock")
        text = body[0]["text"]
        self.assertEqual(text.count("\n"), 1)
        self.assertIn("[1405113（新規）](https://manekiya.cybozu.com/k/255/?view=20&q=", text)
        self.assertIn("[1402088（更新）](https://manekiya.cybozu.com/k/255/?view=20&q=", text)
        self.assertNotIn("Kintone登録が完了しました", text)
        self.assertNotIn("登録が完了しました", text)
        self.assertNotIn("受注No：", text)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TeamsSettingsAndVoucherIntegrationTest(unittest.TestCase):
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
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()
        self.settings = settings

    def _make_voucher_window(self):
        from app.voucher_window import VoucherWindow

        with mock.patch.object(VoucherWindow, "_save_records"):
            win = VoucherWindow(olap_login_id="id", olap_password="pw")
        win._on_add_row()
        input_row = getattr(win, "_new_input_row", None)
        if input_row is not None:
            logical_index = input_row.table_row_index
            if logical_index >= 0:
                win._table.removeRow(logical_index)
            win._new_input_row = None
            for row in win._rows:
                if row.table_row_index > logical_index:
                    row.table_row_index -= 1
        # 他テストが使用した保存済み伝票状態に依存させない。
        win._registration_status_by_order.clear()
        self.addCleanup(win.deleteLater)
        return win

    def test_settings_dialog_defaults_and_saves_teams_values(self) -> None:
        from app.gui import (
            SETTINGS_DEBUG_VISIBLE,
            SETTINGS_TEAMS_ENABLED,
            SETTINGS_TEAMS_WEBHOOK_URL_PROD,
            SETTINGS_TEAMS_WEBHOOK_URL_TEST,
            SettingsDialog,
        )

        with mock.patch.dict(
            os.environ,
            {
                "TKS_TEAMS_WEBHOOK_URL_TEST_DEFAULT": "https://default.test/webhook?sig=secret-test",
                "TKS_TEAMS_WEBHOOK_URL_PROD_DEFAULT": "https://default.prod/webhook?sig=secret-prod",
            },
        ):
            self.settings.setValue(SETTINGS_DEBUG_VISIBLE, "1")
            dialog = SettingsDialog(None, self.settings)
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.teams_enabled.isChecked())
        self.assertTrue(dialog.teams_webhook_url_test.text())
        self.assertTrue(dialog.teams_webhook_url_prod.text())
        self.assertEqual(dialog.teams_webhook_url_test.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(dialog.teams_webhook_url_prod.echoMode(), QLineEdit.EchoMode.Password)
        self.assertFalse(dialog.teams_webhook_url_test.isVisible())
        self.assertFalse(dialog.teams_webhook_url_prod.isVisible())

        dialog.teams_enabled.setChecked(True)
        dialog.teams_webhook_url_test.setText("https://example.test/webhook?sig=secret-test")
        dialog.teams_webhook_url_prod.setText("https://example.prod/webhook?sig=secret-prod")
        dialog.accept()

        self.assertEqual(self.settings.value(SETTINGS_TEAMS_ENABLED), True)
        self.assertEqual(
            self.settings.value(SETTINGS_TEAMS_WEBHOOK_URL_TEST),
            "https://example.test/webhook?sig=secret-test",
        )
        self.assertEqual(
            self.settings.value(SETTINGS_TEAMS_WEBHOOK_URL_PROD),
            "https://example.prod/webhook?sig=secret-prod",
        )

    def test_debug_off_disables_target_and_teams_and_preserves_values(self) -> None:
        from app.gui import (
            KINTONE_TARGET_PROD,
            KINTONE_TARGET_TEST,
            SETTINGS_DEBUG_VISIBLE,
            SETTINGS_KINTONE_TARGET,
            SETTINGS_TEAMS_ENABLED,
            SettingsDialog,
        )

        self.settings.setValue(SETTINGS_DEBUG_VISIBLE, "0")
        self.settings.setValue(SETTINGS_KINTONE_TARGET, KINTONE_TARGET_PROD)
        self.settings.setValue(SETTINGS_TEAMS_ENABLED, True)
        dialog = SettingsDialog(None, self.settings)
        self.addCleanup(dialog.deleteLater)

        self.assertFalse(dialog.kintone_target.isEnabled())
        self.assertFalse(dialog.teams_enabled.isEnabled())
        self.assertEqual(dialog.kintone_target.currentData(), KINTONE_TARGET_PROD)
        self.assertTrue(dialog.teams_enabled.isChecked())

        dialog.kintone_target.setCurrentIndex(
            dialog.kintone_target.findData(KINTONE_TARGET_TEST)
        )
        dialog.teams_enabled.setChecked(False)
        dialog.accept()

        self.assertEqual(self.settings.value(SETTINGS_KINTONE_TARGET), KINTONE_TARGET_PROD)
        self.assertEqual(self.settings.value(SETTINGS_TEAMS_ENABLED, type=bool), True)

    def test_debug_on_enables_target_and_teams(self) -> None:
        from app.gui import SETTINGS_DEBUG_VISIBLE, SettingsDialog

        self.settings.setValue(SETTINGS_DEBUG_VISIBLE, "1")
        dialog = SettingsDialog(None, self.settings)
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.kintone_target.isEnabled())
        self.assertTrue(dialog.teams_enabled.isEnabled())

    def test_saved_teams_enabled_value_wins_over_default_on(self) -> None:
        from app.gui import SETTINGS_TEAMS_ENABLED, SettingsDialog

        self.settings.setValue(SETTINGS_TEAMS_ENABLED, "0")
        dialog = SettingsDialog(None, self.settings)
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.teams_enabled.isChecked())

    def test_webhook_url_fields_visible_only_when_debug_visible(self) -> None:
        from app.gui import SETTINGS_DEBUG_VISIBLE, SettingsDialog

        dialog = SettingsDialog(None, self.settings)
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.teams_webhook_url_test.isVisible())
        self.assertFalse(dialog.teams_webhook_url_prod.isVisible())

        self.settings.setValue(SETTINGS_DEBUG_VISIBLE, "1")
        debug_dialog = SettingsDialog(None, self.settings)
        self.addCleanup(debug_dialog.deleteLater)
        debug_dialog.show()
        self.assertTrue(debug_dialog.teams_webhook_url_test.isVisible())
        self.assertTrue(debug_dialog.teams_webhook_url_prod.isVisible())

    def test_webhook_url_switches_by_kintone_target(self) -> None:
        from app.voucher_window import (
            SETTINGS_KINTONE_TARGET,
            SETTINGS_KINTONE_TARGET_PROD,
            SETTINGS_KINTONE_TARGET_TEST,
            SETTINGS_TEAMS_WEBHOOK_URL_PROD,
            SETTINGS_TEAMS_WEBHOOK_URL_TEST,
        )

        win = self._make_voucher_window()
        self.settings.setValue(SETTINGS_TEAMS_WEBHOOK_URL_TEST, "https://test.example/webhook")
        self.settings.setValue(SETTINGS_TEAMS_WEBHOOK_URL_PROD, "https://prod.example/webhook")
        self.settings.setValue(SETTINGS_KINTONE_TARGET, SETTINGS_KINTONE_TARGET_TEST)
        self.assertEqual(win._teams_webhook_url_for_current_kintone_target(self.settings), "https://test.example/webhook")
        self.settings.setValue(SETTINGS_KINTONE_TARGET, SETTINGS_KINTONE_TARGET_PROD)
        self.assertEqual(win._teams_webhook_url_for_current_kintone_target(self.settings), "https://prod.example/webhook")

    def test_teams_off_does_not_send(self) -> None:
        from app.voucher_window import SETTINGS_TEAMS_ENABLED

        win = self._make_voucher_window()
        self.settings.setValue(SETTINGS_TEAMS_ENABLED, False)
        with mock.patch("app.voucher_window.post_teams_webhook") as post:
            win._notify_teams_registration_completed([{"order_no": "1405113", "label": "新規"}])
        post.assert_not_called()

    def test_missing_webhook_does_not_send(self) -> None:
        from app.voucher_window import SETTINGS_TEAMS_ENABLED, SETTINGS_TEAMS_WEBHOOK_URL_PROD

        win = self._make_voucher_window()
        self.settings.setValue(SETTINGS_TEAMS_ENABLED, True)
        self.settings.setValue(SETTINGS_TEAMS_WEBHOOK_URL_PROD, "")
        with mock.patch("app.voucher_window.post_teams_webhook") as post:
            win._notify_teams_registration_completed([{"order_no": "1405113", "label": "新規"}])
        post.assert_not_called()

    def test_registration_success_notifies_unique_orders_with_new_and_update_labels(self) -> None:
        from app.voucher_window import KINTONE_STATUS_COMPLETED, SETTINGS_TEAMS_ENABLED

        win = self._make_voucher_window()
        win._rows[0].order_input.setText("1405113")
        win._registration_status_by_order["already"] = KINTONE_STATUS_COMPLETED
        self.settings.setValue(SETTINGS_TEAMS_ENABLED, True)
        with mock.patch.object(win, "_teams_webhook_url_for_current_kintone_target", return_value="https://example/webhook"), \
                mock.patch("app.voucher_window.post_teams_webhook") as post, \
                mock.patch.object(win, "_save_records") as save_records:
            win.notify_kintone_registration_completed(["1405113", "already", "1405113"])

        save_records.assert_called_once()
        post.assert_called_once()
        payload = post.call_args.args[1]
        text = payload["attachments"][0]["content"]["body"][0]["text"]
        self.assertIn("[1405113（新規）]", text)
        self.assertIn("[already（更新）]", text)
        self.assertEqual(text.count("[1405113（新規）]"), 1)
        self.assertEqual(win._registration_status_by_order["1405113"], KINTONE_STATUS_COMPLETED)

    def test_notification_items_are_built_before_status_update(self) -> None:
        from app.voucher_window import KINTONE_STATUS_COMPLETED

        win = self._make_voucher_window()
        self.assertEqual(win._build_teams_notification_items(["1405113"]), [{"order_no": "1405113", "label": "新規"}])
        win._registration_status_by_order["1405113"] = KINTONE_STATUS_COMPLETED
        self.assertEqual(win._build_teams_notification_items(["1405113"]), [{"order_no": "1405113", "label": "更新"}])

    def test_notification_failure_keeps_completed_status_and_does_not_log_url(self) -> None:
        from app.teams_notifier import TeamsNotifyError
        from app.voucher_window import KINTONE_STATUS_COMPLETED, SETTINGS_TEAMS_ENABLED

        secret_url = "https://example/webhook?sig=very-secret"
        win = self._make_voucher_window()
        self.settings.setValue(SETTINGS_TEAMS_ENABLED, True)
        with mock.patch.object(win, "_teams_webhook_url_for_current_kintone_target", return_value=secret_url), \
                mock.patch("app.voucher_window.post_teams_webhook", side_effect=TeamsNotifyError("Teams通知に失敗しました。")), \
                self.assertLogs("tks_to_kintone_app", level="WARNING") as logs:
            win.notify_kintone_registration_completed(["1405113"])

        self.assertEqual(win._registration_status_by_order["1405113"], KINTONE_STATUS_COMPLETED)
        joined = "\n".join(logs.output)
        self.assertNotIn(secret_url, joined)
        self.assertNotIn("very-secret", joined)

    def test_post_teams_webhook_does_not_log_webhook_url_on_failure(self) -> None:
        from app.teams_notifier import TeamsNotifyError, post_teams_webhook

        secret_url = "https://example/webhook?sig=very-secret"
        with mock.patch("app.teams_notifier.requests.post", side_effect=RuntimeError(secret_url)), \
                self.assertLogs("tks_to_kintone_app", level="WARNING") as logs:
            with self.assertRaises(TeamsNotifyError):
                post_teams_webhook(secret_url, {"type": "message"})
        joined = "\n".join(logs.output)
        self.assertNotIn(secret_url, joined)
        self.assertNotIn("very-secret", joined)


if __name__ == "__main__":
    unittest.main()
