"""指図書編集ヘッダーの直接配置・折り返し回帰テスト。"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
                self.assertGreaterEqual(win._favorite_font_button.width(), 32)
                self.assertGreaterEqual(win._favorite_font_button.width(),
                                        win._favorite_font_button.sizeHint().width())
                self.assertTrue(win._favorite_font_button.toolTip())
                self.assertIsInstance(win._line_width_spin, QDoubleSpinBox)
                self.assertEqual(win._line_width_group.layout().itemAt(1).widget(),
                                 win._line_width_spin)
                self.assertEqual(win._line_width_spin.width(), 74)
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
                self.assertGreaterEqual(header.minimumWidth(), header.sizeHint().width())
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
            finally:
                if previous is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
