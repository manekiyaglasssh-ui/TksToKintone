"""指図書編集画面のタブレット編集モードの動的テスト。

QApplication を offscreen で起動し、タブレット編集モードの開始/終了・UI 切替・
外部ディスプレイ移動の呼び出し・編集データ共有・保存形式の互換性を検証する。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditTabletMode(unittest.TestCase):
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

    def test_tablet_button_exists(self) -> None:
        """1. 指図書編集画面に「タブレット編集」ボタンが存在する。"""
        from PySide6.QtWidgets import QToolBar

        win = self._make_window()
        actions = [a.text() for a in win._edit_header_widget.actions()]
        self.assertIn("タブレット", actions)

    def test_enter_sets_tablet_mode_true(self) -> None:
        """2. タブレット編集ボタン押下で tablet_mode が True になる。"""
        win = self._make_window()
        self.assertFalse(win.tablet_mode)
        # ボタン押下は表示先選択を経由する。ダイアログをモックして「現在画面で開始」を選ぶ。
        with mock.patch.object(win, "_select_tablet_screen",
                               return_value=(True, None, False)):
            win._tablet_action.trigger()
        self.assertTrue(win.tablet_mode)
        win.exit_tablet_mode()

    def test_tablet_toolbar_visible_in_mode(self) -> None:
        """3. タブレット編集モード中はタブレット用ツールバーが表示される。"""
        win = self._make_window()
        win.show()
        self.assertIsNotNone(win._tablet_toolbar)
        self.assertFalse(win._tablet_toolbar.isVisible())
        win.enter_tablet_mode()
        self.assertTrue(win._tablet_toolbar.isVisible())
        win.exit_tablet_mode()

    def test_normal_panes_hidden_in_mode(self) -> None:
        """4. タブレット編集モード中は通常の細かいペインが隠れる。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        self.assertFalse(win._template_panel.isVisible())
        self.assertFalse(win._main_toolbar.isVisible())
        win.exit_tablet_mode()

    def test_exit_restores_normal_ui(self) -> None:
        """5. タブレット終了で tablet_mode が False に戻り通常UIが復帰する。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        win.exit_tablet_mode()
        self.assertFalse(win.tablet_mode)
        self.assertFalse(win._tablet_toolbar.isVisible())
        self.assertTrue(win._main_toolbar.isVisible())
        self.assertTrue(win._template_panel.isVisible())

    def test_escape_exits_tablet_mode(self) -> None:
        """6. Esc キーでタブレット編集モードを終了できる。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        self.assertTrue(win.tablet_mode)
        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                          Qt.KeyboardModifier.NoModifier)
        win.keyPressEvent(event)
        self.assertFalse(win.tablet_mode)

    def test_f11_exits_tablet_mode(self) -> None:
        """6'. F11 キーでもタブレット編集モードを終了できる。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_F11,
                          Qt.KeyboardModifier.NoModifier)
        win.keyPressEvent(event)
        self.assertFalse(win.tablet_mode)

    def test_external_display_move_called(self) -> None:
        """7. 外部ディスプレイがある場合、外部ディスプレイへ移動する処理が呼ばれる。"""
        win = self._make_window()
        fake_screen = object()
        with mock.patch.object(win, "_find_tablet_screen", return_value=fake_screen), \
                mock.patch.object(win, "_move_to_screen") as move:
            win.enter_tablet_mode()
        move.assert_called_once_with(fake_screen)
        win.exit_tablet_mode()

    def test_no_external_display_no_error(self) -> None:
        """8. 外部ディスプレイがない場合でもエラーにならない。"""
        win = self._make_window()
        notify = mock.MagicMock()
        with mock.patch.object(win, "_find_tablet_screen", return_value=None), \
                mock.patch.object(win, "_notify_no_external_display", notify):
            win.enter_tablet_mode()
        self.assertTrue(win.tablet_mode)
        notify.assert_called_once()
        win.exit_tablet_mode()

    def test_object_created_in_tablet_persists_in_normal(self) -> None:
        """9. タブレット編集モード中に作成したオブジェクトが通常モードでも残る。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        # タブレット編集はペン中心。手書きストロークを1本追加する。
        win.add_freehand([(10, 10), (30, 25), (60, 40)])
        before = len(win.edit_items())
        self.assertGreaterEqual(before, 1)
        win.exit_tablet_mode()
        # 通常モードへ戻っても編集オブジェクトは保持される。
        self.assertEqual(len(win.edit_items()), before)

    def test_save_format_same_as_normal(self) -> None:
        """10. 保存・読込形式が通常編集と同じである。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        win.add_freehand([(20, 20), (40, 35), (80, 50)])
        # 保存は通常編集と同じ _persist（save_edit_objects）を使う。
        # save() のモーダル完了通知を避けるため _persist() を直接呼ぶ。
        self.assertTrue(win._persist())
        win.exit_tablet_mode()
        serialized = win.serialize_objects()
        # 別の（通常モードのみの）ウィンドウで読み込んでも同じオブジェクトが復元できる。
        win2 = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        self.assertEqual(len(win2.edit_items()), len(serialized))

    def test_tablet_toolbar_is_pen_centric(self) -> None:
        """11. タブレット用ツールバーはペン中心で、図形系ツールは基本表示しない。"""
        from app.voucher_edit_window import (
            TOOL_ARROW,
            TOOL_ELLIPSE,
            TOOL_ERASER,
            TOOL_LINE,
            TOOL_PEN,
            TOOL_RECT,
            TOOL_TEXT,
        )

        win = self._make_window()
        # ペン・消しゴムは並ぶ。
        self.assertIn(TOOL_PEN, win._tablet_tool_actions)
        self.assertIn(TOOL_ERASER, win._tablet_tool_actions)
        # テキスト・線・矢印・四角・丸は基本表示しない。
        for tool in (TOOL_TEXT, TOOL_LINE, TOOL_ARROW, TOOL_RECT, TOOL_ELLIPSE):
            self.assertNotIn(tool, win._tablet_tool_actions)

    def test_tablet_buttons_visible_in_light_and_dark(self) -> None:
        """12. ライトモード／ダークモードでタブレット用ボタンが見える。"""
        from app.voucher_edit_window import TABLET_TOOLBAR_STYLE

        win = self._make_window()
        # 配色がテーマパレットに依存せず固定（白文字＋濃色背景）で定義されている。
        self.assertIn("color: #ffffff", TABLET_TOOLBAR_STYLE)
        self.assertIn("background-color", TABLET_TOOLBAR_STYLE)
        # ボタンサイズの目安（高さ44px前後・横幅60〜72px程度）を満たす定義がある。
        self.assertIn("min-height: 44px", TABLET_TOOLBAR_STYLE)
        self.assertIn("min-width: 64px", TABLET_TOOLBAR_STYLE)
        self.assertEqual(win._tablet_toolbar.styleSheet(), TABLET_TOOLBAR_STYLE)

    def test_find_tablet_screen_prefers_saved(self) -> None:
        """補助: 設定に保存した画面名があれば優先して選ばれる。"""
        win = self._make_window()

        class _FakeScreen:
            def __init__(self, name: str) -> None:
                self._name = name

            def name(self) -> str:
                return self._name

        primary = _FakeScreen("primary")
        ext1 = _FakeScreen("ext1")
        ext2 = _FakeScreen("ext2")
        win._tablet_screen_name = "ext2"
        from app.voucher_edit_window import QGuiApplication

        with mock.patch.object(QGuiApplication, "screens",
                               return_value=[primary, ext1, ext2]), \
                mock.patch.object(QGuiApplication, "primaryScreen",
                                  return_value=primary):
            chosen = win._find_tablet_screen()
        self.assertIs(chosen, ext2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
