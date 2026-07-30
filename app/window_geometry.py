"""Window sizing helpers shared by top-level Qt windows."""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

_LOGGER = logging.getLogger("tks_to_kintone_app")


def get_display_scale(window: QWidget | None = None) -> float:
    """ディスプレイ表示倍率（1.0=100%, 1.25=125% ...）を返す。

    logicalDotsPerInch/96 を優先し、devicePixelRatio の大きい方も加味する。
    取得できない場合は 1.0 を返す。
    """
    screen = None
    try:
        if window is not None and hasattr(window, "screen"):
            screen = window.screen()
    except Exception:  # noqa: BLE001
        screen = None
    if screen is None:
        screen = QApplication.primaryScreen()
    if screen is None:
        return 1.0
    scale = 1.0
    try:
        dpi = float(screen.logicalDotsPerInch())
        if dpi > 0:
            scale = dpi / 96.0
    except Exception:  # noqa: BLE001
        scale = 1.0
    try:
        dpr = float(screen.devicePixelRatio())
        if dpr > 0:
            scale = max(scale, dpr)
    except Exception:  # noqa: BLE001
        pass
    return scale


def left_pane_width_for_scale(scale: float, *, base_width: int = 250) -> int:
    """表示倍率に応じた指図書編集の左ペイン幅を返す（要件9）。

    反映先ボタン・お気に入り一覧が窮屈だったため、全DPIの基準値をさらに約1.5cm
    （約60px）広げた。100%: base_width（150→190→250）。125%以上: 300px（200→240→300）。
    150%以上: 320px（220→260→320）。倍率が上がるほど読みやすい幅にする。
    """
    try:
        value = float(scale)
    except (TypeError, ValueError):
        value = 1.0
    if value >= 1.5:
        return max(base_width, 320)
    if value >= 1.25:
        return max(base_width, 300)
    return base_width


def apply_app_dpi_policy() -> None:
    """Apply Qt DPI policy before QApplication is created.

    The app uses Qt logical pixels and avoids manual DPI multiplication. This
    keeps 125% Windows scaling from making fixed-size windows spill off screen.
    """
    os.environ.pop("QT_SCALE_FACTOR", None)
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "0")
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:  # noqa: BLE001 - DPI policy failure must not prevent startup
        _LOGGER.debug("app_dpi_policy_apply_failed", exc_info=True)
    _LOGGER.info(
        "app_dpi_policy_applied %s",
        {
            "QT_ENABLE_HIGHDPI_SCALING": os.environ.get("QT_ENABLE_HIGHDPI_SCALING"),
            "QT_AUTO_SCREEN_SCALE_FACTOR": os.environ.get("QT_AUTO_SCREEN_SCALE_FACTOR"),
            "QT_SCALE_FACTOR": os.environ.get("QT_SCALE_FACTOR", ""),
        },
    )


def clamp_window_to_available_geometry(
    window: QWidget,
    *,
    desired_width: int,
    desired_height: int,
    min_width: int = 0,
    min_height: int = 0,
    max_ratio: float = 0.95,
) -> None:
    """Resize a window so its initial size and minimum fit on the current screen."""
    screen = window.screen() or QApplication.primaryScreen()
    if screen is None:
        window.resize(desired_width, desired_height)
        _LOGGER.info(
            "app_window_initial_size %s",
            {"class": type(window).__name__, "width": desired_width, "height": desired_height},
        )
        return
    available = screen.availableGeometry()
    scale = 1.0
    try:
        scale = float(screen.devicePixelRatio())
    except Exception:  # noqa: BLE001
        scale = 1.0
    _LOGGER.info(
        "app_screen_scale_factor_detected %s",
        {"class": type(window).__name__, "scale": scale},
    )
    _LOGGER.info(
        "app_window_available_geometry %s",
        {
            "class": type(window).__name__,
            "width": available.width(),
            "height": available.height(),
        },
    )
    _LOGGER.info(
        "app_window_requested_size %s",
        {"class": type(window).__name__, "width": desired_width, "height": desired_height},
    )
    max_width = max(1, int(available.width() * max_ratio))
    max_height = max(1, int(available.height() * max_ratio))
    applied_min_width = min(max(0, int(min_width)), max_width)
    applied_min_height = min(max(0, int(min_height)), max_height)
    if applied_min_width or applied_min_height:
        window.setMinimumSize(applied_min_width, applied_min_height)
    width = min(max(int(desired_width), applied_min_width), max_width)
    height = min(max(int(desired_height), applied_min_height), max_height)
    window.resize(width, height)
    _LOGGER.info(
        "app_window_initial_size %s",
        {"class": type(window).__name__, "width": width, "height": height},
    )
    _LOGGER.info(
        "app_window_final_size %s",
        {"class": type(window).__name__, "width": width, "height": height},
    )
    if width != desired_width or height != desired_height:
        _LOGGER.info(
            "app_window_size_clamped_to_available_geometry %s",
            {
                "class": type(window).__name__,
                "requested_width": desired_width,
                "requested_height": desired_height,
                "width": width,
                "height": height,
            },
        )
        _LOGGER.info(
            "app_window_geometry_clamped %s",
            {
                "class": type(window).__name__,
                "requested_width": desired_width,
                "requested_height": desired_height,
                "width": width,
                "height": height,
            },
        )
