"""Locate a separately installed SumatraPDF executable on Windows."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Iterable

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SumatraPDF"


def normalize_display_icon(value: object) -> str:
    """Extract an executable path from a quoted/unquoted DisplayIcon value."""
    text = os.path.expandvars(str(value or "").strip())
    if not text:
        return ""
    quoted = re.match(r'^\s*"([^"]+?\.exe)"', text, re.IGNORECASE)
    if quoted:
        return quoted.group(1).strip()
    match = re.match(r"^\s*(.+?\.exe)(?:\s|,|$)", text, re.IGNORECASE)
    return match.group(1).strip().strip('"') if match else ""


def _valid_sumatra_exe(path: object) -> str:
    text = os.path.expandvars(str(path or "").strip().strip('"'))
    if not text:
        return ""
    candidate = Path(text)
    if candidate.name.lower() != "sumatrapdf.exe" or "-install" in candidate.name.lower():
        return ""
    try:
        return str(candidate) if candidate.is_file() else ""
    except OSError:
        return ""


def _paths_from_registry_values(install_location: object, display_icon: object) -> Iterable[str]:
    location = os.path.expandvars(str(install_location or "").strip().strip('"'))
    if location:
        location_path = Path(location)
        if location_path.name.lower() == "sumatrapdf.exe":
            yield str(location_path)
        else:
            yield str(location_path / "SumatraPDF.exe")
    icon_path = normalize_display_icon(display_icon)
    if icon_path:
        yield icon_path


def _read_windows_registry() -> list[tuple[str, str, str]]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    rows: list[tuple[str, str, str]] = []
    access_views = (
        ("64", getattr(winreg, "KEY_WOW64_64KEY", 0)),
        ("32", getattr(winreg, "KEY_WOW64_32KEY", 0)),
    )
    for hive_name, hive in (("HKCU", winreg.HKEY_CURRENT_USER), ("HKLM", winreg.HKEY_LOCAL_MACHINE)):
        for view_name, view_flag in access_views:
            try:
                with winreg.OpenKey(
                    hive, UNINSTALL_KEY, 0, winreg.KEY_READ | view_flag
                ) as key:
                    try:
                        install_location = str(winreg.QueryValueEx(key, "InstallLocation")[0] or "")
                    except OSError:
                        install_location = ""
                    try:
                        display_icon = str(winreg.QueryValueEx(key, "DisplayIcon")[0] or "")
                    except OSError:
                        display_icon = ""
            except OSError:
                continue
            rows.append((f"{hive_name}{view_name}", install_location, display_icon))
    return rows


def standard_sumatra_paths(environ: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    paths: list[str] = []
    local = env.get("LOCALAPPDATA", "")
    if local:
        paths.append(str(Path(local) / "SumatraPDF" / "SumatraPDF.exe"))
    for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = env.get(key, "")
        if base:
            paths.append(str(Path(base) / "SumatraPDF" / "SumatraPDF.exe"))
    return _unique(paths)


def _unique(paths: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            result.append(str(path))
    return result


def find_installed_sumatra_pdf_exe(
    explicit_path: str = "",
    *,
    registry_reader: Callable[[], list[tuple[str, str, str]]] | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return ``(exe path, source)`` without considering Tks portable locations."""
    explicit = _valid_sumatra_exe(explicit_path)
    if explicit:
        return explicit, "saved"

    read_registry = registry_reader or _read_windows_registry
    for source, install_location, display_icon in read_registry():
        for candidate in _paths_from_registry_values(install_location, display_icon):
            valid = _valid_sumatra_exe(candidate)
            if valid:
                return valid, source.lower()

    for candidate in standard_sumatra_paths(environ):
        valid = _valid_sumatra_exe(candidate)
        if valid:
            local_base = (os.environ if environ is None else environ).get("LOCALAPPDATA", "")
            source = "localappdata" if local_base and os.path.normcase(candidate).startswith(
                os.path.normcase(local_base)
            ) else "program_files"
            return valid, source
    return "", "not_found"


def is_sumatra_pdf_installed(**kwargs: object) -> bool:
    return bool(find_installed_sumatra_pdf_exe(**kwargs)[0])
