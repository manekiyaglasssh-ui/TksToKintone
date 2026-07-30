from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app import update_client
from app.update_client import UpdateInfo
from app.update_progress import UpdateProgressDialog, UpdateWorker


class Sha256FileIntegrationTest(unittest.TestCase):
    def test_240_mb_real_file_hash_reaches_eof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large-setup.exe.part"
            block = b"MZ" + bytes(1024 * 1024 - 2)
            digest = hashlib.sha256()
            with path.open("wb") as handle:
                for _ in range(240):
                    handle.write(block)
                    digest.update(block)
            expected = digest.hexdigest()
            self.assertEqual(update_client._verify_sha256_file(path, expected), expected)

    def test_empty_read_terminates_finite_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty"
            path.write_bytes(b"")
            expected = hashlib.sha256(b"").hexdigest()
            self.assertEqual(update_client._verify_sha256_file(path, expected), expected)

    def test_sha256_logs_started_progress_and_finished_without_full_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "setup.exe.part"
            path.write_bytes(b"MZ" + bytes(2 * 1024 * 1024))
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertLogs("tks_to_kintone_app", logging.INFO) as captured:
                update_client._verify_sha256_file(path, expected)
            output = "\n".join(captured.output)
            for event in (
                "update_verify_sha256_started",
                "update_verify_sha256_progress",
                "update_verify_sha256_finished",
            ):
                self.assertIn(f"event={event}", output)
            self.assertNotIn(expected, output)

    def test_download_handles_are_closed_before_hash_verification(self) -> None:
        payload = b"MZpayload"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [payload]
        response.headers = {"Content-Length": str(len(payload))}
        response_closed_when_hash_started: list[bool] = []
        original_verify = update_client._verify_sha256_file

        def verify(path: Path, expected: str) -> str:
            response_closed_when_hash_started.append(response.__exit__.called)
            # Reopening for append proves the download writer no longer owns it.
            with path.open("ab"):
                pass
            return original_verify(path, expected)

        info = UpdateInfo("1.5.14", 44, "key", "setup.exe", len(payload), sha256=hashlib.sha256(payload).hexdigest())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            update_client.requests, "get", return_value=response
        ), mock.patch.object(update_client, "_verify_sha256_file", side_effect=verify):
            update_client.prepare_installer(info, Path(directory))
        self.assertEqual(response_closed_when_hash_started, [True])


class WorkerTerminalGuaranteeTest(unittest.TestCase):
    def _run_failure(self, exc: Exception) -> list[str]:
        worker = UpdateWorker(
            UpdateInfo("1.5.14", 44, "key", "setup.exe", 10, sha256="a" * 64),
            Path("updates"),
        )
        order: list[str] = []
        worker.failed.connect(lambda *_: order.append("failure"))
        worker.terminal.connect(lambda *_: order.append("terminal"))
        with mock.patch.object(update_client, "prepare_installer", side_effect=exc):
            worker.run()
        return order

    def test_sha256_exception_emits_one_failure_then_terminal(self) -> None:
        self.assertEqual(self._run_failure(OSError("hash read failed")), ["failure", "terminal"])

    def test_invalid_expected_hash_never_stops_silently(self) -> None:
        self.assertEqual(self._run_failure(update_client.InvalidUpdateSha256Error(
            UpdateInfo("1.5.14", 44, "key", "setup.exe", 10, sha256="bad")
        )), ["failure", "terminal"])

    def test_pe_exception_emits_one_failure_then_terminal(self) -> None:
        self.assertEqual(self._run_failure(RuntimeError("invalid PE")), ["failure", "terminal"])


class ProgressRetentionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_download_100_percent_keeps_dialog_open_and_changes_stage(self) -> None:
        dialog = UpdateProgressDialog()
        dialog.show()
        dialog.show_progress(10, 10)
        self.assertTrue(dialog.isVisible())
        dialog.show_stage("ダウンロード完了・ファイル確認中")
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.progress_bar.value(), 100)
        self.assertEqual(dialog.status_label.text(), "ダウンロード完了・ファイル確認中")
        dialog._failure_close_enabled = True
        dialog.close()


if __name__ == "__main__":
    unittest.main()
