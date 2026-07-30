"""伝票作成・印刷機能のテスト。"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestVoucherTemplates(unittest.TestCase):
    """voucher_templates モジュールのテスト。"""

    def test_voucher_types_has_8_entries(self) -> None:
        from app.voucher_templates import VOUCHER_TYPES
        self.assertEqual(len(VOUCHER_TYPES), 8)

    def test_first_voucher_is_uriagedenpyo(self) -> None:
        from app.voucher_templates import VOUCHER_TYPES
        self.assertEqual(VOUCHER_TYPES[0], ("01", "売上伝票"))

    def test_template_path_returns_correct_path(self) -> None:
        from app.voucher_templates import template_path
        p = template_path("01", PROJECT_ROOT)
        self.assertTrue(str(p).endswith("sample_denpyou_01.pdf"))

    def test_all_template_files_exist(self) -> None:
        from app.voucher_templates import VOUCHER_IDS, template_path
        for vid in VOUCHER_IDS:
            p = template_path(vid, PROJECT_ROOT)
            self.assertTrue(p.exists(), f"テンプレートが見つかりません: {p}")

    def test_dummy_data_has_required_keys(self) -> None:
        from app.voucher_templates import DUMMY_DATA
        for key in ("code_no", "customer_name", "order_no", "voucher_no"):
            self.assertIn(key, DUMMY_DATA)
            self.assertTrue(DUMMY_DATA[key])


class TestVoucherService(unittest.TestCase):
    """voucher_service モジュールのテスト。"""

    def test_create_vouchers_raises_on_empty_ids(self) -> None:
        from app.voucher_service import create_vouchers_pdf
        with self.assertRaises(ValueError) as ctx:
            create_vouchers_pdf([])
        self.assertIn("選択", str(ctx.exception))

    def test_create_vouchers_raises_on_missing_template(self) -> None:
        # vid "01"〜"08" はアプリ描画方式のためテンプレート不要。未知IDで確認する。
        from app.voucher_service import create_vouchers_pdf
        with self.assertRaises(FileNotFoundError):
            create_vouchers_pdf(["99"], base_dir=Path("/nonexistent_dir"))

    def test_create_voucher_01_with_dummy_data(self) -> None:
        """売上伝票(01)をダミーデータで生成できること。"""
        import tempfile
        from app.voucher_service import create_vouchers_pdf

        with tempfile.TemporaryDirectory() as tmpdir:
            out = create_vouchers_pdf(["01"], output_dir=Path(tmpdir), base_dir=PROJECT_ROOT)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)

    def test_create_multiple_vouchers_into_single_pdf(self) -> None:
        """複数の伝票を選択すると1つのPDFに結合されること。"""
        import tempfile
        import pypdf
        from app.voucher_service import create_vouchers_pdf

        with tempfile.TemporaryDirectory() as tmpdir:
            out = create_vouchers_pdf(["01", "02", "03"], output_dir=Path(tmpdir), base_dir=PROJECT_ROOT)
            self.assertTrue(out.exists())
            reader = pypdf.PdfReader(str(out))
            self.assertEqual(len(reader.pages), 3)

    def test_output_goes_to_work_voucher_output(self) -> None:
        """output_dir 省略時に work/voucher_output/ に出力されること。"""
        from app.voucher_service import create_vouchers_pdf

        out = create_vouchers_pdf(["01"], base_dir=PROJECT_ROOT)
        try:
            self.assertIn("voucher_output", str(out))
            self.assertTrue(out.exists())
        finally:
            if out.exists():
                out.unlink()

    def test_create_vouchers_uses_specified_output_dir(self) -> None:
        """PDF作成処理が指定された出力先を使用すること。"""
        import tempfile
        from app.voucher_service import create_vouchers_pdf

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "custom_pdf"
            out = create_vouchers_pdf(["01"], output_dir=output_dir, base_dir=PROJECT_ROOT)
            self.assertEqual(out.parent, output_dir)
            self.assertTrue(out.exists())

    def test_pdf_filename_uses_timestamp_and_order_no(self) -> None:
        import re
        import tempfile
        from app.voucher_service import create_vouchers_pdf

        data = {
            "pages": [{
                "order_no": "5218869",
                "voucher_no": "Z740506",
                "customer_name": "得意先",
                "details": [{"name": "品名"}],
            }]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            out = create_vouchers_pdf(["01"], data=data, output_dir=Path(tmpdir), base_dir=PROJECT_ROOT)
            self.assertRegex(out.name, r"^\d{8}_\d{6}_5218869\.pdf$")

    def test_pdf_filename_falls_back_to_order_no(self) -> None:
        import tempfile
        from app.voucher_service import create_vouchers_pdf

        data = {
            "pages": [{
                "order_no": "5218869",
                "voucher_no": "",
                "delivery_no": "",
                "customer_name": "得意先",
                "details": [{"name": "品名"}],
            }]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            out = create_vouchers_pdf(["01"], data=data, output_dir=Path(tmpdir), base_dir=PROJECT_ROOT)
            self.assertRegex(out.name, r"^\d{8}_\d{6}_5218869\.pdf$")

    def test_selected_multi_pdf_filename_uses_multi(self) -> None:
        import tempfile
        from app.voucher_service import save_pdf_bytes

        with tempfile.TemporaryDirectory() as tmpdir:
            out = save_pdf_bytes(b"%PDF", output_dir=Path(tmpdir), filename_token="multi")
            self.assertRegex(out.name, r"^\d{8}_\d{6}_multi\.pdf$")

    def test_pdf_filename_collision_uses_number_from_two(self) -> None:
        import tempfile
        from unittest import mock

        from app import voucher_service
        from app.voucher_service import save_pdf_bytes

        with tempfile.TemporaryDirectory() as tmpdir, \
                mock.patch.object(voucher_service, "datetime") as dt:
            dt.now.return_value.strftime.return_value = "20260622_093015"
            first = save_pdf_bytes(b"%PDF", output_dir=Path(tmpdir), filename_token="1405113")
            second = save_pdf_bytes(b"%PDF", output_dir=Path(tmpdir), filename_token="1405113")
            self.assertEqual(first.name, "20260622_093015_1405113.pdf")
            self.assertEqual(second.name, "20260622_093015_1405113_2.pdf")

    def test_save_named_pdf_bytes_uses_order_no_filename(self) -> None:
        import tempfile
        from app.voucher_service import save_named_pdf_bytes

        with tempfile.TemporaryDirectory() as tmpdir:
            out = save_named_pdf_bytes(b"%PDF", output_dir=Path(tmpdir), filename_stem="1394161_伝票")
            self.assertEqual(out.name, "1394161_伝票.pdf")

    def test_save_named_pdf_bytes_collision_uses_sequence(self) -> None:
        import tempfile
        from app.voucher_service import save_named_pdf_bytes

        with tempfile.TemporaryDirectory() as tmpdir:
            first = save_named_pdf_bytes(b"%PDF", output_dir=Path(tmpdir), filename_stem="1394161_伝票")
            second = save_named_pdf_bytes(b"%PDF", output_dir=Path(tmpdir), filename_stem="1394161_伝票")
            third = save_named_pdf_bytes(b"%PDF", output_dir=Path(tmpdir), filename_stem="1394161_伝票")
            self.assertEqual(first.name, "1394161_伝票.pdf")
            self.assertEqual(second.name, "1394161_伝票_2.pdf")
            self.assertEqual(third.name, "1394161_伝票_3.pdf")

    def test_pdf_filename_sanitizes_invalid_characters(self) -> None:
        import tempfile
        from app.voucher_service import save_pdf_bytes

        with tempfile.TemporaryDirectory() as tmpdir:
            out = save_pdf_bytes(b"%PDF", output_dir=Path(tmpdir), filename_token="A/B:C")
            self.assertRegex(out.name, r"^\d{8}_\d{6}_A_B_C\.pdf$")

    def test_blank_voucher_data_fails_pre_pdf_validation(self) -> None:
        from app.voucher_window import _missing_required_voucher_fields

        missing = _missing_required_voucher_fields(
            [{"order_no": "", "customer_name": "", "delivery_no": "", "details": []}]
        )
        self.assertIn("page1.order_no", missing)
        self.assertIn("page1.customer_name", missing)
        self.assertIn("page1.delivery_no", missing)
        self.assertIn("page1.detail_rows", missing)

    def test_summary_line2_keeps_lower_position_when_line1_blank(self) -> None:
        from app import voucher_service

        with patch.object(voucher_service, "_str") as draw:
            voucher_service._draw_summary_lines(
                MagicMock(),
                {"summary_line1": "　", "summary_line2": "受注見出摘要"},
                7.0,
            )

        self.assertEqual(draw.call_count, 1)
        args, kwargs = draw.call_args
        self.assertEqual(args[1], "受注見出摘要")
        self.assertEqual(args[3], voucher_service._summary_line_y(1))
        self.assertNotEqual(args[3], voucher_service._summary_line_y(0))
        self.assertIn("max_w", kwargs)


class TestVoucherServiceLayout(unittest.TestCase):
    """売上伝票(01)のPDF内部構造テスト。"""

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile
        from app.voucher_service import create_vouchers_pdf
        cls._tmpdir = tempfile.mkdtemp()
        cls._pdf_path = create_vouchers_pdf(
            ["01"], output_dir=Path(cls._tmpdir), base_dir=PROJECT_ROOT
        )

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def test_voucher_01_pdf_exists(self) -> None:
        """売上伝票PDFが生成されること。"""
        self.assertTrue(self._pdf_path.exists())
        self.assertGreater(self._pdf_path.stat().st_size, 0)

    def test_voucher_01_has_one_page(self) -> None:
        import pypdf
        reader = pypdf.PdfReader(str(self._pdf_path))
        self.assertEqual(len(reader.pages), 1)

    def test_voucher_01_page_size(self) -> None:
        """ページサイズが横向きA4程度であること。"""
        import pypdf
        reader = pypdf.PdfReader(str(self._pdf_path))
        page = reader.pages[0]
        self.assertAlmostEqual(float(page.mediabox.width), 729.4, delta=1.0)
        self.assertAlmostEqual(float(page.mediabox.height), 515.5, delta=1.0)


class TestVoucherServiceSource(unittest.TestCase):
    """voucher_service のソースコード構造テスト。"""

    _SOURCE: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._SOURCE = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")

    def test_title_is_drawn_centred(self) -> None:
        """タイトルが中央揃えで描画されること。"""
        self.assertIn("drawCentredString", self._SOURCE)
        self.assertIn("売　上　伝　票", self._SOURCE)

    def test_detail_rows_count(self) -> None:
        """明細が7行分の定数を使うこと。"""
        self.assertIn("FORM_DETAIL_ROWS", self._SOURCE)

    def test_black_header_row_drawn(self) -> None:
        """テーブルヘッダー行が黒塗りで描画されること（合計行も）。"""
        self.assertIn("setFillColorRGB(0, 0, 0)", self._SOURCE)
        self.assertIn("fill=1", self._SOURCE)

    def test_total_row_exists(self) -> None:
        """合計行が描画されること。"""
        self.assertIn("合　計", self._SOURCE)
        self.assertIn("FORM_TOTAL_BOT", self._SOURCE)

    def test_sum_and_bkno_rows_exist(self) -> None:
        """摘要 / 物件No 行が描画されること。"""
        self.assertIn("摘　要", self._SOURCE)
        self.assertIn("物件No", self._SOURCE)

    def test_tax_notice_removed(self) -> None:
        """固定の消費税文言は全廃され、描画コードに残っていないこと。"""
        self.assertNotIn("TAX_NOTICE", self._SOURCE)
        self.assertNotIn("（本伝票には消費税は含まれておりません。）", self._SOURCE)

    def test_customer_order_no_drawn(self) -> None:
        """お客様注文Noの描画ヘルパが使われていること。"""
        self.assertIn("_draw_customer_order_no", self._SOURCE)
        self.assertIn("CUSTOMER_ORDER_NO_LABEL", self._SOURCE)

    def test_lower_vertical_list_exists(self) -> None:
        """下部チェック欄が縦リスト形式で描画されること。"""
        self.assertIn("PROC_LABELS", self._SOURCE)
        # 縦方向ループで各ラベルを描画している
        self.assertIn("for i, label in enumerate(PROC_LABELS)", self._SOURCE)

    def test_rounded_corners_used(self) -> None:
        """角丸矩形が使われていること。"""
        self.assertIn("roundRect", self._SOURCE)

    def test_single_cut_label(self) -> None:
        """切断仕上日が1つのラベルとして存在すること。"""
        self.assertIn("切断仕上日", self._SOURCE)

    def test_narrow_checkbox_column(self) -> None:
        """チェック列の右端定数 FORM_CHK_RIGHT が使われていること。"""
        self.assertIn("FORM_CHK_RIGHT", self._SOURCE)

    def test_sum_right_constant_used(self) -> None:
        """摘要欄の右端が FORM_SUM_RIGHT で制限されていること。"""
        self.assertIn("FORM_SUM_RIGHT", self._SOURCE)

    def test_build_vouchers_pdf_bytes_function_exists(self) -> None:
        """build_vouchers_pdf_bytes 関数が定義されていること。"""
        self.assertIn("def build_vouchers_pdf_bytes", self._SOURCE)

    def test_build_vouchers_pdf_bytes_does_not_write_file(self) -> None:
        """build_vouchers_pdf_bytes 内で open(..., 'wb') が呼ばれないこと。"""
        import ast
        tree = ast.parse(self._SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "build_vouchers_pdf_bytes":
                func_src = ast.get_source_segment(self._SOURCE, node) or ""
                self.assertNotIn("open(", func_src)
                return
        self.fail("build_vouchers_pdf_bytes が見つかりません")


class TestVoucher01LayoutConstants(unittest.TestCase):
    """売上伝票(01)の紙伝票寄せレイアウト定数テスト。"""

    def test_header_left_aligns_with_detail_name_column(self) -> None:
        from app.voucher_templates import FORM_HDR_LEFT, TBL_COLS
        self.assertEqual(FORM_HDR_LEFT, TBL_COLS[1])

    def test_total_label_cell_is_in_unit_price_column(self) -> None:
        from app.voucher_templates import FORM_TOTAL_CELL_LEFT, FORM_TOTAL_CELL_RIGHT, TBL_COLS
        self.assertEqual(FORM_TOTAL_CELL_LEFT, TBL_COLS[3])
        self.assertEqual(FORM_TOTAL_CELL_RIGHT, TBL_COLS[4])

    def test_data_offset_moves_text_inward(self) -> None:
        from app.voucher_templates import DATA_X_PAD, DET_UPPER_OFFSET, HDR_DATA_Y_INNER
        self.assertGreaterEqual(DATA_X_PAD, 5.0)
        self.assertGreaterEqual(DET_UPPER_OFFSET, 11.0)
        self.assertLessEqual(HDR_DATA_Y_INNER, 4.0)

    def test_shiage_date_is_handwritten_field_label(self) -> None:
        src = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")
        self.assertNotIn("仕上日(月/日)", src)
        self.assertIn('"仕上日"', src)
        self.assertIn('"月"', src)
        self.assertIn('"日"', src)

    def test_company_name_has_logo_without_right_circle(self) -> None:
        src = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")
        self.assertIn("assets/manekiya_logo.png", src)
        self.assertIn("drawImage", src)
        self.assertIn("COMPANY_NAME_Y", src)
        self.assertIn("logo_y = COMPANY_NAME_Y", src)
        self.assertIn("COMPANY_LOGO_H", src)
        self.assertNotIn("c.circle(logo_x, logo_y", src)
        self.assertIn('"まねきや硝子株式会社"', src)
        self.assertNotIn("まねきや硝子株式会社　○", src)

    def test_logo_x_is_right_of_header_box(self) -> None:
        from app.voucher_templates import COMPANY_LOGO_X, FORM_HDR_RIGHT
        # ロゴはヘッダー枠右端より右に配置されること
        self.assertGreater(COMPANY_LOGO_X, FORM_HDR_RIGHT)

    def test_top_table_horizontal_margins_are_balanced(self) -> None:
        from app.voucher_templates import PAGE_W, TBL_COLS
        left_margin = TBL_COLS[0]
        right_margin = PAGE_W - TBL_COLS[-1]
        self.assertLessEqual(abs(left_margin - right_margin), 5.0)

    def test_lower_frame_is_inside_top_table(self) -> None:
        from app.voucher_templates import FORM_LWR_LEFT, FORM_LWR_RIGHT, TBL_COLS
        self.assertGreater(FORM_LWR_LEFT, TBL_COLS[0])
        self.assertLess(FORM_LWR_RIGHT, TBL_COLS[-1])

    def test_process_check_column_is_narrower(self) -> None:
        from app.voucher_templates import FORM_CHK_RIGHT, FORM_LWR_LEFT
        self.assertLessEqual(FORM_CHK_RIGHT - FORM_LWR_LEFT, 67.0)

    def test_detail_row_height_is_uniform_and_extended(self) -> None:
        from app.voucher_templates import FORM_DETAIL_BOT, FORM_DETAIL_ROW_H, FORM_DETAIL_ROWS, FORM_TBL_HDR_BOT
        self.assertEqual(FORM_DETAIL_BOT, FORM_TBL_HDR_BOT - FORM_DETAIL_ROWS * FORM_DETAIL_ROW_H)
        self.assertGreaterEqual(FORM_DETAIL_ROW_H, 26.0)

    def test_sum_lines_end_at_name_column_right_border(self) -> None:
        from app.voucher_templates import FORM_SUM_RIGHT, TBL_COLS
        self.assertEqual(FORM_SUM_RIGHT, TBL_COLS[2])

    def test_shiage_label_is_above_month_day_labels(self) -> None:
        from app.voucher_templates import HDR_SHIAGE_LABEL_Y, HDR_SHIAGE_MONTH_DAY_Y
        self.assertGreater(HDR_SHIAGE_LABEL_Y, HDR_SHIAGE_MONTH_DAY_Y)


class TestBuildVouchersPdfBytes(unittest.TestCase):
    """build_vouchers_pdf_bytes の動作テスト。"""

    def test_returns_bytes(self) -> None:
        """バイト列を返すこと。"""
        from app.voucher_service import build_vouchers_pdf_bytes
        result = build_vouchers_pdf_bytes(["01"], base_dir=PROJECT_ROOT)
        self.assertIsInstance(result, bytes)
        self.assertGreater(len(result), 0)

    def test_starts_with_pdf_magic(self) -> None:
        """返り値が有効なPDFバイト列であること。"""
        from app.voucher_service import build_vouchers_pdf_bytes
        result = build_vouchers_pdf_bytes(["01"], base_dir=PROJECT_ROOT)
        self.assertTrue(result.startswith(b"%PDF"))

    def test_raises_on_empty_ids(self) -> None:
        """伝票IDが空のとき ValueError を送出すること。"""
        from app.voucher_service import build_vouchers_pdf_bytes
        with self.assertRaises(ValueError):
            build_vouchers_pdf_bytes([])

    def test_multiple_pages(self) -> None:
        """複数伝票を指定するとページ数に反映されること。"""
        import io
        import pypdf
        from app.voucher_service import build_vouchers_pdf_bytes
        result = build_vouchers_pdf_bytes(["01", "02", "03"], base_dir=PROJECT_ROOT)
        reader = pypdf.PdfReader(io.BytesIO(result))
        self.assertEqual(len(reader.pages), 3)


class TestNameQtyColumnWidthRestored(unittest.TestCase):
    """品名/数量 列幅変更を元に戻したことのテスト（長名称はフォント縮小で対応）。"""

    # 元の（変更前＝復帰後）の品名/数量境界・数量/単価境界。
    NAME_QTY_BORDER = 336.0
    QTY_RIGHT_BORDER = 434.0

    def test_shift_constant_removed(self) -> None:
        """列幅シフト定数 NAME_QTY_BORDER_SHIFT は廃止されている。"""
        import app.voucher_templates as vt
        self.assertFalse(hasattr(vt, "NAME_QTY_BORDER_SHIFT"))

    def test_name_qty_border_restored(self) -> None:
        """1. 品名／数量境界が元の位置(336.0)に戻っている。"""
        from app.voucher_templates import TBL_COLS, SHIZU_TBL_COLS
        self.assertAlmostEqual(TBL_COLS[2], self.NAME_QTY_BORDER, places=2)
        self.assertAlmostEqual(SHIZU_TBL_COLS[2], self.NAME_QTY_BORDER, places=2)

    def test_qty_column_width_restored(self) -> None:
        """数量列幅が元の幅(98.0)に戻っている。"""
        from app.voucher_templates import TBL_COLS, SHIZU_TBL_COLS
        self.assertAlmostEqual(TBL_COLS[3] - TBL_COLS[2], 98.0, places=2)
        self.assertAlmostEqual(SHIZU_TBL_COLS[3] - SHIZU_TBL_COLS[2], 98.0, places=2)

    def test_qty_right_border_unchanged(self) -> None:
        """数量列の右端位置は従来どおり(434.0)。"""
        from app.voucher_templates import TBL_COLS, SHIZU_TBL_COLS
        self.assertAlmostEqual(TBL_COLS[3], self.QTY_RIGHT_BORDER, places=2)
        self.assertAlmostEqual(SHIZU_TBL_COLS[3], self.QTY_RIGHT_BORDER, places=2)

    def test_right_side_columns_unchanged(self) -> None:
        """7. 単価・金額・摘要・受入日など右側列・表外枠の位置が崩れない。"""
        from app.voucher_templates import TBL_COLS, SHIZU_TBL_COLS
        self.assertEqual(TBL_COLS[3:], [434.0, 502.0, 568.0, 695.0])
        self.assertEqual(SHIZU_TBL_COLS[3:], [434.0, 631.5, 695.0])
        # 左端・品名左端も不変。
        self.assertEqual(TBL_COLS[0], 34.0)
        self.assertEqual(TBL_COLS[1], 45.0)

    def test_name_draw_width_restored(self) -> None:
        """2. 明細行の商品名称の描画幅(TBL_MAX_NAME)が元の値(267.0)に戻っている。"""
        from app.voucher_templates import TBL_MAX_NAME
        self.assertAlmostEqual(TBL_MAX_NAME, 267.0, places=2)

    def test_qty_draw_width_restored(self) -> None:
        """3. 数量描画幅(TBL_MAX_QTY)が元の値(88.0)に戻っている。"""
        from app.voucher_templates import TBL_MAX_QTY
        self.assertAlmostEqual(TBL_MAX_QTY, 88.0, places=2)

    def test_alignment_x_restored(self) -> None:
        """品名右寄せ基準(DET_NAME_RX)・数量右寄せ基準(DET_QTY_RX)が元の位置に戻る。"""
        from app.voucher_templates import DET_NAME_RX, DET_QTY_RX, TBL_COLS, DATA_X_PAD
        self.assertAlmostEqual(DET_NAME_RX, TBL_COLS[2] - DATA_X_PAD, places=2)
        self.assertAlmostEqual(DET_NAME_RX, self.NAME_QTY_BORDER - DATA_X_PAD, places=2)
        self.assertAlmostEqual(DET_QTY_RX, self.QTY_RIGHT_BORDER - DATA_X_PAD, places=2)

    def test_name_left_draw_x_unchanged(self) -> None:
        """品名1段目の左寄せ描画X(TBL_X_NAME)は変わらない（先頭スペース保持に影響しない）。"""
        from app.voucher_templates import TBL_X_NAME, TBL_COLS, DATA_X_PAD
        self.assertAlmostEqual(TBL_X_NAME, TBL_COLS[1] + DATA_X_PAD, places=2)
        self.assertAlmostEqual(TBL_X_NAME, 50.0, places=2)

    def test_fit_width_helper_retained(self) -> None:
        """4/5. 列幅は戻すが draw_text_fit_width / 自動縮小処理は維持される。"""
        from app import voucher_service as vs
        self.assertTrue(callable(vs.draw_text_fit_width))
        self.assertTrue(hasattr(vs, "DETAIL_NAME_MIN_FONT_SIZE"))

    def test_all_vouchers_generate_pdf(self) -> None:
        """6. 01〜08すべての伝票でPDF生成できる。"""
        import io
        import pypdf
        from app.voucher_service import build_vouchers_pdf_bytes
        ids = ["01", "02", "03", "04", "05", "06", "07", "08"]
        result = build_vouchers_pdf_bytes(ids, base_dir=PROJECT_ROOT)
        self.assertTrue(result.startswith(b"%PDF"))
        reader = pypdf.PdfReader(io.BytesIO(result))
        self.assertEqual(len(reader.pages), len(ids))


class TestLauncherWindowStatic(unittest.TestCase):
    """LauncherWindow のソースコード静的テスト。"""

    _SOURCE: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._SOURCE = (PROJECT_ROOT / "app" / "launcher_window.py").read_text(encoding="utf-8")

    def test_launcher_file_exists(self) -> None:
        self.assertTrue((PROJECT_ROOT / "app" / "launcher_window.py").exists())

    def test_voucher_btn_defined_before_kintone_btn(self) -> None:
        """伝票作成・印刷ボタンがkintone登録処理ボタンより先に定義されていること。"""
        src = self._SOURCE
        pos_voucher = src.find("伝票作成・印刷")
        pos_kintone = src.find("kintone登録処理")
        self.assertGreater(pos_voucher, 0)
        self.assertGreater(pos_kintone, 0)
        self.assertLess(pos_voucher, pos_kintone)

    def test_olap_fields_used_for_voucher_btn_enable(self) -> None:
        """OLAPフィールドの変更がボタン有効化に影響すること。"""
        self.assertIn("_olap_id", self._SOURCE)
        self.assertIn("_olap_password", self._SOURCE)
        self.assertIn("_update_buttons", self._SOURCE)

    def test_all_four_credential_fields_present(self) -> None:
        for field in ("_olap_id", "_olap_password", "_kintone_id", "_kintone_password"):
            self.assertIn(field, self._SOURCE)

    def test_lock_ui_method_exists(self) -> None:
        """子画面は画面単位で二重起動を抑止すること。"""
        self.assertIn("self._voucher_window is None", self._SOURCE)
        self.assertIn("self._main_window is None", self._SOURCE)

    def test_unlock_ui_method_exists(self) -> None:
        """子画面終了後にボタン状態を再判定するメソッドがあること。"""
        self.assertIn("_bring_launcher_front", self._SOURCE)

    def test_launcher_raises_on_child_close(self) -> None:
        """子画面終了時に機能選択画面を前面表示する処理があること。"""
        self.assertIn("self.show()", self._SOURCE)
        self.assertIn("self.raise_()", self._SOURCE)

    def test_child_closed_handler_exists(self) -> None:
        """子画面終了の共通ハンドラーがあること。"""
        self.assertIn("_on_voucher_closed", self._SOURCE)
        self.assertIn("_on_kintone_closed", self._SOURCE)

    def test_lock_called_on_open_voucher(self) -> None:
        """伝票画面を開くときにボタン状態を更新すること。"""
        src = self._SOURCE
        open_voucher_pos = src.find("def _open_voucher")
        update_pos = src.find("_update_buttons()", open_voucher_pos)
        self.assertGreater(update_pos, open_voucher_pos)

    def test_lock_called_on_open_kintone(self) -> None:
        """Kintone画面を開くときにボタン状態を更新すること。"""
        src = self._SOURCE
        open_kintone_pos = src.find("def _open_kintone")
        update_pos = src.find("_update_buttons()", open_kintone_pos)
        self.assertGreater(update_pos, open_kintone_pos)

    def test_kintone_credentials_are_saved_and_loaded(self) -> None:
        """Kintoneログイン情報を保存・自動入力する処理があること。"""
        self.assertIn("_save_kintone_credentials", self._SOURCE)
        self.assertIn("KINTONE_LOGIN_ID_ENV_KEY", self._SOURCE)
        self.assertIn("KINTONE_PASSWORD_ENV_KEY", self._SOURCE)

    def test_launcher_close_quits_child_windows(self) -> None:
        """機能選択画面を閉じると子画面も閉じてアプリ終了すること。"""
        self.assertIn("def closeEvent", self._SOURCE)
        self.assertIn("QApplication.quit()", self._SOURCE)

    def test_kintone_screen_locks_credential_fields(self) -> None:
        """Kintone登録処理画面の起動中は認証入力欄をロックすること。"""
        self.assertIn("_update_credential_locks", self._SOURCE)
        self.assertIn("kintone_enabled = self._main_window is None", self._SOURCE)

    def test_voucher_screen_locks_olap_fields(self) -> None:
        """伝票作成・印刷画面の起動中はOLAP入力欄をロックすること。"""
        self.assertIn("olap_enabled = self._voucher_window is None and self._main_window is None", self._SOURCE)
        self.assertIn("self._olap_id.setEnabled(olap_enabled)", self._SOURCE)
        self.assertIn("self._olap_password.setEnabled(olap_enabled)", self._SOURCE)


class TestVoucherWindowStatic(unittest.TestCase):
    """VoucherWindow のソースコード静的テスト。"""

    _SOURCE: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._SOURCE = (PROJECT_ROOT / "app" / "voucher_window.py").read_text(encoding="utf-8")

    def test_voucher_window_file_exists(self) -> None:
        self.assertTrue((PROJECT_ROOT / "app" / "voucher_window.py").exists())

    def test_eight_voucher_types_from_templates(self) -> None:
        """VoucherWindow が VOUCHER_TYPES を使っていること。"""
        self.assertIn("VOUCHER_TYPES", self._SOURCE)

    def test_rows_use_voucher_order_row_dataclass(self) -> None:
        """行データを VoucherOrderRow データクラスで扱うこと。"""
        self.assertIn("class VoucherOrderRow", self._SOURCE)
        self.assertIn("process_checks", self._SOURCE)
        self.assertIn("voucher_checks", self._SOURCE)

    def test_pdf_and_print_buttons_present_without_back(self) -> None:
        for label in ("PDF作成", "印刷"):
            self.assertIn(label, self._SOURCE)
        self.assertNotIn('QPushButton("戻る")', self._SOURCE)

    def test_no_selection_error_message(self) -> None:
        self.assertIn("印刷する伝票を1つ以上選択してください", self._SOURCE)

    def test_order_no_required_error_message(self) -> None:
        self.assertIn("受注Noを入力してください", self._SOURCE)

    def test_row_action_buttons_present_with_delete(self) -> None:
        for label in ("行追加", "選択PDF作成", "選択印刷", "選択削除"):
            self.assertIn(label, self._SOURCE)
        self.assertIn('QPushButton("選択削除")', self._SOURCE)

    def test_process_and_finish_date_widgets_present(self) -> None:
        # AM/PM はコンボボックスからラジオボタンへ変更（要件3）。
        for token in ("QDateEdit", "QRadioButton", "PROCESS_NAMES", "指図書編集"):
            self.assertIn(token, self._SOURCE)

    def test_close_event_emits_back_requested(self) -> None:
        """closeEvent が back_requested を emit すること（ソース確認）。"""
        self.assertIn("closeEvent", self._SOURCE)
        self.assertIn("back_requested.emit()", self._SOURCE)

    def test_print_uses_direct_print(self) -> None:
        """印刷ボタンが保存済み設定で即時印刷すること。"""
        self.assertIn("print_pdf_direct", self._SOURCE)


class TestVoucherPrintServiceStatic(unittest.TestCase):
    """voucher_print_service のソースコード静的テスト。"""

    _SOURCE: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._SOURCE = (PROJECT_ROOT / "app" / "voucher_print_service.py").read_text(encoding="utf-8")

    def test_does_not_use_qprint_dialog(self) -> None:
        """伝票印刷では QPrintDialog を表示しないこと。"""
        self.assertNotIn("QPrintDialog(", self._SOURCE)

    def test_does_not_use_os_startfile_print(self) -> None:
        """os.startfile(path, \"print\") をコードとして呼ばないこと。"""
        self.assertNotIn('os.startfile(', self._SOURCE)

    def test_print_pdf_direct_exists(self) -> None:
        """print_pdf_direct 関数が存在すること。"""
        self.assertIn("def print_pdf_direct", self._SOURCE)

    def test_missing_printer_setting_is_guarded(self) -> None:
        """プリンター未設定時は即時印刷せずエラーにすること。"""
        self.assertIn("印刷設定が未設定です", self._SOURCE)

    def test_accepts_bytes_not_path(self) -> None:
        """即時印刷処理が bytes を受け取る構成であること。"""
        self.assertIn("pdf_bytes", self._SOURCE)

    def test_uses_temp_file_internally(self) -> None:
        """内部でテンポラリファイルを使うこと（ファイル保存しない）。"""
        self.assertIn("tempfile", self._SOURCE)

    def test_temp_pdf_is_closed_and_validated_before_load(self) -> None:
        """一時PDFはclose後に存在・サイズ確認してから読み込むこと。"""
        self.assertIn("tempfile.mkstemp", self._SOURCE)
        self.assertIn("os.fdopen", self._SOURCE)
        self.assertIn("fp.flush()", self._SOURCE)
        self.assertIn("os.fsync", self._SOURCE)
        self.assertIn("path.stat().st_size", self._SOURCE)
        self.assertIn("_try_load_pdf_document(tmp_path)", self._SOURCE)

    def test_acrobat_print_jobs_are_kept_and_cleaned_later(self) -> None:
        """Acrobat経由印刷のPDFは即削除せず、print_jobsで一定期間後に削除すること。"""
        self.assertIn("work\" / \"print_jobs", self._SOURCE)
        self.assertIn("PRINT_JOB_RETENTION_DAYS = 7", self._SOURCE)
        self.assertIn("def cleanup_old_print_jobs", self._SOURCE)
        self.assertIn("voucher_print_*.pdf", self._SOURCE)

    def test_qpdf_failure_falls_back_to_pymupdf(self) -> None:
        """QPdfDocumentが使えない場合にPyMuPDFへフォールバックすること。"""
        self.assertIn("_print_with_pymupdf", self._SOURCE)
        self.assertIn("fitz.open", self._SOURCE)
        self.assertIn("フォールバック", self._SOURCE)

    def test_qpdf_status_alone_does_not_fail_print(self) -> None:
        """QPdfDocumentの状態だけで即RuntimeErrorにしないこと。"""
        self.assertIn("load_result == QPdfDocument.Error.None_", self._SOURCE)
        self.assertIn("return None", self._SOURCE)

    def test_print_diagnostics_are_logged(self) -> None:
        """印刷用PDF読み込み診断をログ出力すること。"""
        for text in ("path=%s", "size=%s", "load_result=%s", "status=%s", "error=%s", "pageCount=%s", "PySide6=%s"):
            self.assertIn(text, self._SOURCE)

    def test_cut_date_box_uses_custom_path(self) -> None:
        """切断仕上日枠はroundRectではなく個別角のパスで描画すること。"""
        service_src = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")
        self.assertIn("_cut_date_box_path", service_src)
        self.assertIn("左上・右下は直角", service_src)
        self.assertNotIn("c.roundRect(CL, CB_BOT", service_src)


class TestVoucherWindowPrintStatic(unittest.TestCase):
    """VoucherWindow の印刷ボタン実装の静的テスト。"""

    _SOURCE: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._SOURCE = (PROJECT_ROOT / "app" / "voucher_window.py").read_text(encoding="utf-8")

    def test_print_uses_build_vouchers_pdf_bytes(self) -> None:
        """印刷ボタンが build_vouchers_pdf_bytes を使うこと。"""
        self.assertIn("build_vouchers_pdf_bytes", self._SOURCE)

    def test_print_does_not_save_file(self) -> None:
        """_on_print が create_vouchers_pdf を呼ばないこと（ファイル保存しない）。"""
        import ast
        tree = ast.parse(self._SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_on_print":
                func_src = ast.get_source_segment(self._SOURCE, node) or ""
                self.assertNotIn("create_vouchers_pdf", func_src)
                return
        self.fail("_on_print が見つかりません")


class TestRunGuiUsesLauncher(unittest.TestCase):
    """run_gui が LauncherWindow を起動すること。"""

    def test_run_gui_references_launcher(self) -> None:
        src = (PROJECT_ROOT / "app" / "gui.py").read_text(encoding="utf-8")
        self.assertIn("LauncherWindow", src)
        self.assertIn("launcher_window", src)

    def test_main_window_accepts_initial_credentials(self) -> None:
        src = (PROJECT_ROOT / "app" / "gui.py").read_text(encoding="utf-8")
        self.assertIn("initial_olap_id", src)
        self.assertIn("initial_olap_password", src)

    def test_run_gui_uses_single_instance_guard(self) -> None:
        src = (PROJECT_ROOT / "app" / "gui.py").read_text(encoding="utf-8")
        self.assertIn("QLocalServer", src)
        self.assertIn("QLocalSocket", src)
        self.assertIn("connectToServer", src)


class TestVoucher01Regression(unittest.TestCase):
    """売上伝票(01)レイアウト変更後の回帰テスト。"""

    def test_company_info_y_is_above_table_header(self) -> None:
        """会社情報Y座標が表の黒ヘッダー行(FORM_TBL_HDR_BOT)より上にあること。"""
        from app.voucher_templates import COMPANY_NAME_Y, FORM_TBL_HDR_BOT, FORM_HDR_BOT
        self.assertGreater(COMPANY_NAME_Y, FORM_TBL_HDR_BOT)
        # TEL/FAX（company name - 21）も黒バーより上にあること
        tel_fax_y = COMPANY_NAME_Y - 21.0
        self.assertGreaterEqual(tel_fax_y, FORM_HDR_BOT - 1.0)

    def test_company_info_x_is_right_of_header_box(self) -> None:
        """会社情報X座標がヘッダー枠(FORM_HDR_RIGHT)より右にあること。"""
        from app.voucher_templates import COMPANY_INFO_X, FORM_HDR_RIGHT
        self.assertGreater(COMPANY_INFO_X, FORM_HDR_RIGHT)

    def test_bottom_safety_margin_is_sufficient(self) -> None:
        """下端マージンが9pt(約3mm)以上あること。"""
        from app.voucher_templates import FORM_MB, PRINT_SAFE_BOTTOM_MARGIN_PT
        self.assertGreaterEqual(PRINT_SAFE_BOTTOM_MARGIN_PT, 24.0)
        self.assertEqual(FORM_MB, PRINT_SAFE_BOTTOM_MARGIN_PT)

    def test_total_row_height_equals_detail_row_height(self) -> None:
        """合計行高さが通常明細行と同じであること。"""
        from app.voucher_templates import FORM_TOTAL_ROW_H, FORM_DETAIL_ROW_H
        self.assertEqual(FORM_TOTAL_ROW_H, FORM_DETAIL_ROW_H)

    def test_total_row_bottom_is_below_detail_bot(self) -> None:
        """合計行がFORM_DETAIL_BOTより下にあること（7行目と別行）。"""
        from app.voucher_templates import FORM_TOTAL_BOT, FORM_DETAIL_BOT
        self.assertLess(FORM_TOTAL_BOT, FORM_DETAIL_BOT)

    def test_detail_rows_and_total_row_are_separate(self) -> None:
        """明細7行 + 合計1行が別行として構成されること。"""
        from app.voucher_templates import (
            FORM_TOTAL_BOT, FORM_TOTAL_ROW_H,
            FORM_DETAIL_BOT, FORM_DETAIL_ROWS, FORM_DETAIL_ROW_H,
        )
        # 合計行 top = FORM_DETAIL_BOT, bottom = FORM_TOTAL_BOT
        self.assertEqual(FORM_DETAIL_BOT - FORM_TOTAL_BOT, FORM_TOTAL_ROW_H)
        # 7行目 top = FORM_TBL_HDR_BOT - 6*ROW_H, bottom = FORM_DETAIL_BOT
        # 合計行は7行目の直下
        self.assertEqual(FORM_TOTAL_ROW_H, FORM_DETAIL_ROW_H)

    def test_total_row_amount_column_blank_in_source(self) -> None:
        """合計行の金額列には値を描画しないこと（TBL_COLS[4]への描画がないこと）。"""
        src = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")
        # total_note_upper を TBL_COLS[4] (金額列) に描画するコードがないこと
        self.assertNotIn("total_note_upper", src)

    def test_total_row_note_column_draws_right_aligned(self) -> None:
        """合計行摘要列が右揃えで描画されること（_rstr使用）。"""
        src = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")
        self.assertIn("_rstr", src)
        self.assertIn("note_rx", src)
        self.assertIn("total_upper_y", src)
        self.assertIn("total_lower_y", src)

    def test_extract_note_number(self) -> None:
        """摘要数値抽出関数の動作確認。"""
        from app.voucher_service import _extract_note_number
        self.assertEqual(_extract_note_number("1,580 加"), 1580.0)
        self.assertEqual(_extract_note_number("7,594 倉庫ま"), 7594.0)
        self.assertEqual(_extract_note_number("0 / 378 東大阪"), 0.0)
        self.assertEqual(_extract_note_number(""), 0.0)

    def test_split_note_rows_expands_slash_note(self) -> None:
        """スラッシュ区切りの摘要を上段・下段に分割すること。"""
        from app.voucher_service import _split_note_rows
        self.assertEqual(_split_note_rows("0 / 378 東大阪"), [("0", ""), ("378", "東大阪")])
        self.assertEqual(_split_note_rows("1,580 加"), [("1,580", "加")])

    def test_unit_price_totals_computed_from_details(self) -> None:
        """合計欄が売上単価/仕入単価×受注数量から正しく計算されること。"""
        from app.voucher_service import calculate_unit_price_totals
        details = [
            {"name": "A", "sales_unit_price": "430", "purchase_unit_price": "316", "ordered_quantity": "1"},
            {"name": "B", "sales_unit_price": "250", "purchase_unit_price": "224", "ordered_quantity": "1"},
            {"name": "C", "sales_unit_price": "40", "purchase_unit_price": "34", "ordered_quantity": "1"},
        ]
        sales_total, purchase_total = calculate_unit_price_totals(details)
        self.assertAlmostEqual(sales_total, 720.0)
        self.assertAlmostEqual(purchase_total, 574.0)

    def test_row7_separator_line_in_source(self) -> None:
        """No列7行目と合計行の区切り線が右側列（金額・摘要）にも描画されること。"""
        src = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")
        self.assertIn("FORM_TOTAL_CELL_RIGHT, FORM_DETAIL_BOT, table_right", src)

    def test_title_x_shifted_left_10mm(self) -> None:
        """全伝票タイトル基準Xが元位置から1.0cm左に移動していること。"""
        from app.voucher_templates import FORM_TITLE_SHIFT_LEFT, FORM_TITLE_X
        self.assertAlmostEqual(FORM_TITLE_SHIFT_LEFT, 28.35, places=2)
        self.assertAlmostEqual(FORM_TITLE_X, 205.0 - 28.35, places=2)


class TestVoucher01Regression3(unittest.TestCase):
    """売上伝票(01) レイアウト追加要件の回帰テスト。"""

    _SVC_SRC: str = ""
    _TPL_SRC: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._SVC_SRC = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")
        cls._TPL_SRC = (PROJECT_ROOT / "app" / "voucher_templates.py").read_text(encoding="utf-8")

    # ── 1. 摘要列の左右分割X座標が定義されている ────────────────────────────
    def test_note_mid_x_constant_defined(self) -> None:
        from app.voucher_templates import TBL_NOTE_MID_X, TBL_COLS
        self.assertAlmostEqual(TBL_NOTE_MID_X, (TBL_COLS[5] + TBL_COLS[6]) / 2, places=1)

    # ── 2. 品名/数量のアライメント定数が分かれている ─────────────────────────
    def test_det_name_rx_constant_defined(self) -> None:
        from app.voucher_templates import DET_NAME_RX, TBL_COLS, DATA_X_PAD
        self.assertAlmostEqual(DET_NAME_RX, TBL_COLS[2] - DATA_X_PAD, places=1)

    def test_det_qty_rx_constant_defined(self) -> None:
        from app.voucher_templates import DET_QTY_RX, TBL_COLS, DATA_X_PAD
        self.assertAlmostEqual(DET_QTY_RX, TBL_COLS[3] - DATA_X_PAD, places=1)

    def test_fs_dim_large_is_bigger_than_fs_val(self) -> None:
        from app.voucher_templates import FS_DIM_LARGE
        self.assertGreater(FS_DIM_LARGE, 7.8)

    # ── 3. 品名/数量の1段目・2段目アライメントがソースに反映されている ────────
    def test_name_first_row_uses_str_left_align(self) -> None:
        """品名1段目が_str_name（左寄せ・トリムなし）で描画されること。"""
        self.assertIn("_str_name(c, row.get(\"name\"", self._SVC_SRC)

    def test_dims_second_row_uses_rstr_right_align(self) -> None:
        """寸法2段目が_rstr（右寄せ）でDET_NAME_RXを使うこと。"""
        self.assertIn("DET_NAME_RX", self._SVC_SRC)
        self.assertIn("FS_DIM_LARGE", self._SVC_SRC)

    def test_qty_uses_rstr_and_det_qty_rx(self) -> None:
        """数量2段目が_rstr（右寄せ）でDET_QTY_RXを使うこと。"""
        self.assertIn("DET_QTY_RX", self._SVC_SRC)

    # ── 4. *行の右側列がスキップされる ──────────────────────────────────────
    def test_star_row_skipped_in_source(self) -> None:
        """品名が*の行でis_star判定が行われること。"""
        self.assertIn("is_star", self._SVC_SRC)
        self.assertIn("== \"*\"", self._SVC_SRC)

    def test_star_row_skips_qty_unit_amount(self) -> None:
        """*行はif not is_starブロックで数量・単価・金額をスキップすること。"""
        self.assertIn("if not is_star:", self._SVC_SRC)

    # ── 5. 受注No/伝票Noが表外右下に表示される ──────────────────────────────
    def test_order_no_display_in_source(self) -> None:
        """受注Noの受 表示コードがあること。"""
        self.assertIn("受  {order_no}", self._SVC_SRC)

    def test_voucher_no_display_in_source(self) -> None:
        """伝票Noの伝 表示コードがあること。"""
        self.assertIn("伝  {voucher_no}", self._SVC_SRC)

    def test_note_rx_used_for_order_voucher_no(self) -> None:
        """note_rxが受注No/伝票No表示に使われること。"""
        self.assertIn("note_rx", self._SVC_SRC)

    # ── 6. 加工名リストに既存12加工＋名称なし13枠目が並ぶ ───────────────────
    def test_proc_labels_has_blank_thirteenth_frame(self) -> None:
        from app.voucher_templates import PROC_LABELS
        from app.voucher_window import PROCESS_NAMES
        self.assertEqual(len(PROC_LABELS), 13)
        # 加工チェック・OLAP由来の内部判定は従来12項目のまま。
        self.assertEqual(PROC_LABELS[:12], PROCESS_NAMES)
        self.assertEqual(PROC_LABELS[12], "")

    # ── 7. 担当者ラベルは廃止し、担当者データは維持する ───────────────────────
    def test_staff_labels_removed_but_values_remain(self) -> None:
        self.assertNotIn("営業担当：", self._SVC_SRC)
        self.assertNotIn("工事担当：", self._SVC_SRC)
        self.assertIn("SUM_STAFF_X", self._SVC_SRC)
        self.assertIn('data.get("sales_rep"', self._SVC_SRC)
        self.assertIn('data.get("construction_rep"', self._SVC_SRC)

    # ── 8. 下部エリアが上へ拡張されている ────────────────────────────────────
    def test_lower_section_top_moved_up(self) -> None:
        from app.voucher_templates import FORM_LWR_TOP, FORM_TOTAL_BOT
        self.assertGreater(FORM_LWR_TOP, 160.0)

    def test_proc_labels_count_is_13(self) -> None:
        from app.voucher_templates import PROC_LABELS
        self.assertEqual(len(PROC_LABELS), 13)


class TestVoucher01Regression4(unittest.TestCase):
    """売上伝票(01) 追加修正（合計右端・摘要移動・下部枠統合）の回帰テスト。"""

    _SVC_SRC: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._SVC_SRC = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")

    # ── 1. 合計行摘要列の合計値X座標が摘要列右端基準であること ──────────────────
    def test_total_note_uses_note_rx(self) -> None:
        """合計行摘要列が note_rx（摘要列右端）を使って右揃えで表示されること。"""
        src = self._SVC_SRC
        # total_upper_y / total_lower_y の _rstr 呼び出しで note_rx が使われること
        self.assertIn("note_rx, total_upper_y", src)
        self.assertIn("note_rx, total_lower_y", src)

    def test_total_note_not_uses_mid_x(self) -> None:
        """合計行摘要列が TBL_NOTE_MID_X を使わないこと（右端寄せのみ）。"""
        src = self._SVC_SRC
        # total_upper_y または total_lower_y と TBL_NOTE_MID_X の組み合わせがないこと
        self.assertNotIn("TBL_NOTE_MID_X - TBL_NOTE_MID_PAD, total_upper_y", src)
        self.assertNotIn("TBL_NOTE_MID_X - TBL_NOTE_MID_PAD, total_lower_y", src)

    # ── 2. 中央摘要/物件NoのY座標が合計行底辺より上へ移動していること ──────────
    def test_sum_top_above_total_bot(self) -> None:
        """FORM_SUM_TOP が FORM_TOTAL_BOT より上（Y座標が大きい）であること。"""
        from app.voucher_templates import FORM_SUM_TOP, FORM_TOTAL_BOT
        self.assertGreater(FORM_SUM_TOP, FORM_TOTAL_BOT)

    def test_sum_top_below_detail_bot(self) -> None:
        """FORM_SUM_TOP が FORM_DETAIL_BOT より下（Y座標が小さい）で表と重ならないこと。"""
        from app.voucher_templates import FORM_SUM_TOP, FORM_DETAIL_BOT
        self.assertLess(FORM_SUM_TOP, FORM_DETAIL_BOT)

    def test_sum_gap_uses_detail_bot(self) -> None:
        """FORM_SUM_GAP が FORM_DETAIL_BOT 基準で設定されていること。"""
        from app.voucher_templates import FORM_SUM_TOP, FORM_SUM_GAP, FORM_DETAIL_BOT
        self.assertAlmostEqual(FORM_SUM_TOP, FORM_DETAIL_BOT - FORM_SUM_GAP, places=1)

    # ── 3. 加工名枠と右側大枠の上辺・下辺が一致すること ────────────────────────
    def test_lower_section_unified_outer_box(self) -> None:
        """下部エリアが1つの外枠 (MR - ML) で描画されること。"""
        self.assertIn("MR - ML, TOP - BOT", self._SVC_SRC)

    def test_lower_section_has_divider_line(self) -> None:
        """加工名列と右大枠の縦仕切り線が CHK_R を使って描画されること。"""
        self.assertIn("c.line(CHK_R, BOT, CHK_R, TOP)", self._SVC_SRC)

    def test_lower_boxes_top_and_bottom_same_constant(self) -> None:
        """加工名エリアと右大枠が同じ FORM_LWR_TOP / FORM_LWR_BOT を使うこと。"""
        from app.voucher_templates import FORM_LWR_TOP, FORM_LWR_BOT
        # 統合した外枠が両エリアを包含することをY座標の一致として確認
        self.assertGreater(FORM_LWR_TOP, FORM_LWR_BOT)


class TestVoucher01Regression7(unittest.TestCase):
    """指図書系(03-06)アプリ描画・タイトル下線・レイアウト下移動の回帰テスト。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._SVC_SRC = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")
        cls._TPL_SRC = (PROJECT_ROOT / "app" / "voucher_templates.py").read_text(encoding="utf-8")

    # ── テンプレートチェック ────────────────────────────────────────────────────
    def test_check_templates_skips_app_drawn_vouchers(self) -> None:
        self.assertIn('if vid in ("01", "02", "03", "04", "05", "06", "07", "08"):', self._SVC_SRC)

    # ── PDF 生成 ───────────────────────────────────────────────────────────────
    def _pdf_len(self, vid: str) -> int:
        from app.voucher_service import build_vouchers_pdf_bytes
        from app.voucher_templates import DUMMY_DATA
        return len(build_vouchers_pdf_bytes([vid], DUMMY_DATA))

    def test_shizu_03_pdf_generation(self) -> None:
        self.assertGreater(self._pdf_len("03"), 0)

    def test_shizu_04_pdf_generation(self) -> None:
        self.assertGreater(self._pdf_len("04"), 0)

    def test_shizu_05_pdf_generation(self) -> None:
        self.assertGreater(self._pdf_len("05"), 0)

    def test_shizu_06_pdf_generation(self) -> None:
        self.assertGreater(self._pdf_len("06"), 0)

    # ── 構造 ───────────────────────────────────────────────────────────────────
    def test_shizu_no_total_row_in_source(self) -> None:
        """指図書系は合計行なし(_draw_detail_outline_nototal を使うこと)。"""
        self.assertIn("_draw_detail_outline_nototal", self._SVC_SRC)

    def test_shizu_col_labels_biko_nyuki(self) -> None:
        from app.voucher_templates import SHIZU_COL_LABELS
        self.assertIn("備　考", SHIZU_COL_LABELS)
        self.assertIn("受入日", SHIZU_COL_LABELS)

    def test_shizu_titles_in_source(self) -> None:
        self.assertIn("指　図　書　(1)", self._SVC_SRC)
        self.assertIn("指　図　書　(2)", self._SVC_SRC)
        self.assertIn("梱　包　明　細　書", self._SVC_SRC)
        self.assertIn("配　送　指　示　書", self._SVC_SRC)

    def test_gen_circle_in_source(self) -> None:
        """点線丸「現」の描画関数が存在すること。"""
        self.assertIn("_draw_gen_circle", self._SVC_SRC)
        self.assertIn('"現"', self._SVC_SRC)

    def test_noki_line_in_source(self) -> None:
        """納期行の描画関数が存在すること。"""
        self.assertIn("_draw_noki_line", self._SVC_SRC)
        self.assertIn("納期", self._SVC_SRC)
        self.assertIn("受入方法", self._SVC_SRC)

    # ── タイトル下線 ───────────────────────────────────────────────────────────
    def test_title_underline_extend_positive(self) -> None:
        """FORM_TITLE_UL_EXTEND が正の値であること。"""
        from app.voucher_templates import FORM_TITLE_UL_EXTEND
        self.assertGreater(FORM_TITLE_UL_EXTEND, 0)

    def test_title_underline_extend_used_in_01(self) -> None:
        """売上伝票の下線描画に固定幅定数 FORM_TITLE_UL_HALF が使われていること。"""
        self.assertIn("FORM_TITLE_UL_HALF", self._SVC_SRC)
        self.assertIn("ul_half", self._SVC_SRC)

    def test_title_ul_y_unchanged(self) -> None:
        """タイトル自体(FORM_TITLE_UL_Y)は移動していないこと。"""
        from app.voucher_templates import FORM_TITLE_UL_Y
        self.assertEqual(FORM_TITLE_UL_Y, 489.0)

    # ── フォーム下移動 ─────────────────────────────────────────────────────────
    def test_form_hdr_top_shifted_down(self) -> None:
        """ヘッダー枠上端が以前より下がっていること(484→475)。"""
        from app.voucher_templates import FORM_HDR_TOP
        self.assertLess(FORM_HDR_TOP, 480.0)

    def test_form_mb_keeps_print_safe_margin(self) -> None:
        """実印刷の見切れ防止として下端マージンを24pt以上確保すること。"""
        from app.voucher_templates import FORM_MB, PRINT_SAFE_BOTTOM_MARGIN_PT
        self.assertGreaterEqual(FORM_MB, 24.0)
        self.assertEqual(FORM_MB, PRINT_SAFE_BOTTOM_MARGIN_PT)

    def test_finish_date_in_detail_row(self) -> None:
        """detail_row に finish_date キーが含まれること。"""
        from app.voucher_data_mapper import build_voucher_pages, extract_r1_rows
        import json
        from datetime import date
        from pathlib import Path
        text = (PROJECT_ROOT / "docs" / "olap2" / "04_データ取得_レスポンス.txt").read_text(encoding="utf-8")
        response = json.loads(text.split("\n\n", 1)[1])
        rows = extract_r1_rows(response)
        pages = build_voucher_pages(rows, today=date(2026, 6, 5))
        self.assertIn("finish_date", pages[0]["details"][0])


class TestVoucher01Regression6(unittest.TestCase):
    """中央担当者ラベル削除・データ維持 + 工場控タイトルの回帰テスト。"""

    def _svc_src(self) -> str:
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[1] / "app" / "voucher_service.py").read_text(encoding="utf-8")

    def test_sales_rep_label_is_not_drawn(self) -> None:
        src = self._svc_src()
        self.assertNotIn("営業担当：", src)

    def test_construction_rep_label_is_not_drawn(self) -> None:
        src = self._svc_src()
        self.assertNotIn("工事担当：", src)

    def test_empty_staff_data_still_builds_pdf(self) -> None:
        from app.voucher_service import build_vouchers_pdf_bytes
        from app.voucher_templates import DUMMY_DATA
        data = {**DUMMY_DATA, "sales_rep": "", "construction_rep": ""}
        pdf_bytes = build_vouchers_pdf_bytes(["01"], data)
        self.assertGreater(len(pdf_bytes), 0)

    def test_kouji_hikae_title(self) -> None:
        """工場控(02)の PDF が生成できること。"""
        from app.voucher_service import build_vouchers_pdf_bytes
        from app.voucher_templates import DUMMY_DATA
        pdf_bytes = build_vouchers_pdf_bytes(["02"], DUMMY_DATA)
        self.assertGreater(len(pdf_bytes), 0)

    def test_kouji_hikae_title_in_source(self) -> None:
        """_assemble_pdf_bytes が vid=='02' のとき工場控タイトルで描画すること。"""
        src = self._svc_src()
        self.assertIn('title="工　場　控"', src)

    def test_check_templates_skips_02(self) -> None:
        """_check_templates がアプリ描画方式の伝票をスキップしていること。"""
        src = self._svc_src()
        self.assertIn('if vid in ("01", "02", "03", "04", "05", "06", "07", "08"):', src)


class TestVoucher01Regression5(unittest.TestCase):
    """TAX_Y オーバーラップ修正の回帰テスト。"""

    def test_tax_y_below_bkno_line_with_margin(self) -> None:
        """お客様注文No表示(1.2倍)が物件No下線と重ならないこと。"""
        from app.voucher_templates import TAX_Y, FORM_BKNO_BOT, CUSTOMER_ORDER_NO_FONT_SIZE
        self.assertLess(TAX_Y + CUSTOMER_ORDER_NO_FONT_SIZE, FORM_BKNO_BOT)

    def test_lwr_top_below_tax_y(self) -> None:
        """下部枠の上端がお客様注文No表示位置より下にあること。"""
        from app.voucher_templates import FORM_LWR_TOP, TAX_Y
        self.assertLess(FORM_LWR_TOP, TAX_Y)

    def test_lower_boxes_aligned_via_unified_rect(self) -> None:
        """加工名枠と右側大枠が同一の FORM_LWR_TOP/BOT で上辺・下辺が一致すること。"""
        from app.voucher_templates import (
            FORM_LWR_TOP, FORM_LWR_BOT, FORM_MB, LOWER_SHIFT_UP,
        )
        self.assertGreater(FORM_LWR_TOP, FORM_LWR_BOT)
        # 下部見切れ対策で下端は安全余白より LOWER_SHIFT_UP 分だけ上げる。
        self.assertEqual(FORM_LWR_BOT, FORM_MB + LOWER_SHIFT_UP)

    def test_all_lower_sections_are_above_print_safe_margin(self) -> None:
        """01〜08の最下部枠が安全余白より上に収まること（見切れ対策で更に上げる）。"""
        from app.voucher_templates import (
            FORM_LWR_BOT, FORM_CUT_BOT,
            PRINT_SAFE_BOTTOM_MARGIN_PT,
        )
        # FORM_LWR_BOT は安全余白以上の高さに位置する（より大きい下部余白を確保）。
        self.assertGreaterEqual(FORM_LWR_BOT, PRINT_SAFE_BOTTOM_MARGIN_PT)
        self.assertGreaterEqual(FORM_CUT_BOT, PRINT_SAFE_BOTTOM_MARGIN_PT)

    def test_lower_elements_use_safe_bottom_constant(self) -> None:
        """下部大枠・特記事項枠・QRコードがFORM_LWR_BOT基準で描画されること。"""
        src = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")
        self.assertIn("BOT   = FORM_LWR_BOT", src)
        self.assertIn("c.roundRect(FORM_LWR_LEFT, FORM_LWR_BOT", src)
        self.assertIn("FORM_LWR_BOT + 12.0", src)

    def test_summary_lines_do_not_overlap_detail_table(self) -> None:
        """摘要/物件Noラインは表の下にあり、下部枠より上に残ること。"""
        from app.voucher_templates import FORM_DETAIL_BOT, FORM_SUM_TOP, FORM_BKNO_BOT, FORM_LWR_TOP
        self.assertLess(FORM_SUM_TOP, FORM_DETAIL_BOT)
        self.assertGreater(FORM_BKNO_BOT, FORM_LWR_TOP)


class TestVoucherLowerShiftAndDimShift(unittest.TestCase):
    """下部見切れ対策（摘要以下を上へ）と品名WH表示の左移動の回帰テスト。"""

    def test_lower_shift_up_is_about_5_7pt(self) -> None:
        """約2mm = reportlab座標で約5.7pt 上方向。"""
        from app.voucher_templates import LOWER_SHIFT_UP
        self.assertAlmostEqual(LOWER_SHIFT_UP, 5.7, places=2)

    def test_table_position_unchanged(self) -> None:
        """明細表（FORM_DETAIL_BOT・ヘッダー）は固定のままであること。"""
        from app.voucher_templates import (
            FORM_DETAIL_BOT, FORM_TBL_HDR_BOT, FORM_DETAIL_ROWS, FORM_DETAIL_ROW_H,
            FORM_HDR_TOP,
        )
        self.assertEqual(FORM_DETAIL_BOT, FORM_TBL_HDR_BOT - FORM_DETAIL_ROWS * FORM_DETAIL_ROW_H)
        self.assertEqual(FORM_HDR_TOP, 475.0)

    def test_lower_block_shifted_up_by_shift(self) -> None:
        """摘要以下の下端が安全余白から LOWER_SHIFT_UP 分上がっていること。"""
        from app.voucher_templates import FORM_LWR_BOT, FORM_MB, LOWER_SHIFT_UP
        self.assertAlmostEqual(FORM_LWR_BOT, FORM_MB + LOWER_SHIFT_UP, places=2)

    def test_sum_top_relation_preserved(self) -> None:
        """FORM_SUM_TOP は FORM_DETAIL_BOT - FORM_SUM_GAP の関係を保つ（表基準）。"""
        from app.voucher_templates import FORM_SUM_TOP, FORM_DETAIL_BOT, FORM_SUM_GAP
        self.assertAlmostEqual(FORM_SUM_TOP, FORM_DETAIL_BOT - FORM_SUM_GAP, places=2)

    def test_dim_shift_left_is_zero_after_right_move(self) -> None:
        """WH表示を1cm右へ移動したため、左移動量は 0pt（品名列右端基準）。"""
        from app.voucher_templates import DIM_SHIFT_LEFT
        self.assertAlmostEqual(DIM_SHIFT_LEFT, 0.0, places=2)

    def test_dims_drawn_with_left_shift_in_source(self) -> None:
        """寸法(WH)描画が DET_NAME_RX - DIM_SHIFT_LEFT を使い、商品名は移動しないこと。"""
        src = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")
        self.assertIn("DET_NAME_RX - DIM_SHIFT_LEFT", src)
        # 品名1段目は TBL_X_NAME 左寄せのまま（移動しない）。
        # 品名描画は先頭スペース保持のため _str_name 経由（トリムしない）。
        self.assertIn("_str_name(c, row.get(\"name\", \"\"), TBL_X_NAME", src)


class TestVoucherLayoutRegression8(unittest.TestCase):
    """要件8: レイアウト改修（タイトル下線・印枠・受入日・受伝制御・納期行）の回帰テスト。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._SVC_SRC = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")
        cls._TPL_SRC = (PROJECT_ROOT / "app" / "voucher_templates.py").read_text(encoding="utf-8")

    # ── 2. タイトル下線固定定数 ───────────────────────────────────────────────
    def test_form_title_ul_half_defined(self) -> None:
        """FORM_TITLE_UL_HALF が定義されており 70pt 超であること。"""
        from app.voucher_templates import FORM_TITLE_UL_HALF
        self.assertGreater(FORM_TITLE_UL_HALF, 70.0)

    def test_title_underline_uses_fixed_constant_in_01(self) -> None:
        """売上伝票/工場控の下線描画が FORM_TITLE_UL_HALF を使うこと。"""
        self.assertIn("FORM_TITLE_UL_HALF", self._SVC_SRC)
        self.assertNotIn("title_w / 2 + FORM_TITLE_UL_EXTEND", self._SVC_SRC)

    def test_title_underline_uses_fixed_constant_in_shizu(self) -> None:
        """指図書系の下線描画も FORM_TITLE_UL_HALF を使うこと（全伝票共通）。"""
        count = self._SVC_SRC.count("FORM_TITLE_UL_HALF")
        self.assertGreaterEqual(count, 2)

    # ── 4. 受/伝 表示制御（指図書系には表示しない）─────────────────────────────
    def test_jyudensha_no_removed_from_shizu_data(self) -> None:
        """_draw_form_data_shizu 内に 受/伝 の描画がないこと。"""
        import ast
        tree = ast.parse(self._SVC_SRC)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_draw_form_data_shizu":
                func_src = ast.get_source_segment(self._SVC_SRC, node) or ""
                self.assertNotIn('f"受  {order_no}"', func_src)
                self.assertNotIn('f"伝  {voucher_no}"', func_src)
                return
        self.fail("_draw_form_data_shizu が見つかりません")

    def test_jyudensha_still_in_01_data(self) -> None:
        """売上伝票(_draw_form_data_01)には 受/伝 の描画があること。"""
        import ast
        tree = ast.parse(self._SVC_SRC)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_draw_form_data_01":
                func_src = ast.get_source_segment(self._SVC_SRC, node) or ""
                self.assertIn('f"受  {order_no}"', func_src)
                self.assertIn('f"伝  {voucher_no}"', func_src)
                return
        self.fail("_draw_form_data_01 が見つかりません")

    # ── 5-8. 印枠 ──────────────────────────────────────────────────────────────
    def test_stamp_03_is_koujo_in(self) -> None:
        """03 指図書(1) の stamp_title が '工場印' であること。"""
        idx = self._SVC_SRC.find('vid == "03"')
        self.assertGreater(idx, 0)
        snippet = self._SVC_SRC[idx:idx + 200]
        self.assertIn("工場印", snippet)

    def test_stamp_04_is_shohinkaiin(self) -> None:
        """04 指図書(2) の stamp_title が '商品課印' であること。"""
        idx = self._SVC_SRC.find('vid == "04"')
        self.assertGreater(idx, 0)
        snippet = self._SVC_SRC[idx:idx + 200]
        self.assertIn("商品課印", snippet)

    def test_stamp_05_is_haisousha_in(self) -> None:
        """05 梱包明細書の stamp_title が '配送者印' であること。"""
        idx = self._SVC_SRC.find('vid == "05"')
        self.assertGreater(idx, 0)
        snippet = self._SVC_SRC[idx:idx + 200]
        self.assertIn("配送者印", snippet)

    def test_stamp_06_is_haisousha_in(self) -> None:
        """06 配送指示書の stamp_title が '配送者印' であること。"""
        idx = self._SVC_SRC.find('vid == "06"')
        self.assertGreater(idx, 0)
        snippet = self._SVC_SRC[idx:idx + 200]
        self.assertIn("配送者印", snippet)

    def test_draw_stamp_box_function_exists(self) -> None:
        """_draw_stamp_box 関数が存在すること。"""
        self.assertIn("def _draw_stamp_box", self._SVC_SRC)

    # ── 9. 納期行・現 の移動 ────────────────────────────────────────────────
    def test_gen_circle_x_moved_right(self) -> None:
        """GEN_CIRCLE_X が 30.0 より右であること。"""
        from app.voucher_templates import GEN_CIRCLE_X
        self.assertGreater(GEN_CIRCLE_X, 30.0)

    def test_noki_line_x_at_code_no_right(self) -> None:
        """NOKI_LINE_X がコードNo枠右端（HDR_ROW1_DIVS[0]）以上であること。"""
        from app.voucher_templates import NOKI_LINE_X, HDR_ROW1_DIVS
        self.assertGreaterEqual(NOKI_LINE_X, HDR_ROW1_DIVS[0])

    def test_noki_line_uses_noki_line_x(self) -> None:
        """_draw_noki_line が NOKI_LINE_X を使うこと。"""
        self.assertIn("NOKI_LINE_X", self._SVC_SRC)

    def test_gen_circle_uses_gen_circle_x(self) -> None:
        """_draw_gen_circle が GEN_CIRCLE_X を使うこと。"""
        self.assertIn("GEN_CIRCLE_X", self._SVC_SRC)

    # ── 3. 受入日列データ ──────────────────────────────────────────────────────
    def test_nyuki_note_text_shown_in_shizu(self) -> None:
        """_draw_form_data_shizu が note_lines から _split_note でテキストを取り出すこと。"""
        import ast
        tree = ast.parse(self._SVC_SRC)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_draw_form_data_shizu":
                func_src = ast.get_source_segment(self._SVC_SRC, node) or ""
                self.assertIn("note_lines", func_src)
                self.assertIn("_split_note", func_src)
                return
        self.fail("_draw_form_data_shizu が見つかりません")

    # ── 1. 摘要列 日付右端寄せ ────────────────────────────────────────────────
    def test_is_date_str_function_exists(self) -> None:
        """_is_date_str 関数が存在すること。"""
        self.assertIn("def _is_date_str", self._SVC_SRC)

    def test_is_date_str_detects_date(self) -> None:
        """_is_date_str が 'MM/DD' 形式の日付を検出すること。"""
        from app.voucher_service import _is_date_str
        self.assertTrue(_is_date_str("06/19"))
        self.assertFalse(_is_date_str("加"))
        self.assertFalse(_is_date_str("倉庫ま"))
        self.assertFalse(_is_date_str("東大阪"))

    def test_date_right_align_in_note_column(self) -> None:
        """摘要列で finish_date を上段右端に右揃えで描画するコードがあること。"""
        # finish_date を row から取り出して note_rx に _rstr するコードが存在する
        self.assertIn("finish_date", self._SVC_SRC)
        self.assertIn('row.get("finish_date"', self._SVC_SRC)

    # ── PDF 生成確認（03-06 全種） ────────────────────────────────────────────
    def _make_pdf(self, vid: str) -> int:
        from app.voucher_service import build_vouchers_pdf_bytes
        from app.voucher_templates import DUMMY_DATA
        return len(build_vouchers_pdf_bytes([vid], DUMMY_DATA, base_dir=PROJECT_ROOT))

    def test_pdf_03_generates_ok(self) -> None:
        self.assertGreater(self._make_pdf("03"), 0)

    def test_pdf_04_generates_ok(self) -> None:
        self.assertGreater(self._make_pdf("04"), 0)

    def test_pdf_05_generates_ok(self) -> None:
        self.assertGreater(self._make_pdf("05"), 0)

    def test_pdf_06_generates_ok(self) -> None:
        self.assertGreater(self._make_pdf("06"), 0)


class TestDeliveryReceiptLayout(unittest.TestCase):
    """納品書(07)・受領書(08)アプリ描画レイアウトの回帰テスト。"""

    _SVC_SRC: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._SVC_SRC = (PROJECT_ROOT / "app" / "voucher_service.py").read_text(encoding="utf-8")

    def _func_src(self, name: str) -> str:
        import ast
        tree = ast.parse(self._SVC_SRC)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(self._SVC_SRC, node) or ""
        self.fail(f"{name} が見つかりません")

    def test_delivery_note_has_right_blank_column(self) -> None:
        src = self._func_src("_draw_delivery_table_07")
        self.assertIn('labels = ["No", "品　名", "数　量", "単　価", "金　額", ""]', src)
        self.assertIn("zip(TBL_COLS, TBL_COLS[1:], labels)", src)
        self.assertIn("_draw_delivery_07_right_column_mask(c)", src)

    def test_delivery_note_right_column_is_masked(self) -> None:
        src = self._func_src("_draw_delivery_07_right_column_mask")
        self.assertIn("TBL_COLS[5]", src)
        self.assertIn("FORM_DETAIL_BOT", src)
        self.assertIn("FORM_HDR_BOT - FORM_DETAIL_BOT", src)
        self.assertIn("_fill_delivery_right_round_mask", src)

    def test_delivery_note_right_column_has_no_internal_horizontal_lines(self) -> None:
        src = self._func_src("_draw_delivery_table_07")
        self.assertIn("c.line(table_left, y, total_right, y)", src)
        self.assertIn("c.line(table_left, FORM_TBL_HDR_BOT, total_right, FORM_TBL_HDR_BOT)", src)
        self.assertNotIn("c.line(table_left, y, table_right, y)", src)
        self.assertNotIn("c.line(table_left, FORM_TBL_HDR_BOT, table_right, FORM_TBL_HDR_BOT)", src)

    def test_delivery_note_draws_total_row(self) -> None:
        src = self._func_src("_draw_delivery_table_07")
        self.assertIn("_bottom_left_round_rect_path", src)
        self.assertIn("合　計", src)
        self.assertIn("FORM_TOTAL_CELL_RIGHT, FORM_TOTAL_BOT", src)
        self.assertIn("total_right = TBL_COLS[5]", src)
        self.assertIn("total_right - CORNER_R", src)

    def test_delivery_note_total_outline_omits_right_mask_column(self) -> None:
        src = self._func_src("_draw_delivery_07_outline")
        self.assertIn("total_right", src)
        self.assertIn("p.lineTo(right, detail_bot + r)", src)
        self.assertIn("p.curveTo(right, detail_bot + r / 2", src)
        self.assertIn("p.lineTo(total_right, detail_bot)", src)
        self.assertIn("p.lineTo(total_right, total_bot + r)", src)

    def test_delivery_note_total_right_border_stops_before_round_corner(self) -> None:
        src = self._func_src("_draw_delivery_table_07")
        self.assertIn("elif x == total_right:", src)
        self.assertIn("bottom = FORM_TOTAL_BOT + CORNER_R", src)

    def test_delivery_header_keeps_hidden_cell_frames(self) -> None:
        src = self._func_src("_draw_header_no_issue")
        self.assertIn("_draw_delivery_header_masks(c)", src)
        self.assertIn("for x in HDR_ROW1_DIVS:", src)
        self.assertIn("for x in HDR_ROW2_DIVS:", src)
        self.assertNotIn('lbl("発行日"', src)
        self.assertNotIn('lbl("仕上日"', src)
        self.assertNotIn("AM・PM", src)

    def test_delivery_header_cells_are_masked(self) -> None:
        src = self._func_src("_draw_delivery_header_masks")
        self.assertIn("FORM_HDR_LEFT", src)
        self.assertIn("HDR_ROW2_DIVS[0] - FORM_HDR_LEFT", src)
        self.assertIn("_fill_delivery_top_right_mask", src)
        self.assertIn("HDR_ROW1_DIVS[-1]", src)
        self.assertIn("HDR_ROW2_DIVS[-1]", src)
        self.assertEqual(src.count("_fill_delivery_mask"), 2)

    def test_delivery_round_masks_define_required_rounded_corners(self) -> None:
        top_right_src = self._func_src("_fill_delivery_top_right_mask")
        self.assertIn("p.curveTo(x + w - r / 2", top_right_src)
        right_round_src = self._func_src("_fill_delivery_right_round_mask")
        self.assertIn("p.curveTo(x + w - r / 2", right_round_src)
        self.assertIn("p.curveTo(x + w, y + r / 2", right_round_src)

    def test_delivery_masks_use_gray_fill_and_leave_borders_to_callers(self) -> None:
        src = self._func_src("_fill_delivery_mask")
        self.assertIn("setFillColorRGB(*DELIVERY_MASK_RGB)", src)
        self.assertIn("stroke=0, fill=1", src)
        header_src = self._func_src("_draw_header_no_issue")
        self.assertLess(
            header_src.index("_draw_delivery_header_masks(c)"),
            header_src.index("c.drawPath("),
        )
        table_src = self._func_src("_draw_delivery_table_07")
        self.assertLess(
            table_src.index("_draw_delivery_07_right_column_mask(c)"),
            table_src.index("_draw_delivery_07_outline"),
        )

    def test_delivery_summary_draws_staff_values_without_labels(self) -> None:
        src = self._func_src("_draw_summary_rows")
        self.assertIn("_draw_staff_values", src)
        self.assertNotIn("営業担当：", src)
        self.assertNotIn("工事担当：", src)

    def test_receipt_stamp_column_has_no_internal_horizontal_lines(self) -> None:
        src = self._func_src("_draw_receipt_table_08")
        self.assertIn("receipt_left", src)
        self.assertIn("c.line(table_left, y, receipt_left, y)", src)
        self.assertNotIn("c.line(table_left, y, table_right, y)", src)

    def test_receipt_stamp_boxes_use_stamp_x_and_correct_titles(self) -> None:
        src = self._func_src("_draw_delivery_stamp_boxes")
        self.assertIn("STAMP_X", src)
        self.assertIn('("検印", "配送者印")', src)
        self.assertNotIn("配送者員", src)

    def test_delivery_receipt_draw_company_details(self) -> None:
        src = self._func_src("_draw_company_detail_lines")
        self.assertIn("office_name", src)
        self.assertIn("office_tel", src)
        self.assertIn("office_fax", src)
        self.assertIn("TEL", src)
        self.assertIn("FAX", src)

    def test_pdf_07_08_generate_ok(self) -> None:
        from app.voucher_service import build_vouchers_pdf_bytes
        from app.voucher_templates import DUMMY_DATA
        self.assertGreater(len(build_vouchers_pdf_bytes(["07", "08"], DUMMY_DATA, base_dir=PROJECT_ROOT)), 0)


class TestRowSettingsRendering(unittest.TestCase):
    """画面行設定（仕上日・AM/PM・加工名チェック）の伝票描画テスト。"""

    def _canvas(self):
        import io
        from reportlab.pdfgen import canvas as rl_canvas
        from app import voucher_service

        voucher_service._ensure_font()
        return rl_canvas.Canvas(
            io.BytesIO(), pagesize=(voucher_service.PAGE_W, voucher_service.PAGE_H)
        )

    def test_header_finish_date_drawn_as_month_day(self) -> None:
        """仕上日が月・日の数値として描画されること。"""
        from datetime import date
        from app import voucher_service

        c = self._canvas()
        drawn: list[str] = []
        c.drawRightString = lambda x, y, t: drawn.append(t)  # type: ignore[assignment]
        voucher_service._draw_header_finish_date(c, date(2026, 6, 10))
        self.assertIn("6", drawn)
        self.assertIn("10", drawn)

    def test_header_finish_date_skipped_when_none(self) -> None:
        from app import voucher_service

        c = self._canvas()
        drawn: list[str] = []
        c.drawRightString = lambda x, y, t: drawn.append(t)  # type: ignore[assignment]
        voucher_service._draw_header_finish_date(c, None)
        self.assertEqual(drawn, [])

    def test_screen_finish_date_takes_priority_over_olap(self) -> None:
        """画面設定の仕上日(row_finish_date)がOLAP値(shiage_date)より優先されること。"""
        from datetime import date
        from app import voucher_service

        c = self._canvas()
        drawn: list[str] = []
        c.drawRightString = lambda x, y, t: drawn.append(t)  # type: ignore[assignment]
        c.ellipse = lambda *a, **k: None  # type: ignore[assignment]
        c.rect = lambda *a, **k: None  # type: ignore[assignment]
        data = {
            "row_finish_date": date(2026, 7, 3),
            "shiage_date": "12/25",
            "row_am_pm": "",
            "row_process_checks": {},
        }
        voucher_service._draw_row_settings(c, data)
        self.assertIn("7", drawn)
        self.assertIn("3", drawn)
        self.assertNotIn("12", drawn)
        self.assertNotIn("25", drawn)

    def test_finish_date_not_overlapping_month_day_labels(self) -> None:
        """仕上日データ（月/日の数値）が「月」「日」ラベルと重ならないこと（要件3）。"""
        from datetime import date
        from reportlab.pdfbase import pdfmetrics
        from app import voucher_service
        from app.voucher_templates import (
            HDR_SHIAGE_LABEL_FS,
            HDR_SHIAGE_DAY_LABEL_RX, HDR_SHIAGE_MONTH_LABEL_CX,
            HDR_SHIAGE_MONTH_DATA_RX, HDR_SHIAGE_DAY_DATA_RX,
            HDR_ROW1_DIVS, FORM_HDR_RIGHT,
        )

        font = voucher_service._FONT_NAME
        # 実際の描画フォント（1.2倍。要件1）・2桁の月日（最大幅）で検証する。
        data_fs = voucher_service.HEADER_FINISH_DATE_VALUE_FONT_SIZE
        month_w = pdfmetrics.stringWidth("12", font, data_fs)
        day_w = pdfmetrics.stringWidth("31", font, data_fs)
        getsu_w = pdfmetrics.stringWidth("月", font, HDR_SHIAGE_LABEL_FS)
        nichi_w = pdfmetrics.stringWidth("日", font, HDR_SHIAGE_LABEL_FS)

        # 各要素の左右端（X区間）を算出。
        month_data = (HDR_SHIAGE_MONTH_DATA_RX - month_w, HDR_SHIAGE_MONTH_DATA_RX)
        getsu = (HDR_SHIAGE_MONTH_LABEL_CX - getsu_w / 2, HDR_SHIAGE_MONTH_LABEL_CX + getsu_w / 2)
        day_data = (HDR_SHIAGE_DAY_DATA_RX - day_w, HDR_SHIAGE_DAY_DATA_RX)
        nichi = (HDR_SHIAGE_DAY_LABEL_RX - nichi_w, HDR_SHIAGE_DAY_LABEL_RX)

        # 左から 月データ < 月 < 日データ < 日 の順で重ならないこと。
        self.assertLessEqual(month_data[1], getsu[0])
        self.assertLessEqual(getsu[1], day_data[0])
        self.assertLessEqual(day_data[1], nichi[0])
        # セル(371〜420)内に収まること。
        self.assertGreaterEqual(month_data[0], HDR_ROW1_DIVS[-1])
        self.assertLessEqual(nichi[1], FORM_HDR_RIGHT)

    def test_finish_date_data_larger_than_labels(self) -> None:
        """仕上日データのフォントが月日ラベルより大きいこと（要件3 できるだけ大きめ）。"""
        from app.voucher_templates import HDR_SHIAGE_DATA_FS, HDR_SHIAGE_LABEL_FS

        self.assertGreater(HDR_SHIAGE_DATA_FS, HDR_SHIAGE_LABEL_FS)

    def test_month_label_centered_day_label_right(self) -> None:
        """「月」は中央寄せ、「日」は右寄せで描画されること（要件3）。"""
        from app import voucher_service
        from app.voucher_templates import (
            HDR_SHIAGE_MONTH_LABEL_CX, HDR_SHIAGE_DAY_LABEL_RX,
        )

        c = self._canvas()
        centred: list[tuple[float, str]] = []
        right: list[tuple[float, str]] = []
        c.drawCentredString = lambda x, y, t: centred.append((x, t))  # type: ignore[assignment]
        c.drawRightString = lambda x, y, t: right.append((x, t))  # type: ignore[assignment]
        voucher_service._draw_shiage_month_day_labels(c)
        self.assertIn((HDR_SHIAGE_MONTH_LABEL_CX, "月"), centred)
        self.assertIn((HDR_SHIAGE_DAY_LABEL_RX, "日"), right)

    def test_am_circle_left_of_pm_circle(self) -> None:
        """AM選択時はAM側、PM選択時はPM側に丸が描画されること。"""
        from app import voucher_service

        c = self._canvas()
        spans: list[tuple[float, float]] = []
        c.ellipse = lambda x1, y1, x2, y2, stroke=1, fill=0: spans.append((x1, x2))  # type: ignore[assignment]

        voucher_service._draw_ampm_circle(c, "AM")
        self.assertEqual(len(spans), 1)
        am_center = (spans[0][0] + spans[0][1]) / 2

        spans.clear()
        voucher_service._draw_ampm_circle(c, "PM")
        self.assertEqual(len(spans), 1)
        pm_center = (spans[0][0] + spans[0][1]) / 2

        self.assertLess(am_center, pm_center)

    def test_ampm_circle_skipped_when_empty(self) -> None:
        from app import voucher_service

        c = self._canvas()
        spans: list = []
        c.ellipse = lambda *a, **k: spans.append(a)  # type: ignore[assignment]
        voucher_service._draw_ampm_circle(c, "")
        self.assertEqual(spans, [])

    def test_process_mark_only_on_checked(self) -> None:
        """加工名チェックON項目だけ太字の「✔」が描画され、OFFは描画されないこと。"""
        from app import voucher_service

        c = self._canvas()
        marks: list = []
        widths: list = []
        c.lines = lambda linelist: marks.append(list(linelist))  # type: ignore[assignment]
        c.setLineWidth = lambda w: widths.append(w)  # type: ignore[assignment]
        c.circle = lambda *a, **k: self.fail("●(circle)ではなく「✔」を描画すること")  # type: ignore[assignment]
        voucher_service._draw_process_check_marks(
            c, {"広幅": True, "BOB": False, "印刷": True}
        )
        # ON は 広幅・印刷 の2項目だけ「✔」が描画される
        self.assertEqual(len(marks), 2)
        # 各「✔」は2本の線分（左下→谷→右上）で構成される
        self.assertTrue(all(len(segs) == 2 for segs in marks))
        # 太字（通常の枠線より太い線幅）で描かれている
        self.assertTrue(widths)
        self.assertGreaterEqual(max(widths), 1.2)

    def test_process_mark_is_bold(self) -> None:
        """加工名チェックの「✔」が太字（太い線幅）で描かれること。"""
        from app import voucher_service

        c = self._canvas()
        widths: list = []
        c.lines = lambda linelist: None  # type: ignore[assignment]
        c.setLineWidth = lambda w: widths.append(w)  # type: ignore[assignment]
        voucher_service._draw_process_check_marks(c, {"広幅": True})
        self.assertIn(voucher_service.PROC_CHECK_LINE_WIDTH, widths)
        self.assertGreaterEqual(voucher_service.PROC_CHECK_LINE_WIDTH, 1.2)

    def test_process_mark_skipped_when_empty(self) -> None:
        from app import voucher_service

        c = self._canvas()
        marks: list = []
        c.lines = lambda *a, **k: marks.append(a)  # type: ignore[assignment]
        voucher_service._draw_process_check_marks(c, {})
        self.assertEqual(marks, [])

    def test_process_mark_off_not_drawn(self) -> None:
        """OFFの加工名には「✔」が描画されないこと。"""
        from app import voucher_service

        c = self._canvas()
        marks: list = []
        c.lines = lambda linelist: marks.append(list(linelist))  # type: ignore[assignment]
        voucher_service._draw_process_check_marks(
            c, {"広幅": False, "印刷": False, "BOB": False}
        )
        self.assertEqual(marks, [])

    def test_process_mark_film_and_rtori_drawn_when_on(self) -> None:
        """フィルム貼・Rとり がON時に「✔」が描画されること。"""
        from app import voucher_service

        c = self._canvas()
        marks: list = []
        c.lines = lambda linelist: marks.append(list(linelist))  # type: ignore[assignment]
        voucher_service._draw_process_check_marks(
            c, {"フィルム貼": True, "Rとり": True}
        )
        # フィルム貼・Rとり の2項目に「✔」が描かれる。
        self.assertEqual(len(marks), 2)
        self.assertTrue(all(len(segs) == 2 for segs in marks))

    def test_process_mark_film_and_rtori_off_not_drawn(self) -> None:
        """フィルム貼・Rとり がOFF時は「✔」が描画されないこと。"""
        from app import voucher_service

        c = self._canvas()
        marks: list = []
        c.lines = lambda linelist: marks.append(list(linelist))  # type: ignore[assignment]
        voucher_service._draw_process_check_marks(
            c, {"フィルム貼": False, "Rとり": False}
        )
        self.assertEqual(marks, [])

    def test_row_settings_skipped_for_delivery_vouchers(self) -> None:
        """納品書(07)・受領書(08)では行設定反映処理を呼ばないこと。"""
        from unittest.mock import patch
        from app import voucher_service
        from app.voucher_templates import DUMMY_DATA

        with patch.object(voucher_service, "_draw_row_settings") as m:
            voucher_service.build_vouchers_pdf_bytes(["07", "08"], DUMMY_DATA, base_dir=PROJECT_ROOT)
        m.assert_not_called()

    def test_row_settings_applied_for_target_vouchers(self) -> None:
        """売上伝票(01)・工場控(02)・指図書系(03-06)では行設定反映処理を呼ぶこと。"""
        from unittest.mock import patch
        from app import voucher_service
        from app.voucher_templates import DUMMY_DATA

        for vid in ("01", "02", "03", "04", "05", "06"):
            with patch.object(voucher_service, "_draw_row_settings") as m:
                voucher_service.build_vouchers_pdf_bytes([vid], DUMMY_DATA, base_dir=PROJECT_ROOT)
            m.assert_called()


class TestDeliveryCourseStaffRendering(unittest.TestCase):
    def _canvas(self):
        import io
        from reportlab.pdfgen import canvas as rl_canvas
        from app import voucher_service

        voucher_service._ensure_font()
        return rl_canvas.Canvas(
            io.BytesIO(), pagesize=(voucher_service.PAGE_W, voucher_service.PAGE_H)
        )

    def test_realistic_course_name_and_staff_are_drawn_as_one_string(self) -> None:
        from app import voucher_service

        canvas = self._canvas()
        drawn: list[tuple[float, str]] = []
        canvas.drawRightString = lambda x, y, text: drawn.append((x, text))  # type: ignore[assignment]
        canvas.drawString = lambda x, y, text: drawn.append((x, text))  # type: ignore[assignment]
        voucher_service._draw_staff_values(canvas, {
            "order_no": "1405113", "voucher_no": "Z001",
            "delivery_course_code": "01",
            "delivery_course_name": "パレト", "sales_rep": "大上",
        })
        texts = [text for _, text in drawn]
        self.assertIn("パレト 大上", texts)
        self.assertNotIn("01 大上", texts)
        self.assertNotIn("01", texts)

    def test_blank_name_never_falls_back_to_code(self) -> None:
        from app import voucher_service

        canvas = self._canvas()
        drawn: list[str] = []
        canvas.drawRightString = lambda x, y, text: drawn.append(text)  # type: ignore[assignment]
        canvas.drawString = lambda x, y, text: drawn.append(text)  # type: ignore[assignment]
        voucher_service._draw_staff_values(canvas, {
            "delivery_course_code": "01", "delivery_course_name": "",
            "sales_rep": "大上",
        })
        self.assertIn("大上", drawn)
        self.assertNotIn("01", drawn)

    def test_combined_text_keeps_legacy_sales_right_edge(self) -> None:
        from app import voucher_service

        canvas = self._canvas()
        right_draws: list[tuple[float, str]] = []
        canvas.drawRightString = lambda x, y, text: right_draws.append((x, text))  # type: ignore[assignment]
        voucher_service._draw_staff_values(canvas, {
            "delivery_course_name": "パレト", "sales_rep": "大上",
        })
        expected_right = min(
            voucher_service.STAFF_TEXT_RIGHT,
            voucher_service.STAFF_TEXT_X + canvas.stringWidth(
                "大上", voucher_service._FONT_NAME,
                voucher_service.DETAIL_DATA_FONT_SIZE,
            ),
        )
        combined = next(item for item in right_draws if item[1] == "パレト 大上")
        self.assertAlmostEqual(combined[0], expected_right)

    def test_long_combined_text_is_shrunk_without_line_break(self) -> None:
        from app import voucher_service

        canvas = self._canvas()
        text = "非常に長い配送コース名称テスト 大上"
        drawn: list[str] = []
        canvas.drawRightString = lambda x, y, value: drawn.append(value)  # type: ignore[assignment]
        used = voucher_service.draw_text_fit_width_right(
            canvas, text, 400, 200, 70,
            voucher_service.DATA_BOLD_FONT_NAME,
            voucher_service.DETAIL_DATA_FONT_SIZE, 4.0,
        )
        self.assertLess(used, voucher_service.DETAIL_DATA_FONT_SIZE)
        self.assertEqual(drawn, [text, text])  # 擬似太字の2回描画。改行・切捨てなし。

    def test_pdf_contains_course_name_and_combined_staff_for_all_vouchers(self) -> None:
        import io
        import pypdf
        from app.voucher_service import build_vouchers_pdf_bytes
        from app.voucher_templates import DUMMY_DATA

        page = {
            **DUMMY_DATA,
            "delivery_course_code": "01",
            "delivery_course_name": "パレト",
            "sales_rep": "大上",
        }
        pdf = build_vouchers_pdf_bytes(
            ["01", "02", "03", "04", "05", "06", "07", "08"],
            {"pages": [page]}, base_dir=PROJECT_ROOT,
        )
        reader = pypdf.PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 8)
        for pdf_page in reader.pages:
            text = pdf_page.extract_text() or ""
            self.assertIn("パレト", text)
            self.assertIn("大上", text)


class TestCustomerOrderNo(unittest.TestCase):
    """客先注文No_10桁（お客様注文No）表示の追加仕様テスト。"""

    _ALL_VOUCHER_IDS = ("01", "02", "03", "04", "05", "06", "07", "08")

    def _page_text(self, voucher_id: str, data: dict) -> str:
        import io
        import pypdf
        from app.voucher_service import build_vouchers_pdf_bytes

        pdf = build_vouchers_pdf_bytes([voucher_id], data, base_dir=PROJECT_ROOT)
        reader = pypdf.PdfReader(io.BytesIO(pdf))
        return reader.pages[0].extract_text()

    def test_olap_request_contains_customer_order_no(self) -> None:
        """1. OLAPリクエスト(送信ペイロード)に「客先注文No_10桁」が含まれること。"""
        from app.voucher_olap_service import _build_voucher_payload

        payload, _ = _build_voucher_payload("5218869")
        names = {
            str(c.get("フィールド論理名"))
            for c in payload.get("R1List", [])
            if isinstance(c, dict)
        }
        self.assertIn("客先注文No_10桁", names)

    def test_old_template_supplemented_before_send(self) -> None:
        """2. 古いテンプレートに項目が無い場合、送信直前に補完されること。"""
        from app.voucher_olap_service import _ensure_customer_order_no_column

        payload = {
            "OLAP対象データ": "OLAP_T01-03 受注入力明細データ",
            "R1List": [
                {"OLAP表示No": 6, "フィールド論理名": "受注No",
                 "エンティティ論理名": "OLAP_T01-03 受注入力明細データ"},
            ],
        }
        _ensure_customer_order_no_column(payload)
        names = [c.get("フィールド論理名") for c in payload["R1List"]]
        self.assertIn("客先注文No_10桁", names)
        # 既存項目を壊さず末尾に1件だけ追加していること。
        self.assertEqual(names.count("客先注文No_10桁"), 1)
        added = payload["R1List"][-1]
        self.assertEqual(added["OLAP表示No"], 7)
        self.assertEqual(added["エンティティ論理名"], "OLAP_T01-03 受注入力明細データ")

    def test_supplement_is_idempotent(self) -> None:
        """補完処理は既に項目があれば二重追加しないこと。"""
        from app.voucher_olap_service import _ensure_customer_order_no_column

        payload = {
            "R1List": [
                {"OLAP表示No": 6, "フィールド論理名": "受注No"},
                {"OLAP表示No": 45, "フィールド論理名": "客先注文No_10桁"},
            ],
        }
        _ensure_customer_order_no_column(payload)
        names = [c.get("フィールド論理名") for c in payload["R1List"]]
        self.assertEqual(names.count("客先注文No_10桁"), 1)

    def test_mapper_keeps_customer_order_no(self) -> None:
        """3. voucher_data_mapper でPDF用データに保持されること。"""
        from app.voucher_data_mapper import build_voucher_pages

        rows = [{
            "order_no": "5218869",
            "customer_order_no_10": "ABCD123456",
            "product_name": "品名",
        }]
        pages = build_voucher_pages(rows)
        self.assertEqual(pages[0]["customer_order_no_10"], "ABCD123456")

    def test_pdf_shows_customer_order_no(self) -> None:
        """4. 値がある場合「お客様注文No. xxxx」が表示されること。"""
        data = {"pages": [{
            "order_no": "5218869",
            "customer_order_no_10": "ABCD123456",
            "details": [{"name": "品名"}],
        }]}
        text = self._page_text("01", data)
        self.assertIn("お客様注文No. ABCD123456", text)

    def test_pdf_hides_when_blank(self) -> None:
        """5. 空欄/空白のみの場合は何も表示されないこと。"""
        for blank in ("", "   ", "　", None):
            data = {"pages": [{
                "order_no": "5218869",
                "customer_order_no_10": blank,
                "details": [{"name": "品名"}],
            }]}
            text = self._page_text("01", data)
            self.assertNotIn("お客様注文No", text)

    def test_pdf_has_no_tax_notice(self) -> None:
        """6. 消費税固定文言がPDFに出力されないこと。"""
        data = {"pages": [{
            "order_no": "5218869",
            "customer_order_no_10": "ABCD123456",
            "details": [{"name": "品名"}],
        }]}
        for vid in self._ALL_VOUCHER_IDS:
            text = self._page_text(vid, data)
            self.assertNotIn("消費税", text, f"伝票{vid}に消費税文言が残っている")
            self.assertNotIn("本伝票には", text, f"伝票{vid}に消費税文言が残っている")

    def test_all_vouchers_same_spec(self) -> None:
        """7. 01〜08すべての伝票で同じ仕様（表示あり/空欄非表示）になること。"""
        with_value = {"pages": [{
            "order_no": "5218869",
            "customer_order_no_10": "ABCD123456",
            "details": [{"name": "品名"}],
        }]}
        without_value = {"pages": [{
            "order_no": "5218869",
            "customer_order_no_10": "",
            "details": [{"name": "品名"}],
        }]}
        for vid in self._ALL_VOUCHER_IDS:
            self.assertIn("お客様注文No. ABCD123456", self._page_text(vid, with_value),
                          f"伝票{vid}でお客様注文Noが表示されていない")
            self.assertNotIn("お客様注文No", self._page_text(vid, without_value),
                             f"伝票{vid}で空欄時に表示されている")

    def test_font_size_is_1_2x(self) -> None:
        """8. 表示文字サイズが従来(7.5pt)比1.2倍であること。"""
        from app.voucher_templates import (
            CUSTOMER_ORDER_NO_FONT_SIZE,
            CUSTOMER_ORDER_NO_BASE_FONT_SIZE,
        )
        self.assertEqual(CUSTOMER_ORDER_NO_BASE_FONT_SIZE, 7.5)
        self.assertAlmostEqual(
            CUSTOMER_ORDER_NO_FONT_SIZE, 7.5 * 1.2, places=6
        )

    def test_mapper_resolves_customer_order_no_from_response_key(self) -> None:
        """OLAPレスポンスのキー45から customer_order_no_10 を取得できること。

        現行レイアウト(OP列36-44あり)・非現行レイアウト(OP列無し)の双方で、
        表示No=45 に入った客先注文Noが page data に保持されることを確認する。
        商品名称の先頭スペース保持修正後も他項目の取得が壊れないことの回帰テスト。
        """
        from app.voucher_data_mapper import extract_r1_rows, build_voucher_pages

        for label, extra_keys in (("current", {"40": "02"}), ("legacy", {})):
            row = {str(i): f"v{i}" for i in (1, 5, 6, 7, 8, 9, 16)}
            row.update(extra_keys)
            row["45"] = "ABCD123456"
            rows = extract_r1_rows({"ResponseData": {"R1List": [row]}})
            self.assertEqual(
                rows[0].get("customer_order_no_10"), "ABCD123456",
                f"{label}: extract後にcustomer_order_no_10が消えている",
            )
            pages = build_voucher_pages(rows)
            self.assertEqual(
                pages[0]["customer_order_no_10"], "ABCD123456",
                f"{label}: build_voucher_pages後に保持されていない",
            )

    def test_mapper_response_path_keeps_product_name_spaces(self) -> None:
        """商品名称の前後空白保持と customer_order_no_10 取得が両立すること。"""
        from app.voucher_data_mapper import extract_r1_rows, build_voucher_pages

        row = {str(i): f"v{i}" for i in (1, 5, 6, 7, 8, 9, 40)}
        row["16"] = "  品名スペース  "
        row["45"] = "X1"
        pages = build_voucher_pages(extract_r1_rows({"ResponseData": {"R1List": [row]}}))
        self.assertEqual(pages[0]["details"][0]["name"], "  品名スペース  ")
        self.assertEqual(pages[0]["customer_order_no_10"], "X1")

    def test_bundled_templates_contain_field(self) -> None:
        """同梱OLAPテンプレートに項目が反映されていること。"""
        import json

        for rel in (
            "templates/voucher_olap_request.json",
            "docs/olap/kakou_request_template.json",
        ):
            payload = json.loads((PROJECT_ROOT / rel).read_text(encoding="utf-8-sig"))
            names = {
                str(c.get("フィールド論理名"))
                for c in payload.get("R1List", [])
                if isinstance(c, dict)
            }
            self.assertIn("客先注文No_10桁", names, f"{rel} に項目が無い")


class TestHeaderIGap(unittest.TestCase):
    """コードNo/伝票No/受注No内の各Iの直後を広げる字間補正のテスト。"""

    def _gap(self) -> float:
        from app import voucher_service
        return voucher_service.HEADER_I_CHAR_GAP_PT

    def _canvas(self):
        import io
        from reportlab.pdfgen import canvas as rl_canvas
        from app import voucher_service

        voucher_service._ensure_font()
        return rl_canvas.Canvas(
            io.BytesIO(), pagesize=(voucher_service.PAGE_W, voucher_service.PAGE_H)
        )

    def _collect_emits(self, draw_fn_name: str, data: dict) -> list[tuple[str, float]]:
        """実描画関数を実行し、_emit_text に渡った (text, x) を順に集める。

        _emit_text を捕捉するため、太字の多重描画に依存せず、1描画=1エントリで
        文字列と描画開始Xが取れる。stringWidth 計算のため実キャンバスを使う。
        """
        from app import voucher_service

        emits: list[tuple[str, float]] = []

        def fake_emit(c, method, x, y, text, bold):
            emits.append((text, x))

        c = self._canvas()
        with patch.object(voucher_service, "_emit_text", side_effect=fake_emit):
            getattr(voucher_service, draw_fn_name)(c, data)
        return emits

    def _find_x(self, emits: list[tuple[str, float]], text: str) -> float:
        for t, x in emits:
            if t == text:
                return x
        raise AssertionError(f"{text!r} が描画されていない: {emits}")

    def _str_width(self, text: str, fs: float) -> float:
        from app import voucher_service
        c = self._canvas()
        base = voucher_service._resolve_base_font(voucher_service.DATA_BOLD_FONT_NAME)
        return c.stringWidth(text, base, fs)

    # ── helper 単体（分割描画されるか）──────────────────────────────
    def _helper_emits(self, value, fs: float = 10.0, x: float = 100.0):
        from app import voucher_service
        emits: list[tuple[str, float]] = []

        def fake_emit(c, method, xx, y, text, bold):
            emits.append((text, xx))

        c = self._canvas()
        with patch.object(voucher_service, "_emit_text", side_effect=fake_emit):
            voucher_service._str_header_value(c, value, x, 200.0, fs)
        return emits

    def test_gap_value_is_4pt(self) -> None:
        self.assertEqual(self._gap(), 4.0)

    def test_i_value_has_gap_after_i(self) -> None:
        emits = self._helper_emits("I40186", fs=10.0, x=100.0)
        self.assertEqual([t for t, _ in emits], ["I", "40186"])
        self.assertAlmostEqual(emits[0][1], 100.0)
        self.assertAlmostEqual(
            emits[1][1], 100.0 + self._str_width("I", 10.0) + self._gap())

    def test_middle_i_has_gap_only_after_i(self) -> None:
        emits = self._helper_emits("Y99I111", fs=10.0, x=100.0)
        self.assertEqual([t for t, _ in emits], ["Y99I", "111"])
        self.assertAlmostEqual(
            emits[1][1], emits[0][1] + self._str_width("Y99I", 10.0) + self._gap())

    def test_two_i_values_accumulate_two_gaps(self) -> None:
        emits = self._helper_emits("II123", fs=10.0, x=100.0)
        self.assertEqual([t for t, _ in emits], ["I", "I", "123"])
        self.assertAlmostEqual(
            emits[1][1], emits[0][1] + self._str_width("I", 10.0) + self._gap())
        self.assertAlmostEqual(
            emits[2][1], emits[1][1] + self._str_width("I", 10.0) + self._gap())

    def test_abc_has_normal_spacing(self) -> None:
        emits = self._helper_emits("ABC", fs=10.0, x=100.0)
        self.assertEqual(emits, [("ABC", 100.0)])

    def test_fullwidth_i_has_gap(self) -> None:
        emits = self._helper_emits("Ｉ40186")
        self.assertEqual([t for t, _ in emits], ["Ｉ", "40186"])
        self.assertAlmostEqual(
            emits[1][1], emits[0][1] + self._str_width("Ｉ", 10.0) + self._gap())

    def test_lowercase_i_is_not_adjusted(self) -> None:
        emits = self._helper_emits("i40186")
        self.assertEqual([t for t, _ in emits], ["i40186"])

    def test_none_and_empty_no_draw(self) -> None:
        self.assertEqual(self._helper_emits(None), [])
        self.assertEqual(self._helper_emits(""), [])

    def test_single_char_i_single_draw(self) -> None:
        # 1文字だけの "I" は分割しない（従来通り1回）。
        emits = self._helper_emits("I", x=100.0)
        self.assertEqual(len(emits), 1)
        self.assertEqual(emits[0], ("I", 100.0))

    def test_terminal_i_has_no_gap(self) -> None:
        self.assertEqual(self._helper_emits("123I"), [("123I", 100.0)])

    def test_requested_value_patterns(self) -> None:
        adjusted = ("I123456", "AI12345", "12I3456", "II12345", "Ｉ123456")
        for value in adjusted:
            with self.subTest(value=value):
                self.assertGreater(len(self._helper_emits(value)), 1)
        for value in ("1234567", "I", "123I"):
            with self.subTest(value=value):
                self.assertEqual(len(self._helper_emits(value)), 1)

    def test_width_includes_exact_gap_count(self) -> None:
        from app import voucher_service

        c = self._canvas()
        font = voucher_service.DATA_BOLD_FONT_NAME
        fs = 10.0
        plain = c.stringWidth(
            "II123", voucher_service._resolve_base_font(font), fs
        )
        measured = voucher_service.i_spaced_text_width(
            c, "II123", font, fs
        )
        self.assertAlmostEqual(measured, plain + self._gap() * 2)

    def test_right_and_center_alignment_use_adjusted_total_width(self) -> None:
        from app import voucher_service

        c = self._canvas()
        font = voucher_service.DATA_BOLD_FONT_NAME
        fs = 10.0
        total = voucher_service.i_spaced_text_width(c, "AI12", font, fs)

        def collect(align, anchor):
            emits = []
            with patch.object(
                voucher_service, "_emit_text",
                side_effect=lambda _c, _m, x, _y, text, _b:
                emits.append((text, x)),
            ):
                voucher_service.draw_text_with_i_gap(
                    c, "AI12", anchor, 100.0, fs,
                    font_name=font, align=align,
                )
            return emits

        right = collect("right", 300.0)
        center = collect("center", 300.0)
        self.assertAlmostEqual(right[0][1], 300.0 - total)
        self.assertAlmostEqual(center[0][1], 300.0 - total / 2.0)

    def test_no_i_right_alignment_keeps_legacy_anchor_and_size(self) -> None:
        from app import voucher_service

        c = self._canvas()
        emits = []
        with patch.object(
            voucher_service, "_emit_text",
            side_effect=lambda _c, method, x, _y, text, _bold:
            emits.append((method, x, text)),
        ):
            used = voucher_service.draw_text_with_i_gap(
                c, "1234567", 300.0, 100.0, 10.0,
                max_width=100.0, min_font_size=5.0, align="right",
            )
        self.assertEqual(emits, [("drawRightString", 300.0, "1234567")])
        self.assertEqual(used, 10.0)

    def test_fit_width_uses_gap_in_font_reduction(self) -> None:
        from app import voucher_service

        c = self._canvas()
        used = voucher_service.draw_text_with_i_gap(
            c, "II123456789", 100.0, 100.0, 12.0,
            max_width=40.0, min_font_size=5.0,
            align="right",
        )
        self.assertLess(used, 12.0)
        self.assertLessEqual(
            voucher_service.i_spaced_text_width(
                c, "II123456789",
                voucher_service.DATA_BOLD_FONT_NAME, used,
            ),
            40.0 + 1e-6,
        )

    def test_central_right_order_and_voucher_numbers_use_common_drawer(self) -> None:
        from app import voucher_service

        c = self._canvas()
        calls = []
        with patch.object(
            voucher_service, "draw_text_with_i_gap",
            wraps=voucher_service.draw_text_with_i_gap,
        ) as draw:
            voucher_service._draw_form_data_01(
                c, {"order_no": "I123456", "voucher_no": "AI12345"}
            )
            calls = [
                call for call in draw.call_args_list
                if call.kwargs.get("draw_path") == "_draw_form_data_01_central"
            ]
        self.assertEqual([call.kwargs["field"] for call in calls],
                         ["order_no", "voucher_no"])
        self.assertTrue(all(call.kwargs["align"] == "right" for call in calls))

    # ── 実描画経路（指図書(1)=shizu / overlay など）───────────────
    def _assert_first_i_unchanged_rest_shifted(self, draw_fn_name, field, i_value):
        digit_value = i_value.replace("I", "1")  # 例: I40186 -> 140186
        base_emits = self._collect_emits(draw_fn_name, {field: digit_value})
        base_x = self._find_x(base_emits, digit_value)

        i_emits = self._collect_emits(draw_fn_name, {field: i_value})
        first_x = self._find_x(i_emits, "I")
        rest_x = i_emits[i_emits.index(("I", first_x)) + 1][1]
        # 先頭Iの開始位置は通常値の基準Xと同一（値全体は動かない）
        self.assertAlmostEqual(first_x, base_x)
        # 2文字目以降は I幅 + gap 分だけ右
        self.assertGreater(rest_x, first_x)

    def test_shizu_order_no_has_gap(self) -> None:
        self._assert_first_i_unchanged_rest_shifted(
            "_draw_form_data_shizu", "order_no", "I140999")

    def test_shizu_voucher_no_has_gap(self) -> None:
        self._assert_first_i_unchanged_rest_shifted(
            "_draw_form_data_shizu", "voucher_no", "I640548")

    def test_shizu_rest_x_equals_i_width_plus_gap(self) -> None:
        # 指図書(1) の受注Noで、次文字X - IのX == stringWidth("I") + gap。
        emits = self._collect_emits("_draw_form_data_shizu", {"order_no": "I140999"})
        first_x = self._find_x(emits, "I")
        rest_x = emits[emits.index(("I", first_x)) + 1][1]
        fs = self._shizu_order_fs()
        self.assertAlmostEqual(
            rest_x - first_x, self._str_width("I", fs) + self._gap())

    def _shizu_order_fs(self) -> float:
        from app import voucher_service
        return voucher_service.HEADER_MAIN_VALUE_FONT_SIZE

    def test_shizu_code_no_middle_i_has_gap(self) -> None:
        emits = self._collect_emits(
            "_draw_form_data_shizu", {"code_no": "Y99I111"})
        start = next(i for i in range(len(emits) - 1)
                     if emits[i][0] == "Y99I" and emits[i + 1][0] == "111")
        fs = self._shizu_order_fs()
        self.assertAlmostEqual(
            emits[start + 1][1] - emits[start][1],
            self._str_width("Y99I", fs) + self._gap())

    def test_overlay_order_no_has_gap(self) -> None:
        self._assert_first_i_unchanged_rest_shifted(
            "_draw_header_overlay", "order_no", "I140999")

    def test_overlay_voucher_no_has_gap(self) -> None:
        self._assert_first_i_unchanged_rest_shifted(
            "_draw_header_overlay", "voucher_no", "I640548")

    def test_form01_and_delivery_order_no_has_gap(self) -> None:
        self._assert_first_i_unchanged_rest_shifted(
            "_draw_form_data_01", "order_no", "I140999")
        self._assert_first_i_unchanged_rest_shifted(
            "_draw_delivery_data_common", "order_no", "I140999")

    def test_all_forms_generate_successfully(self) -> None:
        """01〜08のPDFが受注No「I」始まりでも生成できること。"""
        import tempfile
        from app.voucher_service import create_vouchers_pdf

        with tempfile.TemporaryDirectory() as tmpdir:
            for vid in ("01", "02", "03", "04", "05", "06", "07", "08"):
                out = create_vouchers_pdf(
                    [vid], output_dir=Path(tmpdir), base_dir=PROJECT_ROOT)
                self.assertTrue(out.exists(), f"{vid} が生成されない")
                self.assertGreater(out.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
