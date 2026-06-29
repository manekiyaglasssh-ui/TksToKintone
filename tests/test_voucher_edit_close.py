"""指図書編集画面のクローズ処理のリグレッションテスト。

画像処理（背景透過(rembg)／二値化／背景透過(閾値)／背景を戻す）後に保存せず／保存して
閉じても、指図書編集画面だけが閉じてアプリ全体が固まらない・落ちないことを検証する。

方針:
  - closeEvent では GUIスレッドで thread.wait() しない（固まり防止）。
  - 実行中スレッドがある場合は closeEvent を一旦 ignore() し、非同期クローズ
    （_begin_async_close）でスレッド終了を待ってから改めて close() する。
  - 画像処理スレッドはウィンドウを親にせず QThread() で生成し、WA_DeleteOnClose で
    ウィンドウが破棄されても走行中スレッドが道連れに破棄されて落ちないようにする。
"""
from __future__ import annotations

import inspect
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
    from PySide6.QtCore import QObject, QRectF, Qt, QThread, Signal
    from PySide6.QtGui import QCloseEvent, QImage
    from PySide6.QtWidgets import QApplication, QMainWindow

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False


def _solid_png_bytes(width: int = 6, height: int = 4, color: int = 0xFFFFFFFF) -> bytes:
    from app.voucher_edit_window import qimage_to_png_bytes

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    return qimage_to_png_bytes(image)


def _fake_remove_transparent(input_bytes: bytes) -> bytes:
    from app.voucher_edit_window import qimage_to_png_bytes

    src = QImage()
    src.loadFromData(bytes(input_bytes))
    out = QImage(src.width() or 1, src.height() or 1, QImage.Format.Format_ARGB32)
    out.fill(0x00000000)
    return qimage_to_png_bytes(out)


@contextmanager
def _patch_rembg(remove_func):
    fake = types.ModuleType("rembg")
    fake.remove = remove_func
    with mock.patch.dict(sys.modules, {"rembg": fake}), \
            mock.patch("importlib.metadata.version", return_value="0.0"):
        yield fake


