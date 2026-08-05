"""指図書編集ヘッダーの直接配置・折り返し回帰テスト。"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QToolBar, QToolButton

from app.voucher_edit_window import VoucherEditWindow


class TestVoucherEditHeaderLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_header_is_wrapped_widget_and_all_operations_are_direct(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("TKS_TO_KINTONE_HOME")
            os.environ["TKS_TO_KINTONE_HOME"] = home
            try:
                win = VoucherEditWindow(order_no="header", background_pdf_bytes=b"")
                self.addCleanup(win.deleteLater)
                header = win._main_toolbar
                self.assertIsNotNone(header)
                self.assertNotIsInstance(header, QToolBar)
                win.show()
                self.app.processEvents()
                self.assertEqual(win._favorite_font_button.width(), 64)
                self.assertTrue(win._favorite_font_button.toolTip())
                self.assertIn("background: transparent", win._favorite_font_button.styleSheet())
                self.assertIn("border: none", win._favorite_font_button.styleSheet())
                self.assertIsInstance(win._line_width_spin, QDoubleSpinBox)
                self.assertEqual(win._line_width_group.layout().itemAt(1).widget(),
                                 win._line_width_spin)
                self.assertGreaterEqual(win._line_width_spin.width(), 64)
                self.assertEqual(win._line_width_spin.sizePolicy().horizontalPolicy().name,
                                 "Fixed")
                labels = [action.text() for action in header.actions() if action.text()]
                labels += [button.text() for button in header.findChildren(QToolButton)]
                labels.append(win._shape_tool_button.text())
                expected = [
                    "↶", "↷", "選択", "テキスト", "図形", "画像挿入", "貼り付け",
                    "削除", "プレビュー", "保存", "保存して閉じる", "閉じる", "全画面", "タブレット",
                ]
                for label in expected:
                    self.assertIn(label, labels)
                for action in header.actions():
                    widget = header.widgetForAction(action)
                    if action.text() in expected:
                        self.assertIsInstance(widget, QToolButton)
                        self.assertNotEqual(widget.objectName(), "qt_toolbar_ext_button")
                self.assertEqual(header.horizontalScrollBarPolicy().name, "ScrollBarAlwaysOff")
                widgets = [header.widgetForAction(action) for action in header.actions()]
                widgets = [widget for widget in widgets if widget is not None and widget.isVisible()]
                self.assertGreater(len(widgets), 10)
                self.assertTrue(all(widget.geometry().top() >= 0 for widget in widgets))
                self.assertTrue(all(widget.geometry().bottom() <= header.height() for widget in widgets))
                self.assertEqual(
                    [widget.geometry().x() for widget in widgets],
                    sorted(widget.geometry().x() for widget in widgets),
                )
                self.assertEqual(header.minimumWidth(), 0)
                self.assertEqual(header.minimumSizeHint().width(), 0)
            finally:
                if previous is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous

    def test_favorite_font_is_yellow_star_without_button_background(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("TKS_TO_KINTONE_HOME")
            os.environ["TKS_TO_KINTONE_HOME"] = home
            try:
                win = VoucherEditWindow(order_no="favorite-header", background_pdf_bytes=b"")
                self.addCleanup(win.deleteLater)
                button = win._favorite_font_button
                self.assertIn(button.text(), ("☆", "★"))
                self.assertIn("background: transparent", win._main_toolbar.styleSheet())
                self.assertIn("color: #F2B705", win._main_toolbar.styleSheet())
                self.assertTrue(button.toolTip())
                self.assertIn("background-color: transparent", button.styleSheet())
                self.assertNotIn("background-color: #", button.styleSheet())
            finally:
                if previous is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous

    def test_header_fits_actual_contents_rect_at_all_target_window_widths(self) -> None:
        """配置確定後の実 contentsRect に全文表示の全要素が安全余裕付きで収まる。"""
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("TKS_TO_KINTONE_HOME")
            previous_font = QFont(self.app.font())
            test_font = QFont(previous_font)
            test_font.setPointSize(9)
            self.app.setFont(test_font)
            os.environ["TKS_TO_KINTONE_HOME"] = home
            try:
                win = VoucherEditWindow(order_no="dpi-header", background_pdf_bytes=b"")
                self.addCleanup(win.deleteLater)
                header = win._main_toolbar
                win._default_maximize_applied = True
                win.showNormal()
                self.app.processEvents()
                expected_modes = {1920: "NORMAL", 1536: "NORMAL", 1280: "COMPACT", 1260: "COMPACT"}
                for target_width in (1920, 1536, 1280, 1260):
                    win.resize(target_width, 800)
                    self.app.processEvents()
                    header.layout().activate()
                    self.app.processEvents()
                    rect = header.contentsRect()
                    widgets = [header.widgetForAction(action) for action in header.actions()]
                    widgets = [widget for widget in widgets if widget is not None]
                    with self.subTest(target_width=target_width):
                        self.assertEqual(win.width(), target_width)
                        self.assertEqual(win.centralWidget().width(), target_width)
                        self.assertEqual(header.width(), target_width)
                        self.assertEqual(win._toolbar_mode, expected_modes[target_width])
                        self.assertTrue(all(widget.isVisible() for widget in widgets))
                        self.assertTrue(all(widget.width() > 0 for widget in widgets))

                        geometries = []
                        for widget in widgets:
                            top_left = widget.mapTo(header, QPoint(0, 0))
                            bottom_right = widget.mapTo(header, widget.rect().bottomRight())
                            geometries.append((top_left.x(), top_left.y(), bottom_right.x(), bottom_right.y()))
                            self.assertGreaterEqual(top_left.x(), rect.left())
                            self.assertLessEqual(bottom_right.x(), rect.right() - 8)
                            self.assertGreaterEqual(top_left.y(), rect.top())
                            self.assertLessEqual(bottom_right.y(), rect.bottom())
                        for previous_widget, next_widget in zip(geometries, geometries[1:]):
                            self.assertLess(previous_widget[2], next_widget[0])
                            self.assertLessEqual(previous_widget[1], next_widget[3])
                            self.assertLessEqual(next_widget[1], previous_widget[3])

                        rightmost = max(geometry[2] for geometry in geometries)
                        overflow = rightmost - (rect.right() - 8)
                        self.assertLessEqual(overflow, 0)
                        self.assertGreaterEqual(rect.right() - rightmost, 8)

                        for action in header.actions():
                            if action.text() not in (
                                "プレビュー", "保存", "保存して閉じる", "閉じる", "全画面", "タブレット"
                            ):
                                continue
                            button = header.widgetForAction(action)
                            left = button.mapTo(header, QPoint(0, 0)).x()
                            right = button.mapTo(header, button.rect().topRight()).x()
                            self.assertGreaterEqual(left, rect.left())
                            self.assertLessEqual(right, rect.right() - 8)
                            readable = header.fontMetrics().horizontalAdvance(button.text()) + 8
                            self.assertGreaterEqual(button.width(), readable)

                label = win._line_width_group.layout().itemAt(0).widget().geometry()
                spin = win._line_width_spin.geometry()
                gap = spin.left() - label.right() - 1
                self.assertGreaterEqual(gap, 2)
                self.assertLessEqual(gap, 4)
                self.assertEqual(win._line_width_group.sizePolicy().horizontalPolicy().name, "Fixed")
                self.assertEqual(win._line_width_spin.sizePolicy().horizontalPolicy().name, "Fixed")
            finally:
                self.app.setFont(previous_font)
                if previous is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
