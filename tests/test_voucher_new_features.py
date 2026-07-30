"""新機能のテスト。

1. 仕上日「なし」/ AM・PM「なし」
2. 伝票データのフォントサイズ拡大
3. 編集オブジェクトの反映先伝票（target_vouchers）
4. 反映先テンプレートの登録・選択
5. テンプレート選択中の作成オブジェクトへの属性付与
6. テンプレートバッヂ（保存/PDF対象外）
7. PDF描画側の反映条件
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QRectF

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


class _FakeFinishCanvas:
    """_draw_header_finish_date 用の描画呼び出し記録キャンバス。"""

    def __init__(self) -> None:
        self.right_strings: list[tuple[float, float, str]] = []

    def setFont(self, *a): pass

    def drawRightString(self, x, y, text):
        self.right_strings.append((x, y, str(text)))

    def drawString(self, *a): pass


class _FakeAmPmCanvas:
    def __init__(self) -> None:
        self.ellipses: list[tuple] = []

    def setFont(self, *a): pass
    def saveState(self): pass
    def restoreState(self): pass
    def setLineWidth(self, *a): pass

    def stringWidth(self, text, font, size):
        return float(len(text)) * size * 0.5

    def ellipse(self, *args, **kwargs):
        self.ellipses.append(args)


class TestFinishDateAndAmPm(unittest.TestCase):
    def test_finish_date_none_prints_nothing(self) -> None:
        from app import voucher_service

        c = _FakeFinishCanvas()
        voucher_service._draw_header_finish_date(c, None)
        self.assertEqual(c.right_strings, [])

    def test_finish_date_value_prints(self) -> None:
        from app import voucher_service

        c = _FakeFinishCanvas()
        voucher_service._draw_header_finish_date(c, date(2026, 6, 19))
        # 月・日で2回描画される。
        self.assertEqual(len(c.right_strings), 2)
        texts = {s[2] for s in c.right_strings}
        self.assertEqual(texts, {"6", "19"})

    def test_ampm_none_draws_no_circle(self) -> None:
        from app import voucher_service

        c = _FakeAmPmCanvas()
        voucher_service._draw_ampm_circle(c, "none")
        self.assertEqual(c.ellipses, [])
        c2 = _FakeAmPmCanvas()
        voucher_service._draw_ampm_circle(c2, "")
        self.assertEqual(c2.ellipses, [])

    def test_ampm_am_and_pm_draw_circle(self) -> None:
        from app import voucher_service

        for value in ("AM", "PM"):
            c = _FakeAmPmCanvas()
            voucher_service._draw_ampm_circle(c, value)
            self.assertEqual(len(c.ellipses), 1, f"{value} で丸が描かれていない")


class TestDataFontSizeEnlarged(unittest.TestCase):
    def test_data_font_constants_enlarged(self) -> None:
        from app import voucher_service

        # 旧値（FS_VAL=7.8 / FS_DIM=7.0）より拡大されていること。
        self.assertGreater(voucher_service.DATA_FONT_SIZE, 7.8)
        self.assertGreater(voucher_service.DETAIL_DATA_FONT_SIZE, 7.0)

    def test_label_font_sizes_unchanged(self) -> None:
        """ラベル/タイトル/社名/加工名ラベルのサイズはデータ定数と分離されていること。"""
        from app import voucher_service

        # データ用定数はラベル(6.0)・加工名ラベル(6.5)とは別物（拡大対象外を侵食しない）。
        self.assertNotEqual(voucher_service.DATA_FONT_SIZE, 6.0)
        self.assertNotEqual(voucher_service.DETAIL_DATA_FONT_SIZE, 6.0)


class _NoOpPath:
    """beginPath() が返すパスのスタブ（全メソッド no-op）。"""

    def __getattr__(self, name):
        return lambda *a, **k: None


class _RecordingCanvas:
    """setFont のサイズと描画テキストを記録するフェイクキャンバス。

    未定義メソッド（drawImage/line/rect 等）は __getattr__ で no-op にする。
    stringWidth は 0 を返すため _clip による切り詰めは発生しない。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[float, str]] = []
        self._size: float | None = None

    def setFont(self, name, size, *a):
        self._size = size

    def stringWidth(self, text, font, size):
        return 0.0

    def beginPath(self):
        return _NoOpPath()

    def drawString(self, x, y, text):
        self.calls.append((self._size, str(text)))

    def drawRightString(self, x, y, text):
        self.calls.append((self._size, str(text)))

    def drawCentredString(self, cx, y, text):
        self.calls.append((self._size, str(text)))

    def __getattr__(self, name):
        return lambda *a, **k: None

    def size_of(self, text: str) -> float | None:
        for size, drawn in self.calls:
            if drawn == text:
                return size
        return None


def _detail_page():
    return {
        "code_no": "001",
        "customer_name": "テスト得意先",
        "order_no": "",  # QR を発火させない
        "details": [
            {
                "name": "テスト品名",
                "dims": "（1303 * 1061 ミリ）",
                "qty_spec": "510中",
                "qty": "2枚",
                "unit_price": "800",
                "amount": "3,846",
                "op_category": "00",
                "note_lines": [],
            },
        ],
    }


