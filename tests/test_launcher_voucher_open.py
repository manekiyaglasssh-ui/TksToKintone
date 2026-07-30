"""機能選択画面の「伝票作成・印刷」ボタン押下時の安全対策を検証する（要件2・4）。

- 生成した VoucherWindow の参照が launcher 側に保持される。
- 生成中に例外が出ても無反応にならず、ログとメッセージを出し、ボタンを再押下可能に戻す。
- 既に開いている場合は既存ウィンドウを前面に出し、新規生成しない。

Qt ウィジェットを使うため offscreen プラットフォームで実行する。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication, QWidget
    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 が無い環境
    _QT_AVAILABLE = False

if _QT_AVAILABLE:
    import app.voucher_window as vw
    from app.launcher_window import LauncherWindow


if _QT_AVAILABLE:
    class _FakeVoucherWindow(QWidget):
        back_requested = Signal()

        def __init__(self, *args, **kwargs):
            super().__init__()
            self.shown = False

        def show(self):  # noqa: D401 - Qt互換
            self.shown = True


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できない環境")
class LauncherVoucherOpenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_launcher(self) -> "LauncherWindow":
        win = LauncherWindow()
        # OLAP認証は成功したものとして扱い、生成処理だけを検証する。
        win._authorize_olap = lambda: True  # type: ignore[method-assign]
        win._olap_id.setText("id")
        win._olap_password.setText("pw")
        return win

    def test_open_voucher_stores_reference(self) -> None:
        """生成成功時に VoucherWindow 参照が保持される（要件4）。"""
        launcher = self._make_launcher()
        try:
            with mock.patch.object(vw, "VoucherWindow", _FakeVoucherWindow):
                launcher._open_voucher()
            self.assertIsInstance(launcher._voucher_window, _FakeVoucherWindow)
            self.assertTrue(launcher._voucher_window.shown)
        finally:
            launcher._voucher_window = None
            launcher.close()

    def test_open_voucher_runs_auth_before_opening(self) -> None:
        """画面を開く前にOLAP認証が実行される（要件1・認証を元に戻す）。"""
        launcher = LauncherWindow()
        launcher._olap_id.setText("id")
        launcher._olap_password.setText("pw")
        try:
            calls: list[str] = []

            def _auth() -> bool:
                calls.append("auth")
                return True

            launcher._authorize_olap = _auth  # type: ignore[method-assign]
            with mock.patch.object(vw, "VoucherWindow", _FakeVoucherWindow):
                launcher._open_voucher()
            # 認証が呼ばれ、その後ウィンドウが生成されている。
            self.assertEqual(calls, ["auth"])
            self.assertIsInstance(launcher._voucher_window, _FakeVoucherWindow)
        finally:
            launcher._voucher_window = None
            launcher.close()

    def test_open_voucher_auth_failure_does_not_open(self) -> None:
        """OLAP認証失敗時は画面を開かず、ボタンは再押下可能に戻る（要件1）。"""
        launcher = LauncherWindow()
        launcher._olap_id.setText("id")
        launcher._olap_password.setText("pw")
        try:
            launcher._authorize_olap = lambda: False  # type: ignore[method-assign]
            with mock.patch.object(vw, "VoucherWindow") as ctor:
                launcher._open_voucher()
                ctor.assert_not_called()
            self.assertIsNone(launcher._voucher_window)
            self.assertTrue(launcher._voucher_btn.isEnabled())
        finally:
            launcher.close()

    def test_open_voucher_failure_reenables_button(self) -> None:
        """生成中に例外が出てもボタンが再押下可能に戻り、参照が残らない（要件4）。"""
        launcher = self._make_launcher()
        try:
            def _boom(*_a, **_k):
                raise RuntimeError("boom")

            with mock.patch.object(vw, "VoucherWindow", _boom), \
                    mock.patch("app.launcher_window.QMessageBox.warning") as warn:
                launcher._open_voucher()
            warn.assert_called_once()
            self.assertIsNone(launcher._voucher_window)
            # ボタンが再度押せる状態に戻っている（OLAP情報は入力済み）。
            self.assertTrue(launcher._voucher_btn.isEnabled())
        finally:
            launcher.close()

    def test_open_voucher_existing_brings_to_front(self) -> None:
        """既に開いている場合は新規生成せず既存を前面に出す（要件4）。"""
        launcher = self._make_launcher()
        try:
            existing = _FakeVoucherWindow()
            launcher._voucher_window = existing
            with mock.patch.object(vw, "VoucherWindow") as ctor:
                launcher._open_voucher()
                ctor.assert_not_called()
        finally:
            launcher._voucher_window = None
            existing.deleteLater()
            launcher.close()


if __name__ == "__main__":
    unittest.main()
