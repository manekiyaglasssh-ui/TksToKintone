from __future__ import annotations

import os
import sys
from pathlib import Path

BUILD_VARIANT_NORMAL = "normal"
BUILD_VARIANT_NO_UPDATE = "no-update"
BUILD_VARIANT_NO_HELPER = "no-helper"
BUILD_VARIANT_WITH_HELPER = "with-helper"

_VALID_BUILD_VARIANTS = {
    BUILD_VARIANT_NORMAL,
    BUILD_VARIANT_NO_UPDATE,
    BUILD_VARIANT_NO_HELPER,
    BUILD_VARIANT_WITH_HELPER,
}


def build_variant() -> str:
    """Return the packaged build variant.

    Development runs default to the normal build. Packaged builds embed a
    ``build_variant.txt`` marker at the PyInstaller resource root.
    """
    env_value = os.environ.get("TKS_BUILD_VARIANT")
    if env_value:
        return _normalize_variant(env_value)

    marker = _resource_root() / "build_variant.txt"
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return BUILD_VARIANT_NORMAL
    return _normalize_variant(value)


def updates_enabled() -> bool:
    return build_variant() != BUILD_VARIANT_NO_UPDATE


def update_helper_expected() -> bool:
    return build_variant() == BUILD_VARIANT_WITH_HELPER


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


def _normalize_variant(value: str) -> str:
    normalized = value.strip().lower()
    return normalized if normalized in _VALID_BUILD_VARIANTS else BUILD_VARIANT_NORMAL
