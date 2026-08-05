import unittest
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QPainterPath, QGuiApplication

from app.qt_text_path import build_text_path, painter_path_to_reportlab


class _Path:
    def __init__(self): self.commands = []
    def moveTo(self, *v): self.commands.append(("move", v))
    def lineTo(self, *v): self.commands.append(("line", v))
    def curveTo(self, *v): self.commands.append(("curve", v))
    def close(self): self.commands.append(("close", ()))


class QtTextPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QGuiApplication.instance() or QGuiApplication([])

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


if __name__ == "__main__":
    unittest.main()
