from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app import update_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeFunction:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


class _FakeDll:
    def __init__(self, **functions):
        for name, implementation in functions.items():
            setattr(self, name, _FakeFunction(implementation))


def _windows_dlls(*, shell_execute_ok: bool):
    calls: list[str] = []

    def shell_execute(info_pointer):
        calls.append("ShellExecuteExW")
        info = info_pointer._obj
        if shell_execute_ok:
            info.hInstApp = 33
            info.hProcess = 123
            return 1
        return 0

    dlls = {
        "ole32": _FakeDll(
            CoInitializeEx=lambda *_args: 0,
            CoUninitialize=lambda: None,
        ),
        "kernel32": _FakeDll(
            GetProcessId=lambda _handle: 456,
            WaitForSingleObject=lambda *_args: 1,
            CloseHandle=lambda _handle: 1,
        ),
        "shell32": _FakeDll(ShellExecuteExW=shell_execute),
    }
    return calls, lambda name, **_kwargs: dlls[name]


class CtypesImportRegressionTest(unittest.TestCase):
    def test_every_ctypes_reference_has_direct_module_import(self) -> None:
        failures: list[str] = []
        for path in sorted((PROJECT_ROOT / "app").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "ctypes." not in source and "wintypes." not in source:
                continue
            tree = ast.parse(source, filename=str(path))
            imports_ctypes = any(
                isinstance(node, ast.Import)
                and any(alias.name == "ctypes" for alias in node.names)
                for node in ast.walk(tree)
            )
            imports_wintypes = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "ctypes"
                and any(alias.name == "wintypes" for alias in node.names)
                for node in ast.walk(tree)
            )
            if "ctypes." in source and not imports_ctypes:
                failures.append(f"{path.name}: import ctypes")
            if "wintypes." in source and not imports_wintypes:
                failures.append(f"{path.name}: from ctypes import wintypes")
        self.assertEqual(failures, [])

    def test_update_client_imports_standalone_like_frozen_module(self) -> None:
        module = importlib.reload(update_client)
        self.assertIsNotNone(module.ctypes)
        self.assertIsNotNone(module.wintypes)


class WindowsInstallerCtypesRegressionTest(unittest.TestCase):
    def _run_windows_start(self, *, shell_execute_ok: bool):
        calls, fake_win_dll = _windows_dlls(shell_execute_ok=shell_execute_ok)
        with TemporaryDirectory() as temp_dir:
            # Construct the path before emulating os.name == "nt" so this test
            # remains executable on non-Windows CI hosts.
            log_path = Path(temp_dir) / "update.log"
            log_path.write_text("started", encoding="utf-8")
            with mock.patch.object(update_client.os, "name", "nt"), \
                 mock.patch.object(update_client.ctypes, "WinDLL", fake_win_dll, create=True), \
                 mock.patch.object(update_client.ctypes, "set_last_error", lambda _value: None, create=True), \
                 mock.patch.object(update_client.ctypes, "get_last_error", lambda: 5, create=True), \
                 mock.patch.object(update_client, "_wait_for_setup_log") as wait:
                if shell_execute_ok:
                    result = update_client._start_installer_process(
                        ["C:/updates/setup.exe", "/SILENT"], log_path,
                    )
                    return calls, wait, result
                with self.assertRaises(OSError):
                    update_client._start_installer_process(
                        ["C:/updates/setup.exe", "/SILENT"], log_path,
                    )
                return calls, wait, None

    def test_windows_launch_reaches_shell_execute_without_name_error(self) -> None:
        calls, wait, result = self._run_windows_start(shell_execute_ok=True)
        self.assertEqual(calls, ["ShellExecuteExW"])
        self.assertEqual(result, 456)
        wait.assert_called_once()

    def test_shell_execute_failure_does_not_confirm_installer_launch(self) -> None:
        calls, wait, result = self._run_windows_start(shell_execute_ok=False)
        self.assertEqual(calls, ["ShellExecuteExW"])
        self.assertIsNone(result)
        wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
