import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_pyinstaller import helper_args, normal_args, run_command


class TestBuildPyInstaller(unittest.TestCase):
    def setUp(self):
        self.root = Path("C:/work/Tks To Kintone (test)")
        self.variant = self.root / "build" / "variant"

    def test_normal_is_independent_argv(self):
        args = normal_args(self.root, "normal", self.variant, self.root / "dist", self.root / "build" / "work")
        self.assertIsInstance(args, list)
        self.assertEqual(args[-1], str(self.root / "app" / "main.py"))
        self.assertEqual(args.count(str(self.root / "app" / "main.py")), 1)
        self.assertNotIn("build/variant/templates", args)
        self.assertIn(str(self.root / "templates") + ";templates", args)
        self.assertIn("--add-data", args)
        self.assertEqual(args[args.index("--add-data") + 1].split(";", 1)[1], "templates")
        self.assertEqual(args[args.index("--specpath") + 1], str(self.variant))

    def test_helper_is_independent_argv(self):
        args = helper_args(self.root, self.variant, self.root / "dist", self.root / "build" / "helper-work")
        self.assertEqual(args[-1], str(self.root / "app" / "update_helper.py"))
        self.assertEqual(args.count(str(self.root / "app" / "update_helper.py")), 1)

    def test_run_command_uses_list_and_shell_false(self):
        with patch("scripts.build_pyinstaller.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertEqual(run_command(["python", "-m", "PyInstaller", "script.py"]), 0)
        run.assert_called_once_with(["python", "-m", "PyInstaller", "script.py"], check=False, shell=False)
