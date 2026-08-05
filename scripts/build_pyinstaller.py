"""Build TksToKintone executables with paths resolved inside Python."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = SCRIPT_PATH.parent.parent
BUILD_ROOT = PROJECT_ROOT / "build"
VARIANT_DIR = BUILD_ROOT / "variant"
DIST_DIR = PROJECT_ROOT / "dist"


def _common(variant_dir: Path, dist_dir: Path, work_dir: Path) -> list[str]:
    return [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--specpath", str(variant_dir), "--distpath", str(dist_dir),
            "--workpath", str(work_dir), "--paths", str(PROJECT_ROOT)]


def normal_args(variant: str, work_dir: Path) -> list[str]:
    variant_file = VARIANT_DIR / "build_variant.txt"
    args = _common(VARIANT_DIR, DIST_DIR, work_dir) + [
        "--onedir", "--windowed", "--name", "TksToKintone",
        "--icon", str(PROJECT_ROOT / "assets" / "app_icon.ico"),
        "--version-file", str(PROJECT_ROOT / "installer" / "version_info.txt"),
        "--add-data", f"{PROJECT_ROOT / 'templates'};templates",
        "--add-data", f"{PROJECT_ROOT / 'docs' / 'olap'};docs/olap",
        "--add-data", f"{PROJECT_ROOT / 'assets'};assets",
        "--add-data", f"{variant_file};.",
    ]
    if variant == "no-update":
        args += ["--exclude-module", "app.update_client", "--exclude-module", "app.update_helper"]
    else:
        args += ["--hidden-import", "app.update_client"]
    args.append(str(PROJECT_ROOT / "app" / "main.py"))
    return args


def helper_args(work_dir: Path) -> list[str]:
    return _common(VARIANT_DIR, DIST_DIR, work_dir) + [
        "--onefile", "--console", "--name", "tks_update_helper",
        "--icon", str(PROJECT_ROOT / "assets" / "app_icon.ico"),
        "--version-file", str(PROJECT_ROOT / "installer" / "version_info.txt"),
        str(PROJECT_ROOT / "app" / "update_helper.py"),
    ]


def _required(mode: str, variant_file: Path) -> list[Path]:
    paths = [PROJECT_ROOT / "templates", PROJECT_ROOT / "assets", PROJECT_ROOT / "docs" / "olap",
             PROJECT_ROOT / "assets" / "app_icon.ico", PROJECT_ROOT / "installer" / "version_info.txt"]
    paths.append(PROJECT_ROOT / "app" / ("main.py" if mode == "normal" else "update_helper.py"))
    if mode == "normal":
        paths.append(variant_file)
    return paths


def run_command(args: list[str]) -> int:
    print("PyInstaller argv:")
    for index, value in enumerate(args):
        print(f"  argv[{index}] = {value!r}")
    return int(subprocess.run(args, check=False, shell=False).returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("normal", "helper"))
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    variant_file = VARIANT_DIR / "build_variant.txt"
    variant = variant_file.read_text(encoding="utf-8").strip() if variant_file.is_file() else "normal"
    work_dir = BUILD_ROOT / ("work" if options.mode == "normal" else "helper-work")
    script = PROJECT_ROOT / "app" / ("main.py" if options.mode == "normal" else "update_helper.py")
    args = normal_args(variant, work_dir) if options.mode == "normal" else helper_args(work_dir)
    print(f"Build mode: {options.mode}\nBuild script: {SCRIPT_PATH}\nProject root: {PROJECT_ROOT}")
    print(f"Variant dir: {VARIANT_DIR}\nDist dir: {DIST_DIR}\nWork dir: {work_dir}")
    print(f"Input script: {script}\nInput script exists: {script.is_file()}\nSpecpath: {VARIANT_DIR}")
    missing = [path for path in _required(options.mode, variant_file) if not path.exists()]
    if missing:
        for path in missing:
            print(f"ERROR: required path not found: {path}")
        return 1
    print("PyInstaller argv:")
    for index, value in enumerate(args):
        print(f"  argv[{index}] = {value!r}")
    return 0 if options.dry_run else int(subprocess.run(args, check=False, shell=False).returncode)


if __name__ == "__main__":
    raise SystemExit(main())
