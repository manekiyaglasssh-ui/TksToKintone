from __future__ import annotations

import os
import io
import copy
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication


class TestVoucherPreviewUnification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_editor_request_uses_list_targets_and_injects_all_unsaved_snapshots(self):
        from app.voucher_window import VoucherWindow

        pages = [
            {"voucher_no": "A", "edit_objects": [{"id": "old-a"}]},
            {"voucher_no": "B", "edit_objects": [{"id": "old-b"}]},
        ]
        rw = SimpleNamespace(cached_olap={"pages": pages})
        row = SimpleNamespace(
            voucher_checks={
                "01": True, "02": False, "03": True, "04": True,
                "05": False, "06": False, "07": True, "08": False,
            }
        )
        owner = SimpleNamespace(
            _find_row_widget_by_order=lambda _order: rw,
            _collect_row=lambda _rw: row,
            _attach_row_settings=lambda _data, _row: None,
        )
        snapshot = {
            "voucher_no": "A",
            "common_edit": [{"id": "common"}],
            "voucher_edits": {
                "A": [{"id": "only-a"}],
                "B": [{"id": "only-b"}],
            },
        }
        ids, data = VoucherWindow.build_editor_preview_request(
            owner, "ORDER", "A", snapshot, "03")
        self.assertEqual(ids, ["01", "03", "04", "07"])
        self.assertEqual(
            [o["id"] for o in data["pages"][0]["edit_objects"]],
            ["common", "only-a"])
        self.assertEqual(
            [o["id"] for o in data["pages"][1]["edit_objects"]],
            ["common", "only-b"])
        self.assertEqual(pages[0]["edit_objects"], [{"id": "old-a"}])
        self.assertEqual(pages[1]["edit_objects"], [{"id": "old-b"}])

    def test_preview_page_count_and_order_match_list_request(self):
        import pypdf
        from app.voucher_preview_controller import build_voucher_preview_pdf

        ids = ["01", "03", "04", "07"]
        data = {
            "pages": [
                {"order_no": "ORDER", "voucher_no": "A", "details": []},
                {"order_no": "ORDER", "voucher_no": "B", "details": []},
            ]
        }
        list_pdf = build_voucher_preview_pdf(
            ids, data, reload_edit_objects=False
        )
        editor_pdf = build_voucher_preview_pdf(
            ids, data, reload_edit_objects=False
        )
        list_reader = pypdf.PdfReader(io.BytesIO(list_pdf))
        editor_reader = pypdf.PdfReader(io.BytesIO(editor_pdf))
        self.assertEqual(len(list_reader.pages), 8)
        self.assertEqual(len(editor_reader.pages), len(list_reader.pages))
        self.assertEqual(
            [page.extract_text() for page in editor_reader.pages],
            [page.extract_text() for page in list_reader.pages],
        )

    def test_resolve_preview_targets_uses_official_order_and_excludes_disabled(self):
        from app.voucher_preview_controller import resolve_preview_voucher_ids

        checks = {
            "08": True, "03": True, "01": False, "04": True,
            "unknown": True,
        }
        self.assertEqual(resolve_preview_voucher_ids(checks), ["03", "04", "08"])

    def test_snapshot_includes_active_unconfirmed_text_without_mutating_state(self):
        from app.voucher_edit_window import VoucherEditWindow

        history = [[{"id": "history"}]]
        saved_models = {"A": [{"id": "saved-a", "text": "old"}]}
        owner = SimpleNamespace(
            serialize_objects=lambda: [
                {"id": "active", "type": "text", "text": "入力途中"}
            ],
            _edit_mode="individual",
            _common_objects=[{"id": "common"}],
            _voucher_objects=copy.deepcopy(saved_models),
            _current_voucher_key="A",
            current_voucher_no="A",
            _history=copy.deepcopy(history),
            _history_index=0,
            _dirty=True,
        )
        before_models = copy.deepcopy(owner._voucher_objects)
        before_history = copy.deepcopy(owner._history)

        snapshot = VoucherEditWindow.preview_snapshot(owner)

        self.assertEqual(
            snapshot["voucher_edits"]["A"][0]["text"], "入力途中"
        )
        self.assertEqual(owner._voucher_objects, before_models)
        self.assertEqual(owner._history, before_history)
        self.assertEqual(owner._history_index, 0)
        self.assertTrue(owner._dirty)

    def test_preview_finish_restores_action_and_status(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow("PREVIEW-FINISH", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win._preview_action.setEnabled(False)
        win.statusBar().showMessage("プレビュー生成中…")
        win._on_edit_preview_finished()
        self.assertTrue(win._preview_action.isEnabled())
        self.assertEqual(win.statusBar().currentMessage(), "")

    def test_close_cancels_active_preview_worker(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow("PREVIEW-CLOSE", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        worker = mock.Mock()
        win._preview_worker = worker
        win._dirty = False
        win.close()
        worker.cancel.assert_called_once_with()

    def test_preview_double_click_is_ignored_while_worker_runs(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow("PREVIEW-DOUBLE", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win._background_pdf_by_voucher[win._current_voucher_key] = b"%PDF"
        running = mock.Mock()
        running.isRunning.return_value = True
        win._preview_thread = running
        self.assertFalse(win.preview_unsaved_edits())

    def test_small_text_context_menu_and_proxy_resolution(self):
        from app.voucher_edit_window import (
            VoucherEditWindow, _ResizeHandle,
            resolve_edit_object_from_graphics_item,
        )

        win = VoucherEditWindow("SMALL", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_at(QPointF(20, 20), text="x", font_size=6)
        menu = win._build_object_context_menu(item)
        self.assertIsNotNone(menu.findChild(type(menu.actions()[0]), "edit_text_action"))
        handle = _ResizeHandle(item)
        self.assertIs(resolve_edit_object_from_graphics_item(handle, win._scene), item)

    def test_begin_small_text_edit_uses_shared_entry_without_history(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow("SMALL-EDIT", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        item = win.add_text_at(QPointF(20, 20), text="x", font_size=6)
        before = len(win._history)
        self.assertTrue(win.begin_text_edit(item))
        self.assertNotEqual(
            item.textInteractionFlags(),
            item.textInteractionFlags().NoTextInteraction,
        )
        self.assertEqual(len(win._history), before)

    def test_both_screens_use_common_preview_opener(self):
        from app.voucher_window import VoucherWindow

        owner = SimpleNamespace(_preview_window=None)
        preview = mock.Mock()
        with mock.patch(
            "app.voucher_preview_controller.open_voucher_preview",
            return_value=preview,
        ) as opener:
            result = VoucherWindow._open_preview_window(owner, b"%PDF-data")
        self.assertIs(result, preview)
        opener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
