"""Build TksToKintone executables with an explicit subprocess argv list."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _common(project_root: Path, variant_dir: Path, dist_dir: Path, work_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--specpath",
        str(variant_dir),
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--paths",
        str(project_root),
    ]


def normal_args(project_root: Path, variant: str, variant_dir: Path, dist_dir: Path, work_dir: Path) -> list[str]:
    variant_file = variant_dir / "build_variant.txt"
    args = _common(project_root, variant_dir, dist_dir, work_dir) + [
        "--onedir",
        "--windowed",
        "--name",
        "TksToKintone",
        "--icon",
        str(project_root / "assets" / "app_icon.ico"),
        "--version-file",
        str(project_root / "installer" / "version_info.txt"),
        "--add-data",
        f"{project_root / 'templates'};templates",
        "--add-data",
        f"{project_root / 'docs' / 'olap'};docs/olap",
        "--add-data",
        f"{project_root / 'assets'};assets",
        "--add-data",
        f"{variant_file};.",
    ]
    if variant == "no-update":
        args.extend(["--exclude-module", "app.update_client", "--exclude-module", "app.update_helper"])
    else:
        args.extend(["--hidden-import", "app.update_client"])
    args.append(str(project_root / "app" / "main.py"))
    return args


def helper_args(project_root: Path, variant_dir: Path, dist_dir: Path, work_dir: Path) -> list[str]:
    args = _common(project_root, variant_dir, dist_dir, work_dir) + [
        "--onefile",
        "--console",
        "--name",
        "tks_update_helper",
    ]
    args.append(str(project_root / "app" / "update_helper.py"))
    return args


def run_command(args: list[str]) -> int:
    print("PyInstaller argv:")
    for index, value in enumerate(args):
        print(f"  argv[{index}] = {value!r}")
    return int(subprocess.run(args, check=False, shell=False).returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--variant-dir", required=True, type=Path)
    parser.add_argument("--dist-dir", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--mode", choices=("normal", "helper"), required=True)
    options = parser.parse_args(argv)

    project_root = options.project_root.resolve()
    variant_dir = options.variant_dir.resolve()
    dist_dir = options.dist_dir.resolve()
    work_dir = options.work_dir.resolve()
    script = project_root / "app" / ("main.py" if options.mode == "normal" else "update_helper.py")
    print(f"variant=[{options.variant}]")
    print(f"project_root=[{project_root}]")
    print(f"script=[{script}]")
    print(f"script_exists=[{script.is_file()}]")
    print(f"specpath=[{variant_dir}]")
    print(f"distpath=[{dist_dir}]")
    print(f"workpath=[{work_dir}]")
    if not script.is_file():
        print(f"ERROR: script not found: {script}")
        return 1
    args = normal_args(project_root, options.variant, variant_dir, dist_dir, work_dir) if options.mode == "normal" else helper_args(project_root, variant_dir, dist_dir, work_dir)
    return run_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
