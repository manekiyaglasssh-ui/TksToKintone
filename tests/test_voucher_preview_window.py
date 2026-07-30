"""VoucherPrintPreviewWindow（アプリ内印刷プレビュー）のテスト。

PDFバイト列を受け取って表示し、印刷ボタンで即時印刷処理を呼ぶことを検証する。
プレビューはファイル保存を一切行わない。
"""
from __future__ import annotations

import os
import hashlib
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False


def _make_pdf_bytes() -> bytes:
    """レンダリング可能な1ページPDFをメモリ上で生成する。"""
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 100, "preview")
    c.showPage()
    c.save()
    return buf.getvalue()


_MINIMAL_PDF = _make_pdf_bytes()


class _FakeSignal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in list(self.callbacks):
            callback(*args)


class _FakePrintWorker:
    def __init__(self) -> None:
        self.status_changed = _FakeSignal()
        self.request_sent = _FakeSignal()
        self.finished = _FakeSignal()
        self.error = _FakeSignal()

    def cancel(self) -> None:
        pass


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherPreviewWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self, pdf_bytes: bytes = _MINIMAL_PDF):
        from app.voucher_preview_window import VoucherPrintPreviewWindow

        win = VoucherPrintPreviewWindow(pdf_bytes)
        self.addCleanup(win.deleteLater)
        return win

    def test_window_stores_pdf_bytes(self) -> None:
        win = self._make_window(_MINIMAL_PDF)
        self.assertEqual(win.pdf_bytes, _MINIMAL_PDF)

    def test_preview_log_uses_exact_pdf_hash_and_reports_cache_bypass(self) -> None:
        from app.voucher_preview_window import VoucherPrintPreviewWindow

        expected_hash = hashlib.sha256(_MINIMAL_PDF).hexdigest()
        with self.assertLogs("app.voucher_preview_window", level="INFO") as captured:
            win = VoucherPrintPreviewWindow(
                _MINIMAL_PDF, edit_render_trace_id="trace-preview",
                edit_objects_sha256="c" * 64, preview_cache_hit=False)
        self.addCleanup(win.deleteLater)
        logs = "\n".join(captured.output)
        self.assertEqual(win.pdf_sha256, expected_hash)
        self.assertIn(
            f"event=voucher_preview_png_rasterize trace_id=trace-preview "
            f"pdf_sha256={expected_hash}", logs)
        self.assertIn(
            f"event=voucher_preview_pixmap_shown trace_id=trace-preview "
            f"pdf_sha256={expected_hash}", logs)
        self.assertIn("cache_hit=False", logs)

    def test_window_has_print_and_close_buttons(self) -> None:
        win = self._make_window()
        self.assertEqual(win.print_button.text(), "印刷")
        self.assertEqual(win.close_button.text(), "閉じる")

    def test_print_button_invokes_print_service_with_bytes(self) -> None:
        win = self._make_window(_MINIMAL_PDF)
        worker = _FakePrintWorker()
        with mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker) as pr, \
                mock.patch("app.voucher_preview_window.QMessageBox.information"):
            win.print_button.click()
        pr.assert_called_once()
        self.assertEqual(pr.call_args.args[0], _MINIMAL_PDF)

    def test_print_button_uses_background_worker_for_sumatra_backend(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window(_MINIMAL_PDF)
        worker = _FakePrintWorker()
        settings = VoucherPrinterSettings(printer_name="Printer A", print_backend="sumatra")
        with mock.patch(
            "app.voucher_settings.load_voucher_printer_settings", return_value=settings
        ), mock.patch(
            "app.voucher_print_service.start_print_pdf_background", return_value=worker
        ) as pr, mock.patch(
            "app.voucher_print_service.print_pdf_direct"
        ) as direct:
            win.print_button.click()
        pr.assert_called_once()
        direct.assert_not_called()

    def test_request_sent_updates_status_label_without_messagebox(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window(_MINIMAL_PDF)
        worker = _FakePrintWorker()
        settings = VoucherPrinterSettings(printer_name="Printer A", print_backend="acrobat")
        with mock.patch("app.voucher_settings.load_voucher_printer_settings", return_value=settings), \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker), \
                mock.patch("app.voucher_preview_window.QMessageBox") as messagebox:
            win.print_button.click()
            self.assertTrue(win.print_button.isEnabled())
            worker.request_sent.emit({})
        self.assertEqual(win.status_label.text(), "Acrobat Readerへ印刷要求を送信しました")
        self.assertTrue(win.print_button.isEnabled())
        messagebox.information.assert_not_called()
        messagebox.critical.assert_not_called()

    def test_print_click_disables_button_and_sets_status(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window(_MINIMAL_PDF)
        worker = _FakePrintWorker()
        settings = VoucherPrinterSettings(printer_name="Printer A", print_backend="acrobat")
        with mock.patch("app.voucher_settings.load_voucher_printer_settings", return_value=settings), \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker):
            win.print_button.click()
        self.assertTrue(win.print_button.isEnabled())
        self.assertEqual(win.status_label.text(), "Acrobat Reader印刷ジョブを登録しました")

    def test_print_error_shows_status_and_reenables_button(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window(_MINIMAL_PDF)
        worker = _FakePrintWorker()
        settings = VoucherPrinterSettings(printer_name="Printer A", print_backend="acrobat")
        with mock.patch("app.voucher_settings.load_voucher_printer_settings", return_value=settings), \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker), \
                mock.patch("app.voucher_preview_window.QMessageBox") as messagebox:
            win.print_button.click()
            worker.error.emit("失敗理由", {})
        self.assertIn("Acrobat Reader印刷でエラー", win.status_label.text())
        self.assertTrue(win.print_button.isEnabled())
        messagebox.critical.assert_not_called()

    def test_zoom_controls_change_zoom(self) -> None:
        win = self._make_window()
        win._on_zoom_in()
        self.assertGreater(win._zoom, 1.0)
        win._on_zoom_reset()
        self.assertEqual(win._zoom, 1.0)

    def test_opened_preview_is_maximized(self) -> None:
        from app.voucher_window import VoucherWindow

        owner = mock.MagicMock()
        with mock.patch(
            "app.voucher_preview_window.VoucherPrintPreviewWindow"
        ) as preview_cls:
            preview = preview_cls.return_value
            result = VoucherWindow._open_preview_window(owner, _MINIMAL_PDF)
        preview.showMaximized.assert_called_once_with()
        self.assertIs(result, preview)


if __name__ == "__main__":
    unittest.main()
