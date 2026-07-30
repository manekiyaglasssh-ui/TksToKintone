from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import update_helper


class _FakeProcess:
    def __init__(self, command: list[str], returncode: int = 0) -> None:
        self.command = command
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


class UpdateHelperTest(unittest.TestCase):
    def _patch_popen(self, returncode: int = 0):
        calls: list[list[str]] = []

        import subprocess

        original = subprocess.Popen

        def fake_popen(command: list[str], **_kwargs: object) -> _FakeProcess:
            calls.append(command)
            return _FakeProcess(command, returncode)

        subprocess.Popen = fake_popen  # type: ignore[assignment]
        return calls, original, subprocess

    def test_run_update_launches_installer_then_restarts_app(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            installer = base / "tks-to-kintone-setup.exe"
            installer.write_bytes(b"installer")
            app_exe = base / "tks-to-kintone.exe"
            app_exe.write_bytes(b"app")

            calls, original, subprocess = self._patch_popen(returncode=0)
            try:
                # parent_pid=0 で親終了待ちをスキップする。
                result = update_helper.run_update(installer, app_exe, 0)
            finally:
                subprocess.Popen = original  # type: ignore[assignment]

            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0], str(installer))
            self.assertEqual(calls[1][0], str(app_exe))

    def test_run_update_missing_installer_returns_error(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            installer = base / "missing-setup.exe"
            app_exe = base / "tks-to-kintone.exe"
            app_exe.write_bytes(b"app")

            result = update_helper.run_update(installer, app_exe, 0)
            self.assertEqual(result, 2)

    def test_run_update_does_not_restart_when_installer_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            installer = base / "tks-to-kintone-setup.exe"
            installer.write_bytes(b"installer")
            app_exe = base / "tks-to-kintone.exe"
            app_exe.write_bytes(b"app")

            calls, original, subprocess = self._patch_popen(returncode=1)
            try:
                result = update_helper.run_update(installer, app_exe, 0)
            finally:
                subprocess.Popen = original  # type: ignore[assignment]

            self.assertEqual(result, 1)
            # インストーラ起動のみ。再起動は行わない。
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], str(installer))

    def test_run_update_aborts_when_parent_does_not_exit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            installer = base / "tks-to-kintone-setup.exe"
            installer.write_bytes(b"installer")
            app_exe = base / "tks-to-kintone.exe"
            app_exe.write_bytes(b"app")

            original_alive = update_helper._process_alive
            original_timeout = update_helper.PARENT_WAIT_TIMEOUT_SECONDS
            original_interval = update_helper.POLL_INTERVAL_SECONDS
            update_helper._process_alive = lambda pid: True  # type: ignore[assignment]
            update_helper.PARENT_WAIT_TIMEOUT_SECONDS = 0.05
            update_helper.POLL_INTERVAL_SECONDS = 0.01

            calls, original, subprocess = self._patch_popen(returncode=0)
            try:
                result = update_helper.run_update(installer, app_exe, 4321, log_dir=base)
            finally:
                subprocess.Popen = original  # type: ignore[assignment]
                update_helper._process_alive = original_alive  # type: ignore[assignment]
                update_helper.PARENT_WAIT_TIMEOUT_SECONDS = original_timeout
                update_helper.POLL_INTERVAL_SECONDS = original_interval

            # 親が終了しないのでインストーラを起動せず中止する。
            self.assertEqual(result, 3)
            self.assertEqual(calls, [])
            log_text = (base / update_helper.HELPER_LOG_NAME).read_text(encoding="utf-8")
            self.assertIn("parent process did not exit within timeout", log_text)

    def test_run_update_writes_log_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            installer = base / "tks-to-kintone-setup.exe"
            installer.write_bytes(b"installer")
            app_exe = base / "tks-to-kintone.exe"
            app_exe.write_bytes(b"app")

            calls, original, subprocess = self._patch_popen(returncode=0)
            try:
                update_helper.run_update(installer, app_exe, 0, log_dir=base)
            finally:
                subprocess.Popen = original  # type: ignore[assignment]

            log_text = (base / update_helper.HELPER_LOG_NAME).read_text(encoding="utf-8")
            for expected in (
                "helper started",
                "helper_executable_path",
                "current_working_directory",
                "parent_pid",
                "installer_path",
                "app_exe_path",
                "waiting parent process",
                "parent process exited",
                "installer exists true",
                "installer start",
                "installer exit code",
                "app restart",
                "helper finished",
            ):
                self.assertIn(expected, log_text)

    def test_run_update_silent_passes_silent_args(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            installer = base / "tks-to-kintone-setup.exe"
            installer.write_bytes(b"installer")
            app_exe = base / "tks-to-kintone.exe"
            app_exe.write_bytes(b"app")

            calls, original, subprocess = self._patch_popen(returncode=0)
            try:
                update_helper.run_update(installer, app_exe, 0, silent=True, log_dir=base)
            finally:
                subprocess.Popen = original  # type: ignore[assignment]

            installer_call = calls[0]
        self.assertIn("/SILENT", installer_call)
        self.assertNotIn("/VERYSILENT", installer_call)

    def test_run_update_normal_does_not_pass_silent_args(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            installer = base / "tks-to-kintone-setup.exe"
            installer.write_bytes(b"installer")
            app_exe = base / "tks-to-kintone.exe"
            app_exe.write_bytes(b"app")

            calls, original, subprocess = self._patch_popen(returncode=0)
            try:
                update_helper.run_update(installer, app_exe, 0, silent=False, log_dir=base)
            finally:
                subprocess.Popen = original  # type: ignore[assignment]

            installer_call = calls[0]
            self.assertEqual(installer_call, [str(installer)])

    def test_main_parses_required_arguments(self) -> None:
        captured: dict[str, object] = {}

        original = update_helper.run_update

        def fake_run_update(
            installer_path: Path,
            app_exe_path: Path,
            parent_pid: int,
            silent: bool = False,
            log_dir: Path | None = None,
        ) -> int:
            captured["installer"] = installer_path
            captured["app"] = app_exe_path
            captured["pid"] = parent_pid
            captured["silent"] = silent
            return 0

        update_helper.run_update = fake_run_update  # type: ignore[assignment]
        try:
            result = update_helper.main(
                [
                    "--installer-path",
                    "C:/updates/tks-to-kintone-setup.exe",
                    "--app-exe-path",
                    "C:/app/tks-to-kintone.exe",
                    "--parent-pid",
                    "999",
                ]
            )
        finally:
            update_helper.run_update = original  # type: ignore[assignment]

        self.assertEqual(result, 0)
        self.assertEqual(captured["installer"], Path("C:/updates/tks-to-kintone-setup.exe"))
        self.assertEqual(captured["app"], Path("C:/app/tks-to-kintone.exe"))
        self.assertEqual(captured["pid"], 999)
        self.assertFalse(captured["silent"])

    def test_main_silent_flag_enables_silent(self) -> None:
        captured: dict[str, object] = {}

        original = update_helper.run_update

        def fake_run_update(installer_path: Path, app_exe_path: Path, parent_pid: int, silent: bool = False, log_dir: Path | None = None) -> int:
            captured["silent"] = silent
            return 0

        update_helper.run_update = fake_run_update  # type: ignore[assignment]
        try:
            update_helper.main(
                [
                    "--installer-path",
                    "C:/updates/setup.exe",
                    "--app-exe-path",
                    "C:/app/app.exe",
                    "--silent",
                ]
            )
        finally:
            update_helper.run_update = original  # type: ignore[assignment]

        self.assertTrue(captured["silent"])


if __name__ == "__main__":
    unittest.main()
