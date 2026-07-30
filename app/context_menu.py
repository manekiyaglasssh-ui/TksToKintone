"""Qt標準編集メニューの動作を保ったまま、日本語ラベルへ統一する。"""
from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QLineEdit, QMenu, QPlainTextEdit, QTextEdit

_STANDARD_LABELS = {
    QKeySequence.StandardKey.Undo: "元に戻す",
    QKeySequence.StandardKey.Redo: "やり直す",
    QKeySequence.StandardKey.Cut: "切り取り",
    QKeySequence.StandardKey.Copy: "コピー",
    QKeySequence.StandardKey.Paste: "貼り付け",
    QKeySequence.StandardKey.SelectAll: "すべて選択",
    QKeySequence.StandardKey.Delete: "削除",
}
_TEXT_FALLBACKS = {
    "undo": "元に戻す", "redo": "やり直す", "cut": "切り取り",
    "copy": "コピー", "paste": "貼り付け", "delete": "削除",
    "select all": "すべて選択", "clear": "クリア", "remove": "削除",
    "edit": "編集", "open": "開く", "save": "保存", "reset": "リセット",
}


def _action_role_label(action: QAction) -> str | None:
    shortcut = action.shortcut()
    if not shortcut.isEmpty():
        for standard_key, label in _STANDARD_LABELS.items():
            if shortcut.matches(QKeySequence(standard_key)) == QKeySequence.SequenceMatch.ExactMatch:
                return label
    text = action.text().replace("&", "").split("\t", 1)[0]
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    text = text.removesuffix("...").removesuffix("…").strip().lower()
    return _TEXT_FALLBACKS.get(text)


def localize_standard_context_menu(menu: QMenu) -> QMenu:
    """QAction自体は置換せず、標準役割とshortcutを保ったままtextだけ翻訳する。"""
    for action in menu.actions():
        if action.isSeparator():
            continue
        label = _action_role_label(action)
        if label:
            action.setText(label)
        submenu = action.menu()
        if submenu is not None:
            localize_standard_context_menu(submenu)
    return menu


def create_japanese_standard_context_menu(widget) -> QMenu:
    return localize_standard_context_menu(widget.createStandardContextMenu())


class JapaneseContextMenuFilter(QObject):
    """アプリ所有の標準テキスト編集widgetを横断的に日本語化する。"""
    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if (
            event.type() == QEvent.Type.ContextMenu
            and isinstance(watched, (QLineEdit, QTextEdit, QPlainTextEdit))
            and watched.contextMenuPolicy() == Qt.ContextMenuPolicy.DefaultContextMenu
        ):
            menu = create_japanese_standard_context_menu(watched)
            menu.exec(event.globalPos())
            menu.deleteLater()
            return True
        return False


def install_japanese_context_menus(app) -> JapaneseContextMenuFilter:
    existing = getattr(app, "_japanese_context_menu_filter", None)
    if existing is not None:
        return existing
    context_filter = JapaneseContextMenuFilter(app)
    app.installEventFilter(context_filter)
    app._japanese_context_menu_filter = context_filter
    return context_filter
