"""指図書編集画面の画像「背景を透過」（rembg）／「背景を戻す」のテスト（要件1〜8）。

rembg 本体は重く実行環境にも依存するため、ユニットテストでは rembg.remove を
モックして確認する（要件8）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False


def _solid_png_bytes(width: int = 6, height: int = 4, color: int = 0xFFFFFFFF) -> bytes:
    from app.voucher_edit_window import qimage_to_png_bytes

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    return qimage_to_png_bytes(image)


def _fake_remove_transparent(input_bytes: bytes) -> bytes:
    """rembg.remove の代役。入力と同じサイズの全透過PNGを返す。

    サイズは入力画像と一致させ、内容（alpha=0）だけ変える。これにより
    「サイズ不変」「画像が差し替わる」を同時に検証できる。
    """
    from app.voucher_edit_window import qimage_to_png_bytes

    src = QImage()
    src.loadFromData(bytes(input_bytes))
    out = QImage(src.width() or 1, src.height() or 1, QImage.Format.Format_ARGB32)
    out.fill(0x00000000)  # 全透過
    return qimage_to_png_bytes(out)


@contextmanager
def _patch_rembg(remove_func):
    """sys.modules に rembg のダミーを注入し、remove を差し替える。"""
    fake = types.ModuleType("rembg")
    fake.remove = remove_func
    with mock.patch.dict(sys.modules, {"rembg": fake}), \
            mock.patch("importlib.metadata.version", return_value="0.0"):
        yield fake


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestTransparentBackgroundHelper(unittest.TestCase):
    def test_calls_rembg_remove_and_returns_png(self) -> None:
        from app.voucher_edit_window import make_transparent_background_bytes

        remove = mock.Mock(side_effect=_fake_remove_transparent)
        src = _solid_png_bytes(6, 4)
        with _patch_rembg(remove):
            result = make_transparent_background_bytes(src)
        remove.assert_called_once()  # rembg.remove が呼ばれる（要件8）
        image = QImage()
        image.loadFromData(result)
        image = image.convertToFormat(QImage.Format.Format_ARGB32)
        # サイズ不変・透過済み（要件1・8）。
        self.assertEqual((image.width(), image.height()), (6, 4))
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)

    def test_raises_when_rembg_remove_fails(self) -> None:
        from app.voucher_edit_window import (
            BackgroundRemovalError,
            make_transparent_background_bytes,
        )

        remove = mock.Mock(side_effect=RuntimeError("boom"))
        with _patch_rembg(remove):
            with self.assertRaises(BackgroundRemovalError):
                make_transparent_background_bytes(_solid_png_bytes())

    def test_raises_when_rembg_missing(self) -> None:
        from app.voucher_edit_window import (
            BackgroundRemovalError,
            make_transparent_background_bytes,
        )

        # rembg を import 不可にする。
        with mock.patch.dict(sys.modules, {"rembg": None}), \
                mock.patch("importlib.metadata.version", return_value="0.0"):
            with self.assertRaises(BackgroundRemovalError):
                make_transparent_background_bytes(_solid_png_bytes())

    def test_sets_u2net_home_to_bundled_model_dir(self) -> None:
        # remove() 前に U2NET_HOME を同梱モデルフォルダへ設定する（要件4・8）。
        from app.voucher_edit_window import (
            _rembg_model_dir,
            make_transparent_background_bytes,
        )

        remove = mock.Mock(side_effect=_fake_remove_transparent)
        prev = os.environ.pop("U2NET_HOME", None)
        try:
            with _patch_rembg(remove):
                make_transparent_background_bytes(_solid_png_bytes())
            self.assertEqual(os.environ.get("U2NET_HOME"), str(_rembg_model_dir()))
        finally:
            if prev is None:
                os.environ.pop("U2NET_HOME", None)
            else:
                os.environ["U2NET_HOME"] = prev

    def test_raises_clear_error_when_model_missing(self) -> None:
        # u2net.onnx が無い場合、パスを含む分かりやすいエラーにする（要件5・8）。
        from app.voucher_edit_window import (
            BackgroundRemovalError,
            make_transparent_background_bytes,
        )

        with tempfile.TemporaryDirectory() as empty_dir:
            with mock.patch(
                "app.voucher_edit_window._rembg_model_dir",
                return_value=__import__("pathlib").Path(empty_dir),
            ):
                with self.assertRaises(BackgroundRemovalError) as ctx:
                    make_transparent_background_bytes(_solid_png_bytes())
        msg = str(ctx.exception)
        self.assertIn("背景透過モデルが見つかりません", msg)
        self.assertIn("u2net.onnx", msg)

    def test_rembg_import_error_detail_in_message(self) -> None:
        # rembg import 失敗時、元例外の型・内容をメッセージへ含める（要件2・8）。
        from app.voucher_edit_window import (
            BackgroundRemovalError,
            make_transparent_background_bytes,
        )

        with mock.patch.dict(sys.modules, {"rembg": None}), \
                mock.patch("importlib.metadata.version", return_value="0.0"):
            with self.assertRaises(BackgroundRemovalError) as ctx:
                make_transparent_background_bytes(_solid_png_bytes())
        msg = str(ctx.exception)
        self.assertIn("rembg の読み込みに失敗しました", msg)
        # 元例外の型名（ImportError 等）が含まれる。
        self.assertTrue("Error" in msg or "import" in msg.lower())

    def test_onnxruntime_missing_detail_in_message(self) -> None:
        # onnxruntime 不足を模した import 失敗の詳細をメッセージへ含める（要件2・8）。
        from app.voucher_edit_window import (
            BackgroundRemovalError,
            make_transparent_background_bytes,
        )

        def _raise_no_onnx(*_args, **_kwargs):
            raise ModuleNotFoundError("No module named 'onnxruntime'")

        # png は patch 前に作る（生成側の import を巻き込まないため）。
        png = _solid_png_bytes()
        with mock.patch("builtins.__import__", side_effect=_raise_no_onnx):
            with self.assertRaises(BackgroundRemovalError) as ctx:
                make_transparent_background_bytes(png)
        msg = str(ctx.exception)
        self.assertIn("onnxruntime", msg)
        self.assertIn("ModuleNotFoundError", msg)

    def test_metadata_version_called_before_rembg_remove(self) -> None:
        from app.voucher_edit_window import make_transparent_background_bytes

        remove = mock.Mock(side_effect=_fake_remove_transparent)
        with _patch_rembg(remove), \
                mock.patch("importlib.metadata.version", return_value="0.0") as version:
            make_transparent_background_bytes(_solid_png_bytes())
        version.assert_any_call("pymatting")

    def test_pyinstaller_metadata_path_checked(self) -> None:
        from app.voucher_edit_window import make_transparent_background_bytes

        remove = mock.Mock(side_effect=_fake_remove_transparent)
        with _patch_rembg(remove), \
                mock.patch("app.voucher_edit_window._ensure_pyinstaller_metadata_path") as ensure:
            make_transparent_background_bytes(_solid_png_bytes())
        ensure.assert_called()

    def test_package_not_found_retried_once_and_success(self) -> None:
        import importlib.metadata as metadata
        from app.voucher_edit_window import make_transparent_background_bytes

        remove = mock.Mock(side_effect=_fake_remove_transparent)
        with _patch_rembg(remove), \
                mock.patch(
                    "importlib.metadata.version",
                    side_effect=[metadata.PackageNotFoundError("pymatting"), "0.0"],
                ):
            result = make_transparent_background_bytes(_solid_png_bytes())
        self.assertTrue(result)
        remove.assert_called_once()

    def test_package_not_found_second_failure_keeps_detail(self) -> None:
        import importlib.metadata as metadata
        from app.voucher_edit_window import (
            BackgroundRemovalError,
            make_transparent_background_bytes,
        )

        remove = mock.Mock(side_effect=_fake_remove_transparent)
        with _patch_rembg(remove), \
                mock.patch(
                    "importlib.metadata.version",
                    side_effect=[
                        metadata.PackageNotFoundError("pymatting"),
                        metadata.PackageNotFoundError("pymatting"),
                    ],
                ):
            with self.assertRaises(BackgroundRemovalError) as ctx:
                make_transparent_background_bytes(_solid_png_bytes())
        self.assertIn("PackageNotFoundError", str(ctx.exception))


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditTransparentButtons(unittest.TestCase):
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

    def _make_window(self, debug_visible: bool):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="TRANSP", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        win.set_debug_visible(debug_visible)
        return win

    def _add_selected_image(self, win, image_bytes: bytes | None = None,
                            start_warmup: bool = False):
        if start_warmup:
            item = win.add_image(image_bytes or _solid_png_bytes(),
                                 rect=QRectF(100.0, 100.0, 40.0, 30.0),
                                 select=True)
        else:
            with mock.patch.object(win, "_start_rembg_warmup_if_needed"):
                item = win.add_image(image_bytes or _solid_png_bytes(),
                                     rect=QRectF(100.0, 100.0, 40.0, 30.0),
                                     select=True)
        self.assertIsNotNone(item)
        return item

    def _wait_bg(self, win, timeout: float = 5.0) -> None:
        """背景透過 worker(別スレッド)の完了と finished/failed スロット配送を待つ。"""
        deadline = time.time() + timeout
        while win._background_removal_running and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        # 完了直後のスロット（quit/deleteLater 等）も配送しておく。
        self.app.processEvents()

    def _run_transparent_and_wait(self, win) -> None:
        """ボタン押下→worker完了までを同期的に進める（テスト用）。"""
        win._on_transparent_background()
        self._wait_bg(win)

    # ── 表示条件（要件2）──────────────────────────────────────────────────────
    def test_buttons_visible_when_debug_off_and_image_selected(self) -> None:
        win = self._make_window(debug_visible=False)
        self._add_selected_image(win)
        self.assertFalse(win._image_actions_label.isHidden())
        self.assertFalse(win._transparent_bg_button.isHidden())
        self.assertFalse(win._restore_image_button.isHidden())
        self.assertEqual(win._image_actions_label.text(), "画像処理")
        self.assertEqual(win._transparent_bg_button.text(), "背景を透過\n（rembg）")
        self.assertEqual(win._restore_image_button.text(), "背景を戻す")

    def test_buttons_hidden_when_debug_on_but_no_selection(self) -> None:
        win = self._make_window(debug_visible=True)
        win._scene.clearSelection()
        win._update_image_action_buttons()
        self.assertTrue(win._image_actions_label.isHidden())
        self.assertTrue(win._transparent_bg_button.isHidden())
        self.assertTrue(win._restore_image_button.isHidden())

    def test_button_enabled_when_image_selected_regardless_of_rembg(self) -> None:
        # rembg の事前判定なしに、画像選択中なら有効（要件2）。
        win = self._make_window(debug_visible=True)
        self._add_selected_image(win)
        self.assertFalse(win._image_actions_label.isHidden())
        self.assertEqual(win._image_actions_label.text(), "画像処理")
        self.assertFalse(win._transparent_bg_button.isHidden())
        self.assertTrue(win._transparent_bg_button.isEnabled())
        self.assertEqual(win._transparent_bg_button.text(), "背景を透過\n（rembg）")
        self.assertEqual(win._restore_image_button.text(), "背景を戻す")
        # 元画像未保存なので「背景を戻す」は無効（要件5）。
        self.assertFalse(win._restore_image_button.isEnabled())

    def test_image_action_label_hidden_for_non_image_selection(self) -> None:
        from PySide6.QtCore import QRectF

        win = self._make_window(debug_visible=False)
        text = win.add_text_rect(QRectF(10.0, 20.0, 80.0, 24.0),
                                 text="t", auto_edit=False)
        win._select_only(text)
        self.assertTrue(win._image_actions_label.isHidden())
        self.assertTrue(win._transparent_bg_button.isHidden())
        self.assertTrue(win._restore_image_button.isHidden())

    def test_image_action_label_hidden_for_multiple_selection(self) -> None:
        from PySide6.QtCore import QRectF

        win = self._make_window(debug_visible=False)
        image = self._add_selected_image(win)
        text = win.add_text_rect(QRectF(10.0, 20.0, 80.0, 24.0),
                                 text="t", auto_edit=False)
        image.setSelected(True)
        text.setSelected(True)
        win._update_image_action_buttons()
        self.assertTrue(win._image_actions_label.isHidden())
        self.assertTrue(win._transparent_bg_button.isHidden())
        self.assertTrue(win._restore_image_button.isHidden())

    def test_old_restore_label_not_used_in_left_pane(self) -> None:
        win = self._make_window(debug_visible=True)
        self._add_selected_image(win)
        labels = [
            win._image_actions_label.text(),
            win._transparent_bg_button.text(),
            win._restore_image_button.text(),
        ]
        self.assertIn("画像処理", labels)
        self.assertIn("背景を透過\n（rembg）", labels)
        self.assertIn("背景を戻す", labels)
        self.assertNotIn("もとに戻す", labels)

    def test_window_creation_does_not_import_rembg(self) -> None:
        # 指図書編集画面の生成時に rembg を import しない（要件1・3）。
        sys.modules.pop("rembg", None)
        self._make_window(debug_visible=True)
        self.assertNotIn("rembg", sys.modules)

    def test_rembg_imported_only_on_button_press(self) -> None:
        # rembg import はボタン押下時にだけ行われる（要件3）。
        win = self._make_window(debug_visible=True)
        self._add_selected_image(win)
        remove = mock.Mock(side_effect=_fake_remove_transparent)
        imported = {"called": False}
        real_import = __import__

        def _tracking_import(name, *args, **kwargs):
            if name == "rembg" or name.startswith("rembg."):
                imported["called"] = True
            return real_import(name, *args, **kwargs)

        with _patch_rembg(remove), \
                mock.patch("builtins.__import__", side_effect=_tracking_import):
            self.assertFalse(imported["called"])  # 押下前は未 import
            self._run_transparent_and_wait(win)
        self.assertTrue(imported["called"])  # 押下時に import される

    def test_image_selection_starts_rembg_warmup(self) -> None:
        win = self._make_window(debug_visible=True)
        with mock.patch.object(win, "_start_rembg_warmup_if_needed") as warmup:
            self._add_selected_image(win, start_warmup=True)
        warmup.assert_called()

    # ── 透過処理（要件1・3・5・8）─────────────────────────────────────────────
    def test_transparent_replaces_image_and_keeps_geometry(self) -> None:
        win = self._make_window(debug_visible=True)
        item = self._add_selected_image(win)
        before = item.box_rect_scene()
        original = bytes(item.image_bytes)
        remove = mock.Mock(side_effect=_fake_remove_transparent)
        with _patch_rembg(remove):
            self._run_transparent_and_wait(win)
        remove.assert_called_once()  # rembg.remove が呼ばれる（要件8）
        # 画像が差し替わる（要件6・8）。
        self.assertNotEqual(item.image_bytes, original)
        # 位置・サイズは不変（要件3）。
        after = item.box_rect_scene()
        self.assertAlmostEqual(before.x(), after.x())
        self.assertAlmostEqual(before.y(), after.y())
        self.assertAlmostEqual(before.width(), after.width())
        self.assertAlmostEqual(before.height(), after.height())
        # 選択状態を維持（要件1）。
        self.assertTrue(item.isSelected())
        # ピクセル寸法も不変・透過済み（要件8）。
        result = QImage()
        result.loadFromData(item.image_bytes)
        self.assertEqual((result.width(), result.height()), (6, 4))

    def test_restore_returns_first_original_after_multiple(self) -> None:
        win = self._make_window(debug_visible=True)
        item = self._add_selected_image(win)
        original = bytes(item.image_bytes)
        remove = mock.Mock(side_effect=_fake_remove_transparent)
        with _patch_rembg(remove):
            self._run_transparent_and_wait(win)
            self.assertTrue(win._restore_image_button.isEnabled())
            self._run_transparent_and_wait(win)
        win._on_restore_image()
        # 複数回透過しても最初の元画像へ戻る（要件3・4）。
        self.assertEqual(item.image_bytes, original)
        self.assertTrue(item.isSelected())

    # ── 別スレッド実行・ロック（要件1・2）─────────────────────────────────────
    def test_running_flag_true_on_start(self) -> None:
        # 透過開始時に _background_removal_running=True（要件10）。
        win = self._make_window(debug_visible=True)
        self._add_selected_image(win)
        remove = mock.Mock(side_effect=_fake_remove_transparent)
        with _patch_rembg(remove):
            win._on_transparent_background()
            # worker 完了前はフラグが立っている。
            self.assertTrue(win._background_removal_running)
            self._wait_bg(win)
        # 完了後は False に戻る（要件8）。
        self.assertFalse(win._background_removal_running)

    def test_no_double_run_while_running(self) -> None:
        # 透過中に再度押しても二重実行されない（要件2・10）。
        win = self._make_window(debug_visible=True)
        self._add_selected_image(win)
        remove = mock.Mock(side_effect=_fake_remove_transparent)
        with _patch_rembg(remove):
            win._on_transparent_background()
            self.assertTrue(win._background_removal_running)
            # 実行中の再押下は無視される。
            win._on_transparent_background()
            self._wait_bg(win)
        remove.assert_called_once()  # remove は1回だけ（要件10）

    def test_edit_controls_locked_while_running(self) -> None:
        # 透過中は保存・閉じる・削除などが無効（要件2・10）。
        win = self._make_window(debug_visible=True)
        item = self._add_selected_image(win)
        original = bytes(item.image_bytes)
        remove = mock.Mock(side_effect=_fake_remove_transparent)
        with _patch_rembg(remove):
            win._on_transparent_background()
            self.assertTrue(win._background_removal_running)
            # 編集アクションはすべて無効化されている。
            for action in win._edit_actions:
                self.assertFalse(action.isEnabled())
            # ショートカット経由の操作（削除）も透過中は無視される。
            item.setSelected(True)
            win.delete_selected()
            self.assertIn(item, win._scene.items())
            # 保存もガードされる（_persist を呼ばない）。
            with mock.patch.object(win, "_persist") as persist:
                win.save()
                win.save_and_close()
                persist.assert_not_called()
            self._wait_bg(win)
        # 完了後はロック解除（要件8）。
        self.assertTrue(all(a.isEnabled() for a in win._edit_actions))
        self.assertNotEqual(item.image_bytes, original)

    # ── 失敗時の扱い（要件7）──────────────────────────────────────────────────
    def test_failure_does_not_crash_and_notifies(self) -> None:
        win = self._make_window(debug_visible=True)
        item = self._add_selected_image(win)
        original = bytes(item.image_bytes)
        remove = mock.Mock(side_effect=RuntimeError("boom"))
        with _patch_rembg(remove), \
                mock.patch("app.voucher_edit_window.QMessageBox.warning") as warn:
            # 例外でアプリは落ちない（要件7）。
            self._run_transparent_and_wait(win)
        warn.assert_called_once()  # メッセージボックスで通知（要件7）
        # 失敗時は画像も元画像退避も変化しない（要件7）。
        self.assertEqual(item.image_bytes, original)
        self.assertFalse(item.has_original_image())
        # 失敗してもボタン表示は元へ戻す（要件8）。
        self.assertEqual(win._transparent_bg_button.text(), "背景を透過\n（rembg）")
        # 失敗時も running フラグは False に戻る（要件8・10）。
        self.assertFalse(win._background_removal_running)

    def test_failure_message_contains_detail(self) -> None:
        # 失敗通知メッセージに元例外の型・内容が含まれる（要件4・7）。
        win = self._make_window(debug_visible=True)
        self._add_selected_image(win)
        remove = mock.Mock(side_effect=RuntimeError("boom"))
        with _patch_rembg(remove), \
                mock.patch("app.voucher_edit_window.QMessageBox.warning") as warn:
            self._run_transparent_and_wait(win)
        message = warn.call_args.args[2]
        self.assertIn("RuntimeError", message)
        self.assertIn("boom", message)

    def test_button_text_restored_after_success(self) -> None:
        win = self._make_window(debug_visible=True)
        self._add_selected_image(win)
        remove = mock.Mock(side_effect=_fake_remove_transparent)
        with _patch_rembg(remove):
            self._run_transparent_and_wait(win)
        # 処理完了後はラベルを「背景を透過（rembg）」へ戻す（要件8）。
        self.assertEqual(win._transparent_bg_button.text(), "背景を透過\n（rembg）")

    # ── 実装方針の検証（要件3）────────────────────────────────────────────────
    def test_source_does_not_use_process_events(self) -> None:
        # UIスレッドでの processEvents は使わない（白画面・再入防止: 要件3）。
        import re
        from pathlib import Path

        src = Path(
            sys.modules["app.voucher_edit_window"].__file__
        ).read_text(encoding="utf-8")
        # コメント行を除いた実コードに processEvents() 呼び出しが無いこと。
        code_lines = [
            ln for ln in src.splitlines()
            if "processEvents" in ln and not ln.lstrip().startswith("#")
        ]
        self.assertEqual(code_lines, [])


if __name__ == "__main__":
    unittest.main()
