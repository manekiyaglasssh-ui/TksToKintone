"""指図書編集の複数選択・永続グループ操作テスト。"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
    from PySide6.QtGui import QKeySequence
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    PYSIDE_AVAILABLE = True
except Exception:
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditGroups(unittest.TestCase):
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

    def window(self, order="groups"):
        from app.voucher_edit_window import VoucherEditWindow
        win = VoucherEditWindow(order_no=order, background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def two_items(self, win):
        a = win.add_rect(QRectF(20, 30, 40, 20), text="A")
        b = win.add_text_rect(QRectF(100, 60, 80, 30), text="B",
                              font_size=10, auto_edit=False, auto_fit=False)
        win._select_items([a, b])
        return a, b

    def viewport_drag(self, win, scene_start, delta):
        win.show()
        win.set_tool("select")
        QApplication.processEvents()
        start = win._view.mapFromScene(scene_start)
        end = win._view.mapFromScene(scene_start + delta)
        QTest.mousePress(win._view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, start)
        QTest.mouseMove(win._view.viewport(), end, 20)
        QTest.mouseRelease(win._view.viewport(), Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, end)
        QApplication.processEvents()
        return win._scene._last_drag_result

    def assert_common_drag(self, result, kind, items, group_id=None):
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], kind)
        self.assertEqual(set(result["object_ids"]), {item.obj_id for item in items})
        self.assertEqual(result["group_id"], group_id)
        self.assertGreaterEqual(result["move_event_count"], 1)

    def test_viewport_formal_group_member_and_gap_drag_uses_all_members(self):
        win = self.window("viewport-group")
        a = win.add_text_rect(QRectF(80, 80, 100, 30), text="上",
                              font_size=10, auto_edit=False, auto_fit=False)
        b = win.add_text_rect(QRectF(80, 180, 100, 30), text="下",
                              font_size=10, auto_edit=False, auto_fit=False)
        win._select_items([a, b])
        self.assertTrue(win.group_selected())
        gid = a.group_id
        before = {item.obj_id: QPointF(item.pos()) for item in (a, b)}
        # 上辺中央リサイズハンドルから十分離れたメンバー内部は移動になる。
        member_drag_point = a.box_rect_scene().center() + QPointF(15, 0)
        result = self.viewport_drag(win, member_drag_point, QPointF(30, 0))
        self.assert_common_drag(result, "group", (a, b), gid)
        for item in (a, b):
            self.assertAlmostEqual((item.pos() - before[item.obj_id]).x(), 30, delta=1.5)
        before = {item.obj_id: QPointF(item.pos()) for item in (a, b)}
        member_drag_point = a.box_rect_scene().center() + QPointF(15, 0)
        result = self.viewport_drag(win, member_drag_point, QPointF(3, 0))
        self.assert_common_drag(result, "group", (a, b), gid)
        for item in (a, b):
            self.assertAlmostEqual((item.pos() - before[item.obj_id]).x(), 3, delta=1.5)
        # メンバー間の空白も visual group selection rect から全件移動する。
        before = {item.obj_id: QPointF(item.pos()) for item in (a, b)}
        gap = QPointF(win._scene._visual_selection_rect().center().x(),
                      (a.box_rect_scene().bottom() + b.box_rect_scene().top()) / 2)
        result = self.viewport_drag(win, gap, QPointF(30, 0))
        self.assert_common_drag(result, "group", (a, b), gid)
        self.assertEqual(result["hit_source"], "group_selection_rect")
        for item in (a, b):
            self.assertAlmostEqual((item.pos() - before[item.obj_id]).x(), 30, delta=1.5)

    def test_viewport_selection_rect_five_points_and_three_pixels(self):
        for index, (rx, ry) in enumerate(((.25, .25), (.5, .5), (.75, .75),
                                          (.9, .5), (.5, .9))):
            with self.subTest(point=(rx, ry)):
                win = self.window(f"viewport-single-{index}")
                item = win.add_text_rect(QRectF(120, 100, 140, 140), text="x",
                                         font_size=10, auto_edit=False, auto_fit=False)
                item.set_manual_box_size(140, 140)
                win._select_items([item])
                QApplication.processEvents()
                rect = win._scene._visual_selection_rect()
                point = QPointF(rect.left() + rect.width() * rx,
                                rect.top() + rect.height() * ry)
                before = QPointF(item.pos())
                distance = 30 if index == 0 else 3
                result = self.viewport_drag(win, point, QPointF(distance, 0))
                self.assert_common_drag(result, "single", (item,))
                self.assertEqual(result["hit_source"], "single_selection_rect")
                self.assertAlmostEqual((item.pos() - before).x(), distance, delta=1.5)
                self.assertIsNone(win._scene._drag_state)

    def test_viewport_selected_group_rect_precedes_overlapping_single(self):
        win = self.window("viewport-overlap")
        a, b = self.two_items(win)
        self.assertTrue(win.group_selected())
        gid = a.group_id
        overlap = win.add_rect(QRectF(a.sceneBoundingRect()), text="前面単体")
        overlap.setZValue(500)
        win._select_items([a, b])
        before_group = {item.obj_id: QPointF(item.pos()) for item in (a, b)}
        before_overlap = QPointF(overlap.pos())
        result = self.viewport_drag(win, a.sceneBoundingRect().center(), QPointF(30, 0))
        self.assert_common_drag(result, "group", (a, b), gid)
        self.assertEqual(result["hit_source"], "group_selection_rect")
        for item in (a, b):
            self.assertAlmostEqual((item.pos() - before_group[item.obj_id]).x(), 30,
                                   delta=1.5)
        self.assertEqual(overlap.pos(), before_overlap)

    def test_viewport_group_undo_redo_is_one_snapshot(self):
        win = self.window("viewport-undo")
        a, b = self.two_items(win)
        self.assertTrue(win.group_selected())
        gid = a.group_id
        before = {obj["id"]: (obj["x"], obj["y"]) for obj in win.serialize_objects()}
        result = self.viewport_drag(win, a.sceneBoundingRect().center(), QPointF(30, 0))
        self.assert_common_drag(result, "group", (a, b), gid)
        moved = {obj["id"]: (obj["x"], obj["y"]) for obj in win.serialize_objects()}
        win.undo()
        restored = {obj["id"]: obj for obj in win.serialize_objects()}
        for oid, pos in before.items():
            self.assertAlmostEqual(restored[oid]["x"], pos[0], delta=.1)
            self.assertAlmostEqual(restored[oid]["y"], pos[1], delta=.1)
        win.redo()
        restored = {obj["id"]: obj for obj in win.serialize_objects()}
        for oid, pos in moved.items():
            self.assertAlmostEqual(restored[oid]["x"], pos[0], delta=.1)
            self.assertAlmostEqual(restored[oid]["y"], pos[1], delta=.1)
            self.assertEqual(restored[oid]["group_id"], gid)

    def test_group_assigns_id_name_members_and_rejects_nesting(self):
        win = self.window()
        a, b = self.two_items(win)
        self.assertTrue(win.group_selected())
        self.assertTrue(a.group_id)
        self.assertEqual(a.group_id, b.group_id)
        self.assertEqual(a.group_name, "グループ 1")
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            self.assertFalse(win.group_selected())

    def test_ungroup_and_undo_redo_are_single_snapshots(self):
        win = self.window()
        self.two_items(win)
        win.commit_history()
        self.assertTrue(win.group_selected())
        self.assertTrue(win.ungroup_selected())
        self.assertFalse(any(o.get("group_id") for o in win.serialize_objects()))
        win.undo()
        self.assertEqual(len({o.get("group_id") for o in win.serialize_objects()}), 1)
        win.redo()
        self.assertFalse(any(o.get("group_id") for o in win.serialize_objects()))

    def test_copy_paste_preserves_group_and_offset(self):
        win = self.window()
        self.two_items(win)
        win.group_selected()
        original = win.serialize_objects()
        self.assertTrue(win.copy_selected_objects())
        self.assertTrue(win.paste_copied_objects())
        objects = win.serialize_objects()
        groups = {}
        for obj in objects:
            groups.setdefault(obj.get("group_id"), []).append(obj)
        self.assertEqual(sorted(len(v) for k, v in groups.items() if k), [2, 2])
        self.assertEqual(len({o["group_id"] for o in original}), 1)

    def test_cross_voucher_clone_remaps_one_group_without_nesting(self):
        from app.voucher_edit_objects import clone_edit_objects
        source = [
            {"id": "a", "type": "rectangle", "group_id": "old"},
            {"id": "b", "type": "text", "group_id": "old"},
        ]
        cloned = clone_edit_objects(source)
        self.assertNotEqual(cloned[0]["id"], "a")
        self.assertNotEqual(cloned[0]["group_id"], "old")
        self.assertEqual(cloned[0]["group_id"], cloned[1]["group_id"])
        self.assertFalse(any(isinstance(o.get("members"), list) for o in cloned))

    def test_save_reload_keeps_group_metadata(self):
        win = self.window("group-save")
        self.two_items(win)
        win.group_selected()
        self.assertTrue(win._persist())
        restored = self.window("group-save")
        objects = restored.serialize_objects()
        self.assertEqual(len(objects), 2)
        self.assertEqual(len({o.get("group_id") for o in objects}), 1)
        self.assertTrue(restored.ungroup_selected() is False)  # 未選択
        restored._select_items(restored.edit_items())
        self.assertTrue(restored.ungroup_selected())

    def test_group_favorite_uses_relative_coordinates_and_restores_group(self):
        win = self.window()
        a, _ = self.two_items(win)
        win.group_selected()
        self.assertTrue(win.add_object_to_favorites(a))
        favorite = win._favorites[-1]
        self.assertEqual(favorite["type"], "group")
        self.assertAlmostEqual(min(o.get("x", 9999) for o in favorite["objects"]), 0)
        self.assertTrue(win.drop_favorite_object(favorite["id"], QPointF(250, 200)))
        selected = win._selected_edit_items()
        self.assertEqual(len(selected), 2)
        self.assertEqual(len({item.group_id for item in selected}), 1)

    def test_group_favorite_saves_origin_and_double_click_restores_it(self):
        win = self.window("group-favorite-origin")
        a, b = self.two_items(win)
        win.group_selected()
        origin = win._selected_bounds().topLeft()
        self.assertTrue(win.add_object_to_favorites(a))
        favorite = win._favorites[-1]
        self.assertAlmostEqual(favorite["origin_x"], 20.0)
        self.assertAlmostEqual(favorite["origin_y"], 30.0)
        before_relative = b.sceneBoundingRect().topLeft() - a.sceneBoundingRect().topLeft()
        self.assertTrue(win.drop_favorite_object(favorite["id"], None))
        created = win._selected_edit_items()
        self.assertEqual(len(created), 2)
        self.assertEqual(len({item.group_id for item in created}), 1)
        created_bounds = win._selected_bounds()
        self.assertAlmostEqual(created_bounds.x(), origin.x(), delta=0.01)
        self.assertAlmostEqual(created_bounds.y(), origin.y(), delta=0.01)
        created_by_type = sorted(created, key=lambda item: item.sceneBoundingRect().x())
        self.assertAlmostEqual(
            (created_by_type[1].sceneBoundingRect().topLeft()
             - created_by_type[0].sceneBoundingRect().topLeft()).x(),
            before_relative.x(), delta=0.01)

    def test_legacy_group_favorite_without_origin_uses_default_placement(self):
        win = self.window("legacy-group-favorite")
        win._favorites.append({
            "id": "legacy-group", "type": "group", "group_name": "旧",
            "objects": [{"id": "old-a", "type": "rectangle", "x": 0,
                         "y": 0, "width": 20, "height": 20}],
        })
        self.assertTrue(win.drop_favorite_object("legacy-group", None))
        self.assertGreater(win._selected_bounds().x(), 0.0)

    def test_qtest_drag_from_gap_inside_group_bounds_moves_all_members(self):
        win = self.window("group-gap-drag")
        a, b = self.two_items(win)
        win.group_selected()
        win.show()
        win.set_tool("select")
        QApplication.processEvents()
        before = {item.obj_id: QPointF(item.pos()) for item in (a, b)}
        gap = win._view.mapFromScene(QPointF(75, 45))
        end = gap + QPoint(18, 12)
        QTest.mousePress(win._view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, gap)
        QTest.mouseMove(win._view.viewport(), end, 20)
        QTest.mouseRelease(win._view.viewport(), Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, end)
        scene_delta = win._view.mapToScene(end) - win._view.mapToScene(gap)
        for item in (a, b):
            self.assertAlmostEqual(item.pos().x() - before[item.obj_id].x(),
                                   scene_delta.x(), delta=1.0)
            self.assertAlmostEqual(item.pos().y() - before[item.obj_id].y(),
                                   scene_delta.y(), delta=1.0)

    def test_group_resize_scales_members_and_text_from_start_snapshot(self):
        from app.voucher_edit_window import _GroupResizeHandle
        win = self.window()
        a, b = self.two_items(win)
        win.group_selected()
        before = {o["id"]: o for o in win.serialize_objects()}
        handle = next(h for h in win._handles
                      if isinstance(h, _GroupResizeHandle)
                      and h._position == "bottom_right")
        handle._start_objects = [win._serialize_item(a), win._serialize_item(b)]
        handle.setPos(QPointF(handle._start_rect.right() + handle._start_rect.width(),
                              handle._start_rect.bottom() + handle._start_rect.height()))
        after = {o["id"]: o for o in win.serialize_objects()}
        self.assertGreater(after[a.obj_id]["width"], before[a.obj_id]["width"])
        self.assertGreater(after[b.obj_id]["font_size"], before[b.obj_id]["font_size"])

    def test_qtest_shortcuts_group_and_ungroup(self):
        win = self.window()
        self.two_items(win)
        win._view.setFocus()
        QTest.keySequence(win._view, QKeySequence("Ctrl+G"))
        self.assertTrue(all(o.get("group_id") for o in win.serialize_objects()))
        QTest.keySequence(win._view, QKeySequence("Ctrl+Shift+G"))
        self.assertFalse(any(o.get("group_id") for o in win.serialize_objects()))

    def test_qtest_ctrl_click_toggle_and_blank_click(self):
        win = self.window()
        a, b = self.two_items(win)
        win.show()
        win.set_tool("select")
        QApplication.processEvents()
        win._scene.clearSelection()
        pa = win._view.mapFromScene(a.sceneBoundingRect().center())
        pb = win._view.mapFromScene(b.sceneBoundingRect().center())
        QTest.mouseClick(win._view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, pa)
        self.assertEqual({i.obj_id for i in win._selected_edit_items()}, {a.obj_id})
        QTest.mouseClick(win._view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, pb)
        self.assertEqual({i.obj_id for i in win._selected_edit_items()},
                         {a.obj_id, b.obj_id})
        QTest.mouseClick(win._view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, pb)
        self.assertEqual({i.obj_id for i in win._selected_edit_items()}, {a.obj_id})
        blank = win._view.mapFromScene(QPointF(400, 400))
        QTest.mouseClick(win._view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, blank)
        self.assertFalse(win._selected_edit_items())

    def test_qtest_right_click_selected_member_keeps_selection_and_groups(self):
        """複数選択中のメンバー上の実右クリックで選択を壊さない。"""
        win = self.window("right-click-group")
        a, b = self.two_items(win)
        win.show()
        win.set_tool("select")
        QApplication.processEvents()
        menus = []

        def capture(item, _global_pos):
            menus.append(win._build_object_context_menu(item))

        with mock.patch.object(win, "_show_object_context_menu", side_effect=capture):
            point = win._view.mapFromScene(a.sceneBoundingRect().center())
            QTest.mouseClick(win._view.viewport(), Qt.MouseButton.RightButton,
                             Qt.KeyboardModifier.NoModifier, point)
            # offscreen QtではOSのコンテキストメニュー通知が省略されるため、
            # 実クリック後に同じsceneイベントも配送する。
            class Event:
                def scenePos(self): return a.sceneBoundingRect().center()
                def screenPos(self): return QPoint(0, 0)
                def accept(self): pass
            win._scene.contextMenuEvent(Event())

        self.assertEqual({i.obj_id for i in win._selected_edit_items()},
                         {a.obj_id, b.obj_id})
        self.assertEqual(len(menus), 1)
        group_action = next(action for action in menus[0].actions()
                            if action.objectName() == "group_action")
        self.assertTrue(group_action.isEnabled())
        group_action.trigger()
        self.assertEqual(len({o.get("group_id") for o in win.serialize_objects()}), 1)
        self.assertEqual(len(win._selected_edit_items()), 2)

    def test_qtest_right_click_unselected_object_selects_only_that_object(self):
        win = self.window("right-click-single")
        a, b = self.two_items(win)
        win.show()
        win.set_tool("select")
        QApplication.processEvents()
        win._select_items([a])
        with mock.patch.object(win, "_show_object_context_menu"):
            point = win._view.mapFromScene(b.sceneBoundingRect().center())
            QTest.mouseClick(win._view.viewport(), Qt.MouseButton.RightButton,
                             Qt.KeyboardModifier.NoModifier, point)
            class Event:
                def scenePos(self): return b.sceneBoundingRect().center()
                def screenPos(self): return QPoint(0, 0)
                def accept(self): pass
            win._scene.contextMenuEvent(Event())
        self.assertEqual(win._selected_edit_items(), [b])

    def test_qtest_rubber_band_and_group_drag(self):
        win = self.window()
        a, b = self.two_items(win)
        win.show()
        win.set_tool("select")
        QApplication.processEvents()
        win._scene.clearSelection()
        start = win._view.mapFromScene(QPointF(10, 15))
        end = win._view.mapFromScene(QPointF(195, 105))
        QTest.mousePress(win._view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, start)
        QTest.mouseMove(win._view.viewport(), end, 20)
        QTest.mouseRelease(win._view.viewport(), Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, end)
        self.assertEqual(len(win._selected_edit_items()), 2)
        self.assertTrue(win.group_selected())
        before = {i.obj_id: QPointF(i.pos()) for i in (a, b)}
        center = win._view.mapFromScene(a.sceneBoundingRect().center())
        destination = center + QPoint(20, 15)
        QTest.mousePress(win._view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, center)
        QTest.mouseMove(win._view.viewport(), destination, 20)
        QTest.mouseRelease(win._view.viewport(), Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, destination)
        da = a.pos() - before[a.obj_id]
        db = b.pos() - before[b.obj_id]
        self.assertAlmostEqual(da.x(), db.x(), delta=0.5)
        self.assertAlmostEqual(da.y(), db.y(), delta=0.5)
