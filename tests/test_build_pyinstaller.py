import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.build_pyinstaller as build
from scripts.build_pyinstaller import helper_args, normal_args, run_command


class TestBuildPyInstaller(unittest.TestCase):
    def setUp(self):
        self.root = Path("C:/work/Tks To Kintone (test)")
        self.variant = self.root / "build" / "variant"

    def test_normal_is_independent_argv(self):
        old_root = build.PROJECT_ROOT
        old_variant = build.VARIANT_DIR
        old_dist = build.DIST_DIR
        build.PROJECT_ROOT, build.VARIANT_DIR, build.DIST_DIR = self.root, self.variant, self.root / "dist"
        args = normal_args("normal", self.root / "build" / "work")
        build.PROJECT_ROOT, build.VARIANT_DIR, build.DIST_DIR = old_root, old_variant, old_dist
        self.assertIsInstance(args, list)
        self.assertEqual(args[-1], str(self.root / "app" / "main.py"))
        self.assertEqual(args.count(str(self.root / "app" / "main.py")), 1)
        self.assertNotIn("build/variant/templates", args)
        self.assertIn(str(self.root / "templates") + ";templates", args)
        self.assertIn("--add-data", args)
        self.assertEqual(args[args.index("--add-data") + 1].split(";", 1)[1], "templates")
        self.assertEqual(args[args.index("--specpath") + 1], str(self.variant))

    def test_helper_is_independent_argv(self):
        old_root = build.PROJECT_ROOT
        old_variant = build.VARIANT_DIR
        old_dist = build.DIST_DIR
        build.PROJECT_ROOT, build.VARIANT_DIR, build.DIST_DIR = self.root, self.variant, self.root / "dist"
        args = helper_args(self.root / "build" / "helper-work")
        build.PROJECT_ROOT, build.VARIANT_DIR, build.DIST_DIR = old_root, old_variant, old_dist
        self.assertEqual(args[-1], str(self.root / "app" / "update_helper.py"))
        self.assertEqual(args.count(str(self.root / "app" / "update_helper.py")), 1)

    def test_run_command_uses_list_and_shell_false(self):
        with patch("scripts.build_pyinstaller.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertEqual(run_command(["python", "-m", "PyInstaller", "script.py"]), 0)
        run.assert_called_once_with(["python", "-m", "PyInstaller", "script.py"], check=False, shell=False)

    def test_cli_accepts_only_mode_and_dry_run(self):
        self.assertEqual(build.main(["normal", "--dry-run"]), 0)
        self.assertEqual(build.main(["helper", "--dry-run"]), 0)
