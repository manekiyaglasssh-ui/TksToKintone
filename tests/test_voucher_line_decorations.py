"""矢印・両矢印・二重線ツールのテスト。

- ツールバーへのボタン追加と並び順・ハイライト
- 各線種の作成・選択・移動・削除・コピー貼り付け・Undo/Redo
- 保存→再読み込みの往復（旧「線」データ互換を含む）
- PDF出力時の描画（矢じり線分・二重平行線）
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication, QToolBar

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


class TestLineDecorationsGeometry(unittest.TestCase):
    """座標系非依存の幾何計算（line_decorations）の単体テスト。"""

    def test_normalize_line_type_defaults_to_line(self) -> None:
        from app.line_decorations import normalize_line_type

        self.assertEqual(normalize_line_type(None), "line")
        self.assertEqual(normalize_line_type(""), "line")
        self.assertEqual(normalize_line_type("unknown"), "line")
        self.assertEqual(normalize_line_type("arrow"), "arrow")
        self.assertEqual(normalize_line_type("double_arrow"), "double_arrow")
        self.assertEqual(normalize_line_type("double_line"), "double_line")

    def test_line_segments_counts_per_type(self) -> None:
        from app.line_decorations import line_segments

        # 直線=1本、矢印=本体+矢じり2、両矢印=本体+矢じり4、二重線=平行2本。
        self.assertEqual(len(line_segments("line", 0, 0, 100, 0)), 1)
        self.assertEqual(len(line_segments("arrow", 0, 0, 100, 0)), 3)
        self.assertEqual(len(line_segments("double_arrow", 0, 0, 100, 0)), 5)
        self.assertEqual(len(line_segments("double_line", 0, 0, 100, 0)), 2)

    def test_arrowhead_points_back_to_tip(self) -> None:
        from app.line_decorations import arrowhead_segments

        segs = arrowhead_segments(0.0, 0.0, 100.0, 0.0)
        self.assertEqual(len(segs), 2)
        for sx1, sy1, sx2, sy2 in segs:
            # 各矢じり線分は終点(100,0)へ向かう。
            self.assertAlmostEqual(sx2, 100.0)
            self.assertAlmostEqual(sy2, 0.0)
            # 羽根の根元は終点より手前（x<100）。
            self.assertLess(sx1, 100.0)

    def test_double_line_segments_are_parallel_and_offset(self) -> None:
        from app.line_decorations import DOUBLE_LINE_GAP, double_line_segments

        segs = double_line_segments(0.0, 0.0, 100.0, 0.0)
        self.assertEqual(len(segs), 2)
        # 水平線なので2本は y が ±gap/2 にずれる。
        ys = sorted({round(s[1], 3) for s in segs})
        self.assertAlmostEqual(ys[1] - ys[0], DOUBLE_LINE_GAP, places=3)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestLineDecorationsWindow(unittest.TestCase):
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

    def _new_window(self, order_no: str = "ld1"):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no=order_no, background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def test_toolbar_buttons_order_after_line(self) -> None:
        """要件1/5: 図形メニュー内に 線/矢印/両矢印/二重線 がこの順で並ぶ。"""
        win = self._new_window()
        labels = [a.text() for a in win._shape_menu.actions()]
        idx = labels.index("線")
        self.assertEqual(labels[idx:idx + 4], ["線", "矢印", "両矢印", "二重線"])

    def test_tool_buttons_select_modes(self) -> None:
        """要件2-4: 各ボタンで対応する描画モードになる。"""
        from app.voucher_edit_window import (
            TOOL_ARROW, TOOL_DOUBLE_ARROW, TOOL_DOUBLE_LINE,
        )
        win = self._new_window()
        for tool in (TOOL_ARROW, TOOL_DOUBLE_ARROW, TOOL_DOUBLE_LINE):
            win.set_tool(tool)
            self.assertEqual(win.current_tool, tool)

    def test_selected_tool_is_highlighted(self) -> None:
        """要件5: 選択中ボタンだけがチェック＝ハイライトされる。"""
        from app.voucher_edit_window import TOOL_ARROW, TOOL_LINE
        win = self._new_window()
        win.set_tool(TOOL_ARROW)
        self.assertTrue(win._tool_actions[TOOL_ARROW].isChecked())
        self.assertTrue(win._tool_actions[TOOL_ARROW].font().bold())
        self.assertFalse(win._tool_actions[TOOL_LINE].isChecked())

    def test_tool_buttons_have_edit_tool_property_for_themes(self) -> None:
        """要件16/5: 図形ボタンが editToolButton property を持ち、各図形はチェック可能。"""
        from app.voucher_edit_window import (
            TOOL_ARROW, TOOL_DOUBLE_ARROW, TOOL_DOUBLE_LINE,
        )
        win = self._new_window()
        # 図形ボタン本体は editToolButton property を持つ（両テーマでハイライト可）。
        self.assertTrue(win._shape_tool_button.property("editToolButton"))
        # 各図形アクションはチェック可能（選択中が分かる）。
        for tool in (TOOL_ARROW, TOOL_DOUBLE_ARROW, TOOL_DOUBLE_LINE):
            action = win._tool_actions[tool]
            self.assertTrue(action.isCheckable())

    def test_create_select_move_delete_each_type(self) -> None:
        """要件6-8: 矢印・両矢印・二重線を作成→選択→移動→削除できる。"""
        for line_type in ("arrow", "double_arrow", "double_line"):
            with self.subTest(line_type=line_type):
                win = self._new_window(order_no=f"cmd_{line_type}")
                item = win.add_line(QPointF(10.0, 10.0), QPointF(80.0, 60.0),
                                    line_type=line_type)
                self.assertEqual(item.line_type, line_type)
                obj = item.serialize_edit_object()
                self.assertEqual(obj["type"], "line")
                self.assertEqual(obj["line_type"], line_type)
                # 移動
                item.moveBy(15.0, 25.0)
                moved = item.serialize_edit_object()
                self.assertAlmostEqual(moved["x1"], 25.0)
                self.assertAlmostEqual(moved["y1"], 35.0)
                # 選択→削除
                item.setSelected(True)
                win.delete_selected()
                self.assertEqual(len(win.serialize_objects()), 0)

    def test_save_and_reload_roundtrip(self) -> None:
        """要件9: 矢印・両矢印・二重線を保存して再読み込みできる。"""
        win = self._new_window(order_no="rt1")
        win.add_line(QPointF(10.0, 10.0), QPointF(80.0, 10.0), line_type="arrow")
        win.add_line(QPointF(10.0, 30.0), QPointF(80.0, 30.0),
                     line_type="double_arrow")
        win.add_line(QPointF(10.0, 50.0), QPointF(80.0, 50.0),
                     line_type="double_line")
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()

        win2 = self._new_window(order_no="rt1")
        reloaded = sorted((o["line_type"] for o in win2.serialize_objects()))
        self.assertEqual(reloaded, ["arrow", "double_arrow", "double_line"])

    def test_legacy_line_without_line_type_loads_as_line(self) -> None:
        """要件10: line_type の無い旧「線」データを今までどおり読み込める。"""
        from app.voucher_edit_objects import save_edit_objects
        from app.path_utils import get_voucher_edit_objects_dir

        # line_type を持たない旧形式を直接書き出す。
        save_edit_objects("legacy1", [
            {"id": "old", "type": "line", "x1": 5.0, "y1": 5.0,
             "x2": 50.0, "y2": 5.0,
             "coordinate_origin": "scene_top_left",
             "geometry_basis": "object_geometry_v2"},
        ], base_dir=get_voucher_edit_objects_dir())

        win = self._new_window(order_no="legacy1")
        objs = win.serialize_objects()
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0]["type"], "line")
        self.assertEqual(objs[0]["line_type"], "line")

    def test_copy_paste_duplicates_line_type(self) -> None:
        """要件14: Ctrl+C / Ctrl+V 相当で線種を保ったまま複製できる。"""
        win = self._new_window(order_no="cp1")
        item = win.add_line(QPointF(10.0, 10.0), QPointF(60.0, 10.0),
                            line_type="double_arrow")
        item.setSelected(True)
        self.assertTrue(win.copy_selected_objects())
        self.assertTrue(win.paste_copied_objects())
        types = [o["line_type"] for o in win.serialize_objects()]
        self.assertEqual(types, ["double_arrow", "double_arrow"])

    def test_undo_redo_for_new_line_types(self) -> None:
        """要件15: Undo/Redo が効く。"""
        win = self._new_window(order_no="ur1")
        win.add_line(QPointF(10.0, 10.0), QPointF(60.0, 10.0), line_type="arrow")
        win.commit_history()
        self.assertEqual(len(win.serialize_objects()), 1)
        win.undo()
        self.assertEqual(len(win.serialize_objects()), 0)
        win.redo()
        objs = win.serialize_objects()
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0]["line_type"], "arrow")


class TestLineDecorationsPdf(unittest.TestCase):
    """PDF出力（_draw_edit_objects）が線種に応じて描く本数を検証する。"""

    def _collect_lines(self, obj) -> list:
        from app import voucher_service

        calls: list = []

        class _FakeCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def setLineWidth(self, *a): pass
            def setFont(self, *a): pass
            def line(self, *a): calls.append(a)

        voucher_service._draw_edit_objects(_FakeCanvas(), [obj])
        return calls

    def test_pdf_arrow_draws_body_and_head(self) -> None:
        """要件11: 矢印は本体1+矢じり2の計3線分。"""
        lines = self._collect_lines(
            {"id": "a", "type": "line", "line_type": "arrow",
             "x1": 1.0, "y1": 2.0, "x2": 50.0, "y2": 2.0})
        self.assertEqual(len(lines), 3)

    def test_pdf_double_arrow_draws_both_heads(self) -> None:
        """要件12: 両矢印は本体1+矢じり4の計5線分。"""
        lines = self._collect_lines(
            {"id": "b", "type": "line", "line_type": "double_arrow",
             "x1": 1.0, "y1": 2.0, "x2": 50.0, "y2": 2.0})
        self.assertEqual(len(lines), 5)

    def test_pdf_double_line_draws_two_parallels(self) -> None:
        """要件13: 二重線は平行2線分。"""
        lines = self._collect_lines(
            {"id": "c", "type": "line", "line_type": "double_line",
             "x1": 1.0, "y1": 2.0, "x2": 50.0, "y2": 2.0})
        self.assertEqual(len(lines), 2)

    def test_pdf_plain_line_unchanged(self) -> None:
        """要件10: 旧「線」は従来どおり1本だけ描く。"""
        from app import voucher_service

        lines = self._collect_lines(
            {"id": "d", "type": "line", "x1": 1.0, "y1": 2.0,
             "x2": 3.0, "y2": 4.0})
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            lines[0],
            (1.0, voucher_service.PAGE_H - 2.0, 3.0, voucher_service.PAGE_H - 4.0),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