class TestDetailFontsEnlarged(unittest.TestCase):
    """寸法・数量・単価・金額データのフォント1.5倍化を検証する（要件1・2・10）。"""

    def test_new_constants_are_1_5x(self) -> None:
        from app import voucher_service as vs
        from app.voucher_templates import FS_DIM_LARGE

        # 寸法・数量は従来どおり1.5倍。
        self.assertAlmostEqual(vs.DETAIL_DIM_FONT_SIZE, FS_DIM_LARGE * 1.5, places=4)
        self.assertAlmostEqual(vs.DETAIL_QTY_VALUE_FONT_SIZE, vs.DATA_FONT_SIZE * 1.5, places=4)
        # 単価・金額は現在比0.8倍（旧1.5倍 → 1.5*0.8 = 基準の1.2倍。要件5）。
        self.assertAlmostEqual(vs.DETAIL_UNIT_PRICE_FONT_SIZE, vs.DATA_FONT_SIZE * 1.5 * 0.8, places=4)
        self.assertAlmostEqual(vs.DETAIL_AMOUNT_FONT_SIZE, vs.DATA_FONT_SIZE * 1.5 * 0.8, places=4)
        # 品名1段目・摘要列データは基準の1.2倍（要件3・4）。
        self.assertAlmostEqual(vs.DETAIL_NAME_FONT_SIZE, vs.DATA_FONT_SIZE * 1.2, places=4)
        self.assertAlmostEqual(vs.DETAIL_NOTE_FONT_SIZE, vs.DETAIL_DATA_FONT_SIZE * 1.2, places=4)

    def test_form_01_detail_font_sizes(self) -> None:
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_form_data_01(c, _detail_page())
        # 寸法・数量データは1.5倍、単価・金額は専用フォント（0.8倍後）
        self.assertAlmostEqual(c.size_of("（1303 * 1061 ミリ）"), vs.DETAIL_DIM_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("2枚"), vs.DETAIL_QTY_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("800"), vs.DETAIL_UNIT_PRICE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("3,846"), vs.DETAIL_AMOUNT_FONT_SIZE, places=4)
        # 品名1段目は1.2倍、数量1段目コードは据え置き（DATA_FONT_SIZE）
        self.assertAlmostEqual(c.size_of("テスト品名"), vs.DETAIL_NAME_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("510中"), vs.DATA_FONT_SIZE, places=4)

    def test_delivery_07_detail_font_sizes(self) -> None:
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_delivery_details_07(c, _detail_page())
        self.assertAlmostEqual(c.size_of("（1303 * 1061 ミリ）"), vs.DETAIL_DIM_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("2枚"), vs.DETAIL_QTY_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("800"), vs.DETAIL_UNIT_PRICE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("3,846"), vs.DETAIL_AMOUNT_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("テスト品名"), vs.DETAIL_NAME_FONT_SIZE, places=4)

    def test_shizu_detail_font_sizes(self) -> None:
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_form_data_shizu(c, _detail_page())
        self.assertAlmostEqual(c.size_of("（1303 * 1061 ミリ）"), vs.DETAIL_DIM_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("2枚"), vs.DETAIL_QTY_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("テスト品名"), vs.DETAIL_NAME_FONT_SIZE, places=4)

    def test_delivery_08_detail_font_sizes(self) -> None:
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_delivery_details_08(c, _detail_page())
        self.assertAlmostEqual(c.size_of("（1303 * 1061 ミリ）"), vs.DETAIL_DIM_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("2枚"), vs.DETAIL_QTY_VALUE_FONT_SIZE, places=4)

    def test_dim_fits_in_name_column(self) -> None:
        """拡大後も寸法文字列が品名列の最大幅を超えないこと。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app import voucher_service as vs
        from app.voucher_templates import TBL_MAX_NAME

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        w = pdfmetrics.stringWidth("（1303 * 1061 ミリ）", "HeiseiKakuGo-W5", vs.DETAIL_DIM_FONT_SIZE)
        self.assertLessEqual(w, TBL_MAX_NAME)

    def test_all_vouchers_build_with_enlarged_fonts(self) -> None:
        """01〜08 すべてで PDF 生成できること。"""
        from app import voucher_service as vs
        from app.voucher_templates import VOUCHER_IDS

        page = {
            "code_no": "001",
            "customer_name": "テスト得意先",
            "order_no": "5218869",
            "details": [
                {"name": "テスト品名", "dims": "（1303 * 1061 ミリ）",
                 "qty_spec": "510中", "qty": "2枚",
                 "unit_price": "800", "amount": "3,846", "note_lines": []},
            ],
        }
        for vid in VOUCHER_IDS:
            pdf = vs.build_vouchers_pdf_bytes([vid], {"pages": [page]})
            self.assertTrue(pdf.startswith(b"%PDF"), f"{vid} の生成に失敗")


def _header_page():
    return {
        "code_no": "001",
        "customer_name": "テスト得意先",
        "order_no": "",  # QR を発火させない
        "delivery_date": "26/06/19",
        "ship_type": "販PM",
        "voucher_no": "V1",
        "trade_type": "掛",
        "operator": "担当",
        "details": [
            {
                "name": "MT5　四方　磨き",
                "dims": "（1303 * 1061 ミリ）",
                "qty_spec": "510中",
                "qty": "2枚",
                "unit_price": "800",
                "amount": "3,846",
                "note_lines": ["9,660 加"],
            },
        ],
    }


class TestHeaderValueFontsEnlarged(unittest.TestCase):
    """納品日・出荷区分・仕上日データの1.2倍化、摘要1.2倍、AM/PM線幅2倍を検証（要件1・2・4）。"""

    def test_header_value_constants(self) -> None:
        from app import voucher_service as vs
        from app.voucher_templates import HDR_SHIAGE_DATA_FS

        # 上部ヘッダーのデータは基準の1.3倍へ統一（要件1）。
        self.assertAlmostEqual(vs.HEADER_MAIN_VALUE_FONT_SIZE, vs.DATA_FONT_SIZE * 1.3, places=4)
        self.assertAlmostEqual(vs.HEADER_NOUHIN_VALUE_FONT_SIZE, vs.DATA_FONT_SIZE * 1.3, places=4)
        self.assertAlmostEqual(vs.HEADER_SHIPPING_VALUE_FONT_SIZE, vs.DATA_FONT_SIZE * 1.3, places=4)
        self.assertAlmostEqual(vs.HEADER_FINISH_DATE_VALUE_FONT_SIZE, HDR_SHIAGE_DATA_FS * 1.3, places=4)
        # 中央の摘要・物件Noデータは直前バージョンのサイズから1.1倍。
        self.assertAlmostEqual(
            vs.SUMMARY_VALUE_FONT_SIZE,
            vs.SUMMARY_VALUE_BASE_FONT_SIZE * 0.8 * 1.1,
            places=4,
        )
        self.assertAlmostEqual(
            vs.PROPERTY_VALUE_FONT_SIZE,
            vs.PROPERTY_VALUE_BASE_FONT_SIZE * 0.8 * 1.1,
            places=4,
        )
        # 加工名ラベルは基準(6.5)の1.2倍（要件5）。
        self.assertAlmostEqual(vs.PROCESS_LABEL_FONT_SIZE, 6.5 * 1.2, places=4)

    def test_form_01_header_value_fonts(self) -> None:
        from app import voucher_service as vs

        page = dict(_header_page())
        page["issue_date"] = "26/06/30"
        c = _RecordingCanvas()
        vs._draw_form_data_01(c, page)
        # コードNo・得意先名・伝票No・入力者名・発行日データは1.3倍（要件1）。
        self.assertAlmostEqual(c.size_of("001"), vs.HEADER_MAIN_VALUE_FONT_SIZE, places=4)
        # 得意先名データは基準の1.2倍（要件3）。
        self.assertAlmostEqual(c.size_of("テスト得意先"), vs.HEADER_CUSTOMER_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("V1"), vs.HEADER_MAIN_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("担当"), vs.HEADER_MAIN_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("26/06/30"), vs.HEADER_MAIN_VALUE_FONT_SIZE, places=4)
        # 納品日・出荷区分データも1.3倍。
        self.assertAlmostEqual(c.size_of("26/06/19"), vs.HEADER_NOUHIN_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("販PM"), vs.HEADER_SHIPPING_VALUE_FONT_SIZE, places=4)
        # 取引区分データは出荷区分と同じ1.3倍（要件1）。
        self.assertAlmostEqual(c.size_of("掛"), vs.HEADER_TRADE_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(vs.HEADER_TRADE_VALUE_FONT_SIZE, vs.HEADER_SHIPPING_VALUE_FONT_SIZE, places=4)
        # 摘要列データ（数値）は1.2倍据え置き（要件4の対象外＝明細摘要列）。
        self.assertAlmostEqual(c.size_of("9,660"), vs.DETAIL_NOTE_FONT_SIZE, places=4)

    def test_delivery_header_value_fonts(self) -> None:
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_delivery_data_common(c, _header_page())
        self.assertAlmostEqual(c.size_of("26/06/19"), vs.HEADER_NOUHIN_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("販PM"), vs.HEADER_SHIPPING_VALUE_FONT_SIZE, places=4)
        # コードNo・伝票No・入力者名データも1.3倍（要件1）。取引区分のみ据え置き。
        self.assertAlmostEqual(c.size_of("V1"), vs.HEADER_MAIN_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("001"), vs.HEADER_MAIN_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("担当"), vs.HEADER_MAIN_VALUE_FONT_SIZE, places=4)
        # 取引区分データは出荷区分と同じ1.3倍（要件1）。
        self.assertAlmostEqual(c.size_of("掛"), vs.HEADER_TRADE_VALUE_FONT_SIZE, places=4)

    def test_finish_date_value_font_is_1_2x(self) -> None:
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_header_finish_date(c, date(2026, 6, 16))
        self.assertAlmostEqual(c.size_of("6"), vs.HEADER_FINISH_DATE_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("16"), vs.HEADER_FINISH_DATE_VALUE_FONT_SIZE, places=4)

    def test_header_labels_unchanged(self) -> None:
        """ラベル「納品日」「出荷区分」「仕上日」のフォントは拡大対象外（6.0/7.0等のまま）。"""
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_form_data_01(c, _header_page())
        # ラベルは _draw_form_structure 側で描画されるため _draw_form_data_01 には現れない。
        # ここではデータ専用フォントがラベル用フォント(6.0)と別であることを確認。
        self.assertNotAlmostEqual(vs.HEADER_NOUHIN_VALUE_FONT_SIZE, 6.0, places=4)

    def test_ampm_circle_line_width_doubled(self) -> None:
        from app import voucher_service as vs

        widths: list[float] = []

        class _C(_FakeAmPmCanvas):
            def setLineWidth(self, w):
                widths.append(w)

        vs._draw_ampm_circle(_C(), "PM")
        # 線幅は従来 0.9 の2倍。
        self.assertIn(vs.AMPM_CIRCLE_LINE_WIDTH, widths)
        self.assertAlmostEqual(vs.AMPM_CIRCLE_LINE_WIDTH, 0.9 * 2, places=4)


class TestEditObjectTargetVouchers(unittest.TestCase):
    def test_target_vouchers_saved_and_roundtrip(self) -> None:
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            objects = [
                {"id": "a", "type": "text", "x": 10.0, "y": 20.0, "text": "x",
                 "target_vouchers": ["03", "04", "05", "07"]},
            ]
            voucher_edit_objects.save_edit_objects("O1", objects, base_dir=base)
            loaded = voucher_edit_objects.load_edit_objects("O1", base_dir=base)
            self.assertEqual(loaded[0]["target_vouchers"], ["03", "04", "05", "07"])

    def test_old_object_defaults_to_03_04_05(self) -> None:
        from app import voucher_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # target_vouchers 無しの旧オブジェクト。
            objects = [{"id": "a", "type": "text", "x": 10.0, "y": 20.0, "text": "x"}]
            voucher_edit_objects.save_edit_objects("O2", objects, base_dir=base)
            loaded = voucher_edit_objects.load_edit_objects("O2", base_dir=base)
            self.assertEqual(loaded[0]["target_vouchers"], ["03", "04", "05"])


class TestPdfTargetVoucherFiltering(unittest.TestCase):
    def _page(self, objects):
        return {
            "order_no": "5218869",
            "customer_name": "テスト得意先",
            "code_no": "001",
            "delivery_no": "Z1",
            "details": [{"name": "品", "dims": "（10 * 20）", "qty": "1枚"}],
            "edit_objects": objects,
        }

    def test_object_drawn_only_on_target_voucher(self) -> None:
        from app import voucher_service

        objects = [{"id": "x", "type": "text", "x": 100.0, "y": 100.0,
                    "width": 80.0, "height": 20.0, "text": "メモ",
                    "target_vouchers": ["01"]}]
        # 反映先に 01 を含むので 01 には描画され、含まない場合と差が出る。
        with_obj = voucher_service.build_vouchers_pdf_bytes(["01"], {"pages": [self._page(objects)]})
        without = voucher_service.build_vouchers_pdf_bytes(["01"], {"pages": [self._page([])]})
        self.assertNotEqual(with_obj, without)

    def test_object_not_drawn_on_non_target_voucher(self) -> None:
        from app import voucher_service

        objects = [{"id": "x", "type": "text", "x": 100.0, "y": 100.0,
                    "width": 80.0, "height": 20.0, "text": "メモ",
                    "target_vouchers": ["03"]}]
        # 03 のみ対象 → 01 には描画されないので、編集オブジェクト有無で 01 は同一。
        with_obj = voucher_service.build_vouchers_pdf_bytes(["01"], {"pages": [self._page(objects)]})
        without = voucher_service.build_vouchers_pdf_bytes(["01"], {"pages": [self._page([])]})
        self.assertEqual(with_obj, without)

    def test_filter_helper(self) -> None:
        from app import voucher_service

        objs = [
            {"id": "a", "target_vouchers": ["03", "04", "05"]},
            {"id": "b", "target_vouchers": ["07"]},
            {"id": "c"},  # 未設定 → 03/04/05 扱い
        ]
        ids_03 = {o["id"] for o in voucher_service._filter_edit_objects(objs, "03")}
        self.assertEqual(ids_03, {"a", "c"})
        ids_07 = {o["id"] for o in voucher_service._filter_edit_objects(objs, "07")}
        self.assertEqual(ids_07, {"b"})


class TestTemplates(unittest.TestCase):
    def test_builtin_templates_present(self) -> None:
        from app import voucher_edit_templates

        with tempfile.TemporaryDirectory() as tmp:
            templates = voucher_edit_templates.load_templates(base_dir=Path(tmp))
            names = {t["name"] for t in templates}
            self.assertTrue({"標準", "全伝票", "指図書のみ", "梱包のみ"} <= names)
            std = next(t for t in templates if t["name"] == "標準")
            self.assertEqual(std["target_vouchers"], ["03", "04", "05"])

    def test_user_template_saved_and_loaded(self) -> None:
        from app import voucher_edit_templates

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            voucher_edit_templates.save_user_templates(
                [{"name": "納品書にも印字", "target_vouchers": ["03", "04", "05", "07"],
                  "color": "#ff9800"}],
                base_dir=base,
            )
            templates = voucher_edit_templates.load_templates(base_dir=base)
            custom = next(t for t in templates if t["name"] == "納品書にも印字")
            self.assertEqual(custom["target_vouchers"], ["03", "04", "05", "07"])
            self.assertEqual(custom["color"], "#ff9800")


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestEditorTemplateAndBadges(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_editor(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="UTEST_NEW", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def test_new_object_gets_current_template_targets(self) -> None:
        win = self._make_editor()
        # 既定は標準 03/04/05。
        item = win.add_text_rect(QRectF(100, 100, 80, 20), text="hi", auto_edit=False)
        self.assertEqual(item.target_vouchers, ["03", "04", "05"])
        # 全伝票テンプレートを選択してから作成すると全伝票が付く。
        zen = next(t for t in win._templates if t["name"] == "全伝票")
        win._on_template_selected(zen)
        item2 = win.add_text_rect(QRectF(120, 120, 80, 20), text="ho", auto_edit=False)
        self.assertEqual(item2.target_vouchers, zen["target_vouchers"])

    def test_template_buttons_highlight_exactly_one_current_target(self) -> None:
        win = self._make_editor()
        selected = [
            name for name, button in win._template_actions.items()
            if button.isChecked() and button.property("reflectTargetSelected")
        ]
        self.assertEqual(selected, ["標準"])

        target = next(t for t in win._templates if t["name"] == "全伝票")
        win._on_template_selected(target)
        selected = [
            name for name, button in win._template_actions.items()
            if button.isChecked() and button.property("reflectTargetSelected")
        ]
        self.assertEqual(selected, ["全伝票"])
        self.assertEqual(win.current_target_vouchers, target["target_vouchers"])

    def test_selected_object_updates_template_highlight(self) -> None:
        win = self._make_editor()
        item = win.add_text_rect(
            QRectF(100, 100, 80, 20),
            text="hi",
            auto_edit=False,
            target_vouchers=["05"],
        )
        win._select_only(item)
        self.assertEqual(win._current_template_name, "梱包のみ")
        self.assertTrue(win._template_actions["梱包のみ"].isChecked())
        self.assertTrue(
            win._template_actions["梱包のみ"].property("reflectTargetSelected")
        )

    def test_reflect_target_highlight_has_light_and_dark_styles(self) -> None:
        from app.gui import DARK_STYLESHEET, LIGHT_STYLESHEET

        selector = 'QPushButton#reflectTargetButton[reflectTargetSelected="true"]'
        self.assertIn(selector, LIGHT_STYLESHEET)
        self.assertIn(selector, DARK_STYLESHEET)

    def test_template_change_does_not_change_selected_object_targets(self) -> None:
        win = self._make_editor()
        item = win.add_text_rect(QRectF(100, 100, 80, 20), text="hi", auto_edit=False)
        win._select_only(item)
        konpou = next(t for t in win._templates if t["name"] == "梱包のみ")
        win._on_template_selected(konpou)
        self.assertEqual(item.target_vouchers, ["03", "04", "05"])
        item2 = win.add_text_rect(QRectF(120, 120, 80, 20), text="ho", auto_edit=False)
        self.assertEqual(item2.target_vouchers, ["05"])

    def test_badges_shown_and_excluded_from_save(self) -> None:
        win = self._make_editor()
        win.add_text_rect(QRectF(100, 100, 80, 20), text="hi", auto_edit=False)
        win.refresh_badges()
        # 編集画面にバッヂが表示される。
        self.assertTrue(len(win._badges) >= 1)
        # 保存対象（serialize_objects）にはバッヂが含まれない。
        objs = win.serialize_objects()
        self.assertEqual(len(objs), 1)
        self.assertNotIn(win._badges[0], [o for o in objs])
        # バッヂは編集レイヤー（保存対象）にも含まれない。
        self.assertNotIn(win._badges[0], win.edit_items())

    def test_target_vouchers_persist_in_serialized_object(self) -> None:
        win = self._make_editor()
        item = win.add_text_rect(QRectF(100, 100, 80, 20), text="hi", auto_edit=False)
        item.target_vouchers = ["03", "04", "05", "07"]
        objs = win.serialize_objects()
        self.assertEqual(objs[0]["target_vouchers"], ["03", "04", "05", "07"])

    def test_registered_template_appears_in_editor(self) -> None:
        from app import voucher_edit_templates
        from app.voucher_edit_window import VoucherEditWindow

        prev_home = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TKS_TO_KINTONE_HOME"] = tmp
            try:
                voucher_edit_templates.save_user_templates(
                    [{"name": "納品書にも印字",
                      "target_vouchers": ["03", "04", "05", "07"], "color": "#ff9800"}]
                )
                win = VoucherEditWindow(order_no="UTEST_TPL", background_pdf_bytes=b"")
                self.addCleanup(win.deleteLater)
                self.assertIn("納品書にも印字", win._template_actions)
                tpl = win._template_by_name("納品書にも印字")
                self.assertEqual(tpl["target_vouchers"], ["03", "04", "05", "07"])
            finally:
                if prev_home is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = prev_home


class TestHeaderAndDetailLayout(unittest.TestCase):
    def test_issue_cell_width_equals_delivery_cell_width(self) -> None:
        """発行日セル幅が納品日セル幅と一致し、双方に1.3倍の最大幅日付が収まること（要件2）。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app import voucher_service as vs
        from app.voucher_templates import (
            HDR_DELIVERY_X, FORM_HDR_LEFT, HDR_DELIVERY_RIGHT, DATA_X_PAD,
        )

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        issue_w = HDR_DELIVERY_X - FORM_HDR_LEFT
        delivery_w = HDR_DELIVERY_RIGHT - HDR_DELIVERY_X
        # 発行日セル幅 = 納品日セル幅（要件2）。
        self.assertAlmostEqual(issue_w, delivery_w, places=4)
        # 最大幅日付 26/12/31 が両セル内（左パディング控除後）に収まる（1.3倍）。
        max_date_w = pdfmetrics.stringWidth(
            "26/12/31", "HeiseiKakuGo-W5", vs.HEADER_NOUHIN_VALUE_FONT_SIZE
        )
        self.assertLessEqual(max_date_w, issue_w - DATA_X_PAD)
        self.assertLessEqual(max_date_w, delivery_w - DATA_X_PAD)

    def test_delivery_date_does_not_overlap_voucher_no_cell(self) -> None:
        """納品日データ（最大幅）が右隣の伝票Noセルに食い込まないこと（要件1）。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app import voucher_service as vs
        from app.voucher_templates import HDR_DELIVERY_X, HDR_DELIVERY_RIGHT, DATA_X_PAD

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        data_left = HDR_DELIVERY_X + DATA_X_PAD
        max_date_w = pdfmetrics.stringWidth(
            "26/12/31", "HeiseiKakuGo-W5", vs.HEADER_NOUHIN_VALUE_FONT_SIZE
        )
        # データ右端が納品日/伝票No 境界を超えない。
        self.assertLessEqual(data_left + max_date_w, HDR_DELIVERY_RIGHT)

    def test_row2_cells_are_ordered_without_overlap(self) -> None:
        """Row2 の各セル境界が単調増加で重なりがないこと（要件1）。"""
        from app.voucher_templates import HDR_ROW2_DIVS, FORM_HDR_LEFT, FORM_HDR_RIGHT

        bounds = [FORM_HDR_LEFT, *HDR_ROW2_DIVS, FORM_HDR_RIGHT]
        for a, b in zip(bounds, bounds[1:]):
            self.assertLess(a, b)

    def test_all_vouchers_build_with_max_delivery_date(self) -> None:
        """最大幅の納品日でも 01〜08 で PDF 生成できること（要件1）。"""
        from app import voucher_service as vs
        from app.voucher_templates import VOUCHER_IDS

        page = {
            "code_no": "001", "customer_name": "テスト得意先", "order_no": "5218869",
            "delivery_date": "26/12/31", "ship_type": "販PM",
            "voucher_no": "1234567", "trade_type": "掛", "operator": "担当太郎",
            "details": [{"name": "品", "dims": "（10 * 20 ミリ）", "qty": "1枚",
                         "unit_price": "800", "amount": "3,846", "note_lines": []}],
        }
        for vid in VOUCHER_IDS:
            pdf = vs.build_vouchers_pdf_bytes([vid], {"pages": [page]})
            self.assertTrue(pdf.startswith(b"%PDF"), f"{vid} の生成に失敗")

    def test_order_no_left_aligns_with_operator_left(self) -> None:
        """上段「受注No」の左枠線が下段「入力者名」の左枠線と縦に揃うこと（要件）。"""
        from app.voucher_templates import (
            HDR_ORDER_NO_X, HDR_OPERATOR_X, HDR_ROW1_DIVS, HDR_ROW2_DIVS,
        )

        # 受注Noセル左端 = 入力者名セル左端。
        self.assertEqual(HDR_ORDER_NO_X, HDR_OPERATOR_X)
        # 境界リスト上でも同一X（Row1の受注No境界 = Row2の入力者名境界）。
        self.assertEqual(HDR_ROW1_DIVS[1], HDR_ROW2_DIVS[4])

    def test_order_no_data_fits_without_overlapping_shiage(self) -> None:
        """受注Noデータがセル内に収まり、仕上日セル・AM/PM欄(371〜)に食い込まないこと。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app import voucher_service as vs
        from app.voucher_templates import HDR_ORDER_NO_X, HDR_AMPM_X, DATA_X_PAD

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        # 7桁の受注No（一般的な最大幅）が1.3倍でも右端境界(=仕上日セル左端)を超えない。
        w = pdfmetrics.stringWidth("5218869", "HeiseiKakuGo-W5", vs.HEADER_MAIN_VALUE_FONT_SIZE)
        self.assertLessEqual(HDR_ORDER_NO_X + DATA_X_PAD + w, HDR_AMPM_X)

    def test_code_no_cell_width_matches_issue_date(self) -> None:
        """コードNo列の幅が発行日列の幅と一致すること（要件1）。"""
        from app.voucher_templates import (
            HDR_ROW1_DIVS, HDR_DELIVERY_X, FORM_HDR_LEFT,
        )

        code_no_w = HDR_ROW1_DIVS[0] - FORM_HDR_LEFT
        issue_w = HDR_DELIVERY_X - FORM_HDR_LEFT
        self.assertEqual(code_no_w, issue_w)
        # コードNo列右端と発行日列右端が同一Xで縦に揃っていること。
        self.assertEqual(HDR_ROW1_DIVS[0], HDR_DELIVERY_X)

    def test_issue_date_fits_in_cell(self) -> None:
        """発行日の日付が1.3倍フォントでも枠内に収まること（要件1/2）。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app import voucher_service
        from app.voucher_templates import HDR_DELIVERY_X, FORM_HDR_LEFT, DATA_X_PAD

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        # 最大幅の発行日（例 26/12/31）が1.3倍でも発行日セル右端を超えない。
        w = pdfmetrics.stringWidth(
            "26/12/31", "HeiseiKakuGo-W5", voucher_service.HEADER_MAIN_VALUE_FONT_SIZE
        )
        self.assertLessEqual(FORM_HDR_LEFT + DATA_X_PAD + w, HDR_DELIVERY_X)

    def test_detail_lower_offset_moved_down_further(self) -> None:
        """2段目の下段ベースラインが前回(21)より約1mm(2.83pt)下がっていること（要件3）。"""
        from app.voucher_templates import (
            DET_LOWER_OFFSET, DET_UPPER_OFFSET, FORM_DETAIL_ROW_H,
        )

        self.assertAlmostEqual(DET_LOWER_OFFSET, 21.0 + 2.83, delta=0.05)
        self.assertGreater(DET_LOWER_OFFSET, DET_UPPER_OFFSET)
        # 行の枠線（行高26pt）にはみ出さないこと。
        self.assertLess(DET_LOWER_OFFSET, FORM_DETAIL_ROW_H)

    def test_qty_data_centered_in_cell(self) -> None:
        """数量データの2段目がセルの高さ方向中央あたりにあること（要件2）。"""
        from app.voucher_templates import (
            DET_QTY_LOWER_OFFSET, DET_LOWER_OFFSET, DET_UPPER_OFFSET,
            FORM_DETAIL_ROW_H,
        )

        # 上段コード(yu)より下、かつ他列の下段(yl=DET_LOWER_OFFSET)より上＝中央寄り。
        self.assertGreater(DET_QTY_LOWER_OFFSET, DET_UPPER_OFFSET)
        self.assertLess(DET_QTY_LOWER_OFFSET, DET_LOWER_OFFSET)
        # 行高(26pt)の幾何中央(13pt)付近に収まっていること。
        center = FORM_DETAIL_ROW_H / 2
        self.assertLessEqual(abs(DET_QTY_LOWER_OFFSET - center), 4.0)
        # 枠線(行高)内に収まること。
        self.assertLess(DET_QTY_LOWER_OFFSET, FORM_DETAIL_ROW_H)

    def test_wh_moved_right_by_1cm(self) -> None:
        """WH(寸法)表示の右揃え基準が約1cm(28.35pt)右へ移動していること（要件2）。"""
        from app.voucher_templates import DIM_SHIFT_LEFT, DET_NAME_RX

        # 旧位置は DET_NAME_RX - 28.35。新位置は DET_NAME_RX - DIM_SHIFT_LEFT。
        old_x = DET_NAME_RX - 28.35
        new_x = DET_NAME_RX - DIM_SHIFT_LEFT
        self.assertAlmostEqual(new_x - old_x, 28.35, places=2)
        self.assertAlmostEqual(DIM_SHIFT_LEFT, 0.0, places=2)


class TestShippingCellWidthAndSummaryShift(unittest.TestCase):
    """出荷区分セル拡張・境界右移動・会社情報右移動・摘要上段移動の検証。"""

    def test_shipping_cell_widened(self) -> None:
        """出荷区分セル幅が右シフト分だけ拡張されていること。"""
        from app.voucher_templates import (
            HDR_TRADE_RIGHT, HDR_OPERATOR_X, HDR_SHIPPING_SHIFT,
        )

        self.assertGreater(HDR_SHIPPING_SHIFT, 0.0)
        ship_w = HDR_OPERATOR_X - HDR_TRADE_RIGHT
        # 旧幅34pt + シフト分。
        self.assertAlmostEqual(ship_w, 34.0 + HDR_SHIPPING_SHIFT, places=4)

    def test_shipping_value_fits_in_cell(self) -> None:
        """店PM/販PM/直PM/倉PM（全角ＰＭ含む）が1.3倍でもセル内に収まること（要件5）。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app import voucher_service as vs
        from app.voucher_templates import (
            HDR_TRADE_RIGHT, HDR_OPERATOR_X, DATA_X_PAD,
        )

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        data_left = HDR_TRADE_RIGHT + DATA_X_PAD
        for s in ("店PM", "販PM", "直PM", "倉PM", "店ＰＭ", "販ＰＭ"):
            w = pdfmetrics.stringWidth(
                s, "HeiseiKakuGo-W5", vs.HEADER_SHIPPING_VALUE_FONT_SIZE
            )
            self.assertLessEqual(
                data_left + w, HDR_OPERATOR_X, f"{s} が出荷区分セルからはみ出す"
            )

    def test_shipping_font_unchanged_1_3x(self) -> None:
        """出荷区分データフォントは1.3倍指定を維持していること（要件5）。"""
        from app import voucher_service as vs

        self.assertAlmostEqual(
            vs.HEADER_SHIPPING_VALUE_FONT_SIZE, vs.DATA_FONT_SIZE * 1.3, places=4
        )

    def test_order_no_left_aligns_with_operator_left_after_shift(self) -> None:
        """受注No左枠線 = 入力者名左枠線 = 得意先名右枠線 が維持されていること（要件2/4）。"""
        from app.voucher_templates import (
            HDR_ORDER_NO_X, HDR_OPERATOR_X, HDR_ROW1_DIVS, HDR_ROW2_DIVS,
        )

        self.assertEqual(HDR_ORDER_NO_X, HDR_OPERATOR_X)
        # 得意先名右枠線(Row1境界[1]) = 受注No左枠線 = 入力者名左枠線(Row2境界[4])。
        self.assertEqual(HDR_ROW1_DIVS[1], HDR_OPERATOR_X)
        self.assertEqual(HDR_ROW2_DIVS[4], HDR_OPERATOR_X)

    def test_boundary_group_moved_right(self) -> None:
        """整列境界が旧位置(293)より右へ移動していること（要件2）。"""
        from app.voucher_templates import HDR_OPERATOR_X, HDR_RIGHT_SHIFT

        self.assertAlmostEqual(HDR_OPERATOR_X, 293.0 + HDR_RIGHT_SHIFT, places=4)
        self.assertGreater(HDR_OPERATOR_X, 293.0)

    def test_operator_and_shiage_cell_widths_preserved(self) -> None:
        """入力者名・仕上日・AM/PMセル幅が右移動後も維持されていること（要件4）。"""
        from app.voucher_templates import (
            HDR_OPERATOR_X, HDR_AMPM_X, FORM_HDR_RIGHT, HDR_AMPM_WIDEN,
        )

        # 入力者名セル幅は基準78ptから AM・PM拡張分(HDR_AMPM_WIDEN)を分けた値。
        self.assertAlmostEqual(HDR_AMPM_X - HDR_OPERATOR_X, 78.0 - HDR_AMPM_WIDEN, places=4)
        # 仕上日 / AM・PM セル幅は基準49ptに AM・PM拡張分を足した値（AM・PM 1.2倍化対応）。
        self.assertAlmostEqual(FORM_HDR_RIGHT - HDR_AMPM_X, 49.0 + HDR_AMPM_WIDEN, places=4)

    def test_company_moved_right_and_not_overlapping_header(self) -> None:
        """会社ロゴ・会社名が右へ移動し、ヘッダー枠と重ならないこと（要件3）。"""
        from app.voucher_templates import (
            COMPANY_LOGO_X, COMPANY_INFO_X, FORM_HDR_RIGHT, HDR_RIGHT_SHIFT,
        )

        # 同量右へ移動。
        self.assertAlmostEqual(COMPANY_LOGO_X, 423.0 + HDR_RIGHT_SHIFT, places=4)
        self.assertAlmostEqual(COMPANY_INFO_X, 448.0 + HDR_RIGHT_SHIFT, places=4)
        # ロゴ・会社名はヘッダー枠右端より右（重ならない）。
        self.assertGreater(COMPANY_LOGO_X, FORM_HDR_RIGHT)
        self.assertGreater(COMPANY_INFO_X, FORM_HDR_RIGHT)

    def test_company_name_within_page(self) -> None:
        """会社名が用紙右端からはみ出さないこと（要件3）。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app.voucher_templates import COMPANY_INFO_X, FORM_MR

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        w = pdfmetrics.stringWidth("まねきや硝子株式会社", "HeiseiKakuGo-W5", 16)
        self.assertLessEqual(COMPANY_INFO_X + w, FORM_MR)

    def test_row2_cells_ordered_after_shift(self) -> None:
        """右移動後も Row2 各境界が単調増加（重なりなし）であること。"""
        from app.voucher_templates import HDR_ROW2_DIVS, FORM_HDR_LEFT, FORM_HDR_RIGHT

        bounds = [FORM_HDR_LEFT, *HDR_ROW2_DIVS, FORM_HDR_RIGHT]
        for a, b in zip(bounds, bounds[1:]):
            self.assertLess(a, b)

    def test_finish_date_within_shifted_cell(self) -> None:
        """仕上日の月日が右移動した仕上日セル内に収まること（要件4）。"""
        from app.voucher_templates import (
            HDR_SHIAGE_DAY_LABEL_RX, HDR_SHIAGE_MONTH_DATA_RX,
            HDR_ROW1_DIVS, FORM_HDR_RIGHT,
        )

        self.assertGreaterEqual(HDR_SHIAGE_MONTH_DATA_RX, HDR_ROW1_DIVS[-1])
        self.assertLessEqual(HDR_SHIAGE_DAY_LABEL_RX, FORM_HDR_RIGHT)

    def test_summary_upper_moved_up_and_gap_widened(self) -> None:
        """摘要上段だけ上へ移動し、下段との間隔が広がること。下段は不変（要件6）。"""
        from app import voucher_service as vs

        old_upper = vs.FORM_SUM_BOT + 12.0
        old_lower = vs.FORM_SUM_BOT + 3.0
        new_upper = vs._summary_line_y(0)
        new_lower = vs._summary_line_y(1)
        # 上段は上へ（1〜3pt程度）。
        self.assertGreater(new_upper, old_upper)
        self.assertLessEqual(new_upper - old_upper, 3.0)
        # 下段は変わらない。
        self.assertAlmostEqual(new_lower, old_lower, places=4)
        # 間隔が旧(9pt)より広がる。
        self.assertGreater(new_upper - new_lower, old_upper - old_lower)

    def test_summary_upper_not_overlapping_lower_or_bkno(self) -> None:
        """摘要上段が下段・物件No行と重ならないこと（要件6）。"""
        from app import voucher_service as vs

        # 上段 > 下段 > 物件No行（物件Noは FORM_BKNO_BOT 付近）。
        self.assertGreater(vs._summary_line_y(0), vs._summary_line_y(1))
        self.assertGreater(vs._summary_line_y(1), vs.FORM_BKNO_BOT)


class TestTradeCustomerFontsAndStampBoxes(unittest.TestCase):
    """取引区分=出荷区分フォント、得意先名1.2倍、印枠1.3倍・1cm左を検証。"""

    # ── 取引区分・出荷区分フォント ──────────────────────────────────────────
    def test_trade_font_equals_shipping_font(self) -> None:
        """取引区分データフォント = 出荷区分データフォント（1.3倍）であること（要件1）。"""
        from app import voucher_service as vs

        self.assertAlmostEqual(
            vs.HEADER_TRADE_VALUE_FONT_SIZE, vs.HEADER_SHIPPING_VALUE_FONT_SIZE, places=4
        )
        self.assertAlmostEqual(
            vs.HEADER_TRADE_VALUE_FONT_SIZE, vs.DATA_FONT_SIZE * 1.3, places=4
        )

    def test_trade_value_fits_in_cell(self) -> None:
        """取引区分データ（売上/加工/現金）が1.3倍でもセル内に収まること（要件1/2）。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app import voucher_service as vs
        from app.voucher_templates import (
            HDR_VOUCHER_RIGHT, HDR_TRADE_RIGHT, DATA_X_PAD,
        )

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        data_left = HDR_VOUCHER_RIGHT + DATA_X_PAD
        for s in ("売上", "加工", "現金", "掛"):
            w = pdfmetrics.stringWidth(
                s, "HeiseiKakuGo-W5", vs.HEADER_TRADE_VALUE_FONT_SIZE
            )
            self.assertLessEqual(
                data_left + w, HDR_TRADE_RIGHT, f"{s} が取引区分セルからはみ出す"
            )

    def test_shipping_still_fits_after_trade_expansion(self) -> None:
        """取引区分拡張後も出荷区分データがセル内に収まること（要件2）。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app import voucher_service as vs
        from app.voucher_templates import HDR_TRADE_RIGHT, HDR_OPERATOR_X, DATA_X_PAD

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        data_left = HDR_TRADE_RIGHT + DATA_X_PAD
        for s in ("店ＰＭ", "販ＰＭ", "直PM", "倉PM"):
            w = pdfmetrics.stringWidth(
                s, "HeiseiKakuGo-W5", vs.HEADER_SHIPPING_VALUE_FONT_SIZE
            )
            self.assertLessEqual(data_left + w, HDR_OPERATOR_X)

    def test_trade_cell_widened(self) -> None:
        """取引区分セル幅が右シフト分だけ広がっていること（要件2）。"""
        from app.voucher_templates import (
            HDR_VOUCHER_RIGHT, HDR_TRADE_RIGHT, HDR_TRADE_SHIFT,
        )

        self.assertGreater(HDR_TRADE_SHIFT, 0.0)
        self.assertAlmostEqual(
            HDR_TRADE_RIGHT - HDR_VOUCHER_RIGHT, 28.0 + HDR_TRADE_SHIFT, places=4
        )

    def test_alignment_and_company_preserved(self) -> None:
        """受注No左=入力者名左、会社情報がヘッダー枠右より右、用紙内に維持されること。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app.voucher_templates import (
            HDR_ORDER_NO_X, HDR_OPERATOR_X, COMPANY_LOGO_X, COMPANY_INFO_X,
            FORM_HDR_RIGHT, FORM_MR,
        )

        self.assertEqual(HDR_ORDER_NO_X, HDR_OPERATOR_X)
        self.assertGreater(COMPANY_LOGO_X, FORM_HDR_RIGHT)
        self.assertGreater(COMPANY_INFO_X, FORM_HDR_RIGHT)
        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        w = pdfmetrics.stringWidth("まねきや硝子株式会社", "HeiseiKakuGo-W5", 16)
        self.assertLessEqual(COMPANY_INFO_X + w, FORM_MR)

    # ── 得意先名フォント ────────────────────────────────────────────────────
    def test_customer_font_is_1_44x(self) -> None:
        """得意先名データフォントが基準の1.2×1.2（＝1.44倍）であること。"""
        from app import voucher_service as vs

        self.assertAlmostEqual(
            vs.HEADER_CUSTOMER_VALUE_FONT_SIZE, vs.DATA_FONT_SIZE * 1.2 * 1.2, places=4
        )

    def test_customer_not_overlapping_order_no_cell(self) -> None:
        """得意先名データのクリップ幅が受注Noセル左端を越えないこと（要件3）。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app import voucher_service as vs
        from app.voucher_templates import HDR_ROW1_DIVS, HDR_ORDER_NO_X, DATA_X_PAD

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        start = HDR_ROW1_DIVS[0] + DATA_X_PAD
        # クリップ後右端が受注Noセル左端を越えない（殿/御中分の余白も残る）。
        self.assertLessEqual(start + vs._customer_max_w(), HDR_ORDER_NO_X)
        self.assertGreater(vs._customer_max_w(), 0.0)

    def test_customer_drawn_with_1_2x_in_all_header_funcs(self) -> None:
        """01・指図書系・受領系の各ヘッダーで得意先名が1.2倍で描かれること（要件3）。"""
        from app import voucher_service as vs

        page = {"code_no": "1", "customer_name": "株式会社たくみ硝子店",
                "order_no": "1", "trade_type": "売上", "ship_type": "販PM",
                "operator": "担当", "delivery_date": "26/06/19", "voucher_no": "V1"}
        for func in (vs._draw_form_data_01, vs._draw_form_data_shizu,
                     vs._draw_delivery_data_common):
            c = _RecordingCanvas()
            func(c, dict(page))
            self.assertAlmostEqual(
                c.size_of("株式会社たくみ硝子店"),
                vs.HEADER_CUSTOMER_VALUE_FONT_SIZE, places=4,
                msg=f"{func.__name__} で得意先名が1.2倍でない",
            )
            # 取引区分も1.3倍。
            self.assertAlmostEqual(
                c.size_of("売上"), vs.HEADER_TRADE_VALUE_FONT_SIZE, places=4,
                msg=f"{func.__name__} で取引区分が1.3倍でない",
            )

    # ── 印枠サイズ・位置 ────────────────────────────────────────────────────
    def test_stamp_box_scaled_1_3x(self) -> None:
        """指図書系印枠の幅・高さが旧寸法の1.3倍であること（要件4）。"""
        from app.voucher_templates import (
            STAMP_W, STAMP_H, STAMP_BOX_SCALE, _STAMP_W_BASE, _STAMP_H_BASE,
        )

        self.assertAlmostEqual(STAMP_BOX_SCALE, 1.3, places=4)
        self.assertAlmostEqual(STAMP_W, _STAMP_W_BASE * 1.3, places=4)
        self.assertAlmostEqual(STAMP_H, _STAMP_H_BASE * 1.3, places=4)

    def test_stamp_box_shifted_left_1cm(self) -> None:
        """指図書系印枠X座標が約28.35pt左へ移動していること（要件5）。"""
        from app.voucher_templates import (
            STAMP_X, _STAMP_X_BASE, STAMP_BOX_SHIFT_X,
        )

        self.assertAlmostEqual(STAMP_BOX_SHIFT_X, -28.35, places=2)
        self.assertAlmostEqual(STAMP_X, _STAMP_X_BASE - 28.35, places=2)

    def test_delivery_stamp_box_scaled_1_3x(self) -> None:
        """受領書(08) 検印/配送者印枠も1.3倍であること（要件4）。"""
        from app.voucher_templates import (
            DELIV_STAMP_W, DELIV_STAMP_H, _DELIV_STAMP_W_BASE, _DELIV_STAMP_H_BASE,
        )

        self.assertAlmostEqual(DELIV_STAMP_W, _DELIV_STAMP_W_BASE * 1.3, places=4)
        self.assertAlmostEqual(DELIV_STAMP_H, _DELIV_STAMP_H_BASE * 1.3, places=4)

    def test_stamp_box_within_page_and_table(self) -> None:
        """印枠の右端が用紙内かつ表右端を越えないこと（要件4/5）。"""
        from app.voucher_templates import STAMP_X, STAMP_W, FORM_MR, TBL_COLS

        right = STAMP_X + STAMP_W
        self.assertLessEqual(right, FORM_MR)
        self.assertLessEqual(right, TBL_COLS[-1])

    def test_delivery_stamp_boxes_within_page(self) -> None:
        """受領書の検印/配送者印枠（2枠）が用紙右端を越えないこと（要件5）。"""
        from app.voucher_templates import (
            STAMP_X, DELIV_STAMP_W, DELIV_STAMP_GAP, FORM_MR,
        )

        x = STAMP_X - DELIV_STAMP_W - DELIV_STAMP_GAP
        right_box_right = x + 2 * DELIV_STAMP_W + DELIV_STAMP_GAP
        self.assertLessEqual(right_box_right, FORM_MR)

    def test_stamp_box_below_table(self) -> None:
        """指図書系印枠が表本体より下にあり重ならないこと（要件4）。"""
        from app.voucher_templates import (
            STAMP_H, STAMP_GAP, FORM_DETAIL_BOT,
        )

        box_top = FORM_DETAIL_BOT - STAMP_GAP
        self.assertLessEqual(box_top, FORM_DETAIL_BOT)
        self.assertGreater(STAMP_H, 0.0)

    def test_all_vouchers_build_with_trade_customer_stamps(self) -> None:
        """01〜08が取引区分1.3倍・得意先名1.2倍・印枠1.3倍で生成できること。"""
        from app import voucher_service as vs
        from app.voucher_templates import VOUCHER_IDS

        page = {
            "code_no": "40630", "customer_name": "株式会社たくみ硝子店",
            "order_no": "5218869", "issue_date": "26/12/31",
            "delivery_date": "26/12/31", "voucher_no": "Z737704",
            "trade_type": "売上", "ship_type": "店ＰＭ", "operator": "竹内（典）",
            "details": [{"name": "MT5", "dims": "（10 * 20 ミリ）", "qty": "1枚",
                         "unit_price": "800", "amount": "3,846", "note_lines": ["1,580 加"]}],
        }
        for vid in VOUCHER_IDS:
            pdf = vs.build_vouchers_pdf_bytes([vid], {"pages": [page]})
            self.assertTrue(pdf.startswith(b"%PDF"), f"{vid} の生成に失敗")


class _RealMetricsAmPmCanvas:
    """実フォント幅で stringWidth を返し、ellipse と線幅を記録するフェイクキャンバス。"""

    def __init__(self) -> None:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        try:
            pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        except Exception:
            pass
        self._pdfmetrics = pdfmetrics
        self.ellipses: list[tuple] = []
        self.line_widths: list[float] = []
        self._size: float | None = None

    def setFont(self, name, size, *a):
        self._size = size

    def stringWidth(self, text, font, size):
        return self._pdfmetrics.stringWidth(text, "HeiseiKakuGo-W5", size)

    def saveState(self): pass
    def restoreState(self): pass

    def setLineWidth(self, w):
        self.line_widths.append(w)

    def ellipse(self, x0, y0, x1, y1, stroke=1, fill=0):
        self.ellipses.append((x0, y0, x1, y1))

    def __getattr__(self, name):
        return lambda *a, **k: None


class TestAmPmFontAndCircle(unittest.TestCase):
    """AM・PM 文字1.2倍・丸印半径1.2倍・線幅維持・「なし」非描画を検証（要件1/2）。"""

    def test_ampm_text_font_is_1_2x(self) -> None:
        from app import voucher_service as vs

        self.assertAlmostEqual(vs.AMPM_TEXT_FONT_SIZE, 11.0 * 1.2, places=4)
        self.assertAlmostEqual(vs.AMPM_TEXT_BASE_FONT_SIZE, 11.0, places=4)

    def test_ampm_label_drawn_at_1_2x_in_structures(self) -> None:
        """01系・指図書系のヘッダー骨格で「AM・PM」が1.2倍で描かれること。"""
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_form_structure_01(c)
        self.assertAlmostEqual(
            c.size_of("AM・PM"), vs.AMPM_TEXT_FONT_SIZE, places=4,
            msg="_draw_form_structure_01 で AM・PM が1.2倍でない",
        )
        c = _RecordingCanvas()
        vs._draw_form_structure_shizu(c, "指　図　書　(1)")
        self.assertAlmostEqual(
            c.size_of("AM・PM"), vs.AMPM_TEXT_FONT_SIZE, places=4,
            msg="_draw_form_structure_shizu で AM・PM が1.2倍でない",
        )

    def test_circle_radius_is_1_2x_and_line_width_kept(self) -> None:
        """丸印の半径が基準フォント時の1.2倍、線幅は前回調整値(1.8)を維持すること。"""
        from app import voucher_service as vs

        base = vs.AMPM_TEXT_BASE_FONT_SIZE
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        for sel, seg in (("AM", "AM"), ("PM", "PM")):
            c = _RealMetricsAmPmCanvas()
            vs._draw_ampm_circle(c, sel)
            self.assertEqual(len(c.ellipses), 1)
            x0, y0, x1, y1 = c.ellipses[0]
            rx, ry = (x1 - x0) / 2, (y1 - y0) / 2
            base_seg = pdfmetrics.stringWidth(seg, "HeiseiKakuGo-W5", base)
            self.assertAlmostEqual(rx, (base_seg / 2 + 3.0) * 1.2, places=2)
            self.assertAlmostEqual(ry, (base * 0.62) * 1.2, places=2)
            # 線幅は変更しない（前回調整済みの 1.8）。
            self.assertEqual(c.line_widths, [vs.AMPM_CIRCLE_LINE_WIDTH])

    def test_circle_within_cell_for_am_and_pm(self) -> None:
        """AM選択時・PM選択時とも丸印がAM・PMセル枠内に収まること（要件2）。"""
        from app import voucher_service as vs
        from app.voucher_templates import HDR_AMPM_X, FORM_HDR_RIGHT

        from app.voucher_templates import FORM_HDR_BOT, FORM_HDR_MID

        for sel in ("AM", "PM"):
            c = _RealMetricsAmPmCanvas()
            vs._draw_ampm_circle(c, sel)
            x0, y0, x1, y1 = c.ellipses[0]
            self.assertGreaterEqual(round(x0, 2), HDR_AMPM_X, f"{sel} 丸が左枠を越える")
            self.assertLessEqual(round(x1, 2), FORM_HDR_RIGHT, f"{sel} 丸が右枠を越える")
            # 縦方向も行2セル(FORM_HDR_BOT〜FORM_HDR_MID)内に収まること。
            self.assertGreaterEqual(round(y0, 2), FORM_HDR_BOT, f"{sel} 丸が下枠を越える")
            self.assertLessEqual(round(y1, 2), FORM_HDR_MID, f"{sel} 丸が上枠を越える")

    def test_ampm_baseline_centered_and_lowered(self) -> None:
        """AM・PM のベースラインが行2セル縦中央へ下がっていること（中央表示）。"""
        from app import voucher_service as vs
        from app.voucher_templates import FORM_HDR_BOT, FORM_HDR_MID

        # 旧位置(FORM_HDR_BOT+8.0)より下げる＝Yが小さくなる。
        self.assertLess(vs.AMPM_BASELINE_Y, FORM_HDR_BOT + 8.0)
        # 文字の視覚中心(ベースライン+fs*0.35)がセル縦中央付近に来ること。
        cell_center = (FORM_HDR_BOT + FORM_HDR_MID) / 2
        visual_center = vs.AMPM_BASELINE_Y + vs.AMPM_TEXT_FONT_SIZE * 0.35
        self.assertAlmostEqual(visual_center, cell_center, delta=0.5)

    def test_ampm_label_drawn_at_centered_baseline(self) -> None:
        """ヘッダー骨格で AM・PM ラベルが中央化したベースラインYで描かれること。"""
        from app import voucher_service as vs

        class _YCanvas(_RecordingCanvas):
            def __init__(self):
                super().__init__()
                self.ys = {}

            def drawCentredString(self, cx, y, text):
                self.ys[str(text)] = y
                super().drawCentredString(cx, y, text)

        c = _YCanvas()
        vs._draw_form_structure_01(c)
        self.assertAlmostEqual(c.ys["AM・PM"], vs.AMPM_BASELINE_Y, places=4)

    def test_ampm_text_fits_in_cell(self) -> None:
        """「AM・PM」文字が1.2倍でもセル枠内に収まること（要件1）。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app import voucher_service as vs
        from app.voucher_templates import HDR_AMPM_X, FORM_HDR_RIGHT

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        w = pdfmetrics.stringWidth("AM・PM", "HeiseiKakuGo-W5", vs.AMPM_TEXT_FONT_SIZE)
        self.assertLessEqual(w, FORM_HDR_RIGHT - HDR_AMPM_X)

    def test_no_circle_when_none(self) -> None:
        """「なし」/空のときは丸を描かないこと（要件1/2・既存仕様維持）。"""
        from app import voucher_service as vs

        for val in ("none", "None", "", "  ", None):
            c = _RealMetricsAmPmCanvas()
            vs._draw_ampm_circle(c, val)
            self.assertEqual(len(c.ellipses), 0, f"{val!r} で丸が描かれた")


