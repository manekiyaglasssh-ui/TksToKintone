from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton, QToolButton, QWidget

from app.gui import DARK_STYLESHEET, LIGHT_STYLESHEET
from app.voucher_edit_window import (
    REFLECT_TARGET_DARK_STYLE,
    REFLECT_TARGET_LIGHT_STYLE,
    REFLECT_TARGET_SELECTED_STYLE,
)
from app.theme_utils import (
    SEMANTIC_BUTTON_STYLESHEET,
    apply_semantic_button_styles,
)


class TestSemanticButtonStyles(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_buttons_receive_roles_by_operation(self) -> None:
        root = QWidget()
        run = QPushButton("実行", root)
        save = QPushButton("保存", root)
        add = QPushButton("行追加", root)
        delete = QPushButton("行削除", root)
        tool = QToolButton(root)
        tool.setText("プレビュー")

        apply_semantic_button_styles(root)

        self.assertEqual(run.property("buttonRole"), "primary")
        self.assertEqual(save.property("buttonRole"), "success")
        self.assertEqual(add.property("buttonRole"), "secondary")
        self.assertEqual(delete.property("buttonRole"), "danger")
        self.assertEqual(tool.property("buttonRole"), "primary")

    def test_styles_cover_danger_disabled_and_readable_text(self) -> None:
        self.assertIn('buttonRole="danger"', SEMANTIC_BUTTON_STYLESHEET)
        self.assertIn('buttonRole="success"', SEMANTIC_BUTTON_STYLESHEET)
        self.assertIn('buttonRole="olapFetch"', SEMANTIC_BUTTON_STYLESHEET)
        self.assertIn('buttonRole="olapUpdate"', SEMANTIC_BUTTON_STYLESHEET)
        self.assertIn("[buttonRole]:disabled", SEMANTIC_BUTTON_STYLESHEET)
        self.assertIn("background-color: #747f89", SEMANTIC_BUTTON_STYLESHEET)
        self.assertIn("color: #ffffff", SEMANTIC_BUTTON_STYLESHEET)
        self.assertIn("border-radius: 6px", SEMANTIC_BUTTON_STYLESHEET)

    def test_light_and_dark_themes_define_readable_controls(self) -> None:
        for stylesheet in (LIGHT_STYLESHEET, DARK_STYLESHEET):
            self.assertIn("QRadioButton::indicator", stylesheet)
            self.assertIn("QRadioButton::indicator:checked", stylesheet)
            self.assertIn("QRadioButton::indicator:disabled", stylesheet)
            self.assertIn("QTabBar::tab", stylesheet)
            self.assertIn("QTabBar::tab:selected", stylesheet)
            self.assertIn("selection-color: #ffffff", stylesheet)
            self.assertIn("QPushButton:disabled, QToolButton:disabled", stylesheet)
            self.assertIn("border-radius: 6px", stylesheet)
            self.assertIn(
                'QPushButton[reflectTargetButton="true"]:checked',
                stylesheet,
            )
            self.assertIn(
                'QPushButton[reflectTargetSelected="true"]',
                stylesheet,
            )
            self.assertIn("background-color: #0d6efd", stylesheet)
            self.assertIn("color: #ffffff", stylesheet)
            self.assertIn("border: 2px solid #66b2ff", stylesheet)
            self.assertIn(
                'QPushButton[reflectTargetSelected="true"]:disabled',
                stylesheet,
            )
            self.assertGreater(
                stylesheet.rfind('QPushButton[reflectTargetButton="true"]:checked'),
                stylesheet.rfind("QPushButton:disabled, QToolButton:disabled"),
            )

    def test_reflect_target_direct_styles_keep_selected_state_blue(self) -> None:
        self.assertIn("background-color: #0d6efd", REFLECT_TARGET_SELECTED_STYLE)
        self.assertIn("color: #ffffff", REFLECT_TARGET_SELECTED_STYLE)
        self.assertIn("border: 2px solid #66b2ff", REFLECT_TARGET_SELECTED_STYLE)
        self.assertIn("font-weight: bold", REFLECT_TARGET_SELECTED_STYLE)
        for normal_style in (REFLECT_TARGET_LIGHT_STYLE, REFLECT_TARGET_DARK_STYLE):
            self.assertNotIn("background-color: #0d6efd", normal_style)
            self.assertIn("font-weight: normal", normal_style)


if __name__ == "__main__":
    unittest.main()
