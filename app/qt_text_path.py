"""Qt glyph outlines shared by the editor and the edit-text PDF renderer."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF
from PySide6.QtGui import QFont, QFontMetricsF, QPainterPath, QGuiApplication

_HEADLESS_QT_APP: QGuiApplication | None = None


def ensure_qt_application() -> QGuiApplication:
    """Return the process Qt application, creating one for headless PDF tests."""
    global _HEADLESS_QT_APP
    existing = QGuiApplication.instance()
    if existing is not None:
        return existing
    # QFontDatabase aborts the process when used without a QGuiApplication.
    # Production GUI/PDF workers already share the application; this branch is
    # for CLI/tests that generate a PDF without opening the editor.
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # QApplication is required because the editor tests and some PDF preview
    # paths construct widgets after generating a path.
    from PySide6.QtWidgets import QApplication
    _HEADLESS_QT_APP = QApplication([])
    return _HEADLESS_QT_APP


def build_text_path(text: str, font: QFont, *, line_height: float | None = None) -> tuple[QPainterPath, QRectF]:
    """Create the same Qt glyph outlines used by QGraphicsTextItem.

    The path is in the editor's logical scene coordinates, with its origin at
    the top-left and a baseline per line. Font decorations are deliberately
    disabled by the caller; they are drawn as shared logical geometry.
    """
    ensure_qt_application()
    # Local import keeps the module usable in lightweight geometry tests.
    from PySide6.QtCore import QRectF
    result = QPainterPath()
    metrics = QFontMetricsF(font)
    spacing = float(line_height if line_height is not None else metrics.lineSpacing())
    lines = str(text or "").splitlines() or [""]
    for index, line in enumerate(lines):
        result.addText(QPointF(0.0, metrics.ascent() + index * spacing), font, line)
    return result, QRectF(result.boundingRect())


def painter_path_to_reportlab(path: QPainterPath, pdf_path: Any, *, page_height: float,
                              x_offset: float = 0.0, y_offset: float = 0.0) -> None:
    """Copy Qt move/line/cubic/close elements into a ReportLab path.

    Quadratic elements are emitted by Qt as cubic elements; the explicit
    branch is retained for bindings/backends that expose them directly.
    """
    for index in range(path.elementCount()):
        element = path.elementAt(index)
        x = float(element.x) + x_offset
        y = page_height - (float(element.y) + y_offset)
        kind_value = getattr(element.type, "value", element.type)
        kind = int(kind_value)
        if kind == QPainterPath.ElementType.MoveToElement.value:
            pdf_path.moveTo(x, y)
        elif kind == QPainterPath.ElementType.LineToElement.value:
            pdf_path.lineTo(x, y)
        elif kind == QPainterPath.ElementType.CurveToElement.value:
            if index + 2 >= path.elementCount():
                continue
            c1 = path.elementAt(index)
            c2 = path.elementAt(index + 1)
            end = path.elementAt(index + 2)
            pdf_path.curveTo(
                float(c1.x) + x_offset, page_height - (float(c1.y) + y_offset),
                float(c2.x) + x_offset, page_height - (float(c2.y) + y_offset),
                float(end.x) + x_offset, page_height - (float(end.y) + y_offset),
            )
        # QPainterPath does not expose a CloseSubpathElement in PySide6;
        # filled ReportLab subpaths are implicitly closed.


def draw_qt_text_path_on_pdf(canvas: Any, text: str, font: QFont,
                             scene_x: float, scene_y: float, *, page_height: float,
                             line_height: float | None = None) -> tuple[float, float, float, float]:
    """Fill a PDF path made from Qt glyph outlines and return its scene bounds."""
    path, bounds = build_text_path(text, font, line_height=line_height)
    pdf_path = canvas.beginPath()
    painter_path_to_reportlab(path, pdf_path, page_height=page_height,
                              x_offset=scene_x, y_offset=scene_y)
    canvas.drawPath(pdf_path, stroke=0, fill=1)
    return (scene_x + bounds.x(), scene_y + bounds.y(),
            bounds.width(), bounds.height())
