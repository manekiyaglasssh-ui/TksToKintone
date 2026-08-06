"""指図書編集画面のリサイズハンドル操作と Ctrl+V 経路のテスト（不具合1・2）。"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
    from PySide6.QtGui import QImage, QKeyEvent, QTransform
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False


def _png_bytes(width: int = 12, height: int = 8, color: int = 0xFF3366CC) -> bytes:
    from app.voucher_edit_window import qimage_to_png_bytes

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    return qimage_to_png_bytes(image)


def _set_clipboard_image(color: int = 0xFF00AA55) -> None:
    image = QImage(10, 10, QImage.Format.Format_ARGB32)
    image.fill(color)
    QApplication.clipboard().setImage(image)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestResizeHandles(unittest.TestCase):
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

        win = VoucherEditWindow(order_no="resize-1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def _select_resize_handle(self, win, item):
        """item を選択して付与された右下リサイズハンドルを返す。"""
        from app.voucher_edit_window import _ResizeHandle

        win._scene.clearSelection()
        item.setSelected(True)
        win._on_selection_changed()
        handles = [h for h in win._handles if isinstance(h, _ResizeHandle)]
        self.assertEqual(len(handles), 8)
        return [h for h in handles if h._position == "bottom_right"][0]

    # ── 各オブジェクトがハンドルでリサイズできる ────────────────────────────
    def test_image_resized_by_dragging_handle(self) -> None:
        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(20.0, 20.0, 50.0, 40.0))
        handle = self._select_resize_handle(win, item)
        # 右下ハンドルをドラッグ（setPos が itemChange→_resize_target を発火）。
        handle.setPos(QPointF(220.0, 180.0))
        obj = win.serialize_objects()[0]
        self.assertGreater(obj["width"], 50.0)
        self.assertGreater(obj["height"], 40.0)

    def test_text_resized_by_dragging_handle(self) -> None:
        win = self._make_window()
        item = win.add_text_rect(QRectF(20.0, 20.0, 80.0, 24.0),
                                 text="T", auto_edit=False)
        handle = self._select_resize_handle(win, item)
        handle.setPos(QPointF(260.0, 200.0))
        obj = win.serialize_objects()[0]
        self.assertGreater(obj["width"], 80.0)
        self.assertGreater(obj["height"], 24.0)
        self.assertGreater(obj["font_size"], 4.0)

    def test_scene_handle_press_keeps_same_handle_alive_and_scales_font_size(self) -> None:
        """実イベント経路でハンドルを再生成せず、開始時基準で文字も拡大する。"""
        from PySide6.QtWidgets import QGraphicsSceneMouseEvent

        win = self._make_window()
        item = win.add_text_rect(
            QRectF(40.0, 50.0, 80.0, 30.0), text="123",
            font_size=36.0, auto_edit=False, auto_fit=False)
        handle = self._select_resize_handle(win, item)
        before = item.font_size
        press = QGraphicsSceneMouseEvent(
            QGraphicsSceneMouseEvent.Type.GraphicsSceneMousePress)
        press.setScenePos(handle.pos())
        press.setButton(Qt.MouseButton.LeftButton)
        press.setButtons(Qt.MouseButton.LeftButton)
        win._scene.mousePressEvent(press)

        self.assertIs(handle.scene(), win._scene)
        self.assertIn(handle, win._handles)
        self.assertIs(handle._target, item)
        self.assertIs(handle.owner_item, item)
        self.assertIs(handle.source_item, item)

        handle.setPos(QPointF(240.0, 140.0))
        self.assertGreater(item.font_size, before)
        release = QGraphicsSceneMouseEvent(
            QGraphicsSceneMouseEvent.Type.GraphicsSceneMouseRelease)
        release.setScenePos(handle.pos())
        release.setButton(Qt.MouseButton.LeftButton)
        win._scene.mouseReleaseEvent(release)
        self.assertGreater(item.font_size, before)

    def test_text_resize_uses_start_snapshot_without_cumulative_error(self) -> None:
        win = self._make_window()
        item = win.add_text_rect(
            QRectF(40.0, 50.0, 80.0, 40.0), text="123",
            font_size=20.0, auto_edit=False, auto_fit=False)
        handle = self._select_resize_handle(win, item)
        start_size = item.font_size
        handle._resize_start_rect = QRectF(item.box_rect_scene())
        handle._font_size_before = start_size
        start_rect = handle._resize_start_rect
        handle._resize_target(QPointF(start_rect.right() * 2.0 - start_rect.left(),
                                      start_rect.bottom() * 2.0 - start_rect.top()))
        expanded_size = item.font_size
        handle._resize_target(start_rect.bottomRight())
        self.assertAlmostEqual(item.font_size, start_size * 1.0, delta=0.01)
        self.assertGreater(expanded_size, start_size)

    def test_handles_after_symbol_conversion_reference_new_text_item(self) -> None:
        """symbol_text昇格後の8ハンドルが、削除済みsymbolではなく新itemを指す。"""
        from app.voucher_edit_window import _EditSymbolTextItem, _EditTextItem

        win = self._make_window()
        original = win.add_text_rect(
            QRectF(40.0, 50.0, 100.0, 40.0), text="123",
            font_size=72.0, auto_edit=False)
        object_id = original.obj_id
        self.assertTrue(win.maybe_convert_text_item_to_symbol(original))
        symbol = win._edit_item_by_id(object_id)
        self.assertIsInstance(symbol, _EditSymbolTextItem)
        symbol.setSelected(True)
        self.assertTrue(win.begin_text_edit(symbol))
        editable = win._edit_item_by_id(object_id)
        self.assertIsInstance(editable, _EditTextItem)
        handles = [h for h in win._handles if hasattr(h, "_position")]
        self.assertEqual(len(handles), 8)
        self.assertTrue(all(h._target is editable for h in handles))
        self.assertTrue(all(h.owner_item is editable for h in handles))
        self.assertTrue(all(h.source_item is editable for h in handles))

    def test_rect_resized_by_dragging_handle(self) -> None:
        win = self._make_window()
        item = win.add_rect(QRectF(20.0, 20.0, 60.0, 40.0), text="r")
        handle = self._select_resize_handle(win, item)
        handle.setPos(QPointF(260.0, 220.0))
        obj = win.serialize_objects()[0]
        self.assertGreater(obj["width"], 60.0)
        self.assertGreater(obj["height"], 40.0)

    def test_ellipse_resized_by_dragging_handle(self) -> None:
        win = self._make_window()
        item = win.add_ellipse(QRectF(20.0, 20.0, 60.0, 40.0), text="e")
        handle = self._select_resize_handle(win, item)
        handle.setPos(QPointF(260.0, 220.0))
        obj = win.serialize_objects()[0]
        self.assertGreater(obj["width"], 60.0)
        self.assertGreater(obj["height"], 40.0)

    def test_line_endpoint_changed_by_dragging_handle(self) -> None:
        from app.voucher_edit_window import _LineEndHandle

        win = self._make_window()
        line = win.add_line(QPointF(30.0, 30.0), QPointF(90.0, 30.0))
        win._scene.clearSelection()
        line.setSelected(True)
        win._on_selection_changed()
        handles = [h for h in win._handles if isinstance(h, _LineEndHandle)]
        self.assertEqual(len(handles), 2)
        # p2 端点ハンドルを移動して終点を変える。
        p2_handle = [h for h in handles if h._which == "p2"][0]
        p2_handle.setPos(QPointF(150.0, 120.0))
        obj = win.serialize_objects()[0]
        self.assertAlmostEqual(obj["x2"], 150.0, delta=1.0)
        self.assertAlmostEqual(obj["y2"], 120.0, delta=1.0)

    # ── リサイズ完了で dirty/commit_history/refresh_handles ──────────────────
    def test_resize_marks_dirty(self) -> None:
        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(20.0, 20.0, 50.0, 40.0))
        win.commit_history()
        win.mark_saved()
        self.assertFalse(win.is_dirty())
        handle = self._select_resize_handle(win, item)
        handle.setPos(QPointF(220.0, 180.0))
        self._release(handle)
        self.assertTrue(win.is_dirty())

    def test_resize_commits_history(self) -> None:
        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(20.0, 20.0, 50.0, 40.0))
        win.commit_history()
        handle = self._select_resize_handle(win, item)
        handle.setPos(QPointF(220.0, 180.0))
        with mock.patch.object(win, "commit_history",
                               wraps=win.commit_history) as spy:
            self._release(handle)
        self.assertTrue(spy.called)

    def test_resize_is_undoable(self) -> None:
        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(20.0, 20.0, 50.0, 40.0))
        win.commit_history()
        small_w = win.serialize_objects()[0]["width"]
        handle = self._select_resize_handle(win, item)
        handle.setPos(QPointF(240.0, 200.0))
        self._release(handle)
        big_w = win.serialize_objects()[0]["width"]
        self.assertGreater(big_w, small_w)
        win.undo()
        self.assertAlmostEqual(win.serialize_objects()[0]["width"], small_w,
                               delta=1.0)

    def _release(self, handle) -> None:
        from PySide6.QtWidgets import QGraphicsSceneMouseEvent

        ev = QGraphicsSceneMouseEvent(
            QGraphicsSceneMouseEvent.Type.GraphicsSceneMouseRelease)
        ev.setButton(Qt.MouseButton.LeftButton)
        handle.mouseReleaseEvent(ev)

    # ── 背景はリサイズ対象外 ─────────────────────────────────────────────────
    def test_background_gets_no_resize_handle(self) -> None:
        win = self._make_window()
        # 背景アイテムを選択しようとしても serialize_edit_object を持たないため
        # ハンドルは付与されない。
        for bg in win.background_items():
            win._scene.clearSelection()
            bg.setSelected(True)
            win._on_selection_changed()
            self.assertEqual(win._handles, [])

    # ── ハンドルのクリック判定が画像本体より優先される（不具合1）──────────────
    def test_handle_shape_larger_than_visual(self) -> None:
        from app.voucher_edit_window import HANDLE_HIT_SIZE_PX, HANDLE_SIZE

        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(20.0, 20.0, 50.0, 40.0))
        handle = self._select_resize_handle(win, item)
        shape_rect = handle.shape().boundingRect()
        # クリック判定（shape）は見た目（rect）より広い。
        view_scale = abs(win._view.transform().m11()) or 1.0
        self.assertAlmostEqual(
            shape_rect.width() * view_scale, HANDLE_HIT_SIZE_PX, delta=0.01)
        self.assertGreater(shape_rect.width(), HANDLE_SIZE)

    def test_handle_hit_area_prioritized_over_image(self) -> None:
        """画像本体と重なる点・拡大判定領域とも、最前面のハンドルが拾われる。"""
        from PySide6.QtGui import QTransform

        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(20.0, 20.0, 50.0, 40.0))
        handle = self._select_resize_handle(win, item)
        scene = win._scene
        # 画像本体（〜70,60）と重なる点でもハンドルが最前面で拾われる。
        on_body = QPointF(68.0, 58.0)
        self.assertIs(scene.itemAt(on_body, QTransform()), handle)
        # 見た目の矩形外（拡大判定領域内）でもハンドルが拾われる。
        enlarged = QPointF(78.0, 60.0)
        self.assertIs(scene.itemAt(enlarged, QTransform()), handle)
        # scene 側のハンドル判定も拡大判定領域を拾う。
        self.assertTrue(scene._press_on_handle(enlarged))

    def test_handle_press_accepts_and_stops_target_move(self) -> None:
        """ハンドル押下で event.accept() され、対象の移動が止まる。"""
        from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(20.0, 20.0, 50.0, 40.0))
        handle = self._select_resize_handle(win, item)
        movable = QGraphicsItem.GraphicsItemFlag.ItemIsMovable
        # 通常移動も scene 共通 drag が担うため、item 標準移動は常時無効。
        self.assertFalse(bool(item.flags() & movable))
        ev = QGraphicsSceneMouseEvent(
            QGraphicsSceneMouseEvent.Type.GraphicsSceneMousePress)
        ev.setScenePos(handle.sceneBoundingRect().center())
        ev.setButton(Qt.MouseButton.LeftButton)
        handle.mousePressEvent(ev)
        self.assertTrue(ev.isAccepted())
        # リサイズ中は対象の移動が止まる（本体が動かない）。
        self.assertFalse(bool(item.flags() & movable))

    def test_handle_drag_does_not_move_target(self) -> None:
        """ハンドルドラッグ中は対象の位置が変わらず、サイズだけ変わる。"""
        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(20.0, 20.0, 50.0, 40.0))
        before_pos = (item.pos().x(), item.pos().y())
        before_w = win.serialize_objects()[0]["width"]
        handle = self._select_resize_handle(win, item)
        handle.setPos(QPointF(240.0, 200.0))
        after_pos = (item.pos().x(), item.pos().y())
        after_w = win.serialize_objects()[0]["width"]
        self.assertAlmostEqual(after_pos[0], before_pos[0], delta=0.01)
        self.assertAlmostEqual(after_pos[1], before_pos[1], delta=0.01)
        self.assertGreater(after_w, before_w)

    def test_clicking_handle_keeps_selection(self) -> None:
        """ハンドル押下で対象の選択が外れない（リサイズが途中で壊れない）。"""
        from PySide6.QtWidgets import QGraphicsSceneMouseEvent

        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(20.0, 20.0, 50.0, 40.0))
        handle = self._select_resize_handle(win, item)
        scene = win._scene
        handle_pos = handle.sceneBoundingRect().center()
        ev = QGraphicsSceneMouseEvent(
            QGraphicsSceneMouseEvent.Type.GraphicsSceneMousePress)
        ev.setScenePos(handle_pos)
        ev.setButton(Qt.MouseButton.LeftButton)
        scene.mousePressEvent(ev)
        # ハンドル押下後も対象は選択されたまま＝ハンドルが撤去されない。
        self.assertTrue(item.isSelected())
        self.assertTrue(scene._press_handle)

    def test_corner_handle_keeps_aspect_ratio(self) -> None:
        win = self._make_window()
        item = win.add_image(_png_bytes(width=20, height=10),
                             rect=QRectF(20.0, 20.0, 100.0, 50.0))
        handle = self._select_resize_handle(win, item)
        handle.setPos(QPointF(220.0, 90.0))
        obj = win.serialize_objects()[0]
        self.assertAlmostEqual(obj["width"] / obj["height"], 2.0, delta=0.01)

    def test_top_bottom_handles_change_only_height(self) -> None:
        from app.voucher_edit_window import _ResizeHandle

        win = self._make_window()
        item = win.add_rect(QRectF(20.0, 20.0, 100.0, 50.0), text="r")
        win._scene.clearSelection()
        item.setSelected(True)
        win._on_selection_changed()
        handle = [h for h in win._handles
                  if isinstance(h, _ResizeHandle) and h._position == "bottom"][0]
        handle.setPos(QPointF(70.0, 120.0))
        obj = win.serialize_objects()[0]
        self.assertAlmostEqual(obj["width"], 100.0, delta=0.01)
        self.assertGreater(obj["height"], 50.0)

    def test_left_right_handles_change_only_width(self) -> None:
        from app.voucher_edit_window import _ResizeHandle

        win = self._make_window()
        item = win.add_rect(QRectF(20.0, 20.0, 100.0, 50.0), text="r")
        win._scene.clearSelection()
        item.setSelected(True)
        win._on_selection_changed()
        handle = [h for h in win._handles
                  if isinstance(h, _ResizeHandle) and h._position == "right"][0]
        handle.setPos(QPointF(160.0, 45.0))
        obj = win.serialize_objects()[0]
        self.assertGreater(obj["width"], 100.0)
        self.assertAlmostEqual(obj["height"], 50.0, delta=0.01)

    def test_resize_does_not_go_below_minimum(self) -> None:
        from app.voucher_edit_window import MIN_OBJECT_WIDTH, _ResizeHandle

        win = self._make_window()
        item = win.add_rect(QRectF(20.0, 20.0, 100.0, 50.0), text="r")
        handle = _ResizeHandle(item, "right")
        handle._resize_target(QPointF(21.0, 45.0))
        obj = item.serialize_edit_object()
        self.assertGreaterEqual(obj["width"], MIN_OBJECT_WIDTH)

    def _viewport_drag_handle(self, win, handle, delta: QPoint,
                              offset: QPoint = QPoint()) -> None:
        win.show()
        win.set_tool("select")
        QApplication.processEvents()
        start = win._view.mapFromScene(handle.scenePos()) + offset
        end = start + delta
        QTest.mouseMove(win._view.viewport(), start)
        QApplication.processEvents()
        # offscreen plugin は viewport の native hover cursor を更新しないため、
        # 実mouseMove位置が解決するhandleの方向cursorを確認する。
        hovered = win._scene._resolve_handle(win._view.mapToScene(start))
        self.assertIs(hovered, handle)
        self.assertEqual(hovered.cursor().shape(), handle.cursor().shape())
        QTest.mousePress(win._view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, start)
        QTest.mouseMove(win._view.viewport(), end, 20)
        QTest.mouseRelease(win._view.viewport(), Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, end)
        QApplication.processEvents()

    def test_viewport_single_all_handles_resize_and_undo_redo(self) -> None:
        """viewport実イベントで8方向が移動DragStateを作らずリサイズする。"""
        from app.voucher_edit_window import _ResizeHandle

        directions = {
            "top_left": QPoint(-3, -3), "top": QPoint(0, -3),
            "top_right": QPoint(3, -3), "right": QPoint(3, 0),
            "bottom_right": QPoint(3, 3), "bottom": QPoint(0, 3),
            "bottom_left": QPoint(-3, 3), "left": QPoint(-3, 0),
        }
        for position, delta in directions.items():
            with self.subTest(position=position):
                win = self._make_window()
                item = win.add_rect(QRectF(80, 80, 100, 60), text="r")
                win.commit_history()
                self._select_resize_handle(win, item)
                handle = next(h for h in win._handles
                              if isinstance(h, _ResizeHandle) and h._position == position)
                before = dict(item.serialize_edit_object())
                self._viewport_drag_handle(win, handle, delta)
                after = dict(item.serialize_edit_object())
                self.assertNotEqual((before["x"], before["y"], before["width"], before["height"]),
                                    (after["x"], after["y"], after["width"], after["height"]))
                self.assertIsNone(win._scene._drag_state)
                self.assertIsNone(win._scene._resize_state)
                win.undo()
                restored = win.serialize_objects()[0]
                self.assertAlmostEqual(restored["width"], before["width"], delta=0.1)
                win.redo()
                redone = win.serialize_objects()[0]
                self.assertAlmostEqual(redone["width"], after["width"], delta=0.1)

    def test_viewport_small_symbol_handle_hit_at_dpi_scales(self) -> None:
        """+2相当の小さい文字を中心から2px外しても各DPI相当で掴める。"""
        from app.voucher_edit_window import _ResizeHandle

        for dpi_scale in (1.0, 1.25, 1.5):
            with self.subTest(dpi_scale=dpi_scale):
                win = self._make_window()
                item = win.add_text_rect(QRectF(80, 80, 24, 14), text="+2",
                                         font_size=8, auto_edit=False, auto_fit=False)
                self._select_resize_handle(win, item)
                win.show()
                QApplication.processEvents()
                win._view.setTransform(QTransform.fromScale(dpi_scale, dpi_scale))
                handle = next(h for h in win._handles
                              if isinstance(h, _ResizeHandle)
                              and h._position == "bottom_right")
                before = item.box_rect_scene()
                self._viewport_drag_handle(win, handle, QPoint(3, 3), QPoint(2, 0))
                self.assertNotEqual(item.box_rect_scene(), before)
                self.assertIsNone(win._scene._drag_state)

    def test_viewport_formal_group_all_eight_handles_resize_members(self) -> None:
        """正式グループの角・辺中央すべてが全メンバーを30pxリサイズする。"""
        from app.voucher_edit_window import _GroupResizeHandle

        directions = {
            "top_left": QPoint(-30, -30), "top": QPoint(0, -30),
            "top_right": QPoint(30, -30), "right": QPoint(30, 0),
            "bottom_right": QPoint(30, 30), "bottom": QPoint(0, 30),
            "bottom_left": QPoint(-30, 30), "left": QPoint(-30, 0),
        }
        for position, delta in directions.items():
            with self.subTest(position=position):
                win = self._make_window()
                a = win.add_text_rect(QRectF(80, 80, 80, 25), text="上",
                                      font_size=10, auto_edit=False, auto_fit=False)
                b = win.add_text_rect(QRectF(100, 180, 100, 30), text="下",
                                      font_size=12, auto_edit=False, auto_fit=False)
                win._select_items([a, b])
                self.assertTrue(win.group_selected())
                gid = a.group_id
                win.commit_history()
                handle = next(h for h in win._handles
                              if isinstance(h, _GroupResizeHandle)
                              and h._position == position)
                before = {o["id"]: o for o in win.serialize_objects()}
                self._viewport_drag_handle(win, handle, delta)
                after = {o["id"]: o for o in win.serialize_objects()}
                for member in (a, b):
                    self.assertEqual(member.group_id, gid)
                    self.assertNotEqual(
                        (before[member.obj_id]["x"], before[member.obj_id]["y"],
                         before[member.obj_id]["width"], before[member.obj_id]["height"]),
                        (after[member.obj_id]["x"], after[member.obj_id]["y"],
                         after[member.obj_id]["width"], after[member.obj_id]["height"]))
                if "_" in position:
                    self.assertNotEqual(before[a.obj_id]["font_size"],
                                        after[a.obj_id]["font_size"])
                self.assertIsNone(win._scene._drag_state)
                win.undo()
                restored = {o["id"]: o for o in win.serialize_objects()}
                self.assertEqual(restored[a.obj_id]["group_id"], gid)
                win.redo()
                redone = {o["id"]: o for o in win.serialize_objects()}
                self.assertAlmostEqual(redone[a.obj_id]["font_size"],
                                       after[a.obj_id]["font_size"], delta=0.1)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestPasteShortcutRouting(unittest.TestCase):
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

        win = VoucherEditWindow(order_no="paste-1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def _paste_key_event(self) -> "QKeyEvent":
        return QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_V,
                         Qt.KeyboardModifier.ControlModifier)

    def test_handle_paste_shortcut_calls_paste_image(self) -> None:
        win = self._make_window()
        _set_clipboard_image()
        with mock.patch.object(win, "paste_image_from_clipboard",
                               wraps=win.paste_image_from_clipboard) as spy:
            handled = win.handle_paste_shortcut()
        self.assertTrue(handled)
        self.assertTrue(spy.called)

    def test_ctrl_v_on_view_pastes_image(self) -> None:
        """QGraphicsView にフォーカスがある状態でも Ctrl+V で貼り付けできる。"""
        win = self._make_window()
        _set_clipboard_image(0xFF123456)
        win._view.keyPressEvent(self._paste_key_event())
        objects = win.serialize_objects()
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["type"], "image")

    def test_ctrl_v_on_view_selects_pasted_image(self) -> None:
        win = self._make_window()
        _set_clipboard_image(0xFF654321)
        win._view.keyPressEvent(self._paste_key_event())
        selected = [it for it in win._scene.selectedItems()
                    if hasattr(it, "serialize_edit_object")]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].serialize_edit_object()["type"], "image")

    def test_ctrl_v_on_view_during_text_edit_prefers_text(self) -> None:
        """テキスト編集中の Ctrl+V は画像貼り付けしない（テキスト優先）。"""
        from PySide6.QtCore import QRectF

        win = self._make_window()
        _set_clipboard_image()
        text_item = win.add_text_rect(QRectF(50.0, 50.0, 120.0, 30.0), text="")
        text_item.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        text_item.setFocus(Qt.FocusReason.OtherFocusReason)
        with mock.patch.object(win, "paste_image_from_clipboard") as spy:
            win._view.keyPressEvent(self._paste_key_event())
        spy.assert_not_called()
        images = [o for o in win.serialize_objects() if o["type"] == "image"]
        self.assertEqual(images, [])

    def test_handle_paste_without_image_returns_false(self) -> None:
        win = self._make_window()
        QApplication.clipboard().clear()
        QApplication.clipboard().setText("ただのテキスト")
        self.assertFalse(win.handle_paste_shortcut())

    def test_ctrl_v_pastes_image_in_any_tool(self) -> None:
        """選択以外のツール選択中でも Ctrl+V で画像貼り付けできる（不具合2）。"""
        from app.voucher_edit_window import (
            TOOL_ELLIPSE, TOOL_LINE, TOOL_RECT, TOOL_SELECT, TOOL_TEXT,
        )

        for tool in (TOOL_TEXT, TOOL_LINE, TOOL_RECT, TOOL_ELLIPSE):
            with self.subTest(tool=tool):
                win = self._make_window()
                win.set_tool(tool)
                _set_clipboard_image(0xFF223344)
                win._view.keyPressEvent(self._paste_key_event())
                images = [o for o in win.serialize_objects()
                          if o["type"] == "image"]
                self.assertEqual(len(images), 1)
                # 貼り付け後は選択ツールへ戻る。
                self.assertEqual(win.current_tool, TOOL_SELECT)

    def test_ctrl_v_returns_to_select_tool(self) -> None:
        from app.voucher_edit_window import TOOL_RECT, TOOL_SELECT

        win = self._make_window()
        win.set_tool(TOOL_RECT)
        _set_clipboard_image(0xFF445566)
        self.assertTrue(win.handle_paste_shortcut())
        self.assertEqual(win.current_tool, TOOL_SELECT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