class TestDeliverySummaryPropertyFonts(unittest.TestCase):
    """納品書(07)・受領書(08)の摘要/物件Noデータが他伝票と同フォントであることを検証（要件3/4）。"""

    def _page(self):
        return {
            "code_no": "40630", "customer_name": "得意先",
            "order_no": "1", "delivery_date": "26/06/19", "voucher_no": "V1",
            "trade_type": "売上", "ship_type": "販PM", "operator": "担当",
            "summary_lines": ["☆吉祥院倉庫入れ☆", "【５１０号室】"],
            "property_lines": ["40630808 シャングリラ京都"],
            "details": [],
        }

    def test_summary_rows_use_shared_value_fonts(self) -> None:
        """_draw_summary_rows（07/08共通）で摘要・物件Noが専用フォントで描かれること。"""
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_summary_rows(c, self._page())
        self.assertAlmostEqual(
            c.size_of("☆吉祥院倉庫入れ☆"), vs.SUMMARY_VALUE_FONT_SIZE, places=4
        )
        self.assertAlmostEqual(
            c.size_of("【５１０号室】"), vs.SUMMARY_VALUE_FONT_SIZE, places=4
        )
        self.assertAlmostEqual(
            c.size_of("40630808 シャングリラ京都"), vs.PROPERTY_VALUE_FONT_SIZE, places=4
        )

    def test_07_08_match_other_vouchers_font(self) -> None:
        """07/08の摘要・物件Noデータフォントが01の値と一致すること（要件3）。"""
        from app import voucher_service as vs

        # 01 と同じ定数を参照していること（共通化・要件4）。
        self.assertAlmostEqual(
            vs.SUMMARY_VALUE_FONT_SIZE,
            vs.SUMMARY_VALUE_BASE_FONT_SIZE * 0.8 * 1.1,
            places=4,
        )
        self.assertAlmostEqual(
            vs.PROPERTY_VALUE_FONT_SIZE,
            vs.PROPERTY_VALUE_BASE_FONT_SIZE * 0.8 * 1.1,
            places=4,
        )

    def test_07_08_build_with_summary_property(self) -> None:
        from app import voucher_service as vs

        for vid in ("07", "08"):
            pdf = vs.build_vouchers_pdf_bytes([vid], {"pages": [self._page()]})
            self.assertTrue(pdf.startswith(b"%PDF"), f"{vid} の生成に失敗")


