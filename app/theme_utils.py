"""Application theme helpers shared by Qt windows."""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget


# 年配の方でも見やすい標準フォントサイズ（要件4: 10pt → 12pt）。
UI_FONT_POINT_SIZE = 12
_TITLE_BAR_EVENT_FILTER: QObject | None = None
_TITLE_BAR_IS_DARK = False


class _TitleBarThemeEventFilter(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
            apply_windows_title_bar_theme(watched, _TITLE_BAR_IS_DARK)
        return False


def current_app_is_dark() -> bool:
    app = QApplication.instance()
    if app is None:
        return False
    color = app.palette().color(app.palette().ColorRole.Window)
    return color.lightness() < 128


def current_title_bar_is_dark() -> bool:
    return _TITLE_BAR_IS_DARK


def apply_app_font_size(point_size: int = UI_FONT_POINT_SIZE) -> None:
    app = QApplication.instance()
    if app is None:
        return
    font = app.font()
    font.setPointSize(point_size)
    app.setFont(font)


def apply_windows_title_bar_theme(widget: QWidget, is_dark: bool) -> None:
    """Apply Windows 10/11 title-bar dark mode to a QWidget if available."""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if is_dark else 0)
        # 20 is the documented Windows 10 1903+ attribute. 19 is kept for older builds.
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(attr),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
    except Exception:
        return


def apply_title_bar_theme_to_top_level_widgets(is_dark: bool) -> None:
    global _TITLE_BAR_EVENT_FILTER, _TITLE_BAR_IS_DARK
    app = QApplication.instance()
    if app is None:
        return
    _TITLE_BAR_IS_DARK = bool(is_dark)
    if _TITLE_BAR_EVENT_FILTER is None:
        _TITLE_BAR_EVENT_FILTER = _TitleBarThemeEventFilter(app)
        app.installEventFilter(_TITLE_BAR_EVENT_FILTER)
    for widget in app.topLevelWidgets():
        apply_windows_title_bar_theme(widget, is_dark)
