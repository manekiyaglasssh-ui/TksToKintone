import unittest
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QPainterPath

from app.qt_text_path import build_text_path, painter_path_to_reportlab


class _Path:
    def __init__(self): self.commands = []
    def moveTo(self, *v): self.commands.append(("move", v))
    def lineTo(self, *v): self.commands.append(("line", v))
    def curveTo(self, *v): self.commands.append(("curve", v))
    def close(self): self.commands.append(("close", ()))


class _Canvas:
    def __init__(self): self.lines = []
    def beginPath(self): return _Path()
    def drawPath(self, *_args, **_kwargs): pass
    def setFillColorRGB(self, *_args): pass
    def setStrokeColorRGB(self, *_args): pass
    def setLineWidth(self, *_args): pass
    def line(self, *args): self.lines.append(args)


class QtTextPathTests(unittest.TestCase):
    def test_japanese_path_and_cubic_conversion(self):
        path, bounds = build_text_path("こんにちは", QFont("Sans", 20))
        self.assertGreater(path.elementCount(), 0)
        self.assertGreater(bounds.width(), 0)
        target = _Path()
        painter_path_to_reportlab(path, target, page_height=100)
        self.assertTrue(any(item[0] == "curve" for item in target.commands))

    def test_move_line_and_close_compatible_path(self):
        path = QPainterPath()
        path.moveTo(1, 2); path.lineTo(3, 4); path.closeSubpath()
        target = _Path()
        painter_path_to_reportlab(path, target, page_height=10)
        self.assertEqual(target.commands[0], ("move", (1.0, 8.0)))
        self.assertEqual(target.commands[1], ("line", (3.0, 6.0)))

    def test_decoration_flags_and_qt_metric_positions(self):
        from app import voucher_service
        for underline, strikeout, expected in (
            (False, False, 0), (True, False, 1),
            (False, True, 1), (True, True, 2)):
            canvas = _Canvas()
            voucher_service.draw_text_in_scene_rect(
                canvas, "こんにちは", 10, 20, 180, 60, "unused", 24,
                underline=underline, strikeout=strikeout,
                font_metadata={"requested_family": "DejaVu Sans"},
                object_id="decorations")
            self.assertEqual(len(canvas.lines), expected)
            if expected == 2:
                # The two metric positions remain distinct after the PDF
                # Y-axis inversion; the first line is the requested underline.
                self.assertLess(canvas.lines[0][1], canvas.lines[1][1])


if __name__ == "__main__":
    unittest.main()
