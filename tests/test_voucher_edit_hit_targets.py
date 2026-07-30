"""小さい文字・細線のview-pixel基準ヒット領域の回帰テスト。"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QFocusEvent, QTransform
    from PySide6.QtWidgets import QApplication, QGraphicsSceneMouseEvent

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditHitTargets(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._old_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._old_home
        self._tmp.cleanup()

    def _window(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="hit-target", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.show()
        QApplication.processEvents()
        return win

    def test_small_text_has_at_least_24_view_pixel_hit_target(self) -> None:
        win = self._window()
        for index, size in enumerate((4.0, 6.0, 8.0)):
            item = win.add_text_rect(
                QRectF(30.0, 30.0 + index * 40.0, 2.0, 2.0),
                text="1", font_size=size, auto_edit=False, auto_fit=False)
            hit = item.shape().boundingRect()
            self.assertGreaterEqual(hit.width() * win._view.transform().m11(), 23.9)
            self.assertGreaterEqual(hit.height() * win._view.transform().m11(), 23.9)
            edge = item.mapToScene(hit.center() + QPointF(hit.width() * 0.45, 0.0))
            self.assertIs(win._scene._resolve_edit_object(edge), item)

    def test_begin_small_text_edit_focus_cursor_and_temporary_area(self) -> None:
        win = self._window()
        item = win.add_text_rect(
            QRectF(40.0, 40.0, 4.0, 4.0), text="123",
            font_size=4.0, auto_edit=False, auto_fit=False)
        saved = item.serialize_edit_object()
        self.assertTrue(win.begin_text_edit(item))
        self.assertTrue(item.hasFocus() or win._scene.focusItem() is item)
        self.assertTrue(item.textInteractionFlags()
                        & Qt.TextInteractionFlag.TextEditorInteraction)
        self.assertFalse(item.textCursor().isNull())
        self.assertGreaterEqual(
            item.shape().boundingRect().width() * win._view.transform().m11(), 79.9)
        during = item.serialize_edit_object()
        for key in ("x", "y", "width", "height", "font_size"):
            self.assertEqual(saved[key], during[key])
        item.clearFocus()
        QApplication.processEvents()
        if hasattr(item, "_inline_edit_min_rect"):
            item.focusOutEvent(QFocusEvent(QFocusEvent.Type.FocusOut))
        self.assertFalse(hasattr(item, "_inline_edit_min_rect"))

    def test_thin_horizontal_diagonal_and_short_line_hit(self) -> None:
        win = self._window()
        cases = (
            (QPointF(20, 20), QPointF(120, 20), QPointF(70, 27)),
            (QPointF(20, 50), QPointF(120, 100), QPointF(67, 82)),
            (QPointF(20, 130), QPointF(25, 130), QPointF(22, 137)),
        )
        for index, (p1, p2, near) in enumerate(cases):
            line = win.add_line(p1, p2, line_width=0.5 if index != 1 else 1.0)
            self.assertTrue(line.shape().contains(line.mapFromScene(near)))
            self.assertIs(win._scene._resolve_edit_object(near), line)
        outside = QPointF(70, 29)
        first = win.edit_items()[0]
        self.assertFalse(first.shape().contains(first.mapFromScene(outside)))

    def test_hit_width_stays_constant_across_zoom(self) -> None:
        win = self._window()
        line = win.add_line(QPointF(20, 20), QPointF(120, 20), line_width=0.5)
        text = win.add_text_rect(
            QRectF(20, 60, 2, 2), text="1", font_size=4,
            auto_edit=False, auto_fit=False)
        for scale in (0.5, 1.0, 2.0):
            win._view.setTransform(QTransform().scale(scale, scale))
            line_px = line.shape().boundingRect().height() * scale
            text_rect = text.shape().boundingRect()
            self.assertGreaterEqual(line_px, 15.9)
            self.assertLessEqual(line_px, 16.1)
            self.assertGreaterEqual(text_rect.width() * scale, 23.9)
            self.assertGreaterEqual(text_rect.height() * scale, 23.9)

    def test_candidate_prefers_actual_small_text_and_nearest_crossing_line(self) -> None:
        win = self._window()
        win.add_rect(QRectF(0, 0, 300, 200), text="")
        text = win.add_text_rect(
            QRectF(80, 60, 12, 10), text="A", font_size=6,
            auto_edit=False, auto_fit=False)
        text_point = text.mapToScene(
            super(type(text), text).boundingRect().center())
        self.assertIs(win._scene._resolve_edit_object(text_point), text)

        horizontal = win.add_line(QPointF(30, 140), QPointF(170, 140), line_width=0.5)
        win.add_line(QPointF(100, 100), QPointF(100, 180), line_width=0.5)
        self.assertIs(win._scene._resolve_edit_object(QPointF(92, 141)), horizontal)

    def test_short_and_long_text_edit_matrix_at_all_required_font_sizes(self) -> None:
        """指定された文字列×fontの全組合せを、保存後の実item型で編集できる。"""
        from app.voucher_edit_window import (
            _EditSymbolTextItem, _EditTextItem, is_symbol_text_candidate,
        )

        win = self._window()
        strings = ("1", "12", "123", "1234", "A", "AB", "ABC",
                   "テ", "テキ", "テキス")
        sizes = (12.0, 36.0, 72.0, 120.0, 200.0)
        for text in strings:
            for size in sizes:
                with self.subTest(text=text, font_size=size):
                    item = win.add_text_rect(
                        QRectF(180, 250, 20, 20), text=text, font_size=size,
                        auto_edit=False)
                    if is_symbol_text_candidate(text):
                        self.assertTrue(win.maybe_convert_text_item_to_symbol(item))
                        item = win._edit_item_by_id(item.obj_id)
                        self.assertIsInstance(item, _EditSymbolTextItem)
                    else:
                        self.assertIsInstance(item, _EditTextItem)

                    # 実文字上（中央）を通る共通解決と編集開始。
                    center = item.sceneBoundingRect().center()
                    self.assertIs(win._scene._resolve_edit_object(center), item)
                    self.assertTrue(win.begin_text_edit(item))
                    editable = win._edit_item_by_id(item.obj_id)
                    self.assertIsInstance(editable, _EditTextItem)
                    self.assertTrue(editable.textInteractionFlags()
                                    & Qt.TextInteractionFlag.TextEditorInteraction)
                    self.assertIs(win._scene.focusItem(), editable)
                    self.assertGreater(editable.textWidth(), 0.0)
                    self.assertGreater(editable.document().idealWidth(), 0.0)
                    editable.clearFocus()
                    QApplication.processEvents()
                    current = win._edit_item_by_id(editable.obj_id)
                    if current is not None and current.scene() is not None:
                        win._scene.removeItem(current)

    def test_symbol_text_all_edit_entry_operations(self) -> None:
        """文字上・余白・拡張領域・右クリック編集・再選択後の入口を固定する。"""
        from app.voucher_edit_window import _EditTextItem

        win = self._window()

        def new_symbol():
            for existing in tuple(win.edit_items()):
                if existing.scene() is win._scene:
                    win._scene.removeItem(existing)
            item = win.add_text_rect(
                QRectF(220, 300, 140, 100), text="123", font_size=72,
                auto_edit=False, auto_fit=False)
            self.assertTrue(win.maybe_convert_text_item_to_symbol(item))
            return win._edit_item_by_id(item.obj_id)

        def double_click_at(pos):
            event = QGraphicsSceneMouseEvent(
                QGraphicsSceneMouseEvent.Type.GraphicsSceneMouseDoubleClick)
            event.setScenePos(pos)
            event.setButton(Qt.MouseButton.LeftButton)
            event.setButtons(Qt.MouseButton.LeftButton)
            win._scene.mouseDoubleClickEvent(event)

        # 実文字上。
        item = new_symbol()
        double_click_at(item.sceneBoundingRect().center())
        self.assertIsInstance(win._edit_item_by_id(item.obj_id), _EditTextItem)

        # 選択枠内の余白と、透明な拡張hit領域は通常textでも同じresolverを通る。
        for location in ("box_whitespace", "expanded_hit"):
            font_size = 72 if location == "box_whitespace" else 4
            rect = (QRectF(220, 300, 180, 120)
                    if location == "box_whitespace"
                    else QRectF(220, 300, 2, 2))
            item = win.add_text_rect(
                rect, text="123", font_size=font_size,
                auto_edit=False, auto_fit=False)
            if location == "box_whitespace":
                pos = item.mapToScene(QPointF(item.box_w - 2, item.box_h - 2))
            else:
                hit = item.shape().boundingRect()
                pos = item.mapToScene(
                    QPointF(hit.right() - 0.5, hit.center().y()))
            double_click_at(pos)
            self.assertTrue(item.textInteractionFlags()
                            & Qt.TextInteractionFlag.TextEditorInteraction)
            item.clearFocus()
            QApplication.processEvents()

        # 右クリック「編集」。
        item = new_symbol()
        menu = win._build_object_context_menu(item)
        action = menu.findChild(type(menu.actions()[0]), "edit_text_action")
        self.assertIsNotNone(action)
        action.trigger()
        self.assertIsInstance(win._edit_item_by_id(item.obj_id), _EditTextItem)

        # 一度選択してから再度ダブルクリック。
        item = new_symbol()
        win._select_only(item)
        double_click_at(item.sceneBoundingRect().center())
        self.assertIsInstance(win._edit_item_by_id(item.obj_id), _EditTextItem)


if __name__ == "__main__":
    unittest.main()
