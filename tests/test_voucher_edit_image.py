"""指図書編集画面の画像オブジェクト（挿入・貼り付け・保存・PDF反映）のテスト。"""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False


def _png_bytes(width: int = 12, height: int = 8, color: int = 0xFF3366CC) -> bytes:
    from app.voucher_edit_window import qimage_to_png_bytes

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    return qimage_to_png_bytes(image)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditImageWindow(unittest.TestCase):
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

    def test_insert_image_from_file_adds_image_object(self) -> None:
        win = self._make_window()
        png = _png_bytes()
        path = os.path.join(self._tmp.name, "pic.png")
        with open(path, "wb") as fp:
            fp.write(png)
        item = win.insert_image_from_file(path)
        self.assertIsNotNone(item)
        objects = win.serialize_objects()
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["type"], "image")

    def test_paste_image_from_clipboard_adds_image_object(self) -> None:
        win = self._make_window()
        image = QImage(10, 10, QImage.Format.Format_ARGB32)
        image.fill(0xFF00AA55)
        QApplication.clipboard().setImage(image)
        item = win.paste_image_from_clipboard()
        self.assertIsNotNone(item)
        objects = win.serialize_objects()
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["type"], "image")

    def test_ctrl_v_keypress_pastes_image(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        win = self._make_window()
        image = QImage(10, 10, QImage.Format.Format_ARGB32)
        image.fill(0xFF112233)
        QApplication.clipboard().setImage(image)
        win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_V,
                                    Qt.KeyboardModifier.ControlModifier))
        objects = win.serialize_objects()
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["type"], "image")

    def test_ctrl_v_paste_selects_image(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        win = self._make_window()
        image = QImage(10, 10, QImage.Format.Format_ARGB32)
        image.fill(0xFF445566)
        QApplication.clipboard().setImage(image)
        win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_V,
                                    Qt.KeyboardModifier.ControlModifier))
        selected = [it for it in win._scene.selectedItems()
                    if hasattr(it, "serialize_edit_object")]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].serialize_edit_object()["type"], "image")

    def test_ctrl_v_paste_is_undoable(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        win = self._make_window()
        image = QImage(10, 10, QImage.Format.Format_ARGB32)
        image.fill(0xFF778899)
        QApplication.clipboard().setImage(image)
        win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_V,
                                    Qt.KeyboardModifier.ControlModifier))
        self.assertEqual(len(win.serialize_objects()), 1)
        win.undo()
        self.assertEqual(len(win.serialize_objects()), 0)

    def test_text_editing_ctrl_v_does_not_paste_image(self) -> None:
        from PySide6.QtCore import Qt, QRectF
        from PySide6.QtGui import QKeyEvent

        win = self._make_window()
        # 画像をクリップボードに置いた状態でテキスト編集中に Ctrl+V を送る。
        image = QImage(10, 10, QImage.Format.Format_ARGB32)
        image.fill(0xFFAABBCC)
        QApplication.clipboard().setImage(image)
        text_item = win.add_text_rect(QRectF(50.0, 50.0, 120.0, 30.0), text="")
        text_item.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        text_item.setFocus(Qt.FocusReason.OtherFocusReason)
        win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_V,
                                    Qt.KeyboardModifier.ControlModifier))
        # テキスト編集中は画像オブジェクトが追加されない（テキスト貼り付け優先）。
        images = [o for o in win.serialize_objects() if o["type"] == "image"]
        self.assertEqual(images, [])

    def test_image_resize_persists_to_json(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(40.0, 40.0, 100.0, 60.0))
        item.set_box_size(180.0, 130.0)
        win.commit_history()
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        win2 = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        obj = win2.serialize_objects()[0]
        self.assertAlmostEqual(obj["width"], 180.0, delta=1.0)
        self.assertAlmostEqual(obj["height"], 130.0, delta=1.0)

    def test_image_resize_reflected_in_pdf(self) -> None:
        from app import voucher_service

        obj = {
            "id": "img-resize",
            "type": "image",
            "x": 50.0,
            "y": 60.0,
            "width": 200.0,
            "height": 150.0,
            "image_data": base64.b64encode(_png_bytes()).decode("ascii"),
            "image_format": "png",
        }
        canvas = mock.MagicMock()
        voucher_service._draw_edit_image(canvas, obj, "img-resize")
        # drawImage の幅・高さ引数にリサイズ後サイズが渡る。
        _, kwargs = canvas.drawImage.call_args
        self.assertAlmostEqual(kwargs["width"], 200.0, delta=0.5)
        self.assertAlmostEqual(kwargs["height"], 150.0, delta=0.5)

    def test_paste_without_image_does_nothing(self) -> None:
        win = self._make_window()
        QApplication.clipboard().clear()
        QApplication.clipboard().setText("ただのテキスト")
        result = win.paste_image_from_clipboard()
        self.assertIsNone(result)
        self.assertEqual(len(win.serialize_objects()), 0)

    def test_image_serialized_with_base64(self) -> None:
        win = self._make_window()
        win.add_image(_png_bytes(), rect=QRectF(50.0, 60.0, 120.0, 80.0))
        obj = win.serialize_objects()[0]
        self.assertEqual(obj["type"], "image")
        self.assertTrue(obj["image_data"])
        # base64 として復号でき、PNG シグネチャを持つ。
        raw = base64.b64decode(obj["image_data"])
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(obj["width"], 120.0)
        self.assertEqual(obj["height"], 80.0)

    def test_image_save_then_reload(self) -> None:
        from app.voucher_edit_window import VoucherEditWindow

        win = self._make_window()
        win.add_image(_png_bytes(), rect=QRectF(40.0, 40.0, 100.0, 60.0))
        with mock.patch("app.voucher_edit_window.QMessageBox.information"):
            win.save()
        win2 = VoucherEditWindow(order_no="5218869", background_pdf_bytes=b"")
        self.addCleanup(win2.deleteLater)
        reloaded = win2.serialize_objects()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["type"], "image")

    def test_image_can_be_moved(self) -> None:
        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(10.0, 10.0, 50.0, 40.0))
        item.setPos(200.0, 150.0)
        obj = win.serialize_objects()[0]
        self.assertAlmostEqual(obj["x"], 200.0, delta=1.0)
        self.assertAlmostEqual(obj["y"], 150.0, delta=1.0)

    def test_image_can_be_resized(self) -> None:
        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(10.0, 10.0, 50.0, 40.0))
        item.set_box_size(220.0, 160.0)
        obj = win.serialize_objects()[0]
        self.assertAlmostEqual(obj["width"], 220.0, delta=1.0)
        self.assertAlmostEqual(obj["height"], 160.0, delta=1.0)

    def test_image_resized_via_resize_handle(self) -> None:
        from app.voucher_edit_window import _ResizeHandle

        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(10.0, 10.0, 50.0, 40.0))
        # 右下リサイズハンドルをドラッグして大きくする。
        _ResizeHandle(item)._resize_target(QPointF(210.0, 170.0))
        obj = win.serialize_objects()[0]
        self.assertGreater(obj["width"], 50.0)
        self.assertGreater(obj["height"], 40.0)

    def test_background_item_is_not_resizable(self) -> None:
        from PySide6.QtWidgets import QGraphicsItem

        win = self._make_window()
        for bg in win.background_items():
            # 背景は選択不可・移動不可（リサイズ対象外）。
            self.assertFalse(bool(bg.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable))
            self.assertFalse(bool(bg.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable))
            self.assertTrue(getattr(bg, "_BG_MARK", False))
            # 背景はリサイズハンドル付与対象（serialize_edit_object）を持たない。
            self.assertFalse(hasattr(bg, "serialize_edit_object"))

    def test_image_deleted_with_delete_selected(self) -> None:
        win = self._make_window()
        item = win.add_image(_png_bytes(), rect=QRectF(10.0, 10.0, 50.0, 40.0))
        win._scene.clearSelection()
        item.setSelected(True)
        win.delete_selected()
        self.assertEqual(len(win.serialize_objects()), 0)

    def test_image_undo_redo(self) -> None:
        win = self._make_window()
        win.add_image(_png_bytes(), rect=QRectF(10.0, 10.0, 50.0, 40.0))
        win.commit_history()
        self.assertEqual(len(win.serialize_objects()), 1)
        win.undo()
        self.assertEqual(len(win.serialize_objects()), 0)
        win.redo()
        self.assertEqual(len(win.serialize_objects()), 1)
        self.assertEqual(win.serialize_objects()[0]["type"], "image")


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditImageObjects(unittest.TestCase):
    """voucher_edit_objects の image タイプ永続化テスト。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_image_type_in_object_types(self) -> None:
        from app.voucher_edit_objects import OBJECT_TYPES

        self.assertIn("image", OBJECT_TYPES)

    def test_save_and_load_image_object(self) -> None:
        from app.voucher_edit_objects import load_edit_objects, save_edit_objects

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            base = Path(tmp)
            obj = {
                "id": "img-1",
                "type": "image",
                "x": 100.0,
                "y": 200.0,
                "width": 120.0,
                "height": 80.0,
                "image_data": base64.b64encode(_png_bytes()).decode("ascii"),
                "image_format": "png",
            }
            save_edit_objects("5218869", [obj], base_dir=base)
            loaded = load_edit_objects("5218869", base_dir=base)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["type"], "image")
            self.assertEqual(loaded[0]["width"], 120.0)
            self.assertTrue(loaded[0]["image_data"])


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditImagePdf(unittest.TestCase):
    """画像オブジェクトのPDF反映テスト（03/04/05のみ）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _image_obj(self) -> dict:
        return {
            "id": "img-1",
            "type": "image",
            "x": 100.0,
            "y": 120.0,
            "width": 80.0,
            "height": 60.0,
            "image_data": base64.b64encode(_png_bytes()).decode("ascii"),
            "image_format": "png",
        }

    def test_draw_edit_image_calls_draw_image(self) -> None:
        from app import voucher_service

        canvas = mock.MagicMock()
        voucher_service._draw_edit_objects(canvas, [self._image_obj()])
        self.assertTrue(canvas.drawImage.called)

    def test_image_drawn_only_on_03_04_05(self) -> None:
        from app.voucher_service import build_vouchers_pdf_bytes
        from app.voucher_templates import DUMMY_DATA

        data = {**DUMMY_DATA, "edit_objects": [self._image_obj()]}
        drawn_on = []
        not_drawn_on = []
        for vid in ("01", "02", "03", "04", "05", "06", "07", "08"):
            with mock.patch("app.voucher_service._draw_edit_image") as draw:
                build_vouchers_pdf_bytes([vid], data)
            if draw.called:
                drawn_on.append(vid)
            else:
                not_drawn_on.append(vid)
        self.assertEqual(drawn_on, ["03", "04", "05"])
        self.assertEqual(not_drawn_on, ["01", "02", "06", "07", "08"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