class _YRecordingCanvas:
    """drawString 系の (text, x, y, size) を記録するフェイクキャンバス。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float, float, float | None]] = []
        self._size: float | None = None

    def setFont(self, name, size, *a):
        self._size = size

    def stringWidth(self, text, font, size):
        return 0.0

    def drawString(self, x, y, text):
        self.calls.append((str(text), x, y, self._size))

    drawRightString = drawString
    drawCentredString = drawString

    def __getattr__(self, name):
        return lambda *a, **k: None

    def y_of(self, text: str) -> float | None:
        for drawn, _x, y, _s in self.calls:
            if drawn == text:
                return y
        return None


class TestHeaderBottomAlignAndSummary(unittest.TestCase):
    """ヘッダーデータの下寄せ（要件3）と摘要・物件Noの1.3倍（要件4）を検証。"""

    def _page(self):
        return {
            "code_no": "001", "customer_name": "テスト得意先",
            "order_no": "", "issue_date": "26/06/30",
            "delivery_date": "26/06/19", "voucher_no": "V1",
            "trade_type": "掛", "ship_type": "販PM", "operator": "担当",
            "summary_lines": ["摘要データ1", "摘要データ2"],
            "property_lines": ["物件データ"],
            "details": [],
        }

    def test_header_value_bottom_aligned(self) -> None:
        """ヘッダーデータのベースラインがセル下線ギリギリ（下寄せ）であること（要件3）。"""
        from app.voucher_templates import (
            HDR_DATA_Y_INNER, FORM_HDR_MID, FORM_HDR_BOT,
        )

        # 行下端の罫線からベースラインまでの余白がごく僅か（下線ギリギリ）であること。
        self.assertLessEqual(HDR_DATA_Y_INNER, 2.0)
        self.assertGreater(HDR_DATA_Y_INNER, 0.0)  # 下線に潰れて重ならない
        c = _YRecordingCanvas()
        from app import voucher_service as vs
        vs._draw_form_data_01(c, self._page())
        # 行1データ(コードNo)は行1下線 FORM_HDR_MID のすぐ上に来る。
        self.assertAlmostEqual(c.y_of("001"), FORM_HDR_MID + HDR_DATA_Y_INNER, places=4)
        # 行2データ(発行日)は行2下線 FORM_HDR_BOT のすぐ上に来る。
        self.assertAlmostEqual(c.y_of("26/06/30"), FORM_HDR_BOT + HDR_DATA_Y_INNER, places=4)

    def test_summary_and_property_value_fonts_scaled_up_1_1(self) -> None:
        """摘要・物件Noデータが直前バージョンから1.1倍で描画されること。"""
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_form_data_01(c, self._page())
        self.assertAlmostEqual(c.size_of("摘要データ1"), vs.SUMMARY_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("摘要データ2"), vs.SUMMARY_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("物件データ"), vs.PROPERTY_VALUE_FONT_SIZE, places=4)
        # ラベル「摘　要」「物件No」はデータ描画関数には現れない（構造側で据え置き描画）。
        self.assertIsNone(c.size_of("摘　要"))

    def test_shizu_summary_property_fonts_scaled_up_1_1(self) -> None:
        """指図書系(03-06)でも摘要・物件Noデータが1.1倍であること。"""
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_form_data_shizu(c, self._page())
        self.assertAlmostEqual(c.size_of("摘要データ1"), vs.SUMMARY_VALUE_FONT_SIZE, places=4)
        self.assertAlmostEqual(c.size_of("物件データ"), vs.PROPERTY_VALUE_FONT_SIZE, places=4)


class TestProcessLabelFont(unittest.TestCase):
    """加工名ラベルの1.2倍化（要件5）を検証。"""

    def test_process_label_font_is_1_2x(self) -> None:
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_lower_section(c)
        for label in ("エッジング", "広幅", "工場切", "DM-10", "BOB", "印刷",
                      "フィルム貼", "Rとり"):
            self.assertAlmostEqual(
                c.size_of(label), vs.PROCESS_LABEL_FONT_SIZE, places=4,
                msg=f"{label} のフォントが1.2倍でない",
            )

    def test_lower_section_draws_film_and_rtori_labels(self) -> None:
        """伝票PDF左下の加工名欄に フィルム貼・Rとり が描画されること。"""
        from app import voucher_service as vs

        c = _RecordingCanvas()
        vs._draw_lower_section(c)
        self.assertIsNotNone(c.size_of("フィルム貼"))
        self.assertIsNotNone(c.size_of("Rとり"))

    def test_process_label_does_not_overlap_cell_border(self) -> None:
        """1.2倍ラベルがセル枠（区切り線）と重ならず収まること（要件5）。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from app import voucher_service as vs
        from app.voucher_templates import (
            PROC_LABELS, FORM_LWR_TOP, FORM_LWR_BOT,
        )

        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        n = len(PROC_LABELS)
        item_h = (FORM_LWR_TOP - FORM_LWR_BOT) / n
        fs = vs.PROCESS_LABEL_FONT_SIZE
        # 各セル内に下寄せ中央でラベルを描いても、上端区切り線まで余白が残ること。
        baseline_off = (item_h - fs) / 2 + 0.5      # item_bot からの上方向
        ascender = fs * 0.8                           # おおよその文字上端
        self.assertLess(baseline_off + ascender, item_h)

    def test_proc_labels_frame_unchanged(self) -> None:
        """加工名一覧の外枠13行と名称なし13枠目を維持すること。"""
        from app.voucher_templates import PROC_LABELS

        # 合計13行の枠は不変。13枠目は従来どおり名称なし。
        self.assertEqual(len(PROC_LABELS), 13)
        self.assertEqual(PROC_LABELS[-1], "")
        self.assertIn("フィルム貼", PROC_LABELS)
        self.assertIn("Rとり", PROC_LABELS)


