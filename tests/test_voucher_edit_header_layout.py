"""指図書編集ヘッダーの直接配置・折り返し回帰テスト。"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QDoubleSpinBox, QSizePolicy, QSpacerItem, QToolBar, QToolButton,
)

from app.gui import THEME_DARK, THEME_LIGHT, apply_theme
from app.theme_utils import SEMANTIC_BUTTON_STYLESHEET
from app.voucher_edit_window import (
    EDIT_TOOLBAR_DARK_STYLE,
    EDIT_TOOLBAR_LIGHT_STYLE,
    EDIT_TOOLBAR_STYLE,
    FAVORITE_FONT_ICON_SIZE_PX,
    VoucherEditWindow,
)


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
                self.assertEqual(FAVORITE_FONT_ICON_SIZE_PX, 24)
                self.assertIn("font-size: 24px", button.styleSheet())
            finally:
                if previous is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous

    def test_theme_roles_survive_density_and_light_dark_reapplication(self) -> None:
        """密度変更は用途別の通常・削除・プレビュー・保存色を変更しない。"""
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("TKS_TO_KINTONE_HOME")
            os.environ["TKS_TO_KINTONE_HOME"] = home
            try:
                win = VoucherEditWindow(order_no="header-theme", background_pdf_bytes=b"")
                self.addCleanup(win.deleteLater)
                header = win._main_toolbar
                by_text = {
                    action.text(): header.widgetForAction(action)
                    for action in header.actions() if action.text()
                }
                expected_roles = {
                    "削除": "danger",
                    "プレビュー": "primary",
                    "保存": "success",
                    "保存して閉じる": "success",
                    "全画面": "secondary",
                    "タブレット": "secondary",
                }
                self.assertIn('QToolButton[buttonRole="secondary"]', SEMANTIC_BUTTON_STYLESHEET)
                self.assertIn('QToolButton[buttonRole="primary"]', SEMANTIC_BUTTON_STYLESHEET)
                self.assertIn('QToolButton[buttonRole="success"]', SEMANTIC_BUTTON_STYLESHEET)
                self.assertIn('QToolButton[buttonRole="danger"]', SEMANTIC_BUTTON_STYLESHEET)
                self.assertIn("background-color: #c62828", EDIT_TOOLBAR_STYLE)
                self.assertIn("background-color: #0b7a3b", EDIT_TOOLBAR_STYLE)

                for width in (1920, 1400, 1280):
                    win._apply_toolbar_density(header, width)
                    for theme in (THEME_LIGHT, THEME_DARK):
                        apply_theme(theme)
                        win._apply_toolbar_theme()
                        win.show()
                        self.app.processEvents()
                        for text, role in expected_roles.items():
                            self.assertEqual(by_text[text].property("buttonRole"), role)
                        expected_colors = {
                            "削除": "#c62828",
                            "プレビュー": "#1565c0",
                            "保存": "#0b7a3b",
                            "保存して閉じる": "#0b7a3b",
                            "全画面": "#546e7a",
                            "タブレット": "#546e7a",
                        }
                        for text, color in expected_colors.items():
                            button = by_text[text]
                            image = button.grab().toImage()
                            rendered = image.pixelColor(5, button.height() // 2).name()
                            self.assertEqual(rendered, color, (theme, text, rendered))
                # テーマQSSは背景色を通常ボタンへ直接強制せず、共通role色を遮らない。
                self.assertNotIn("QToolButton {", EDIT_TOOLBAR_LIGHT_STYLE)
                self.assertNotIn("QToolButton {", EDIT_TOOLBAR_DARK_STYLE)
                self.assertIn(
                    win._favorite_font_button.property("favorite"),
                    (True, False, "true", "false"),
                )
                self.assertIn(win._favorite_font_button.text(), ("☆", "★"))
            finally:
                apply_theme(THEME_LIGHT)
                if previous is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous

    def test_header_has_only_one_trailing_expanding_spacer_and_fixed_controls(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("TKS_TO_KINTONE_HOME")
            os.environ["TKS_TO_KINTONE_HOME"] = home
            try:
                win = VoucherEditWindow(order_no="header-spacing", background_pdf_bytes=b"")
                self.addCleanup(win.deleteLater)
                win._default_maximize_applied = True
                win.showNormal()
                win.resize(1920, 800)
                self.app.processEvents()
                header = win._main_toolbar
                layout = header.layout()
                spacers = [
                    index for index in range(layout.count())
                    if layout.itemAt(index).spacerItem() is not None
                ]
                self.assertEqual(spacers, [layout.count() - 1])
                self.assertIsInstance(layout.itemAt(spacers[0]).spacerItem(), QSpacerItem)
                widgets = [header.widgetForAction(action) for action in header.actions()]
                widgets = [widget for widget in widgets if widget is not None]
                for widget in widgets:
                    self.assertNotIn(
                        widget.sizePolicy().horizontalPolicy(),
                        (QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding),
                    )
                self.assertEqual(win._font_family_combo.sizePolicy().horizontalPolicy(),
                                 QSizePolicy.Policy.Fixed)
                self.assertEqual(win._font_size_spin.sizePolicy().horizontalPolicy(),
                                 QSizePolicy.Policy.Fixed)
                self.assertEqual(win._line_width_spin.sizePolicy().horizontalPolicy(),
                                 QSizePolicy.Policy.Fixed)
                self.assertEqual(win._line_width_group.sizePolicy().horizontalPolicy(),
                                 QSizePolicy.Policy.Fixed)
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
                            self.assertLessEqual(next_widget[0] - previous_widget[2] - 1, 8)
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

    def test_first_show_is_synchronously_laid_out_without_resize_wait(self) -> None:
        """showEventが返る時点で密度・順序・末尾stretchが確定している。"""
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("TKS_TO_KINTONE_HOME")
            os.environ["TKS_TO_KINTONE_HOME"] = home
            try:
                win = VoucherEditWindow(order_no="first-show", background_pdf_bytes=b"")
                self.addCleanup(win.deleteLater)
                win._default_maximize_applied = True
                win.resize(1536, 800)  # restoreGeometry後の通常表示に相当
                win.showNormal()
                header = win._main_toolbar
                self.assertEqual(win._toolbar_mode, "NORMAL")
                self.assertTrue(header.updatesEnabled())
                widgets = [header.widgetForAction(action) for action in header.actions()]
                widgets = [widget for widget in widgets if widget is not None]
                positions = [widget.mapTo(header, QPoint(0, 0)).x() for widget in widgets]
                self.assertEqual(positions, sorted(positions))
                self.assertTrue(all(widget.isVisible() for widget in widgets))
                self.assertEqual(
                    [i for i in range(header.layout().count())
                     if header.layout().itemAt(i).spacerItem() is not None],
                    [header.layout().count() - 1],
                )
                rightmost = max(
                    widget.mapTo(header, widget.rect().topRight()).x() for widget in widgets)
                self.assertLessEqual(rightmost, header.contentsRect().right() - 8)
            finally:
                if previous is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous

    def test_normal_buttons_have_natural_horizontal_proportions(self) -> None:
        """100%相当のNORMAL文字ボタンは左右8px余白と横長比率を持つ。"""
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("TKS_TO_KINTONE_HOME")
            os.environ["TKS_TO_KINTONE_HOME"] = home
            try:
                win = VoucherEditWindow(order_no="normal-size", background_pdf_bytes=b"")
                self.addCleanup(win.deleteLater)
                header = win._main_toolbar
                self.assertEqual(win._apply_toolbar_density(header, 1920), "NORMAL")
                self.assertEqual(header.property("headerMode"), "NORMAL")
                by_text = {
                    action.text(): header.widgetForAction(action)
                    for action in header.actions() if action.text()
                }
                for text in ("選択", "テキスト", "画像挿入", "保存して閉じる", "タブレット"):
                    button = by_text[text]
                    self.assertGreaterEqual(
                        button.width(), header.fontMetrics().horizontalAdvance(text) + 16)
                    self.assertGreaterEqual(button.width() / button.minimumHeight(), 1.15)
                self.assertGreaterEqual(win._shape_tool_button.width(), 58)
                self.assertIn('padding-left: 8px', EDIT_TOOLBAR_STYLE)
                self.assertIn('padding-right: 8px', EDIT_TOOLBAR_STYLE)
            finally:
                if previous is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
