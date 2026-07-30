"""SumatraPDF vendor dependency settings (single source of truth)."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SUMATRA_VERSION = "3.6.1"
SUMATRA_ARCH = "64"
SUMATRA_INSTALLER_FILENAME = f"SumatraPDF-{SUMATRA_VERSION}-{SUMATRA_ARCH}-install.exe"
SUMATRA_DOWNLOAD_URL = (
    f"https://www.sumatrapdfreader.org/dl/rel/{SUMATRA_VERSION}/"
    f"{SUMATRA_INSTALLER_FILENAME}"
)
SUMATRA_SHA256 = "1eee71cccd2ea6e94d5bcea54ee2f759844da3e1a0ee2f6045035b1d17b94381"
SUMATRA_INSTALLER_BYTES = 11_075_960
SUMATRA_VENDOR_DIR = PROJECT_ROOT / "build" / "vendor" / "sumatra"
SUMATRA_INSTALLER_PATH = SUMATRA_VENDOR_DIR / SUMATRA_INSTALLER_FILENAME
SUMATRA_INNO_INCLUDE_PATH = SUMATRA_VENDOR_DIR / "sumatra-config.iss"
