"""指図書編集画面の反映先・お気に入り表示順テスト。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QMimeData, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app import voucher_edit_window as edit


class _MimeEvent:
    def __init__(self, mime: QMimeData) -> None:
        self._mime = mime

    def mimeData(self) -> QMimeData:  # noqa: N802
        return self._mime


class TestVoucherEditReordering(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._temp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = self._temp.name
        settings = QSettings(edit.SETTINGS_ORG, edit.SETTINGS_APP)
        self._old_reflection = settings.value(edit.REFLECTION_TARGET_ORDER_KEY)
        self._old_favorites = settings.value(edit.FAVORITE_OBJECT_ORDER_KEY)
        settings.remove(edit.REFLECTION_TARGET_ORDER_KEY)
        settings.remove(edit.FAVORITE_OBJECT_ORDER_KEY)
        settings.sync()

    def tearDown(self) -> None:
        settings = QSettings(edit.SETTINGS_ORG, edit.SETTINGS_APP)
        for key, value in (
            (edit.REFLECTION_TARGET_ORDER_KEY, self._old_reflection),
            (edit.FAVORITE_OBJECT_ORDER_KEY, self._old_favorites),
        ):
            if value is None:
                settings.remove(key)
            else:
                settings.setValue(key, value)
        settings.sync()
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home
        self._temp.cleanup()

    def _window(self, name: str) -> edit.VoucherEditWindow:
        window = edit.VoucherEditWindow(order_no=name, background_pdf_bytes=b"")
        def cleanup() -> None:
            window._dirty = False
            window.close()
            window.deleteLater()
            self.app.processEvents()

        self.addCleanup(cleanup)
        window.show()
        self.app.processEvents()
        return window

    def _mime_for(self, widget, row: int) -> QMimeData:
        item = widget.item(row)
        payload = {
            "section": widget._section,
            "stable_id": str(item.data(Qt.ItemDataRole.UserRole)),
            "from_row": row,
            "token": widget._drag_token,
        }
        mime = QMimeData()
        mime.setData(
            edit.LEFT_PANE_ORDER_MIME,
            QByteArray(json.dumps(payload).encode("utf-8")),
        )
        return mime

    def _send_drop(self, widget, row: int, pos: QPointF) -> tuple[bool, bool, bool]:
        mime = self._mime_for(widget, row)
        enter = QDragEnterEvent(
            pos.toPoint(), Qt.DropAction.MoveAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        moved = QDragMoveEvent(
            pos.toPoint(), Qt.DropAction.MoveAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        dropped = QDropEvent(
            pos, Qt.DropAction.MoveAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(widget.viewport(), enter)
        QApplication.sendEvent(widget.viewport(), moved)
        QApplication.sendEvent(widget.viewport(), dropped)
        return enter.isAccepted(), moved.isAccepted(), dropped.isAccepted()

    def test_reflection_target_drag_reorders_by_stable_key_without_history(self) -> None:
        window = self._window("reflect-drag")
        before_history = list(window._history)
        keys = window._template_order_keys()
        moved = [keys[2], keys[0], keys[1], *keys[3:]]
        selected = window._current_template_name
        window._on_reflection_order_changed(moved)
        self.assertEqual(window._template_order_keys(), moved)
        self.assertEqual(window._current_template_name, selected)
        self.assertEqual(window._history, before_history)

    def test_reflection_target_order_saves_restores_and_repairs(self) -> None:
        settings = QSettings(edit.SETTINGS_ORG, edit.SETTINGS_APP)
        settings.setValue(
            edit.REFLECTION_TARGET_ORDER_KEY,
            ["instruction_only", "unknown", "instruction_only", "standard"],
        )
        settings.sync()
        window = self._window("reflect-restore")
        self.assertEqual(
            window._template_order_keys(),
            ["instruction_only", "standard", "all_vouchers", "packing_only"],
        )
        window._on_reflection_order_changed(
            ["packing_only", "standard", "all_vouchers", "instruction_only"])
        restored = self._window("reflect-restored")
        self.assertEqual(
            restored._template_order_keys(),
            ["packing_only", "standard", "all_vouchers", "instruction_only"],
        )

    def test_favorite_drag_reorders_by_id_and_preserves_object_data(self) -> None:
        window = self._window("favorite-drag")
        first = window.add_text_at(QPointF(10, 10), text="same")
        second = window.add_text_at(QPointF(20, 20), text="same")
        self.assertTrue(window.add_object_to_favorites(first))
        self.assertTrue(window.add_object_to_favorites(second))
        ids = [favorite["id"] for favorite in window._favorites]
        originals = {
            favorite["id"]: json.dumps(favorite, sort_keys=True)
            for favorite in window._favorites
        }
        history_size = len(window._history)
        window._on_favorite_order_changed(list(reversed(ids)))
        self.assertEqual([favorite["id"] for favorite in window._favorites],
                         list(reversed(ids)))
        self.assertEqual(
            {favorite["id"]: json.dumps(favorite, sort_keys=True)
             for favorite in window._favorites},
            originals,
        )
        self.assertEqual(len(window._history), history_size)

    def test_favorite_order_saves_restores_adds_and_deletes(self) -> None:
        window = self._window("favorite-save")
        for index in range(3):
            item = window.add_text_at(QPointF(index, index), text=str(index))
            self.assertTrue(window.add_object_to_favorites(item))
        ids = [favorite["id"] for favorite in window._favorites]
        window._on_favorite_order_changed([ids[2], ids[0], ids[1]])
        restored = self._window("favorite-restore")
        self.assertEqual([favorite["id"] for favorite in restored._favorites],
                         [ids[2], ids[0], ids[1]])
        new_item = restored.add_text_at(QPointF(4, 4), text="new")
        self.assertTrue(restored.add_object_to_favorites(new_item))
        self.assertEqual([favorite["id"] for favorite in restored._favorites[:3]],
                         [ids[2], ids[0], ids[1]])
        self.assertTrue(restored.remove_favorite_object(ids[0]))
        self.assertEqual([favorite["id"] for favorite in restored._favorites[:2]],
                         [ids[2], ids[1]])

    def test_legacy_favorite_gets_persistent_stable_id_once(self) -> None:
        path = edit._favorites_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([
            {
                "name": "legacy",
                "object": {"type": "text", "text": "old"},
                "favorite_position": {"x": 12, "y": 34},
            }
        ], ensure_ascii=False), encoding="utf-8")
        first = edit.load_favorite_objects()
        second = edit.load_favorite_objects()
        self.assertTrue(first[0]["id"])
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(second[0]["favorite_position"], {"x": 12, "y": 34})
        self.assertEqual(second[0]["registration_order"], 0)

    def test_cross_section_drop_payload_is_rejected(self) -> None:
        reflection = edit._ReflectionTargetListWidget()
        favorites = edit._FavoriteListWidget()
        self.addCleanup(reflection.deleteLater)
        self.addCleanup(favorites.deleteLater)
        mime = QMimeData()
        mime.setData(
            edit.LEFT_PANE_ORDER_MIME,
            QByteArray(json.dumps(
                {
                    "section": "reflection_targets",
                    "stable_id": "standard",
                    "from_row": 0,
                    "token": reflection._drag_token,
                }
            ).encode("utf-8")),
        )
        event = _MimeEvent(mime)
        self.assertIsNotNone(reflection._drag_payload(event))
        self.assertIsNone(favorites._drag_payload(event))

    def test_qtest_reflection_handle_starts_explicit_list_drag(self) -> None:
        window = self._window("reflect-qtest-start")
        widget = window._reflect_list
        item = widget.item(2)
        row_widget = widget.itemWidget(item)
        handle = row_widget.findChild(edit._ReorderDragHandle)
        started: list[dict] = []
        widget.dragStarted.connect(started.append)
        with mock.patch.object(edit.QDrag, "exec",
                               return_value=Qt.DropAction.IgnoreAction):
            QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=QPoint(8, 10))
            QTest.mouseMove(
                handle,
                QPoint(8 + QApplication.startDragDistance() + 4, 10),
                delay=10,
            )
            QTest.mouseRelease(handle, Qt.MouseButton.LeftButton,
                               pos=QPoint(20, 10))
        self.assertEqual(started[0]["stable_id"], "instruction_only")
        self.assertEqual(started[0]["from_row"], 2)

    def test_qtest_reflection_button_click_does_not_start_drag(self) -> None:
        window = self._window("reflect-qtest-click")
        widget = window._reflect_list
        started: list[dict] = []
        widget.dragStarted.connect(started.append)
        button = window._template_actions["指図書のみ"]
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertEqual(started, [])
        self.assertEqual(window._current_template_name, "指図書のみ")

    def test_real_drag_events_move_third_reflection_to_first(self) -> None:
        window = self._window("reflect-real-drop")
        widget = window._reflect_list
        selected = window._current_template_name
        before = widget._ordered_ids()
        target = widget.visualItemRect(widget.item(0))
        accepted = self._send_drop(
            widget, 2, QPointF(target.center().x(), target.top() + 1))
        self.assertEqual(accepted, (True, True, True))
        self.assertEqual(
            window._template_order_keys(),
            [before[2], before[0], before[1], before[3]],
        )
        self.assertEqual(window._current_template_name, selected)
        self.assertEqual(
            edit._settings_string_list(edit.REFLECTION_TARGET_ORDER_KEY),
            window._template_order_keys(),
        )

    def test_real_drag_events_move_first_reflection_to_blank_tail(self) -> None:
        window = self._window("reflect-tail-drop")
        widget = window._reflect_list
        before = widget._ordered_ids()
        accepted = self._send_drop(
            widget, 0,
            QPointF(widget.viewport().width() / 2, widget.viewport().height() - 2),
        )
        self.assertEqual(accepted, (True, True, True))
        self.assertEqual(window._template_order_keys(), [*before[1:], before[0]])

    def test_real_drop_at_same_reflection_position_is_noop(self) -> None:
        window = self._window("reflect-noop-drop")
        widget = window._reflect_list
        before = widget._ordered_ids()
        rect = widget.visualItemRect(widget.item(1))
        self._send_drop(widget, 1, QPointF(rect.center().x(), rect.top() + 1))
        self.assertEqual(window._template_order_keys(), before)

    def test_qtest_favorite_handle_starts_reorder_not_canvas_drag(self) -> None:
        window = self._window("favorite-qtest-handle")
        for index in range(2):
            window.add_object_to_favorites(
                window.add_text_at(QPointF(index, index), text=str(index)))
        widget = window._favorite_list
        started: list[dict] = []
        widget.dragStarted.connect(started.append)
        canvas_drags: list[object] = []
        with mock.patch.object(edit.QDrag, "exec",
                               return_value=Qt.DropAction.IgnoreAction), \
                mock.patch.object(
                    window, "drop_favorite_object",
                    side_effect=lambda *args: canvas_drags.append(args)
                ):
            rect = widget.visualItemRect(widget.item(1))
            start = QPoint(8, rect.center().y())
            QTest.mousePress(widget.viewport(), Qt.MouseButton.LeftButton,
                             pos=start)
            QTest.mouseMove(
                widget.viewport(),
                QPoint(start.x() + QApplication.startDragDistance() + 5,
                       start.y()),
                delay=10,
            )
            QTest.mouseRelease(widget.viewport(), Qt.MouseButton.LeftButton,
                               pos=QPoint(start.x() + 20, start.y()))
        self.assertEqual(started[0]["from_row"], 1)
        self.assertEqual(canvas_drags, [])

    def test_favorite_body_drag_uses_only_canvas_mime(self) -> None:
        window = self._window("favorite-body-mime")
        window.add_object_to_favorites(
            window.add_text_at(QPointF(1, 1), text="body"))
        widget = window._favorite_list
        widget.setCurrentRow(0)
        captured: list[QMimeData] = []

        def capture_mime(mime):
            captured.append(mime)

        with mock.patch.object(edit.QDrag, "setMimeData", autospec=True,
                               side_effect=capture_mime), \
                mock.patch.object(edit.QDrag, "exec",
                                  return_value=Qt.DropAction.IgnoreAction):
            widget.startDrag(Qt.DropAction.CopyAction)
        self.assertTrue(captured[0].hasFormat(edit.FAVORITE_OBJECT_MIME))
        self.assertFalse(captured[0].hasFormat(edit.LEFT_PANE_ORDER_MIME))

    def test_qtest_favorite_body_starts_canvas_drag_not_reorder(self) -> None:
        window = self._window("favorite-body-qtest")
        window.add_object_to_favorites(
            window.add_text_at(QPointF(1, 1), text="body"))
        widget = window._favorite_list
        reorder_started: list[dict] = []
        widget.dragStarted.connect(reorder_started.append)
        rect = widget.visualItemRect(widget.item(0))
        start = QPoint(widget.HANDLE_WIDTH + 20, rect.center().y())
        with mock.patch.object(widget, "startDrag") as canvas_start:
            QTest.mousePress(widget.viewport(), Qt.MouseButton.LeftButton,
                             pos=start)
            QTest.mouseMove(
                widget.viewport(),
                QPoint(start.x() + QApplication.startDragDistance() + 5,
                       start.y()),
                delay=10,
            )
            QTest.mouseRelease(widget.viewport(), Qt.MouseButton.LeftButton,
                               pos=QPoint(start.x() + 20, start.y()))
        canvas_start.assert_called_once()
        self.assertEqual(reorder_started, [])

    def test_reorder_lists_expose_indicator_and_autoscroll_configuration(self) -> None:
        window = self._window("reorder-ui-config")
        for widget in (window._reflect_list, window._favorite_list):
            self.assertTrue(widget.dragEnabled())
            self.assertTrue(widget.acceptDrops())
            self.assertTrue(widget.viewport().acceptDrops())
            self.assertTrue(widget.showDropIndicator())
            self.assertEqual(
                widget.dragDropMode(),
                edit.QAbstractItemView.DragDropMode.InternalMove,
            )
            self.assertEqual(widget.defaultDropAction(), Qt.DropAction.MoveAction)
            self.assertFalse(widget.dragDropOverwriteMode())
            self.assertTrue(widget.hasAutoScroll())
            self.assertGreaterEqual(widget.autoScrollMargin(), 24)
            self.assertGreaterEqual(widget.HANDLE_WIDTH, 20)

    def test_real_drag_events_reorder_favorites_without_history(self) -> None:
        window = self._window("favorite-real-drop")
        for index in range(3):
            window.add_object_to_favorites(
                window.add_text_at(QPointF(index, index), text="same"))
        widget = window._favorite_list
        ids = [favorite["id"] for favorite in window._favorites]
        history_size = len(window._history)
        rect = widget.visualItemRect(widget.item(0))
        accepted = self._send_drop(
            widget, 2, QPointF(rect.center().x(), rect.top() + 1))
        self.assertEqual(accepted, (True, True, True))
        self.assertEqual(
            [favorite["id"] for favorite in window._favorites],
            [ids[2], ids[0], ids[1]],
        )
        self.assertEqual(len(window._history), history_size)
        self.assertEqual(
            edit._settings_string_list(edit.FAVORITE_OBJECT_ORDER_KEY),
            [ids[2], ids[0], ids[1]],
        )

    def test_reset_orders(self) -> None:
        window = self._window("reset-orders")
        keys = window._template_order_keys()
        window._on_reflection_order_changed(list(reversed(keys)))
        window.reset_reflection_target_order()
        self.assertEqual(
            window._template_order_keys(),
            ["standard", "all_vouchers", "instruction_only", "packing_only"],
        )
        for index in range(3):
            self.assertTrue(window.add_object_to_favorites(
                window.add_text_at(QPointF(index, index), text=str(index))))
        registered = [favorite["id"] for favorite in window._favorites]
        window._on_favorite_order_changed(list(reversed(registered)))
        window.reset_favorite_object_order()
        self.assertEqual([favorite["id"] for favorite in window._favorites],
                         registered)


if __name__ == "__main__":
    unittest.main()
