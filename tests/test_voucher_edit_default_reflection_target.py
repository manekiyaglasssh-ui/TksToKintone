"""指図書編集画面の既定反映先テスト。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, QSettings
from PySide6.QtWidgets import QApplication, QMenu

from app import voucher_edit_window as edit
from app.voucher_edit_templates import load_user_templates, save_user_templates


class TestVoucherEditDefaultReflectionTarget(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._temp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = self._temp.name
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            self._temp.name,
        )
        QSettings.setPath(
            QSettings.Format.NativeFormat,
            QSettings.Scope.UserScope,
            self._temp.name,
        )
        self.settings = QSettings(edit.SETTINGS_ORG, edit.SETTINGS_APP)
        self._old_default = self.settings.value(edit.DEFAULT_REFLECTION_TARGET_KEY)
        self.settings.remove(edit.DEFAULT_REFLECTION_TARGET_KEY)
        self.settings.sync()

    def tearDown(self) -> None:
        if self._old_default is None:
            self.settings.remove(edit.DEFAULT_REFLECTION_TARGET_KEY)
        else:
            self.settings.setValue(edit.DEFAULT_REFLECTION_TARGET_KEY, self._old_default)
        self.settings.sync()
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home
        self._temp.cleanup()
        stable_settings = Path(tempfile.gettempdir()) / "tks_to_kintone_tests_qsettings"
        stable_settings.mkdir(parents=True, exist_ok=True)
        QSettings.setPath(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            str(stable_settings),
        )
        QSettings.setPath(
            QSettings.Format.NativeFormat,
            QSettings.Scope.UserScope,
            str(stable_settings),
        )

    def _window(self, name: str = "default-target") -> edit.VoucherEditWindow:
        window = edit.VoucherEditWindow(order_no=name, background_pdf_bytes=b"")

        def cleanup() -> None:
            window._dirty = False
            window._closing = True
            window.close()
            window.deleteLater()
            self.app.processEvents()

        self.addCleanup(cleanup)
        return window

    def test_unsaved_default_is_standard_without_explicit_write(self) -> None:
        window = self._window()
        self.assertEqual(window._default_reflection_target_key, "standard")
        self.assertEqual(window._creation_template_key, "standard")
        self.assertIsNone(self.settings.value(edit.DEFAULT_REFLECTION_TARGET_KEY))

    def test_stable_key_is_saved_and_restored_for_new_objects(self) -> None:
        window = self._window("save")
        self.assertTrue(window.set_default_reflection_target("all_vouchers"))
        self.assertEqual(
            self.settings.value(edit.DEFAULT_REFLECTION_TARGET_KEY), "all_vouchers"
        )
        restored = self._window("restore")
        self.assertEqual(restored._default_reflection_target_key, "all_vouchers")
        self.assertEqual(restored._current_template_name, "全伝票")
        text = restored.add_text_at(QPointF(10, 10), text="new")
        line = restored.add_line(QPointF(0, 0), QPointF(10, 10))
        self.assertEqual(text.target_vouchers, ["01", "02", "03", "04", "05", "06", "07", "08"])
        self.assertEqual(line.target_vouchers, text.target_vouchers)

    def test_default_indicator_and_context_menu_are_distinct_from_selection(self) -> None:
        window = self._window()
        window.set_default_reflection_target("all_vouchers")
        window._on_template_selected(window._template_by_key("instruction_only"))
        self.assertEqual(window._current_template_name, "指図書のみ")
        self.assertEqual(window._template_default_labels["all_vouchers"].text(), "既定")
        self.assertEqual(window._template_default_labels["instruction_only"].text(), "")

        menu = QMenu(window)
        action = window._add_default_reflection_action(menu, "all_vouchers")
        self.assertEqual(action.text(), "既定に設定")
        self.assertTrue(action.isCheckable())
        self.assertTrue(action.isChecked())
        self.assertFalse(action.isEnabled())
        self.assertIn("初期反映先", action.toolTip())

    def test_object_selection_does_not_replace_next_creation_target(self) -> None:
        window = self._window()
        window._on_template_selected(window._template_by_key("instruction_only"))
        old = window.add_text_rect(
            QRectF(10, 10, 60, 18), text="old", auto_edit=False,
            target_vouchers=["05"]
        )
        before_json = json.dumps(window.serialize_objects(), sort_keys=True)
        before_history = list(window._history)
        window._select_only(old)
        self.app.processEvents()
        self.assertEqual(window._current_template_name, "梱包のみ")
        self.assertTrue(window.set_default_reflection_target("all_vouchers"))
        self.assertEqual(old.target_vouchers, ["05"])
        self.assertEqual(json.dumps(window.serialize_objects(), sort_keys=True), before_json)
        self.assertEqual(window._history, before_history)
        window._scene.clearSelection()
        self.app.processEvents()
        self.assertEqual(window._current_template_name, "指図書のみ")
        created = window.add_text_at(QPointF(20, 20), text="new")
        self.assertEqual(created.target_vouchers, ["03", "04"])

    def test_reordering_keeps_default_by_stable_key(self) -> None:
        window = self._window()
        window.set_default_reflection_target("instruction_only")
        window._on_reflection_order_changed(list(reversed(window._template_order_keys())))
        self.assertEqual(window._default_reflection_target_key, "instruction_only")
        self.assertEqual(window._template_default_labels["instruction_only"].text(), "既定")
        window.reset_reflection_target_order()
        self.assertEqual(window._default_reflection_target_key, "instruction_only")

    def test_unknown_and_deleted_user_key_fall_back_to_standard(self) -> None:
        self.settings.setValue(edit.DEFAULT_REFLECTION_TARGET_KEY, "unknown")
        self.settings.sync()
        unknown = self._window("unknown")
        self.assertEqual(unknown._default_reflection_target_key, "standard")
        self.assertEqual(self.settings.value(edit.DEFAULT_REFLECTION_TARGET_KEY), "standard")

        save_user_templates([{
            "key": "user-12345678",
            "name": "ユーザー",
            "target_vouchers": ["07"],
            "color": "#607d8b",
            "badge": "ユ",
        }])
        user_window = self._window("user")
        self.assertTrue(user_window.set_default_reflection_target("user-12345678"))
        renamed = load_user_templates()
        renamed[0]["name"] = "名称変更後"
        save_user_templates(renamed)
        renamed_window = self._window("renamed")
        self.assertEqual(renamed_window._default_reflection_target_key, "user-12345678")
        save_user_templates([])
        renamed_window._reload_templates_panel()
        self.assertEqual(renamed_window._default_reflection_target_key, "standard")
        self.assertEqual(self.settings.value(edit.DEFAULT_REFLECTION_TARGET_KEY), "standard")

    def test_favorite_explicit_target_wins_and_legacy_uses_current_target(self) -> None:
        window = self._window()
        explicit = window.add_text_rect(
            QRectF(1, 1, 60, 18), text="explicit", auto_edit=False,
            target_vouchers=["05"]
        )
        self.assertTrue(window.add_object_to_favorites(explicit))
        explicit_id = window._favorites[-1]["id"]
        window._scene.removeItem(explicit)
        window._on_template_selected(window._template_by_key("instruction_only"))
        self.assertTrue(window.drop_favorite_object(explicit_id, QPointF(20, 20)))
        explicit_created = next(
            item for item in window.edit_items() if getattr(item, "toPlainText", lambda: "")() == "explicit"
        )
        self.assertEqual(explicit_created.target_vouchers, ["05"])

        legacy = {
            "id": "legacy-id",
            "name": "legacy",
            "object": {"type": "text", "text": "legacy", "x": 0, "y": 0},
        }
        window._favorites.append(legacy)
        self.assertTrue(window.drop_favorite_object("legacy-id", QPointF(30, 30)))
        legacy_created = next(
            item for item in window.edit_items() if getattr(item, "toPlainText", lambda: "")() == "legacy"
        )
        self.assertEqual(legacy_created.target_vouchers, ["03", "04"])


if __name__ == "__main__":
    unittest.main()
