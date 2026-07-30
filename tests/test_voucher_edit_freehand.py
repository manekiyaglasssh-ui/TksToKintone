"""指図書編集のタブレット手書きペン（freehand）の動的テスト。

QApplication を offscreen で起動し、ペン中心UI・freehand オブジェクトの作成/保存/
読込・消しゴム・Undo/Redo・通常モードでの表示・PDF出力を検証する。
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditFreehand(unittest.TestCase):
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

        win = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def _freehand_items(self, win):
        return [it for it in win.edit_items()
                if it.serialize_edit_object().get("type") == "freehand"]

    def test_initial_tool_is_pen_in_tablet_mode(self) -> None:
        """1. タブレット編集モード開始時、初期ツールがペンになる。"""
        from app.voucher_edit_window import TOOL_PEN

        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        self.assertEqual(win.current_tool, TOOL_PEN)
        win.exit_tablet_mode()

    def test_tablet_toolbar_shows_pen_buttons(self) -> None:
        """2. タブレット用ツールバーに「手書き」「消しゴム」「太さ」「保存」「タブレット終了」が表示される。"""
        win = self._make_window()
        # 上部メニューは1段。すべてのボタンが1本のツールバーに並ぶ。
        labels = [a.text() for a in win._tablet_toolbar.actions()]
        self.assertIn("手書き", labels)
        self.assertIn("消しゴム", labels)
        self.assertTrue(any(t.startswith("太さ") for t in labels))
        self.assertIn("保存", labels)
        self.assertIn("タブレット終了", labels)

    def test_tablet_toolbar_hides_shape_tools(self) -> None:
        """3. タブレット用ツールバーでは、テキスト・線・矢印・四角・丸が基本表示されない。"""
        win = self._make_window()
        labels = [a.text() for a in win._tablet_toolbar.actions()]
        for hidden in ("テキスト", "線", "矢印", "両矢印", "二重線", "四角", "丸", "画像"):
            self.assertNotIn(hidden, labels)

    def test_pen_drag_creates_freehand(self) -> None:
        """4. ペン操作で freehand オブジェクトが作成される。"""
        from app.voucher_edit_window import TOOL_PEN

        win = self._make_window()
        win.set_tool(TOOL_PEN)
        scene = win._scene
        scene.begin_freehand(QPointF(10, 10))
        scene._freehand_item.add_point(QPointF(30, 20))
        scene._freehand_item.add_point(QPointF(50, 40))
        scene.end_freehand()
        items = self._freehand_items(win)
        self.assertEqual(len(items), 1)

    def test_freehand_serializes_points_width_color(self) -> None:
        """5. freehand オブジェクトに points / pen_width / color が保存される。"""
        win = self._make_window()
        win.current_pen_width = 4.0
        win.current_pen_color = "#d32f2f"
        win.add_freehand([(5, 5), (10, 10), (20, 15)])
        obj = self._freehand_items(win)[0].serialize_edit_object()
        self.assertEqual(obj["type"], "freehand")
        self.assertEqual(len(obj["points"]), 3)
        self.assertEqual(obj["pen_width"], 4.0)
        self.assertEqual(obj["stroke_color"].lower(), "#d32f2f")

    def test_freehand_save_and_reload(self) -> None:
        """6. freehand オブジェクトを保存して再読み込みできる。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = self._make_window()
        win.add_freehand([(10, 10), (40, 30), (70, 60)])
        self.assertTrue(win._persist())
        win2 = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        reloaded = self._freehand_items(win2)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(len(reloaded[0].points()), 3)

    def test_old_template_without_freehand_loads(self) -> None:
        """7. 古いテンプレート（freehand なし）を読み込んでもエラーにならない。"""
        from app.voucher_edit_objects import save_edit_objects
        from app.voucher_edit_window import VoucherEditWindow

        old_objects = [
            {"id": "a1", "type": "line", "x1": 10, "y1": 10, "x2": 50, "y2": 50,
             "coordinate_origin": "scene_top_left", "line_width": 1.0,
             "stroke_color": "#000000", "target_vouchers": ["03"]},
        ]
        save_edit_objects("5218869", old_objects)
        win = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        # 線オブジェクトが復元され、freehand は存在しない。
        types = [it.serialize_edit_object().get("type") for it in win.edit_items()]
        self.assertIn("line", types)
        self.assertNotIn("freehand", types)

    def test_freehand_drawn_in_pdf(self) -> None:
        """8. freehand オブジェクトがPDF出力に反映される。"""
        from app import voucher_service

        objects = [
            {"id": "f1", "type": "freehand",
             "points": [[10, 10], [40, 40], [80, 30]],
             "coordinate_origin": "scene_top_left",
             "pen_width": 3.0, "stroke_color": "#000000",
             "target_vouchers": ["03"]},
        ]
        captured = {"path": False}

        class _FakePath:
            def moveTo(self, *a):
                pass

            def lineTo(self, *a):
                pass

        class _FakeCanvas:
            def saveState(self):
                pass

            def restoreState(self):
                pass

            def setStrokeColorRGB(self, *a):
                pass

            def setFillColorRGB(self, *a):
                pass

            def setLineWidth(self, *a):
                pass

            def setLineCap(self, *a):
                pass

            def setLineJoin(self, *a):
                pass

            def beginPath(self):
                return _FakePath()

            def drawPath(self, *a, **k):
                captured["path"] = True

        voucher_service._draw_edit_objects(_FakeCanvas(), objects)
        self.assertTrue(captured["path"])

    def test_undo_removes_last_stroke(self) -> None:
        """9. Undoで直前の1ストロークが消える。"""
        win = self._make_window()
        win.add_freehand([(1, 1), (2, 2), (3, 3)])
        win.commit_history()
        win.add_freehand([(10, 10), (20, 20), (30, 30)])
        win.commit_history()
        self.assertEqual(len(self._freehand_items(win)), 2)
        win.undo()
        self.assertEqual(len(self._freehand_items(win)), 1)

    def test_redo_restores_stroke(self) -> None:
        """10. Redoで直前の1ストロークが戻る。"""
        win = self._make_window()
        win.add_freehand([(1, 1), (2, 2), (3, 3)])
        win.commit_history()
        win.add_freehand([(10, 10), (20, 20), (30, 30)])
        win.commit_history()
        win.undo()
        self.assertEqual(len(self._freehand_items(win)), 1)
        win.redo()
        self.assertEqual(len(self._freehand_items(win)), 2)

    def test_eraser_removes_freehand(self) -> None:
        """11. 消しゴムでfreehandオブジェクトを削除できる。"""
        win = self._make_window()
        win.add_freehand([(100, 100), (110, 110), (120, 120)])
        self.assertEqual(len(self._freehand_items(win)), 1)
        # ストロークの点に近い位置をなぞる。
        removed = win.erase_freehand_at(QPointF(105, 105))
        self.assertTrue(removed)
        self.assertEqual(len(self._freehand_items(win)), 0)

    def test_freehand_visible_in_normal_mode(self) -> None:
        """12. 通常モードに戻ってもfreehandが表示される。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        win.add_freehand([(10, 10), (30, 30), (50, 20)])
        win.exit_tablet_mode()
        self.assertFalse(win.tablet_mode)
        self.assertEqual(len(self._freehand_items(win)), 1)

    def test_normal_tools_still_work(self) -> None:
        """13. 通常モードの既存ツール（テキスト・線・四角等）は今までどおり使える。"""
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import TOOL_RECT

        win = self._make_window()
        # 通常ツールバーには従来どおりのツールがある（図形は「図形」メニューへ統合・要件5）。
        labels = [a.text() for a in win._main_toolbar.actions()]
        for t in ("選択", "テキスト"):
            self.assertIn(t, labels)
        shape_labels = [a.text() for a in win._shape_menu.actions()]
        for t in ("線", "四角", "丸"):
            self.assertIn(t, shape_labels)
        # 既存の図形追加が動作する。
        win.set_tool(TOOL_RECT)
        win.add_rect(QRectF(10, 10, 40, 30))
        types = [it.serialize_edit_object().get("type") for it in win.edit_items()]
        self.assertIn("rectangle", types)

    def test_cycle_pen_width_and_color(self) -> None:
        """補助: 太さ・色の切替で current_pen_width / current_pen_color が変わる。"""
        from app.voucher_edit_window import DEFAULT_PEN_WIDTH

        win = self._make_window()
        self.assertEqual(win.current_pen_width, DEFAULT_PEN_WIDTH)
        before = win.current_pen_width
        win.cycle_pen_width()
        self.assertNotEqual(win.current_pen_width, before)
        c_before = win.current_pen_color
        win.cycle_pen_color()
        self.assertNotEqual(win.current_pen_color, c_before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
