from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import update_client
from app.update_client import (
    UpdateInfo,
    cleanup_stale_update_script,
    installer_command,
    launch_installer_for_update,
    _looks_like_installer,
    _record_to_update_info,
)


class UpdateClientTest(unittest.TestCase):
    def test_record_to_update_info_reads_distribution_record(self) -> None:
        info = _record_to_update_info(
            {
                "バージョン名": {"value": "1.2.0"},
                "バージョンコード": {"value": "12"},
                "リリースノート": {"value": "更新内容"},
                "APKファイル": {"value": [{"fileKey": "abc", "name": "TksToKintone.exe", "size": "12345"}]},
            }
        )

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.version_name, "1.2.0")
        self.assertEqual(info.version_code, 12)
        self.assertEqual(info.file_key, "abc")
        self.assertEqual(info.file_size, 12345)

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


class InstallerCommandTest(unittest.TestCase):
    def test_installer_command_passes_silent_arguments_and_log_path(self) -> None:
        command = installer_command(
            Path("C:/updates/tks-to-kintone-setup.exe"),
            Path("C:/ProgramData/Manekiya/TksToKintone/logs/update_installer.log"),
        )

        self.assertEqual(command[0], "C:/updates/tks-to-kintone-setup.exe")
        self.assertIn("/VERYSILENT", command)
        self.assertIn("/SUPPRESSMSGBOXES", command)
        self.assertIn("/NORESTART", command)
        self.assertIn("/SP-", command)
        self.assertIn("/LOG=C:/ProgramData/Manekiya/TksToKintone/logs/update_installer.log", command)

    def test_installer_command_does_not_invoke_powershell(self) -> None:
        command = installer_command(Path("C:/updates/setup.exe"), Path("C:/logs/update_installer.log"))
        joined = " ".join(command).lower()
        self.assertNotIn("powershell", joined)
        self.assertNotIn("executionpolicy", joined)
        self.assertNotIn(".ps1", joined)


class LaunchInstallerForUpdateTest(unittest.TestCase):
    def _patch_start_installer(self):
        calls: list[list[str]] = []

        def fake_start(command: list[str]) -> int:
            calls.append(command)
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
            self.assertIn("/VERYSILENT", command)
            self.assertIn(f"/LOG={base / 'logs' / 'update_installer.log'}", command)
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

        original_popen = update_client.subprocess.Popen
        update_client.subprocess.Popen = _FakePopen  # type: ignore[assignment]
        try:
            pid = update_client._start_installer_process(
                [
                    "C:/updates/tks-to-kintone-setup.exe",
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    "/SP-",
                    "/LOG=C:/logs/update_installer.log",
                ]
            )
        finally:
            update_client.subprocess.Popen = original_popen  # type: ignore[assignment]

        self.assertEqual(pid, 777)
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(command[0], "C:/updates/tks-to-kintone-setup.exe")
        self.assertIn("/VERYSILENT", command)
        self.assertTrue(kwargs["close_fds"])
        self.assertIn("creationflags", kwargs)
        joined = " ".join(command).lower()
        self.assertNotIn("powershell", joined)
        self.assertNotIn(".ps1", joined)
        self.assertNotIn(".bat", joined)
        self.assertNotIn(".cmd", joined)


class CleanupStaleUpdateScriptTest(unittest.TestCase):
    def test_cleanup_stale_update_script_deletes_old_ps1(self) -> None:
        with TemporaryDirectory() as temp_dir:
            update_dir = Path(temp_dir)
            stale_script = update_dir / "run_update.ps1"
            stale_script.write_text("old", encoding="utf-8")

            cleanup_stale_update_script(update_dir)

            self.assertFalse(stale_script.exists())


class UpdateClientSourceTest(unittest.TestCase):
    """外部更新スクリプトの旧実装が完全に廃止されていることを検証する。"""

    SOURCE = Path("app/update_client.py").read_text(encoding="utf-8")
    HELPER_SOURCE = Path("app/update_helper.py").read_text(encoding="utf-8")
    BUILD_EXE_SOURCE = Path("build_exe.bat").read_text(encoding="utf-8")
    INNO_SOURCE = Path("installer/tks-to-kintone.iss").read_text(encoding="utf-8")

    def test_no_powershell_invocation(self) -> None:
        for forbidden in ("powershell", "ExecutionPolicy", "Bypass"):
            self.assertNotIn(forbidden, self.SOURCE)
            self.assertNotIn(forbidden, self.HELPER_SOURCE)

    def test_no_external_script_generation(self) -> None:
        self.assertNotIn("write_text", self.SOURCE)
        self.assertNotIn("_create_external_update_script", self.SOURCE)
        self.assertNotIn("subprocess.Popen([\"powershell", self.SOURCE)
        self.assertNotIn("subprocess.Popen(['powershell", self.SOURCE)

    def test_update_client_does_not_reference_helper_exe(self) -> None:
        self.assertNotIn("UPDATE_HELPER_EXE_NAME", self.SOURCE)
        self.assertNotIn("launch_update_helper", self.SOURCE)
        self.assertNotIn("tks_update_runner", self.SOURCE)

    def test_official_build_does_not_build_or_bundle_helper(self) -> None:
        self.assertIn('if /I "%BUILD_VARIANT%"=="with-helper"', self.BUILD_EXE_SOURCE)
        self.assertIn('#define MyOutputBaseFilename "tks-to-kintone-setup"', self.INNO_SOURCE)
        self.assertIn('OutputBaseFilename={#MyOutputBaseFilename}', self.INNO_SOURCE)
        self.assertIn('[InstallDelete]', self.INNO_SOURCE)
        self.assertIn('{app}\\tks_update_helper.exe', self.INNO_SOURCE)


if __name__ == "__main__":
    unittest.main()
