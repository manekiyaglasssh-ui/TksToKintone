from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.sumatra_detection import (
    find_installed_sumatra_pdf_exe,
    normalize_display_icon,
)


class TestSumatraDetection(unittest.TestCase):
    def _exe(self, root: Path) -> Path:
        exe = root / "SumatraPDF.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ-test")
        return exe

    def test_hkcu_valid_install_location_precedes_hklm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            hkcu = self._exe(root / "user")
            hklm = self._exe(root / "machine")
            path, source = find_installed_sumatra_pdf_exe(
                registry_reader=lambda: [
                    ("HKCU64", str(hkcu.parent), ""),
                    ("HKLM64", str(hklm.parent), ""),
                ],
                environ={},
            )
            self.assertEqual(path, str(hkcu))
            self.assertEqual(source, "hkcu64")

    def test_hklm_valid_install_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe = self._exe(Path(temp_dir))
            path, source = find_installed_sumatra_pdf_exe(
                registry_reader=lambda: [("HKLM32", str(exe.parent), "")],
                environ={},
            )
            self.assertEqual((path, source), (str(exe), "hklm32"))

    def test_display_icon_quoted_and_with_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe = self._exe(Path(temp_dir) / "with spaces")
            for value in (f'"{exe}",0', f'"{exe}" -reuse-instance'):
                with self.subTest(value=value):
                    path, _ = find_installed_sumatra_pdf_exe(
                        registry_reader=lambda value=value: [("HKCU64", "", value)],
                        environ={},
                    )
                    self.assertEqual(path, str(exe))

    def test_normalize_unquoted_display_icon_with_argument(self) -> None:
        self.assertEqual(
            normalize_display_icon(r"C:\Program Files\SumatraPDF\SumatraPDF.exe,0"),
            r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
        )

    def test_localappdata_and_program_files_standard_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local_exe = self._exe(root / "local" / "SumatraPDF")
            program_exe = self._exe(root / "program" / "SumatraPDF")
            env = {
                "LOCALAPPDATA": str(root / "local"),
                "ProgramFiles": str(root / "program"),
            }
            self.assertEqual(
                find_installed_sumatra_pdf_exe(registry_reader=lambda: [], environ=env)[0],
                str(local_exe),
            )
            local_exe.unlink()
            self.assertEqual(
                find_installed_sumatra_pdf_exe(registry_reader=lambda: [], environ=env)[0],
                str(program_exe),
            )

    def test_broken_registry_and_unknown_path_are_not_installed(self) -> None:
        path, source = find_installed_sumatra_pdf_exe(
            registry_reader=lambda: [("HKCU64", "/unknown", '"/unknown/SumatraPDF.exe",0')],
            environ={},
        )
        self.assertEqual((path, source), ("", "not_found"))

    def test_old_tks_portable_path_is_not_considered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self._exe(Path(temp_dir) / "TksToKintone" / "tools" / "SumatraPDF")
            self.assertEqual(
                find_installed_sumatra_pdf_exe(
                    registry_reader=lambda: [],
                    environ={"ProgramFiles": str(Path(temp_dir) / "other")},
                )[0],
                "",
            )

    def test_explicit_valid_path_has_highest_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit = self._exe(root / "custom")
            registered = self._exe(root / "registered")
            path, source = find_installed_sumatra_pdf_exe(
                str(explicit),
                registry_reader=lambda: [("HKCU64", str(registered.parent), "")],
                environ={},
            )
            self.assertEqual((path, source), (str(explicit), "saved"))

    def test_installer_filename_is_never_accepted_as_viewer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            installer = Path(temp_dir) / "SumatraPDF-3.6.1-64-install.exe"
            installer.write_bytes(b"MZ")
            self.assertEqual(
                find_installed_sumatra_pdf_exe(
                    str(installer), registry_reader=lambda: [], environ={}
                )[0],
                "",
            )


if __name__ == "__main__":
    unittest.main()
