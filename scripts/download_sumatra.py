"""Download and verify the pinned official SumatraPDF installer at build time."""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

# Running ``python scripts/download_sumatra.py`` puts only the scripts directory
# at the front of sys.path.  Resolve the repository from this file so imports of
# shared project modules work regardless of the caller's current directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from .sumatra_config import (
        SUMATRA_DOWNLOAD_URL,
        SUMATRA_INSTALLER_FILENAME,
        SUMATRA_INSTALLER_PATH,
        SUMATRA_INNO_INCLUDE_PATH,
        SUMATRA_INSTALLER_BYTES,
        SUMATRA_SHA256,
        SUMATRA_VERSION,
        SUMATRA_ARCH,
    )
except ImportError:
    from scripts.sumatra_config import (  # type: ignore
        SUMATRA_DOWNLOAD_URL,
        SUMATRA_INSTALLER_FILENAME,
        SUMATRA_INSTALLER_PATH,
        SUMATRA_INNO_INCLUDE_PATH,
        SUMATRA_INSTALLER_BYTES,
        SUMATRA_SHA256,
        SUMATRA_VERSION,
        SUMATRA_ARCH,
    )

_PE_SIGNATURE_OFFSET = 0x3C
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_pe_executable(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                return False
            stream.seek(_PE_SIGNATURE_OFFSET)
            offset_bytes = stream.read(4)
            if len(offset_bytes) != 4:
                return False
            stream.seek(int.from_bytes(offset_bytes, "little"))
            return stream.read(4) == b"PE\0\0"
    except OSError:
        return False


def verify_installer(path: Path, *, verify_authenticode: bool = False) -> None:
    path = Path(path).resolve()
    if path.name != SUMATRA_INSTALLER_FILENAME:
        raise RuntimeError(f"Unexpected SumatraPDF installer filename: {path.name}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"SumatraPDF installer is missing: {path}") from exc
    if size != SUMATRA_INSTALLER_BYTES:
        raise RuntimeError(
            "SumatraPDF installer size mismatch: "
            f"path={path} expected={SUMATRA_INSTALLER_BYTES} actual={size}"
        )
    if not _is_pe_executable(path):
        raise RuntimeError(
            f"SumatraPDF download is not a Windows PE executable (HTML/error response rejected): {path}"
        )
    actual_hash = sha256_file(path)
    if actual_hash.lower() != SUMATRA_SHA256.lower():
        raise RuntimeError(
            "SumatraPDF installer SHA-256 mismatch: "
            f"expected={SUMATRA_SHA256} actual={actual_hash}"
        )
    # Kept as an ignored compatibility argument. Code signing is not a build
    # prerequisite; the pinned size, SHA-256 and PE checks remain mandatory.


def _download_to(path: Path) -> None:
    request = urllib.request.Request(
        SUMATRA_DOWNLOAD_URL,
        headers={"User-Agent": "TksToKintone-build/1.6.3"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        final_url = response.geturl()
        if not final_url.lower().startswith("https://"):
            raise RuntimeError(f"SumatraPDF download redirected outside HTTPS: {final_url}")
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if "text/html" in content_type:
            raise RuntimeError(f"SumatraPDF server returned HTML: {content_type}")
        with path.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)


def ensure_sumatra_installer(destination: Path = SUMATRA_INSTALLER_PATH) -> Path:
    destination = Path(destination).resolve()
    if destination.is_file():
        try:
            verify_installer(destination)
            write_inno_include()
            print(
                "SumatraPDF installer prepared "
                f"(verified existing): {destination}"
            )
            return destination
        except RuntimeError as exc:
            print(f"Existing installer rejected; downloading again: {exc}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".tks-sumatra-download-", dir=destination.parent
    ) as temp_dir:
        temporary = Path(temp_dir) / SUMATRA_INSTALLER_FILENAME
        print(f"Downloading pinned SumatraPDF installer: {SUMATRA_DOWNLOAD_URL}")
        _download_to(temporary)
        verify_installer(temporary)
        os.replace(temporary, destination)
    write_inno_include()
    print(f"SumatraPDF installer prepared: {destination}")
    return destination


def write_inno_include(path: Path = SUMATRA_INNO_INCLUDE_PATH) -> None:
    """Generate Inno preprocessor definitions from the canonical Python settings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "; Generated by scripts/download_sumatra.py; do not edit.\n"
        f'#define SumatraVersion "{SUMATRA_VERSION}"\n'
        f'#define SumatraArch "{SUMATRA_ARCH}"\n'
        f'#define SumatraInstallerFilename "{SUMATRA_INSTALLER_FILENAME}"\n'
        f'#define SumatraSha256 "{SUMATRA_SHA256}"\n'
    )
    path.write_text(content, encoding="ascii", newline="\r\n")


def main() -> int:
    destination = Path(
        os.environ.get("SUMATRA_INSTALLER_DESTINATION", str(SUMATRA_INSTALLER_PATH))
    )
    try:
        ensure_sumatra_installer(destination)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: failed to prepare SumatraPDF installer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