if PYSIDE_AVAILABLE:
    class _SleepWorker(QObject):
        """別スレッドで一定時間ブロックする worker の代役（quit では止まらない重い処理を模す）。

        started_running は run() が実際に sleep へ入った合図。これを待ってから閉じることで、
        quit() がスロット実行前に event loop を畳んでしまう非決定性を排除する。
        """

        started_running = Signal()
        finished = Signal()

        def __init__(self, seconds: float = 0.3) -> None:
            super().__init__()
            self._seconds = seconds

        def run(self) -> None:
            self.started_running.emit()
            time.sleep(self._seconds)
            self.finished.emit()


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherEditClose(unittest.TestCase):
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

    # ── ヘルパ ────────────────────────────────────────────────────────────────
    def _make_window(self, parent=None, delete_on_close: bool = False,
                     suppress_warmup: bool = True):
        from app.voucher_edit_window import VoucherEditWindow

        win = VoucherEditWindow(order_no="CLOSE", background_pdf_bytes=b"", parent=parent)
        if suppress_warmup:
            # 画像選択や加工のたびに走る実 warmup スレッド（import rembg）を抑止して
            # クローズ判定を決定的にする。warmup 自体のテストは個別に行う。
            win._start_rembg_warmup_if_needed = lambda: None  # type: ignore[method-assign]
        if delete_on_close:
            win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        else:
            self.addCleanup(win.deleteLater)
        win.set_debug_visible(True)
        return win

    def _add_selected_image(self, win):
        with mock.patch.object(win, "_start_rembg_warmup_if_needed"):
            item = win.add_image(_solid_png_bytes(),
                                 rect=QRectF(100.0, 100.0, 40.0, 30.0),
                                 select=True)
        self.assertIsNotNone(item)
        return item

    def _wait_bg(self, win, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while win._background_removal_running and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()

    def _run_rembg(self, win) -> None:
        remove = mock.Mock(side_effect=_fake_remove_transparent)
        with _patch_rembg(remove):
            win._on_transparent_background()
            self._wait_bg(win)

    def _close(self, win) -> bool:
        """closeEvent を直接駆動し、accept されたか（閉じたか）を返す。"""
        event = QCloseEvent()
        win.closeEvent(event)
        self.app.processEvents()
        return event.isAccepted()

    def _start_tracked_sleep_thread(self, win, seconds: float = 0.3):
        """ウィンドウの画像スレッド管理へ、走行中の擬似 worker スレッドを登録する。

        戻り値は (thread, state)。state["running"] は worker が sleep へ入った合図、
        state["finished"] は完了の合図。ウィンドウ破棄後も安全に参照できるよう、進捗は
        QThread のメソッドでなく Python 側の dict で追跡する。
        """
        thread = QThread()
        worker = _SleepWorker(seconds)
        worker.moveToThread(thread)
        state = {"running": False, "finished": False}
        worker.started_running.connect(lambda: state.update(running=True))
        worker.finished.connect(lambda: state.update(finished=True))
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        win._register_blocking_image_thread(thread)
        thread.start()
        # worker が実際に sleep へ入るまで待つ（quit による途中終了の非決定性を排除）。
        self._pump_until(lambda: state["running"], timeout=2.0)
        return thread, state

    def _start_tracked_warmup_thread(self, win, seconds: float = 0.3):
        """ウィンドウの warmup（non-blocking）スレッド管理へ擬似 worker を登録する。

        戻り値は (thread, state)。blocking ではないため、閉じる時の待機対象にならない。
        """
        thread = QThread()
        worker = _SleepWorker(seconds)
        worker.moveToThread(thread)
        state = {"running": False, "finished": False}
        worker.started_running.connect(lambda: state.update(running=True))
        worker.finished.connect(lambda: state.update(finished=True))
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        win._register_warmup_thread(thread)
        thread.start()
        self._pump_until(lambda: state["running"], timeout=2.0)
        return thread, state

    def _pump_until(self, predicate, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while not predicate() and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()

    # ── 固まり防止（要件3・6）──────────────────────────────────────────────────
    def test_close_event_does_not_call_thread_wait(self) -> None:
        # closeEvent / 非同期クローズ関連で GUIスレッドの wait() を呼ばない（要件6）。
        from app.voucher_edit_window import VoucherEditWindow

        for name in ("closeEvent", "_begin_async_close", "_request_image_threads_stop",
                     "_finish_async_close", "_on_image_thread_finished"):
            src = inspect.getsource(getattr(VoucherEditWindow, name))
            self.assertNotIn(".wait(", src, f"{name} は wait() を呼んではいけない")

    def test_close_ignored_then_closes_after_thread_finishes(self) -> None:
        win = self._make_window()
        thread, state = self._start_tracked_sleep_thread(win, 0.2)

        # 実行中スレッドがあるので closeEvent は一旦 ignore される（固まらない）。
        accepted = self._close(win)
        self.assertFalse(accepted)
        self.assertTrue(win._close_in_progress)

        # スレッド終了後に改めて close される。
        self._pump_until(lambda: state["finished"] and not win._close_in_progress)
        self.assertFalse(win._close_in_progress)
        self.assertFalse(win.isVisible())

    # ── 保存せず閉じる → 親画面は残る（要件3・13）─────────────────────────────
    def _assert_discard_close_keeps_parent(self, process):
        parent = QMainWindow()
        self.addCleanup(parent.deleteLater)
        win = self._make_window(parent=parent)
        self._add_selected_image(win)
        process(win)
        self.assertTrue(win.is_dirty())

        with mock.patch.object(win, "_prompt_unsaved_changes", return_value="discard"), \
                mock.patch("app.voucher_edit_window.QApplication.quit") as quit_mock:
            self._close(win)
            self._pump_until(lambda: not win._close_in_progress)

        quit_mock.assert_not_called()                 # アプリ全体は終了しない（要件4・12）
        self.assertIsNotNone(parent.metaObject())     # 親画面は破棄されない（要件3）

    def test_discard_close_after_binarize_keeps_parent(self) -> None:
        self._assert_discard_close_keeps_parent(lambda w: w._on_binarize())

    def test_discard_close_after_threshold_transparent_keeps_parent(self) -> None:
        self._assert_discard_close_keeps_parent(lambda w: w._on_threshold_transparent())

    def test_discard_close_after_rembg_keeps_parent(self) -> None:
        self._assert_discard_close_keeps_parent(self._run_rembg)

    def test_discard_close_after_restore_keeps_parent(self) -> None:
        def _process(w):
            w._on_binarize()
            w._on_restore_image()
        self._assert_discard_close_keeps_parent(_process)

    # ── キャンセル（要件3・13）──────────────────────────────────────────────
    def test_cancel_keeps_window_and_processed_image(self) -> None:
        win = self._make_window()
        item = self._add_selected_image(win)
        win._on_binarize()
        processed = bytes(item.image_bytes)

        with mock.patch.object(win, "_prompt_unsaved_changes", return_value="cancel"):
            accepted = self._close(win)

        self.assertFalse(accepted)
        self.assertFalse(win._closing)
        self.assertFalse(win._close_in_progress)
        self.assertEqual(item.image_bytes, processed)

    # ── 保存して閉じる（要件5）──────────────────────────────────────────────
    def test_save_and_close_persists_and_keeps_parent(self) -> None:
        from app.voucher_edit_objects import load_edit_objects

        parent = QMainWindow()
        self.addCleanup(parent.deleteLater)
        win = self._make_window(parent=parent)
        self._add_selected_image(win)
        win._on_binarize()

        with mock.patch.object(win, "_prompt_unsaved_changes", return_value="save"), \
                mock.patch("app.voucher_edit_window.QApplication.quit") as quit_mock:
            accepted = self._close(win)

        # 実行中スレッドが無いので同期的に閉じる。
        self.assertTrue(accepted)
        quit_mock.assert_not_called()
        saved = load_edit_objects("CLOSE")
        self.assertTrue(any(o["type"] == "image" for o in saved))
        self.assertIsNotNone(parent.metaObject())

    # ── warmup スレッド（要件7）──────────────────────────────────────────────
    def test_warmup_thread_has_no_window_parent(self) -> None:
        win = self._make_window(suppress_warmup=False)
        # RembgWarmupWorker を、本物の Qt シグナルを持つ軽量 worker へ差し替える。
        with mock.patch("app.voucher_edit_window.RembgWarmupWorker",
                        lambda: _SleepWorker(0.05)):
            win._start_rembg_warmup_if_needed()
        thread = win._rembg_warmup_thread
        self.assertIsNotNone(thread)
        # ウィンドウを親に持たない（WA_DeleteOnClose 道連れ破棄の回避: 要件6）。
        self.assertIsNone(thread.parent())
        # warmup 集合に登録され、blocking 集合には含まれない（要件4・7）。
        self.assertIn(thread, win._warmup_threads)
        self.assertNotIn(thread, win._blocking_image_threads)
        # warmup 実行中でも blocking 判定は False（閉じる時に待たない: 要件4・5）。
        self.assertFalse(win._has_running_blocking_image_threads())
        # 完了まで進めてスレッドを片付ける（リーク防止）。
        self._pump_until(lambda: not win._rembg_warmup_running, timeout=2.0)
        self.app.processEvents()

    def test_warmup_finished_does_not_update_ui_while_closing(self) -> None:
        win = self._make_window()
        win._close_in_progress = True
        win._closing = True
        win._rembg_warmed_up = False
        win._on_rembg_warmup_finished()
        # クローズ処理中は warmup 完了を反映しない（要件7・11）。
        self.assertFalse(win._rembg_warmed_up)

    def test_warmup_not_in_blocking_set(self) -> None:
        # warmup スレッドは blocking image thread に含まれない（要件4）。
        win = self._make_window()
        thread, state = self._start_tracked_warmup_thread(win, 0.1)
        self.assertIn(thread, win._warmup_threads)
        self.assertNotIn(thread, win._blocking_image_threads)
        # warmup 実行中でも blocking 判定は False（要件4・5）。
        self.assertFalse(win._has_running_blocking_image_threads())
        self._pump_until(lambda: state["finished"], timeout=2.0)
        self.app.processEvents()

    # ── 画像貼り付け後の保存して閉じる（要件1・9・10）─────────────────────────
    def test_save_and_close_immediate_with_only_warmup_running(self) -> None:
        # 画像貼り付けだけの状態（warmup だけ走行中）で save_and_close が即閉じる。
        parent = QMainWindow()
        self.addCleanup(parent.deleteLater)
        win = self._make_window(parent=parent)
        self._add_selected_image(win)
        thread, state = self._start_tracked_warmup_thread(win, 0.5)

        # warmup がまだ走っている状態で保存して閉じる。
        self.assertTrue(thread.isRunning())
        win.save_and_close()
        self.app.processEvents()

        # 非同期クローズに入らず即閉じる（warmup は待たない: 要件4・9）。
        self.assertFalse(win._close_in_progress)
        self.assertTrue(win._closing)
        self.assertFalse(win.isVisible())
        # 「画像処理の終了を待っています...」は表示しない（要件10）。
        self.assertIsNone(win._closing_overlay)
        # 親画面は残る。
        self.assertIsNotNone(parent.metaObject())
        self._pump_until(lambda: state["finished"], timeout=2.0)
        self.app.processEvents()

    def test_close_immediate_with_only_warmup_running(self) -> None:
        # closeEvent 経路でも、warmup だけなら待たずに即閉じる（要件4・8）。
        win = self._make_window()
        thread, state = self._start_tracked_warmup_thread(win, 0.5)
        self.assertTrue(thread.isRunning())

        accepted = self._close(win)
        self.assertTrue(accepted)
        self.assertFalse(win._close_in_progress)
        self.assertIsNone(win._closing_overlay)
        self._pump_until(lambda: state["finished"], timeout=2.0)
        self.app.processEvents()

    # ── 実画像処理 worker（要件10・13）────────────────────────────────────────
    def test_blocking_thread_triggers_async_close_and_overlay(self) -> None:
        # 実際に加工 worker が動いている時だけ非同期クローズ＆待機表示を出す（要件10）。
        win = self._make_window()
        thread, state = self._start_tracked_sleep_thread(win, 0.3)

        accepted = self._close(win)
        self.assertFalse(accepted)
        self.assertTrue(win._close_in_progress)
        # この時だけ「画像処理の終了を待っています...」を表示する（要件10）。
        self.assertIsNotNone(win._closing_overlay)

        self._pump_until(lambda: state["finished"] and not win._close_in_progress)
        self.assertFalse(win._close_in_progress)

    # ── wait禁止（要件12）────────────────────────────────────────────────────
    def test_close_paths_never_wait_on_warmup_thread(self) -> None:
        # closeEvent / save_and_close 経路で warmup スレッドへ wait() を呼ばない（要件12）。
        win = self._make_window()
        thread, state = self._start_tracked_warmup_thread(win, 0.3)
        with mock.patch.object(thread, "wait",
                               side_effect=AssertionError("warmup へ wait() してはいけない")):
            self._add_selected_image(win)
            win.save_and_close()
            self.app.processEvents()
        self._pump_until(lambda: state["finished"], timeout=2.0)
        self.app.processEvents()

    # ── WA_DeleteOnClose（要件15）────────────────────────────────────────────
    def test_delete_on_close_not_destroyed_while_thread_running(self) -> None:
        win = self._make_window(delete_on_close=True)
        thread, state = self._start_tracked_sleep_thread(win, 0.3)

        # 実行中は即破棄せず ignore（要件15・前者）。
        accepted = self._close(win)
        self.assertFalse(accepted)
        self.assertFalse(state["finished"])
        # ウィンドウはまだ生きている（C++破棄なら metaObject() で RuntimeError）。
        self.assertIsNotNone(win.metaObject())

        # スレッド終了後に安全に閉じる（落ちない＝この後 core dump しない）。
        # ウィンドウは WA_DeleteOnClose で破棄されるため、進捗は state 側で確認する。
        self._pump_until(lambda: state["finished"], timeout=5.0)
        self.app.processEvents()
        self.assertTrue(state["finished"])

    # ── コールバックガード（要件11）──────────────────────────────────────────
    def test_finished_callback_does_not_apply_while_closing(self) -> None:
        win = self._make_window()
        item = self._add_selected_image(win)
        win._background_removal_target = item
        win._close_in_progress = True

        with mock.patch.object(item, "apply_processed_image") as apply_mock:
            win._on_background_removal_finished(_solid_png_bytes(6, 4, 0x00000000))
        apply_mock.assert_not_called()

    def test_finished_callback_ignores_removed_item(self) -> None:
        win = self._make_window()
        item = self._add_selected_image(win)
        before = bytes(item.image_bytes)
        win._scene.removeItem(item)          # scene から外す（削除済みを模す）
        win._background_removal_target = item

        win._on_background_removal_finished(_solid_png_bytes(6, 4, 0x00000000))
        self.assertEqual(item.image_bytes, before)

    # ── 実行中スレッド判定（要件9）──────────────────────────────────────────
    def test_has_running_blocking_image_threads_tracks_worker(self) -> None:
        win = self._make_window()
        self.assertFalse(win._has_running_blocking_image_threads())
        thread, state = self._start_tracked_sleep_thread(win, 0.2)
        self.assertTrue(win._has_running_blocking_image_threads())
        # worker 完了→thread.quit→thread.finished→集合から除去 までを待つ。
        self._pump_until(lambda: not win._has_running_blocking_image_threads())
        self.assertFalse(win._has_running_blocking_image_threads())


if __name__ == "__main__":
    unittest.main()
