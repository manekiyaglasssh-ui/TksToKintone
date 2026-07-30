"""伝票の即時印刷設定テスト。"""
from __future__ import annotations

import os
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


class _FakePrinter:
    class PrinterMode:
        HighResolution = object()

    class ColorMode:
        GrayScale = "GrayScale"
        Color = "Color"

    def __init__(self, _mode):
        self.printer_name = ""
        self.page_size = None
        self.orientation = None
        self.color_mode = None
        self.copy_count = None
        self.full_page = False
        self.page_layout = _FakePageLayout()

    def setPrinterName(self, value):
        self.printer_name = value

    def setPageSize(self, value):
        self.page_size = value

    def setPageOrientation(self, value):
        self.orientation = value

    def setColorMode(self, value):
        self.color_mode = value

    def setCopyCount(self, value):
        self.copy_count = value

    def setFullPage(self, value):
        self.full_page = bool(value)

    def fullPage(self):
        return self.full_page

    def pageLayout(self):
        return self.page_layout

    def setPageLayout(self, value):
        self.page_layout = value

    def resolution(self):
        return 300


class _FakeMargins:
    def __init__(self, left=0, top=0, right=0, bottom=0):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class _FakePageLayout:
    def __init__(self):
        self.margins_value = None

    def setMargins(self, margins):
        self.margins_value = margins

    def margins(self, _unit=None):
        return self.margins_value

    def fullRect(self, _unit=None):
        from PySide6.QtCore import QRectF

        return QRectF(0, 0, 257, 182)

    def paintRect(self, _unit=None):
        from PySide6.QtCore import QRectF

        return QRectF(0, 0, 257, 182)

    def fullRectPixels(self, _dpi):
        from PySide6.QtCore import QRect

        return QRect(0, 0, 3039, 2150)

    def paintRectPixels(self, _dpi):
        from PySide6.QtCore import QRect

        return QRect(0, 0, 3039, 2150)


