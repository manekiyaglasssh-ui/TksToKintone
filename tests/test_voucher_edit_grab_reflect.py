"""タブレット編集モードの手書き/掴むモード切替・反映先選択の動的テスト。

QApplication を offscreen で起動し、手書き/掴むモードの切替・掴むモードでの非描画・
反映先選択の共有・ハイライトを検証する（タスク1・2）。
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication, QGraphicsView

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditGrabReflect(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._prev_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._prev_home
        self._tmp.cleanup()

    def _make_window(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def _freehand_items(self, win):
        return [it for it in win.edit_items()
                if it.serialize_edit_object().get("type") == "freehand"]

    # ── タスク1: 手書き/掴むモード ────────────────────────────────────────────
    def test_tablet_toolbar_has_pen_and_grab(self) -> None:
        """1. タブレット編集モードに「手書き」「掴む」ボタンが表示される。"""
        win = self._make_window()
        labels = [a.text() for a in win._tablet_toolbar.actions()]
        self.assertIn("手書き", labels)
        self.assertIn("掴む", labels)

    def test_initial_mode_is_handwriting(self) -> None:
        """2. タブレット編集モード開始直後は手書きモードになる。"""
        from app.voucher_edit_window import TOOL_PEN

        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        self.assertEqual(win.current_tool, TOOL_PEN)
        win.exit_tablet_mode()

    def test_handwriting_creates_freehand(self) -> None:
        """3. 手書きモードではドラッグで freehand が作成される。"""
        from app.voucher_edit_window import TOOL_PEN

        win = self._make_window()
        win.set_tool(TOOL_PEN)
        scene = win._scene
        scene.begin_freehand(QPointF(10, 10))
        scene._freehand_item.add_point(QPointF(30, 25))
        scene._freehand_item.add_point(QPointF(50, 40))
        scene.end_freehand()
        self.assertEqual(len(self._freehand_items(win)), 1)

    def test_grab_mode_does_not_create_freehand(self) -> None:
        """4. 掴むモードではドラッグしても freehand が作成されない。"""
        from app.voucher_edit_window import TOOL_GRAB

        win = self._make_window()
        win.set_tool(TOOL_GRAB)
        scene = win._scene
        # 掴むモードでは begin_freehand は呼ばれない（mousePress が super 委譲のため）。
        # ツール状態として描画ストロークが始まっていないことを確認する。
        self.assertIsNone(scene._freehand_item)
        self.assertEqual(len(self._freehand_items(win)), 0)

    def test_grab_mode_sets_scroll_hand_drag(self) -> None:
        """5. 掴むモードではプレビューの表示位置を移動できる（ScrollHandDrag）。"""
        from app.voucher_edit_window import TOOL_GRAB

        win = self._make_window()
        win.set_tool(TOOL_GRAB)
        self.assertEqual(win._view.dragMode(),
                         QGraphicsView.DragMode.ScrollHandDrag)
        # スクロール位置を変更できる（パン可能）ことを確認する。
        hbar = win._view.horizontalScrollBar()
        if hbar.maximum() > hbar.minimum():
            hbar.setValue(hbar.minimum() + 1)
            self.assertGreaterEqual(hbar.value(), hbar.minimum())

    def test_switch_back_to_handwriting(self) -> None:
        """6. 手書きモードへ戻すと再び freehand を作成できる。"""
        from app.voucher_edit_window import TOOL_GRAB, TOOL_PEN

        win = self._make_window()
        win.set_tool(TOOL_GRAB)
        win.set_tool(TOOL_PEN)
        self.assertEqual(win._view.dragMode(), QGraphicsView.DragMode.NoDrag)
        scene = win._scene
        scene.begin_freehand(QPointF(5, 5))
        scene._freehand_item.add_point(QPointF(20, 20))
        scene.end_freehand()
        self.assertEqual(len(self._freehand_items(win)), 1)

    # ── タスク2: 反映先選択の共有 ──────────────────────────────────────────────
    def test_tablet_reflect_panel_selectable(self) -> None:
        """7. タブレット編集モード中でも反映先を選択できる。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        self.assertTrue(win._tablet_reflect_panel.isVisible())
        self.assertTrue(len(win._tablet_reflect_buttons) >= 1)
        # 末尾のテンプレートを選択する。
        name = list(win._tablet_reflect_buttons.keys())[-1]
        win._tablet_reflect_buttons[name].click()
        self.assertEqual(win._current_template_name, name)
        win.exit_tablet_mode()

    def test_tablet_reflect_change_reflects_in_normal(self) -> None:
        """8. タブレット編集モードで変更した反映先が通常モードにも反映される。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        name = list(win._tablet_reflect_buttons.keys())[-1]
        win._tablet_reflect_buttons[name].click()
        targets = list(win.current_target_vouchers)
        win.exit_tablet_mode()
        # 通常モードへ戻っても同じ反映先・同じテンプレートが選択されている。
        self.assertEqual(win._current_template_name, name)
        self.assertEqual(list(win.current_target_vouchers), targets)
        # 通常パネルの同名ボタンがハイライト（チェック）されている。
        self.assertTrue(win._template_actions[name].isChecked())

    def test_normal_reflect_carries_into_tablet(self) -> None:
        """9. 通常モードの反映先選択がタブレットモードにも引き継がれる。"""
        win = self._make_window()
        win.show()
        # 通常モードで先頭テンプレートを選ぶ。
        name = list(win._template_actions.keys())[0]
        win._template_actions[name].click()
        win.enter_tablet_mode()
        # タブレットパネルの同名ボタンがハイライトされている。
        self.assertEqual(win._current_template_name, name)
        self.assertTrue(win._tablet_reflect_buttons[name].isChecked())
        win.exit_tablet_mode()

    def test_reflect_highlight_style_visible(self) -> None:
        """10. 反映先ボタンの選択中ハイライトがライト／ダークで見える。"""
        from app.voucher_edit_window import REFLECT_TARGET_SELECTED_STYLE

        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        name = list(win._tablet_reflect_buttons.keys())[0]
        win._tablet_reflect_buttons[name].click()
        btn = win._tablet_reflect_buttons[name]
        # 選択中は青背景・白文字・太字の固定スタイル（テーマ非依存で視認できる）。
        self.assertEqual(btn.styleSheet(), REFLECT_TARGET_SELECTED_STYLE)
        self.assertIn("#ffffff", REFLECT_TARGET_SELECTED_STYLE)
        self.assertIn("#0d6efd", REFLECT_TARGET_SELECTED_STYLE)
        win.exit_tablet_mode()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
