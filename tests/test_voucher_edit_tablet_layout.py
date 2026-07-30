"""タブレット編集画面のレイアウト要件テスト。

- 上部メニューが2段に折り返り、右端（保存／タブレット終了）まで見切れず操作できる。
- 左ペインが2列にならず、「反映先」の下に「レイヤー」が縦並びで表示される。
- 左ペインは縦スクロール可能で、選択状態のハイライトが維持される。
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QApplication,
        QScrollArea,
        QToolBar,
        QVBoxLayout,
    )

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が必要です")
class TestVoucherEditTabletLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_home = os.environ.get("TKS_TO_KINTONE_HOME")
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

    # ── 上部メニュー（要件1）──────────────────────────────────────────────────
    def test_toolbar_has_save_button(self) -> None:
        """1. 上部メニューに「保存」が表示される。"""
        win = self._make_window()
        labels = self._all_toolbar_labels(win)
        self.assertIn("保存", labels)

    def test_toolbar_has_exit_button(self) -> None:
        """2. 上部メニューに「タブレット終了」が表示される。"""
        win = self._make_window()
        labels = self._all_toolbar_labels(win)
        self.assertIn("タブレット終了", labels)

    def test_toolbar_is_single_row(self) -> None:
        """3. 上部メニューは1段で構成され、2段目ツールバーは存在しない。"""
        win = self._make_window()
        # 1本の QToolBar に全ボタンが並ぶ。2段目は持たない。
        self.assertIsInstance(win._tablet_toolbar, QToolBar)
        self.assertFalse(hasattr(win, "_tablet_toolbar_row2"))
        # 主要15ボタンがすべて同じ1段（1本のツールバー）に並ぶ。
        labels = self._all_toolbar_labels(win)
        for expected in ("手書き", "掴む", "消しゴム", "選択", "太さ:中", "色:黒",
                         "元に戻す", "やり直す", "削除", "全消去", "拡大", "縮小",
                         "全体表示", "保存", "タブレット終了"):
            self.assertIn(expected, labels)

    def test_toolbar_horizontal_scroll_fallback(self) -> None:
        """6. 横幅不足時の保険として横スクロール可能（縦は出さない）。"""
        win = self._make_window()
        container = win._tablet_toolbar_container
        self.assertIsInstance(container, QScrollArea)
        self.assertEqual(container.horizontalScrollBarPolicy(),
                         Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.assertEqual(container.verticalScrollBarPolicy(),
                         Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 1段ぶんの薄い高さ（縦領域を節約）。
        self.assertLessEqual(container.maximumHeight(), 72)

    def test_all_toolbar_actions_operable(self) -> None:
        """4. 画面幅が狭くても全メニューが操作可能（全アクションが有効）。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        actions = list(win._tablet_toolbar.actions())
        # セパレータ以外のアクションはすべて有効でクリック可能。
        for act in actions:
            if act.isSeparator():
                continue
            self.assertTrue(act.isEnabled(), f"{act.text()} が無効です")
        win.exit_tablet_mode()

    def _all_toolbar_labels(self, win) -> list[str]:
        return [a.text() for a in win._tablet_toolbar.actions()]

    # ── 左ペイン（要件2）─────────────────────────────────────────────────────
    def test_left_pane_is_single_column(self) -> None:
        """5. 左ペインが2列表示にならない（反映先とレイヤーが同じ1列に縦並び）。"""
        win = self._make_window()
        pane = win._tablet_left_pane
        content = pane.widget()
        layout = content.layout()
        # 左ペインの中身は縦（QVBoxLayout）の1列。横並びレイアウトではない。
        self.assertIsInstance(layout, QVBoxLayout)
        # 反映先パネル・レイヤーパネルとも同じ content を親に持つ（別列に分かれていない）。
        self.assertIs(win._tablet_reflect_panel.parent(), content)
        self.assertIs(win._tablet_layer_panel.parent(), content)

    def test_layer_below_reflect(self) -> None:
        """6. レイヤーメニューが反映先の下に表示される。"""
        win = self._make_window()
        layout = win._tablet_left_pane.widget().layout()
        idx_reflect = layout.indexOf(win._tablet_reflect_panel)
        idx_layer = layout.indexOf(win._tablet_layer_panel)
        self.assertGreaterEqual(idx_reflect, 0)
        self.assertGreaterEqual(idx_layer, 0)
        # 反映先が先（上）、レイヤーが後（下）。
        self.assertLess(idx_reflect, idx_layer)

    def test_reflect_buttons_are_vertical(self) -> None:
        """7. 反映先ボタンが縦並び（QVBoxLayout）で表示される。"""
        win = self._make_window()
        self.assertIsInstance(win._tablet_reflect_layout, QVBoxLayout)
        self.assertGreaterEqual(len(win._tablet_reflect_buttons), 1)

    def test_layer_buttons_below_reflect_vertical(self) -> None:
        """8. レイヤーボタンが反映先ボタンの下に縦並びで表示される。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        # レイヤー一覧は縦並び（QVBoxLayout）。
        self.assertIsInstance(win._tablet_layer_list_layout, QVBoxLayout)
        self.assertGreaterEqual(len(win._tablet_layer_buttons), 1)
        # レイヤーパネルは反映先パネルより下にある。
        layout = win._tablet_left_pane.widget().layout()
        self.assertLess(layout.indexOf(win._tablet_reflect_panel),
                        layout.indexOf(win._tablet_layer_panel))
        win.exit_tablet_mode()

    def test_layer_highlight_kept(self) -> None:
        """9. レイヤー選択状態のハイライトが維持される。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        l1 = win.current_freehand_layer()
        l2 = win.add_freehand_layer()
        win.select_freehand_layer(l1.layer_id)
        self.assertTrue(win._tablet_layer_buttons[l1.layer_id].isChecked())
        self.assertFalse(win._tablet_layer_buttons[l2.layer_id].isChecked())
        win.select_freehand_layer(l2.layer_id)
        self.assertTrue(win._tablet_layer_buttons[l2.layer_id].isChecked())
        self.assertFalse(win._tablet_layer_buttons[l1.layer_id].isChecked())
        win.exit_tablet_mode()

    def test_reflect_highlight_kept(self) -> None:
        """10. 反映先選択状態のハイライトが維持される。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        name = list(win._tablet_reflect_buttons.keys())[-1]
        win._tablet_reflect_buttons[name].click()
        self.assertTrue(win._tablet_reflect_buttons[name].isChecked())
        # 別のテンプレートを選ぶと前の選択は外れる。
        other = list(win._tablet_reflect_buttons.keys())[0]
        if other != name:
            win._tablet_reflect_buttons[other].click()
            self.assertTrue(win._tablet_reflect_buttons[other].isChecked())
            self.assertFalse(win._tablet_reflect_buttons[name].isChecked())
        win.exit_tablet_mode()

    def test_left_pane_vertically_scrollable(self) -> None:
        """11. 左ペインが縦に長い場合、縦スクロールできる。"""
        win = self._make_window()
        pane = win._tablet_left_pane
        self.assertIsInstance(pane, QScrollArea)
        # 縦は必要時にスクロール、横は出さない（1列固定）。
        self.assertEqual(pane.verticalScrollBarPolicy(),
                         Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.assertEqual(pane.horizontalScrollBarPolicy(),
                         Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    # ── プレビュー領域の拡大（要件4）─────────────────────────────────────────
    def test_toolbar_is_thin_for_more_preview(self) -> None:
        """7. 1段化で上部メニューが薄くなり、プレビュー縦領域が広がる。"""
        win = self._make_window()
        container = win._tablet_toolbar_container
        # 2段時（約96px）より薄い1段ぶんの高さで、空いた縦領域をプレビューへ回す。
        self.assertLessEqual(container.maximumHeight(), 72)

    def test_taller_view_shows_voucher_larger(self) -> None:
        """8. プレビュー領域が高いほど、全体表示で伝票が大きく見える。"""
        win = self._make_window()
        win.show()
        view = win._view
        # 縦に狭いビューポートでフィットしたときの拡大率。
        view.resize(800, 560)
        win.fit_page_to_view()
        small = view.transform().m11()
        # 上部メニューが薄くなり縦に広いビューポートになったときの拡大率。
        view.resize(800, 700)
        win.fit_page_to_view()
        large = view.transform().m11()
        # 縦領域が広いほど伝票が大きく表示される。
        self.assertGreater(large, small)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
