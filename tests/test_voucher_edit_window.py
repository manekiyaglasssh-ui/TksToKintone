"""指図書編集画面（VoucherEditWindow）の動的テスト。

QApplication を offscreen で起動し、画面が開くこと・オブジェクト追加→保存→
再読み込みで編集内容が復元されることを検証する。
"""
from __future__ import annotations

import os
import hashlib
import io
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import pypdf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF, Qt
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
        header = win._edit_header_widget
        self.assertIsNotNone(header)
        self.assertNotIsInstance(header, QToolBar)
        actions = [a.text() for a in header.actions()]
        # 図形6種は「図形」ボタン1つへまとめたため、個別ラベルはヘッダー直下に無い（要件5）。
        for label in ("選択", "テキスト", "削除", "保存", "閉じる"):
            self.assertIn(label, actions)
        for label in ("線", "四角", "丸", "矢印", "両矢印", "二重線"):
            self.assertNotIn(label, actions)

    def test_instruction_sheet_background_contains_delivery_course_name(self) -> None:
        from app import voucher_service
        from app.voucher_templates import DUMMY_DATA

        page = {
            **DUMMY_DATA,
            "order_no": "1405113",
            "voucher_no": "Z001",
            "delivery_course_code": "01",
            "delivery_course_name": "パレト",
            "sales_rep": "大上",
            "edit_objects": [],
        }
        background_pdf = voucher_service.build_vouchers_pdf_bytes(
            ["03"], {"pages": [page]}
        )
        text = "\n".join(
            pdf_page.extract_text() or ""
            for pdf_page in pypdf.PdfReader(io.BytesIO(background_pdf)).pages
        )
        self.assertIn("パレト", text)
        self.assertIn("大上", text)

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

    def test_save_emits_trace_hash_revision_with_same_object_id(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(
            order_no="TRACE-SAVE", background_pdf_bytes=b"",
            voucher_nos=["V1"])
        self.addCleanup(win.deleteLater)
        win.add_text_at(
            QPointF(100.0, 150.0), text="太字斜体テスト",
            font_size=18.0)
        item = win.edit_items()[0]
        item.apply_text_style(bold=True, italic=True, underline=True)
        object_id = item.obj_id
        emitted: list[tuple] = []
        win.voucherEditSaved.connect(lambda *args: emitted.append(args))
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        self.assertEqual(len(emitted), 1)
        order_no, voucher_no, content_hash, trace_id, revision = emitted[0]
        self.assertEqual(order_no, "TRACE-SAVE")
        self.assertEqual(voucher_no, "V1")
        self.assertEqual(len(content_hash), 64)
        self.assertTrue(trace_id)
        self.assertEqual(revision, 1)
        win2 = VoucherEditWindow(
            order_no="TRACE-SAVE", background_pdf_bytes=b"",
            voucher_nos=["V1"])
        self.addCleanup(win2.deleteLater)
        restored = win2.serialize_objects()[0]
        self.assertEqual(restored["id"], object_id)
        self.assertTrue(restored["font_italic"])

    def test_main_save_notification_invalidates_snapshot_generation_and_pixmap(self) -> None:
        from app.voucher_window import VoucherWindow

        worker = mock.Mock()
        editor = SimpleNamespace(
            _background_load_generation=4,
            invalidate_preview_cache=mock.Mock(),
        )
        cached = {
            "edit_objects": [{"id": "old"}],
            "pages": [{
                "edit_objects": [{"id": "old"}],
                "_edit_objects_sha256": "old",
                "_edit_data_revision": 1,
            }],
        }
        row = SimpleNamespace(
            order_input=SimpleNamespace(text=lambda: "TRACE"),
            cached_olap=cached,
        )
        window = SimpleNamespace(
            _edit_render_context_by_order={},
            _editor_load_generation=9,
            _editor_workers={9: (object(), worker)},
            _rows=[row],
            _editor_window=editor,
        )
        VoucherWindow._on_voucher_edit_saved(
            window, "TRACE", "V1", "b" * 64, "trace-id", 8)
        self.assertEqual(window._editor_load_generation, 10)
        worker.cancel.assert_called_once_with()
        self.assertNotIn("edit_objects", cached)
        self.assertNotIn("edit_objects", cached["pages"][0])
        self.assertEqual(editor._background_load_generation, 5)
        editor.invalidate_preview_cache.assert_called_once_with("V1")
        self.assertEqual(
            window._edit_render_context_by_order["TRACE"]["trace_id"],
            "trace-id")

    def test_save_to_worker_pdf_preview_e2e_uses_latest_style_and_trace(self) -> None:
        from app import voucher_service
        from app.voucher_edit_window import VoucherEditWindow
        from app.voucher_preview_window import VoucherPrintPreviewWindow

        win = VoucherEditWindow(
            order_no="TRACE-FULL-E2E", background_pdf_bytes=b"",
            voucher_nos=["V1"])
        self.addCleanup(win.deleteLater)
        item = win.add_text_at(
            QPointF(80.0, 80.0), text="TEST", font_size=18.0)
        item.font_family = "Helvetica"
        item.apply_text_style(bold=True, italic=True, underline=True)
        object_id = item.obj_id
        emitted: list[tuple] = []
        win.voucherEditSaved.connect(lambda *args: emitted.append(args))
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        order_no, _voucher_no, content_hash, trace_id, revision = emitted[0]
        stale = dict(win.serialize_objects()[0], font_italic=False, italic=False)
        data = {"pages": [{
            "order_no": order_no, "voucher_no": "V1",
            "customer_name": "顧客", "details": [],
            "edit_objects": [stale],
        }]}
        with self.assertLogs("tks_to_kintone_app", level="INFO") as captured:
            pdf = voucher_service.build_vouchers_pdf_bytes(
                ["03"], data, edit_render_trace_id=trace_id,
                reload_edit_objects=True, bypass_preview_cache=True)
        logs = "\n".join(captured.output)
        self.assertIn(
            f"event=voucher_pdf_worker_input trace_id={trace_id} "
            f"object_id={object_id}", logs)
        self.assertIn("italic=True", logs)
        self.assertIn(
            f"event=voucher_edit_qt_glyph_path trace_id={trace_id} "
            f"object_id={object_id}", logs)
        self.assertIn("font_italic=True", logs)
        self.assertIn(f"edit_data_revision={revision}", logs)
        self.assertIn(f"edit_objects_sha256={content_hash}", logs)
        pdf_hash = hashlib.sha256(pdf).hexdigest()
        with self.assertLogs(
                "app.voucher_preview_window", level="INFO") as preview_logs:
            preview = VoucherPrintPreviewWindow(
                pdf, edit_render_trace_id=trace_id,
                edit_objects_sha256=content_hash, preview_cache_hit=False)
        self.addCleanup(preview.deleteLater)
        self.assertEqual(preview.pdf_sha256, pdf_hash)
        shown = "\n".join(preview_logs.output)
        self.assertIn(
            f"event=voucher_preview_pixmap_shown trace_id={trace_id} "
            f"pdf_sha256={pdf_hash}", shown)
        self.assertIn("cache_hit=False", shown)
        stream = pypdf.PdfReader(
            io.BytesIO(pdf)).pages[0].get_contents().get_data()
        self.assertIn(b"f*", stream)

    def test_switch_voucher_keeps_independent_unsaved_objects(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(
            order_no="MULTI", background_pdf_bytes=b"",
            voucher_nos=[" 001 ", "A002", "001"])
        self.addCleanup(win.deleteLater)
        win._individual_voucher_radio.setChecked(True)
        win.add_text_at(QPointF(10, 20), text="伝票A")
        win.switch_voucher("A002")
        self.assertEqual(win.serialize_objects(), [])
        win.add_text_at(QPointF(30, 40), text="伝票B")
        win.switch_voucher("001")
        self.assertEqual([o["text"] for o in win.serialize_objects()], ["伝票A"])
        self.assertEqual(win.voucher_nos, ["001", "A002"])

    def test_edit_scope_defaults_to_common_and_combo_follows_mode(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(
            order_no="SCOPE-UI", background_pdf_bytes=b"",
            voucher_nos=["Z001", "Z002"])
        self.addCleanup(win.deleteLater)
        self.assertTrue(win._all_vouchers_radio.isChecked())
        self.assertFalse(win._voucher_combo.isEnabled())
        self.assertIn("すべての伝票No", win._all_vouchers_radio.toolTip())
        win._individual_voucher_radio.setChecked(True)
        self.assertTrue(win._voucher_combo.isEnabled())
        self.assertIn("選択した伝票Noだけ", win._individual_voucher_radio.toolTip())
        win.switch_voucher("Z002")
        win._all_vouchers_radio.setChecked(True)
        self.assertFalse(win._voucher_combo.isEnabled())
        self.assertEqual(win.current_voucher_no, "Z002")
        win._individual_voucher_radio.setChecked(True)
        self.assertEqual(win.current_voucher_no, "Z002")

    def test_single_unique_voucher_disables_scope_and_hides_notice(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        for values, expected in (([" 001 ", "001"], "001"), ([""], ""), ([], "")):
            with self.subTest(values=values):
                win = VoucherEditWindow(
                    order_no=f"SINGLE-{expected}", background_pdf_bytes=b"",
                    voucher_nos=values)
                self.addCleanup(win.deleteLater)
                self.assertEqual(win.voucher_nos, [expected])
                self.assertTrue(win._all_vouchers_radio.isChecked())
                self.assertFalse(win._all_vouchers_radio.isEnabled())
                self.assertFalse(win._individual_voucher_radio.isEnabled())
                self.assertFalse(win._voucher_combo.isEnabled())
                self.assertTrue(win._multiple_vouchers_notice.isHidden())
                self.assertIn("1件のため", win._all_vouchers_radio.toolTip())
                self.assertIn("1件のため", win._individual_voucher_radio.toolTip())

    def test_multiple_voucher_inline_notice_and_no_modal(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        with (mock.patch("app.voucher_edit_window.QMessageBox.information") as info,
              mock.patch("app.voucher_edit_window.QMessageBox.warning") as warning):
            win = VoucherEditWindow(
                order_no="MULTIPLE-NOTICE", background_pdf_bytes=b"",
                voucher_nos=["001", "002", "003"])
        self.addCleanup(win.deleteLater)
        self.assertTrue(win._all_vouchers_radio.isEnabled())
        self.assertTrue(win._individual_voucher_radio.isEnabled())
        self.assertFalse(win._multiple_vouchers_notice.isHidden())
        self.assertIn("複数の伝票Noがあります", win._multiple_vouchers_notice.text())
        self.assertIn("3件", win._multiple_vouchers_notice.text())
        self.assertIn("共通編集", win._multiple_vouchers_notice.toolTip())
        self.assertFalse(win._voucher_combo.isEnabled())
        win._individual_voucher_radio.setChecked(True)
        self.assertTrue(win._voucher_combo.isEnabled())
        info.assert_not_called()
        warning.assert_not_called()

    def test_voucher_count_change_is_non_destructive(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(
            order_no="COUNT-CHANGE", background_pdf_bytes=b"",
            voucher_nos=["Z001", "Z002"])
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10, 20), text="共通")
        win._individual_voucher_radio.setChecked(True)
        win.add_text_at(QPointF(30, 40), text="個別")
        individual_id = win.serialize_objects()[0]["id"]
        win.set_voucher_numbers([" Z001 ", "Z001"])
        self.assertEqual(win._edit_mode, "common")
        self.assertTrue(win._all_vouchers_radio.isChecked())
        self.assertFalse(win._all_vouchers_radio.isEnabled())
        self.assertEqual(win._voucher_objects["Z001"][0]["id"], individual_id)
        self.assertEqual([o["text"] for o in win.serialize_objects()], ["共通"])
        win.set_voucher_numbers(["Z001", "Z002"])
        self.assertTrue(win._all_vouchers_radio.isEnabled())
        self.assertFalse(win._multiple_vouchers_notice.isHidden())
        win._individual_voucher_radio.setChecked(True)
        self.assertEqual(win.serialize_objects()[0]["id"], individual_id)

    def test_multiple_voucher_notice_theme_reapplies(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        with mock.patch("app.voucher_edit_window.current_title_bar_is_dark", return_value=False):
            win = VoucherEditWindow(
                order_no="NOTICE-THEME", background_pdf_bytes=b"",
                voucher_nos=["A", "B"])
        self.addCleanup(win.deleteLater)
        light_style = win._multiple_vouchers_notice.styleSheet()
        self.assertIn("#fff3cd", light_style)
        self.assertIn("#5f4300", light_style)
        self.assertIn("font-weight: bold", light_style)
        with mock.patch("app.voucher_edit_window.current_title_bar_is_dark", return_value=True):
            win._apply_toolbar_theme()
        dark_style = win._multiple_vouchers_notice.styleSheet()
        self.assertIn("#5a4618", dark_style)
        self.assertIn("#fff1b8", dark_style)
        self.assertNotEqual(light_style, dark_style)

    def test_mode_switch_is_non_destructive_and_common_is_readonly_in_individual(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(
            order_no="SCOPE-DATA", background_pdf_bytes=b"",
            voucher_nos=["Z001", "Z002"])
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10, 20), text="共通")
        common_id = win.serialize_objects()[0]["id"]
        win._individual_voucher_radio.setChecked(True)
        readonly = [item for item in win._scene.items()
                    if getattr(item, "_COMMON_READONLY", False)]
        self.assertEqual(len(readonly), 1)
        self.assertFalse(readonly[0].flags() &
                         readonly[0].GraphicsItemFlag.ItemIsSelectable)
        win.add_text_at(QPointF(30, 40), text="個別")
        individual_id = win.serialize_objects()[0]["id"]
        win._all_vouchers_radio.setChecked(True)
        self.assertEqual(win.serialize_objects()[0]["id"], common_id)
        win._individual_voucher_radio.setChecked(True)
        self.assertEqual(win.serialize_objects()[0]["id"], individual_id)

    def test_common_and_individual_undo_histories_are_independent(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(
            order_no="SCOPE-HISTORY", background_pdf_bytes=b"",
            voucher_nos=["Z001", "Z002"])
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10, 20), text="共通")
        win.commit_history()
        win._individual_voucher_radio.setChecked(True)
        win.add_text_at(QPointF(30, 40), text="個別")
        win.commit_history()
        win.undo()
        self.assertEqual(win.serialize_objects(), [])
        win._all_vouchers_radio.setChecked(True)
        self.assertEqual([o["text"] for o in win.serialize_objects()], ["共通"])
        win.undo()
        self.assertEqual(win.serialize_objects(), [])
        win._individual_voucher_radio.setChecked(True)
        self.assertEqual(win.serialize_objects(), [])

    def test_copy_between_common_and_individual_reissues_ids(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(
            order_no="SCOPE-COPY", background_pdf_bytes=b"",
            voucher_nos=["Z001", "Z002"])
        self.addCleanup(win.deleteLater)
        common_item = win.add_text_at(QPointF(10, 20), text="共通元")
        common_item.setSelected(True)
        common_id = win.serialize_objects()[0]["id"]
        self.assertEqual(win.copy_objects_to_vouchers(
            ["Z001"], selected_only=True), (1, 1))
        win._individual_voucher_radio.setChecked(True)
        copied = win.serialize_objects()[0]
        self.assertNotEqual(copied["id"], common_id)
        copied_item = win.edit_items()[0]
        copied_item.setSelected(True)
        self.assertEqual(win.copy_objects_to_common(selected_only=True), 1)
        win._all_vouchers_radio.setChecked(True)
        self.assertEqual(len(win.serialize_objects()), 2)
        self.assertEqual(len({o["id"] for o in win.serialize_objects()}), 2)

    def test_copy_selected_to_multiple_vouchers_reissues_id(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(
            order_no="COPY", background_pdf_bytes=b"",
            voucher_nos=["A001", "A002", "A003"])
        self.addCleanup(win.deleteLater)
        win._individual_voucher_radio.setChecked(True)
        item = win.add_text_at(QPointF(10, 20), text="コピー元")
        item.setSelected(True)
        source_id = win.serialize_objects()[0]["id"]
        self.assertEqual(
            win.copy_objects_to_vouchers(["A002", "A003"], selected_only=True),
            (1, 2))
        win.switch_voucher("A002")
        copied = win.serialize_objects()[0]
        self.assertNotEqual(copied["id"], source_id)
        copied["text"] = "モデル変更"
        win.switch_voucher("A001")
        self.assertEqual(win.serialize_objects()[0]["text"], "コピー元")

    def test_copy_all_append_replace_and_destination_undo(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(
            order_no="COPY2", background_pdf_bytes=b"",
            voucher_nos=["A001", "A002"])
        self.addCleanup(win.deleteLater)
        win._individual_voucher_radio.setChecked(True)
        win.add_text_at(QPointF(1, 2), text="source")
        win.switch_voucher("A002")
        win.add_text_at(QPointF(3, 4), text="existing")
        win.switch_voucher("A001")
        win.copy_objects_to_vouchers(["A002"], selected_only=False)
        win.switch_voucher("A002")
        self.assertEqual({o["text"] for o in win.serialize_objects()}, {"source", "existing"})
        win.undo()
        self.assertEqual([o["text"] for o in win.serialize_objects()], ["existing"])
        win.switch_voucher("A001")
        win.copy_objects_to_vouchers(
            ["A002"], selected_only=False, replace=True)
        win.switch_voucher("A002")
        self.assertEqual([o["text"] for o in win.serialize_objects()], ["source"])

    def test_multi_voucher_save_and_reload(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(
            order_no="SAVE-MULTI", background_pdf_bytes=b"",
            voucher_nos=["A001", "A002"])
        self.addCleanup(win.deleteLater)
        win._individual_voucher_radio.setChecked(True)
        win.add_text_at(QPointF(1, 2), text="A")
        win.switch_voucher("A002")
        win.add_text_at(QPointF(3, 4), text="B")
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        win2 = VoucherEditWindow(
            order_no="SAVE-MULTI", background_pdf_bytes=b"",
            voucher_nos=["A001", "A002"])
        self.addCleanup(win2.deleteLater)
        win2._individual_voucher_radio.setChecked(True)
        self.assertEqual(win2.serialize_objects()[0]["text"], "A")
        win2.switch_voucher("A002")
        self.assertEqual(win2.serialize_objects()[0]["text"], "B")

    def test_switch_voucher_changes_background_and_uses_pixmap_cache(self) -> None:
        from PySide6.QtGui import QColor, QPixmap
        from app.voucher_edit_window import VoucherEditWindow

        def render(data: bytes):
            pixmap = QPixmap(20, 20)
            pixmap.fill(QColor("red" if data == b"pdf-a" else "blue"))
            return pixmap

        def size(data: bytes):
            return (100.0, 200.0) if data == b"pdf-a" else (300.0, 400.0)

        with (mock.patch("app.voucher_edit_window.render_order_sheet_background",
                         side_effect=render) as renderer,
              mock.patch("app.voucher_edit_window.pdf_page_size", side_effect=size)):
            win = VoucherEditWindow(
                order_no="PREVIEW", background_pdf_bytes=b"pdf-a",
                voucher_nos=["A001", "A002"],
                background_pdf_by_voucher={"A001": b"pdf-a", "A002": b"pdf-b"})
            self.addCleanup(win.deleteLater)
            win._individual_voucher_radio.setChecked(True)
            win.add_text_at(QPointF(1, 2), text="A object")
            self.assertEqual(win.background_items()[0].data(1), "A001")
            self.assertEqual(win._scene.sceneRect().size().toTuple(), (100.0, 200.0))
            win.switch_voucher("A002")
            self.assertEqual(win.background_items()[0].data(1), "A002")
            self.assertEqual(win.serialize_objects(), [])
            self.assertEqual(win._scene.sceneRect().size().toTuple(), (300.0, 400.0))
            win.add_text_at(QPointF(3, 4), text="B object")
            win.switch_voucher("A001")
            self.assertEqual(win.background_items()[0].data(1), "A001")
            self.assertEqual(win.serialize_objects()[0]["text"], "A object")
            win.switch_voucher("A002")
            self.assertEqual(renderer.call_count, 2)

    def test_preview_failure_does_not_leave_previous_voucher_background(self) -> None:
        from PySide6.QtGui import QColor, QPixmap
        from app.voucher_edit_window import VoucherEditWindow

        good = QPixmap(20, 20)
        good.fill(QColor("red"))
        with (mock.patch("app.voucher_edit_window.render_order_sheet_background",
                         side_effect=lambda data: good if data == b"good" else None),
              mock.patch("app.voucher_edit_window.pdf_page_size",
                         return_value=(100.0, 200.0))):
            win = VoucherEditWindow(
                order_no="PREVIEW-FAIL", background_pdf_bytes=b"good",
                voucher_nos=["001", "002"],
                background_pdf_by_voucher={"001": b"good", "002": b"broken"})
            self.addCleanup(win.deleteLater)
            win.switch_voucher("002")
            backgrounds = win.background_items()
            self.assertTrue(backgrounds)
            self.assertTrue(all(item.data(1) == "002" for item in backgrounds))
            self.assertEqual(backgrounds[0].data(2), "error")
            self.assertEqual(win.current_voucher_no, "002")

    def test_preview_cache_invalidation_and_target_voucher(self) -> None:
        from PySide6.QtGui import QColor, QPixmap
        from app.voucher_edit_window import VoucherEditWindow

        def render(_data):
            pixmap = QPixmap(10, 10)
            pixmap.fill(QColor("white"))
            return pixmap

        with (mock.patch("app.voucher_edit_window.render_order_sheet_background",
                         side_effect=render) as renderer,
              mock.patch("app.voucher_edit_window.pdf_page_size",
                         return_value=(100.0, 200.0))):
            win = VoucherEditWindow(
                order_no="CACHE", background_pdf_bytes=b"a",
                voucher_nos=["A", "B"],
                background_pdf_by_voucher={"A": b"a", "B": b"b"},
                preview_target_voucher="04")
            self.addCleanup(win.deleteLater)
            self.assertEqual(win._active_preview_cache_key[2], "04")
            win.switch_voucher("B")
            self.assertEqual(renderer.call_count, 2)
            win.invalidate_preview_cache(
                "B", background_pdf_by_voucher={"A": b"a2", "B": b"b2"})
            win.switch_voucher("A")
            win.switch_voucher("B")
            # AもPDF bytesがa→a2へ変わったため、明示invalidate対象がBだけでも
            # SHA-256入りキーにより旧Pixmapを再利用しない。
            self.assertEqual(renderer.call_count, 4)
            self.assertEqual(win._active_preview_cache_key[4], 5)
            self.assertEqual(len(win._active_preview_cache_key[5]), 64)

    def test_fast_preview_switch_with_blank_voucher_is_safe(self) -> None:
        from PySide6.QtGui import QColor, QPixmap
        from app.voucher_edit_window import VoucherEditWindow

        pixmap = QPixmap(10, 10)
        pixmap.fill(QColor("white"))
        with (mock.patch("app.voucher_edit_window.render_order_sheet_background",
                         return_value=pixmap),
              mock.patch("app.voucher_edit_window.pdf_page_size",
                         return_value=(100.0, 200.0))):
            win = VoucherEditWindow(
                order_no="FAST", background_pdf_bytes=b"blank",
                voucher_nos=["", "0002"],
                background_pdf_by_voucher={"": b"blank", "0002": b"two"})
            self.addCleanup(win.deleteLater)
            for _ in range(10):
                win.switch_voucher("0002")
                win.switch_voucher("")
            self.assertEqual(win.current_voucher_no, "")
            self.assertEqual(win.background_items()[0].data(1), "__tks_empty_voucher_no__")

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
        self.assertGreaterEqual(obj["h"], item.font_size * 1.2)

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

    def _png_bytes(self) -> bytes:
        from PySide6.QtGui import QColor, QImage
        from app.voucher_edit_window import qimage_to_png_bytes

        image = QImage(20, 12, QImage.Format.Format_ARGB32)
        image.fill(QColor("#ffffff"))
        return qimage_to_png_bytes(image)

    def test_image_selection_does_not_show_left_image_processing_menu(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-left", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        image = win.add_image(self._png_bytes(), QRectF(10.0, 10.0, 20.0, 12.0))
        self.assertIsNotNone(image)
        win._select_only(image)
        win._update_image_action_buttons()
        self.assertIsNone(win._image_actions_label)
        self.assertIsNotNone(win._favorite_list)

    def test_image_context_menu_has_image_processing_menu(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-img-menu", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        image = win.add_image(self._png_bytes(), QRectF(10.0, 10.0, 20.0, 12.0))
        menu = win._build_object_context_menu(image)
        submenus = getattr(menu, "_submenus", [])
        self.assertTrue(any(m.objectName() == "image_processing_menu" for m in submenus))

    def test_non_image_context_menu_has_no_image_processing_menu(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-text-menu", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_at(QPointF(10.0, 10.0), text="abc")
        menu = win._build_object_context_menu(text)
        submenus = getattr(menu, "_submenus", [])
        self.assertFalse(any(m.objectName() == "image_processing_menu" for m in submenus))

    def test_image_processing_runs_from_context_menu(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-img-action", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        image = win.add_image(self._png_bytes(), QRectF(10.0, 10.0, 20.0, 12.0))
        with mock.patch.object(win, "_on_threshold_transparent") as threshold:
            menu = win._build_object_context_menu(image)
            image_menu = next(m for m in getattr(menu, "_submenus", [])
                              if m.objectName() == "image_processing_menu")
            action = next(a for a in image_menu.actions()
                          if a.objectName() == "transparent_background_action")
            action.trigger()
        threshold.assert_called_once()

    def test_image_object_can_be_added_to_favorites(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-image", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        image = win.add_image(self._png_bytes(), QRectF(10.0, 10.0, 20.0, 12.0))
        self.assertTrue(win.add_object_to_favorites(image))
        self.assertEqual(len(win._favorites), 1)
        self.assertEqual(win._favorites[0]["object"]["type"], "image")

    def test_text_object_can_be_added_to_favorites_and_reloaded(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-text", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_at(QPointF(10.0, 10.0), text="favorite")
        self.assertTrue(win.add_object_to_favorites(text))
        fav_id = win._favorites[0]["id"]

        win2 = VoucherEditWindow(order_no="fav-text-2", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        self.assertEqual(win2._favorites[0]["id"], fav_id)
        self.assertEqual(win2._favorites[0]["object"]["text"], "favorite")

    def test_favorite_can_be_removed(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-remove", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_at(QPointF(10.0, 10.0), text="favorite")
        win.add_object_to_favorites(text)
        fav_id = win._favorites[0]["id"]
        self.assertTrue(win.remove_favorite_object(fav_id))
        self.assertEqual(win._favorites, [])

    def test_favorite_drop_adds_separate_instance(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-drop", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_at(QPointF(10.0, 10.0), text="favorite")
        original_id = text.obj_id
        win.add_object_to_favorites(text)
        fav_id = win._favorites[0]["id"]
        self.assertTrue(win.drop_favorite_object(fav_id, QPointF(100.0, 120.0)))
        objects = win.serialize_objects()
        ids = {obj["id"] for obj in objects}
        self.assertEqual(len(objects), 2)
        self.assertIn(original_id, ids)
        self.assertEqual(len(ids), 2)
        dropped = [obj for obj in objects if obj["id"] != original_id][0]
        self.assertEqual(dropped["text"], "favorite")

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
        """保存して閉じる等の主要アクションと、図形メニュー（線/四角/丸）がある（要件5・8）。"""
        from PySide6.QtWidgets import QToolBar

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="t1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        actions = [a.text() for a in win._edit_header_widget.actions()]
        for label in ("選択", "テキスト", "保存", "保存して閉じる", "閉じる"):
            self.assertIn(label, actions)
        # 図形6種は「図形」メニューへまとめた（要件5）。
        menu_labels = [a.text() for a in win._shape_menu.actions()]
        for label in ("線", "矢印", "両矢印", "二重線", "四角", "丸"):
            self.assertIn(label, menu_labels)

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
        bar = win._edit_header_widget
        text_button = bar.widgetForAction(win._tool_actions[TOOL_TEXT])
        self.assertIsInstance(text_button, QToolButton)
        self.assertTrue(text_button.property("editToolButton"))
        self.assertTrue(win._tool_actions[TOOL_TEXT].isChecked())
        self.assertIn('QToolButton[editToolButton="true"]:checked', EDIT_TOOLBAR_STYLE)
        self.assertIn("background-color: #0d6efd", EDIT_TOOLBAR_STYLE)
        self.assertIn("color: #ffffff", EDIT_TOOLBAR_STYLE)
        self.assertIn("border: 2px solid #66b2ff", EDIT_TOOLBAR_STYLE)
        self.assertIn(":checked:disabled", EDIT_TOOLBAR_STYLE)

    def test_toolbar_dark_theme_colors_applied(self) -> None:
        """ダークテーマでは上部メニューに文字色・背景色が明示され読める（要件6）。"""
        from PySide6.QtWidgets import QToolBar

        from app.voucher_edit_window import (
            EDIT_SHAPE_MENU_DARK_STYLE,
            EDIT_TOOLBAR_CONTAINER_DARK_BG,
            EDIT_TOOLBAR_DARK_STYLE,
            VoucherEditWindow,
        )

        with mock.patch(
            "app.voucher_edit_window.current_title_bar_is_dark",
            return_value=True,
        ):
            win = VoucherEditWindow(order_no="drk1", background_pdf_bytes=b"")
            self.addCleanup(win.deleteLater)
            bar = win._edit_header_widget
            # 通常状態の文字色が背景色と別に指定されている。
            self.assertIn("color: #f0f0f0", EDIT_TOOLBAR_DARK_STYLE)
            self.assertIn("background-color: #3a4047", EDIT_TOOLBAR_DARK_STYLE)
            # ダーク用の配色がツールバーへ適用されている。
            self.assertIn(EDIT_TOOLBAR_DARK_STYLE.replace("QToolBar", "#voucher_edit_header").strip()[:20], bar.styleSheet())
            # コンテナ背景・図形メニューもダーク配色。
            self.assertIn(
                EDIT_TOOLBAR_CONTAINER_DARK_BG,
                win._main_toolbar_container.styleSheet(),
            )
            self.assertEqual(win._shape_menu.styleSheet(), EDIT_SHAPE_MENU_DARK_STYLE)
            # disabled でも背景と同化しない別色を指定している。
            self.assertIn("color: #9aa3ac", EDIT_TOOLBAR_DARK_STYLE)

    def test_toolbar_light_theme_keeps_default(self) -> None:
        """ライトテーマでは上部メニューを明示的なライト配色にし黒っぽくしない（要件6）。"""
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QToolBar

        from app.voucher_edit_window import (
            EDIT_SHAPE_MENU_LIGHT_STYLE,
            EDIT_TOOLBAR_CONTAINER_LIGHT_BG,
            EDIT_TOOLBAR_LIGHT_STYLE,
            VoucherEditWindow,
        )

        with mock.patch(
            "app.voucher_edit_window.current_title_bar_is_dark",
            return_value=False,
        ):
            win = VoucherEditWindow(order_no="lgt1", background_pdf_bytes=b"")
            self.addCleanup(win.deleteLater)
            container_ss = win._main_toolbar_container.styleSheet()
            self.assertIn(EDIT_TOOLBAR_CONTAINER_LIGHT_BG, container_ss)
            # コンテナ背景は明るい色（黒系ではない）。
            self.assertGreater(QColor(EDIT_TOOLBAR_CONTAINER_LIGHT_BG).lightness(), 200)
            # 図形メニューはライト配色（空ではなく明示指定）で黒っぽくならない。
            self.assertEqual(win._shape_menu.styleSheet(), EDIT_SHAPE_MENU_LIGHT_STYLE)
            self.assertIn("#ffffff", EDIT_SHAPE_MENU_LIGHT_STYLE)
            # ツールバー本体にライト配色が適用され、ダーク配色は含まれない。
            bar = win._edit_header_widget
            bar_ss = bar.styleSheet()
            self.assertIn(EDIT_TOOLBAR_LIGHT_STYLE.replace("QToolBar", "#voucher_edit_header").strip()[:20], bar_ss)
            self.assertNotIn("#3a4047", bar_ss)  # ダーク用ボタン背景が残っていない
            self.assertNotIn("#2b2f33", bar_ss)  # ダーク用ツールバー背景が残っていない
            # ライト用のボタン文字色・背景色が明示されている。
            self.assertIn("color: #202124", EDIT_TOOLBAR_LIGHT_STYLE)
            self.assertIn("background-color: #ffffff", EDIT_TOOLBAR_LIGHT_STYLE)

    def test_toolbar_theme_switches_light_after_dark(self) -> None:
        """ダーク適用後にライトへ切り替えると黒配色が残らず再適用される（要件6）。"""
        from PySide6.QtWidgets import QToolBar

        from app.voucher_edit_window import (
            EDIT_TOOLBAR_LIGHT_STYLE,
            VoucherEditWindow,
        )

        with mock.patch(
            "app.voucher_edit_window.current_title_bar_is_dark",
            return_value=True,
        ):
            win = VoucherEditWindow(order_no="sw1", background_pdf_bytes=b"")
            self.addCleanup(win.deleteLater)
            bar = win._edit_header_widget
            self.assertIn("#3a4047", bar.styleSheet())  # 初期はダーク

        # テーマをライトへ切り替えて再適用する。
        with mock.patch(
            "app.voucher_edit_window.current_title_bar_is_dark",
            return_value=False,
        ):
            win._apply_toolbar_theme()
            bar_ss = bar.styleSheet()
            self.assertNotIn("#3a4047", bar_ss)  # ダーク配色は消えている
            self.assertIn(EDIT_TOOLBAR_LIGHT_STYLE.replace("QToolBar", "#voucher_edit_header").strip()[:20], bar_ss)

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
        from PySide6.QtWidgets import QScrollArea, QToolBar

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ts1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        actions = [a.text() for a in win._edit_header_widget.actions()]
        for label in ("画像挿入", "貼り付け", "全画面", "保存して閉じる"):
            self.assertIn(label, actions)
        self.assertIs(win._edit_header_widget, win._main_toolbar)

    def test_toolbar_scroll_area_does_not_require_horizontal_scroll(self) -> None:
        from PySide6.QtWidgets import QScrollArea

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ts-scroll", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        scroll = win._edit_header_widget
        self.assertTrue(scroll.widgetResizable())
        self.assertEqual(scroll.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertEqual(scroll.verticalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertEqual(win._main_toolbar.minimumWidth(), 0)

    def test_left_pane_is_vertical_scroll_area(self) -> None:
        from PySide6.QtWidgets import QScrollArea

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="left-scroll", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        scroll = win.findChild(QScrollArea, "templatePanelScroll")
        self.assertIsNotNone(scroll)
        self.assertIs(scroll.widget(), win._template_panel)
        self.assertTrue(scroll.widgetResizable())
        self.assertEqual(
            scroll.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        self.assertEqual(
            scroll.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        # 左ペインをさらに約1.5cm広げた（基準190→250）。scroll 幅も一致して広がる。
        self.assertLessEqual(scroll.maximumWidth(), 250)
        self.assertGreaterEqual(scroll.maximumWidth(), 250)

    def test_left_pane_has_right_margin_for_scrollbar(self) -> None:
        """左ペイン内側レイアウトに、縦スクロールバー分＋余白の右marginがある（要件3）。"""
        from PySide6.QtWidgets import QStyle

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="left-margin", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        layout = win._template_panel.layout()
        margins = layout.contentsMargins()
        scrollbar_width = win.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        # 右marginは 8px 以上あり、かつ左marginよりスクロールバー分以上広い。
        self.assertGreaterEqual(margins.right(), 8)
        self.assertGreaterEqual(margins.right() - margins.left(), scrollbar_width)
        # パネル幅（左ペイン幅）は前回広げた状態（250）を維持する。
        self.assertEqual(win._template_panel.width(), 250)
        # ボタンが収まる内容幅はパネル幅より右marginぶん狭い（スクロールバーに重ならない）。
        content_width = win._template_panel.width() - margins.left() - margins.right()
        self.assertLess(content_width, win._template_panel.width())
        self.assertGreater(content_width, 0)

    def test_show_opens_maximized(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="ts-max", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        with mock.patch.object(win, "showMaximized") as show_maximized:
            win.show()
        show_maximized.assert_called_once_with()

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
        bar = win._edit_header_widget
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
        actions = [a.text() for a in win._edit_header_widget.actions()]
        self.assertNotIn("座標マーカー", actions)
        # 内部関数はテスト用に残る。
        self.assertTrue(hasattr(win, "add_debug_markers"))

    def test_toolbar_buttons_have_border_radius_style(self) -> None:
        from PySide6.QtWidgets import QToolBar

        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="cm2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        style = win._edit_header_widget.styleSheet()
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


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditUndoRedoButtons(unittest.TestCase):
    """上部ツールバーのアンドゥ・リドゥボタン（要件1）。"""

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

    def test_undo_redo_buttons_exist_with_icons(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="btn1", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertIsNotNone(win._undo_action)
        self.assertIsNotNone(win._redo_action)
        self.assertFalse(win._undo_action.icon().isNull())
        self.assertFalse(win._redo_action.icon().isNull())

    def test_buttons_disabled_when_no_history(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="btn2", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertFalse(win._undo_action.isEnabled())
        self.assertFalse(win._redo_action.isEnabled())

    def test_button_state_follows_history(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="btn3", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.add_text_at(QPointF(10.0, 10.0), text="a", font_size=12.0)
        win.commit_history()
        self.assertTrue(win._undo_action.isEnabled())
        self.assertFalse(win._redo_action.isEnabled())
        win.undo()
        self.assertFalse(win._undo_action.isEnabled())
        self.assertTrue(win._redo_action.isEnabled())
        win.redo()
        self.assertTrue(win._undo_action.isEnabled())
        self.assertFalse(win._redo_action.isEnabled())

    def test_history_limited_to_50(self) -> None:
        from app.voucher_edit_window import HISTORY_LIMIT, VoucherEditWindow

        win = VoucherEditWindow(order_no="btn4", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        for i in range(HISTORY_LIMIT + 20):
            win.add_text_at(QPointF(10.0 + i, 10.0 + i), text=f"t{i}", font_size=12.0)
            win.commit_history()
        self.assertLessEqual(len(win._history), HISTORY_LIMIT)

    def test_favorite_drop_undo_redo(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="btn5", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_at(QPointF(10.0, 10.0), text="fav", font_size=12.0)
        win.commit_history()
        win.add_object_to_favorites(text)
        fav_id = win._favorites[0]["id"]
        base = len(win.serialize_objects())
        self.assertTrue(win.drop_favorite_object(fav_id, QPointF(80.0, 80.0)))
        self.assertEqual(len(win.serialize_objects()), base + 1)
        win.undo()
        self.assertEqual(len(win.serialize_objects()), base)
        win.redo()
        self.assertEqual(len(win.serialize_objects()), base + 1)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditReflectTemplateLimit(unittest.TestCase):
    """反映先テンプレ登録の上限8個（要件2）。"""

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

    def _seed_user_templates(self, extra: int) -> None:
        from app.voucher_edit_templates import save_user_templates

        templates = [
            {"name": f"ユーザー{i}", "target_vouchers": ["03"], "color": "#607d8b", "badge": "U"}
            for i in range(extra)
        ]
        save_user_templates(templates)

    def _register_new(self, win, name: str):
        from PySide6.QtWidgets import QDialog

        with mock.patch("app.voucher_edit_window._TemplateRegisterDialog") as Dlg, \
                mock.patch("app.voucher_edit_window.QMessageBox.information") as info:
            inst = Dlg.return_value
            inst.exec.return_value = QDialog.DialogCode.Accepted
            inst.template.return_value = {
                "name": name, "target_vouchers": ["03"],
                "color": "#607d8b", "badge": "N",
            }
            win._on_register_template()
        return info

    def test_label_shows_count_over_max(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        # 組み込み4個 + ユーザー2個 = 6個。
        self._seed_user_templates(2)
        win = VoucherEditWindow(order_no="rt-label", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertEqual(win._reflect_count_label.text(), "6/8")

    def test_can_add_when_below_limit(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        # 組み込み4個 + ユーザー3個 = 7個（追加可能）。
        self._seed_user_templates(3)
        win = VoucherEditWindow(order_no="rt-7", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertEqual(win._reflect_template_count(), 7)
        info = self._register_new(win, "追加テンプレ")
        info.assert_not_called()
        self.assertEqual(win._reflect_template_count(), 8)
        self.assertEqual(win._reflect_count_label.text(), "8/8")

    def test_cannot_add_when_at_limit(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        # 組み込み4個 + ユーザー4個 = 8個（追加不可）。
        self._seed_user_templates(4)
        win = VoucherEditWindow(order_no="rt-8", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertEqual(win._reflect_template_count(), 8)
        info = self._register_new(win, "溢れテンプレ")
        info.assert_called_once()
        self.assertEqual(win._reflect_template_count(), 8)

    def test_deleting_allows_adding_again(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        from PySide6.QtWidgets import QMessageBox

        self._seed_user_templates(4)  # 合計8個
        win = VoucherEditWindow(order_no="rt-del", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        with mock.patch("app.voucher_edit_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes):
            win._delete_template("ユーザー0")
        self.assertEqual(win._reflect_template_count(), 7)
        info = self._register_new(win, "再追加")
        info.assert_not_called()
        self.assertEqual(win._reflect_template_count(), 8)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditFavoriteLimit(unittest.TestCase):
    """お気に入り登録の上限20個と固定表示枠（要件2・4）。"""

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

    def _add_n_favorites(self, win, n: int) -> None:
        for i in range(n):
            item = win.add_text_at(QPointF(10.0 + i, 10.0 + i), text=f"f{i}", font_size=12.0)
            with mock.patch("app.voucher_edit_window.QMessageBox.information"):
                win.add_object_to_favorites(item)

    def test_max_favorite_objects_is_20(self) -> None:
        from app.voucher_edit_window import MAX_FAVORITE_OBJECTS

        self.assertEqual(MAX_FAVORITE_OBJECTS, 20)

    def test_can_add_below_limit(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-19", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self._add_n_favorites(win, 19)
        self.assertEqual(len(win._favorites), 19)
        item = win.add_text_at(QPointF(200.0, 200.0), text="20th", font_size=12.0)
        with mock.patch("app.voucher_edit_window.QMessageBox.information") as info:
            self.assertTrue(win.add_object_to_favorites(item))
        info.assert_not_called()
        self.assertEqual(len(win._favorites), 20)

    def test_cannot_add_at_limit(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-20", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self._add_n_favorites(win, 20)
        self.assertEqual(len(win._favorites), 20)
        item = win.add_text_at(QPointF(200.0, 200.0), text="21th", font_size=12.0)
        with mock.patch("app.voucher_edit_window.QMessageBox.information") as info:
            self.assertFalse(win.add_object_to_favorites(item))
        info.assert_called_once()
        # 上限メッセージが「最大20個」であること。
        message = " ".join(str(a) for a in info.call_args.args)
        self.assertIn("20", message)
        self.assertEqual(len(win._favorites), 20)

    def test_label_shows_count(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-label", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertEqual(win._favorite_count_label.text(), "0/20")
        self._add_n_favorites(win, 6)
        self.assertEqual(win._favorite_count_label.text(), "6/20")

    def test_delete_allows_adding_again(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-del", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self._add_n_favorites(win, 20)
        removed_id = win._favorites[0]["id"]
        self.assertTrue(win.remove_favorite_object(removed_id))
        self.assertEqual(len(win._favorites), 19)
        item = win.add_text_at(QPointF(300.0, 300.0), text="again", font_size=12.0)
        with mock.patch("app.voucher_edit_window.QMessageBox.information") as info:
            self.assertTrue(win.add_object_to_favorites(item))
        info.assert_not_called()
        self.assertEqual(len(win._favorites), 20)

    def test_favorite_list_has_fixed_height(self) -> None:
        from app.voucher_edit_window import FAVORITE_LIST_FIXED_HEIGHT, VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-h", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        widget = win._favorite_list
        self.assertEqual(widget.minimumHeight(), widget.maximumHeight())
        self.assertEqual(widget.maximumHeight(), FAVORITE_LIST_FIXED_HEIGHT)

    def test_fixed_height_maintained_when_empty(self) -> None:
        from app.voucher_edit_window import FAVORITE_LIST_FIXED_HEIGHT, VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-empty", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertEqual(win._favorite_list.count(), 0)
        self.assertEqual(win._favorite_list.maximumHeight(), FAVORITE_LIST_FIXED_HEIGHT)

    def test_all_20_displayed(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-all", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self._add_n_favorites(win, 20)
        self.assertEqual(win._favorite_list.count(), 20)

    def test_drag_and_drop_still_works(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="fav-dnd", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_at(QPointF(10.0, 10.0), text="dnd", font_size=12.0)
        win.add_object_to_favorites(text)
        fav_id = win._favorites[0]["id"]
        before = len(win.serialize_objects())
        self.assertTrue(win.drop_favorite_object(fav_id, QPointF(120.0, 120.0)))
        self.assertEqual(len(win.serialize_objects()), before + 1)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditDuplicateContextMenu(unittest.TestCase):
    """オブジェクト右クリックメニューの「複製」（要件1）。"""

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

    def _png_bytes(self) -> bytes:
        from PySide6.QtGui import QColor, QImage
        from app.voucher_edit_window import qimage_to_png_bytes

        image = QImage(20, 12, QImage.Format.Format_ARGB32)
        image.fill(QColor("#ffffff"))
        return qimage_to_png_bytes(image)

    def _duplicate_actions(self, menu):
        return [a for a in menu.actions() if a.objectName() == "duplicate_action"]

    def test_image_menu_has_single_duplicate(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="dup-img", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        image = win.add_image(self._png_bytes(), QRectF(10.0, 10.0, 20.0, 12.0))
        menu = win._build_object_context_menu(image)
        dups = self._duplicate_actions(menu)
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0].text(), "複製")
        # 画像処理サブメニューは維持される。
        submenus = getattr(menu, "_submenus", [])
        self.assertTrue(any(m.objectName() == "image_processing_menu" for m in submenus))

    def test_text_menu_has_single_duplicate(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="dup-text", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_at(QPointF(10.0, 10.0), text="abc")
        menu = win._build_object_context_menu(text)
        dups = self._duplicate_actions(menu)
        self.assertEqual(len(dups), 1)
        # 非画像には画像処理メニューを出さない仕様を維持。
        submenus = getattr(menu, "_submenus", [])
        self.assertFalse(any(m.objectName() == "image_processing_menu" for m in submenus))

    def test_duplicate_increases_count_and_selects_new(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="dup-count", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_at(QPointF(30.0, 30.0), text="dup")
        win.commit_history()
        self.assertEqual(len(win.serialize_objects()), 1)
        self.assertTrue(win.duplicate_object(text))
        objs = win.serialize_objects()
        self.assertEqual(len(objs), 2)
        # 複製後の新オブジェクトが選択状態になる。
        selected = win._scene.selectedItems()
        self.assertEqual(len(selected), 1)
        self.assertNotEqual(getattr(selected[0], "obj_id", None), text.obj_id)

    def test_duplicate_undo_redo(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="dup-undo", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        text = win.add_text_at(QPointF(30.0, 30.0), text="dup")
        win.commit_history()
        self.assertTrue(win.duplicate_object(text))
        self.assertEqual(len(win.serialize_objects()), 2)
        win.undo()
        self.assertEqual(len(win.serialize_objects()), 1)
        win.redo()
        self.assertEqual(len(win.serialize_objects()), 2)

    def test_duplicate_action_triggered_from_menu(self) -> None:
        from PySide6.QtCore import QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="dup-trigger", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        rect = win.add_rect(QRectF(20.0, 20.0, 40.0, 30.0), text="r")
        win.commit_history()
        menu = win._build_object_context_menu(rect)
        action = self._duplicate_actions(menu)[0]
        action.trigger()
        self.assertEqual(len(win.serialize_objects()), 2)

    def _make_item(self, win, kind):
        from PySide6.QtCore import QPointF, QRectF

        if kind == "image":
            return win.add_image(self._png_bytes(), QRectF(10.0, 10.0, 20.0, 12.0))
        if kind == "text":
            return win.add_text_at(QPointF(10.0, 10.0), text="abc")
        if kind == "rect":
            return win.add_rect(QRectF(10.0, 10.0, 20.0, 12.0))
        if kind == "ellipse":
            return win.add_ellipse(QRectF(10.0, 10.0, 20.0, 12.0))
        if kind == "line":
            return win.add_line(QPointF(10.0, 10.0), QPointF(40.0, 40.0))
        raise ValueError(kind)

    # モーダルダイアログを開くアクションはテストでトリガーしない（ハング防止）。
    _DIALOG_ACTIONS = {"threshold_settings_action"}

    def _trigger_all_enabled(self, menu):
        for a in list(menu.actions()):
            sub = a.menu()
            if sub is not None:
                self._trigger_all_enabled(sub)
            elif (
                not a.isSeparator()
                and a.isEnabled()
                and a.objectName() not in self._DIALOG_ACTIONS
            ):
                a.trigger()

    def test_context_menu_all_types_do_not_crash(self) -> None:
        # 画像・テキスト・図形・線すべてで右クリックメニューを作れ、落ちない。
        from app.voucher_edit_window import VoucherEditWindow

        for kind in ("image", "text", "rect", "ellipse", "line"):
            win = VoucherEditWindow(order_no=f"rc-{kind}", background_pdf_bytes=b"")
            self.addCleanup(win.deleteLater)
            item = self._make_item(win, kind)
            menu = win._build_object_context_menu(item)
            self.assertGreaterEqual(len(menu.actions()), 1)

    def test_context_menu_action_on_deleted_item_does_not_crash(self) -> None:
        # 削除直後の stale item を参照するアクションを叩いても落ちない（要件2）。
        from app.voucher_edit_window import VoucherEditWindow

        for kind in ("image", "text", "rect", "ellipse", "line"):
            win = VoucherEditWindow(order_no=f"rc-del-{kind}", background_pdf_bytes=b"")
            self.addCleanup(win.deleteLater)
            item = self._make_item(win, kind)
            win.commit_history()
            menu = win._build_object_context_menu(item)
            # メニュー表示後に対象を削除 → 全アクションを叩いても例外を出さない。
            win._delete_object(item)
            self._trigger_all_enabled(menu)  # 例外を投げない

    def test_run_object_action_with_missing_id_is_safe(self) -> None:
        # obj_id が None / 未知でも安全に中止しログを出す（クラッシュしない）。
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="rc-missing", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        called = []
        win._run_object_action(None, lambda it: called.append(it), name="noop")
        win._run_object_action("does-not-exist", lambda it: called.append(it), name="noop")
        self.assertEqual(called, [])

    def test_context_menu_event_on_empty_area_does_not_crash(self) -> None:
        # 何もない場所を右クリックしても落ちない（キャンバスメニューへ委譲）。
        from unittest import mock
        from PySide6.QtCore import QPointF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="rc-empty", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)

        class _Evt:
            def __init__(self):
                self._accepted = False

            def scenePos(self):
                return QPointF(500.0, 500.0)

            def screenPos(self):
                return QPointF(0.0, 0.0)

            def accept(self):
                self._accepted = True

        with mock.patch.object(win, "_show_canvas_context_menu") as canvas, \
                mock.patch.object(win, "_show_object_context_menu") as obj:
            win._scene.contextMenuEvent(_Evt())
        canvas.assert_called_once()
        obj.assert_not_called()

    def test_context_menu_event_exception_does_not_propagate(self) -> None:
        # メニュー生成/表示で例外が出ても contextMenuEvent は落とさない（要件2）。
        from unittest import mock
        from PySide6.QtCore import QPointF, QRectF
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="rc-exc", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        rect = win.add_rect(QRectF(10.0, 10.0, 40.0, 40.0))

        class _Evt:
            def scenePos(self):
                return QPointF(20.0, 20.0)

            def screenPos(self):
                return QPointF(0.0, 0.0)

            def accept(self):
                pass

        with mock.patch.object(
            win, "_show_object_context_menu", side_effect=RuntimeError("boom")
        ):
            win._scene.contextMenuEvent(_Evt())  # 例外を投げない


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditReflectAreaFixedHeight(unittest.TestCase):
    """反映先表示領域を常に最大8個分の固定高さにする（要件3）。"""

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

    def _seed_user_templates(self, extra: int) -> None:
        from app.voucher_edit_templates import save_user_templates

        templates = [
            {"name": f"ユーザー{i}", "target_vouchers": ["03"], "color": "#607d8b", "badge": "U"}
            for i in range(extra)
        ]
        save_user_templates(templates)

    def test_fixed_height_with_zero_extra(self) -> None:
        # 組み込みテンプレートを全削除し、反映先0個の状態にする。
        from app.voucher_edit_window import REFLECT_LIST_FIXED_HEIGHT, VoucherEditWindow
        from app.voucher_edit_templates import save_user_templates

        save_user_templates([], deleted_builtins=["指図書のみ", "梱包のみ"])
        win = VoucherEditWindow(order_no="rf-0", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        container = win._reflect_list_container
        self.assertEqual(container.minimumHeight(), container.maximumHeight())
        self.assertEqual(container.maximumHeight(), REFLECT_LIST_FIXED_HEIGHT)

    def test_fixed_height_with_four(self) -> None:
        from app.voucher_edit_window import REFLECT_LIST_FIXED_HEIGHT, VoucherEditWindow

        # 既定の組み込み4個。
        win = VoucherEditWindow(order_no="rf-4", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertEqual(win._reflect_template_count(), 4)
        self.assertEqual(win._reflect_list_container.maximumHeight(), REFLECT_LIST_FIXED_HEIGHT)
        self.assertEqual(win._reflect_count_label.text(), "4/8")

    def test_fixed_height_with_eight(self) -> None:
        from app.voucher_edit_window import REFLECT_LIST_FIXED_HEIGHT, VoucherEditWindow

        self._seed_user_templates(4)  # 4 + 4 = 8
        win = VoucherEditWindow(order_no="rf-8", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        self.assertEqual(win._reflect_template_count(), 8)
        self.assertEqual(win._reflect_list_container.maximumHeight(), REFLECT_LIST_FIXED_HEIGHT)
        self.assertEqual(len(win._template_actions), 8)

    def test_height_unchanged_after_delete(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from app.voucher_edit_window import REFLECT_LIST_FIXED_HEIGHT, VoucherEditWindow

        self._seed_user_templates(4)
        win = VoucherEditWindow(order_no="rf-del", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        before = win._reflect_list_container.maximumHeight()
        with mock.patch("app.voucher_edit_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes):
            win._delete_template("ユーザー0")
        self.assertEqual(win._reflect_template_count(), 7)
        self.assertEqual(win._reflect_list_container.maximumHeight(), before)
        self.assertEqual(win._reflect_list_container.maximumHeight(), REFLECT_LIST_FIXED_HEIGHT)

    def test_selection_highlight_preserved(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="rf-sel", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        names = list(win._template_actions)
        win._on_template_selected(win._template_by_name(names[1]))
        checked = [n for n, b in win._template_actions.items() if b.isChecked()]
        self.assertEqual(checked, [names[1]])
        self.assertIn("background-color: #0d6efd",
                      win._template_actions[names[1]].styleSheet())


class TestVoucherEditShapeMenu(unittest.TestCase):
    """図形6種を「図形」ボタンへ統合（要件5・6・7）。"""

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

    def _make(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="shape-menu", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def test_shape_button_exists(self) -> None:
        win = self._make()
        self.assertIsNotNone(win._shape_tool_button)
        self.assertIsNotNone(win._shape_menu)

    def test_shape_menu_has_six_items(self) -> None:
        win = self._make()
        labels = [a.text() for a in win._shape_menu.actions()]
        for label in ("線", "矢印", "両矢印", "二重線", "四角", "丸"):
            self.assertIn(label, labels)
        self.assertEqual(len(win._shape_menu.actions()), 6)

    def test_individual_shape_buttons_not_in_header(self) -> None:
        from PySide6.QtWidgets import QToolBar

        win = self._make()
        actions = [a.text() for a in win._edit_header_widget.actions()]
        for label in ("線", "矢印", "両矢印", "二重線", "四角", "丸"):
            self.assertNotIn(label, actions)

    def test_menu_item_switches_tool_mode(self) -> None:
        from app.voucher_edit_window import (
            TOOL_LINE, TOOL_ARROW, TOOL_DOUBLE_ARROW, TOOL_DOUBLE_LINE,
            TOOL_RECT, TOOL_ELLIPSE,
        )

        win = self._make()
        for tool in (TOOL_LINE, TOOL_ARROW, TOOL_DOUBLE_ARROW,
                     TOOL_DOUBLE_LINE, TOOL_RECT, TOOL_ELLIPSE):
            win._tool_actions[tool].trigger()
            self.assertEqual(win.current_tool, tool)

    def test_shape_actions_are_exclusive_group(self) -> None:
        from app.voucher_edit_window import TOOL_LINE, TOOL_RECT

        win = self._make()
        win._tool_actions[TOOL_LINE].trigger()
        win._tool_actions[TOOL_RECT].trigger()
        checked = [a.text() for a in win._shape_action_group.actions() if a.isChecked()]
        self.assertEqual(checked, ["四角"])
        self.assertTrue(win._shape_action_group.isExclusive())

    def test_shape_button_reflects_current_shape(self) -> None:
        from app.voucher_edit_window import TOOL_ELLIPSE

        win = self._make()
        win._tool_actions[TOOL_ELLIPSE].trigger()
        self.assertIn("丸", win._shape_tool_button.text())

    def test_header_menu_scroll_area_maintained(self) -> None:
        # 上部メニュー横スクロール対応は維持する（要件7）。
        win = self._make()
        self.assertIsNotNone(getattr(win, "_main_toolbar_container", None))


class TestVoucherEditUndoRedoIconTheme(unittest.TestCase):
    """アンドゥ/リドゥの色・有効無効状態（要件8）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_icon_has_normal_and_disabled_pixmaps(self) -> None:
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QIcon
        from app.voucher_edit_window import make_undo_redo_icon

        icon, _ = make_undo_redo_icon("undo", dark=False)
        normal = icon.pixmap(QSize(20, 20), QIcon.Mode.Normal)
        disabled = icon.pixmap(QSize(20, 20), QIcon.Mode.Disabled)
        self.assertFalse(normal.isNull())
        self.assertFalse(disabled.isNull())

    def test_enabled_and_disabled_colors_differ(self) -> None:
        from app.voucher_edit_window import _undo_redo_colors

        for dark in (True, False):
            enabled, disabled = _undo_redo_colors(dark)
            self.assertNotEqual(enabled.name(), disabled.name())

    def test_dark_enabled_icon_not_same_as_background(self) -> None:
        # ダークテーマの enabled 色が暗色背景と同化しない（明るい色）。
        from app.voucher_edit_window import _undo_redo_colors

        enabled, _ = _undo_redo_colors(dark=True)
        # 明るい色（各チャネルが高い）であること。
        self.assertGreater(enabled.lightness(), 180)

    def test_theme_reapply_method_runs(self) -> None:
        import os
        import tempfile

        prev = os.environ.get("TKS_TO_KINTONE_HOME")
        tmp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = tmp.name
        try:
            from app.voucher_edit_window import VoucherEditWindow

            win = VoucherEditWindow(order_no="icon-theme", background_pdf_bytes=b"")
            self.addCleanup(win.deleteLater)
            # 再適用してもアイコンが空にならない。
            win._apply_undo_redo_icon_theme()
            self.assertFalse(win._undo_action.icon().isNull())
            self.assertFalse(win._redo_action.icon().isNull())
        finally:
            if prev is None:
                os.environ.pop("TKS_TO_KINTONE_HOME", None)
            else:
                os.environ["TKS_TO_KINTONE_HOME"] = prev
            tmp.cleanup()


class TestLeftPaneWidthByDpi(unittest.TestCase):
    """125%以上で左ペイン幅を広げる（要件9）。"""

    def test_scale_100_uses_base_width(self) -> None:
        """scale 1.0 の左ペイン幅が約250px（190からさらに+60px）。"""
        from app.window_geometry import left_pane_width_for_scale

        # 既定 base_width が 250（190 + 60px）へ広がっている。
        self.assertEqual(left_pane_width_for_scale(1.0), 250)
        self.assertEqual(left_pane_width_for_scale(1.0) - 190, 60)

    def test_scale_125_widens(self) -> None:
        """scale 1.25 の左ペイン幅が約300px（240からさらに+60px）。"""
        from app.window_geometry import left_pane_width_for_scale

        self.assertGreater(left_pane_width_for_scale(1.25), left_pane_width_for_scale(1.0))
        self.assertEqual(left_pane_width_for_scale(1.25), 300)
        self.assertEqual(left_pane_width_for_scale(1.25) - 240, 60)

    def test_scale_150_widens_further(self) -> None:
        """scale 1.5 の左ペイン幅が約320px（260からさらに+60px）。"""
        from app.window_geometry import left_pane_width_for_scale

        w125 = left_pane_width_for_scale(1.25)
        w150 = left_pane_width_for_scale(1.5)
        self.assertGreaterEqual(w150, w125)
        self.assertEqual(w150, 320)
        self.assertEqual(w150 - 260, 60)

    def test_get_display_scale_returns_positive(self) -> None:
        from app.window_geometry import get_display_scale

        self.assertGreater(get_display_scale(None), 0)


if __name__ == "__main__":
    unittest.main()
