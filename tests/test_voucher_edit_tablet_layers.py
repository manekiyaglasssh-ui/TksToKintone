"""タブレット編集モードのレイヤー管理・表示先ディスプレイ選択・ツールバーの動的テスト。

QApplication を offscreen で起動し、手書きレイヤー（freehand_layer）の作成/選択/
反映先・太さ・色の保持/保存・読込/PDF出力、表示先ディスプレイ選択ダイアログ、
横スクロール可能なツールバーを検証する（タスク1〜7）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication, QDialog

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


class _FakeGeometry:
    def __init__(self, w: int, h: int) -> None:
        self._w = w
        self._h = h

    def width(self) -> int:
        return self._w

    def height(self) -> int:
        return self._h

    def topLeft(self):  # noqa: N802 - QScreen.availableGeometry 互換
        from PySide6.QtCore import QPoint

        return QPoint(0, 0)


class _FakeScreen:
    def __init__(self, name: str, w: int = 1920, h: int = 1080) -> None:
        self._name = name
        self._geo = _FakeGeometry(w, h)

    def name(self) -> str:
        return self._name

    def geometry(self):
        return self._geo

    def availableGeometry(self):  # noqa: N802
        return self._geo


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditTabletLayers(unittest.TestCase):
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

    def _layer_items(self, win):
        from app.voucher_edit_window import _EditFreehandLayerItem

        return [it for it in win.edit_items()
                if isinstance(it, _EditFreehandLayerItem)]

    def _freehand_items(self, win):
        return [it for it in win.edit_items()
                if it.serialize_edit_object().get("type") == "freehand"]

    # ── 1. ツールバーは1段で全ボタンを並べる（要件1）─────────────────────────────
    def test_tablet_toolbar_is_single_row(self) -> None:
        """1. タブレット上部ツールバーは1段で、全ボタンが1本に並ぶ。"""
        from PySide6.QtWidgets import QToolBar

        win = self._make_window()
        # 1本の QToolBar に全ボタンが並ぶ。2段目は存在しない。
        self.assertIsInstance(win._tablet_toolbar, QToolBar)
        self.assertFalse(hasattr(win, "_tablet_toolbar_row2"))
        self.assertIsNotNone(win._tablet_toolbar_container)
        labels = [a.text() for a in win._tablet_toolbar.actions()]
        for expected in ("手書き", "掴む", "消しゴム", "選択",
                         "削除", "全消去", "保存", "タブレット終了"):
            self.assertIn(expected, labels)

    # ── 2. freehand_layer 作成 ─────────────────────────────────────────────────
    def test_create_freehand_layer(self) -> None:
        """2. freehand_layer を作成できる。"""
        win = self._make_window()
        layer = win.add_freehand_layer(layer_name="レイヤーA")
        self.assertEqual(layer.serialize_edit_object().get("type"), "freehand_layer")
        self.assertEqual(layer.layer_name, "レイヤーA")
        self.assertEqual(len(self._layer_items(win)), 1)

    # ── 3. ストロークは現在レイヤーへ追加される ─────────────────────────────────
    def test_stroke_added_to_current_layer(self) -> None:
        """3. 手書きストロークが現在選択中レイヤーの strokes に追加される。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        layer = win.current_freehand_layer()
        self.assertIsNotNone(layer)
        before = layer.stroke_count()
        scene = win._scene
        scene.begin_freehand(QPointF(10, 10))
        scene._freehand_item.add_point(QPointF(30, 20))
        scene._freehand_item.add_point(QPointF(50, 40))
        scene.end_freehand()
        self.assertEqual(win.current_freehand_layer().stroke_count(), before + 1)
        win.exit_tablet_mode()

    # ── 4. 1ストロークごとの独立オブジェクトは増えない ─────────────────────────
    def test_no_independent_freehand_in_tablet(self) -> None:
        """4. 手書きしても1ストロークごとの独立オブジェクトが増えない。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        for pts in ([(10, 10), (20, 20)], [(30, 30), (40, 40)], [(50, 50), (60, 60)]):
            scene = win._scene
            scene.begin_freehand(QPointF(*pts[0]))
            scene._freehand_item.add_point(QPointF(*pts[1]))
            scene.end_freehand()
        # 独立した freehand オブジェクトは生成されない。
        self.assertEqual(len(self._freehand_items(win)), 0)
        # レイヤーは1つのまま、ストロークが3本溜まる。
        layers = self._layer_items(win)
        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0].stroke_count(), 3)
        win.exit_tablet_mode()

    # ── 5. レイヤー追加・選択 ──────────────────────────────────────────────────
    def test_add_and_select_layer(self) -> None:
        """5. レイヤーを追加・選択できる。"""
        win = self._make_window()
        l1 = win.add_freehand_layer()
        l2 = win.add_freehand_layer()
        self.assertEqual(len(self._layer_items(win)), 2)
        win.select_freehand_layer(l1.layer_id)
        self.assertEqual(win.current_freehand_layer().layer_id, l1.layer_id)
        win.select_freehand_layer(l2.layer_id)
        self.assertEqual(win.current_freehand_layer().layer_id, l2.layer_id)

    # ── 6. 選択中レイヤーのハイライト ──────────────────────────────────────────
    def test_selected_layer_highlighted(self) -> None:
        """6. 選択中レイヤーがハイライトされる。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        l1 = win.current_freehand_layer()
        l2 = win.add_freehand_layer()
        win.select_freehand_layer(l1.layer_id)
        btn1 = win._tablet_layer_buttons[l1.layer_id]
        btn2 = win._tablet_layer_buttons[l2.layer_id]
        self.assertTrue(btn1.isChecked())
        self.assertFalse(btn2.isChecked())
        win.exit_tablet_mode()

    # ── 7〜9. レイヤーごとの反映先・太さ・色 ────────────────────────────────────
    def test_layer_keeps_target_vouchers(self) -> None:
        """7. レイヤーごとに反映先を保持できる。"""
        win = self._make_window()
        l1 = win.add_freehand_layer(target_vouchers=["03"])
        l2 = win.add_freehand_layer(target_vouchers=["03", "04", "05"])
        self.assertEqual(l1.target_vouchers, ["03"])
        self.assertEqual(l2.target_vouchers, ["03", "04", "05"])

    def test_layer_keeps_pen_width(self) -> None:
        """8. レイヤーごとに太さを保持できる。"""
        win = self._make_window()
        l1 = win.add_freehand_layer(pen_width=1.5)
        l2 = win.add_freehand_layer(pen_width=6.0)
        self.assertEqual(l1.pen_width, 1.5)
        self.assertEqual(l2.pen_width, 6.0)

    def test_layer_keeps_color(self) -> None:
        """9. レイヤーごとに色を保持できる。"""
        win = self._make_window()
        l1 = win.add_freehand_layer(stroke_color="#000000")
        l2 = win.add_freehand_layer(stroke_color="#d32f2f")
        self.assertEqual(l1.stroke_color.lower(), "#000000")
        self.assertEqual(l2.stroke_color.lower(), "#d32f2f")

    # ── 10. レイヤー選択でツールバーへ反映 ─────────────────────────────────────
    def test_select_layer_reflects_to_toolbar(self) -> None:
        """10. レイヤー選択時に反映先・太さ・色がツールバーへ反映される。"""
        win = self._make_window()
        win.show()
        win.enter_tablet_mode()
        l1 = win.current_freehand_layer()
        l1.target_vouchers = ["03"]
        l1.pen_width = 1.5
        l1.stroke_color = "#000000"
        l2 = win.add_freehand_layer(target_vouchers=["03", "04", "05"],
                                    pen_width=6.0, stroke_color="#d32f2f")
        win.select_freehand_layer(l1.layer_id)
        self.assertEqual(win.current_pen_width, 1.5)
        self.assertEqual(win.current_pen_color.lower(), "#000000")
        self.assertEqual(win.current_target_vouchers, ["03"])
        win.select_freehand_layer(l2.layer_id)
        self.assertEqual(win.current_pen_width, 6.0)
        self.assertEqual(win.current_pen_color.lower(), "#d32f2f")
        self.assertEqual(win.current_target_vouchers, ["03", "04", "05"])
        win.exit_tablet_mode()

    # ── 11. 保存して再読み込み ─────────────────────────────────────────────────
    def test_freehand_layer_save_and_reload(self) -> None:
        """11. freehand_layer を保存して再読み込みできる。"""
        from app.voucher_edit_window import VoucherEditWindow

        win = self._make_window()
        layer = win.add_freehand_layer(layer_name="保存テスト",
                                       target_vouchers=["03"], pen_width=4.0,
                                       stroke_color="#1976d2")
        layer.add_stroke([(10, 10), (40, 30), (70, 60)])
        self.assertTrue(win._persist())
        win2 = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        reloaded = self._layer_items(win2)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].layer_name, "保存テスト")
        self.assertEqual(reloaded[0].stroke_count(), 1)
        self.assertEqual(reloaded[0].target_vouchers, ["03"])

    # ── 12. 既存 freehand 読込でエラーにならない ───────────────────────────────
    def test_existing_freehand_loads_without_error(self) -> None:
        """12. 既存 freehand を読み込んでもエラーにならない。"""
        from app.voucher_edit_objects import save_edit_objects
        from app.voucher_edit_window import VoucherEditWindow

        old_objects = [
            {"id": "f1", "type": "freehand",
             "points": [[10, 10], [40, 40], [80, 30]],
             "coordinate_origin": "scene_top_left",
             "pen_width": 3.0, "stroke_color": "#000000",
             "target_vouchers": ["03"]},
        ]
        save_edit_objects("5218869", old_objects)
        win = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win.deleteLater)
        types = [it.serialize_edit_object().get("type") for it in win.edit_items()]
        self.assertIn("freehand", types)

    # ── 13〜15. PDF出力 ────────────────────────────────────────────────────────
    def _fake_canvas(self, captured):
        class _FakePath:
            def moveTo(self, *a):
                pass

            def lineTo(self, *a):
                pass

        class _FakeCanvas:
            def saveState(self):
                pass

            def restoreState(self):
                pass

            def setStrokeColorRGB(self, *a):
                pass

            def setFillColorRGB(self, *a):
                pass

            def setLineWidth(self, *a):
                pass

            def setLineCap(self, *a):
                pass

            def setLineJoin(self, *a):
                pass

            def beginPath(self):
                return _FakePath()

            def drawPath(self, *a, **k):
                captured["paths"] += 1

        return _FakeCanvas()

    def test_freehand_layer_drawn_in_pdf(self) -> None:
        """13. PDF出力で freehand_layer の stroke が描画される。"""
        from app import voucher_service

        objects = [{
            "id": "L1", "type": "freehand_layer", "layer_id": "L1",
            "layer_name": "レイヤー1", "visible": True, "locked": False,
            "coordinate_origin": "scene_top_left",
            "pen_width": 3.0, "stroke_color": "#000000",
            "target_vouchers": ["03"],
            "strokes": [
                {"points": [[10, 10], [40, 40], [80, 30]], "pen_width": 3.0,
                 "stroke_color": "#000000"},
                {"points": [[5, 5], [9, 9]], "pen_width": 1.5,
                 "stroke_color": "#d32f2f"},
            ],
        }]
        captured = {"paths": 0}
        voucher_service._draw_edit_objects(self._fake_canvas(captured), objects)
        self.assertEqual(captured["paths"], 2)

    def test_invisible_layer_not_drawn(self) -> None:
        """14. visible=false のレイヤーはPDFに出ない。"""
        from app import voucher_service

        objects = [{
            "id": "L1", "type": "freehand_layer", "layer_id": "L1",
            "layer_name": "レイヤー1", "visible": False, "locked": False,
            "coordinate_origin": "scene_top_left",
            "pen_width": 3.0, "stroke_color": "#000000",
            "target_vouchers": ["03"],
            "strokes": [
                {"points": [[10, 10], [40, 40]], "pen_width": 3.0,
                 "stroke_color": "#000000"},
            ],
        }]
        captured = {"paths": 0}
        voucher_service._draw_edit_objects(self._fake_canvas(captured), objects)
        self.assertEqual(captured["paths"], 0)

    def test_layer_target_voucher_filter(self) -> None:
        """15. target_vouchers に応じて対象伝票だけに出る。"""
        from app import voucher_service

        objects = [{
            "id": "L1", "type": "freehand_layer", "layer_id": "L1",
            "layer_name": "レイヤー1", "visible": True,
            "coordinate_origin": "scene_top_left",
            "pen_width": 3.0, "stroke_color": "#000000",
            "target_vouchers": ["03"],
            "strokes": [{"points": [[10, 10], [40, 40]], "pen_width": 3.0,
                         "stroke_color": "#000000"}],
        }]
        # 反映先 03 の伝票には含まれ、07 の伝票には含まれない。
        self.assertEqual(len(voucher_service._filter_edit_objects(objects, "03")), 1)
        self.assertEqual(len(voucher_service._filter_edit_objects(objects, "07")), 0)

    # ── 16〜20. 表示先ディスプレイ選択 ─────────────────────────────────────────
    def test_display_dialog_shown_on_start(self) -> None:
        """16. タブレット編集開始時にディスプレイ選択ダイアログが出る。"""
        from app.voucher_edit_window import QGuiApplication

        win = self._make_window()
        screens = [_FakeScreen("内蔵"), _FakeScreen("SuperDisplay", 2560, 1600)]
        with mock.patch.object(QGuiApplication, "screens", return_value=screens), \
                mock.patch("app.voucher_edit_window.TabletScreenDialog") as Dlg:
            instance = Dlg.return_value
            instance.exec.return_value = QDialog.DialogCode.Rejected
            win.prompt_and_enter_tablet_mode()
            Dlg.assert_called_once()
            instance.exec.assert_called_once()
        # Rejected なので入らない。
        self.assertFalse(win.tablet_mode)

    def test_moves_to_selected_display(self) -> None:
        """17. 選択したディスプレイへ移動する。"""
        from app.voucher_edit_window import QGuiApplication

        win = self._make_window()
        chosen = _FakeScreen("SuperDisplay", 2560, 1600)
        screens = [_FakeScreen("内蔵"), chosen]
        with mock.patch.object(QGuiApplication, "screens", return_value=screens), \
                mock.patch("app.voucher_edit_window.TabletScreenDialog") as Dlg, \
                mock.patch.object(win, "_move_to_screen") as move:
            instance = Dlg.return_value
            instance.exec.return_value = QDialog.DialogCode.Accepted
            instance.selected_screen.return_value = chosen
            win.prompt_and_enter_tablet_mode()
        move.assert_called_once_with(chosen)
        self.assertTrue(win.tablet_mode)
        win.exit_tablet_mode()

    def test_cancel_does_not_enter_tablet(self) -> None:
        """18. キャンセル時はタブレット編集モードに入らない。"""
        from app.voucher_edit_window import QGuiApplication

        win = self._make_window()
        screens = [_FakeScreen("内蔵"), _FakeScreen("SuperDisplay", 2560, 1600)]
        with mock.patch.object(QGuiApplication, "screens", return_value=screens), \
                mock.patch("app.voucher_edit_window.TabletScreenDialog") as Dlg:
            instance = Dlg.return_value
            instance.exec.return_value = QDialog.DialogCode.Rejected
            win.prompt_and_enter_tablet_mode()
        self.assertFalse(win.tablet_mode)

    def test_dialog_preselects_saved_screen(self) -> None:
        """19. 前回選択した画面名が初期選択される。"""
        from app.voucher_edit_window import TabletScreenDialog

        screens = [_FakeScreen("内蔵"), _FakeScreen("SuperDisplay", 2560, 1600)]
        dialog = TabletScreenDialog(screens, saved_name="SuperDisplay")
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.selected_index(), 1)
        self.assertIs(dialog.selected_screen(), screens[1])

    def test_single_screen_no_error(self) -> None:
        """20. 画面が1つだけでもエラーにならない。"""
        from PySide6.QtWidgets import QMessageBox
        from app.voucher_edit_window import QGuiApplication

        win = self._make_window()
        screens = [_FakeScreen("内蔵")]
        with mock.patch.object(QGuiApplication, "screens", return_value=screens), \
                mock.patch.object(QMessageBox, "question",
                                  return_value=QMessageBox.StandardButton.Yes):
            win.prompt_and_enter_tablet_mode()
        self.assertTrue(win.tablet_mode)
        win.exit_tablet_mode()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
