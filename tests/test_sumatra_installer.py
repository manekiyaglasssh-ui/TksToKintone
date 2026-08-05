from __future__ import annotations

import tempfile
import unittest
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import scripts.download_sumatra as download_sumatra
from scripts.download_sumatra import (
    ensure_sumatra_installer,
    verify_installer,
)
from scripts.sumatra_config import (
    SUMATRA_ARCH,
    SUMATRA_DOWNLOAD_URL,
    SUMATRA_INSTALLER_BYTES,
    SUMATRA_INSTALLER_FILENAME,
    SUMATRA_SHA256,
    SUMATRA_VERSION,
)

ROOT = Path(__file__).resolve().parents[1]


class TestSumatraInstaller(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.iss = (ROOT / "installer" / "tks-to-kintone.iss").read_text(encoding="utf-8")
        cls.build = (ROOT / "build_exe.bat").read_text(encoding="ascii")

    def test_canonical_fixed_official_installer_settings(self) -> None:
        self.assertEqual(SUMATRA_VERSION, "3.6.1")
        self.assertEqual(SUMATRA_ARCH, "64")
        self.assertEqual(SUMATRA_INSTALLER_FILENAME, "SumatraPDF-3.6.1-64-install.exe")
        self.assertTrue(SUMATRA_DOWNLOAD_URL.startswith("https://www.sumatrapdfreader.org/"))
        self.assertEqual(len(SUMATRA_SHA256), 64)
        self.assertEqual(SUMATRA_INSTALLER_BYTES, 11_075_960)

    def test_direct_script_resolves_project_root_from_any_working_directory(self) -> None:
        script = ROOT / "scripts" / "download_sumatra.py"
        probe = (
            "import runpy, sys; "
            f"ns = runpy.run_path({str(script)!r}); "
            "root = str(ns['PROJECT_ROOT']); "
            "print(root); print(sys.path.count(root))"
        )
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, "-I", "-c", probe],
                cwd=temp_dir,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(ROOT), "1"])

    def test_build_adds_project_root_to_pythonpath(self) -> None:
        self.assertIn('set "PROJECT_ROOT=%~dp0"', self.build)
        self.assertIn('set "PYTHONPATH=%PROJECT_ROOT%;%PYTHONPATH%"', self.build)

    def test_build_writes_pyinstaller_specs_under_build_variant(self) -> None:
        self.assertGreaterEqual(self.build.count('--variant-dir "%VARIANT_DIR%"'), 2)
        self.assertIn('scripts\\build_pyinstaller.py', self.build)
        self.assertIn("installer/*.exe.sha256", (ROOT / ".gitignore").read_text())

    def test_allow_missing_sumatra_remains_a_development_only_success_path(self) -> None:
        self.assertIn('if /I "%~1"=="--allow-missing-sumatra" goto set_allow_flag', self.build)
        self.assertIn('if "%ALLOW_MISSING_SUMATRA%"=="1" goto sumatra_dev_skip', self.build)
        self.assertIn('set "SKIP_INSTALLER=1"', self.build)
        self.assertIn("Build complete without installer", self.build)
        self.assertIn("exit /b 0", self.build)

    def test_checked_in_build_artifact_matches_hash_and_pe_format(self) -> None:
        installer = ROOT / "build" / "vendor" / "sumatra" / SUMATRA_INSTALLER_FILENAME
        verify_installer(installer, verify_authenticode=False)

    def test_verifier_rejects_html_small_file_and_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            invalid = Path(temp_dir) / SUMATRA_INSTALLER_FILENAME
            invalid.write_bytes(b"<html>error</html>")
            with self.assertRaises(RuntimeError):
                verify_installer(invalid, verify_authenticode=False)

    def test_download_is_formally_placed_only_after_all_verification_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "vendor" / SUMATRA_INSTALLER_FILENAME

            def fake_download(path: Path) -> None:
                path.write_bytes(b"downloaded")

            with (
                patch.object(download_sumatra, "_download_to", side_effect=fake_download),
                patch.object(download_sumatra, "verify_installer") as verify,
                patch.object(download_sumatra, "write_inno_include"),
            ):
                result = ensure_sumatra_installer(destination)

            self.assertEqual(result, destination.resolve())
            self.assertEqual(destination.read_bytes(), b"downloaded")
            self.assertEqual(verify.call_count, 1)
            verified_path = verify.call_args.args[0]
            self.assertNotEqual(verified_path, destination)
            self.assertIn(".tks-sumatra-download-", str(verified_path))

    def test_failed_download_verification_never_gets_formal_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "vendor" / SUMATRA_INSTALLER_FILENAME

            def fake_download(path: Path) -> None:
                path.write_bytes(b"unverified")

            with (
                patch.object(download_sumatra, "_download_to", side_effect=fake_download),
                patch.object(
                    download_sumatra,
                    "verify_installer",
                    side_effect=RuntimeError("signature failed"),
                ),
                patch.object(download_sumatra, "write_inno_include"),
            ):
                with self.assertRaisesRegex(RuntimeError, "signature failed"):
                    ensure_sumatra_installer(destination)

            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.iterdir()), [])

    def test_existing_formal_file_is_preserved_if_reverification_and_download_fail(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "vendor" / SUMATRA_INSTALLER_FILENAME
            destination.parent.mkdir()
            destination.write_bytes(b"previously placed installer")

            with (
                patch.object(
                    download_sumatra,
                    "verify_installer",
                    side_effect=RuntimeError("verification infrastructure failed"),
                ),
                patch.object(
                    download_sumatra,
                    "_download_to",
                    side_effect=RuntimeError("download unavailable"),
                ),
                patch.object(download_sumatra, "write_inno_include"),
            ):
                with self.assertRaisesRegex(RuntimeError, "download unavailable"):
                    ensure_sumatra_installer(destination)

            self.assertEqual(destination.read_bytes(), b"previously placed installer")

    def test_inno_embeds_installer_only_as_temporary_file(self) -> None:
        self.assertIn(r'Flags: dontcopy deleteafterinstall', self.iss)
        self.assertIn(r"ExtractTemporaryFile('{#SumatraInstallerFilename}')", self.iss)
        self.assertIn(r"ExpandConstant('{tmp}\{#SumatraInstallerFilename}')", self.iss)
        self.assertNotIn(r'DestDir: "{app}\tools\SumatraPDF"', self.iss)

    def test_new_and_update_install_share_unconditional_prepare_check(self) -> None:
        self.assertIn("function PrepareToInstall", self.iss)
        self.assertIn("FindInstalledSumatraPdf(SumatraPath)", self.iss)
        self.assertNotIn("IsUpgrade", self.iss)

    def test_installed_dependency_is_skipped(self) -> None:
        self.assertIn("status=already_installed", self.iss)
        self.assertIn("action=skip", self.iss)

    def test_admin_all_user_arguments_wait_and_result_code(self) -> None:
        self.assertIn("PrivilegesRequired=admin", self.iss)
        self.assertIn("'-install -silent -all-users'", self.iss)
        self.assertIn("ewWaitUntilTerminated", self.iss)
        self.assertIn("ResultCode <> 0", self.iss)
        self.assertNotIn("-with-preview", self.iss)
        self.assertNotIn("-with-filter", self.iss)

    def test_success_requires_post_install_exe_detection(self) -> None:
        first = self.iss.index("ResultCode <> 0")
        second = self.iss.index("not FindInstalledSumatraPdf(SumatraPath)", first)
        self.assertGreater(second, first)
        self.assertIn("exe_not_found_after_success", self.iss)

    def test_registry_views_and_standard_paths_are_checked(self) -> None:
        for item in (
            "HKCU64",
            "HKCU32",
            "HKLM64",
            "HKLM32",
            r"{localappdata}\SumatraPDF\SumatraPDF.exe",
            r"{autopf}\SumatraPDF\SumatraPDF.exe",
            r"{pf}\SumatraPDF\SumatraPDF.exe",
            r"{pf32}\SumatraPDF\SumatraPDF.exe",
        ):
            self.assertIn(item, self.iss)
        self.assertIn("FileExists(Candidate)", self.iss)

    def test_old_portable_is_removed_but_installed_sumatra_is_not_uninstalled(self) -> None:
        self.assertIn(r'Name: "{app}\tools\SumatraPDF"', self.iss)
        self.assertIn(r'Name: "{app}\_internal\tools\SumatraPDF"', self.iss)
        self.assertNotIn(r"{localappdata}\SumatraPDF\"; Name:", self.iss)
        self.assertNotIn("unins000.exe", self.iss)

    def test_build_fetches_before_inno_and_does_not_bundle_portable_with_pyinstaller(self) -> None:
        self.assertIn(r"python scripts\download_sumatra.py", self.build)
        self.assertNotIn(r"--add-data ^\"tools\SumatraPDF", self.build)
        self.assertLess(
            self.build.index(r"python scripts\download_sumatra.py"),
            self.build.index("ISCC_PATH"),
        )

    def test_third_party_notice_is_packaged(self) -> None:
        notice = ROOT / "third_party_licenses" / "SumatraPDF.txt"
        self.assertTrue(notice.is_file())
        text = notice.read_text(encoding="utf-8")
        self.assertIn("3.6.1", text)
        self.assertIn("GPL", text)
        self.assertIn("改変せず", text)
        self.assertIn("github.com/sumatrapdfreader", text)
        self.assertIn(r"..\third_party_licenses\SumatraPDF.txt", self.iss)


if __name__ == "__main__":
    unittest.main()
