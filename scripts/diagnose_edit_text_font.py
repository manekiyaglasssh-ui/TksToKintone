"""Windows実機で編集テキストのQt実解決と輪郭を診断する。"""
from __future__ import annotations

import argparse
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QFontMetricsF, QGuiApplication

from app.qt_text_path import build_text_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", required=True)
    parser.add_argument("--size", type=float, default=36.0)
    parser.add_argument("--text", default="こんにちは")
    parser.add_argument("--bold", action="store_true")
    parser.add_argument("--italic", action="store_true")
    args = parser.parse_args()
    app = QGuiApplication.instance() or QGuiApplication([])
    del app
    font = QFont(args.family)
    font.setPointSizeF(args.size)
    font.setBold(args.bold)
    font.setItalic(args.italic)
    path, bounds = build_text_path(args.text, font)
    print(f"requested family: {args.family}")
    print(f"resolved family: {font.family()}")
    print(f"exact match: {font.exactMatch()}")
    print(f"point size: {font.pointSizeF()}")
    print(f"weight: {font.weight()}")
    print(f"italic: {font.italic()}")
    print(f"underline: {font.underline()}")
    print(f"strikeout: {font.strikeOut()}")
    print(f"glyph count/path elements: {path.elementCount()}")
    print(f"path bounding rect: {bounds.x()},{bounds.y()},{bounds.width()},{bounds.height()}")
    print(f"font metrics ascent/descent: {QFontMetricsF(font).ascent()}/{QFontMetricsF(font).descent()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