class TestAllVouchersBuildWithFullHeader(unittest.TestCase):
    """01〜08 すべてがフル項目（1.3倍ヘッダー・摘要・加工名）で生成できること。"""

    def test_all_vouchers_build(self) -> None:
        from app import voucher_service as vs
        from app.voucher_templates import VOUCHER_IDS

        page = {
            "code_no": "40630", "customer_name": "株式会社たくみ硝子店",
            "order_no": "5218869", "issue_date": "26/12/31",
            "delivery_date": "26/12/31", "voucher_no": "Z737704",
            "trade_type": "売上", "ship_type": "販PM", "operator": "竹内（典）",
            "summary_lines": ["合計 1,580 加工", "東大阪 倉庫まわり"],
            "property_lines": ["大阪市中央区 物件A"],
            "sales_rep": "船橋", "construction_rep": "山田",
            "row_process_checks": {"エッジング": True, "広幅": True, "印刷": True},
            "details": [
                {"name": "MT5 四方 磨き", "dims": "（1303 * 1061 ミリ）",
                 "qty_spec": "510中", "qty": "1枚",
                 "unit_price": "1.382㎡", "amount": "1.382㎡",
                 "note_lines": ["1,580 加", "7,594 倉庫ま"]},
            ],
        }
        for vid in VOUCHER_IDS:
            pdf = vs.build_vouchers_pdf_bytes([vid], {"pages": [page]})
            self.assertTrue(pdf.startswith(b"%PDF"), f"{vid} の生成に失敗")


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestEditorTemplatePanelAndSave(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_editor(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="UTEST_PANEL", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def test_template_panel_is_vertical_with_heading(self) -> None:
        from PySide6.QtWidgets import QPushButton, QLabel, QVBoxLayout

        win = self._make_editor()
        panel = win._template_panel
        self.assertIsInstance(panel.layout(), QVBoxLayout)
        labels = [l.text() for l in panel.findChildren(QLabel)]
        self.assertIn("反映先", labels)
        btn_texts = {b.text() for b in panel.findChildren(QPushButton)}
        # 固定テンプレートはロックバッヂ付きで表示する（要件3・10）。
        self.assertTrue({"🔒 標準", "🔒 全伝票", "指図書のみ", "梱包のみ"} <= btn_texts)
        # 内部キー（_template_actions のキー）はロックバッヂを含まない（要件10）。
        self.assertIn("標準", win._template_actions)
        self.assertIn("全伝票", win._template_actions)
        self.assertNotIn("🔒 標準", win._template_actions)

    def test_template_selection_highlight(self) -> None:
        win = self._make_editor()
        win._on_template_selected(win._template_by_name("全伝票"))
        checked = [n for n, b in win._template_actions.items() if b.isChecked()]
        self.assertEqual(checked, ["全伝票"])

    def test_save_message_is_short(self) -> None:
        from unittest import mock
        from app import voucher_edit_window

        win = self._make_editor()
        with mock.patch.object(win, "_persist", return_value=True), \
                mock.patch.object(voucher_edit_window.QMessageBox, "information") as info:
            win.save()
        info.assert_called_once()
        # 「保存しました」のみ（反映先説明文を含まない）。
        msg = info.call_args.args[2]
        self.assertEqual(msg, "保存しました")
        self.assertNotIn("反映", msg)

    def test_template_buttons_have_context_menu(self) -> None:
        from PySide6.QtCore import Qt

        win = self._make_editor()
        for btn in win._template_actions.values():
            self.assertEqual(btn.contextMenuPolicy(), Qt.ContextMenuPolicy.CustomContextMenu)
        # 右クリックメニュー表示メソッドが存在する。
        self.assertTrue(hasattr(win, "_show_template_context_menu"))


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestEditorTemplateEditDelete(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_editor_with_home(self, tmp):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="UTEST_ED", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def _restore_home(self, prev):
        if prev is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = prev

    def test_edit_template_updates_name_targets_color(self) -> None:
        from unittest import mock
        from app import voucher_edit_window as vew
        from app import voucher_edit_templates as vet

        prev = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TKS_TO_KINTONE_HOME"] = tmp
            try:
                vet.save_user_templates(
                    [{"name": "旧名", "target_vouchers": ["03"], "color": "#111111"}])
                win = self._make_editor_with_home(tmp)
                updated = {"name": "新名", "target_vouchers": ["03", "04", "07"],
                           "color": "#ff9800", "badge": "新"}
                fake = mock.Mock()
                fake.exec.return_value = vew._TemplateRegisterDialog.DialogCode.Accepted
                fake.template.return_value = updated
                with mock.patch.object(vew, "_TemplateRegisterDialog", return_value=fake):
                    win._edit_template("旧名")
                saved = {t["name"]: t for t in vet.load_user_templates()}
                self.assertIn("新名", saved)
                self.assertNotIn("旧名", saved)
                self.assertEqual(saved["新名"]["target_vouchers"], ["03", "04", "07"])
                self.assertEqual(saved["新名"]["color"], "#ff9800")
                # パネルが再描画され新名ボタンがある。
                self.assertIn("新名", win._template_actions)
            finally:
                self._restore_home(prev)

    def test_delete_template_after_confirm(self) -> None:
        from unittest import mock
        from app import voucher_edit_window as vew
        from app import voucher_edit_templates as vet

        prev = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TKS_TO_KINTONE_HOME"] = tmp
            try:
                vet.save_user_templates(
                    [{"name": "消す", "target_vouchers": ["05"], "color": "#222222"}])
                win = self._make_editor_with_home(tmp)
                self.assertIn("消す", win._template_actions)
                with mock.patch.object(vew.QMessageBox, "question",
                                       return_value=vew.QMessageBox.StandardButton.Yes):
                    win._delete_template("消す")
                self.assertNotIn("消す", {t["name"] for t in vet.load_user_templates()})
                self.assertNotIn("消す", win._template_actions)
            finally:
                self._restore_home(prev)

    def test_delete_cancel_keeps_template(self) -> None:
        from unittest import mock
        from app import voucher_edit_window as vew
        from app import voucher_edit_templates as vet

        prev = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TKS_TO_KINTONE_HOME"] = tmp
            try:
                vet.save_user_templates(
                    [{"name": "残す", "target_vouchers": ["05"], "color": "#222222"}])
                win = self._make_editor_with_home(tmp)
                with mock.patch.object(vew.QMessageBox, "question",
                                       return_value=vew.QMessageBox.StandardButton.No):
                    win._delete_template("残す")
                self.assertIn("残す", {t["name"] for t in vet.load_user_templates()})
                self.assertIn("残す", win._template_actions)
            finally:
                self._restore_home(prev)

    def test_object_target_vouchers_preserved_after_template_delete(self) -> None:
        from unittest import mock
        from PySide6.QtCore import QRectF
        from app import voucher_edit_window as vew
        from app import voucher_edit_templates as vet

        prev = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TKS_TO_KINTONE_HOME"] = tmp
            try:
                vet.save_user_templates(
                    [{"name": "使用中", "target_vouchers": ["03", "07"], "color": "#333333"}])
                win = self._make_editor_with_home(tmp)
                item = win.add_text_rect(QRectF(100, 100, 80, 20), text="hi",
                                         auto_edit=False, target_vouchers=["03", "07"])
                with mock.patch.object(vew.QMessageBox, "question",
                                       return_value=vew.QMessageBox.StandardButton.Yes):
                    win._delete_template("使用中")
                # テンプレート削除だけでは既存オブジェクトの保存値を変更しない。
                self.assertEqual(item.target_vouchers, ["03", "07"])
            finally:
                self._restore_home(prev)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestTemplateLockAndDelete(unittest.TestCase):
    """反映先テンプレートのロックバッヂ・削除可否（要件3・7・8・12）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._prev = os.environ.get("TKS_TO_KINTONE_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._prev
        self._tmp.cleanup()

    def _make_editor(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="LOCK_TEST", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    # ── ロックバッヂ（要件12）──────────────────────────────────────────────
    def test_lock_badge_on_standard_and_all(self) -> None:
        win = self._make_editor()
        self.assertEqual(win._template_actions["標準"].text(), "🔒 標準")
        self.assertEqual(win._template_actions["全伝票"].text(), "🔒 全伝票")

    def test_internal_keys_stay_plain(self) -> None:
        win = self._make_editor()
        # 内部キー（保存名）はロックバッヂを含まない（要件10）。
        self.assertIn("標準", win._template_actions)
        self.assertIn("全伝票", win._template_actions)
        self.assertEqual(win._template_by_name("標準")["name"], "標準")
        self.assertEqual(win._template_by_name("全伝票")["name"], "全伝票")

    def test_no_lock_badge_on_deletable_presets(self) -> None:
        win = self._make_editor()
        self.assertEqual(win._template_actions["指図書のみ"].text(), "指図書のみ")
        self.assertEqual(win._template_actions["梱包のみ"].text(), "梱包のみ")

    # ── 削除可否（要件12）──────────────────────────────────────────────────
    def test_locked_templates_not_deletable(self) -> None:
        from app import voucher_edit_templates as vet

        self.assertFalse(vet.delete_template("標準"))
        self.assertFalse(vet.delete_template("全伝票"))
        names = {t["name"] for t in vet.load_templates()}
        self.assertIn("標準", names)
        self.assertIn("全伝票", names)

    def test_preset_templates_deletable(self) -> None:
        from app import voucher_edit_templates as vet

        self.assertTrue(vet.delete_template("指図書のみ"))
        self.assertTrue(vet.delete_template("梱包のみ"))
        names = {t["name"] for t in vet.load_templates()}
        self.assertNotIn("指図書のみ", names)
        self.assertNotIn("梱包のみ", names)

    def test_user_template_deletable(self) -> None:
        from app import voucher_edit_templates as vet

        vet.save_user_templates(
            [{"name": "ユーザーA", "target_vouchers": ["07"], "color": "#888888"}])
        self.assertTrue(vet.delete_template("ユーザーA"))
        self.assertNotIn("ユーザーA", {t["name"] for t in vet.load_templates()})

    def test_register_button_not_deletable_target(self) -> None:
        win = self._make_editor()
        # 「＋ テンプレ登録」ボタンはテンプレートアクションに含まれない（要件9）。
        self.assertNotIn("＋ テンプレ登録", win._template_actions)
        self.assertEqual(win._register_template_button.text(), "＋ テンプレ登録")

    # ── 削除後の挙動（要件8・12）──────────────────────────────────────────
    def test_deleted_preset_removed_from_panel(self) -> None:
        from unittest import mock
        from app import voucher_edit_window as vew

        win = self._make_editor()
        self.assertIn("指図書のみ", win._template_actions)
        with mock.patch.object(vew.QMessageBox, "question",
                               return_value=vew.QMessageBox.StandardButton.Yes):
            win._delete_template("指図書のみ")
        self.assertNotIn("指図書のみ", win._template_actions)

    def test_deleted_preset_persists_after_restart(self) -> None:
        from unittest import mock
        from app import voucher_edit_window as vew
        from app import voucher_edit_templates as vet

        win = self._make_editor()
        with mock.patch.object(vew.QMessageBox, "question",
                               return_value=vew.QMessageBox.StandardButton.Yes):
            win._delete_template("梱包のみ")
        # 再起動相当（再読み込み）でも復活しない（要件8）。
        names = {t["name"] for t in vet.load_templates()}
        self.assertNotIn("梱包のみ", names)
        win2 = self._make_editor()
        self.assertNotIn("梱包のみ", win2._template_actions)

    def test_selection_resets_to_standard_when_deleting_selected(self) -> None:
        from unittest import mock
        from app import voucher_edit_window as vew

        win = self._make_editor()
        win._on_template_selected(win._template_by_name("指図書のみ"))
        self.assertEqual(win._current_template_name, "指図書のみ")
        with mock.patch.object(vew.QMessageBox, "question",
                               return_value=vew.QMessageBox.StandardButton.Yes):
            win._delete_template("指図書のみ")
        self.assertEqual(win._current_template_name, "標準")
        self.assertEqual(win.current_target_vouchers, ["03", "04", "05"])

    # ── 確認ダイアログ（要件6・12）────────────────────────────────────────
    def test_delete_cancel_no_change(self) -> None:
        from unittest import mock
        from app import voucher_edit_window as vew
        from app import voucher_edit_templates as vet

        win = self._make_editor()
        with mock.patch.object(vew.QMessageBox, "question",
                               return_value=vew.QMessageBox.StandardButton.No):
            win._delete_template("指図書のみ")
        self.assertIn("指図書のみ", win._template_actions)
        self.assertIn("指図書のみ", {t["name"] for t in vet.load_templates()})

    def test_delete_yes_removes(self) -> None:
        from unittest import mock
        from app import voucher_edit_window as vew
        from app import voucher_edit_templates as vet

        win = self._make_editor()
        with mock.patch.object(vew.QMessageBox, "question",
                               return_value=vew.QMessageBox.StandardButton.Yes):
            win._delete_template("指図書のみ")
        self.assertNotIn("指図書のみ", win._template_actions)
        self.assertNotIn("指図書のみ", {t["name"] for t in vet.load_templates()})

    def test_locked_delete_does_not_prompt_or_remove(self) -> None:
        from unittest import mock
        from app import voucher_edit_window as vew
        from app import voucher_edit_templates as vet

        win = self._make_editor()
        with mock.patch.object(vew.QMessageBox, "question") as q:
            win._delete_template("標準")
        # 固定テンプレートは確認すら出さず、削除もしない（要件7）。
        q.assert_not_called()
        self.assertIn("標準", win._template_actions)
        self.assertIn("標準", {t["name"] for t in vet.load_templates()})

    def test_locked_context_menu_contains_default_action_only(self) -> None:
        from unittest import mock
        from app import voucher_edit_window as vew

        win = self._make_editor()
        with mock.patch.object(vew, "QMenu") as menu_cls:
            win._show_template_context_menu("標準", None)
        # 固定テンプレートでも一覧専用の既定設定と並び順リセットは利用できる。
        menu_cls.assert_called_once_with(win)
        texts = [call.args[0] for call in menu_cls.return_value.addAction.call_args_list]
        self.assertIn("既定に設定", texts)
        self.assertIn("反映先を既定順に戻す", texts)
        self.assertNotIn("編集", texts)
        self.assertNotIn("削除", texts)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestWindowFinishAndAmPmNone(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self):
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        win._on_add_row()
        input_row = getattr(win, "_new_input_row", None)
        if input_row is not None:
            logical_index = input_row.table_row_index
            if logical_index >= 0:
                win._table.removeRow(logical_index)
            win._new_input_row = None
            for row in win._rows:
                if row.table_row_index > logical_index:
                    row.table_row_index -= 1
        self.addCleanup(win.deleteLater)
        return win

    def test_finish_none_yields_none_finish_date(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.finish_none_check.setChecked(True)
        self.assertIsNone(win._collect_row(rw).finish_date)
        # 日付入力は無効化される。
        self.assertFalse(rw.date_edit.isEnabled())

    def test_ampm_none_yields_none_string(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.ampm_none.setChecked(True)
        self.assertEqual(win._collect_row(rw).am_pm, "none")

    def test_finish_none_row_is_valid(self) -> None:
        """仕上日「なし」でもバリデーションを通る（要件1）。"""
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        rw.finish_none_check.setChecked(True)
        row = win._collect_row(rw)
        self.assertIsNone(win._row_error_message(row))


class _WidthCanvas:
    """stringWidth を len(text)*size*k で近似するフェイクキャンバス。

    フォント自動縮小（draw_text_fit_width）の挙動を検証するために使う。
    drawString されたテキストとその時点のフォントサイズを記録する。
    """

    def __init__(self, k: float = 0.6) -> None:
        self.k = k
        self.drawn: list[tuple[float, str]] = []
        self._size: float | None = None

    def setFont(self, name, size, *a):
        self._size = size

    def stringWidth(self, text, font, size):
        return len(text) * size * self.k

    def drawString(self, x, y, text):
        self.drawn.append((self._size, str(text)))

    def __getattr__(self, name):
        return lambda *a, **k: None


class TestProductNameFitWidth(unittest.TestCase):
    """品名（商品名称）をフォント縮小で全文字表示する変更のテスト。"""

    BASE = 10.0
    MIN = 5.0
    MAX_W = 50.0

    def _fit(self, text, k=0.6):
        from app.voucher_service import draw_text_fit_width
        c = _WidthCanvas(k=k)
        fs = draw_text_fit_width(c, text, 0.0, 0.0, self.MAX_W,
                                 "HeiseiKakuGo-W5", self.BASE, self.MIN)
        return c, fs

    def test_short_name_keeps_base_font(self) -> None:
        """1. 通常サイズで収まる名称はそのまま base フォントで描く。"""
        c, fs = self._fit("ABCDE")  # 5*10*0.6=30 <= 50
        self.assertAlmostEqual(fs, self.BASE, places=4)
        self.assertEqual(c.drawn[-1][1], "ABCDE")

    def test_long_name_font_shrinks(self) -> None:
        """3. 品名列幅を超える名称はフォントが自動縮小される。"""
        c, fs = self._fit("A" * 12)  # 12*10*0.6=72 > 50
        self.assertLess(fs, self.BASE)
        # 縮小後の幅は max_width 以内に収まる。
        self.assertLessEqual(len("A" * 12) * fs * c.k, self.MAX_W + 1e-6)

    def test_very_long_name_drops_below_min_to_fit(self) -> None:
        """min フォントでも収まらない長い名称は下限を割ってでも全文字表示する。"""
        c, fs = self._fit("A" * 40)  # min(5)でも 40*5*0.6=120 > 50
        self.assertLess(fs, self.MIN)
        self.assertLessEqual(len("A" * 40) * fs * c.k, self.MAX_W + 1e-6)

    def test_full_text_never_truncated(self) -> None:
        """1. 長い名称でも文字列が切り捨てられない（全文字描画）。"""
        text = "非常に長い商品名称ABCDEFGHIJKLMNOP" * 2
        c, _ = self._fit(text)
        self.assertEqual(c.drawn[-1][1], text)

    def test_no_ellipsis_in_output(self) -> None:
        """2. 省略記号「…」を使わない。"""
        text = "長い商品名称" * 5
        c, _ = self._fit(text)
        self.assertNotIn("…", c.drawn[-1][1])
        self.assertNotIn("...", c.drawn[-1][1])

    def test_leading_and_consecutive_spaces_preserved(self) -> None:
        """4. 先頭スペース・全角/連続スペースが維持される。"""
        text = "　 先頭  全角　 連続スペース"  # 全角＋半角混在
        c, _ = self._fit(text)
        self.assertEqual(c.drawn[-1][1], text)

    def test_str_name_does_not_clip(self) -> None:
        """_str_name は _clip による途中切り捨てをせずフォント縮小で全文字描く。"""
        from app import voucher_service as vs
        c = _WidthCanvas(k=0.6)
        text = "A" * 40
        vs._str_name(c, text, 0.0, 0.0, self.BASE, max_w=self.MAX_W, min_fs=self.MIN)
        self.assertEqual(c.drawn[-1][1], text)
        self.assertLess(c.drawn[-1][0], self.BASE)

    def test_star_row_name_unchanged(self) -> None:
        """5. 商品名称が「*」の行は従来どおり（"*" を等倍で描画）。"""
        from app import voucher_service as vs
        c = _WidthCanvas(k=0.6)
        vs._str_name(c, "*", 0.0, 0.0, self.BASE, max_w=self.MAX_W, min_fs=self.MIN)
        self.assertEqual(c.drawn[-1][1], "*")
        self.assertAlmostEqual(c.drawn[-1][0], self.BASE, places=4)

    def test_empty_name_draws_nothing(self) -> None:
        """空の品名は何も描かない（"*"空欄処理後の空文字含む）。"""
        from app import voucher_service as vs
        c = _WidthCanvas(k=0.6)
        vs._str_name(c, "", 0.0, 0.0, self.BASE, max_w=self.MAX_W)
        self.assertEqual(c.drawn, [])

    def test_min_font_constant_defined(self) -> None:
        from app import voucher_service as vs
        self.assertTrue(hasattr(vs, "DETAIL_NAME_MIN_FONT_SIZE"))
        self.assertLess(vs.DETAIL_NAME_MIN_FONT_SIZE, vs.DETAIL_NAME_FONT_SIZE)

    def test_all_vouchers_build_with_long_name(self) -> None:
        """6. 長い商品名称を含むデータで 01〜08 すべて PDF 生成できる。"""
        from app.voucher_service import build_vouchers_pdf_bytes
        from app.voucher_templates import VOUCHER_IDS
        import io
        import pypdf
        from pathlib import Path

        long_name = "  超長尺ガラス特注研磨スーパーミラーガード全周面取りエッジング仕上"
        page = {
            "code_no": "001",
            "customer_name": "テスト得意先",
            "order_no": "5218869",
            "details": [
                {
                    "name": long_name,
                    "dims": "（1303 * 1061 ミリ）",
                    "qty_spec": "510中",
                    "qty": "2枚",
                    "unit_price": "800",
                    "amount": "3,846",
                    "note_lines": ["1,580 加"],
                },
            ],
        }
        result = build_vouchers_pdf_bytes(
            list(VOUCHER_IDS), data=page,
            base_dir=Path(__file__).resolve().parents[1])
        self.assertTrue(result.startswith(b"%PDF"))
        reader = pypdf.PdfReader(io.BytesIO(result))
        self.assertEqual(len(reader.pages), len(VOUCHER_IDS))


if __name__ == "__main__":
    unittest.main()
