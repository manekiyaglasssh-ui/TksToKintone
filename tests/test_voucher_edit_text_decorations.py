from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QToolButton


class TestVoucherEditTextDecorations(unittest.TestCase):
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

    def window(self, order_no: str = "DECORATION"):
        from app.voucher_edit_window import VoucherEditWindow
        win = VoucherEditWindow(order_no=order_no, background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def test_toolbar_menu_actions_shortcuts_and_order(self) -> None:
        win = self.window("UI")
        self.assertIsInstance(win._text_decoration_button, QToolButton)
        self.assertEqual(win._text_decoration_button.toolTip(), "文字装飾")
        self.assertIsInstance(win._text_decoration_menu, QMenu)
        self.assertFalse(hasattr(win, "_font_bold_button"))
        expected = {
            "bold": "Ctrl+B",
            "italic": "Ctrl+I",
            "underline": "Ctrl+U",
            "strikeout": "Ctrl+5",
        }
        for key, shortcut in expected.items():
            action = win._text_decoration_actions[key]
            self.assertIsInstance(action, QAction)
            self.assertTrue(action.isCheckable())
            self.assertEqual(action.shortcut().toString(), QKeySequence(shortcut).toString())
            self.assertEqual(
                action.shortcutContext(),
                Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcuts = [action.shortcut().toString() for action in win.actions()
                     if not action.shortcut().isEmpty()]
        for shortcut in expected.values():
            normalized = QKeySequence(shortcut).toString()
            self.assertEqual(shortcuts.count(normalized), 1)

        widgets = [win._main_toolbar.widgetForAction(action)
                   for action in win._main_toolbar.actions()]
        self.assertLess(widgets.index(win._favorite_font_button),
                        widgets.index(win._font_family_combo))
        self.assertLess(widgets.index(win._font_family_combo),
                        widgets.index(win._font_size_spin))
        self.assertLess(widgets.index(win._font_size_spin),
                        widgets.index(win._text_decoration_button))

    def test_four_decorations_apply_together_and_qfont_updates(self) -> None:
        win = self.window("APPLY")
        item = win.add_text_at(QPointF(10, 10), text="装飾")
        win.commit_history()
        item.setSelected(True)
        for key in ("bold", "italic", "underline", "strikeout"):
            win._text_decoration_actions[key].trigger()
        self.assertTrue(item.font_bold)
        self.assertTrue(item.font_italic)
        self.assertTrue(item.font_underline)
        self.assertTrue(item.font_strikeout)
        font = item.font()
        self.assertTrue(font.bold())
        self.assertTrue(font.italic())
        self.assertTrue(font.underline())
        self.assertTrue(font.strikeOut())
        serialized = item.serialize_edit_object()
        self.assertTrue(serialized["font_bold"])
        self.assertTrue(serialized["bold"])
        self.assertTrue(serialized["font_italic"])
        self.assertTrue(serialized["font_underline"])
        self.assertTrue(serialized["font_strikeout"])

    def test_keyboard_shortcuts_toggle_selected_text(self) -> None:
        win = self.window("SHORTCUT")
        item = win.add_text_at(QPointF(10, 10), text="キー")
        win.commit_history()
        item.setSelected(True)
        win.show()
        win.activateWindow()
        win.setFocus()
        QApplication.processEvents()
        for key, attr in (
            (Qt.Key.Key_B, "font_bold"),
            (Qt.Key.Key_I, "font_italic"),
            (Qt.Key.Key_U, "font_underline"),
            (Qt.Key.Key_5, "font_strikeout"),
        ):
            QTest.keyClick(win, key, Qt.KeyboardModifier.ControlModifier)
            QApplication.processEvents()
            self.assertTrue(getattr(item, attr))

    def test_mixed_selection_uses_dash_then_all_on_and_one_history_entry(self) -> None:
        win = self.window("MIXED")
        first = win.add_text_at(QPointF(10, 10), text="A")
        second = win.add_text_at(QPointF(30, 30), text="B")
        first.apply_text_style(underline=True)
        win.commit_history()
        first.setSelected(True)
        second.setSelected(True)
        win._on_selection_changed()
        action = win._text_decoration_actions["underline"]
        self.assertTrue(action.text().startswith("－ "))
        self.assertFalse(action.isChecked())
        history_before = len(win._history)
        action.trigger()
        self.assertTrue(first.font_underline)
        self.assertTrue(second.font_underline)
        self.assertEqual(len(win._history), history_before + 1)
        self.assertEqual(action.text(), "下線")
        self.assertTrue(action.isChecked())
        action.trigger()
        self.assertFalse(first.font_underline)
        self.assertFalse(second.font_underline)

    def test_unselected_and_non_text_selection_change_next_text_defaults(self) -> None:
        win = self.window("DEFAULT")
        line = win.add_line(QPointF(1, 1), QPointF(20, 20))
        line.setSelected(True)
        for key in ("bold", "italic", "underline", "strikeout"):
            win._text_decoration_actions[key].trigger()
        item = win.add_text_at(QPointF(30, 30), text="次回")
        self.assertTrue(item.font_bold)
        self.assertTrue(item.font_italic)
        self.assertTrue(item.font_underline)
        self.assertTrue(item.font_strikeout)

    def test_undo_redo_restores_each_decoration_and_action_state(self) -> None:
        win = self.window("UNDO")
        item = win.add_text_at(QPointF(10, 10), text="戻す")
        win.commit_history()
        item.setSelected(True)
        action = win._text_decoration_actions["strikeout"]
        action.trigger()
        self.assertTrue(item.font_strikeout)
        win.undo()
        restored = next(it for it in win.edit_items()
                        if getattr(it, "obj_id", "") == item.obj_id)
        self.assertFalse(restored.font_strikeout)
        self.assertFalse(action.isChecked())
        win.redo()
        redone = next(it for it in win.edit_items()
                      if getattr(it, "obj_id", "") == item.obj_id)
        self.assertTrue(redone.font_strikeout)
        self.assertTrue(action.isChecked())

    def test_context_menu_uses_shared_actions_only_for_text(self) -> None:
        win = self.window("CONTEXT")
        text = win.add_text_at(QPointF(10, 10), text="右クリック")
        text.setSelected(True)
        menu = win._build_object_context_menu(text)
        submenu_action = next(action for action in menu.actions()
                              if action.menu() and action.menu().objectName()
                              == "text_decoration_context_menu")
        submenu = submenu_action.menu()
        self.assertEqual(submenu.title(), "文字装飾")
        self.assertEqual(submenu.actions(), list(win._text_decoration_actions.values()))
        self.assertEqual(
            [action.shortcut().toString() for action in submenu.actions()],
            ["Ctrl+B", "Ctrl+I", "Ctrl+U", "Ctrl+5"],
        )
        favorite = next(action for action in menu.actions()
                        if action.objectName() == "favorite_add_action")
        self.assertEqual(favorite.text(), "オブジェクトをお気に入り登録")

        line = win.add_line(QPointF(1, 1), QPointF(5, 5))
        non_text_menu = win._build_object_context_menu(line)
        self.assertFalse(any(action.menu() and action.menu().objectName()
                             == "text_decoration_context_menu"
                             for action in non_text_menu.actions()))

    def test_save_reload_clone_and_old_data_defaults(self) -> None:
        from app.voucher_edit_objects import clone_edit_objects, _normalize_objects

        win = self.window("SAVE")
        item = win.add_text_at(QPointF(10, 10), text="保存")
        item.apply_text_style(bold=True, italic=True, underline=True, strikeout=True)
        original = item.serialize_edit_object()
        clone = clone_edit_objects([original])[0]
        for key in ("font_bold", "font_italic", "font_underline", "font_strikeout"):
            self.assertTrue(clone[key])
        clone["font_underline"] = False
        self.assertTrue(original["font_underline"])

        old = _normalize_objects([{
            "id": "old", "type": "text", "text": "旧", "x": 1, "y": 2,
            "width": 20, "height": 10,
        }])[0]
        self.assertFalse(old["font_bold"])
        self.assertFalse(old["font_italic"])
        self.assertFalse(old["font_underline"])
        self.assertFalse(old["font_strikeout"])

        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        reloaded = self.window("SAVE").serialize_objects()[0]
        self.assertTrue(reloaded["font_italic"])
        self.assertTrue(reloaded["font_underline"])
        self.assertTrue(reloaded["font_strikeout"])

    def test_pdf_bold_italic_resolution_and_decoration_lines(self) -> None:
        from app import voucher_service

        calls: list[tuple[float, float, float, float]] = []

        class Canvas:
            def setFont(self, *args): pass
            def setFillColorRGB(self, *args): pass
            def setStrokeColorRGB(self, *args): pass
            def setLineWidth(self, *args): pass
            def drawString(self, *args): pass
            def drawRightString(self, *args): pass
            def drawCentredString(self, *args): pass
            def line(self, *args): calls.append(args)

        voucher_service.draw_text_in_scene_rect(
            Canvas(), "Decorated", 10, 20, 100, 30, "Helvetica", 12,
            text_align="left", vertical_align="top",
            underline=True, strikeout=True,
        )
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[0][1], calls[1][1])

        with mock.patch.object(voucher_service, "_windows_font_file",
                               return_value=None) as windows, \
             mock.patch.object(voucher_service, "_fontconfig_font_file",
                               return_value=None) as fontconfig:
            voucher_service._EDIT_FONT_CACHE.clear()
            voucher_service._resolve_edit_pdf_font("Missing", True, True)
        windows.assert_any_call("Missing", True, True)
        fontconfig.assert_any_call("Missing", True, True)

        class ObjectCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *args): pass
            def setFillColorRGB(self, *args): pass

        with mock.patch.object(voucher_service, "_resolve_edit_pdf_font",
                               return_value="BoldItalicFace") as resolve, \
             mock.patch.object(voucher_service, "draw_text_in_scene_rect") as draw:
            voucher_service._draw_edit_objects(ObjectCanvas(), [{
                "id": "decorated", "type": "text", "text": "PDF",
                "x": 1, "y": 2, "width": 100, "height": 30,
                "font_family": "Example", "font_size": 14,
                "font_bold": True, "font_italic": True,
                "font_underline": True, "font_strikeout": True,
                "text_color": "#123456",
            }])
        resolve.assert_called_once_with("Example", True, True)
        self.assertTrue(draw.call_args.kwargs["underline"])
        self.assertTrue(draw.call_args.kwargs["strikeout"])


if __name__ == "__main__":
    unittest.main()