class _FakePrinterInfo:
    def __init__(self, name: str = ""):
        self._name = name

    def printerName(self):
        return self._name


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherDirectPrint(unittest.TestCase):
    def _settings(
        self,
        printer_name: str,
        *,
        backend: str = "qt",
        acrobat_path: str = "",
        hide_window: bool = True,
        close_after_print: bool = True,
        close_delay_seconds: int = 10,
        allow_force_kill: bool = False,
        hide_watch_enabled: bool = True,
        hide_watch_seconds: int = 5,
    ):
        from app.voucher_settings import VoucherPrinterSettings

        return VoucherPrinterSettings(
            printer_name=printer_name,
            paper_size="B5",
            orientation="landscape",
            color_mode="grayscale",
            copies=2,
            print_backend=backend,
            acrobat_path=acrobat_path,
            acrobat_hide_window=hide_window,
            acrobat_close_after_print=close_after_print,
            acrobat_close_delay_seconds=close_delay_seconds,
            acrobat_allow_force_kill=allow_force_kill,
            acrobat_hide_watch_enabled=hide_watch_enabled,
            acrobat_hide_watch_seconds=hide_watch_seconds,
        )

    def test_saved_printer_settings_are_applied_to_qprinter(self) -> None:
        from app import voucher_print_service
        from PySide6.QtGui import QPageLayout

        with mock.patch("PySide6.QtPrintSupport.QPrinter", _FakePrinter), \
                mock.patch(
                    "PySide6.QtPrintSupport.QPrinterInfo.availablePrinters",
                    return_value=[_FakePrinterInfo("Printer A")],
                ), \
                mock.patch(
                    "app.voucher_settings.load_voucher_printer_settings",
                    return_value=self._settings("Printer A"),
                ):
            printer = voucher_print_service.create_printer_from_saved_settings()

        self.assertEqual(printer.printer_name, "Printer A")
        self.assertIsNotNone(printer.page_size)
        self.assertEqual(printer.orientation, QPageLayout.Orientation.Landscape)
        self.assertEqual(printer.color_mode, _FakePrinter.ColorMode.GrayScale)
        self.assertEqual(printer.copy_count, 2)
        self.assertTrue(printer.full_page)
        self.assertIsNotNone(printer.page_layout.margins_value)
        self.assertEqual(getattr(printer, "_voucher_print_scale_mode"), "actual_size")

    def test_missing_printer_setting_stops_before_qprinter(self) -> None:
        from app import voucher_print_service
        from app.voucher_settings import VoucherPrinterSettings

        with mock.patch(
            "app.voucher_settings.load_voucher_printer_settings",
            return_value=VoucherPrinterSettings(),
        ), self.assertRaisesRegex(RuntimeError, "印刷設定が未設定です"):
            voucher_print_service.create_printer_from_saved_settings()

    def test_saved_printer_not_found_stops_print(self) -> None:
        from app import voucher_print_service

        with mock.patch(
            "PySide6.QtPrintSupport.QPrinterInfo.availablePrinters",
            return_value=[_FakePrinterInfo("Other")],
        ), mock.patch(
            "app.voucher_settings.load_voucher_printer_settings",
            return_value=self._settings("Missing"),
        ), self.assertRaisesRegex(RuntimeError, "設定済みプリンターが見つかりません"):
            voucher_print_service.create_printer_from_saved_settings()

    def test_print_pdf_qt_direct_does_not_create_qprintdialog(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service, "create_printer_from_saved_settings", return_value=object()), \
                mock.patch.object(voucher_print_service, "_print_pdf_bytes") as do_print, \
                mock.patch("PySide6.QtPrintSupport.QPrintDialog") as dialog:
            self.assertTrue(voucher_print_service.print_pdf_qt_direct(b"%PDF"))
        dialog.assert_not_called()
        do_print.assert_called_once()

    def test_print_pdf_direct_uses_acrobat_backend_by_default(self) -> None:
        from app import voucher_print_service

        settings = self._settings("Printer A", backend="acrobat", acrobat_path="/tmp/acrobat.exe")
        with mock.patch(
            "app.voucher_settings.load_voucher_printer_settings",
            return_value=settings,
        ), mock.patch.object(voucher_print_service, "start_print_pdf_background") as enqueue, \
                mock.patch.object(voucher_print_service, "_print_pdf_with_acrobat") as acrobat, \
                mock.patch.object(voucher_print_service, "print_pdf_qt_direct") as qt_print:
            self.assertTrue(voucher_print_service.print_pdf_direct(b"%PDF", job_name="1394160"))
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0], b"%PDF")
        self.assertEqual(enqueue.call_args.kwargs["job_name"], "1394160")
        acrobat.assert_not_called()
        qt_print.assert_not_called()

    def test_print_pdf_direct_uses_qt_backend_when_selected(self) -> None:
        from app import voucher_print_service

        settings = self._settings("Printer A", backend="qt")
        with mock.patch(
            "app.voucher_settings.load_voucher_printer_settings",
            return_value=settings,
        ), mock.patch.object(voucher_print_service, "start_print_pdf_background") as enqueue, \
                mock.patch.object(voucher_print_service, "_print_pdf_with_acrobat") as acrobat, \
                mock.patch.object(voucher_print_service, "print_pdf_qt_direct", return_value=True) as qt_print:
            self.assertTrue(voucher_print_service.print_pdf_direct(b"%PDF"))
        qt_print.assert_called_once()
        enqueue.assert_not_called()
        acrobat.assert_not_called()

    def test_acrobat_backend_missing_path_stops_without_qt_fallback(self) -> None:
        from app import voucher_print_service

        settings = self._settings("Printer A", backend="acrobat", acrobat_path="")
        with mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                mock.patch.object(voucher_print_service, "detect_acrobat_reader_path", return_value=""), \
                mock.patch.object(voucher_print_service, "print_pdf_qt_direct") as qt_print, \
                self.assertRaisesRegex(RuntimeError, "Acrobat Readerが見つかりません"):
            voucher_print_service._print_pdf_with_acrobat(b"%PDF", settings, job_name="1394160")
        qt_print.assert_not_called()

    def test_acrobat_backend_missing_executable_stops_without_qt_fallback(self) -> None:
        from app import voucher_print_service

        settings = self._settings("Printer A", backend="acrobat", acrobat_path="/not/found/AcroRd32.exe")
        with mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                mock.patch.object(voucher_print_service, "print_pdf_qt_direct") as qt_print, \
                self.assertRaisesRegex(RuntimeError, "Acrobat Readerのパスが存在しません"):
            voucher_print_service._print_pdf_with_acrobat(b"%PDF", settings, job_name="1394160")
        qt_print.assert_not_called()

    def test_acrobat_backend_saves_print_job_and_starts_process(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            acrobat = Path(temp_dir) / "AcroRd32.exe"
            acrobat.write_text("stub", encoding="utf-8")
            settings = self._settings("Printer A", backend="acrobat", acrobat_path=str(acrobat))
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                popen.return_value.pid = 1234
                popen.return_value.poll.return_value = None
                voucher_print_service._print_pdf_with_acrobat(b"%PDF", settings, job_name="1394160")
                saved = list((Path(temp_dir) / "work" / "print_jobs").glob("voucher_print_*_1394160.pdf"))
        self.assertEqual(len(saved), 1)
        popen.assert_called_once()
        self.assertIn("/t", popen.call_args.args[0])
        self.assertIn("/h", popen.call_args.args[0])

    def test_acrobat_backend_does_not_render_pdf_with_qprinter_or_qpainter(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            acrobat = Path(temp_dir) / "AcroRd32.exe"
            acrobat.write_text("stub", encoding="utf-8")
            settings = self._settings("Printer A", backend="acrobat", acrobat_path=str(acrobat))
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service, "_print_pdf_bytes") as qt_render, \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                popen.return_value.pid = 1234
                popen.return_value.poll.return_value = None
                voucher_print_service._print_pdf_with_acrobat(b"%PDF", settings)
        qt_render.assert_not_called()

    def test_acrobat_hide_window_sets_startupinfo(self) -> None:
        from app import voucher_print_service

        class FakeStartupInfo:
            def __init__(self):
                self.dwFlags = 0
                self.wShowWindow = None

        settings = self._settings("Printer A", backend="acrobat", hide_window=True)
        with mock.patch.object(voucher_print_service.subprocess, "STARTUPINFO", FakeStartupInfo, create=True), \
                mock.patch.object(voucher_print_service.subprocess, "STARTF_USESHOWWINDOW", 1, create=True), \
                mock.patch.object(voucher_print_service.subprocess, "SW_HIDE", 0, create=True):
            kwargs = voucher_print_service._build_acrobat_popen_kwargs(settings)
        self.assertIsInstance(kwargs["startupinfo"], FakeStartupInfo)
        self.assertEqual(kwargs["startupinfo"].dwFlags, 1)
        self.assertEqual(kwargs["startupinfo"].wShowWindow, 0)

    def test_acrobat_hide_window_sets_creationflags(self) -> None:
        from app import voucher_print_service

        settings = self._settings("Printer A", backend="acrobat", hide_window=True)
        kwargs = voucher_print_service._build_acrobat_popen_kwargs(settings)
        flags = int(kwargs["creationflags"])
        self.assertTrue(flags & voucher_print_service.CREATE_NO_WINDOW)
        self.assertTrue(flags & voucher_print_service.CREATE_NEW_PROCESS_GROUP)
        self.assertIs(kwargs["shell"], False)

    def test_sumatra_windows_popen_kwargs_suppress_console_and_shell_false(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service, "_is_windows", return_value=True):
            kwargs = voucher_print_service._build_sumatra_popen_kwargs()
        flags = int(kwargs["creationflags"])
        self.assertTrue(flags & voucher_print_service.CREATE_NO_WINDOW)
        self.assertIs(kwargs["shell"], False)

    def test_print_paths_do_not_invoke_powershell_cmd_or_shell_true(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "voucher_print_service.py").read_text(encoding="utf-8")
        forbidden = [
            '"powershell"',
            "'powershell'",
            "powershell.exe",
            '"pwsh"',
            "'pwsh'",
            ".ps1",
            "shell=True",
            "start /wait",
            "cmd /c",
            "os.system",
        ]
        lower_source = source.lower()
        for token in forbidden:
            self.assertNotIn(token.lower(), lower_source)

    def test_acrobat_command_contains_t_and_hidden_option(self) -> None:
        from app import voucher_print_service

        settings = self._settings("Printer A", backend="acrobat", hide_window=True)
        command = voucher_print_service._build_acrobat_print_command(
            r"C:\Adobe\AcroRd32.exe", Path(r"C:\tmp\job.pdf"), "Printer A", settings
        )
        self.assertIn("/t", command)
        self.assertIn("/h", command)
        self.assertEqual(command[-2:], [str(Path(r"C:\tmp\job.pdf")), "Printer A"])

    def test_acrobat_command_includes_s_o_h_t(self) -> None:
        from app import voucher_print_service

        settings = self._settings("Printer A", backend="acrobat", hide_window=True)
        command = voucher_print_service._build_acrobat_print_command(
            r"C:\Adobe\AcroRd32.exe", Path(r"C:\tmp\job.pdf"), "Printer A", settings
        )
        for arg in ("/s", "/o", "/h", "/t"):
            self.assertIn(arg, command)

    def test_acrobat_command_has_no_sumatra_arguments(self) -> None:
        from app import voucher_print_service

        settings = self._settings("Printer A", backend="acrobat", hide_window=True)
        command = voucher_print_service._build_acrobat_print_command(
            r"C:\Adobe\AcroRd32.exe", Path(r"C:\tmp\job.pdf"), "Printer A", settings
        )
        for sumatra_arg in ("-silent", "-print-to", "-print-settings"):
            self.assertNotIn(sumatra_arg, command)

    def test_hide_watch_interval_is_shorter_right_after_launch(self) -> None:
        from app import voucher_print_service

        fast = voucher_print_service._hide_watch_interval_seconds(0.0)
        slow = voucher_print_service._hide_watch_interval_seconds(5.0)
        self.assertLess(fast, slow)
        self.assertLessEqual(fast, 0.05)

    def test_start_hide_watch_before_popen_runs_watch_thread(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(
            voucher_print_service,
            "_hide_acrobat_windows_for_pids",
            return_value={"target_acrobat_pids": [5678]},
        ) as watch:
            handle = voucher_print_service._start_hide_watch_before_popen(
                {99}, duration_seconds=1, enabled=True
            )
            handle.add_target_pid(1234)
            result = handle.join(timeout=5)
        watch.assert_called_once()
        self.assertEqual(watch.call_args.args[0], set())
        self.assertEqual(watch.call_args.args[1], {99})
        self.assertTrue(watch.call_args.kwargs["enabled"])
        self.assertTrue(watch.call_args.kwargs["started_before_popen"])
        self.assertEqual(result, {"target_acrobat_pids": [5678]})

    def test_hide_watch_records_foreground_and_hide_counts(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service, "_is_windows", return_value=True), \
                mock.patch.object(voucher_print_service, "_list_acrobat_process_ids", return_value={5678}), \
                mock.patch.object(voucher_print_service, "_child_process_ids", return_value=set()), \
                mock.patch.object(
                    voucher_print_service,
                    "_enum_top_level_windows_for_pids",
                    return_value=[{"hwnd": 111, "pid": 5678, "title": "Adobe Acrobat", "visible": True}],
                ), \
                mock.patch.object(voucher_print_service, "_is_foreground_window", return_value=True), \
                mock.patch.object(
                    voucher_print_service,
                    "_hide_or_minimize_window",
                    return_value={
                        "hide_window_called": True,
                        "minimize_window_called": True,
                        "set_bottom_result": True,
                    },
                ), \
                mock.patch.object(voucher_print_service.time, "monotonic", side_effect=[0, 0, 2]), \
                mock.patch.object(voucher_print_service.time, "sleep"):
            info = voucher_print_service._hide_acrobat_windows_for_pids(
                set(), set(), duration_seconds=1, enabled=True, started_before_popen=True
            )
        self.assertTrue(info["hide_watch_started_before_popen"])
        self.assertTrue(info["window_foreground_detected"])
        self.assertTrue(info["window_sent_to_bottom"])
        self.assertEqual(info["window_hidden_count"], 1)
        self.assertEqual(info["window_minimized_count"], 1)
        self.assertIsNotNone(info["hide_watch_first_hide_elapsed_ms"])

    def test_close_after_print_sends_wm_close_to_target_pid(self) -> None:
        from app import voucher_print_service

        process = mock.Mock()
        process.pid = 1234
        process.poll.return_value = None
        settings = self._settings("Printer A", backend="acrobat", close_after_print=True, close_delay_seconds=5)
        with mock.patch.object(voucher_print_service, "_is_windows", return_value=True), \
                mock.patch.object(voucher_print_service, "_wait_before_acrobat_close"), \
                mock.patch.object(voucher_print_service, "_window_handles_for_process", return_value=[111, 222]), \
                mock.patch.object(voucher_print_service, "_send_wm_close_to_windows", return_value=True) as send, \
                mock.patch.object(voucher_print_service.time, "sleep"):
            info = voucher_print_service._close_print_acrobat_process(process, settings, set())
        send.assert_called_once_with([111, 222])
        self.assertTrue(info["close_sent"])
        self.assertEqual(info["close_result"], "warning")
        self.assertEqual(info["close_skipped_reason"], "force_kill_disabled")
        process.terminate.assert_not_called()

    def test_existing_acrobat_process_is_not_closed(self) -> None:
        from app import voucher_print_service

        process = mock.Mock()
        process.pid = 1234
        process.poll.return_value = None
        settings = self._settings("Printer A", backend="acrobat")
        with mock.patch.object(voucher_print_service, "_send_wm_close_to_windows") as send:
            info = voucher_print_service._close_print_acrobat_process(process, settings, {1234})
        send.assert_not_called()
        self.assertEqual(info["close_skipped_reason"], "existing_acrobat_process")

    def test_quickly_exited_process_does_not_close_existing_acrobat(self) -> None:
        from app import voucher_print_service

        process = mock.Mock()
        process.pid = 1234
        process.poll.return_value = 0
        settings = self._settings("Printer A", backend="acrobat")
        with mock.patch.object(voucher_print_service, "_send_wm_close_to_windows") as send:
            info = voucher_print_service._close_print_acrobat_process(process, settings, set())
        send.assert_not_called()
        self.assertEqual(info["close_skipped_reason"], "process_already_exited")

    def test_force_kill_off_does_not_terminate_or_kill(self) -> None:
        from app import voucher_print_service

        process = mock.Mock()
        process.pid = 1234
        process.poll.return_value = None
        settings = self._settings("Printer A", backend="acrobat", allow_force_kill=False)
        with mock.patch.object(voucher_print_service, "_is_windows", return_value=True), \
                mock.patch.object(voucher_print_service, "_wait_before_acrobat_close"), \
                mock.patch.object(voucher_print_service, "_window_handles_for_process", return_value=[111]), \
                mock.patch.object(voucher_print_service, "_send_wm_close_to_windows", return_value=True), \
                mock.patch.object(voucher_print_service.time, "sleep"):
            info = voucher_print_service._close_print_acrobat_process(process, settings, set())
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertEqual(info["close_skipped_reason"], "force_kill_disabled")

    def test_close_after_print_off_skips_close(self) -> None:
        from app import voucher_print_service

        process = mock.Mock()
        process.pid = 1234
        settings = self._settings("Printer A", backend="acrobat", close_after_print=False)
        with mock.patch.object(voucher_print_service, "_send_wm_close_to_windows") as send:
            info = voucher_print_service._close_print_acrobat_process(process, settings, set())
        send.assert_not_called()
        self.assertEqual(info["close_skipped_reason"], "close_after_print_disabled")

    def test_hide_watch_starts_before_acrobat_popen(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            acrobat = Path(temp_dir) / "AcroRd32.exe"
            acrobat.write_text("stub", encoding="utf-8")
            settings = self._settings("Printer A", backend="acrobat", acrobat_path=str(acrobat))
            call_order: list[str] = []
            handle = mock.Mock()
            handle.join.return_value = {"target_acrobat_pids": [1234]}

            def _start_watch(existing, **kwargs):
                call_order.append("watch")
                self.assertEqual(existing, {99})
                self.assertTrue(kwargs["enabled"])
                return handle

            def _popen(*_args, **_kwargs):
                call_order.append("popen")
                proc = mock.Mock()
                proc.pid = 1234
                proc.poll.return_value = None
                return proc

            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service, "_list_acrobat_process_ids", return_value={99}), \
                    mock.patch.object(voucher_print_service, "_start_hide_watch_before_popen", side_effect=_start_watch) as start_watch, \
                    mock.patch.object(voucher_print_service.subprocess, "Popen", side_effect=_popen), \
                    mock.patch.object(voucher_print_service, "_close_print_acrobat_processes", return_value={}):
                voucher_print_service._print_pdf_with_acrobat(b"%PDF", settings)
        start_watch.assert_called_once()
        self.assertEqual(call_order, ["watch", "popen"])
        handle.add_target_pid.assert_called_once_with(1234)

    def test_quickly_exited_popen_pid_still_starts_hide_watch(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            acrobat = Path(temp_dir) / "AcroRd32.exe"
            acrobat.write_text("stub", encoding="utf-8")
            settings = self._settings("Printer A", backend="acrobat", acrobat_path=str(acrobat))
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service, "_list_acrobat_process_ids", return_value={99}), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen, \
                    mock.patch.object(voucher_print_service, "_hide_acrobat_windows_for_pids", return_value={"target_acrobat_pids": [5678]}) as watch, \
                    mock.patch.object(voucher_print_service, "_close_print_acrobat_processes", return_value={}) as close:
                popen.return_value.pid = 1234
                popen.return_value.poll.return_value = 0
                voucher_print_service._print_pdf_with_acrobat(b"%PDF", settings)
        self.assertTrue(watch.call_args.kwargs["enabled"])
        close.assert_called_once()
        self.assertEqual(close.call_args.args[3], {5678})

    def test_hide_watch_off_skips_window_hide_processing(self) -> None:
        from app import voucher_print_service

        settings = self._settings("Printer A", backend="acrobat", hide_watch_enabled=False)
        with mock.patch.object(voucher_print_service, "_is_windows", return_value=True), \
                mock.patch.object(voucher_print_service, "_enum_top_level_windows_for_pid") as enum:
            info = voucher_print_service._hide_acrobat_windows_for_pid(
                1234, set(), enabled=settings.acrobat_hide_watch_enabled
            )
        enum.assert_not_called()
        self.assertFalse(info["hide_watch_enabled"])

    def test_enum_top_level_windows_for_pid_detects_popen_pid_window(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(
            voucher_print_service,
            "_enum_top_level_windows_for_pid",
            return_value=[{"hwnd": 111, "pid": 1234, "title": "Adobe Acrobat Reader"}],
        ):
            windows = voucher_print_service._enum_top_level_windows_for_pid(1234)
        self.assertEqual(windows[0]["hwnd"], 111)
        self.assertEqual(windows[0]["pid"], 1234)

    def test_popen_pid_window_calls_showwindow_hide_and_set_bottom(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service, "_show_window_async", return_value=True) as show_async, \
                mock.patch.object(voucher_print_service, "_show_window", return_value=True) as show, \
                mock.patch.object(voucher_print_service, "_set_window_bottom_no_activate", return_value=True) as bottom:
            info = voucher_print_service._hide_or_minimize_window(111)
        self.assertEqual(show.call_args_list[0].args, (111, voucher_print_service.SW_HIDE))
        self.assertEqual(show.call_args_list[1].args, (111, voucher_print_service.SW_MINIMIZE))
        self.assertEqual(show_async.call_args_list[0].args, (111, voucher_print_service.SW_HIDE))
        self.assertEqual(show_async.call_args_list[1].args, (111, voucher_print_service.SW_MINIMIZE))
        bottom.assert_called_once_with(111)
        self.assertTrue(info["hide_window_called"])
        self.assertTrue(info["minimize_window_called"])
        self.assertTrue(info["set_bottom_called"])

    def test_hide_failure_calls_minimize_and_set_bottom(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service, "_show_window_async", side_effect=[False, True]) as show_async, \
                mock.patch.object(voucher_print_service, "_show_window", return_value=False) as show, \
                mock.patch.object(voucher_print_service, "_set_window_bottom_no_activate", return_value=True) as bottom:
            info = voucher_print_service._hide_or_minimize_window(111)
        self.assertEqual(show.call_args_list[0].args, (111, voucher_print_service.SW_HIDE))
        self.assertEqual(show.call_args_list[1].args, (111, voucher_print_service.SW_MINIMIZE))
        self.assertEqual(show_async.call_args_list[0].args, (111, voucher_print_service.SW_HIDE))
        self.assertEqual(show_async.call_args_list[1].args, (111, voucher_print_service.SW_MINIMIZE))
        bottom.assert_called_once_with(111)
        self.assertTrue(info["minimize_window_called"])

    def test_hide_watch_does_not_touch_existing_acrobat_pid(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service, "_hide_or_minimize_window") as hide:
            info = voucher_print_service._hide_acrobat_windows_for_pid(1234, {1234}, enabled=True)
        hide.assert_not_called()
        self.assertTrue(info["ignored_existing_acrobat_window"])

    def test_hide_watch_adds_new_acrobat_pid_and_skips_existing_window(self) -> None:
        from app import voucher_print_service

        windows = [
            {"hwnd": 111, "pid": 5678, "title": "Acrobat Readerへようこそ", "visible": True},
            {"hwnd": 222, "pid": 99, "title": "User PDF", "visible": True},
        ]
        with mock.patch.object(voucher_print_service, "_is_windows", return_value=True), \
                mock.patch.object(voucher_print_service, "_list_acrobat_process_ids", return_value={99, 5678}), \
                mock.patch.object(voucher_print_service, "_child_process_ids", return_value=set()), \
                mock.patch.object(voucher_print_service, "_enum_top_level_windows_for_pids", return_value=windows) as enum, \
                mock.patch.object(voucher_print_service, "_hide_or_minimize_window", return_value={"hide_attempted": True}) as hide, \
                mock.patch.object(voucher_print_service.time, "monotonic", side_effect=[0, 0, 2]), \
                mock.patch.object(voucher_print_service.time, "sleep"):
            info = voucher_print_service._hide_acrobat_windows_for_pids(
                {1234}, {99}, duration_seconds=1, enabled=True
            )
        enum.assert_called()
        hide.assert_called_once_with(111)
        self.assertIn(5678, info["target_acrobat_pids"])
        self.assertNotIn(99, info["target_acrobat_pids"])
        self.assertEqual(info["new_acrobat_pids_detected"], [5678])
        self.assertTrue(info["windows_seen"][0]["window_is_target"])
        self.assertFalse(info["windows_seen"][1]["window_is_target"])

    def test_hide_watch_uses_pid_not_pdf_title_for_home_window(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service, "_is_windows", return_value=True), \
                mock.patch.object(voucher_print_service, "_list_acrobat_process_ids", return_value={5678}), \
                mock.patch.object(voucher_print_service, "_child_process_ids", return_value=set()), \
                mock.patch.object(
                    voucher_print_service,
                    "_enum_top_level_windows_for_pids",
                    return_value=[{"hwnd": 111, "pid": 5678, "title": "Adobe Acrobat", "visible": True}],
                ), \
                mock.patch.object(voucher_print_service, "_hide_or_minimize_window", return_value={"hide_attempted": True}) as hide, \
                mock.patch.object(voucher_print_service.time, "monotonic", side_effect=[0, 0, 2]), \
                mock.patch.object(voucher_print_service.time, "sleep"):
            info = voucher_print_service._hide_acrobat_windows_for_pids(
                set(), set(), duration_seconds=1, enabled=True
            )
        hide.assert_called_once_with(111)
        self.assertEqual(info["window_title"], "Adobe Acrobat")
        self.assertTrue(info["window_is_target"])

    def test_hide_watch_repeats_window_hide_during_watch_period(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service, "_is_windows", return_value=True), \
                mock.patch.object(voucher_print_service, "_list_acrobat_process_ids", return_value={5678}), \
                mock.patch.object(voucher_print_service, "_child_process_ids", return_value=set()), \
                mock.patch.object(
                    voucher_print_service,
                    "_enum_top_level_windows_for_pids",
                    return_value=[{"hwnd": 111, "pid": 5678, "title": "Adobe Acrobat Reader", "visible": True}],
                ), \
                mock.patch.object(voucher_print_service, "_hide_or_minimize_window", return_value={"hide_attempted": True}) as hide, \
                mock.patch.object(voucher_print_service.time, "monotonic", side_effect=[0, 0, 0.1, 2]), \
                mock.patch.object(voucher_print_service.time, "sleep"):
            info = voucher_print_service._hide_acrobat_windows_for_pids(
                set(), set(), duration_seconds=1, enabled=True
            )
        self.assertEqual(hide.call_count, 2)
        self.assertEqual(info["hide_watch_loop_count"], 2)

    def test_close_target_pids_include_new_and_exclude_existing(self) -> None:
        from app import voucher_print_service

        process = mock.Mock()
        process.pid = 1234
        process.poll.return_value = 0
        settings = self._settings("Printer A", backend="acrobat")
        with mock.patch.object(voucher_print_service, "_is_windows", return_value=True), \
                mock.patch.object(voucher_print_service, "_wait_before_acrobat_close"), \
                mock.patch.object(voucher_print_service, "_window_handles_for_process", return_value=[]):
            info = voucher_print_service._close_print_acrobat_processes(process, settings, {99}, {99, 5678})
        self.assertIn(5678, info["close_target_pids"])
        self.assertNotIn(99, info["close_target_pids"])
        self.assertEqual(info["close_skipped_existing_pids"], [99])

    def test_existing_only_acrobat_is_not_hidden_or_closed(self) -> None:
        from app import voucher_print_service

        process = mock.Mock()
        process.pid = 99
        process.poll.return_value = None
        settings = self._settings("Printer A", backend="acrobat")
        with mock.patch.object(voucher_print_service, "_is_windows", return_value=True), \
                mock.patch.object(voucher_print_service, "_list_acrobat_process_ids", return_value={99}), \
                mock.patch.object(voucher_print_service, "_child_process_ids", return_value=set()), \
                mock.patch.object(
                    voucher_print_service,
                    "_enum_top_level_windows_for_pids",
                    return_value=[{"hwnd": 222, "pid": 99, "title": "User PDF", "visible": True}],
                ), \
                mock.patch.object(voucher_print_service, "_hide_or_minimize_window") as hide, \
                mock.patch.object(voucher_print_service.time, "monotonic", side_effect=[0, 0, 2]), \
                mock.patch.object(voucher_print_service.time, "sleep"):
            hide_info = voucher_print_service._hide_acrobat_windows_for_pids({99}, {99}, duration_seconds=1)
        hide.assert_not_called()
        close_info = voucher_print_service._close_print_acrobat_processes(process, settings, {99}, {99})
        self.assertEqual(hide_info["hide_skipped_reason"], "no_new_acrobat_pid_found")
        self.assertEqual(close_info["close_skipped_reason"], "existing_acrobat_process")

    def test_hide_watch_handles_quickly_exited_or_missing_pid(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service, "_enum_top_level_windows_for_pid") as enum:
            info = voucher_print_service._hide_acrobat_windows_for_pid(None, set(), enabled=True)
        enum.assert_not_called()
        self.assertTrue(info["hide_watch_finished"])

    def test_hide_watch_logs_result_to_jsonl(self) -> None:
        from app import voucher_print_service

        settings = self._settings("Printer A", backend="acrobat")
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(voucher_print_service, "get_order_capture_debug_dir", return_value=Path(temp_dir)):
                voucher_print_service._log_acrobat_print_event(
                    settings,
                    "AcroRd32.exe",
                    "job.pdf",
                    "Printer A",
                    hide_watch_info={
                        "hide_watch_started": True,
                        "hide_watch_target_pid": 1234,
                        "hide_watch_existing_pids": [99],
                        "existing_acrobat_pids_before": [99],
                        "current_acrobat_pids": [99, 1234],
                        "new_acrobat_pids_detected": [1234],
                        "target_acrobat_pids": [1234],
                        "ignored_existing_acrobat_pids": [],
                        "windows_seen": [{"window_pid": 1234, "window_hwnd": 111, "window_is_target": True}],
                        "acrobat_window_found": True,
                        "acrobat_window_hwnd": 111,
                        "acrobat_window_title": "Adobe Acrobat Reader",
                        "acrobat_window_pid": 1234,
                        "hide_window_called": True,
                        "hide_attempted": True,
                        "minimize_window_called": False,
                        "set_bottom_called": True,
                        "hide_result": True,
                        "hide_watch_finished": True,
                    },
                )
            log_files = list(Path(temp_dir).glob("voucher_print_*.jsonl"))
            payload = json.loads(log_files[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(payload["hide_watch_started"])
        self.assertEqual(payload["hide_watch_target_pid"], 1234)
        self.assertEqual(payload["acrobat_window_hwnd"], 111)
        self.assertTrue(payload["set_bottom_called"])
        self.assertEqual(payload["existing_acrobat_pids_before"], [99])
        self.assertEqual(payload["new_acrobat_pids_detected"], [1234])
        self.assertEqual(payload["target_acrobat_pids"], [1234])
        self.assertEqual(payload["windows_seen"][0]["window_pid"], 1234)

    def test_acrobat_backend_invokes_close_path_for_row_select_and_preview_entry(self) -> None:
        from app import voucher_print_service

        settings = self._settings("Printer A", backend="acrobat")
        with mock.patch("app.voucher_settings.load_voucher_printer_settings", return_value=settings), \
                mock.patch.object(voucher_print_service, "start_print_pdf_background") as enqueue, \
                mock.patch.object(voucher_print_service, "_print_pdf_with_acrobat") as acrobat:
            voucher_print_service.print_pdf_direct(b"%PDF", job_name="row")
            voucher_print_service.print_pdf_direct(b"%PDF", job_name="select")
            voucher_print_service.print_pdf_direct(b"%PDF")
        self.assertEqual(enqueue.call_count, 3)
        acrobat.assert_not_called()

    def test_no_global_acrobat_termination_command_is_used(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "voucher_print_service.py").read_text(encoding="utf-8")
        self.assertNotIn("taskkill", source.lower())
        self.assertNotIn("/IM AcroRd32.exe", source)

    def test_print_worker_does_not_use_messagebox(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "voucher_print_service.py").read_text(encoding="utf-8")
        # ワーカーは signal 経由でのみ UI へ通知し、QMessageBox を直接触らない。
        self.assertNotIn("QMessageBox", source)

    def test_saved_printer_not_found_stops_acrobat_print(self) -> None:
        from app import voucher_print_service

        with mock.patch(
            "PySide6.QtPrintSupport.QPrinterInfo.availablePrinters",
            return_value=[_FakePrinterInfo("Other")],
        ), self.assertRaisesRegex(RuntimeError, "指定プリンターが見つかりません"):
            voucher_print_service._validate_saved_printer("Missing")

    def test_detect_acrobat_reader_path_uses_candidate(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            candidate = Path(temp_dir) / "AcroRd32.exe"
            candidate.write_text("stub", encoding="utf-8")
            with mock.patch.object(voucher_print_service, "_acrobat_candidate_paths", return_value=[candidate]), \
                    mock.patch.object(voucher_print_service, "_detect_acrobat_from_registry", return_value=""):
                self.assertEqual(voucher_print_service.detect_acrobat_reader_path(), str(candidate))

    def test_cleanup_old_print_jobs_removes_old_pdfs(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            jobs = Path(temp_dir) / "work" / "print_jobs"
            jobs.mkdir(parents=True)
            old_pdf = jobs / "voucher_print_20200101_000000_old.pdf"
            fresh_pdf = jobs / "voucher_print_20990101_000000_fresh.pdf"
            old_pdf.write_bytes(b"%PDF")
            fresh_pdf.write_bytes(b"%PDF")
            old_time = time.time() - 9 * 24 * 60 * 60
            os.utime(old_pdf, (old_time, old_time))
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)):
                removed = voucher_print_service.cleanup_old_print_jobs(retention_days=7)
            self.assertEqual(removed, 1)
            self.assertFalse(old_pdf.exists())
            self.assertTrue(fresh_pdf.exists())

    def test_actual_size_target_rect_does_not_fit_to_printable_area(self) -> None:
        from app import voucher_print_service

        printer = _FakePrinter(None)
        printer._voucher_print_scale_mode = "actual_size"
        with mock.patch.object(voucher_print_service, "_warn_if_actual_size_may_clip"):
            rect = voucher_print_service._target_rect_for_scale_mode(
                printer, 3039, 2148, 729.4, 515.5
            )
        self.assertEqual(rect.x(), 0)
        self.assertEqual(rect.y(), 0)
        self.assertEqual(rect.width(), 3039)
        self.assertEqual(rect.height(), 2148)

    def test_fit_to_page_only_uses_printable_area(self) -> None:
        from app import voucher_print_service
        from PySide6.QtCore import QRect

        class Layout(_FakePageLayout):
            def paintRectPixels(self, _dpi):
                return QRect(10, 20, 1000, 500)

        printer = _FakePrinter(None)
        printer.page_layout = Layout()
        printer._voucher_print_scale_mode = "fit_to_page"
        rect = voucher_print_service._target_rect_for_scale_mode(
            printer, 2000, 1000, 729.4, 515.5
        )
        self.assertEqual(rect.width(), 1000)
        self.assertEqual(rect.height(), 500)
        self.assertEqual(rect.x(), 10)
        self.assertEqual(rect.y(), 20)

    def test_print_geometry_log_contains_pdf_and_effective_scale(self) -> None:
        from app import voucher_print_service

        printer = _FakePrinter(None)
        printer._voucher_print_scale_mode = "actual_size"
        with self.assertLogs("app.voucher_print_service", level="INFO") as logs:
            voucher_print_service._log_print_geometry(printer, 729.4, 515.5, 3039, 2148)
        text = "\n".join(logs.output)
        self.assertIn("pdf_page_width_pt=729.400", text)
        self.assertIn("printer_page_width_mm=257.000", text)
        self.assertIn("effective_scale=1.000000", text)
        self.assertIn("scale_mode=actual_size", text)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherSumatraPrint(unittest.TestCase):
    def _settings(self, printer_name: str = "Printer A", *, sumatra_path: str = "", paperkind: str = ""):
        from app.voucher_settings import DEFAULT_SUMATRA_PRINT_SETTINGS, VoucherPrinterSettings

        return VoucherPrinterSettings(
            printer_name=printer_name,
            paper_size="B5",
            orientation="landscape",
            color_mode="grayscale",
            copies=1,
            print_backend="sumatra",
            sumatra_path=sumatra_path,
            sumatra_print_settings=DEFAULT_SUMATRA_PRINT_SETTINGS,
            sumatra_paperkind=paperkind,
            # 本テストはSumatra印刷機構のみを検証する。印刷補正は別テストで検証するため
            # ダミーPDFで補正PDF生成が走らないよう明示的にOFFにする。
            print_adjustment_enabled=False,
        )

    def _popen_success(self, popen, exit_code: int = 0):
        popen.return_value.pid = 4321
        popen.return_value.communicate.return_value = (b"ok", b"")
        popen.return_value.returncode = exit_code

    def test_print_pdf_direct_uses_sumatra_backend_when_selected(self) -> None:
        from app import voucher_print_service

        with mock.patch(
            "app.voucher_settings.load_voucher_printer_settings",
            return_value=self._settings(sumatra_path="/tmp/SumatraPDF.exe"),
        ), mock.patch.object(voucher_print_service, "start_print_pdf_background") as enqueue, \
                mock.patch.object(voucher_print_service, "_print_pdf_with_sumatra") as sumatra, \
                mock.patch.object(voucher_print_service, "_print_pdf_with_acrobat") as acrobat, \
                mock.patch.object(voucher_print_service, "print_pdf_qt_direct") as qt_print:
            self.assertTrue(voucher_print_service.print_pdf_direct(b"%PDF", job_name="1394160"))
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["job_name"], "1394160")
        sumatra.assert_not_called()
        acrobat.assert_not_called()
        qt_print.assert_not_called()

    def test_sumatra_missing_path_stops_without_acrobat_or_qt(self) -> None:
        from app import voucher_print_service

        settings = self._settings(sumatra_path="")
        with mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                mock.patch.object(voucher_print_service, "_installed_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(voucher_print_service, "_pyinstaller_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(voucher_print_service, "_dev_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(voucher_print_service, "detect_sumatra_pdf_path", return_value=""), \
                mock.patch.object(voucher_print_service, "_print_pdf_with_acrobat") as acrobat, \
                mock.patch.object(voucher_print_service, "print_pdf_qt_direct") as qt_print, \
                mock.patch.object(voucher_print_service.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(RuntimeError, "SumatraPDFが見つかりません"):
            voucher_print_service._print_pdf_with_sumatra(b"%PDF", settings, job_name="1394160")
        acrobat.assert_not_called()
        qt_print.assert_not_called()
        popen.assert_not_called()

    def test_sumatra_missing_executable_stops_without_fallback(self) -> None:
        from app import voucher_print_service

        settings = self._settings(sumatra_path="/not/found/SumatraPDF.exe")
        with mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                mock.patch.object(voucher_print_service, "_installed_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(voucher_print_service, "_pyinstaller_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(voucher_print_service, "_dev_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(voucher_print_service, "_print_pdf_with_acrobat") as acrobat, \
                mock.patch.object(voucher_print_service, "print_pdf_qt_direct") as qt_print, \
                mock.patch.object(voucher_print_service.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(RuntimeError, "セットアップを再実行"):
            voucher_print_service._print_pdf_with_sumatra(b"%PDF", settings, job_name="1394160")
        acrobat.assert_not_called()
        qt_print.assert_not_called()
        popen.assert_not_called()

    def test_sumatra_backend_does_not_launch_acrobat_or_qt(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path=str(sumatra))
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service, "_print_pdf_with_acrobat") as acrobat, \
                    mock.patch.object(voucher_print_service, "_print_pdf_bytes") as qt_render, \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                self._popen_success(popen)
                voucher_print_service._print_pdf_with_sumatra(b"%PDF", settings, job_name="1394160")
        acrobat.assert_not_called()
        qt_render.assert_not_called()

    def test_sumatra_command_contains_required_flags(self) -> None:
        from app import voucher_print_service

        command = voucher_print_service._build_sumatra_print_command(
            r"C:\Tools\SumatraPDF.exe",
            Path(r"C:\tmp\job.pdf"),
            "Printer A",
            "noscale,monochrome,paper=auto,bin=auto,center",
        )
        self.assertIn("-silent", command)
        self.assertIn("-print-to", command)
        self.assertIn("-print-settings", command)
        self.assertEqual(command[command.index("-print-to") + 1], "Printer A")
        self.assertEqual(command[-1], str(Path(r"C:\tmp\job.pdf")))

    def test_sumatra_print_settings_include_noscale_monochrome_paper_auto(self) -> None:
        from app import voucher_print_service

        settings = self._settings(sumatra_path="/tmp/SumatraPDF.exe")
        print_settings = voucher_print_service._resolve_sumatra_print_settings(settings)
        self.assertIn("noscale", print_settings)
        self.assertIn("monochrome", print_settings)
        self.assertIn("paper=auto", print_settings)

    def test_sumatra_paperkind_replaces_paper_auto(self) -> None:
        from app import voucher_print_service

        settings = self._settings(sumatra_path="/tmp/SumatraPDF.exe", paperkind="13")
        print_settings = voucher_print_service._resolve_sumatra_print_settings(settings)
        self.assertIn("paperkind=13", print_settings)
        self.assertNotIn("paper=auto", print_settings)

    def test_sumatra_backend_saves_single_merged_pdf_and_launches_once(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path=str(sumatra))
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                self._popen_success(popen)
                voucher_print_service._print_pdf_with_sumatra(
                    b"%PDF", settings, job_name="batch", print_metadata={"selected_count": 3}
                )
                saved = list((Path(temp_dir) / "work" / "print_jobs").glob("voucher_print_*_batch.pdf"))
        self.assertEqual(len(saved), 1)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command.count("-print-to"), 1)

    def test_sumatra_exit_code_is_logged(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path=str(sumatra))
            debug_dir = Path(temp_dir) / "debug"
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "get_order_capture_debug_dir", return_value=debug_dir), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                self._popen_success(popen, exit_code=0)
                voucher_print_service._print_pdf_with_sumatra(b"%PDF", settings, job_name="job")
            log_files = list(debug_dir.glob("voucher_print_*.jsonl"))
            payload = json.loads(log_files[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(payload["print_backend"], "sumatra")
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["stdout"], "ok")
        self.assertTrue(payload["sumatra_args_include_print_to"])
        self.assertTrue(payload["sumatra_args_include_silent"])
        self.assertTrue(payload["sumatra_args_include_print_settings"])

    def test_sumatra_backend_logs_resolve_popen_and_request_sent(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path=str(sumatra))
            debug_dir = Path(temp_dir) / "debug"
            sent_payloads: list[dict[str, object]] = []
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "get_order_capture_debug_dir", return_value=debug_dir), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                self._popen_success(popen, exit_code=0)
                voucher_print_service._print_pdf_with_sumatra(
                    b"%PDF",
                    settings,
                    job_name="job",
                    print_metadata={"print_job_id": "job-1", "source_type": "row"},
                    request_sent_callback=sent_payloads.append,
                )
            lines = [
                json.loads(line)
                for path in debug_dir.glob("voucher_print_*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
        event_types = [line.get("event_type") for line in lines]
        self.assertIn("sumatra_resolve_started", event_types)
        self.assertIn("sumatra_resolve_finished", event_types)
        self.assertIn("sumatra_popen_started", event_types)
        self.assertIn("sumatra_request_sent_signal_emitted", event_types)
        popen.assert_called_once()
        self.assertTrue(sent_payloads)

    def test_sumatra_nonzero_exit_code_raises_error(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path=str(sumatra))
            debug_dir = Path(temp_dir) / "debug"
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "get_order_capture_debug_dir", return_value=debug_dir), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                self._popen_success(popen, exit_code=2)
                with self.assertRaisesRegex(RuntimeError, "終了コード 2"):
                    voucher_print_service._print_pdf_with_sumatra(b"%PDF", settings, job_name="job")
            log_files = list(debug_dir.glob("voucher_print_*.jsonl"))
            payload = json.loads(log_files[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(payload["exit_code"], 2)
        self.assertTrue(payload["worker_error"])

    def test_detect_sumatra_pdf_path_uses_candidate(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            candidate = Path(temp_dir) / "SumatraPDF.exe"
            candidate.write_text("stub", encoding="utf-8")
            with mock.patch.object(voucher_print_service, "_sumatra_candidate_paths", return_value=[candidate]):
                self.assertEqual(voucher_print_service.detect_sumatra_pdf_path(), str(candidate))

    def test_sumatra_command_has_no_acrobat_arguments(self) -> None:
        from app import voucher_print_service

        command = voucher_print_service._build_sumatra_print_command(
            r"C:\Tools\SumatraPDF.exe",
            Path(r"C:\tmp\job.pdf"),
            "Printer A",
            "noscale,monochrome,paper=auto,bin=auto,center",
        )
        for acrobat_arg in ("/n", "/s", "/o", "/h", "/t"):
            self.assertNotIn(acrobat_arg, command)

    def _run_sumatra(self, settings, popen, *, job_name: str = "job", metadata=None, debug_dir=None):
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            settings = settings._replace(sumatra_path=str(sumatra)) if hasattr(settings, "_replace") else settings
            debug = debug_dir or (Path(temp_dir) / "debug")
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "get_order_capture_debug_dir", return_value=debug), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service, "_installed_bundled_sumatra_path", return_value=sumatra), \
                    mock.patch.object(voucher_print_service, "bundled_sumatra_path", return_value=str(sumatra)), \
                    mock.patch.object(voucher_print_service, "detect_sumatra_pdf_path", return_value=str(sumatra)), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen", return_value=popen) as p:
                error = None
                try:
                    voucher_print_service._print_pdf_with_sumatra(
                        b"%PDF", settings, job_name=job_name, print_metadata=metadata
                    )
                except Exception as exc:  # noqa: BLE001
                    error = exc
                logs = list(debug.glob("voucher_print_*.jsonl"))
                payload = json.loads(logs[0].read_text(encoding="utf-8").splitlines()[-1]) if logs else {}
            return p, payload, error

    def test_request_sent_emitted_before_communicate(self) -> None:
        from app import voucher_print_service

        order: list[str] = []
        popen = mock.Mock()
        popen.pid = 4321
        popen.returncode = 0
        popen.communicate.side_effect = lambda timeout=None: order.append("communicate") or (b"", b"")

        with tempfile.TemporaryDirectory() as temp_dir:
            sumatra = Path(temp_dir) / "SumatraPDF.exe"
            sumatra.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path=str(sumatra))
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen", return_value=popen):
                voucher_print_service._print_pdf_with_sumatra(
                    b"%PDF",
                    settings,
                    job_name="job",
                    request_sent_callback=lambda payload: order.append("request_sent"),
                )
        self.assertEqual(order, ["request_sent", "communicate"])

    def test_communicate_called_with_timeout(self) -> None:
        popen = mock.Mock()
        popen.pid = 4321
        popen.returncode = 0
        popen.communicate.return_value = (b"", b"")
        settings = self._settings()
        p, payload, error = self._run_sumatra(settings, popen)
        self.assertIsNone(error)
        self.assertIn("timeout", popen.communicate.call_args.kwargs)
        self.assertEqual(popen.communicate.call_args.kwargs["timeout"], 15)
        self.assertTrue(payload["request_sent_signal_emitted"])
        self.assertTrue(payload["ui_released_after_popen"])

    def test_wait_timeout_logs_flag_and_does_not_raise(self) -> None:
        from app import voucher_print_service

        popen = mock.Mock()
        popen.pid = 4321
        popen.communicate.side_effect = voucher_print_service.subprocess.TimeoutExpired(
            cmd="SumatraPDF.exe", timeout=15
        )
        settings = self._settings()
        p, payload, error = self._run_sumatra(settings, popen)
        self.assertIsNone(error)  # タイムアウトは例外化しない（UIは復帰済み）
        self.assertTrue(payload["sumatra_wait_timeout"])
        self.assertEqual(payload["wait_timeout_seconds"], 15)
        # 既定では強制終了しない。
        popen.terminate.assert_not_called()

    def test_wait_timeout_terminates_only_when_force_kill_enabled(self) -> None:
        from app import voucher_print_service
        from app.voucher_settings import DEFAULT_SUMATRA_PRINT_SETTINGS, VoucherPrinterSettings

        popen = mock.Mock()
        popen.pid = 4321
        popen.communicate.side_effect = voucher_print_service.subprocess.TimeoutExpired(
            cmd="SumatraPDF.exe", timeout=15
        )
        settings = VoucherPrinterSettings(
            printer_name="Printer A",
            print_backend="sumatra",
            sumatra_print_settings=DEFAULT_SUMATRA_PRINT_SETTINGS,
            sumatra_allow_force_kill=True,
            print_adjustment_enabled=False,
        )
        p, payload, error = self._run_sumatra(settings, popen)
        self.assertIsNone(error)
        popen.terminate.assert_called_once()

    def test_exit_code_zero_is_success(self) -> None:
        popen = mock.Mock()
        popen.pid = 1
        popen.returncode = 0
        popen.communicate.return_value = (b"", b"")
        p, payload, error = self._run_sumatra(self._settings(), popen)
        self.assertIsNone(error)
        self.assertEqual(payload["exit_code"], 0)
        self.assertFalse(payload["worker_error"])

    def test_exit_code_4_reports_printer_not_found(self) -> None:
        popen = mock.Mock()
        popen.pid = 1
        popen.returncode = 4
        popen.communicate.return_value = (b"", b"printer error")
        p, payload, error = self._run_sumatra(self._settings(), popen)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("プリンターが見つかりません", str(error))
        self.assertEqual(payload["exit_code"], 4)

    def test_exit_code_5_reports_driver_or_device_failure(self) -> None:
        popen = mock.Mock()
        popen.pid = 1
        popen.returncode = 5
        popen.communicate.return_value = (b"", b"")
        p, payload, error = self._run_sumatra(self._settings(), popen)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("プリンタードライバーまたはデバイス", str(error))
        self.assertEqual(payload["exit_code"], 5)

    def _fail_payload(self, settings, *, detect_return: str = ""):
        """Popen前エラーを起こして最後のログ payload を返す。"""
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            debug_dir = Path(temp_dir) / "debug"
            with mock.patch.object(voucher_print_service, "get_app_data_dir", return_value=Path(temp_dir)), \
                    mock.patch.object(voucher_print_service, "get_order_capture_debug_dir", return_value=debug_dir), \
                    mock.patch.object(voucher_print_service, "_validate_saved_printer", return_value="Printer A"), \
                    mock.patch.object(voucher_print_service, "_installed_bundled_sumatra_path", return_value=Path()), \
                    mock.patch.object(voucher_print_service, "_pyinstaller_bundled_sumatra_path", return_value=Path()), \
                    mock.patch.object(voucher_print_service, "_dev_bundled_sumatra_path", return_value=Path()), \
                    mock.patch.object(voucher_print_service, "detect_sumatra_pdf_path", return_value=detect_return), \
                    mock.patch.object(voucher_print_service.subprocess, "Popen") as popen:
                error = None
                try:
                    voucher_print_service._print_pdf_with_sumatra(b"%PDF", settings, job_name="job")
                except Exception as exc:  # noqa: BLE001
                    error = exc
            payload = json.loads(
                list(debug_dir.glob("voucher_print_*.jsonl"))[0]
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            return popen, payload, error

    def test_empty_sumatra_path_does_not_popen_and_logs_worker_error_finished(self) -> None:
        settings = self._settings(sumatra_path="")
        popen, payload, error = self._fail_payload(settings, detect_return="")
        popen.assert_not_called()
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("SumatraPDFが見つかりません", str(error))
        self.assertTrue(payload["worker_error"])
        self.assertTrue(payload["worker_finished"])
        self.assertTrue(payload["worker_error_signal_emitted"])
        self.assertEqual(payload["return_reason"], "sumatra_path_missing")
        self.assertTrue(payload["sumatra_path_missing"])
        self.assertFalse(payload["popen_started"])
        self.assertFalse(payload["request_sent_signal_emitted"])
        self.assertFalse(payload["ui_released_after_popen"])
        self.assertEqual(payload["command_args"], [])

    def test_sumatra_path_not_found_return_reason(self) -> None:
        settings = self._settings(sumatra_path="/no/such/SumatraPDF.exe")
        popen, payload, error = self._fail_payload(settings, detect_return="")
        popen.assert_not_called()
        self.assertIn("セットアップを再実行", str(error))
        self.assertEqual(payload["return_reason"], "sumatra_path_not_found")
        self.assertTrue(payload["sumatra_path_not_found"])
        self.assertTrue(payload["worker_error"])
        self.assertTrue(payload["worker_finished"])

    def test_printer_name_missing_return_reason(self) -> None:
        from app.voucher_settings import DEFAULT_SUMATRA_PRINT_SETTINGS, VoucherPrinterSettings

        settings = VoucherPrinterSettings(
            printer_name="",
            print_backend="sumatra",
            sumatra_path="/tmp/SumatraPDF.exe",
            sumatra_print_settings=DEFAULT_SUMATRA_PRINT_SETTINGS,
        )
        popen, payload, error = self._fail_payload(settings, detect_return="/tmp/SumatraPDF.exe")
        popen.assert_not_called()
        self.assertIn("プリンターが設定されていません", str(error))
        self.assertEqual(payload["return_reason"], "printer_name_missing")
        self.assertTrue(payload["printer_name_missing"])
        self.assertTrue(payload["worker_error"])
        self.assertTrue(payload["worker_finished"])

    def test_sumatra_log_contains_command_args_and_process_id(self) -> None:
        popen = mock.Mock()
        popen.pid = 9876
        popen.returncode = 0
        popen.communicate.return_value = (b"", b"")
        p, payload, error = self._run_sumatra(self._settings(), popen)
        self.assertEqual(payload["process_id"], 9876)
        self.assertIn("-print-to", payload["command_args"])
        for acrobat_arg in ("/n", "/s", "/o", "/h", "/t"):
            self.assertNotIn(acrobat_arg, payload["command_args"])

    # ── 旧同梱SumatraPDFを使わない1.5.13移行関連 ─────────────────────────────
    def test_detect_sumatra_returns_installed_bundled_when_present(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            exe = Path(temp_dir) / "SumatraPDF.exe"
            exe.write_text("stub", encoding="utf-8")
            with mock.patch.object(voucher_print_service, "_bundled_sumatra_paths", return_value=[exe]):
                self.assertEqual(voucher_print_service.detect_sumatra_pdf_path(), "")

    def test_resolve_prefers_installed_bundled_over_saved(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            bundled = Path(temp_dir) / "installed" / "tools" / "SumatraPDF" / "SumatraPDF.exe"
            saved = Path(temp_dir) / "saved" / "SumatraPDF.exe"
            for p in (bundled, saved):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path=str(saved))
            with mock.patch.object(voucher_print_service, "_installed_bundled_sumatra_path", return_value=bundled), \
                    mock.patch.object(voucher_print_service, "_pyinstaller_bundled_sumatra_path", return_value=Path()), \
                    mock.patch.object(voucher_print_service, "_dev_bundled_sumatra_path", return_value=Path()):
                path, source = voucher_print_service.resolve_sumatra_executable(settings)
        self.assertEqual(path, str(saved))
        self.assertEqual(source, "saved")

    def test_resolve_prefers_installed_bundled_over_program_files(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            bundled = Path(temp_dir) / "tools" / "SumatraPDF" / "SumatraPDF.exe"
            bundled.parent.mkdir(parents=True, exist_ok=True)
            bundled.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path="")
            with mock.patch.object(voucher_print_service, "_installed_bundled_sumatra_path", return_value=bundled), \
                    mock.patch.object(voucher_print_service, "_pyinstaller_bundled_sumatra_path", return_value=Path()), \
                    mock.patch.object(voucher_print_service, "_dev_bundled_sumatra_path", return_value=Path()), \
                    mock.patch.object(
                        voucher_print_service,
                        "_detect_program_files_sumatra_path",
                        return_value=r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
                    ):
                path, source = voucher_print_service.resolve_sumatra_executable(settings)
        self.assertEqual(path, r"C:\Program Files\SumatraPDF\SumatraPDF.exe")
        self.assertEqual(source, "program_files")

    def test_resolve_returns_installed_bundled_without_dev_path(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            bundled = Path(temp_dir) / "TksToKintone" / "tools" / "SumatraPDF" / "SumatraPDF.exe"
            bundled.parent.mkdir(parents=True, exist_ok=True)
            bundled.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path="")
            with mock.patch.object(voucher_print_service, "_installed_bundled_sumatra_path", return_value=bundled), \
                    mock.patch.object(voucher_print_service, "_pyinstaller_bundled_sumatra_path", return_value=Path()), \
                    mock.patch.object(voucher_print_service, "_dev_bundled_sumatra_path", return_value=Path(temp_dir) / "missing.exe"), \
                    mock.patch.object(voucher_print_service, "_detect_program_files_sumatra_path", return_value=""):
                path, source = voucher_print_service.resolve_sumatra_executable(settings)
        self.assertEqual(path, "")
        self.assertEqual(source, "not_found")

    def test_resolve_uses_saved_when_no_bundled(self) -> None:
        from app import voucher_print_service

        with tempfile.TemporaryDirectory() as temp_dir:
            saved = Path(temp_dir) / "SumatraPDF.exe"
            saved.write_text("stub", encoding="utf-8")
            settings = self._settings(sumatra_path=str(saved))
            with mock.patch.object(voucher_print_service, "_installed_bundled_sumatra_path", return_value=Path()), \
                    mock.patch.object(voucher_print_service, "_pyinstaller_bundled_sumatra_path", return_value=Path()), \
                    mock.patch.object(voucher_print_service, "_dev_bundled_sumatra_path", return_value=Path()), \
                    mock.patch.object(voucher_print_service, "_detect_program_files_sumatra_path", return_value=""):
                path, source = voucher_print_service.resolve_sumatra_executable(settings)
        self.assertEqual(path, str(saved))
        self.assertEqual(source, "saved")

    def test_resolve_uses_program_files_when_no_bundled_or_saved(self) -> None:
        from app import voucher_print_service

        settings = self._settings(sumatra_path="")
        with mock.patch.object(voucher_print_service, "_installed_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(voucher_print_service, "_pyinstaller_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(voucher_print_service, "_dev_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(
                    voucher_print_service,
                    "_detect_program_files_sumatra_path",
                    return_value=r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
                ):
            path, source = voucher_print_service.resolve_sumatra_executable(settings)
        self.assertEqual(path, r"C:\Program Files\SumatraPDF\SumatraPDF.exe")
        self.assertEqual(source, "program_files")

    def test_resolve_not_found_when_none_available(self) -> None:
        from app import voucher_print_service

        settings = self._settings(sumatra_path="")
        with mock.patch.object(voucher_print_service, "_installed_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(voucher_print_service, "_pyinstaller_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(voucher_print_service, "_dev_bundled_sumatra_path", return_value=Path()), \
                mock.patch.object(voucher_print_service, "_detect_program_files_sumatra_path", return_value=""):
            path, source = voucher_print_service.resolve_sumatra_executable(settings)
        self.assertEqual(path, "")
        self.assertEqual(source, "not_found")

    def test_bundled_used_when_saved_empty_logs_source_and_no_error(self) -> None:
        popen = mock.Mock()
        popen.pid = 55
        popen.returncode = 0
        popen.communicate.return_value = (b"", b"")
        # _run_sumatra はインストール先同梱版を temp exe にモックし、設定パスは空。
        p, payload, error = self._run_sumatra(self._settings(sumatra_path=""), popen)
        self.assertIsNone(error)
        p.assert_called_once()
        self.assertEqual(payload["sumatra_path_source"], "installed_detected")
        self.assertEqual(payload["resolved_sumatra_path"], payload["sumatra_path"])
        self.assertFalse(payload["installed_bundled_sumatra_exists"])
        self.assertFalse(payload["bundled_sumatra_exists"])

    def test_no_not_found_error_when_bundled_exists_and_saved_empty(self) -> None:
        from app import voucher_print_service

        popen = mock.Mock()
        popen.pid = 7
        popen.returncode = 0
        popen.communicate.return_value = (b"", b"")
        p, payload, error = self._run_sumatra(self._settings(sumatra_path=""), popen)
        # 「SumatraPDFが見つかりません」エラーを出さず同梱版で印刷している。
        self.assertIsNone(error)
        self.assertFalse(payload["validation_failed"])
        self.assertNotEqual(payload["return_reason"], "sumatra_path_missing")


if __name__ == "__main__":
    unittest.main()
