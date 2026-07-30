"""指図書編集オブジェクトの保存/再読み込み・PDF反映のテスト。"""
from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TestVoucherEditObjects(unittest.TestCase):
    def _sample_objects(self):
        return [
            {"id": "a", "type": "text", "x": 100.0, "y": 200.0,
             "text": "メモ", "font_size": 12.0, "color": [0, 0, 0]},
            {"id": "b", "type": "line", "x1": 10.0, "y1": 20.0,
             "x2": 30.0, "y2": 40.0, "line_width": 1.0, "color": [0, 0, 0]},
            {"id": "c", "type": "rectangle", "x": 50.0, "y": 60.0,
             "w": 70.0, "h": 80.0, "line_width": 1.0, "color": [0, 0, 0]},
        ]

    def test_save_and_reload_roundtrip(self) -> None:
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            voucher_edit_objects.save_edit_objects("5218869", self._sample_objects(), base_dir=base)
            loaded = voucher_edit_objects.load_edit_objects("5218869", base_dir=base)
            self.assertEqual(len(loaded), 3)
            kinds = {obj["type"] for obj in loaded}
            self.assertEqual(kinds, {"text", "line", "rectangle"})
            text_obj = next(o for o in loaded if o["type"] == "text")
            self.assertEqual(text_obj["text"], "メモ")
            self.assertIn("created_at", text_obj)
            self.assertIn("updated_at", text_obj)

    def test_style_change_updates_canonical_hash_and_revision(self) -> None:
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            regular = [{
                "id": "trace-text", "type": "text", "text": "テキスト",
                "font_family": "Yu Gothic UI", "font_size": 18,
                "font_bold": True, "font_italic": False,
                "font_underline": True, "x": 10, "y": 20,
            }]
            path = voucher_edit_objects.save_edit_objects(
                "TRACE", regular, base, voucher_no="V1")
            first = voucher_edit_objects.load_edit_document_metadata("TRACE", base)
            italic = [dict(regular[0], font_italic=True)]
            voucher_edit_objects.save_edit_objects(
                "TRACE", italic, base, voucher_no="V1")
            second = voucher_edit_objects.load_edit_document_metadata("TRACE", base)
            self.assertNotEqual(
                voucher_edit_objects.edit_objects_sha256(regular),
                voucher_edit_objects.edit_objects_sha256(italic))
            self.assertNotEqual(
                first["edit_objects_sha256"], second["edit_objects_sha256"])
            self.assertEqual(second["edit_revision"], first["edit_revision"] + 1)
            self.assertIn(
                '"edit_revision": 2', path.read_text(encoding="utf-8"))

    def test_save_worker_pdf_trace_reloads_latest_deep_copy(self) -> None:
        import pypdf
        from app import voucher_service

        trace_id = "trace-save-worker-pdf-preview"
        object_id = "same-object-id"
        stale = {
            "id": object_id, "type": "text", "text": "TEST",
            "font_family": "Helvetica", "font_size": 24,
            "font_bold": True, "font_italic": False,
            "font_underline": True, "x": 80, "y": 80,
            "width": 180, "height": 40, "target_vouchers": ["03"],
        }
        latest = dict(stale, font_italic=True)
        latest_hash = "a" * 64
        data = {"pages": [{
            "order_no": "TRACE-E2E", "voucher_no": "V1",
            "customer_name": "顧客", "details": [],
            "edit_objects": [stale],
        }]}
        with mock.patch(
            "app.voucher_edit_objects.load_edit_objects",
            return_value=[latest],
        ), mock.patch(
            "app.voucher_edit_objects.load_edit_document_metadata",
            return_value={
                "edit_revision": 7,
                "edit_objects_sha256": latest_hash,
            },
        ), self.assertLogs("tks_to_kintone_app", level="INFO") as captured:
            pdf = voucher_service.build_vouchers_pdf_bytes(
                ["03"], data, edit_render_trace_id=trace_id,
                reload_edit_objects=True, bypass_preview_cache=True)
        logs = "\n".join(captured.output)
        self.assertIn(
            f"event=voucher_pdf_worker_input trace_id={trace_id} "
            f"object_id={object_id}", logs)
        self.assertIn("italic=True", logs)
        self.assertIn(
            f"event=voucher_edit_pdf_text_draw trace_id={trace_id} "
            f"object_id={object_id}", logs)
        self.assertIn(
            f"event=draw_styled_pdf_text trace_id={trace_id} "
            f"object_id={object_id}", logs)
        self.assertIn("font_italic=True", logs)
        self.assertIn(f"edit_objects_sha256={latest_hash}", logs)
        pdf_hash = hashlib.sha256(pdf).hexdigest()
        self.assertIn(
            f"event=voucher_pdf_bytes_ready trace_id={trace_id} "
            f"pdf_sha256={pdf_hash}", logs)
        stream = pypdf.PdfReader(
            io.BytesIO(pdf)).pages[0].get_contents().get_data()
        self.assertIn(b"1 0 .2 1", stream)
        # 呼出し元の古いsnapshotはdeep copy境界の外なので変更しない。
        self.assertFalse(data["pages"][0]["edit_objects"][0]["font_italic"])

    def test_voucher_no_state_is_independent_and_preserves_leading_zero(self) -> None:
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state = {
                voucher_edit_objects.voucher_key_for("0012"): [
                    {"id": "a", "type": "text", "text": "A", "x": 1, "y": 2}],
                voucher_edit_objects.voucher_key_for("A002"): [
                    {"id": "b", "type": "text", "text": "B", "x": 3, "y": 4}],
            }
            voucher_edit_objects.save_voucher_edit_state(
                "ORDER", state, [" 0012 ", "A002"], base)
            loaded = voucher_edit_objects.load_voucher_edit_state(
                "ORDER", ["0012", "A002"], base)
            self.assertEqual(loaded["0012"][0]["text"], "A")
            self.assertEqual(loaded["A002"][0]["text"], "B")
            loaded["0012"][0]["text"] = "changed"
            self.assertEqual(loaded["A002"][0]["text"], "B")

    def test_partial_voucher_save_keeps_other_vouchers(self) -> None:
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            voucher_edit_objects.save_edit_objects(
                "ORDER", [{"id": "a", "type": "text", "text": "A"}],
                base, voucher_no="A001")
            voucher_edit_objects.save_edit_objects(
                "ORDER", [{"id": "b", "type": "text", "text": "B"}],
                base, voucher_no="A002")
            self.assertEqual(
                voucher_edit_objects.load_edit_objects(
                    "ORDER", base, voucher_no="A001")[0]["text"], "A")
            self.assertEqual(
                voucher_edit_objects.load_edit_objects(
                    "ORDER", base, voucher_no="A002")[0]["text"], "B")

    def test_legacy_objects_migrate_only_to_first_voucher(self) -> None:
        import json
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = voucher_edit_objects.edit_objects_path_for("ORDER", base)
            path.write_text(json.dumps({"order_no": "ORDER", "objects": [
                {"id": "legacy", "type": "text", "text": "旧データ"}
            ]}), encoding="utf-8")
            loaded = voucher_edit_objects.load_voucher_edit_state(
                "ORDER", ["A001", "A002"], base)
            self.assertEqual(len(loaded["A001"]), 1)
            self.assertEqual(loaded["A002"], [])
            voucher_edit_objects.save_voucher_edit_state(
                "ORDER", loaded, ["A001", "A002"], base)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], 3)
            self.assertEqual(saved["common_edit"]["objects"], [])
            self.assertIn("voucher_edits", saved)

    def test_clone_reissues_ids_and_deep_copies(self) -> None:
        from app import voucher_edit_objects

        source = [{"id": "same", "type": "image", "image_data": "abc",
                   "meta": {"nested": [1]}}]
        cloned = voucher_edit_objects.clone_edit_objects(source)
        self.assertNotEqual(cloned[0]["id"], source[0]["id"])
        cloned[0]["meta"]["nested"].append(2)
        self.assertEqual(source[0]["meta"]["nested"], [1])

    def test_schema_v3_roundtrip_keeps_common_and_individual_separate(self) -> None:
        import json
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            common = [{"id": "common", "type": "text", "text": "共通"}]
            edits = {
                "Z001": [{"id": "one", "type": "text", "text": "個別1"}],
                "Z002": [{"id": "two", "type": "text", "text": "個別2"}],
            }
            path = voucher_edit_objects.save_voucher_edit_document(
                "ORDER", common, edits, ["Z001", "Z002"], base)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], 3)
            self.assertEqual(saved["common_edit"]["objects"][0]["text"], "共通")
            loaded = voucher_edit_objects.load_voucher_edit_document(
                "ORDER", ["Z001", "Z002"], base)
            self.assertEqual(loaded["common_edit"][0]["text"], "共通")
            self.assertEqual(loaded["voucher_edits"]["Z001"][0]["text"], "個別1")
            self.assertEqual(
                [o["text"] for o in voucher_edit_objects.load_edit_objects(
                    "ORDER", base, voucher_no="Z002")], ["共通", "個別2"])

    def test_schema_v2_load_adds_empty_common_without_losing_individual(self) -> None:
        import json
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = voucher_edit_objects.edit_objects_path_for("ORDER", base)
            path.write_text(json.dumps({
                "schema_version": 2,
                "voucher_order": ["Z001"],
                "voucher_edits": {"Z001": {"voucher_no": "Z001", "objects": [
                    {"id": "old", "type": "text", "text": "旧個別"}
                ]}},
            }), encoding="utf-8")
            loaded = voucher_edit_objects.load_voucher_edit_document(
                "ORDER", ["Z001"], base)
            self.assertEqual(loaded["common_edit"], [])
            self.assertEqual(loaded["voucher_edits"]["Z001"][0]["text"], "旧個別")

    def test_pdf_resolves_objects_by_page_voucher_no(self) -> None:
        from app import voucher_service

        with mock.patch("app.voucher_edit_objects.load_edit_objects") as load:
            load.side_effect = lambda order_no, **kwargs: [
                {"id": kwargs["voucher_no"], "type": "text", "text": kwargs["voucher_no"]}]
            a = voucher_service._resolve_edit_objects(
                {"order_no": "ORDER", "voucher_no": "A001"})
            b = voucher_service._resolve_edit_objects(
                {"order_no": "ORDER", "voucher_no": "A002"})
        self.assertEqual(a[0]["text"], "A001")
        self.assertEqual(b[0]["text"], "A002")

    def test_save_creates_file_keyed_by_order_no(self) -> None:
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = voucher_edit_objects.save_edit_objects("52/18", self._sample_objects(), base_dir=base)
            self.assertTrue(path.exists())
            # ファイル名に使えない文字はエスケープされる
            self.assertEqual(path.name, "52_18.json")

    def test_load_missing_returns_empty(self) -> None:
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(voucher_edit_objects.load_edit_objects("none", base_dir=Path(tmp)), [])

    def test_edit_objects_reflected_in_03_04_05_pdf(self) -> None:
        """編集オブジェクトが指図書(1)/(2)/梱包明細書のPDF生成へ反映されること。"""
        from app import voucher_service

        objects = self._sample_objects()
        base_page = {
            "order_no": "5218869",
            "customer_name": "テスト得意先",
            "code_no": "001",
            "delivery_no": "Z1",
            "details": [{"name": "品", "dims": "（10 * 20 ミリ）", "qty": "1枚"}],
            "edit_objects": objects,
        }
        # 反映ありと反映なしでバイト列が変わる（重ね描きされている）こと
        for vid in ("03", "04", "05"):
            with_obj = voucher_service.build_vouchers_pdf_bytes([vid], {"pages": [dict(base_page)]})
            no_obj_page = dict(base_page)
            no_obj_page["edit_objects"] = []
            without = voucher_service.build_vouchers_pdf_bytes([vid], {"pages": [no_obj_page]})
            self.assertNotEqual(with_obj, without, f"vid={vid} で編集オブジェクトが反映されていない")

    def test_draw_edit_objects_helper_exists(self) -> None:
        from app import voucher_service

        self.assertTrue(hasattr(voucher_service, "_draw_edit_objects"))

    def test_scene_rect_to_pdf_rect_coordinates(self) -> None:
        from app import voucher_service
        from app.voucher_templates import PAGE_H

        self.assertEqual(
            voucher_service._scene_rect_to_pdf_rect(100.0, 200.0, 80.0, 40.0),
            (100.0, PAGE_H - 200.0 - 40.0, 80.0, 40.0),
        )
        self.assertEqual(
            voucher_service._scene_rect_to_pdf_rect(300.0, 250.0, 90.0, 50.0),
            (300.0, PAGE_H - 250.0 - 50.0, 90.0, 50.0),
        )

    def test_scene_line_to_pdf_coordinates(self) -> None:
        from app import voucher_service
        from app.voucher_templates import PAGE_H

        self.assertEqual(
            voucher_service._scene_line_to_pdf(50.0, 60.0, 200.0, 160.0),
            (50.0, PAGE_H - 60.0, 200.0, PAGE_H - 160.0),
        )

    def test_text_pdf_baseline_uses_top_left_alignment_by_default(self) -> None:
        from app import voucher_service
        from app.voucher_templates import PAGE_H

        calls: list[tuple[float, float, str]] = []

        class _FakeCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def setLineWidth(self, *a): pass
            def setFont(self, *a): pass
            def drawString(self, x, y, text):
                calls.append((x, y, text))

        obj = {"id": "t", "type": "text", "x": 120.0, "y": 180.0,
               "width": 80.0, "height": 60.0, "text": "A", "font_size": 14.0}
        voucher_service._draw_edit_objects(_FakeCanvas(), [obj])
        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(calls[0][0], 120.0)
        pdf_y = PAGE_H - 180.0 - 60.0
        self.assertAlmostEqual(calls[0][1], pdf_y + 60.0 - 14.0)

    def test_save_overwrites_not_appends(self) -> None:
        """保存は append ではなく上書きで、件数が増えないこと。"""
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            voucher_edit_objects.save_edit_objects("X", self._sample_objects(), base_dir=base)
            # 2件だけにして保存し直すと、合計2件（追記されない）。
            voucher_edit_objects.save_edit_objects(
                "X", self._sample_objects()[:2], base_dir=base)
            loaded = voucher_edit_objects.load_edit_objects("X", base_dir=base)
            self.assertEqual(len(loaded), 2)

    def test_load_assigns_id_when_missing(self) -> None:
        """旧形式で id が無ければ読み込み時に付与される（要件9）。"""
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            voucher_edit_objects.save_edit_objects(
                "X", [{"type": "rectangle", "x": 1, "y": 2, "w": 3, "h": 4}],
                base_dir=base)
            loaded = voucher_edit_objects.load_edit_objects("X", base_dir=base)
            self.assertEqual(len(loaded), 1)
            self.assertTrue(loaded[0].get("id"))
            # text が無ければ空文字で補完される
            self.assertEqual(loaded[0].get("text"), "")

    def test_load_dedupes_same_id(self) -> None:
        """同じIDのオブジェクトが二重保存されても1件に重複排除される（要件2）。"""
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dup = {"id": "dup", "type": "text", "x": 1, "y": 2, "text": "a"}
            voucher_edit_objects.save_edit_objects("X", [dict(dup), dict(dup)], base_dir=base)
            loaded = voucher_edit_objects.load_edit_objects("X", base_dir=base)
            self.assertEqual(len(loaded), 1)

    def test_rectangle_inner_text_roundtrip(self) -> None:
        """四角形内テキストが保存・再読み込みされる（要件6）。"""
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            obj = {"id": "r1", "type": "rectangle", "x": 10, "y": 20,
                   "w": 30, "h": 40, "line_width": 1.5, "text": "+2", "font_size": 14}
            voucher_edit_objects.save_edit_objects("X", [obj], base_dir=base)
            loaded = voucher_edit_objects.load_edit_objects("X", base_dir=base)
            self.assertEqual(loaded[0]["text"], "+2")
            self.assertEqual(loaded[0]["font_size"], 14)

    def test_pdf_no_duplicate_for_same_id(self) -> None:
        """同一IDが複数渡されてもPDFには1回だけ描画される（要件10）。"""
        from app import voucher_service

        obj = {"id": "same", "type": "text", "x": 100.0, "y": 200.0,
               "text": "重複チェック", "font_size": 12.0, "color": [0, 0, 0]}
        base_page = {
            "order_no": "1", "customer_name": "得意先", "code_no": "001",
            "delivery_no": "Z1", "details": [{"name": "品", "dims": "", "qty": "1"}],
        }
        one = dict(base_page); one["edit_objects"] = [dict(obj)]
        twice = dict(base_page); twice["edit_objects"] = [dict(obj), dict(obj)]
        pdf_one = voucher_service.build_vouchers_pdf_bytes(["03"], {"pages": [one]})
        pdf_twice = voucher_service.build_vouchers_pdf_bytes(["03"], {"pages": [twice]})
        # 重複排除されていれば、1件と2件で出力が一致する。
        self.assertEqual(pdf_one, pdf_twice)

    def test_edit_objects_only_on_03_04_05(self) -> None:
        """編集オブジェクトは 03/04/05 のみ描画され、他では描画されない。"""
        from app import voucher_service

        obj = {"id": "o", "type": "text", "x": 100.0, "y": 200.0,
               "text": "限定反映", "font_size": 12.0, "color": [0, 0, 0]}
        base_page = {
            "order_no": "1", "customer_name": "得意先", "code_no": "001",
            "delivery_no": "Z1", "details": [{"name": "品", "dims": "", "qty": "1"}],
        }
        for vid in ("01", "02", "06"):
            with_obj = dict(base_page); with_obj["edit_objects"] = [dict(obj)]
            without = dict(base_page); without["edit_objects"] = []
            a = voucher_service.build_vouchers_pdf_bytes([vid], {"pages": [with_obj]})
            b = voucher_service.build_vouchers_pdf_bytes([vid], {"pages": [without]})
            self.assertEqual(a, b, f"vid={vid} に編集オブジェクトが反映されてはいけない")

    def test_ellipse_roundtrip(self) -> None:
        """ellipse が保存・再読み込みされる（要件8）。"""
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            obj = {"id": "e1", "type": "ellipse", "x": 10, "y": 20,
                   "w": 30, "h": 40, "line_width": 1.5, "text": "丸", "font_size": 14}
            voucher_edit_objects.save_edit_objects("X", [obj], base_dir=base)
            loaded = voucher_edit_objects.load_edit_objects("X", base_dir=base)
            self.assertEqual(loaded[0]["type"], "ellipse")
            self.assertEqual(loaded[0]["text"], "丸")

    def test_ellipse_reflected_in_03_pdf(self) -> None:
        """ellipse が指図書(1)PDFへ反映される（要件14）。"""
        from app import voucher_service

        obj = {"id": "e", "type": "ellipse", "x": 100.0, "y": 200.0,
               "w": 60.0, "h": 40.0, "line_width": 1.0, "text": "○",
               "font_size": 12.0, "color": [0, 0, 0]}
        base_page = {
            "order_no": "1", "customer_name": "得意先", "code_no": "001",
            "delivery_no": "Z1", "details": [{"name": "品", "dims": "", "qty": "1"}],
        }
        with_obj = dict(base_page); with_obj["edit_objects"] = [dict(obj)]
        without = dict(base_page); without["edit_objects"] = []
        a = voucher_service.build_vouchers_pdf_bytes(["03"], {"pages": [with_obj]})
        b = voucher_service.build_vouchers_pdf_bytes(["03"], {"pages": [without]})
        self.assertNotEqual(a, b)

    def test_shape_inner_text_centered(self) -> None:
        """図形内テキストが drawCentredString（中央寄せ）で描画される（要件7）。"""
        from app import voucher_service

        calls = {"centered": 0}

        class _FakeCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def setLineWidth(self, *a): pass
            def setFont(self, *a): pass
            def rect(self, *a, **k): pass
            def ellipse(self, *a, **k): pass
            def drawString(self, *a, **k): pass
            def drawCentredString(self, *a, **k):
                calls["centered"] += 1
            def drawRightString(self, *a, **k): pass

        objs = [
            {"id": "r", "type": "rectangle", "x": 10, "y": 20, "w": 60, "h": 40,
             "text": "A", "font_size": 12, "line_width": 1},
            {"id": "e", "type": "ellipse", "x": 10, "y": 80, "w": 60, "h": 40,
             "text": "B", "font_size": 12, "line_width": 1},
        ]
        voucher_service._draw_edit_objects(_FakeCanvas(), objs)
        self.assertEqual(calls["centered"], 2)

    def test_shape_inner_text_pdf_baseline_is_vertically_centered(self) -> None:
        from app import voucher_service

        calls = []

        class _FakeCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def setLineWidth(self, *a): pass
            def setFont(self, *a): pass
            def rect(self, *a, **k): pass
            def drawCentredString(self, *a): calls.append(a)

        voucher_service._draw_edit_objects(_FakeCanvas(), [
            {"id": "r", "type": "rectangle", "x": 10.0, "y": 20.0,
             "width": 60.0, "height": 40.0, "text": "A", "font_size": 10.0},
        ])
        pdf_y = voucher_service.PAGE_H - 20.0 - 40.0
        expected_baseline = pdf_y + 40.0 / 2.0 + (10.0 * 1.2) / 2.0 - 10.0
        self.assertEqual(calls[0][0], 40.0)
        self.assertAlmostEqual(calls[0][1], expected_baseline)

    def test_text_in_scene_rect_alignments_choose_pdf_methods(self) -> None:
        from app import voucher_service

        calls: list[tuple[str, float, float, str]] = []

        class _FakeCanvas:
            def setFont(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def drawString(self, x, y, text): calls.append(("left", x, y, text))
            def drawCentredString(self, x, y, text): calls.append(("center", x, y, text))
            def drawRightString(self, x, y, text): calls.append(("right", x, y, text))

        for align in ("left", "center", "right"):
            voucher_service.draw_text_in_scene_rect(
                _FakeCanvas(), "A", 10.0, 20.0, 100.0, 30.0,
                voucher_service._FONT_NAME, 12.0, text_align=align,
                vertical_align="top",
            )
        self.assertEqual([c[0] for c in calls], ["left", "center", "right"])
        self.assertEqual(calls[0][1], 10.0)
        self.assertEqual(calls[1][1], 60.0)
        self.assertEqual(calls[2][1], 110.0)

    def test_text_in_scene_rect_vertical_top_and_middle_baselines(self) -> None:
        from app import voucher_service

        calls: list[tuple[float, float, str]] = []

        class _FakeCanvas:
            def setFont(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def drawString(self, *a): calls.append(a)
            def drawCentredString(self, *a): calls.append(a)
            def drawRightString(self, *a): calls.append(a)

        voucher_service.draw_text_in_scene_rect(
            _FakeCanvas(), "A", 10.0, 20.0, 100.0, 30.0,
            voucher_service._FONT_NAME, 12.0, text_align="left",
            vertical_align="top",
        )
        voucher_service.draw_text_in_scene_rect(
            _FakeCanvas(), "A", 10.0, 20.0, 100.0, 30.0,
            voucher_service._FONT_NAME, 12.0, text_align="left",
            vertical_align="middle",
        )
        pdf_y = voucher_service.PAGE_H - 20.0 - 30.0
        self.assertAlmostEqual(calls[0][1], pdf_y + 30.0 - 12.0)
        self.assertAlmostEqual(calls[1][1], pdf_y + (30.0 + 12.0 * 1.2) / 2.0 - 12.0)

    def test_text_and_shape_objects_use_common_text_rect_drawer(self) -> None:
        from app import voucher_service

        class _FakeCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def setLineWidth(self, *a): pass
            def rect(self, *a, **k): pass
            def ellipse(self, *a, **k): pass

        objs = [
            {"id": "t", "type": "text", "x": 1, "y": 2, "width": 3, "height": 4,
             "text": "T", "font_size": 10},
            {"id": "r", "type": "rectangle", "x": 1, "y": 2, "width": 3, "height": 4,
             "text": "R", "font_size": 10},
            {"id": "e", "type": "ellipse", "x": 1, "y": 2, "width": 3, "height": 4,
             "text": "E", "font_size": 10},
        ]
        with mock.patch("app.voucher_service.draw_text_in_scene_rect") as draw:
            voucher_service._draw_edit_objects(_FakeCanvas(), objs)
        self.assertEqual(draw.call_count, 3)

    def test_symbol_text_uses_dedicated_pdf_drawer(self) -> None:
        from app import voucher_service

        class _FakeCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass

        objs = [{"id": "s", "type": "symbol_text", "x": 50.0, "y": 60.0,
                 "text": "×", "font_size": 20.0, "anchor": "center"}]
        with mock.patch("app.voucher_service.draw_symbol_text") as symbol_draw, \
                mock.patch("app.voucher_service.draw_text_in_scene_rect") as rect_draw:
            voucher_service._draw_edit_objects(_FakeCanvas(), objs)
        symbol_draw.assert_called_once()
        rect_draw.assert_not_called()

    def test_symbol_text_pdf_draw_uses_centered_string_and_y_correction(self) -> None:
        from app import voucher_service

        calls = {"font": None, "center": None}

        class _FakeCanvas:
            def setFont(self, *a): calls["font"] = a
            def setFillColorRGB(self, *a): pass
            def drawCentredString(self, *a): calls["center"] = a

        voucher_service.draw_symbol_text(_FakeCanvas(), {
            "id": "s", "type": "symbol_text", "x": 50.0, "y": 60.0,
            "text": "+3", "font_size": 20.0, "anchor": "center",
            "text_color": "#000000",
        })
        self.assertEqual(calls["center"][0], 50.0)
        self.assertEqual(calls["center"][2], "+3")
        self.assertAlmostEqual(calls["center"][1], voucher_service.PAGE_H - 60.0 - 20.0 * 0.35)

    def test_symbol_text_normalization_does_not_depend_on_text_box_fields(self) -> None:
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            voucher_edit_objects.save_edit_objects(
                "SYM",
                [{"id": "s", "type": "symbol_text", "x": 10, "y": 20,
                  "text": "×", "font_size": 35, "anchor": "center",
                  "width": 99, "height": 88, "vertical_align": "top"}],
                base_dir=base,
            )
            obj = voucher_edit_objects.load_edit_objects("SYM", base_dir=base)[0]
            self.assertEqual(obj["type"], "symbol_text")
            self.assertEqual(obj["anchor"], "center")
            self.assertEqual(obj["font_size"], 35.0)
            self.assertNotIn("width", obj)
            self.assertNotIn("height", obj)
            self.assertNotIn("vertical_align", obj)

    def test_debug_boxes_are_drawn_only_when_enabled(self) -> None:
        from app import voucher_service

        class _FakeCanvas:
            def __init__(self):
                self.debug_rects = 0
                self.circles = 0
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def setLineWidth(self, *a): pass
            def setDash(self, *a): pass
            def setFont(self, *a): pass
            def drawString(self, *a): pass
            def drawCentredString(self, *a): pass
            def drawRightString(self, *a): pass
            def line(self, *a): pass
            def rect(self, *a, **k):
                if k.get("fill") == 0:
                    self.debug_rects += 1
            def circle(self, *a, **k):
                self.circles += 1

        objs = [
            {"id": "t", "type": "text", "x": 10.0, "y": 20.0,
             "width": 30.0, "height": 12.0, "text": "A", "font_size": 10.0},
            {"id": "l", "type": "line", "x1": 1.0, "y1": 2.0, "x2": 3.0, "y2": 4.0},
        ]
        with mock.patch.dict("os.environ", {}, clear=True):
            off = _FakeCanvas()
            voucher_service._draw_edit_objects(off, objs)
        with mock.patch.dict("os.environ", {"VOUCHER_EDIT_DEBUG_BOXES": "1"}):
            on = _FakeCanvas()
            voucher_service._draw_edit_objects(on, objs)
        self.assertEqual(off.debug_rects, 0)
        self.assertEqual(off.circles, 0)
        self.assertEqual(on.debug_rects, 1)
        self.assertEqual(on.circles, 2)

    def test_edit_text_font_uses_standard_japanese_font(self) -> None:
        """編集テキスト用PDFフォントは既存の日本語ゴシックを使う。"""
        from app import voucher_service

        name = voucher_service._ensure_edit_text_font()
        self.assertTrue(name)
        self.assertEqual(name, voucher_service._FONT_NAME)

    def test_missing_attributes_are_defaulted(self) -> None:
        """旧JSONに属性が無い場合は既定値で補完される。"""
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            voucher_edit_objects.save_edit_objects(
                "D", [{"id": "old", "type": "text", "x": 1, "y": 2, "text": "旧"}],
                base_dir=base,
            )
            loaded = voucher_edit_objects.load_edit_objects("D", base_dir=base)
            obj = loaded[0]
            self.assertEqual(obj["font_family"], "Yu Gothic UI")
            self.assertEqual(obj["font_size"], 12.0)
            self.assertEqual(obj["line_width"], 1.0)
            self.assertEqual(obj["width"], 60.0)
            self.assertEqual(obj["height"], 18.0)
            self.assertEqual(obj["text_align"], "left")
            self.assertEqual(obj["vertical_align"], "top")
            self.assertTrue(obj["auto_fit"])
            self.assertFalse(obj["manual_resized"])

    def test_shape_inner_text_alignment_is_not_corrected_on_load(self) -> None:
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            voucher_edit_objects.save_edit_objects(
                "S",
                [{"id": "shape", "type": "rectangle", "x": 1, "y": 2, "text": "図",
                  "text_align": "right", "vertical_align": "bottom"}],
                base_dir=base,
            )
            loaded = voucher_edit_objects.load_edit_objects("S", base_dir=base)
            obj = loaded[0]
            self.assertEqual(obj["text_align"], "right")
            self.assertEqual(obj["vertical_align"], "bottom")

    def test_pdf_uses_object_font_size_and_line_width(self) -> None:
        """PDF反映時に各オブジェクトの font_size / line_width が使われる。"""
        from app import voucher_service

        calls = {"fonts": [], "widths": []}

        class _FakeCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def setLineWidth(self, w): calls["widths"].append(w)
            def setFont(self, name, size): calls["fonts"].append((name, size))
            def rect(self, *a, **k): pass
            def ellipse(self, *a, **k): pass
            def line(self, *a, **k): pass
            def drawString(self, *a, **k): pass
            def drawCentredString(self, *a, **k): pass
            def drawRightString(self, *a, **k): pass

        voucher_service._draw_edit_objects(_FakeCanvas(), [
            {"id": "t", "type": "text", "x": 10, "y": 20,
             "width": 100, "height": 20, "text": "A", "font_size": 19},
            {"id": "l", "type": "line", "x1": 1, "y1": 2, "x2": 3, "y2": 4,
             "line_width": 2.5},
            {"id": "r", "type": "rectangle", "x": 10, "y": 20,
             "width": 30, "height": 40, "line_width": 3.5},
        ])
        self.assertIn((voucher_service._FONT_NAME, 19.0), calls["fonts"])
        self.assertIn(2.5, calls["widths"])
        self.assertIn(3.5, calls["widths"])

    def test_scene_rect_to_pdf_conversion(self) -> None:
        from app import voucher_service

        self.assertEqual(
            voucher_service._scene_rect_to_pdf(10.0, 20.0, 30.0, 40.0),
            (10.0, voucher_service.PAGE_H - 20.0 - 40.0, 30.0, 40.0),
        )

    def test_scene_line_to_pdf_conversion(self) -> None:
        from app import voucher_service

        self.assertEqual(
            voucher_service._scene_line_to_pdf(1.0, 2.0, 3.0, 4.0),
            (1.0, voucher_service.PAGE_H - 2.0, 3.0, voucher_service.PAGE_H - 4.0),
        )

    def test_rectangle_pdf_draw_uses_scene_top_left_coordinates(self) -> None:
        from app import voucher_service

        calls = {"rect": None}

        class _FakeCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def setLineWidth(self, *a): pass
            def setFont(self, *a): pass
            def rect(self, *a, **k): calls["rect"] = a

        voucher_service._draw_edit_objects(_FakeCanvas(), [
            {"id": "r", "type": "rectangle", "x": 10.0, "y": 20.0,
             "width": 30.0, "height": 40.0},
        ])
        self.assertEqual(calls["rect"], (10.0, voucher_service.PAGE_H - 60.0, 30.0, 40.0))

    def test_ellipse_pdf_draw_uses_scene_top_left_coordinates(self) -> None:
        from app import voucher_service

        calls = {"ellipse": None}

        class _FakeCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def setLineWidth(self, *a): pass
            def setFont(self, *a): pass
            def ellipse(self, *a, **k): calls["ellipse"] = a

        voucher_service._draw_edit_objects(_FakeCanvas(), [
            {"id": "e", "type": "ellipse", "x": 10.0, "y": 20.0,
             "width": 30.0, "height": 40.0},
        ])
        self.assertEqual(calls["ellipse"], (10.0, voucher_service.PAGE_H - 60.0, 40.0, voucher_service.PAGE_H - 20.0))

    def test_line_pdf_draw_uses_scene_top_left_coordinates(self) -> None:
        from app import voucher_service

        calls = {"line": None}

        class _FakeCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def setLineWidth(self, *a): pass
            def line(self, *a): calls["line"] = a

        voucher_service._draw_edit_objects(_FakeCanvas(), [
            {"id": "l", "type": "line", "x1": 1.0, "y1": 2.0,
             "x2": 3.0, "y2": 4.0},
        ])
        self.assertEqual(calls["line"], (1.0, voucher_service.PAGE_H - 2.0, 3.0, voucher_service.PAGE_H - 4.0))

    def test_text_pdf_baseline_tracks_scene_top(self) -> None:
        from app import voucher_service

        calls = {"text": None}

        class _FakeCanvas:
            def saveState(self): pass
            def restoreState(self): pass
            def setStrokeColorRGB(self, *a): pass
            def setFillColorRGB(self, *a): pass
            def setFont(self, *a): pass
            def drawString(self, *a): calls["text"] = a

        voucher_service._draw_edit_objects(_FakeCanvas(), [
            {"id": "t", "type": "text", "x": 10.0, "y": 20.0,
             "width": 100.0, "height": 30.0, "font_size": 12.0, "text": "A"},
        ])
        expected_y = voucher_service.PAGE_H - 20.0 - 12.0
        self.assertEqual(calls["text"], (10.0, expected_y, "A"))


if __name__ == "__main__":
    unittest.main()
