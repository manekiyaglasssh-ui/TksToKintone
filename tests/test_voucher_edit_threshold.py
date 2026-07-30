"""指図書編集画面の画像処理拡張のテスト（要件4〜13）。

二値化／背景を透過（閾値）／閾値設定ダイアログ／背景を戻す（対象拡張）と、
左ペインのボタン表示・表記を検証する。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QRectF, QSettings
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication, QDialog

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False


def _isolated_qsettings_patch(ini_path: str):
    """QSettings(org, app) を一時 ini ファイルへ固定する patch を返す。

    Qt はプロセス内で org/app の保存先パスをキャッシュするため XDG 環境変数の
    上書きだけでは隔離できない。モジュールの QSettings を ini ファイル指定の
    インスタンスへ差し替え、実ユーザー設定を汚さないようにする。
    """
    def _factory(*_args, **_kwargs):
        return QSettings(ini_path, QSettings.Format.IniFormat)

    return mock.patch("app.voucher_edit_window.QSettings", side_effect=_factory)


def _png_bytes(pixels: list[list[int]]) -> bytes:
    """2次元の 0xAARRGGBB 配列から PNG バイト列を作る。"""
    from app.voucher_edit_window import qimage_to_png_bytes

    h = len(pixels)
    w = len(pixels[0])
    image = QImage(w, h, QImage.Format.Format_ARGB32)
    for y in range(h):
        for x in range(w):
            image.setPixelColor(x, y, QColor.fromRgba(pixels[y][x]))
    return qimage_to_png_bytes(image)


def _white_with_black_line() -> bytes:
    """左列だけ黒、ほかは白の 3x2 画像。"""
    W = 0xFFFFFFFF  # 不透明な白
    B = 0xFF000000  # 不透明な黒
    return _png_bytes([[B, W, W], [B, W, W]])


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestImageProcessingHelpers(unittest.TestCase):
    """二値化・閾値透過の純粋関数（輝度判定: 要件3・4・6・7・10）。"""

    def _binarized(self, src: bytes, threshold):
        from app.voucher_edit_window import make_binarized_bytes

        image = QImage()
        image.loadFromData(make_binarized_bytes(src, threshold))
        return image.convertToFormat(QImage.Format.Format_ARGB32)

    def _transparent(self, src: bytes, threshold):
        from app.voucher_edit_window import make_threshold_transparent_bytes

        image = QImage()
        image.loadFromData(make_threshold_transparent_bytes(src, threshold))
        return image.convertToFormat(QImage.Format.Format_ARGB32)

    def test_is_light_background_pixel_uses_brightness(self) -> None:
        from app.voucher_edit_window import is_light_background_pixel

        th = (200, 200, 200)
        # 輝度がしきい値平均(200)以上なら背景扱い。
        self.assertTrue(is_light_background_pixel(230, 230, 230, th))
        self.assertFalse(is_light_background_pixel(180, 180, 180, th))
        self.assertFalse(is_light_background_pixel(20, 20, 20, th))

    # ── 二値化（要件6・10）─────────────────────────────────────────────────────
    def test_binarize_light_gray_becomes_white(self) -> None:
        # threshold=(200,200,200) で (230,230,230) は白（要件10）。
        c = self._binarized(_png_bytes([[0xFFE6E6E6]]), (200, 200, 200)).pixelColor(0, 0)
        self.assertEqual((c.red(), c.green(), c.blue()), (255, 255, 255))

    def test_binarize_dark_gray_becomes_black(self) -> None:
        # (180,180,180) は黒（要件10）。
        c = self._binarized(_png_bytes([[0xFFB4B4B4]]), (200, 200, 200)).pixelColor(0, 0)
        self.assertEqual((c.red(), c.green(), c.blue()), (0, 0, 0))

    def test_binarize_near_black_becomes_black(self) -> None:
        # (20,20,20) は黒（要件10）。
        c = self._binarized(_png_bytes([[0xFF141414]]), (200, 200, 200)).pixelColor(0, 0)
        self.assertEqual((c.red(), c.green(), c.blue()), (0, 0, 0))

    def test_binarize_keeps_size(self) -> None:
        image = self._binarized(_white_with_black_line(), (200, 200, 200))
        self.assertEqual((image.width(), image.height()), (3, 2))

    def test_binarize_keeps_alpha(self) -> None:
        # alpha が維持される（要件6・10）。半透明の明るい画素と暗い画素。
        light = 0x80E6E6E6  # alpha=128, 230 gray
        dark = 0x40141414   # alpha=64, 20 gray
        image = self._binarized(_png_bytes([[light, dark]]), (200, 200, 200))
        self.assertEqual(image.pixelColor(0, 0).alpha(), 128)
        self.assertEqual(image.pixelColor(1, 0).alpha(), 64)

    # ── 背景透過（閾値）（要件7・10）────────────────────────────────────────────
    def test_threshold_light_gray_becomes_transparent(self) -> None:
        # (230,230,230) は alpha=0（要件10）。
        self.assertEqual(
            self._transparent(_png_bytes([[0xFFE6E6E6]]), (200, 200, 200)).pixelColor(0, 0).alpha(),
            0,
        )

    def test_threshold_near_black_keeps_opaque(self) -> None:
        # (20,20,20) は alpha=255 のまま（要件10）。
        self.assertEqual(
            self._transparent(_png_bytes([[0xFF141414]]), (200, 200, 200)).pixelColor(0, 0).alpha(),
            255,
        )

    def test_threshold_keeps_black_line(self) -> None:
        image = self._transparent(_white_with_black_line(), (200, 200, 200))
        self.assertEqual((image.width(), image.height()), (3, 2))
        self.assertEqual(image.pixelColor(2, 0).alpha(), 0)   # 白背景は透明
        self.assertEqual(image.pixelColor(0, 0).alpha(), 255)  # 黒線は残る

    def test_threshold_handles_existing_alpha(self) -> None:
        # 既に alpha のある画像でも壊れない（要件10）。半透明の黒は不透明側を維持。
        dark = 0x40141414  # alpha=64, 20 gray
        image = self._transparent(_png_bytes([[dark]]), (200, 200, 200))
        # 暗い画素は背景判定されず、元の alpha(64) を維持する。
        self.assertEqual(image.pixelColor(0, 0).alpha(), 64)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestThresholdSettings(unittest.TestCase):
    """閾値設定の保存・既定・プリセット（要件9）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # QSettings(org, app) を一時 ini ファイルへ隔離し、実設定を汚さない。
        self._tmp = tempfile.TemporaryDirectory()
        ini = os.path.join(self._tmp.name, "settings.ini")
        self._patch = _isolated_qsettings_patch(ini)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_default_is_mid(self) -> None:
        from app.voucher_edit_window import load_threshold_rgb

        # 未設定時の初期値は中(90,90,90)（要件2）。
        self.assertEqual(load_threshold_rgb(), (90, 90, 90))

    def test_save_and_load_roundtrip(self) -> None:
        from app.voucher_edit_window import load_threshold_rgb, save_threshold_rgb

        save_threshold_rgb((10, 20, 30))
        self.assertEqual(load_threshold_rgb(), (10, 20, 30))

    def test_dialog_initial_is_mid_and_presets(self) -> None:
        from app.voucher_edit_window import ThresholdSettingsDialog

        dialog = ThresholdSettingsDialog((90, 90, 90))
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.values(), (90, 90, 90))
        dialog._apply_preset("low")
        self.assertEqual(dialog.values(), (60, 60, 60))
        dialog._apply_preset("mid")
        self.assertEqual(dialog.values(), (90, 90, 90))
        dialog._apply_preset("high")
        self.assertEqual(dialog.values(), (120, 120, 120))

    def test_preset_syncs_slider_and_spinbox(self) -> None:
        # プリセット押下でスライダーと数値入力の両方が同期する（要件7）。
        from app.voucher_edit_window import ThresholdSettingsDialog

        dialog = ThresholdSettingsDialog((90, 90, 90))
        self.addCleanup(dialog.deleteLater)
        dialog._apply_preset("high")
        for key in ("r", "g", "b"):
            self.assertEqual(dialog._sliders[key].value(), 120)
            self.assertEqual(dialog._spins[key].value(), 120)

    # ── 数値入力欄の表示幅（不具合修正: 要件4・7）─────────────────────────────
    def test_spinbox_minimum_width_sufficient(self) -> None:
        # R/G/B すべての数値入力欄に十分な最小幅があり、同じ幅になる（要件4）。
        from app.voucher_edit_window import ThresholdSettingsDialog

        dialog = ThresholdSettingsDialog((90, 90, 90))
        self.addCleanup(dialog.deleteLater)
        widths = []
        for key in ("r", "g", "b"):
            width = dialog._spins[key].minimumWidth()
            self.assertGreaterEqual(width, 72)
            widths.append(width)
        # 全行で同じ幅（要件4）。
        self.assertEqual(len(set(widths)), 1)

    def test_spinbox_initial_value_visible(self) -> None:
        # 初期値 90 が数値入力欄に設定されている（要件7）。
        from app.voucher_edit_window import ThresholdSettingsDialog

        dialog = ThresholdSettingsDialog((90, 90, 90))
        self.addCleanup(dialog.deleteLater)
        for key in ("r", "g", "b"):
            self.assertEqual(dialog._spins[key].value(), 90)

    def test_spinbox_value_after_presets(self) -> None:
        # 低/中/高 ボタン押下後、数値入力欄に 60/90/120 が設定される（要件7）。
        from app.voucher_edit_window import ThresholdSettingsDialog

        dialog = ThresholdSettingsDialog((90, 90, 90))
        self.addCleanup(dialog.deleteLater)
        for preset, expected in (("low", 60), ("mid", 90), ("high", 120)):
            dialog._apply_preset(preset)
            for key in ("r", "g", "b"):
                self.assertEqual(dialog._spins[key].value(), expected)

    def test_spinbox_width_holds_max_value(self) -> None:
        # 255 を設定しても数値欄が潰れない幅（最小幅）を保つ（要件7）。
        from app.voucher_edit_window import ThresholdSettingsDialog

        dialog = ThresholdSettingsDialog((90, 90, 90))
        self.addCleanup(dialog.deleteLater)
        for key in ("r", "g", "b"):
            dialog._spins[key].setValue(255)
            self.assertEqual(dialog._spins[key].value(), 255)
            self.assertGreaterEqual(dialog._spins[key].minimumWidth(), 72)

    def test_spinbox_change_updates_slider(self) -> None:
        # R/G/B の数値入力を変えると対応スライダーも変わる（要件7）。
        from app.voucher_edit_window import ThresholdSettingsDialog

        dialog = ThresholdSettingsDialog((90, 90, 90))
        self.addCleanup(dialog.deleteLater)
        for key, value in (("r", 10), ("g", 20), ("b", 30)):
            dialog._spins[key].setValue(value)
            self.assertEqual(dialog._sliders[key].value(), value)

    def test_slider_change_updates_spinbox(self) -> None:
        # スライダーを動かすと対応する数値入力も更新される（要件7）。
        from app.voucher_edit_window import ThresholdSettingsDialog

        dialog = ThresholdSettingsDialog((90, 90, 90))
        self.addCleanup(dialog.deleteLater)
        dialog._sliders["g"].setValue(42)
        self.assertEqual(dialog._spins["g"].value(), 42)

    def test_input_range_is_0_to_255(self) -> None:
        from app.voucher_edit_window import ThresholdSettingsDialog

        dialog = ThresholdSettingsDialog((90, 90, 90))
        self.addCleanup(dialog.deleteLater)
        for key in ("r", "g", "b"):
            self.assertEqual(
                (dialog._spins[key].minimum(), dialog._spins[key].maximum()), (0, 255)
            )
            self.assertEqual(
                (dialog._sliders[key].minimum(), dialog._sliders[key].maximum()), (0, 255)
            )

    # ── カスタム（要件3〜6）─────────────────────────────────────────────────────
    def test_custom_save_and_load_roundtrip(self) -> None:
        from app.voucher_edit_window import (
            load_custom_threshold_rgb,
            save_custom_threshold_rgb,
        )

        self.assertIsNone(load_custom_threshold_rgb())  # 未保存時は None
        save_custom_threshold_rgb((80, 85, 90))
        self.assertEqual(load_custom_threshold_rgb(), (80, 85, 90))

    def test_apply_custom_values_updates_channels(self) -> None:
        from app.voucher_edit_window import (
            ThresholdSettingsDialog,
            save_custom_threshold_rgb,
        )

        save_custom_threshold_rgb((80, 85, 90))
        dialog = ThresholdSettingsDialog((90, 90, 90))
        self.addCleanup(dialog.deleteLater)
        dialog._apply_custom_values()
        self.assertEqual(dialog.values(), (80, 85, 90))
        # スライダー・数値入力の両方が反映される（要件5・7）。
        self.assertEqual(dialog._spins["g"].value(), 85)
        self.assertEqual(dialog._sliders["b"].value(), 90)

    def test_apply_custom_values_no_saved_does_not_crash(self) -> None:
        from app.voucher_edit_window import ThresholdSettingsDialog

        dialog = ThresholdSettingsDialog((90, 90, 90))
        self.addCleanup(dialog.deleteLater)
        with mock.patch(
            "app.voucher_edit_window.QMessageBox.information"
        ) as info:
            dialog._apply_custom_values()
        info.assert_called_once()
        # 未保存時は値が変わらない（要件5）。
        self.assertEqual(dialog.values(), (90, 90, 90))

    def test_save_current_as_custom_persists(self) -> None:
        from app.voucher_edit_window import (
            ThresholdSettingsDialog,
            load_custom_threshold_rgb,
        )

        dialog = ThresholdSettingsDialog((40, 50, 60))
        self.addCleanup(dialog.deleteLater)
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            dialog._save_current_as_custom()
        # QSettings に保存され、再読込相当で取得できる（要件6）。
        self.assertEqual(load_custom_threshold_rgb(), (40, 50, 60))


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditThresholdButtons(unittest.TestCase):
    """左ペインのボタン表示・表記と加工→復元（要件4〜13）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._prev_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        self._settings_tmp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = self._tmp.name
        # QSettings(org, app) を一時 ini ファイルへ隔離し、実設定を汚さない。
        ini = os.path.join(self._settings_tmp.name, "settings.ini")
        self._patch = _isolated_qsettings_patch(ini)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        if self._prev_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._prev_home
        self._tmp.cleanup()
        self._settings_tmp.cleanup()

    def _make_window(self):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="THRESH", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        return win

    def _add_selected_image(self, win, image_bytes: bytes | None = None):
        item = win.add_image(
            image_bytes or _white_with_black_line(),
            rect=QRectF(100.0, 100.0, 30.0, 20.0),
            select=True,
        )
        self.assertIsNotNone(item)
        return item

    def _image_processing_menu(self, win, item):
        menu = win._build_object_context_menu(item)
        for submenu in getattr(menu, "_submenus", []):
            if submenu.objectName() == "image_processing_menu":
                return submenu
        return None

    # ── 表示条件（要件3・13）─────────────────────────────────────────────────
    def test_buttons_hidden_when_no_selection(self) -> None:
        win = self._make_window()
        win._scene.clearSelection()
        win._update_image_action_buttons()
        self.assertIsNone(win._image_actions_label)
        self.assertIsNotNone(win._favorite_list)

    def test_buttons_hidden_for_non_image_selection(self) -> None:
        win = self._make_window()
        text = win.add_text_rect(QRectF(10.0, 20.0, 80.0, 24.0),
                                 text="t", auto_edit=False)
        win._select_only(text)
        self.assertIsNone(win._image_actions_label)
        self.assertIsNone(self._image_processing_menu(win, text))

    def test_all_buttons_visible_when_image_selected(self) -> None:
        win = self._make_window()
        item = self._add_selected_image(win)
        processing = self._image_processing_menu(win, item)
        self.assertIsNotNone(processing)
        self.assertIn("二値化", [a.text() for a in processing.actions()])

    def test_button_order_under_label(self) -> None:
        # 左ペインには画像処理ではなくお気に入り一覧を表示する。
        win = self._make_window()
        layout = win._template_panel_layout
        widgets = [
            layout.itemAt(i).widget()
            for i in range(layout.count())
            if layout.itemAt(i).widget() is not None
        ]
        self.assertIn(win._favorite_list, widgets)

    # ── ボタン表記（要件4・5・13）──────────────────────────────────────────────
    def test_button_labels_two_lines(self) -> None:
        win = self._make_window()
        item = self._add_selected_image(win)
        processing = self._image_processing_menu(win, item)
        labels = [a.text() for a in processing.actions()]
        self.assertNotIn("背景を透過 （rembg）", labels)
        self.assertIn("背景を透過 （閾値）", labels)
        self.assertIn("二値化", labels)
        self.assertIn("背景を戻す", labels)
        self.assertIn("閾値設定", labels)

    # ── 二値化（要件6・13）──────────────────────────────────────────────────────
    def test_binarize_keeps_geometry_and_selection(self) -> None:
        win = self._make_window()
        item = self._add_selected_image(win)
        before = item.box_rect_scene()
        win._on_binarize()
        after = item.box_rect_scene()
        self.assertAlmostEqual(before.x(), after.x())
        self.assertAlmostEqual(before.y(), after.y())
        self.assertAlmostEqual(before.width(), after.width())
        self.assertAlmostEqual(before.height(), after.height())
        self.assertTrue(item.isSelected())
        # 白は白・黒は黒のまま（要件6）。
        image = QImage()
        image.loadFromData(item.image_bytes)
        image = image.convertToFormat(QImage.Format.Format_ARGB32)
        self.assertEqual(image.pixelColor(2, 0).red(), 255)
        self.assertEqual(image.pixelColor(0, 0).red(), 0)

    # ── 背景を透過（閾値）（要件7・13）─────────────────────────────────────────
    def test_threshold_transparent_keeps_geometry_and_selection(self) -> None:
        win = self._make_window()
        item = self._add_selected_image(win)
        before = item.box_rect_scene()
        win._on_threshold_transparent()
        after = item.box_rect_scene()
        self.assertAlmostEqual(before.width(), after.width())
        self.assertAlmostEqual(before.height(), after.height())
        self.assertTrue(item.isSelected())
        image = QImage()
        image.loadFromData(item.image_bytes)
        image = image.convertToFormat(QImage.Format.Format_ARGB32)
        # 白背景は透明、黒線は残る（要件7）。
        self.assertEqual(image.pixelColor(2, 0).alpha(), 0)
        self.assertEqual(image.pixelColor(0, 0).alpha(), 255)

    # ── 画面反映（要件8・10）───────────────────────────────────────────────────
    def test_binarize_calls_apply_processed_image(self) -> None:
        from app.voucher_edit_window import _EditImageItem

        win = self._make_window()
        item = self._add_selected_image(win)
        with mock.patch.object(_EditImageItem, "apply_processed_image",
                               autospec=True) as apply_mock:
            win._on_binarize()
        apply_mock.assert_called_once()
        self.assertIs(apply_mock.call_args.args[0], item)

    def test_threshold_transparent_calls_apply_processed_image(self) -> None:
        from app.voucher_edit_window import _EditImageItem

        win = self._make_window()
        item = self._add_selected_image(win)
        with mock.patch.object(_EditImageItem, "apply_processed_image",
                               autospec=True) as apply_mock:
            win._on_threshold_transparent()
        apply_mock.assert_called_once()
        self.assertIs(apply_mock.call_args.args[0], item)

    # ── 背景を戻す（対象拡張: 要件10・13）─────────────────────────────────────
    def test_restore_after_binarize(self) -> None:
        win = self._make_window()
        item = self._add_selected_image(win)
        original = bytes(item.image_bytes)
        win._on_binarize()
        self.assertTrue(item.has_original_image())
        win._on_restore_image()
        self.assertEqual(item.image_bytes, original)
        self.assertTrue(item.isSelected())

    def test_restore_after_threshold_transparent(self) -> None:
        win = self._make_window()
        item = self._add_selected_image(win)
        original = bytes(item.image_bytes)
        win._on_threshold_transparent()
        win._on_restore_image()
        self.assertEqual(item.image_bytes, original)

    def test_restore_returns_first_original_after_multiple(self) -> None:
        # 二値化→閾値透過 と複数回加工しても最初の元画像へ戻る（要件10・11）。
        win = self._make_window()
        item = self._add_selected_image(win)
        original = bytes(item.image_bytes)
        win._on_binarize()
        win._on_threshold_transparent()
        win._on_restore_image()
        self.assertEqual(item.image_bytes, original)

    # ── 閾値設定ダイアログ（要件8・9・13）──────────────────────────────────────
    def test_settings_ok_saves(self) -> None:
        from app.voucher_edit_window import ThresholdSettingsDialog, load_threshold_rgb

        win = self._make_window()
        with mock.patch.object(ThresholdSettingsDialog, "exec",
                               return_value=QDialog.DialogCode.Accepted), \
                mock.patch.object(ThresholdSettingsDialog, "values",
                                  return_value=(220, 220, 220)):
            win._on_threshold_settings()
        self.assertEqual(win._threshold_rgb, (220, 220, 220))
        self.assertEqual(load_threshold_rgb(), (220, 220, 220))

    def test_settings_cancel_does_not_change(self) -> None:
        from app.voucher_edit_window import ThresholdSettingsDialog, load_threshold_rgb

        win = self._make_window()
        before = win._threshold_rgb
        before_saved = load_threshold_rgb()
        with mock.patch.object(ThresholdSettingsDialog, "exec",
                               return_value=QDialog.DialogCode.Rejected), \
                mock.patch.object(ThresholdSettingsDialog, "values",
                                  return_value=(10, 10, 10)):
            win._on_threshold_settings()
        # 通常閾値は変更されない（要件8）。
        self.assertEqual(win._threshold_rgb, before)
        self.assertEqual(load_threshold_rgb(), before_saved)

    def test_custom_save_survives_cancel(self) -> None:
        # カスタム保存後に Cancel しても、カスタム値は保存済みとして残る（要件8・10）。
        from app.voucher_edit_window import (
            ThresholdSettingsDialog,
            load_custom_threshold_rgb,
            save_custom_threshold_rgb,
        )

        win = self._make_window()
        save_custom_threshold_rgb((33, 44, 55))
        with mock.patch.object(ThresholdSettingsDialog, "exec",
                               return_value=QDialog.DialogCode.Rejected), \
                mock.patch.object(ThresholdSettingsDialog, "values",
                                  return_value=(10, 10, 10)):
            win._on_threshold_settings()
        self.assertEqual(load_custom_threshold_rgb(), (33, 44, 55))


if __name__ == "__main__":
    unittest.main()
