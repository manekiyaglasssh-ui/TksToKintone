from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit

from app.context_menu import create_japanese_standard_context_menu
from app.voucher_window import VoucherWindow


class _MenuOwner:
    def _open_order_range_dialog(self):
        pass


class TestJapaneseContextMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_standard_actions_are_japanese_and_keep_state(self):
        edit = QLineEdit("abc")
        edit.selectAll()
        menu = create_japanese_standard_context_menu(edit)
        actions = [action for action in menu.actions() if not action.isSeparator()]
        labels = {action.text().replace("&", "") for action in actions}
        self.assertIn("コピー", labels)
        self.assertIn("切り取り", labels)
        self.assertIn("すべて選択", labels)
        self.assertFalse(any(label in labels for label in ("Undo", "Redo", "Cut", "Copy", "Paste", "Delete", "Select All")))
        copy_action = next(action for action in actions if action.text() == "コピー")
        self.assertTrue(copy_action.isEnabled())

    def test_range_action_is_added_only_by_voucher_order_input_builder(self):
        normal = QLineEdit()
        normal_labels = {a.text() for a in create_japanese_standard_context_menu(normal).actions()}
        self.assertNotIn("範囲指定", normal_labels)
        order_edit = QLineEdit()
        menu = VoucherWindow._build_order_input_context_menu(_MenuOwner(), order_edit)
        self.assertIn("範囲指定", {a.text() for a in menu.actions()})


if __name__ == "__main__":
    unittest.main()
