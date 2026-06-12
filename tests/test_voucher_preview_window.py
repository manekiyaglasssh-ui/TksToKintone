"""VoucherPrintPreviewWindow（アプリ内印刷プレビュー）のテスト。

PDFバイト列を受け取って表示し、印刷ボタンで既存の印刷処理を呼ぶことを検証する。
プレビューはファイル保存を一切行わない。
"""
from __future__ import annotations

import os
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

    def test_window_has_print_and_close_buttons(self) -> None:
        win = self._make_window()
        self.assertEqual(win.print_button.text(), "印刷")
        self.assertEqual(win.close_button.text(), "閉じる")

    def test_print_button_invokes_print_service_with_bytes(self) -> None:
        win = self._make_window(_MINIMAL_PDF)
        with mock.patch("app.voucher_print_service.print_pdf_with_dialog") as pr:
            win.print_button.click()
        pr.assert_called_once()
        self.assertEqual(pr.call_args.args[0], _MINIMAL_PDF)

    def test_zoom_controls_change_zoom(self) -> None:
        win = self._make_window()
        win._on_zoom_in()
        self.assertGreater(win._zoom, 1.0)
        win._on_zoom_reset()
        self.assertEqual(win._zoom, 1.0)


if __name__ == "__main__":
    unittest.main()
