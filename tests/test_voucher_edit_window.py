"""指図書編集画面（VoucherEditWindow）の動的テスト。

QApplication を offscreen で起動し、画面が開くこと・オブジェクト追加→保存→
再読み込みで編集内容が復元されることを検証する。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditWindow(unittest.TestCase):
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

    def test_window_opens_with_toolbar(self) -> None:
        from PySide6.QtWidgets import QToolBar

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertIn("指図書編集", win.windowTitle())
        toolbars = win.findChildren(QToolBar)
        self.assertTrue(toolbars)
        actions = [a.text() for tb in toolbars for a in tb.actions()]
        for label in ("選択", "テキスト", "線", "四角", "削除", "保存", "閉じる"):
            self.assertIn(label, actions)

    def test_add_text_and_save_then_reload(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(100.0, 150.0), text="テストメモ", font_size=12.0)
        objects = win.serialize_objects()
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["type"], "text")
        self.assertEqual(objects[0]["text"], "テストメモ")

        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()

        # 再度開くと編集内容が復元される
        win2 = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        reloaded = win2.serialize_objects()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["text"], "テストメモ")

    def test_short_text_is_converted_to_symbol_text(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="sym1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_rect(QRectF(100.0, 150.0, 80.0, 30.0),
                                 text="×", font_size=35.0, auto_edit=False)
        self.assertTrue(win.maybe_convert_text_item_to_symbol(item))
        obj = win.serialize_objects()[0]
        self.assertEqual(obj["type"], "symbol_text")
        self.assertEqual(obj["text"], "×")
        self.assertEqual(obj["font_size"], 35.0)
        self.assertEqual(obj["anchor"], "center")
        self.assertIn("x", obj)
        self.assertIn("y", obj)
        self.assertNotIn("width", obj)
        self.assertNotIn("height", obj)
        self.assertNotIn("vertical_align", obj)

    def test_plus_three_is_converted_to_symbol_text(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="sym2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_rect(QRectF(100.0, 150.0, 80.0, 30.0),
                                 text="+3", auto_edit=False)
        self.assertTrue(win.maybe_convert_text_item_to_symbol(item))
        self.assertEqual(win.serialize_objects()[0]["type"], "symbol_text")

    def test_multiline_and_long_text_stay_normal_text(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="sym3", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        multiline = win.add_text_rect(QRectF(10.0, 20.0, 160.0, 50.0),
                                      text="A\nB", auto_edit=False)
        long_text = win.add_text_rect(QRectF(10.0, 90.0, 200.0, 30.0),
                                      text="6/23 PM 西野商会様入", auto_edit=False)
        self.assertFalse(win.maybe_convert_text_item_to_symbol(multiline))
        self.assertFalse(win.maybe_convert_text_item_to_symbol(long_text))
        self.assertEqual([o["type"] for o in win.serialize_objects()], ["text", "text"])

    def test_symbol_text_roundtrip(self) -> None:
        from PySide6.QtCore import QPointF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="sym4", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_symbol_text(QPointF(500.0, 300.0), "+3", font_size=28.0)
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()

        win2 = VoucherEditWindow(order_no="sym4", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        obj = win2.serialize_objects()[0]
        self.assertEqual(obj["type"], "symbol_text")
        self.assertEqual(obj["text"], "+3")
        self.assertAlmostEqual(obj["x"], 500.0, delta=0.5)
        self.assertAlmostEqual(obj["y"], 300.0, delta=0.5)

    def test_delete_selected_removes_object(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="9999999", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_at(QPointF(50.0, 50.0), text="消す", font_size=12.0)
        self.assertEqual(len(win.serialize_objects()), 1)
        item.setSelected(True)
        win.delete_selected()
        self.assertEqual(len(win.serialize_objects()), 0)

    def test_loaded_objects_are_selectable_and_movable(self) -> None:
        """保存済みオブジェクトを読み込んでも編集フラグが付与される（要件1）。"""
        from PySide6.QtWidgets import QGraphicsItem

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="55", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10.0, 10.0), text="memo", font_size=12.0)
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()

        win2 = VoucherEditWindow(order_no="55", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        items = win2.edit_items()
        self.assertEqual(len(items), 1)
        flags = items[0].flags()
        self.assertTrue(flags & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.assertTrue(flags & QGraphicsItem.GraphicsItemFlag.ItemIsMovable)

    def test_reload_does_not_duplicate(self) -> None:
        """load_edit_layer を2回呼んでも編集レイヤーがクリアされ重複しない（要件2）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="66", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10.0, 10.0), text="一つ", font_size=12.0)
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        # 何度開き直しても1件のまま
        win.load_edit_layer()
        win.load_edit_layer()
        self.assertEqual(len(win.serialize_objects()), 1)

    def test_background_not_saved(self) -> None:
        """背景レイヤーは保存対象外（要件3）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="77", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        # 背景アイテムはシーンに存在するが、シリアライズ対象は0件。
        self.assertEqual(len(win.serialize_objects()), 0)
        self.assertTrue(len(win._scene.items()) >= 1)

    def test_add_text_rect_creates_box(self) -> None:
        """テキストがドラッグ矩形で作成され、サイズが保存・復元される（要件4）。"""
        from PySide6.QtCore import QRectF

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="88", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_rect(QRectF(20.0, 30.0, 120.0, 40.0), text="箱テキスト")
        self.assertGreater(item.box_w, 0.0)
        self.assertGreater(item.box_h, 0.0)
        obj = item.serialize_edit_object()
        self.assertEqual(obj["type"], "text")
        self.assertAlmostEqual(obj["h"], item.font_size * 1.2, delta=4.0)

    def test_add_line_drag(self) -> None:
        """線がドラッグ始点/終点で作成される（要件5）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="89", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_line(QPointF(10.0, 10.0), QPointF(100.0, 50.0))
        obj = item.serialize_edit_object()
        self.assertEqual(obj["type"], "line")
        self.assertAlmostEqual(obj["x1"], 10.0)
        self.assertAlmostEqual(obj["x2"], 100.0)

    def test_add_rect_with_inner_text_roundtrip(self) -> None:
        """四角形がドラッグ矩形で作成され、内部テキストが保存・再読み込みされる（要件5・6）。"""
        from PySide6.QtCore import QRectF

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="90", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        rect = win.add_rect(QRectF(40.0, 50.0, 120.0, 40.0), text="+2", font_size=14.0)
        obj = rect.serialize_edit_object()
        self.assertEqual(obj["type"], "rectangle")
        self.assertEqual(obj["text"], "+2")
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()

        win2 = VoucherEditWindow(order_no="90", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        reloaded = win2.serialize_objects()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["type"], "rectangle")
        self.assertEqual(reloaded[0]["text"], "+2")

    def test_toolbar_has_new_actions(self) -> None:
        """丸・保存して閉じるが追加されている（要件5・8）。"""
        from PySide6.QtWidgets import QToolBar

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="t1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        from PySide6.QtWidgets import QToolBar as _TB
        actions = [a.text() for tb in win.findChildren(_TB) for a in tb.actions()]
        for label in ("選択", "テキスト", "線", "四角", "丸", "保存", "保存して閉じる", "閉じる"):
            self.assertIn(label, actions)

    def test_delete_key_removes_selected(self) -> None:
        """Deleteキーで選択中オブジェクトが削除される（要件2）。"""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="del1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_at(QPointF(20.0, 20.0), text="x", font_size=12.0)
        item.setSelected(True)
        ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Delete,
                       Qt.KeyboardModifier.NoModifier)
        win.keyPressEvent(ev)
        self.assertEqual(len(win.serialize_objects()), 0)

    def test_ctrl_a_selects_only_edit_objects(self) -> None:
        """Ctrl+A（select_all）で編集オブジェクトだけが選択される（要件4）。"""
        from PySide6.QtCore import QRectF

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="sa1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10.0, 10.0), text="a", font_size=12.0)
        win.add_rect(QRectF(30.0, 30.0, 40.0, 20.0), text="b")
        win.select_all()
        selected = win._scene.selectedItems()
        # 選択された全アイテムが編集オブジェクト（背景・ハンドルは含まれない）。
        self.assertTrue(selected)
        self.assertTrue(all(hasattr(it, "serialize_edit_object") for it in selected))
        self.assertEqual(len(selected), 2)

    def test_undo_redo_add_and_delete(self) -> None:
        """Undo/Redoで追加・削除を戻せる（要件3）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ur1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertEqual(len(win.serialize_objects()), 0)
        item = win.add_text_at(QPointF(10.0, 10.0), text="a", font_size=12.0)
        win.commit_history()
        self.assertEqual(len(win.serialize_objects()), 1)
        win.undo()
        self.assertEqual(len(win.serialize_objects()), 0)
        win.redo()
        self.assertEqual(len(win.serialize_objects()), 1)
        # 削除も戻せる
        for it in win.edit_items():
            it.setSelected(True)
        win.delete_selected()
        self.assertEqual(len(win.serialize_objects()), 0)
        win.undo()
        self.assertEqual(len(win.serialize_objects()), 1)

    def test_save_and_close_saves_then_closes(self) -> None:
        """保存して閉じるが保存後に画面を閉じる（要件5）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="sc1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10.0, 10.0), text="closeme", font_size=12.0)
        closed = {"v": False}
        win.close = lambda: closed.__setitem__("v", True)  # type: ignore[assignment]
        win.save_and_close()
        self.assertTrue(closed["v"])

        win2 = VoucherEditWindow(order_no="sc1", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        self.assertEqual(len(win2.serialize_objects()), 1)

    def test_ellipse_create_save_reload(self) -> None:
        """丸/楕円が作成・保存・再読み込みされる（要件8）。"""
        from PySide6.QtCore import QRectF

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="el1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_ellipse(QRectF(40.0, 50.0, 80.0, 60.0), text="O", font_size=14.0)
        obj = item.serialize_edit_object()
        self.assertEqual(obj["type"], "ellipse")
        self.assertEqual(obj["text"], "O")
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        win2 = VoucherEditWindow(order_no="el1", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        reloaded = win2.serialize_objects()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["type"], "ellipse")
        self.assertEqual(reloaded[0]["text"], "O")

    def test_resize_text_box_saved(self) -> None:
        """テキストボックスのサイズ変更が保存される（要件6）。"""
        from PySide6.QtCore import QPointF as _QP
        from PySide6.QtCore import QRectF

        from app.voucher_edit_window import VoucherEditWindow, _ResizeHandle

        win = VoucherEditWindow(order_no="rs1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_rect(QRectF(20.0, 20.0, 60.0, 18.0), text="t")
        handle = _ResizeHandle(item)
        handle._resize_target(_QP(200.0, 120.0))
        self.assertGreater(item.box_w, 60.0)
        self.assertGreater(item.box_h, 18.0)

    def test_line_endpoint_move_saved(self) -> None:
        """線の終点ハンドルで終点を移動できる（要件6）。"""
        from PySide6.QtCore import QPointF as _QP

        from app.voucher_edit_window import VoucherEditWindow, _LineEndHandle

        win = VoucherEditWindow(order_no="ln1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        line = win.add_line(_QP(10.0, 10.0), _QP(50.0, 10.0))
        handle = _LineEndHandle(line, "p2")
        win._scene.addItem(handle)
        handle.setPos(_QP(120.0, 80.0))
        obj = line.serialize_edit_object()
        self.assertAlmostEqual(obj["x2"], 120.0, places=3)

    def test_line_width_applies_to_selection(self) -> None:
        """線幅変更が選択中オブジェクトへ反映される（要件9）。"""
        from PySide6.QtCore import QPointF as _QP

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="lw1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        line = win.add_line(_QP(10.0, 10.0), _QP(50.0, 50.0))
        line.setSelected(True)
        win._line_width_spin.setValue(3.5)
        self.assertAlmostEqual(line.line_width, 3.5)

    def test_font_size_applies_to_selection(self) -> None:
        """フォントサイズ変更が選択中テキストへ反映される（要件10）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fs1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_at(QPointF(10.0, 10.0), text="z", font_size=12.0)
        item.setSelected(True)
        win._font_size_spin.setValue(28)
        self.assertEqual(item.font_size, 28.0)

    def test_tool_highlight_switches(self) -> None:
        """選択中ツールのボタンだけがハイライト（チェック）される（要件11）。"""
        from app.voucher_edit_window import (
            TOOL_ELLIPSE,
            TOOL_LINE,
            TOOL_RECT,
            TOOL_SELECT,
            TOOL_TEXT,
            VoucherEditWindow,
        )

        win = VoucherEditWindow(order_no="th1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        tools = (TOOL_TEXT, TOOL_LINE, TOOL_RECT, TOOL_ELLIPSE, TOOL_SELECT)
        for selected_tool in tools:
            win.set_tool(selected_tool)
            checked = [
                tool for tool, action in win._tool_actions.items()
                if action.isChecked()
            ]
            self.assertEqual(checked, [selected_tool])

    def test_edit_tool_buttons_use_selected_button_style(self) -> None:
        """編集ツールだけに、反映先と同じ選択色の限定スタイルを適用する。"""
        from PySide6.QtWidgets import QToolBar, QToolButton

        from app.voucher_edit_window import (
            EDIT_TOOLBAR_STYLE,
            TOOL_TEXT,
            VoucherEditWindow,
        )

        win = VoucherEditWindow(order_no="ths1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        bar = win.findChildren(QToolBar)[0]
        text_button = bar.widgetForAction(win._tool_actions[TOOL_TEXT])
        self.assertIsInstance(text_button, QToolButton)
        self.assertTrue(text_button.property("editToolButton"))
        self.assertTrue(win._tool_actions[TOOL_TEXT].isChecked())
        self.assertIn('QToolButton[editToolButton="true"]:checked', EDIT_TOOLBAR_STYLE)
        self.assertIn("background-color: #0d6efd", EDIT_TOOLBAR_STYLE)
        self.assertIn("color: #ffffff", EDIT_TOOLBAR_STYLE)
        self.assertIn("border: 2px solid #66b2ff", EDIT_TOOLBAR_STYLE)
        self.assertIn(":checked:disabled", EDIT_TOOLBAR_STYLE)

    def test_reflect_target_highlight_switches_exclusively(self) -> None:
        """反映先を切り替えると青背景が1ボタンだけへ移る。"""
        from app.voucher_edit_window import VoucherEditWindow

        with mock.patch(
            "app.voucher_edit_window.current_title_bar_is_dark",
            return_value=True,
        ):
            win = VoucherEditWindow(order_no="rth1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        names = list(win._template_actions)
        self.assertGreaterEqual(len(names), 2)
        for button in win._template_actions.values():
            self.assertTrue(button.property("reflectTargetButton"))

        for selected_name in names[:2]:
            win._on_template_selected(win._template_by_name(selected_name))
            selected = [
                name for name, button in win._template_actions.items()
                if button.isChecked()
                and button.property("reflectTargetSelected") is True
            ]
            self.assertEqual(selected, [selected_name])
            for name, button in win._template_actions.items():
                self.assertEqual(button.isChecked(), name == selected_name)
                self.assertEqual(
                    button.property("reflectTargetSelected"),
                    name == selected_name,
                )
                if name == selected_name:
                    self.assertIn(
                        "background-color: #0d6efd",
                        button.styleSheet(),
                    )
                else:
                    self.assertNotIn(
                        "background-color: #0d6efd",
                        button.styleSheet(),
                    )

    def test_reflect_target_selected_style_is_blue_in_light_and_dark_modes(self) -> None:
        """ライト/ダークとも選択中は直接指定の青背景になる。"""
        from app.voucher_edit_window import VoucherEditWindow

        for is_dark in (False, True):
            with self.subTest(is_dark=is_dark), mock.patch(
                "app.voucher_edit_window.current_title_bar_is_dark",
                return_value=is_dark,
            ):
                win = VoucherEditWindow(
                    order_no=f"rth-theme-{is_dark}",
                    background_pdf_bytes=b"",
                )
                self.addCleanup(win.deleteLater)
                selected = [
                    button for button in win._template_actions.values()
                    if button.isChecked()
                ]
                self.assertEqual(len(selected), 1)
                self.assertIn(
                    "background-color: #0d6efd",
                    selected[0].styleSheet(),
                )
                for button in win._template_actions.values():
                    if button is not selected[0]:
                        self.assertNotIn(
                            "background-color: #0d6efd",
                            button.styleSheet(),
                        )

    def test_locked_reflect_target_buttons_have_style_properties(self) -> None:
        """ロックアイコン付き固定テンプレートにも直接スタイルが設定される。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="rth2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        locked_buttons = [
            button for button in win._template_actions.values()
            if button.text().startswith("🔒 ")
        ]
        self.assertTrue(locked_buttons)
        for button in locked_buttons:
            self.assertTrue(button.property("reflectTargetButton"))
            self.assertTrue(button.styleSheet().strip())
        selected_locked = [button for button in locked_buttons if button.isChecked()]
        self.assertEqual(len(selected_locked), 1)
        self.assertIn(
            "background-color: #0d6efd",
            selected_locked[0].styleSheet(),
        )

    def test_continuous_insert_keeps_tool(self) -> None:
        """オブジェクト作成後もツールが選択へ戻らない（要件12）。"""
        from PySide6.QtCore import QPointF as _QP
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QGraphicsSceneMouseEvent

        from app.voucher_edit_window import TOOL_RECT, VoucherEditWindow

        win = VoucherEditWindow(order_no="ci1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.set_tool(TOOL_RECT)
        scene = win._scene

        def _mk(etype, pos):
            ev = QGraphicsSceneMouseEvent(etype)
            ev.setScenePos(pos)
            ev.setButton(Qt.MouseButton.LeftButton)
            return ev

        scene.mousePressEvent(_mk(QGraphicsSceneMouseEvent.Type.GraphicsSceneMousePress, _QP(10, 10)))
        scene.mouseMoveEvent(_mk(QGraphicsSceneMouseEvent.Type.GraphicsSceneMouseMove, _QP(60, 50)))
        scene.mouseReleaseEvent(_mk(QGraphicsSceneMouseEvent.Type.GraphicsSceneMouseRelease, _QP(60, 50)))
        self.assertEqual(win.current_tool, TOOL_RECT)
        self.assertEqual(len(win.serialize_objects()), 1)

    def test_escape_returns_to_select(self) -> None:
        """Escキーで選択ツールへ戻る（要件12）。"""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        from app.voucher_edit_window import (
            TOOL_LINE,
            TOOL_SELECT,
            VoucherEditWindow,
        )

        win = VoucherEditWindow(order_no="esc1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.set_tool(TOOL_LINE)
        ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                       Qt.KeyboardModifier.NoModifier)
        win.keyPressEvent(ev)
        self.assertEqual(win.current_tool, TOOL_SELECT)

    def test_initial_tool_is_text(self) -> None:
        """初期ツールが「テキスト」になっている（要件2）。"""
        from app.voucher_edit_window import TOOL_TEXT, VoucherEditWindow

        win = VoucherEditWindow(order_no="it1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertEqual(win.current_tool, TOOL_TEXT)

    def test_initial_highlight_is_text(self) -> None:
        """初期表示でテキストボタンがハイライト（チェック）されている（要件2）。"""
        from app.voucher_edit_window import (
            TOOL_SELECT,
            TOOL_TEXT,
            VoucherEditWindow,
        )

        win = VoucherEditWindow(order_no="ih1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertTrue(win._tool_actions[TOOL_TEXT].isChecked())
        self.assertFalse(win._tool_actions[TOOL_SELECT].isChecked())
        self.assertTrue(win._tool_actions[TOOL_TEXT].font().bold())

    def test_ctrl_y_redo(self) -> None:
        """Undo後に redo() でやり直せる（Ctrl+Y相当: 要件1）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="cy1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10.0, 10.0), text="a", font_size=12.0)
        win.commit_history()
        self.assertEqual(len(win.serialize_objects()), 1)
        win.undo()
        self.assertEqual(len(win.serialize_objects()), 0)
        win.redo()
        self.assertEqual(len(win.serialize_objects()), 1)

    def test_ctrl_shift_z_shortcut_registered(self) -> None:
        """Ctrl+Shift+Z / Ctrl+Y がやり直しショートカットとして一意登録される（要件1）。"""
        from PySide6.QtGui import QShortcut

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="csz1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        keys = {sc.key().toString() for sc in win.findChildren(QShortcut)}
        self.assertIn("Ctrl+Y", keys)
        self.assertIn("Ctrl+Shift+Z", keys)
        # 同一キー列の二重登録（曖昧化）が無いこと。
        key_list = [sc.key().toString() for sc in win.findChildren(QShortcut)]
        self.assertEqual(len(key_list), len(set(key_list)))

    def test_redo_preserved_after_undo(self) -> None:
        """Undo後もRedo履歴が残る（復元中に消えない: 要件1）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="rp1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10.0, 10.0), text="a", font_size=12.0)
        win.commit_history()
        win.add_text_at(QPointF(40.0, 40.0), text="b", font_size=12.0)
        win.commit_history()
        self.assertEqual(len(win.serialize_objects()), 2)
        win.undo()
        self.assertEqual(len(win.serialize_objects()), 1)
        # Redoスタックが残っている
        self.assertLess(win._history_index, len(win._history) - 1)
        win.redo()
        self.assertEqual(len(win.serialize_objects()), 2)

    def test_new_op_clears_redo(self) -> None:
        """新規操作後だけRedo履歴がクリアされる（要件1）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="nc1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10.0, 10.0), text="a", font_size=12.0)
        win.commit_history()
        win.undo()
        self.assertEqual(len(win.serialize_objects()), 0)
        # 新しい操作をするとRedoスタックは消える
        win.add_text_at(QPointF(40.0, 40.0), text="c", font_size=12.0)
        win.commit_history()
        self.assertEqual(win._history_index, len(win._history) - 1)
        win.redo()  # もう先がない
        self.assertEqual(len(win.serialize_objects()), 1)

    def test_empty_text_not_saved(self) -> None:
        """空文字テキストオブジェクトは保存対象外（要件3）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="et1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10.0, 10.0), text="   ", font_size=12.0)
        win.add_text_at(QPointF(40.0, 40.0), text="残す", font_size=12.0)
        objs = win.serialize_objects()
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0]["text"], "残す")
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        win2 = VoucherEditWindow(order_no="et1", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        self.assertEqual(len(win2.serialize_objects()), 1)

    def test_empty_text_not_reloaded(self) -> None:
        """JSONに空文字テキストがあっても読み込み時に復元しない（要件3）。"""
        from app.voucher_edit_objects import save_edit_objects
        from app.voucher_edit_window import VoucherEditWindow

        save_edit_objects("et2", [
            {"id": "x1", "type": "text", "x": 10.0, "y": 10.0,
             "w": 60.0, "h": 18.0, "text": "  ", "font_size": 12.0,
             "color": [0, 0, 0]},
            {"id": "x2", "type": "text", "x": 20.0, "y": 20.0,
             "w": 60.0, "h": 18.0, "text": "有効", "font_size": 12.0,
             "color": [0, 0, 0]},
        ])
        win = VoucherEditWindow(order_no="et2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        objs = win.serialize_objects()
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0]["text"], "有効")

    def test_rect_auto_edit_enters_text_mode(self) -> None:
        """四角形作成直後に内部テキスト編集状態になる（要件4）。"""
        from PySide6.QtCore import QRectF, Qt

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ra1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        rect = win.add_rect(QRectF(40.0, 40.0, 80.0, 40.0), auto_edit=True)
        self.assertEqual(
            rect._text.textInteractionFlags(),
            Qt.TextInteractionFlag.TextEditorInteraction,
        )
        # 図形は内部テキストが空でも残る
        self.assertEqual(len(win.edit_items()), 1)

    def test_ellipse_auto_edit_enters_text_mode(self) -> None:
        """丸/楕円作成直後に内部テキスト編集状態になる（要件4）。"""
        from PySide6.QtCore import QRectF, Qt

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ea1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        el = win.add_ellipse(QRectF(40.0, 40.0, 80.0, 40.0), auto_edit=True)
        self.assertEqual(
            el._text.textInteractionFlags(),
            Qt.TextInteractionFlag.TextEditorInteraction,
        )
        self.assertEqual(len(win.edit_items()), 1)

    def test_shape_tool_does_not_hijack_existing_object(self) -> None:
        """図形ツール選択中でも既存オブジェクト上の操作は新規作成しない（要件5）。"""
        from PySide6.QtCore import QPointF as _QP
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtWidgets import QGraphicsSceneMouseEvent

        from app.voucher_edit_window import TOOL_RECT, VoucherEditWindow

        win = VoucherEditWindow(order_no="sh1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_rect(QRectF(40.0, 50.0, 120.0, 40.0), text="既存")
        win.set_tool(TOOL_RECT)
        scene = win._scene
        # 既存四角の中央を判定: 既存オブジェクト扱い
        self.assertTrue(scene._hits_existing_object(_QP(100.0, 70.0)))
        # 既存四角中央を押下しても新規作成（temp_item）が始まらない
        ev = QGraphicsSceneMouseEvent(
            QGraphicsSceneMouseEvent.Type.GraphicsSceneMousePress)
        ev.setScenePos(_QP(100.0, 70.0))
        ev.setButton(Qt.MouseButton.LeftButton)
        scene.mousePressEvent(ev)
        self.assertIsNone(scene._temp_item)
        # 移動/サイズ変更の履歴記録のためスナップショットが取られている
        self.assertIsNotNone(scene._select_snapshot)
        self.assertEqual(len(win.serialize_objects()), 1)

    def test_shape_tool_creates_on_empty_space(self) -> None:
        """図形ツール選択中、空白部分ドラッグでは新規作成する（要件5）。"""
        from PySide6.QtCore import QPointF as _QP

        from app.voucher_edit_window import TOOL_RECT, VoucherEditWindow

        win = VoucherEditWindow(order_no="se1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.set_tool(TOOL_RECT)
        # 何も無い空白位置は新規作成対象
        self.assertFalse(win._scene._hits_existing_object(_QP(300.0, 300.0)))

    def test_resize_handle_works_regardless_of_tool(self) -> None:
        """図形ツール選択中でも既存図形をリサイズできる（要件5）。"""
        from PySide6.QtCore import QPointF as _QP
        from PySide6.QtCore import QRectF

        from app.voucher_edit_window import (
            TOOL_RECT,
            VoucherEditWindow,
            _ResizeHandle,
        )

        win = VoucherEditWindow(order_no="rh1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        rect = win.add_rect(QRectF(40.0, 50.0, 80.0, 40.0), text="r")
        win.set_tool(TOOL_RECT)
        handle = _ResizeHandle(rect)
        handle._resize_target(_QP(200.0, 160.0))
        self.assertGreater(rect.rect().width(), 80.0)

    def test_selection_resize_handle_is_not_serialized(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="handle-not-saved", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_rect(QRectF(10.0, 20.0, 80.0, 24.0),
                                 text="T", auto_edit=False)
        item.setSelected(True)
        win._on_selection_changed()
        self.assertTrue(win._handles)
        objs = win.serialize_objects()
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0]["type"], "text")

    def test_pick_text_font_selects_candidate(self) -> None:
        """利用可能な通常フォント候補が選択される。"""
        from app.voucher_edit_window import pick_text_font_family

        with mock.patch("app.voucher_edit_window.QFontDatabase.families",
                        return_value=["Arial", "Meiryo", "Times"]):
            self.assertEqual(pick_text_font_family(), "Meiryo")

    def test_pick_text_font_fallback(self) -> None:
        """候補が無い場合は空文字（Qt既定）へフォールバックする。"""
        from app.voucher_edit_window import pick_text_font_family

        with mock.patch("app.voucher_edit_window.QFontDatabase.families",
                        return_value=["Arial", "Times"]):
            self.assertEqual(pick_text_font_family(), "")

    def test_new_text_uses_normal_font(self) -> None:
        """新規テキストは手書き風ではなく通常フォント候補で作成される。"""
        from app.voucher_edit_window import VoucherEditWindow

        with mock.patch("app.voucher_edit_window.QFontDatabase.families",
                        return_value=["Yu Gothic UI", "Meiryo"]):
            win = VoucherEditWindow(order_no="nf1", background_pdf_bytes=b"")
            self.addCleanup(win.deleteLater)
            item = win.add_text_at(QPointF(10.0, 10.0), text="通常", font_size=12.0)
            self.assertEqual(item.font_family, "Yu Gothic UI")
            self.assertFalse(item.font().italic())

    def test_selected_object_updates_toolbar_values(self) -> None:
        """選択時にオブジェクト属性がツールバーへ反映される。"""
        from PySide6.QtCore import QPointF as _QP
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="tb1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_at(_QP(10.0, 10.0), text="a", font_size=18.0)
        line = win.add_line(_QP(10.0, 40.0), _QP(60.0, 40.0), line_width=3.0)
        text.setSelected(True)
        self.assertEqual(win._font_size_spin.value(), 18)
        text.setSelected(False)
        line.setSelected(True)
        self.assertAlmostEqual(win._line_width_spin.value(), 3.0)

    def test_toolbar_change_does_not_affect_unselected_objects(self) -> None:
        """ツールバー変更は選択中オブジェクトだけへ反映される。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="iso1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        a = win.add_text_at(QPointF(10.0, 10.0), text="a", font_size=12.0)
        b = win.add_text_at(QPointF(40.0, 40.0), text="b", font_size=14.0)
        a.setSelected(True)
        win._font_size_spin.setValue(22)
        self.assertEqual(a.font_size, 22.0)
        self.assertEqual(b.font_size, 14.0)

        a.setSelected(False)
        win._font_size_spin.setValue(30)
        self.assertEqual(a.font_size, 22.0)
        self.assertEqual(b.font_size, 14.0)
        c = win.add_text_at(QPointF(80.0, 80.0), text="c")
        self.assertEqual(c.font_size, 30.0)

    def test_resize_results_are_serialized(self) -> None:
        """テキスト・図形・線のリサイズ結果がJSON属性へ出る。"""
        from PySide6.QtCore import QPointF as _QP
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow, _LineEndHandle, _ResizeHandle

        win = VoucherEditWindow(order_no="rjs1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_rect(QRectF(20.0, 20.0, 60.0, 18.0), text="t")
        rect = win.add_rect(QRectF(100.0, 20.0, 40.0, 20.0), text="r")
        ellipse = win.add_ellipse(QRectF(160.0, 20.0, 40.0, 20.0), text="e")
        line = win.add_line(_QP(10.0, 100.0), _QP(30.0, 100.0))
        _ResizeHandle(text)._resize_target(_QP(120.0, 80.0))
        _ResizeHandle(rect)._resize_target(_QP(170.0, 80.0))
        _ResizeHandle(ellipse)._resize_target(_QP(240.0, 90.0))
        h = _LineEndHandle(line, "p2")
        win._scene.addItem(h)
        h.setPos(_QP(90.0, 140.0))

        objs = {o["type"]: o for o in win.serialize_objects()}
        self.assertGreater(objs["text"]["width"], 60.0)
        self.assertGreater(objs["rectangle"]["width"], 40.0)
        self.assertGreater(objs["ellipse"]["height"], 20.0)
        self.assertAlmostEqual(objs["line"]["x2"], 90.0, places=3)

    def test_text_save_uses_held_box_rect_not_scene_bounding_rect(self) -> None:
        """テキスト保存座標は保持boxで、描画矩形に引きずられない。"""
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="text-box", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_rect(QRectF(20.0, 30.0, 80.0, 24.0),
                                 text="A\nB", font_size=18.0, auto_edit=False)
        item.set_box_size(120.0, 40.0)
        obj = item.serialize_edit_object()
        self.assertAlmostEqual(obj["x"], 20.0)
        self.assertAlmostEqual(obj["y"], 30.0)
        self.assertAlmostEqual(obj["width"], 120.0)
        self.assertAlmostEqual(obj["height"], 40.0)
        self.assertFalse(obj["auto_fit"])
        self.assertTrue(obj["manual_resized"])

    def test_text_document_margin_is_zero_for_text_and_shape_text(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="text-margin", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_rect(QRectF(10.0, 20.0, 80.0, 24.0),
                                 text="T", auto_edit=False)
        rect = win.add_rect(QRectF(100.0, 20.0, 80.0, 30.0), text="R")
        ellipse = win.add_ellipse(QRectF(200.0, 20.0, 80.0, 30.0), text="E")
        self.assertEqual(text.document().documentMargin(), 0)
        self.assertEqual(rect._text.document().documentMargin(), 0)
        self.assertEqual(ellipse._text.document().documentMargin(), 0)

    def test_text_box_saves_default_left_top_alignment(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="text-align-default", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_rect(QRectF(20.0, 30.0, 80.0, 24.0),
                                 text="T", auto_edit=False)
        obj = item.serialize_edit_object()
        self.assertEqual(obj["text_align"], "left")
        self.assertEqual(obj["vertical_align"], "top")

    def test_shape_text_defaults_stay_center_middle(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="shape-align-default", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        rect = win.add_rect(QRectF(20.0, 30.0, 80.0, 24.0), text="R")
        obj = rect.serialize_edit_object()
        self.assertEqual(obj["text_align"], "center")
        self.assertEqual(obj["vertical_align"], "middle")

    def test_text_box_auto_fits_height_to_font_size(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="text-autofit", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_rect(QRectF(20.0, 30.0, 200.0, 90.0),
                                 text="T", font_size=30.0, auto_edit=False)
        obj = item.serialize_edit_object()
        self.assertLess(obj["height"], 45.0)
        self.assertGreaterEqual(obj["height"], 30.0 * 1.2)

    def test_text_box_refits_after_font_size_change(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="text-font-refit", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_rect(QRectF(20.0, 30.0, 80.0, 18.0),
                                 text="T", font_size=12.0, auto_edit=False)
        item.apply_font_size(40.0)
        self.assertGreaterEqual(item.serialize_edit_object()["height"], 40.0 * 1.2)

    def test_shape_save_uses_item_rect_mapped_to_scene(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="shape-rect", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        rect = win.add_rect(QRectF(10.0, 20.0, 40.0, 30.0), line_width=20.0)
        ellipse = win.add_ellipse(QRectF(60.0, 70.0, 50.0, 35.0), line_width=20.0)
        rect.setPos(5.0, 7.0)
        ellipse.setPos(11.0, 13.0)
        r_obj = rect.serialize_edit_object()
        e_obj = ellipse.serialize_edit_object()
        self.assertEqual((r_obj["x"], r_obj["y"], r_obj["width"], r_obj["height"]),
                         (15.0, 27.0, 40.0, 30.0))
        self.assertEqual((e_obj["x"], e_obj["y"], e_obj["width"], e_obj["height"]),
                         (71.0, 83.0, 50.0, 35.0))

    def test_line_save_uses_line_endpoints_mapped_to_scene(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="line-map", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        line = win.add_line(QPointF(1.0, 2.0), QPointF(3.0, 4.0))
        line.setPos(10.0, 20.0)
        obj = line.serialize_edit_object()
        self.assertEqual((obj["x1"], obj["y1"], obj["x2"], obj["y2"]),
                         (11.0, 22.0, 13.0, 24.0))

    # ── 背景レイヤーが消えないこと（指図書編集の背景消失バグ対策）──────────────
    def _bg_count(self, win) -> int:
        return len(win.background_items())

    def test_background_present_on_open(self) -> None:
        """編集画面を開くと背景アイテムが存在する。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertGreaterEqual(self._bg_count(win), 1)

    def test_scene_rect_and_background_pixmap_use_pdf_point_space(self) -> None:
        """背景pixmapはsceneのPDFポイント空間へ配置・拡縮される。"""
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QGraphicsPixmapItem

        from app.voucher_templates import PAGE_H, PAGE_W
        from app.voucher_edit_window import VoucherEditWindow

        pixmap = QPixmap(1000, 500)
        with mock.patch("app.voucher_edit_window.render_order_sheet_background",
                        return_value=pixmap):
            win = VoucherEditWindow(order_no="bg-coord", background_pdf_bytes=b"pdf")
        self.addCleanup(win.deleteLater)
        self.assertEqual(win._scene.sceneRect().x(), 0.0)
        self.assertEqual(win._scene.sceneRect().y(), 0.0)
        self.assertAlmostEqual(win._scene.sceneRect().width(), PAGE_W)
        self.assertAlmostEqual(win._scene.sceneRect().height(), PAGE_H)
        bg = next(it for it in win.background_items()
                  if isinstance(it, QGraphicsPixmapItem))
        self.assertAlmostEqual(bg.pos().x(), 0.0)
        self.assertAlmostEqual(bg.pos().y(), 0.0)
        self.assertAlmostEqual(bg.scale(), PAGE_W / pixmap.width())

    def test_background_pixmap_scale_log_is_emitted(self) -> None:
        from PySide6.QtGui import QPixmap
        from app.voucher_edit_window import VoucherEditWindow

        pixmap = QPixmap(1000, 500)
        with mock.patch("app.voucher_edit_window.render_order_sheet_background",
                        return_value=pixmap):
            with self.assertLogs("tks_to_kintone_app", level="DEBUG") as logs:
                win = VoucherEditWindow(order_no="bg-log", background_pdf_bytes=b"pdf")
        self.addCleanup(win.deleteLater)
        joined = "\n".join(logs.output)
        self.assertIn("pixmap.width=1000", joined)
        self.assertIn("pixmap.height=500", joined)
        self.assertIn("scale_x=", joined)
        self.assertIn("scale_y=", joined)

    def test_text_insert_keeps_background(self) -> None:
        """テキストを挿入しても背景アイテムが scene に残る。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        win.add_text_at(QPointF(100.0, 100.0), text="メモ", font_size=12.0)
        win.commit_history()
        self.assertEqual(self._bg_count(win), before)

    def test_shapes_insert_keep_background(self) -> None:
        """四角・丸・線を挿入しても背景が残る。"""
        from PySide6.QtCore import QRectF

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg3", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        win.add_rect(QRectF(10.0, 10.0, 50.0, 30.0))
        win.add_ellipse(QRectF(80.0, 10.0, 40.0, 40.0))
        win.add_line(QPointF(10.0, 80.0), QPointF(90.0, 80.0))
        self.assertEqual(self._bg_count(win), before)

    def test_empty_text_delete_keeps_background(self) -> None:
        """空文字テキストを作って削除しても背景が残る（要件3・4）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg4", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        item = win.add_text_at(QPointF(50.0, 50.0), text="", font_size=12.0)
        win.remove_text_item(item)
        self.assertEqual(self._bg_count(win), before)
        # 編集オブジェクトは無いが背景は残る。
        self.assertEqual(len(win.serialize_objects()), 0)

    def test_undo_keeps_background(self) -> None:
        """Undo しても背景が残る（要件1・2）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg5", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        win.add_text_at(QPointF(20.0, 20.0), text="あ", font_size=12.0)
        win.commit_history()
        win.undo()
        self.assertEqual(self._bg_count(win), before)

    def test_redo_keeps_background(self) -> None:
        """Redo しても背景が残る（要件1・2）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg6", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        win.add_text_at(QPointF(20.0, 20.0), text="い", font_size=12.0)
        win.commit_history()
        win.undo()
        win.redo()
        self.assertEqual(self._bg_count(win), before)
        self.assertEqual(len(win.serialize_objects()), 1)

    def test_clear_edit_layer_keeps_background(self) -> None:
        """clear_edit_layer は背景を削除しない（要件1）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg7", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        win.add_text_at(QPointF(30.0, 30.0), text="x", font_size=12.0)
        win.clear_edit_layer()
        self.assertEqual(self._bg_count(win), before)
        self.assertEqual(len(win.edit_items()), 0)

    def test_restore_snapshot_keeps_background(self) -> None:
        """restore_snapshot（Undo/Redo経路）は背景を削除しない（要件2）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg8", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        win.add_text_at(QPointF(40.0, 40.0), text="y", font_size=12.0)
        snap = win.serialize_objects()
        win._restore_snapshot(snap)
        self.assertEqual(self._bg_count(win), before)
        self.assertEqual(len(win.edit_items()), 1)

    def test_select_all_does_not_select_background(self) -> None:
        """Ctrl+A で背景は選択されない（要件4・5）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg9", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(15.0, 15.0), text="z", font_size=12.0)
        win.select_all()
        for it in win.background_items():
            self.assertFalse(it.isSelected())

    def test_delete_does_not_remove_background(self) -> None:
        """全選択して削除しても背景は消えない（要件5）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg10", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        win.add_text_at(QPointF(15.0, 15.0), text="w", font_size=12.0)
        win.select_all()
        win.delete_selected()
        self.assertEqual(self._bg_count(win), before)
        self.assertEqual(len(win.serialize_objects()), 0)

    def test_background_not_selectable_or_movable(self) -> None:
        """背景は選択不可・移動不可（要件5）。"""
        from PySide6.QtWidgets import QGraphicsItem

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg11", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        for it in win.background_items():
            flags = it.flags()
            self.assertFalse(flags & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
            self.assertFalse(flags & QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
            self.assertLess(it.zValue(), 0)

    def test_ensure_background_visible_recovers(self) -> None:
        """背景が失われても ensure_background_visible で復旧する（要件6・保険）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bg12", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(15.0, 15.0), text="t", font_size=12.0)
        # 想定外操作を模して背景を強制削除する。
        for it in win.background_items():
            win._scene.removeItem(it)
        self.assertEqual(self._bg_count(win), 0)
        win.ensure_background_visible()
        self.assertGreaterEqual(self._bg_count(win), 1)
        # 編集オブジェクトには影響しない。
        self.assertEqual(len(win.edit_items()), 1)


    # ── ドラッグ作成中も背景が消えないこと（要件1・2）──────────────────────────
    def _mk_scene_event(self, etype, pos, button=None, modifiers=None):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QGraphicsSceneMouseEvent

        ev = QGraphicsSceneMouseEvent(etype)
        ev.setScenePos(pos)
        if button is not None:
            ev.setButton(button)
        if modifiers is not None:
            ev.setModifiers(modifiers)
        return ev

    def _drag(self, win, tool, start, end, release=True):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QGraphicsSceneMouseEvent as _E

        win.set_tool(tool)
        scene = win._scene
        scene.mousePressEvent(self._mk_scene_event(
            _E.Type.GraphicsSceneMousePress, start, Qt.MouseButton.LeftButton))
        scene.mouseMoveEvent(self._mk_scene_event(
            _E.Type.GraphicsSceneMouseMove, end, Qt.MouseButton.LeftButton))
        if release:
            scene.mouseReleaseEvent(self._mk_scene_event(
                _E.Type.GraphicsSceneMouseRelease, end, Qt.MouseButton.LeftButton))

    def test_text_drag_start_keeps_background(self) -> None:
        """テキスト挿入開始（押下直後）も背景が残る（要件1・2）。"""
        from PySide6.QtCore import QPointF as _QP
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QGraphicsSceneMouseEvent as _E

        from app.voucher_edit_window import TOOL_TEXT, VoucherEditWindow

        win = VoucherEditWindow(order_no="bgd1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        win.set_tool(TOOL_TEXT)
        win._scene.mousePressEvent(self._mk_scene_event(
            _E.Type.GraphicsSceneMousePress, _QP(50, 50), Qt.MouseButton.LeftButton))
        self.assertEqual(self._bg_count(win), before)

    def test_text_drag_complete_keeps_background(self) -> None:
        """テキスト挿入完了後も背景が残る（要件1・2）。"""
        from PySide6.QtCore import QPointF as _QP

        from app.voucher_edit_window import TOOL_TEXT, VoucherEditWindow

        win = VoucherEditWindow(order_no="bgd2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        self._drag(win, TOOL_TEXT, _QP(40, 40), _QP(160, 80))
        self.assertEqual(self._bg_count(win), before)

    def test_line_drag_keeps_background(self) -> None:
        """線ドラッグ中・完了後も背景が残る（要件1・2）。"""
        from PySide6.QtCore import QPointF as _QP
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QGraphicsSceneMouseEvent as _E

        from app.voucher_edit_window import TOOL_LINE, VoucherEditWindow

        win = VoucherEditWindow(order_no="bgd3", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        self._drag(win, TOOL_LINE, _QP(20, 20), _QP(120, 90), release=False)
        self.assertEqual(self._bg_count(win), before)  # ドラッグ中
        win._scene.mouseReleaseEvent(self._mk_scene_event(
            _E.Type.GraphicsSceneMouseRelease, _QP(120, 90),
            Qt.MouseButton.LeftButton))
        self.assertEqual(self._bg_count(win), before)  # 完了後

    def test_rect_drag_keeps_background(self) -> None:
        """四角ドラッグ中・完了後も背景が残る（要件1・2）。"""
        from PySide6.QtCore import QPointF as _QP

        from app.voucher_edit_window import TOOL_RECT, VoucherEditWindow

        win = VoucherEditWindow(order_no="bgd4", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        self._drag(win, TOOL_RECT, _QP(30, 30), _QP(130, 90))
        self.assertEqual(self._bg_count(win), before)

    def test_ellipse_drag_keeps_background(self) -> None:
        """丸ドラッグ中・完了後も背景が残る（要件1・2）。"""
        from PySide6.QtCore import QPointF as _QP

        from app.voucher_edit_window import TOOL_ELLIPSE, VoucherEditWindow

        win = VoucherEditWindow(order_no="bgd5", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        self._drag(win, TOOL_ELLIPSE, _QP(30, 30), _QP(110, 110))
        self.assertEqual(self._bg_count(win), before)

    def test_temp_preview_not_saved_during_drag(self) -> None:
        """ドラッグ中の一時アイテムは保存対象外（_IS_PREVIEW: 要件11）。"""
        from PySide6.QtCore import QPointF as _QP

        from app.voucher_edit_window import TOOL_RECT, VoucherEditWindow

        win = VoucherEditWindow(order_no="tp1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self._drag(win, TOOL_RECT, _QP(40, 40), _QP(120, 90), release=False)
        temp = win._scene._temp_item
        self.assertIsNotNone(temp)
        self.assertTrue(getattr(temp, "_IS_PREVIEW", False))
        # 一時アイテムは編集レイヤー・保存対象に含まれない。
        self.assertEqual(len(win.edit_items()), 0)
        self.assertEqual(len(win.serialize_objects()), 0)

    def test_ensure_background_visible_only_rebuilds_background(self) -> None:
        """ensure_background_visible は背景だけ復旧し編集オブジェクトを消さない（要件4・5）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="eb1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(20.0, 20.0), text="残す", font_size=12.0)
        edit_before = win.serialize_objects()
        for it in win.background_items():
            win._scene.removeItem(it)
        win.ensure_background_visible()
        self.assertGreaterEqual(self._bg_count(win), 1)
        self.assertEqual(win.serialize_objects(), edit_before)

    # ── 単一選択（要件6・7・10）────────────────────────────────────────────────
    def test_auto_create_results_in_single_selection(self) -> None:
        """空テキストを連続作成しても選択は最後の1つだけ（全選択化しない: 要件6・10）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ss1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        a = win.add_text_at(QPointF(10.0, 10.0), text="")
        b = win.add_text_at(QPointF(80.0, 80.0), text="")
        selected = win._scene.selectedItems()
        self.assertEqual(len(selected), 1)
        self.assertIn(b, selected)
        self.assertNotIn(a, selected)

    def test_select_only_clears_previous_selection(self) -> None:
        """_select_only は既存選択を解除して1つだけ選択する（要件6）。"""
        from PySide6.QtCore import QRectF

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ss2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        a = win.add_text_at(QPointF(10.0, 10.0), text="a")
        b = win.add_rect(QRectF(40.0, 40.0, 30.0, 20.0), text="b")
        a.setSelected(True)
        b.setSelected(True)
        self.assertEqual(len(win._scene.selectedItems()), 2)
        win._select_only(a)
        selected = win._scene.selectedItems()
        self.assertEqual(selected, [a])

    def test_click_selects_single_object(self) -> None:
        """選択ツールで通常クリックするとそのオブジェクトだけ選択される（要件6）。"""
        from PySide6.QtCore import QPointF as _QP
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtWidgets import QGraphicsSceneMouseEvent as _E

        from app.voucher_edit_window import TOOL_SELECT, VoucherEditWindow

        win = VoucherEditWindow(order_no="ck1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        a = win.add_rect(QRectF(20.0, 20.0, 40.0, 30.0), text="a")
        b = win.add_rect(QRectF(120.0, 120.0, 40.0, 30.0), text="b")
        win.set_tool(TOOL_SELECT)
        win._scene.clearSelection()
        scene = win._scene

        def _click(pos, mods=Qt.KeyboardModifier.NoModifier):
            scene.mousePressEvent(self._mk_scene_event(
                _E.Type.GraphicsSceneMousePress, pos, Qt.MouseButton.LeftButton, mods))
            scene.mouseReleaseEvent(self._mk_scene_event(
                _E.Type.GraphicsSceneMouseRelease, pos, Qt.MouseButton.LeftButton, mods))

        _click(_QP(40, 35))
        self.assertEqual(win._scene.selectedItems(), [a])
        # 別オブジェクトクリックで前の選択は解除
        _click(_QP(140, 135))
        self.assertEqual(win._scene.selectedItems(), [b])
        # Ctrl+クリックで複数選択
        _click(_QP(40, 35), Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(set(win._scene.selectedItems()), {a, b})
        # 空白クリックで選択解除
        _click(_QP(400, 400))
        self.assertEqual(win._scene.selectedItems(), [])

    # ── Esc（要件8）────────────────────────────────────────────────────────────
    def test_escape_clears_selection(self) -> None:
        """Escで選択中オブジェクトが全解除される（要件8）。"""
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QKeyEvent

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="esc2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10.0, 10.0), text="a")
        win.add_rect(QRectF(40.0, 40.0, 30.0, 20.0), text="b")
        win.select_all()
        self.assertEqual(len(win._scene.selectedItems()), 2)
        win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                    Qt.KeyboardModifier.NoModifier))
        self.assertEqual(win._scene.selectedItems(), [])

    def test_escape_keeps_background(self) -> None:
        """Escで背景・編集オブジェクトは消えない（要件8）。"""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="esc3", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = self._bg_count(win)
        win.add_text_at(QPointF(10.0, 10.0), text="残る")
        win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                    Qt.KeyboardModifier.NoModifier))
        self.assertEqual(self._bg_count(win), before)
        self.assertEqual(len(win.serialize_objects()), 1)

    def test_escape_cancels_temp_item(self) -> None:
        """Escで作成中の一時オブジェクトがキャンセルされる（要件8）。"""
        from PySide6.QtCore import QPointF as _QP
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        from app.voucher_edit_window import TOOL_RECT, VoucherEditWindow

        win = VoucherEditWindow(order_no="esc4", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self._drag(win, TOOL_RECT, _QP(30, 30), _QP(100, 80), release=False)
        self.assertIsNotNone(win._scene._temp_item)
        win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                    Qt.KeyboardModifier.NoModifier))
        self.assertIsNone(win._scene._temp_item)
        self.assertEqual(len(win.serialize_objects()), 0)

    def test_ctrl_a_then_escape_clears_all(self) -> None:
        """Ctrl+A後にEscで全選択を解除できる（要件8・9）。"""
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QKeyEvent

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ca1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10.0, 10.0), text="a")
        win.add_rect(QRectF(40.0, 40.0, 30.0, 20.0), text="b")
        win.select_all()
        self.assertEqual(len(win._scene.selectedItems()), 2)
        win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                    Qt.KeyboardModifier.NoModifier))
        self.assertEqual(win._scene.selectedItems(), [])

    def test_background_items_list_reference_maintained(self) -> None:
        """背景リスト参照 self._background_items が保持される（要件3）。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="bl1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertTrue(hasattr(win, "_background_items"))
        self.assertGreaterEqual(len(win._background_items), 1)
        # scene 走査の結果とリストが一致する。
        self.assertEqual(set(win._background_items), set(win.background_items()))

    # ── 全画面 / 最大化表示・ツールバー（要件2-1・2-2・2-5・2-6・2-7）──────────────
    def test_toolbar_has_image_paste_fullscreen_actions(self) -> None:
        from PySide6.QtWidgets import QToolBar

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ts1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        actions = [a.text() for tb in win.findChildren(QToolBar) for a in tb.actions()]
        for label in ("画像挿入", "貼り付け", "全画面", "保存して閉じる"):
            self.assertIn(label, actions)

    def test_fullscreen_toggle_switches_state(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ts2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.showMaximized()
        self.assertFalse(win.isFullScreen())
        win.toggle_fullscreen()
        self.assertTrue(win.isFullScreen())
        self.assertEqual(win._fullscreen_action.text(), "全画面解除")
        win.toggle_fullscreen()
        self.assertFalse(win.isFullScreen())
        self.assertEqual(win._fullscreen_action.text(), "全画面")

    def test_escape_exits_fullscreen(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ts3", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.enter_fullscreen()
        self.assertTrue(win.isFullScreen())
        win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                    Qt.KeyboardModifier.NoModifier))
        self.assertFalse(win.isFullScreen())

    def test_delete_button_is_danger_colored(self) -> None:
        from PySide6.QtWidgets import QToolBar, QToolButton

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ts4", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        bar = win.findChildren(QToolBar)[0]
        names = {b.objectName() for b in bar.findChildren(QToolButton)}
        self.assertIn("dangerButton", names)
        self.assertIn("successButton", names)
        # ツールバーの stylesheet に警告色・安全色・余白指定がある。
        style = bar.styleSheet()
        self.assertIn("#c62828", style)
        self.assertIn("#0b7a3b", style)
        self.assertIn("padding-left", style)

    # ── 座標マーカー削除・フィット・dirty・ボタン枠線（要件1〜4・8）─────────────
    def test_coordinate_marker_button_removed(self) -> None:
        from PySide6.QtWidgets import QToolBar

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="cm1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        actions = [a.text() for tb in win.findChildren(QToolBar) for a in tb.actions()]
        self.assertNotIn("座標マーカー", actions)
        # 内部関数はテスト用に残る。
        self.assertTrue(hasattr(win, "add_debug_markers"))

    def test_toolbar_buttons_have_border_radius_style(self) -> None:
        from PySide6.QtWidgets import QToolBar

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="cm2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        style = win.findChildren(QToolBar)[0].styleSheet()
        self.assertIn("border", style)
        self.assertIn("border-radius", style)
        self.assertIn(":checked", style)

    def test_show_event_fits_page_to_view(self) -> None:
        from PySide6.QtGui import QShowEvent

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fit1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        with mock.patch.object(win, "fit_page_to_view") as fit:
            win.showEvent(QShowEvent())
        fit.assert_called()

    def test_resize_event_refits_page(self) -> None:
        from PySide6.QtGui import QResizeEvent
        from PySide6.QtCore import QSize

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fit2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        with mock.patch.object(win, "fit_page_to_view") as fit:
            win.resizeEvent(QResizeEvent(QSize(800, 600), QSize(400, 300)))
        fit.assert_called()

    def test_fit_page_to_view_calls_fitinview(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fit3", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        with mock.patch.object(win._view, "fitInView") as fit_in_view:
            win.fit_page_to_view()
        fit_in_view.assert_called_once()

    def test_initial_state_is_not_dirty(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="d0", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertFalse(win.is_dirty())

    def test_adding_object_marks_dirty(self) -> None:
        from PySide6.QtCore import QPointF

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="d1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(100.0, 100.0), text="メモ")
        win.commit_history()
        self.assertTrue(win.is_dirty())

    def test_moving_object_marks_dirty(self) -> None:
        from PySide6.QtCore import QPointF

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="d2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_at(QPointF(100.0, 100.0), text="メモ")
        win.commit_history()
        win.mark_saved()
        item.setPos(220.0, 220.0)
        win.commit_history()
        self.assertTrue(win.is_dirty())

    def test_resizing_object_marks_dirty(self) -> None:
        from PySide6.QtCore import QRectF

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="d3", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        rect = win.add_rect(QRectF(50.0, 50.0, 80.0, 60.0))
        win.commit_history()
        win.mark_saved()
        rect.setRect(QRectF(50.0, 50.0, 160.0, 120.0))
        win.commit_history()
        self.assertTrue(win.is_dirty())

    def test_save_clears_dirty(self) -> None:
        from PySide6.QtCore import QPointF

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="d4", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(100.0, 100.0), text="メモ")
        win.commit_history()
        self.assertTrue(win.is_dirty())
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        self.assertFalse(win.is_dirty())

    def test_close_without_changes_does_not_prompt(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="c1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertFalse(win.is_dirty())
        with mock.patch.object(win, "_prompt_unsaved_changes") as prompt:
            win.close()
        prompt.assert_not_called()

    def test_close_with_changes_prompts(self) -> None:
        from PySide6.QtCore import QPointF

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="c2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(100.0, 100.0), text="メモ")
        win.commit_history()
        self.assertTrue(win.is_dirty())
        with mock.patch.object(win, "_prompt_unsaved_changes", return_value="discard") as prompt:
            win.close()
        prompt.assert_called_once()

    def test_close_cancel_keeps_window_open(self) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QCloseEvent

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="c3", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(100.0, 100.0), text="メモ")
        win.commit_history()
        event = QCloseEvent()
        with mock.patch.object(win, "_prompt_unsaved_changes", return_value="cancel"):
            win.closeEvent(event)
        self.assertFalse(event.isAccepted())

    def test_close_save_choice_persists(self) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QCloseEvent

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="c4", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(100.0, 100.0), text="メモ")
        win.commit_history()
        event = QCloseEvent()
        with mock.patch.object(win, "_prompt_unsaved_changes", return_value="save"), \
                mock.patch.object(win, "_persist", return_value=True) as persist:
            win.closeEvent(event)
        persist.assert_called_once()
        self.assertTrue(event.isAccepted())


if __name__ == "__main__":
    unittest.main()
