from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app import update_client
from app.update_kintone_config import UpdateKintoneConfig
from app.update_client import (
    UpdateInfo,
    installer_command,
    launch_installer_for_update,
    _looks_like_installer,
    _record_to_update_info,
)


class UpdateClientTest(unittest.TestCase):
    def test_version_44_only_accepts_a_higher_version_code(self) -> None:
        def record(version_code: int) -> dict[str, object]:
            return {
                "バージョン名": {"value": f"1.6.{version_code - 44}"},
                "バージョンコード": {"value": str(version_code)},
                "APKファイル": {"value": [{"fileKey": f"key-{version_code}"}]},
            }

        response = mock.Mock()
        response.json.return_value = {"records": [record(44), record(45)]}
        connection = UpdateKintoneConfig("255", "token", "production")
        with mock.patch.object(
            update_client, "_resolved_update_kintone_config", return_value=connection
        ), mock.patch.object(update_client.requests, "get", return_value=response):
            info = update_client.UpdateClient().check_for_update(44)
            no_update = update_client.UpdateClient().check_for_update(45)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.version_code, 45)
        self.assertIsNone(no_update)

    def test_all_supported_sha256_field_codes_preserve_digest(self) -> None:
        digest = "0123456789abcdef" * 4
        for field_code in update_client.UPDATE_SHA256_FIELD_CODES:
            with self.subTest(field_code=field_code):
                info = _record_to_update_info(
                    {
                        "バージョンコード": {"value": "44"},
                        field_code: {"value": digest},
                        "APKファイル": {"value": [{"fileKey": "key"}]},
                    }
                )
                assert info is not None
                self.assertEqual(info.sha256_field_code, field_code)
                self.assertEqual(info.sha256, digest)
                self.assertEqual(info.sha256_before_update_info_length, 64)

    def test_sha256_fields_use_priority_and_skip_empty_candidates(self) -> None:
        record = {
            "バージョンコード": {"value": "44"},
            "SHA-256": {"value": ""},
            "SHA_256": {"value": "a" * 64},
            "SHA256": {"value": "b" * 64},
            "sha256": {"value": "c" * 64},
            "APKファイル": {"value": [{"fileKey": "key"}]},
        }
        info = _record_to_update_info(record)

        assert info is not None
        self.assertEqual(info.sha256_field_code, "SHA_256")
        self.assertEqual(info.sha256, "a" * 64)

    def test_record_to_update_info_reads_distribution_record(self) -> None:
        info = _record_to_update_info(
            {
                "バージョン名": {"value": "1.2.0"},
                "バージョンコード": {"value": "12"},
                "リリースノート": {"value": "更新内容"},
                "SHA-256": {"value": "a" * 64},
                "APKファイル": {"value": [{"fileKey": "abc", "name": "TksToKintone.exe", "size": "12345"}]},
            }
        )

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.version_name, "1.2.0")
        self.assertEqual(info.version_code, 12)
        self.assertEqual(info.file_key, "abc")
        self.assertEqual(info.file_size, 12345)
        self.assertEqual(info.sha256, "a" * 64)
        self.assertEqual(info.sha256_before_update_info_length, 64)
        self.assertTrue(info.sha256_source_valid)

    def test_record_sha256_and_safe_diagnostics_survive_for_both_connections(self) -> None:
        digest = "0123456789abcdef" * 4
        record = {
            "$id": {"value": "12"},
            "バージョン名": {"value": "1.5.14"},
            "バージョンコード": {"value": "44"},
            "SHA_256": {"value": digest},
            "APKファイル": {"value": [{"fileKey": "secret-key", "name": "setup.exe"}]},
        }
        for source in ("production", "debug_override"):
            info = _record_to_update_info(record, connection_source=source, app_id="255")
            assert info is not None
            self.assertEqual(info.sha256, digest)
            self.assertEqual(len(info.sha256), 64)
            diagnostic = update_client.format_sha256_diagnostic(
                info, worker_sha256_length=len(info.sha256)
            )
            self.assertIn(f"接続元: {source}", diagnostic)
            self.assertIn("UpdateInfo生成後: 64", diagnostic)
            self.assertIn("worker受渡し時: 64", diagnostic)
            self.assertNotIn(digest, diagnostic)
            self.assertNotIn("secret-key", diagnostic)

    def test_invalid_sha256_error_never_contains_digest_token_or_file_key(self) -> None:
        secret_token = "api-secret-value"
        info = UpdateInfo(
            "1.5.14", 44, "secret-file-key", "setup.exe", 1,
            sha256="bad-value", connection_source="debug_override", app_id="255",
        )
        error = update_client.InvalidUpdateSha256Error(info)
        shown = f"{error}\n{error.diagnostic}"
        self.assertNotIn("bad-value", shown)
        self.assertNotIn(secret_token, shown)
        self.assertNotIn("secret-file-key", shown)

    def test_record_to_update_info_ignores_missing_file(self) -> None:
        info = _record_to_update_info(
            {
                "バージョン名": {"value": "1.2.0"},
                "バージョンコード": {"value": "12"},
                "APKファイル": {"value": []},
            }
        )

        self.assertIsNone(info)

    def test_looks_like_installer_requires_installer_name(self) -> None:
        self.assertTrue(_looks_like_installer(Path("tks-to-kintone-setup.exe")))
        self.assertTrue(_looks_like_installer(Path("TksToKintoneInstaller.exe")))
        self.assertFalse(_looks_like_installer(Path("TksToKintone.exe")))

    def test_check_uses_resolved_update_kintone_credentials(self) -> None:
        response = mock.Mock()
        response.json.return_value = {"records": []}
        connection = UpdateKintoneConfig(
            app_id="999",
            api_token="debug-token",
            source="debug_override",
        )
        with mock.patch.object(
            update_client, "_resolved_update_kintone_config",
            return_value=connection,
        ), mock.patch.object(
            update_client.requests, "get", return_value=response
        ) as get:
            result = update_client.UpdateClient().check_for_update(43)

        self.assertIsNone(result)
        self.assertEqual(get.call_args.kwargs["params"]["app"], "999")
        self.assertEqual(
            get.call_args.kwargs["headers"]["X-Cybozu-API-Token"],
            "debug-token",
        )

    def test_check_logs_selected_record_metadata_without_sha256_or_token(self) -> None:
        sha256 = "0123456789abcdef" * 4
        api_token = "debug-token-that-must-not-be-logged"
        response = mock.Mock()
        response.json.return_value = {
            "records": [
                {
                    "$id": {"value": "12"},
                    "バージョン名": {"value": "1.5.13"},
                    "バージョンコード": {"value": "43"},
                    "SHA_256": {"value": sha256},
                    "APKファイル": {
                        "value": [
                            {
                                "fileKey": "secret-file-key",
                                "name": "TksToKintone_Setup_1.5.13.exe",
                                "size": "12345",
                            }
                        ]
                    },
                }
            ]
        }
        connection = UpdateKintoneConfig(
            app_id="255",
            api_token=api_token,
            source="debug_override",
        )
        with mock.patch.object(
            update_client, "_resolved_update_kintone_config", return_value=connection
        ), mock.patch.object(
            update_client.requests, "get", return_value=response
        ), self.assertLogs(
            "tks_to_kintone_app", level="INFO"
        ) as captured:
            result = update_client.UpdateClient().check_for_update(42)

        self.assertIsNotNone(result)
        log_output = "\n".join(captured.output)
        self.assertIn("event=update_record_selected", log_output)
        self.assertIn("source=debug_override", log_output)
        self.assertIn("app_id=255", log_output)
        self.assertIn("record_id=12", log_output)
        self.assertIn("version_name=1.5.13", log_output)
        self.assertIn("version_code=43", log_output)
        self.assertIn("file_name=TksToKintone_Setup_1.5.13.exe", log_output)
        self.assertIn("field_codes=[$id,APKファイル,SHA_256,バージョンコード,バージョン名]", log_output)
        self.assertIn("sha256_field=SHA_256", log_output)
        self.assertIn("sha256_field_present=true", log_output)
        self.assertIn("sha256_length=64", log_output)
        self.assertIn("sha256_valid=true", log_output)
        self.assertIn("before_update_info_length=64", log_output)
        self.assertIn("update_info_sha256_length=64", log_output)
        self.assertNotIn(sha256, log_output)
        self.assertNotIn(api_token, log_output)
        self.assertNotIn("secret-file-key", log_output)

    def test_check_logs_missing_sha256_field_for_selected_record(self) -> None:
        response = mock.Mock()
        response.json.return_value = {
            "records": [
                {
                    "$id": {"value": "13"},
                    "バージョン名": {"value": "1.5.14"},
                    "バージョンコード": {"value": "44"},
                    "APKファイル": {
                        "value": [{"fileKey": "key", "name": "setup.exe"}]
                    },
                }
            ]
        }
        connection = UpdateKintoneConfig("250", "production-token", "production")
        with mock.patch.object(
            update_client, "_resolved_update_kintone_config", return_value=connection
        ), mock.patch.object(
            update_client.requests, "get", return_value=response
        ), self.assertLogs(
            "tks_to_kintone_app", level="INFO"
        ) as captured:
            update_client.UpdateClient().check_for_update(43)

        log_output = "\n".join(captured.output)
        self.assertIn("sha256_field_present=false", log_output)
        self.assertIn("sha256_length=0", log_output)
        self.assertIn("sha256_valid=false", log_output)
        self.assertNotIn("production-token", log_output)

    def test_debug_connection_failure_does_not_retry_production_or_leak_token(self) -> None:
        secret = "debug-secret-that-must-not-leak"
        response = mock.Mock()
        response.raise_for_status.side_effect = update_client.requests.HTTPError(
            f"401 unauthorized {secret}"
        )
        connection = UpdateKintoneConfig(
            app_id="999",
            api_token=secret,
            source="debug_override",
        )
        with mock.patch.object(
            update_client, "_resolved_update_kintone_config",
            return_value=connection,
        ) as resolve, mock.patch.object(
            update_client.requests, "get", return_value=response
        ) as get, self.assertLogs(
            "tks_to_kintone_app", level="WARNING"
        ) as captured:
            with self.assertRaises(RuntimeError) as raised:
                update_client.UpdateClient().check_for_update(43)

        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(get.call_count, 1)
        self.assertIn("デバッグ接続先", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, "\n".join(captured.output))

    def test_download_uses_same_resolved_debug_connection(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        payload = b"MZinstaller-bytes"
        response.iter_content.return_value = [payload]
        response.headers = {"Content-Length": str(len(payload)), "Content-Type": "application/octet-stream"}
        connection = UpdateKintoneConfig(
            app_id="999",
            api_token="download-debug-token",
            source="debug_override",
        )
        info = UpdateInfo(
            version_name="1.5.14",
            version_code=44,
            file_key="file-key",
            file_name="tks-to-kintone-setup.exe",
            file_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        with TemporaryDirectory() as temp_dir, mock.patch.object(
            update_client, "_resolved_update_kintone_config",
            return_value=connection,
        ), mock.patch.object(
            update_client.requests, "get", return_value=response
        ) as get:
            path = update_client.download_installer(info, Path(temp_dir))
            self.assertTrue(path.exists())
            self.assertEqual(
                get.call_args.kwargs["headers"]["X-Cybozu-API-Token"],
                "download-debug-token",
            )

    def test_download_reports_real_bytes_and_publishes_only_after_verification(self) -> None:
        chunks = [b"MZab", b"cdef", b"ghij"]
        payload = b"".join(chunks)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = chunks
        response.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": "application/octet-stream",
        }
        info = UpdateInfo(
            "1.5.14",
            44,
            "key",
            "tks-to-kintone-setup.exe",
            len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        progress: list[tuple[int, int]] = []
        with TemporaryDirectory() as temp_dir, mock.patch.object(
            update_client.requests, "get", return_value=response
        ), self.assertLogs(
            "tks_to_kintone_app", level="INFO"
        ) as captured:
            target = update_client.prepare_installer(
                info,
                Path(temp_dir),
                progress_callback=lambda received, total: progress.append(
                    (received, total)
                ),
            )
            self.assertEqual(progress, [(4, 12), (8, 12), (12, 12)])
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(target.with_name(target.name + ".part").exists())
            log_output = "\n".join(captured.output)
            self.assertIn("event=update_sha256_worker_handoff", log_output)
            self.assertIn("update_info_sha256_length=64", log_output)
            self.assertIn("worker_sha256_length=64", log_output)
            self.assertNotIn(info.sha256, log_output)

    def test_cancel_removes_partial_file_and_never_verifies(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"MZfirst", b"second"]
        response.headers = {}
        info = UpdateInfo(
            "1.5.14",
            44,
            "key",
            "tks-to-kintone-setup.exe",
            0,
            sha256="a" * 64,
        )
        progress_calls = 0

        def cancelled() -> bool:
            return progress_calls >= 1

        def progress(_received: int, _total: int) -> None:
            nonlocal progress_calls
            progress_calls += 1

        with TemporaryDirectory() as temp_dir, mock.patch.object(
            update_client.requests, "get", return_value=response
        ):
            target = Path(temp_dir) / info.file_name
            with self.assertRaises(update_client.UpdateCancelled):
                update_client.prepare_installer(
                    info,
                    Path(temp_dir),
                    progress_callback=progress,
                    cancel_check=cancelled,
                )
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name(target.name + ".part").exists())

    def test_rejects_html_size_and_sha_mismatches(self) -> None:
        cases = [
            (b"<html>error</html>", {"Content-Type": "text/html"}, 0, "HTML"),
            (b"MZshort", {"Content-Length": "100"}, 0, "Content-Length"),
            (b"MZpayload", {}, 20, "サイズ"),
            (b"MZpayload", {}, 9, "SHA-256"),
        ]
        for payload, headers, size, message in cases:
            with self.subTest(message=message), TemporaryDirectory() as temp_dir:
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.iter_content.return_value = [payload]
                response.headers = headers
                info = UpdateInfo(
                    "1.5.14",
                    44,
                    "key",
                    "tks-to-kintone-setup.exe",
                    size,
                    sha256="0" * 64,
                )
                with mock.patch.object(
                    update_client.requests, "get", return_value=response
                ):
                    with self.assertRaisesRegex(RuntimeError, message):
                        update_client.prepare_installer(info, Path(temp_dir))

    def test_missing_sha256_is_rejected_before_network_request(self) -> None:
        info = UpdateInfo(
            "1.5.14", 44, "key", "tks-to-kintone-setup.exe", 10
        )
        with TemporaryDirectory() as temp_dir, mock.patch.object(
            update_client.requests, "get"
        ) as get:
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                update_client.prepare_installer(info, Path(temp_dir))
            get.assert_not_called()


class InstallerCommandTest(unittest.TestCase):
    def test_installer_command_passes_silent_arguments_and_log_path(self) -> None:
        command = installer_command(
            Path("C:/updates/tks-to-kintone-setup.exe"),
            Path("C:/ProgramData/Manekiya/TksToKintone/logs/update_installer.log"),
        )

        self.assertEqual(command[0], "C:/updates/tks-to-kintone-setup.exe")
        self.assertIn("/SILENT", command)
        self.assertNotIn("/VERYSILENT", command)
        self.assertIn("/SUPPRESSMSGBOXES", command)
        self.assertIn("/NORESTART", command)
        self.assertIn("/SP-", command)
        self.assertIn("/RELAUNCHAPP=1", command)
        self.assertIn("/LOG=C:/ProgramData/Manekiya/TksToKintone/logs/update_installer.log", command)

    def test_installer_command_does_not_invoke_powershell(self) -> None:
        command = installer_command(Path("C:/updates/setup.exe"), Path("C:/logs/update_installer.log"))
        joined = " ".join(command).lower()
        self.assertNotIn("powershell", joined)
        self.assertNotIn("executionpolicy", joined)
        self.assertNotIn(".ps1", joined)

    def test_windows_setup_launch_uses_open_not_explicit_runas(self) -> None:
        source = Path("app/update_client.py").read_text(encoding="utf-8")
        self.assertIn('info.lpVerb = "open"', source)
        self.assertNotIn('info.lpVerb = "runas"', source)
        installer = Path("installer/tks-to-kintone.iss").read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=admin", installer)


class LaunchInstallerForUpdateTest(unittest.TestCase):
    def _patch_start_installer(self):
        calls: list[list[str]] = []

        def fake_start(command: list[str], log_path: Path) -> int:
            calls.append(command)
            self.assertIn(str(log_path), command[-1])
            return 555

        original_start = update_client._start_installer_process
        update_client._start_installer_process = fake_start  # type: ignore[assignment]
        return calls, original_start

    def test_launch_installer_starts_process_when_installer_exists(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            installer = base / "tks-to-kintone-setup.exe"
            installer.write_bytes(b"installer")
            app_exe = base / "tks-to-kintone.exe"
            app_exe.write_bytes(b"app")

            calls, original_start = self._patch_start_installer()
            try:
                started = launch_installer_for_update(installer, app_exe, log_dir=base / "logs")
            finally:
                update_client._start_installer_process = original_start  # type: ignore[assignment]

            self.assertTrue(started)
            self.assertEqual(len(calls), 1)
            command = calls[0]
            self.assertEqual(command[0], str(installer))
            self.assertIn("/SILENT", command)
            self.assertNotIn("/VERYSILENT", command)
            self.assertIn("/RELAUNCHAPP=1", command)
            self.assertRegex(
                command[-1],
                rf"^/LOG={base / 'logs'}/update_installer_\d{{8}}_\d{{6}}_\d+_[0-9a-f]{{8}}\.log$",
            )
            joined = " ".join(command).lower()
            self.assertNotIn("powershell", joined)
            self.assertNotIn("tks_update_helper", joined)

    def test_launch_installer_aborts_when_installer_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            installer = base / "missing-setup.exe"
            app_exe = base / "tks-to-kintone.exe"
            app_exe.write_bytes(b"app")

            calls, original_start = self._patch_start_installer()
            try:
                started = launch_installer_for_update(installer, app_exe, log_dir=base / "logs")
            finally:
                update_client._start_installer_process = original_start  # type: ignore[assignment]

            self.assertFalse(started)
            self.assertEqual(calls, [])

    def test_launch_installer_returns_false_on_start_error(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            installer = base / "tks-to-kintone-setup.exe"
            installer.write_bytes(b"installer")
            app_exe = base / "tks-to-kintone.exe"
            app_exe.write_bytes(b"app")

            def _raise(*_a: object, **_k: object):
                raise OSError("起動失敗")

            original_start = update_client._start_installer_process
            update_client._start_installer_process = _raise  # type: ignore[assignment]
            try:
                started = launch_installer_for_update(installer, app_exe, log_dir=base / "logs")
            finally:
                update_client._start_installer_process = original_start  # type: ignore[assignment]

            self.assertFalse(started)


class StartInstallerProcessTest(unittest.TestCase):
    def test_start_installer_process_uses_subprocess_popen_directly(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        class _FakePopen:
            pid = 777

            def __init__(self, command: list[str], **kwargs: object) -> None:
                calls.append((command, kwargs))

            def poll(self) -> None:
                return None

        original_popen = update_client.subprocess.Popen
        update_client.subprocess.Popen = _FakePopen  # type: ignore[assignment]
        try:
            log_path = Path("C:/logs/update_installer.log")
            with mock.patch.object(update_client, "_wait_for_setup_log") as wait:
                pid = update_client._start_installer_process(
                    [
                        "C:/updates/tks-to-kintone-setup.exe",
                        "/SILENT",
                        "/SUPPRESSMSGBOXES",
                        "/NORESTART",
                        "/SP-",
                        "/LOG=C:/logs/update_installer.log",
                    ],
                    log_path,
                )
            wait.assert_called_once()
        finally:
            update_client.subprocess.Popen = original_popen  # type: ignore[assignment]

        self.assertEqual(pid, 777)
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(command[0], "C:/updates/tks-to-kintone-setup.exe")
        self.assertIn("/SILENT", command)
        self.assertNotIn("/VERYSILENT", command)
        self.assertTrue(kwargs["close_fds"])
        joined = " ".join(command).lower()
        self.assertNotIn("powershell", joined)
        self.assertNotIn(".ps1", joined)
        self.assertNotIn(".bat", joined)
        self.assertNotIn(".cmd", joined)


class InstallerLaunchConfirmationTest(unittest.TestCase):
    def test_shell_execute_false_is_failure(self) -> None:
        with self.assertRaises(OSError):
            update_client._validate_shell_execute_result(False, 5, 0, False)

    def test_uac_rejection_is_failure(self) -> None:
        with self.assertRaises(update_client.ElevationCancelled):
            update_client._validate_shell_execute_result(False, 1223, 0, False)

    def test_true_without_process_handle_is_failure(self) -> None:
        with self.assertRaises(update_client.InstallerLaunchError):
            update_client._validate_shell_execute_result(True, 0, 42, False)

    def test_hinstapp_must_be_greater_than_32(self) -> None:
        with self.assertRaises(update_client.InstallerLaunchError):
            update_client._validate_shell_execute_result(True, 0, 32, True)

    def test_valid_shell_result_requires_handle(self) -> None:
        update_client._validate_shell_execute_result(True, 0, 42, True)

    def test_setup_log_must_be_nonempty(self) -> None:
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "attempt.log"
            log_path.touch()
            with mock.patch.object(
                update_client.time, "monotonic", side_effect=[0.0, 1.0]
            ):
                with self.assertRaises(update_client.InstallerLaunchError):
                    update_client._wait_for_setup_log(
                        log_path, lambda: False, timeout_seconds=0.5
                    )

    def test_process_exit_before_log_is_failure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                update_client.InstallerLaunchError, "ログを作成する前"
            ):
                update_client._wait_for_setup_log(
                    Path(temp_dir) / "attempt.log", lambda: True
                )

    def test_only_current_nonempty_log_confirms_start(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / "update_installer.log").write_text("old", encoding="utf-8")
            current = base / "update_installer_current.log"
            current.write_text("new", encoding="utf-8")
            update_client._wait_for_setup_log(current, lambda: False)

    def test_unique_log_path_never_uses_fixed_name(self) -> None:
        with TemporaryDirectory() as temp_dir:
            first = update_client.installer_log_path(Path(temp_dir))
            second = update_client.installer_log_path(Path(temp_dir))
            self.assertNotEqual(first, second)
            self.assertNotEqual(first.name, "update_installer.log")

    def test_windows_source_sets_masks_and_worker_com_lifecycle(self) -> None:
        source = Path("app/update_client.py").read_text(encoding="utf-8")
        self.assertIn("SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC", source)
        self.assertIn("ole32.CoInitializeEx(None, 0x2 | 0x4)", source)
        self.assertIn("ole32.CoUninitialize()", source)


class UpdateClientSourceTest(unittest.TestCase):
    """外部更新スクリプトの旧実装が完全に廃止されていることを検証する。"""

    SOURCE = Path("app/update_client.py").read_text(encoding="utf-8")
    HELPER_SOURCE = Path("app/update_helper.py").read_text(encoding="utf-8")
    BUILD_EXE_SOURCE = Path("build_exe.bat").read_text(encoding="utf-8")
    INNO_SOURCE = Path("installer/tks-to-kintone.iss").read_text(encoding="utf-8")

    def test_update_does_not_require_authenticode(self) -> None:
        self.assertNotIn("verify_authenticode_signature", self.SOURCE)
        self.assertNotIn("AuthenticodeVerificationError", self.SOURCE)
        self.assertNotIn("--authenticode-verify-helper", self.SOURCE)

    def test_no_external_script_generation(self) -> None:
        self.assertNotIn("write_text", self.SOURCE)
        self.assertNotIn("_create_external_update_script", self.SOURCE)
        self.assertNotIn("subprocess.Popen([\"powershell", self.SOURCE)
        self.assertNotIn("subprocess.Popen(['powershell", self.SOURCE)

    def test_update_client_does_not_reference_helper_exe(self) -> None:
        self.assertNotIn("UPDATE_HELPER_EXE_NAME", self.SOURCE)
        self.assertNotIn("launch_update_helper", self.SOURCE)
        self.assertNotIn("tks_update_runner", self.SOURCE)

    def test_build_keeps_optional_signing_and_installer_layout(self) -> None:
        self.assertIn('Code signing not configured; continuing with an unsigned build.', self.BUILD_EXE_SOURCE)
        self.assertIn('#define MyOutputBaseFilename "tks-to-kintone-setup"', self.INNO_SOURCE)
        self.assertIn('OutputBaseFilename={#MyOutputBaseFilename}', self.INNO_SOURCE)
        self.assertIn('[InstallDelete]', self.INNO_SOURCE)
        self.assertIn('{app}\\tks_update_helper.exe', self.INNO_SOURCE)

    def test_release_setup_is_hashed_without_mandatory_signing(self) -> None:
        compile_at = self.BUILD_EXE_SOURCE.index('installer\\tks-to-kintone.iss')
        hash_at = self.BUILD_EXE_SOURCE.index('Computing SHA-256 for update verification')
        self.assertLess(compile_at, hash_at)
        self.assertNotIn('--authenticode-verify-helper', self.BUILD_EXE_SOURCE)
        self.assertNotIn('verify /pa /v', self.BUILD_EXE_SOURCE)
        self.assertNotIn('MANEKIYA GLASS CORPORATION TksToKintone Test', self.BUILD_EXE_SOURCE)
        self.assertIn('/sha1 "%SIGN_CERT_THUMBPRINT%"', self.BUILD_EXE_SOURCE)
        self.assertIn('/n "%SIGN_CERT_SUBJECT%"', self.BUILD_EXE_SOURCE)


if __name__ == "__main__":
    unittest.main()
