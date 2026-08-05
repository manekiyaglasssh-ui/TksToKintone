from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QSettings, Qt
from PySide6.QtGui import QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QToolButton

from app import voucher_edit_window as edit


class TestVoucherEditFontFavorites(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            self._tmp.name,
        )
        QSettings(edit.SETTINGS_ORG, edit.SETTINGS_APP).clear()

    def tearDown(self) -> None:
        QSettings(edit.SETTINGS_ORG, edit.SETTINGS_APP).clear()
        self._tmp.cleanup()

    def window(self, order_no: str = "FONT-FAVORITES") -> edit.VoucherEditWindow:
        win = edit.VoucherEditWindow(order_no=order_no, background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    @staticmethod
    def available_families() -> list[str]:
        return [str(name) for name in QFontDatabase.families() if str(name).strip()]

    @staticmethod
    def section_families(combo: QComboBox, section: str) -> list[str]:
        return [
            str(combo.itemData(index))
            for index in range(combo.count())
            if combo.itemData(index, edit.FONT_SECTION_ROLE) == section
        ]

    def test_qsettings_round_trip_deduplicates_and_keeps_registration_order(self) -> None:
        edit.save_favorite_fonts(["ＭＳ ゴシック", "Arial", "ＭＳ ゴシック", ""])
        loaded = edit.load_favorite_fonts(
            available_fonts={"ＭＳ ゴシック", "Arial"})
        self.assertEqual(loaded, ["ＭＳ ゴシック", "Arial"])
        raw = QSettings(edit.SETTINGS_ORG, edit.SETTINGS_APP).value(
            edit.SETTINGS_FAVORITE_FONTS)
        self.assertIsNotNone(raw)

    def test_corrupt_and_missing_fonts_are_removed_safely(self) -> None:
        settings = QSettings(edit.SETTINGS_ORG, edit.SETTINGS_APP)
        settings.setValue(edit.SETTINGS_FAVORITE_FONTS, 12345)
        self.assertEqual(edit.load_favorite_fonts(available_fonts={"Arial"}), [])

        settings.setValue(edit.SETTINGS_FAVORITE_FONTS, ["Arial", "削除済みフォント"])
        self.assertEqual(edit.load_favorite_fonts(available_fonts={"Arial"}), ["Arial"])
        self.assertEqual(
            edit.load_favorite_fonts(available_fonts={"Arial"}), ["Arial"])

    def test_toolbar_button_add_remove_persistence_and_no_undo_entry(self) -> None:
        families = self.available_families()
        self.assertTrue(families)
        family = families[0]
        win = self.window("ADD")
        self.assertIsInstance(win._favorite_font_button, QToolButton)
        self.assertIsInstance(win._font_family_combo, QComboBox)
        self.assertFalse(hasattr(win, "_favorite_font_combo"))
        self.assertIs(win._edit_header_widget.widgetForAction(
            next(action for action in win._edit_header_widget.actions()
                 if win._edit_header_widget.widgetForAction(action) is win._font_family_combo)),
            win._font_family_combo)
        self.assertIsNone(win.findChild(QComboBox, "favoriteFontCombo"))
        win._font_family_combo.setCurrentFont(QFont(family))
        self.assertEqual(win._favorite_font_button.text(), "☆")
        history_before = len(win._history)

        win._favorite_font_button.click()
        self.assertEqual(win._favorite_font_button.text(), "★")
        self.assertEqual(len(win._history), history_before)
        self.assertEqual(win._favorite_fonts, [family])
        self.assertEqual(self.section_families(
            win._font_family_combo, edit.FONT_SECTION_FAVORITE), [family])
        self.assertIn(family, self.section_families(
            win._font_family_combo, edit.FONT_SECTION_ALL))
        self.assertEqual(win._font_family_combo.currentFont().family(), family)

        restored = self.window("RESTORED")
        restored._font_family_combo.setCurrentFont(QFont(family))
        self.assertIn(family, restored._favorite_fonts)
        self.assertEqual(restored._favorite_font_button.text(), "★")

        restored_history = len(restored._history)
        restored._favorite_font_button.click()
        self.assertNotIn(family, restored._favorite_fonts)
        self.assertEqual(self.section_families(
            restored._font_family_combo, edit.FONT_SECTION_FAVORITE), [])
        self.assertIn(family, self.section_families(
            restored._font_family_combo, edit.FONT_SECTION_ALL))
        self.assertEqual(restored._favorite_font_button.text(), "☆")
        self.assertEqual(len(restored._history), restored_history)

    def test_favorite_selection_uses_normal_font_change_and_undo(self) -> None:
        families = self.available_families()
        if len(families) < 2:
            self.skipTest("複数のテスト用フォントがありません")
        first, second = families[:2]
        edit.save_favorite_fonts([second])
        win = self.window("SELECT")
        item = win.add_text_at(QPointF(10, 10), text="対象")
        item.apply_text_style(family=first)
        win.commit_history()
        item.setSelected(True)
        history_before = len(win._history)

        index = win._font_family_combo.findData(second)
        self.assertGreaterEqual(index, 0)
        win._font_family_combo.setCurrentIndex(index)
        self.assertEqual(item.font_family, second)
        self.assertEqual(len(win._history), history_before + 1)
        win.undo()
        restored = next(it for it in win.edit_items()
                        if getattr(it, "obj_id", "") == item.obj_id)
        self.assertEqual(restored.font_family, first)
        self.assertEqual(win._font_family_combo.currentFont().family(), first)
        self.assertEqual(win._favorite_font_button.text(), "☆")
        win.redo()
        self.assertEqual(win._favorite_font_button.text(), "★")

    def test_favorite_selection_without_selection_updates_next_text_default(self) -> None:
        families = self.available_families()
        self.assertTrue(families)
        family = families[-1]
        edit.save_favorite_fonts([family])
        win = self.window("DEFAULT")
        self.assertFalse(win._scene.selectedItems())
        index = win._font_family_combo.findData(family)
        win._font_family_combo.setCurrentIndex(index)
        item = win.add_text_at(QPointF(10, 10), text="次回")
        self.assertEqual(win.current_font_family, family)
        self.assertEqual(item.font_family, family)
        self.assertEqual(win._favorite_font_button.text(), "★")

    def test_mixed_text_selection_disables_star_but_non_text_uses_default(self) -> None:
        families = self.available_families()
        if len(families) < 2:
            self.skipTest("複数のテスト用フォントがありません")
        win = self.window("MIXED")
        first = win.add_text_at(QPointF(10, 10), text="A")
        second = win.add_text_at(QPointF(30, 30), text="B")
        first.apply_text_style(family=families[0])
        second.apply_text_style(family=families[1])
        first.setSelected(True)
        second.setSelected(True)
        win._on_selection_changed()
        self.assertEqual(win._font_family_combo.currentIndex(), -1)
        self.assertFalse(win._favorite_font_button.isEnabled())

        # 複数選択でも表示フォントが単一なら操作できる。
        second.apply_text_style(family=families[0])
        win._on_selection_changed()
        self.assertTrue(win._favorite_font_button.isEnabled())

        first.setSelected(False)
        second.setSelected(False)
        line = win.add_line(QPointF(1, 1), QPointF(5, 5))
        line.setSelected(True)
        win._on_selection_changed()
        self.assertTrue(win._favorite_font_button.isEnabled())

    def test_toolbar_order_and_theme_styles_keep_favorite_visible(self) -> None:
        win = self.window("UI")
        widgets = [win._main_toolbar.widgetForAction(action)
                   for action in win._main_toolbar.actions()]
        self.assertLess(widgets.index(win._favorite_font_button),
                        widgets.index(win._font_family_combo))
        self.assertLess(widgets.index(win._font_family_combo),
                        widgets.index(win._font_size_spin))
        self.assertLessEqual(win._font_family_combo.maximumWidth(), 210)
        self.assertFalse(hasattr(win, "_favorite_font_combo"))
        self.assertIn("favoriteFontButton:checked", edit.EDIT_TOOLBAR_STYLE)
        self.assertIn("background: transparent", edit.EDIT_TOOLBAR_STYLE)
        self.assertIn("border: none", edit.EDIT_TOOLBAR_STYLE)
        self.assertIn("font-size: 24px", edit.EDIT_TOOLBAR_STYLE)
        self.assertIn("min-width: 48px", edit.EDIT_TOOLBAR_STYLE)
        self.assertIn("max-width: 48px", edit.EDIT_TOOLBAR_STYLE)
        combined_styles = (
            edit.EDIT_TOOLBAR_STYLE
            + edit.EDIT_TOOLBAR_LIGHT_STYLE
            + edit.EDIT_TOOLBAR_DARK_STYLE
        )
        self.assertNotIn("palette(text)", combined_styles)
        self.assertIn(
            f"color: {edit.FAVORITE_FONT_UNREGISTERED_LIGHT_COLOR}",
            edit.EDIT_TOOLBAR_LIGHT_STYLE,
        )
        self.assertIn(
            f"color: {edit.FAVORITE_FONT_UNREGISTERED_DARK_COLOR}",
            edit.EDIT_TOOLBAR_DARK_STYLE,
        )
        for theme_style in (
            edit.EDIT_TOOLBAR_LIGHT_STYLE,
            edit.EDIT_TOOLBAR_DARK_STYLE,
        ):
            self.assertIn(
                f"color: {edit.FAVORITE_FONT_ICON_COLOR}", theme_style)
            self.assertIn(
                f"color: {edit.FAVORITE_FONT_DISABLED_COLOR}", theme_style)
            self.assertIn(
                f"color: {edit.FAVORITE_FONT_REGISTERED_DISABLED_COLOR}",
                theme_style,
            )
            for state in (":hover", ":pressed", ":focus"):
                self.assertIn(
                    f'QToolButton#favoriteFontButton[favorite="false"]{state}',
                    theme_style,
                )
                self.assertIn(
                    f'QToolButton#favoriteFontButton[favorite="true"]{state}',
                    theme_style,
                )
        self.assertTrue(win._favorite_font_button.autoRaise())
        self.assertEqual(
            win._favorite_font_button.width(),
            edit.FAVORITE_FONT_BUTTON_WIDTH_PX,
        )
        self.assertEqual(edit.FAVORITE_FONT_ICON_SIZE_PX, 24)
        win._apply_toolbar_theme()
        self.assertTrue(win._favorite_font_button.toolTip())

    def test_favorite_icon_color_state_switches_immediately(self) -> None:
        win = self.window("ICON-COLOR")
        families = self.available_families()
        self.assertTrue(families)
        win._font_family_combo.setCurrentFont(QFont(families[0]))
        button = win._favorite_font_button
        self.assertEqual(button.text(), "☆")
        self.assertEqual(button.property("favorite"), "false")

        # ライト→ダーク→ライトで未登録色を即時更新する。
        for dark, expected in (
            (False, edit.FAVORITE_FONT_UNREGISTERED_LIGHT_COLOR),
            (True, edit.FAVORITE_FONT_UNREGISTERED_DARK_COLOR),
            (False, edit.FAVORITE_FONT_UNREGISTERED_LIGHT_COLOR),
        ):
            with mock.patch(
                "app.voucher_edit_window.current_title_bar_is_dark",
                return_value=dark,
            ):
                win._apply_toolbar_theme()
            self.app.processEvents()
            self.assertEqual(
                button.palette().color(QPalette.ColorRole.ButtonText).name(),
                expected.lower(),
            )

        button.click()
        self.assertEqual(button.text(), "★")
        self.assertEqual(button.property("favorite"), "true")

        # テーマ配色を再適用しても、状態別セレクターと透明背景・枠なしを維持する。
        for dark in (False, True, False):
            with mock.patch(
                "app.voucher_edit_window.current_title_bar_is_dark",
                return_value=dark,
            ):
                win._apply_toolbar_theme()
            self.app.processEvents()
            toolbar_style = win._main_toolbar.styleSheet()
            self.assertIn(
                f"color: {edit.FAVORITE_FONT_ICON_COLOR}", toolbar_style)
            self.assertIn("background: transparent", toolbar_style)
            self.assertIn("border: none", toolbar_style)
            self.assertEqual(
                button.palette().color(QPalette.ColorRole.ButtonText).name(),
                edit.FAVORITE_FONT_ICON_COLOR.lower(),
            )

        button.click()
        self.assertEqual(button.text(), "☆")
        self.assertEqual(button.property("favorite"), "false")
        self.app.processEvents()
        self.assertEqual(
            button.palette().color(QPalette.ColorRole.ButtonText).name(),
            edit.FAVORITE_FONT_UNREGISTERED_LIGHT_COLOR.lower(),
        )

    def test_favorite_disabled_colors_remain_visible(self) -> None:
        families = self.available_families()
        self.assertTrue(families)
        family = families[0]
        edit.save_favorite_fonts([family])
        win = self.window("DISABLED-COLOR")
        win._font_family_combo.setCurrentFont(QFont(family))
        button = win._favorite_font_button
        self.assertEqual(button.text(), "★")

        with mock.patch(
            "app.voucher_edit_window.current_title_bar_is_dark",
            return_value=False,
        ):
            win._apply_toolbar_theme()
        button.setEnabled(False)
        self.app.processEvents()
        self.assertEqual(
            button.palette().color(QPalette.ColorRole.ButtonText).name(),
            edit.FAVORITE_FONT_ICON_COLOR.lower(),
        )

        button.setProperty("favorite", "false")
        win._refresh_favorite_font_button_style()
        self.app.processEvents()
        self.assertEqual(
            button.palette().color(QPalette.ColorRole.ButtonText).name(),
            edit.FAVORITE_FONT_ICON_COLOR.lower(),
        )

    def test_integrated_list_has_disabled_headers_separator_and_all_fonts(self) -> None:
        families = self.available_families()
        if len(families) < 2:
            self.skipTest("複数のテスト用フォントがありません")
        edit.save_favorite_fonts([families[1], families[0]])
        win = self.window("LIST")
        combo = win._font_family_combo
        self.assertEqual(self.section_families(
            combo, edit.FONT_SECTION_FAVORITE), [families[1], families[0]])
        # 全一覧はプルダウン初回表示時に遅延構築する。
        combo._ensure_all_fonts_loaded()
        self.assertEqual(self.section_families(
            combo, edit.FONT_SECTION_ALL), families)
        favorite_header = combo.findText("★ お気に入り")
        all_header = combo.findText("すべてのフォント")
        self.assertGreaterEqual(favorite_header, 0)
        self.assertGreater(all_header, favorite_header)
        self.assertFalse(combo.model().item(favorite_header).isSelectable())
        self.assertFalse(combo.model().item(all_header).isSelectable())
        separator = favorite_header + 1 + 2
        self.assertFalse(combo.model().item(separator).isEnabled())
        self.assertEqual(combo.itemData(favorite_header + 1), families[1])
        self.assertEqual(
            combo.itemData(favorite_header + 1, Qt.ItemDataRole.FontRole).family(),
            families[1],
        )

    def test_favorites_are_not_part_of_edit_data(self) -> None:
        families = self.available_families()
        self.assertTrue(families)
        edit.save_favorite_fonts([families[0]])
        win = self.window("SERIALIZE")
        win.add_text_at(QPointF(10, 10), text="保存対象")
        serialized = win.serialize_objects()
        self.assertNotIn(edit.SETTINGS_FAVORITE_FONTS, repr(serialized))
        self.assertNotIn("favorite_fonts", repr(serialized))


if __name__ == "__main__":
    unittest.main()
