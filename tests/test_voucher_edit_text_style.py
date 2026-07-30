from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QComboBox, QToolButton


class TestVoucherEditTextStyle(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home
        self._tmp.cleanup()

    def window(self, order_no: str = "STYLE"):
        from app.voucher_edit_window import VoucherEditWindow
        win = VoucherEditWindow(order_no=order_no, background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def test_toolbar_has_family_size_and_bold_in_order(self) -> None:
        win = self.window()
        self.assertIsInstance(win._font_family_combo, QComboBox)
        self.assertEqual(win._font_size_spin.objectName(), "textFontSizeCombo")
        self.assertFalse(hasattr(win, "_font_bold_button"))
        self.assertIsInstance(win._text_decoration_button, QToolButton)
        self.assertEqual(win._text_decoration_button.toolTip(), "文字装飾")
        self.assertTrue(win._text_decoration_actions["bold"].isCheckable())
        widgets = [win._main_toolbar.widgetForAction(action)
                   for action in win._main_toolbar.actions()]
        self.assertLess(widgets.index(win._font_family_combo),
                        widgets.index(win._font_size_spin))
        self.assertLess(widgets.index(win._font_size_spin),
                        widgets.index(win._text_decoration_button))

    def test_unselected_style_becomes_new_text_default(self) -> None:
        win = self.window("DEFAULT")
        family = QFontDatabase.families()[0] if QFontDatabase.families() else ""
        if family:
            win._font_family_combo.setCurrentFont(QFont(family))
        win._font_size_spin.setValue(18.5)
        win._text_decoration_actions["bold"].trigger()
        item = win.add_text_at(QPointF(10, 10), text="新規")
        self.assertEqual(item.font_family, win.current_font_family)
        self.assertEqual(item.font_size, 18.5)
        self.assertTrue(item.font_bold)

    def test_selected_style_sync_apply_and_undo_redo(self) -> None:
        win = self.window("UNDO")
        item = win.add_text_at(QPointF(10, 10), text="対象", font_size=12)
        win.commit_history()
        item.setSelected(True)
        self.assertEqual(win._font_size_spin.value(), 12)
        win._font_size_spin.setValue(24)
        self.assertEqual(item.font_size, 24)
        win._text_decoration_actions["bold"].trigger()
        self.assertTrue(item.font_bold)
        win.undo()
        restored = next(it for it in win.edit_items()
                        if getattr(it, "obj_id", "") == item.obj_id)
        self.assertFalse(restored.font_bold)
        self.assertEqual(restored.font_size, 24)
        win.redo()
        redone = next(it for it in win.edit_items()
                      if getattr(it, "obj_id", "") == item.obj_id)
        self.assertTrue(redone.font_bold)

    def test_non_text_selection_changes_next_text_default_only(self) -> None:
        from PySide6.QtCore import QPointF as Point
        win = self.window("NON_TEXT")
        line = win.add_line(Point(1, 1), Point(20, 20), font_size=11)
        line.setSelected(True)
        win._font_size_spin.setValue(30)
        win._text_decoration_actions["bold"].trigger()
        self.assertEqual(line.font_size, 11)
        text = win.add_text_at(Point(30, 30), text="次")
        self.assertEqual(text.font_size, 30)
        self.assertTrue(text.font_bold)

    def test_save_reload_and_copy_keep_independent_style(self) -> None:
        win = self.window("SAVE")
        item = win.add_text_at(QPointF(10, 10), text="保存", font_size=16)
        # Linux CIにはYu Gothic UIが無いため、WindowsでQtが選択した保存値を再現する。
        item.font_family = "Yu Gothic UI"
        item.apply_text_style(bold=True, italic=True, underline=True)
        original = win.serialize_objects()[0]
        self.assertEqual(original["font_family"], "Yu Gothic UI")
        self.assertTrue(original["bold"])
        self.assertTrue(original["font_bold"])
        self.assertTrue(original["font_italic"])
        self.assertTrue(original["font_underline"])
        from app.voucher_edit_objects import clone_edit_objects
        clone = clone_edit_objects([original])[0]
        clone["font_size"] = 48
        self.assertEqual(original["font_size"], 16)
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        with mock.patch(
                "app.voucher_edit_window.QFontDatabase.families",
                return_value=["Yu Gothic UI"]):
            reloaded = self.window("SAVE").serialize_objects()[0]
        self.assertEqual(reloaded["font_family"], original["font_family"])
        self.assertEqual(reloaded["font_size"], 16)
        self.assertTrue(reloaded["font_bold"])
        self.assertTrue(reloaded["bold"])
        self.assertTrue(reloaded["font_italic"])
        self.assertTrue(reloaded["italic"])
        self.assertTrue(reloaded["font_underline"])
        self.assertTrue(reloaded["underline"])

    def test_pdf_uses_resolved_family_size_and_bold(self) -> None:
        from app import voucher_service
        calls: list[tuple[str, float]] = []

        class Canvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *args): pass
            def setFillColorRGB(self, *args): pass
            def setFont(self, name, size): calls.append((name, size))
            def drawString(self, *args): pass
            def drawRightString(self, *args): pass
            def drawCentredString(self, *args): pass

        with mock.patch.object(voucher_service, "_resolve_edit_pdf_font",
                               return_value="SelectedPdfFont") as resolve:
            voucher_service._draw_edit_objects(Canvas(), [{
                "id": "t", "type": "text", "x": 1, "y": 2,
                "width": 100, "height": 30, "text": "PDF",
                "font_family": "Yu Gothic UI", "font_size": 17.5,
                "font_bold": True,
            }])
        resolve.assert_called_with("Yu Gothic UI", True, False)
        self.assertIn(("SelectedPdfFont", 17.5), calls)

    def test_missing_japanese_glyphs_are_disclosed_by_tooltip(self) -> None:
        from app import voucher_edit_window as view

        with mock.patch.object(view, "text_font_missing_glyphs",
                               return_value=list("テキスト")):
            item = view._EditTextItem(
                "テキスト", font_family="Latin Only", font_size=12)
        self.assertIn("日本語文字に対応していない", item.toolTip())
        self.assertIn("代替フォント", item.toolTip())


if __name__ == "__main__":
    unittest.main()
