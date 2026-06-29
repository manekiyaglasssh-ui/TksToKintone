"""Application theme helpers shared by Qt windows."""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QPushButton, QToolButton, QWidget


# 年配の方でも見やすい標準フォントサイズ（要件4: 10pt → 12pt）。
UI_FONT_POINT_SIZE = 12
_TITLE_BAR_EVENT_FILTER: QObject | None = None
_TITLE_BAR_IS_DARK = False

SEMANTIC_BUTTON_STYLESHEET = """
QPushButton, QToolButton {
  border-radius: 6px;
  padding: 5px 10px;
}
QPushButton[buttonRole="primary"], QToolButton[buttonRole="primary"] {
  background-color: #1565c0;
  color: #ffffff;
  border: 1px solid #0d47a1;
  font-weight: bold;
}
QPushButton[buttonRole="primary"]:hover, QToolButton[buttonRole="primary"]:hover {
  background-color: #1976d2;
}
QPushButton[buttonRole="success"], QToolButton[buttonRole="success"],
QPushButton[buttonRole="olapUpdate"], QToolButton[buttonRole="olapUpdate"] {
  background-color: #2e7d32;
  color: #ffffff;
  border: 1px solid #1b5e20;
  font-weight: bold;
}
QPushButton[buttonRole="success"]:hover, QToolButton[buttonRole="success"]:hover,
QPushButton[buttonRole="olapUpdate"]:hover, QToolButton[buttonRole="olapUpdate"]:hover {
  background-color: #388e3c;
}
QPushButton[buttonRole="olapFetch"], QToolButton[buttonRole="olapFetch"] {
  background-color: #1565c0;
  color: #ffffff;
  border: 1px solid #0d47a1;
  font-weight: bold;
}
QPushButton[buttonRole="olapFetch"]:hover, QToolButton[buttonRole="olapFetch"]:hover {
  background-color: #1976d2;
}
QPushButton[buttonRole="secondary"], QToolButton[buttonRole="secondary"] {
  background-color: #546e7a;
  color: #ffffff;
  border: 1px solid #37474f;
}
QPushButton[buttonRole="secondary"]:hover, QToolButton[buttonRole="secondary"]:hover {
  background-color: #607d8b;
}
QPushButton[buttonRole="danger"], QToolButton[buttonRole="danger"] {
  background-color: #c62828;
  color: #ffffff;
  border: 1px solid #8e0000;
  font-weight: bold;
}
QPushButton[buttonRole="danger"]:hover, QToolButton[buttonRole="danger"]:hover {
  background-color: #d32f2f;
}
QPushButton[buttonRole]:disabled, QToolButton[buttonRole]:disabled {
  background-color: #747f89;
  color: #e8ecef;
  border: 1px solid #5d6770;
  border-radius: 6px;
  font-weight: normal;
}
"""

_DANGER_BUTTON_WORDS = ("削除", "初期値に戻す", "登録キャンセル")
_SECONDARY_BUTTON_WORDS = (
    "行追加",
    "参照",
    "画像挿入",
    "貼り付け",
    "テンプレ登録",
    "加工名マスタ",
    "得意先ヘッダー設定",
    "設定",
    "開く",
    "戻る",
    "閉じる",
    "縮小",
    "拡大",
    "100%",
    "クリア",
    "キャンセル",
)
_PRIMARY_BUTTON_WORDS = (
    "実行",
    "取得",
    "作成",
    "プレビュー",
    "印刷",
    "受注No追加",
)
_SUCCESS_BUTTON_WORDS = ("更新", "保存", "登録")


class _TitleBarThemeEventFilter(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
            apply_windows_title_bar_theme(watched, _TITLE_BAR_IS_DARK)
            apply_semantic_button_styles(watched)
        return False


def _button_role(text: str) -> str:
    normalized = str(text or "").replace("&", "").strip()
    if any(word in normalized for word in _DANGER_BUTTON_WORDS):
        return "danger"
    if any(word in normalized for word in _SECONDARY_BUTTON_WORDS):
        return "secondary"
    if any(word in normalized for word in _SUCCESS_BUTTON_WORDS):
        return "success"
    if any(word in normalized for word in _PRIMARY_BUTTON_WORDS):
        return "primary"
    return "secondary"


def apply_semantic_button_styles(root: QWidget) -> None:
    """画面内ボタンへ用途別の共通スタイル属性を付ける。"""
    buttons = []
    if isinstance(root, (QPushButton, QToolButton)):
        buttons.append(root)
    buttons.extend(root.findChildren(QPushButton))
    buttons.extend(root.findChildren(QToolButton))
    for button in buttons:
        # 登録状態表示や編集画面ツールバーなど、専用背景色を持つボタンは維持する。
        if button.objectName() in {"dangerButton", "successButton"}:
            continue
        inline_style = button.styleSheet()
        if "background-color" in inline_style or "background:" in inline_style:
            continue
        if button.property("buttonRole") in {"olapFetch", "olapUpdate"}:
            continue
        role = _button_role(button.text())
        if button.property("buttonRole") == role:
            continue
        button.setProperty("buttonRole", role)
        style = button.style()
        style.unpolish(button)
        style.polish(button)


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
        apply_semantic_button_styles(widget)
