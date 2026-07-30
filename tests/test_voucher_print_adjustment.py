"""印刷位置・余白補正（SumatraPDF印刷用の印刷補正PDF）のテスト。

元PDFを変更せず、印刷時だけ補正PDFを作り SumatraPDF に渡す挙動を検証する。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


def _make_source_pdf(path: Path, *, pages: int = 1, width: float = 728.5, height: float = 515.9) -> None:
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=(width, height))
    for i in range(pages):
        c.drawString(100, 100, f"page {i}")
        c.showPage()
    c.save()


def _first_page_ctm(pdf_path: Path):
    """補正PDF先頭ページの内容ストリーム先頭の cm 行列 (a,b,c,d,e,f) を返す。"""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    data = reader.pages[0].get_contents().get_data()
    # 最初の "cm" オペレーターの直前6数値を取り出す。
    text = data.decode("latin-1")
    idx = text.index(" cm")
    head = text[:idx].strip().splitlines()[-1]
    return tuple(float(x) for x in head.split())


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestCreateAdjustedPrintPdf(unittest.TestCase):
    def test_source_pdf_is_not_modified(self) -> None:
        # 13. PDF保存・プレビューのPDFは変わらない（元PDFは不変）。
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "src.pdf"
            _make_source_pdf(src)
            before = src.read_bytes()
            out = Path(temp_dir) / "out.pdf"
            voucher_print_service.create_adjusted_print_pdf(
                src, out, 1.0, 1.0, 1.0, 1.0, 99.0, 99.0, 1.0, 1.0
            )
            self.assertEqual(src.read_bytes(), before)

    def test_page_size_is_maintained(self) -> None:
        # 4. 補正済みPDFのページサイズは元PDFと同じ。
        from pypdf import PdfReader

        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "src.pdf"
            _make_source_pdf(src)
            out = Path(temp_dir) / "out.pdf"
            voucher_print_service.create_adjusted_print_pdf(
                src, out, 2.0, 0.0, 0.0, 0.0, 99.0, 101.0, 1.0, -1.0
            )
            sbox = PdfReader(str(src)).pages[0].mediabox
            obox = PdfReader(str(out)).pages[0].mediabox
            self.assertAlmostEqual(float(sbox.width), float(obox.width), places=2)
            self.assertAlmostEqual(float(sbox.height), float(obox.height), places=2)

    def test_scale_percent_is_reflected(self) -> None:
        # 5. 横倍率/縦倍率が補正PDFに反映される（余白0なら倍率がそのまま行列に出る）。
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "src.pdf"
            _make_source_pdf(src)
            out = Path(temp_dir) / "out.pdf"
            voucher_print_service.create_adjusted_print_pdf(
                src, out, 0.0, 0.0, 0.0, 0.0, 99.0, 101.0, 0.0, 0.0
            )
            a, b, c, d, e, f = _first_page_ctm(out)
            self.assertAlmostEqual(a, 0.99, places=4)
            self.assertAlmostEqual(d, 1.01, places=4)

    def test_offset_mm_is_reflected(self) -> None:
        # 6. 横位置/縦位置が補正PDFに反映される（正の縦位置は下へ = y 減少）。
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "src.pdf"
            _make_source_pdf(src)
            out = Path(temp_dir) / "out.pdf"
            voucher_print_service.create_adjusted_print_pdf(
                src, out, 0.0, 0.0, 0.0, 0.0, 100.0, 100.0, 1.0, 1.0
            )
            a, b, c, d, e, f = _first_page_ctm(out)
            mm = 72.0 / 25.4
            self.assertAlmostEqual(e, 1.0 * mm, places=3)
            self.assertAlmostEqual(f, -1.0 * mm, places=3)

    def test_margin_mm_is_reflected(self) -> None:
        # 7. 余白補正が補正PDFに反映される（左余白は内容を右へ・幅を縮める）。
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "src.pdf"
            _make_source_pdf(src, width=728.5, height=515.9)
            out = Path(temp_dir) / "out.pdf"
            voucher_print_service.create_adjusted_print_pdf(
                src, out, 10.0, 0.0, 0.0, 0.0, 100.0, 100.0, 0.0, 0.0
            )
            a, b, c, d, e, f = _first_page_ctm(out)
            mm = 72.0 / 25.4
            # 左余白 10mm 分、幅が縮み内容が右へ寄る。
            self.assertLess(a, 1.0)
            self.assertAlmostEqual(e, 10.0 * mm, places=2)

    def test_all_pages_are_adjusted(self) -> None:
        # 8. 複数ページPDFでも全ページ補正される。
        from pypdf import PdfReader

        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "src.pdf"
            _make_source_pdf(src, pages=3)
            out = Path(temp_dir) / "out.pdf"
            voucher_print_service.create_adjusted_print_pdf(
                src, out, 0.0, 0.0, 0.0, 0.0, 99.0, 99.0, 0.0, 0.0
            )
            reader = PdfReader(str(out))
            self.assertEqual(len(reader.pages), 3)
            for page in reader.pages:
                self.assertIn(b" cm", page.get_contents().get_data())

    def test_failure_raises(self) -> None:
        # 10（下地）: 入力が壊れていれば例外を送出する。
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "broken.pdf"
            src.write_bytes(b"not a pdf")
            out = Path(temp_dir) / "out.pdf"
            with self.assertRaises(Exception):
                voucher_print_service.create_adjusted_print_pdf(
                    src, out, 0.0, 0.0, 0.0, 0.0, 100.0, 100.0, 0.0, 0.0
                )


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestAdjustmentSettingsPersistence(unittest.TestCase):
    def test_settings_save_and_restore(self) -> None:
        # 1. 印刷補正設定を保存・復元できる。
        from PySide6.QtCore import QSettings

        from app import voucher_settings
        from app.voucher_settings import VoucherPrinterSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "settings.ini")
            values = VoucherPrinterSettings(
                printer_name="Printer A",
                print_adjustment_enabled=True,
                print_adjustment_margin_left_mm=1.5,
                print_adjustment_margin_right_mm=2.0,
                print_adjustment_margin_top_mm=0.5,
                print_adjustment_margin_bottom_mm=0.25,
                print_adjustment_scale_x_percent=100.5,
                print_adjustment_scale_y_percent=99.5,
                print_adjustment_offset_x_mm=1.0,
                print_adjustment_offset_y_mm=-1.0,
                print_adjustment_save_pdf=True,
            )
            voucher_settings.save_voucher_printer_settings(
                values, QSettings(path, QSettings.Format.IniFormat)
            )
            loaded = voucher_settings.load_voucher_printer_settings(
                QSettings(path, QSettings.Format.IniFormat)
            )
        self.assertTrue(loaded.print_adjustment_enabled)
        self.assertAlmostEqual(loaded.print_adjustment_margin_left_mm, 1.5)
        self.assertAlmostEqual(loaded.print_adjustment_margin_right_mm, 2.0)
        self.assertAlmostEqual(loaded.print_adjustment_scale_x_percent, 100.5)
        self.assertAlmostEqual(loaded.print_adjustment_scale_y_percent, 99.5)
        self.assertAlmostEqual(loaded.print_adjustment_offset_x_mm, 1.0)
        self.assertAlmostEqual(loaded.print_adjustment_offset_y_mm, -1.0)
        self.assertTrue(loaded.print_adjustment_save_pdf)

    def test_settings_are_clamped_to_range(self) -> None:
        from app import voucher_settings

        self.assertEqual(voucher_settings.normalize_adjustment_margin_mm(999), 20.0)
        self.assertEqual(voucher_settings.normalize_adjustment_margin_mm(-999), -20.0)
        self.assertEqual(voucher_settings.normalize_adjustment_offset_mm(50), 20.0)
        self.assertEqual(voucher_settings.normalize_adjustment_scale_percent(80), 95.0)
        self.assertEqual(voucher_settings.normalize_adjustment_scale_percent(200), 105.0)

    def test_profile_carries_adjustment(self) -> None:
        # 6（要件6）: プロファイルにも補正設定が含まれる。
        from PySide6.QtCore import QSettings

        from app import voucher_settings
        from app.voucher_settings import SumatraPrintProfile

        with tempfile.TemporaryDirectory() as temp_dir:
            store = QSettings(str(Path(temp_dir) / "s.ini"), QSettings.Format.IniFormat)
            profile = SumatraPrintProfile(
                profile_name="補正あり",
                print_settings="noscale,monochrome,paper=auto,bin=auto,center",
                adjustment_enabled=True,
                margin_left_mm=1.0,
                scale_y_percent=99.0,
                offset_x_mm=0.5,
            )
            voucher_settings.save_sumatra_print_profiles([profile], store)
            loaded = voucher_settings.load_sumatra_print_profiles(
                QSettings(str(Path(temp_dir) / "s.ini"), QSettings.Format.IniFormat)
            )
        found = [p for p in loaded if p.profile_name == "補正あり"]
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].adjustment_enabled)
        self.assertAlmostEqual(found[0].margin_left_mm, 1.0)
        self.assertAlmostEqual(found[0].scale_y_percent, 99.0)
        self.assertAlmostEqual(found[0].offset_x_mm, 0.5)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestSumatraAdjustmentIntegration(unittest.TestCase):
    def _settings(self, *, sumatra_path: str, enabled: bool, save_pdf: bool = False):
        from app.voucher_settings import DEFAULT_SUMATRA_PRINT_SETTINGS, VoucherPrinterSettings

        return VoucherPrinterSettings(
            printer_name="Printer A",
            paper_size="B5",
            print_backend="sumatra",
            sumatra_path=sumatra_path,
            sumatra_print_settings=DEFAULT_SUMATRA_PRINT_SETTINGS,
            print_adjustment_enabled=enabled,
            print_adjustment_margin_left_mm=1.0,
            print_adjustment_margin_right_mm=1.0,
            print_adjustment_scale_x_percent=100.5,
            print_adjustment_scale_y_percent=99.5,
            print_adjustment_offset_x_mm=0.5,
            print_adjustment_offset_y_mm=0.5,
            print_adjustment_save_pdf=save_pdf,
        )

    def _popen_success(self, popen, exit_code: int = 0):
        popen.return_value.pid = 4321
        popen.return_value.communicate.return_value = (b"ok", b"")
        popen.return_value.returncode = exit_code
        popen.return_value.poll.return_value = None

    def _pdf_bytes(self) -> bytes:
        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "s.pdf"
            _make_source_pdf(src)
            return src.read_bytes()

    def test_adjustment_off_sends_original_pdf(self) -> None:
        # 2. 補正OFFなら元PDF（print_jobs）が SumatraPDF へ渡される。
        from app import voucher_print_service

        pdf_bytes = self._pdf_bytes()
        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path=str(sumatra), enabled=False)
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                self._popen_success(popen)
                voucher_print_service._print_pdf_with_sumatra(pdf_bytes, settings, job_name="job")
            command = popen.call_args.args[0]
            printed = Path(command[-1])
            self.assertIn("print_jobs", printed.parts)
            self.assertNotIn("print_adjusted", printed.parts)

    def test_adjustment_on_creates_and_sends_adjusted_pdf(self) -> None:
        # 3. 補正ONなら補正済みPDFが作成され、そのPDFが SumatraPDF へ渡される。
        from app import voucher_print_service

        pdf_bytes = self._pdf_bytes()
        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path=str(sumatra), enabled=True)
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                self._popen_success(popen)
                voucher_print_service._print_pdf_with_sumatra(pdf_bytes, settings, job_name="job")
            command = popen.call_args.args[0]
            printed = Path(command[-1])
            self.assertIn("print_adjusted", printed.parts)
            self.assertTrue(printed.is_file())

    def test_adjustment_failure_does_not_print_original(self) -> None:
        # 10. 補正失敗時は元PDFを勝手に印刷しない。
        from app import voucher_print_service

        pdf_bytes = self._pdf_bytes()
        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path=str(sumatra), enabled=True)
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(
                        voucher_print_service, "create_adjusted_print_pdf",
                        side_effect=RuntimeError("boom"),
                    ), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                self._popen_success(popen)
                with self.assertRaisesRegex(RuntimeError, "印刷補正PDF"):
                    voucher_print_service._print_pdf_with_sumatra(pdf_bytes, settings, job_name="job")
            popen.assert_not_called()

    def test_log_contains_adjustment_and_actual_pdf(self) -> None:
        # 11. ログに補正値と実際にSumatraPDFへ渡したPDFが出る。
        from app import voucher_print_service

        pdf_bytes = self._pdf_bytes()
        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            debug_dir = Path(temp_dir) / "debug"
            settings = self._settings(sumatra_path=str(sumatra), enabled=True)
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "get_order_capture_debug_dir", return_value=debug_dir), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                self._popen_success(popen)
                voucher_print_service._print_pdf_with_sumatra(pdf_bytes, settings, job_name="job")
            log_files = list(debug_dir.glob("voucher_print_*.jsonl"))
            payload = json.loads(log_files[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertTrue(payload["print_adjustment_enabled"])
        self.assertTrue(payload["print_adjustment_pdf_created"])
        self.assertIn("print_adjusted", payload["sumatra_pdf_path_actual"])
        self.assertIn("print_adjusted", payload["print_adjustment_output_pdf"])
        self.assertTrue(payload["print_adjustment_output_pdf_exists"])
        self.assertAlmostEqual(payload["print_adjustment_scale_y_percent"], 99.5)

    def test_test_print_applies_adjustment(self) -> None:
        # 9. テスト印刷（source_type="test"）にも補正が反映される。
        from app import voucher_print_service

        pdf_bytes = self._pdf_bytes()
        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            debug_dir = Path(temp_dir) / "debug"
            settings = self._settings(sumatra_path=str(sumatra), enabled=True)
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "get_order_capture_debug_dir", return_value=debug_dir), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                self._popen_success(popen)
                voucher_print_service._print_pdf_with_sumatra(
                    pdf_bytes,
                    settings,
                    job_name="test",
                    print_metadata={"source_type": "test", "test_print_requested": True},
                )
            command = popen.call_args.args[0]
            self.assertIn("print_adjusted", Path(command[-1]).parts)
            payload = json.loads(
                list(debug_dir.glob("voucher_print_*.jsonl"))[0]
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
        self.assertTrue(payload["test_print_requested"])
        self.assertTrue(payload["print_adjustment_pdf_created"])

    def test_acrobat_backend_does_not_apply_adjustment(self) -> None:
        # 12. Acrobat Reader経由には補正が勝手に適用されない。
        from app import voucher_print_service

        pdf_bytes = self._pdf_bytes()
        with tempfile.TemporaryDirectory() as temp_dir:
            acrobat = Path(temp_dir) / "AcroRd32.exe"
            acrobat.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path="", enabled=True)
            settings = settings.__class__(**{**settings.__dict__, "acrobat_path": str(acrobat)})
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "get_order_capture_debug_dir", return_value=Path(temp_dir) / "debug"), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service, "create_adjusted_print_pdf") as create_adj, \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                self._popen_success(popen)
                voucher_print_service._print_pdf_with_acrobat(pdf_bytes, settings, job_name="job")
            create_adj.assert_not_called()


if __name__ == "__main__":
    unittest.main()
