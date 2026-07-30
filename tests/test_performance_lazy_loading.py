from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app import voucher_edit_window as edit
from app import voucher_window as voucher_list


class PerformanceLazyLoadingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_editor_slow_background_does_not_block_window_or_show(self) -> None:
        """5秒相当の背景処理が開始してもwindow_visibleは待たない。"""
        release = threading.Event()
        entered = threading.Event()

        def slow_raster(_pdf: bytes, _zoom: float = 2.0):
            entered.set()
            release.wait(5.0)
            return {"png_bytes": b"invalid", "page_w": 595.0, "page_h": 842.0}

        started = time.perf_counter()
        with mock.patch.object(edit, "rasterize_order_sheet_background", side_effect=slow_raster), \
                mock.patch.object(edit.QFontDatabase, "families") as font_scan:
            win = edit.VoucherEditWindow(
                "PERF-SLOW", b"pdf", voucher_nos=["001"],
                background_pdf_by_voucher={"001": b"pdf"}, defer_background=True,
                request_started=started,
            )
            self.addCleanup(win.deleteLater)
            win.show()
            self.app.processEvents()
            self.assertLess(time.perf_counter() - started, 1.0)
            self.assertTrue(any(item.data(2) == "loading" for item in win.background_items()))
            font_scan.assert_not_called()
            for _ in range(100):
                self.app.processEvents()
                if entered.is_set():
                    break
                time.sleep(0.002)
            self.assertTrue(entered.is_set())
            release.set()
            for _ in range(100):
                self.app.processEvents()
                if not edit._BACKGROUND_THREADS:
                    break
                time.sleep(0.002)

    def test_font_families_cache_is_process_shared(self) -> None:
        old = edit._FONT_FAMILY_CACHE
        edit._FONT_FAMILY_CACHE = None
        try:
            with mock.patch.object(edit.QFontDatabase, "families", return_value=["A", "B"]) as scan:
                self.assertEqual(edit.cached_font_families(), ("A", "B"))
                self.assertEqual(edit.cached_font_families(), ("A", "B"))
                scan.assert_called_once()
        finally:
            edit._FONT_FAMILY_CACHE = old

    def test_list_batch_constants_and_no_row_threads(self) -> None:
        self.assertEqual(voucher_list.INITIAL_INTERACTIVE_ROW_COUNT, 10)
        self.assertEqual(voucher_list.BACKGROUND_LOAD_BATCH_SIZE, 10)
        self.assertEqual(
            voucher_list.VoucherWindow._SAVED_ROWS_RESTORE_CHUNK_SIZE, 10)

    def test_stale_background_generation_is_discarded(self) -> None:
        win = edit.VoucherEditWindow(
            "STALE", b"", voucher_nos=["001"], defer_background=True)
        self.addCleanup(win.deleteLater)
        win._background_load_generation = 2
        before = list(win.background_items())
        win._on_background_raster_ready(
            1, win._current_voucher_key,
            {"png_bytes": b"not-used", "page_w": 595.0, "page_h": 842.0},
        )
        self.assertEqual(win.background_items(), before)


if __name__ == "__main__":
    unittest.main()
