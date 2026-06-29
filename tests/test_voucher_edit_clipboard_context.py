from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication, QMenu

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


def _png_bytes(width: int = 8, height: int = 6, color: int = 0xFFAA7733) -> bytes:
    from app.voucher_edit_window import qimage_to_png_bytes

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    return qimage_to_png_bytes(image)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditClipboardAndContext(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._prev_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._prev_home
        self._tmp.cleanup()

    def _make_window(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ctx", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    @staticmethod
    def _submenu(menu: QMenu, title: str) -> QMenu | None:
        for action in menu.actions():
            sub = action.menu()
            if sub is not None and sub.title() == title:
                return sub
        return None

    @staticmethod
    def _action_texts(menu: QMenu) -> list[str]:
        return [action.text() for action in menu.actions()]

    def test_ctrl_c_copies_selected_object_to_internal_clipboard(self) -> None:
        win = self._make_window()
        rect = win.add_rect(QRectF(10.0, 20.0, 40.0, 30.0),
                            text="r", target_vouchers=["05"])
        rect.setSelected(True)
        self.assertTrue(win.copy_selected_objects())
        self.assertEqual(len(win._object_clipboard), 1)
        self.assertEqual(win._object_clipboard[0]["type"], "rectangle")
        self.assertEqual(win._object_clipboard[0]["target_vouchers"], ["05"])

    def test_ctrl_v_duplicates_with_offset_target_and_selection(self) -> None:
        from app.voucher_edit_window import PASTE_OFFSET_X, PASTE_OFFSET_Y

        win = self._make_window()
        rect = win.add_rect(QRectF(10.0, 20.0, 40.0, 30.0),
                            text="r", target_vouchers=["05"])
        rect.setSelected(True)
        win.copy_selected_objects()
        with mock.patch.object(win, "commit_history",
                               wraps=win.commit_history) as commit:
            self.assertTrue(win.paste_copied_objects())
        objs = sorted(win.serialize_objects(), key=lambda o: o["x"])
        self.assertEqual(len(objs), 2)
        self.assertAlmostEqual(objs[1]["x"], objs[0]["x"] + PASTE_OFFSET_X)
        self.assertAlmostEqual(objs[1]["y"], objs[0]["y"] + PASTE_OFFSET_Y)
        self.assertEqual(objs[1]["target_vouchers"], ["05"])
        selected = [it for it in win._scene.selectedItems()
                    if hasattr(it, "serialize_edit_object")]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].obj_id, objs[1]["id"])
        self.assertTrue(commit.called)
        self.assertTrue(win.is_dirty())

    def test_image_copy_paste_duplicates_image_bytes(self) -> None:
        win = self._make_window()
        src_bytes = _png_bytes()
        image = win.add_image(src_bytes, rect=QRectF(10.0, 20.0, 40.0, 30.0),
                              target_vouchers=["03"])
        image.setSelected(True)
        win.copy_selected_objects()
        win.paste_copied_objects()
        images = [it for it in win.edit_items()
                  if it.serialize_edit_object()["type"] == "image"]
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0].image_bytes, images[1].image_bytes)
        self.assertEqual(images[1].target_vouchers, ["03"])

    def test_left_template_change_does_not_change_selected_object(self) -> None:
        win = self._make_window()
        rect = win.add_rect(QRectF(10.0, 20.0, 40.0, 30.0),
                            text="r", target_vouchers=["05"])
        rect.setSelected(True)
        win._on_template_selected(win._template_by_name("全伝票"))
        self.assertEqual(rect.target_vouchers, ["05"])
        text = win.add_text_rect(QRectF(80.0, 20.0, 80.0, 24.0),
                                 text="t", auto_edit=False)
        self.assertEqual(text.target_vouchers, ["01", "02", "03", "04", "05", "06", "07", "08"])

    def test_context_target_change_updates_object_only(self) -> None:
        win = self._make_window()
        rect = win.add_rect(QRectF(10.0, 20.0, 40.0, 30.0),
                            text="r", target_vouchers=["03"])
        rect.setSelected(True)
        with mock.patch.object(win, "commit_history",
                               wraps=win.commit_history) as commit:
            win._set_object_target_vouchers(rect, ["05"])
        self.assertEqual(rect.target_vouchers, ["05"])
        self.assertTrue(rect.isSelected())
        self.assertTrue(commit.called)

    def test_context_menu_visibility_by_object_type(self) -> None:
        win = self._make_window()
        text = win.add_text_rect(QRectF(10.0, 20.0, 80.0, 24.0),
                                 text="t", auto_edit=False)
        line = win.add_line(QPointF(10.0, 80.0), QPointF(80.0, 80.0))
        rect = win.add_rect(QRectF(10.0, 120.0, 40.0, 30.0), text="r")
        ellipse = win.add_ellipse(QRectF(70.0, 120.0, 40.0, 30.0), text="e")
        image = win.add_image(_png_bytes(), rect=QRectF(10.0, 100.0, 40.0, 30.0))

        text_menu = win._build_object_context_menu(text)
        self.assertIsNone(self._submenu(text_menu, "線幅"))
        self.assertIsNotNone(self._submenu(text_menu, "文字サイズ"))
        self.assertIn("削除", self._action_texts(text_menu))
        self.assertIsNotNone(self._submenu(text_menu, "反映先"))

        line_menu = win._build_object_context_menu(line)
        self.assertIsNotNone(self._submenu(line_menu, "線幅"))
        self.assertIsNotNone(self._submenu(line_menu, "文字サイズ"))

        rect_menu = win._build_object_context_menu(rect)
        self.assertIsNotNone(self._submenu(rect_menu, "線幅"))
        self.assertIsNotNone(self._submenu(rect_menu, "文字サイズ"))

        ellipse_menu = win._build_object_context_menu(ellipse)
        self.assertIsNotNone(self._submenu(ellipse_menu, "線幅"))
        self.assertIsNotNone(self._submenu(ellipse_menu, "文字サイズ"))

        image_menu = win._build_object_context_menu(image)
        self.assertIsNone(self._submenu(image_menu, "線幅"))
        self.assertIsNone(self._submenu(image_menu, "文字サイズ"))
        self.assertIn("背景を透過", self._action_texts(image_menu))
        self.assertIn("背景を戻す", self._action_texts(image_menu))

    def test_image_context_menu_shows_background_actions_when_debug_off(self) -> None:
        win = self._make_window()
        win.set_debug_visible(False)
        image = win.add_image(_png_bytes(), rect=QRectF(10.0, 100.0, 40.0, 30.0))
        menu = win._build_object_context_menu(image)
        self.assertIn("背景を透過", self._action_texts(menu))
        self.assertIn("背景を戻す", self._action_texts(menu))

    def test_image_context_transparency_uses_threshold_not_rembg(self) -> None:
        win = self._make_window()
        with mock.patch.object(win, "_start_rembg_warmup_if_needed") as warmup:
            image = win.add_image(
                _png_bytes(), rect=QRectF(10.0, 100.0, 40.0, 30.0)
            )
            menu = win._build_object_context_menu(image)
            action = next(a for a in menu.actions() if a.text() == "背景を透過")
            warmup.reset_mock()
            with mock.patch.object(win, "_on_threshold_transparent") as threshold, \
                    mock.patch.object(win, "_on_transparent_background") as rembg:
                action.trigger()

            threshold.assert_called_once_with()
            rembg.assert_not_called()
            warmup.assert_not_called()
            self.assertTrue(image.isSelected())

    def test_context_font_size_change_applies_to_shape_and_line(self) -> None:
        win = self._make_window()
        rect = win.add_rect(QRectF(10.0, 20.0, 40.0, 30.0), text="r",
                            font_size=12.0)
        line = win.add_line(QPointF(10.0, 80.0), QPointF(80.0, 80.0),
                            font_size=12.0)
        for item in (rect, line):
            with self.subTest(kind=item.serialize_edit_object()["type"]):
                win._select_only(item)
                with mock.patch.object(win, "commit_history",
                                       wraps=win.commit_history) as commit:
                    win._set_object_font_size(item, 24)
                self.assertEqual(item.font_size, 24.0)
                self.assertTrue(item.isSelected())
                self.assertTrue(commit.called)
                self.assertTrue(win.is_dirty())

    def test_context_menu_current_target_is_checked(self) -> None:
        win = self._make_window()
        rect = win.add_rect(QRectF(10.0, 20.0, 40.0, 30.0),
                            text="r", target_vouchers=["05"])
        menu = win._build_object_context_menu(rect)
        target_menu = self._submenu(menu, "反映先")
        self.assertIsNotNone(target_menu)
        checked = [a.text() for a in target_menu.actions() if a.isChecked()]
        self.assertEqual(checked, ["梱包のみ"])

    def test_right_click_selects_object_before_menu(self) -> None:
        win = self._make_window()
        rect = win.add_rect(QRectF(10.0, 20.0, 40.0, 30.0), text="r")
        class Event:
            def __init__(self):
                self.accepted = False

            def scenePos(self):
                return QPointF(20.0, 30.0)

            def screenPos(self):
                return QPointF(20.0, 30.0)

            def accept(self):
                self.accepted = True

        event = Event()
        with mock.patch.object(win, "_show_object_context_menu") as show:
            win._scene.contextMenuEvent(event)
            show.assert_called_once()
        self.assertTrue(rect.isSelected())
        self.assertTrue(event.accepted)

    def test_unselected_ctrl_v_does_not_paste_internal_object(self) -> None:
        win = self._make_window()
        rect = win.add_rect(QRectF(10.0, 20.0, 40.0, 30.0), text="r")
        rect.setSelected(True)
        win.copy_selected_objects()
        win._scene.clearSelection()
        QApplication.clipboard().clear()
        self.assertFalse(win.handle_paste_shortcut())
        self.assertEqual(len(win.serialize_objects()), 1)

    def test_unselected_ctrl_v_flows_to_clipboard_image_paste(self) -> None:
        win = self._make_window()
        rect = win.add_rect(QRectF(10.0, 20.0, 40.0, 30.0), text="r")
        rect.setSelected(True)
        win.copy_selected_objects()
        win._scene.clearSelection()
        image = QImage(10, 10, QImage.Format.Format_ARGB32)
        image.fill(0xFF112233)
        QApplication.clipboard().setImage(image)
        with mock.patch.object(win, "paste_image_from_clipboard",
                               wraps=win.paste_image_from_clipboard) as paste:
            self.assertTrue(win.handle_paste_shortcut())
        self.assertTrue(paste.called)
        objs = win.serialize_objects()
        self.assertEqual(len([o for o in objs if o["type"] == "rectangle"]), 1)
        self.assertEqual(len([o for o in objs if o["type"] == "image"]), 1)


if __name__ == "__main__":
    unittest.main()
