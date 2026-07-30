from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from app.gui import (
    BACKGROUND_LOAD_BATCH_SIZE,
    INITIAL_INTERACTIVE_ROW_COUNT,
    RegistrationPrepareWorkerThread,
    RegistrationPreviewDialog,
)
from app.kakou_master import KAKOU_MASTER_HEADERS, load_master_cached
from app.kintone_client import load_field_mapping_cached


def _row(index: int, customer_name: str = "") -> dict[str, str]:
    return {
        "受注No": f"{1000 + index:07d}",
        "硝/加工": "2",
        "商品名称": "強化 長2 磨き",
        "掛率集計コード": "0300",
        "掛率集計名称": "エッチング",
        "W寸法": "1303",
        "H寸法": "1061",
        "仕上日": "2026-07-23",
        "出荷区分": "AM",
        "得意先名称": customer_name,
    }


class _FiveSecondWorker(QThread):
    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self.release = release

    def run(self) -> None:
        self.release.wait(5.0)


class KintoneRegistrationProgressiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self) -> RegistrationPreviewDialog:
        return RegistrationPreviewDialog(
            rows=[],
            master=[],
            shukka_options=["AM", "PM"],
            customer_labels={},
            progressive_loading=True,
            request_started=time.perf_counter(),
        )

    def test_constants_are_ten(self) -> None:
        self.assertEqual(INITIAL_INTERACTIVE_ROW_COUNT, 10)
        self.assertEqual(BACKGROUND_LOAD_BATCH_SIZE, 10)

    def test_five_second_worker_does_not_delay_window_show(self) -> None:
        release = threading.Event()
        worker = _FiveSecondWorker(release)
        dialog = self._dialog()
        try:
            started = time.perf_counter()
            dialog.show()
            worker.start()
            self.app.processEvents()
            self.assertTrue(dialog.isVisible())
            self.assertLess(time.perf_counter() - started, 1.0)
            self.assertFalse(dialog.register_button.isEnabled())
        finally:
            release.set()
            worker.wait(1000)
            dialog.close()
            dialog.deleteLater()

    def test_first_ten_are_interactive_but_register_waits_for_all(self) -> None:
        dialog = self._dialog()
        try:
            dialog.show()
            dialog.append_prepared_batch(
                [_row(i) for i in range(10)], [{} for _ in range(10)], total_count=25
            )
            self.assertEqual(dialog.table.rowCount(), 10)
            self.assertTrue(dialog.table.isEnabled())
            self.assertTrue(dialog._filter_edit.isEnabled())
            self.assertFalse(dialog.register_button.isEnabled())
            self.assertIn("10 / 25", dialog.loading_status_label.text())

            dialog.append_prepared_batch(
                [_row(i) for i in range(10, 20)], [{} for _ in range(10)], total_count=25
            )
            dialog.append_prepared_batch(
                [_row(i) for i in range(20, 25)], [{} for _ in range(5)], total_count=25
            )
            dialog.finish_progressive_loading({"worker_ms": 12})
            self.assertEqual(dialog.table.rowCount(), 25)
            self.assertTrue(dialog.register_button.isEnabled())
            self.assertTrue(dialog.csv_create_button.isEnabled())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_less_than_ten_becomes_interactive_when_all_rows_arrive(self) -> None:
        dialog = self._dialog()
        try:
            with self.assertLogs("tks_to_kintone_app", level="INFO") as logs:
                dialog.append_prepared_batch(
                    [_row(i) for i in range(4)], [{} for _ in range(4)], total_count=4
                )
            self.assertIn("phase=interactive", "\n".join(logs.output))
            self.assertFalse(dialog.register_button.isEnabled())
            dialog.finish_progressive_loading()
            self.assertTrue(dialog.register_button.isEnabled())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_filter_is_applied_to_rows_added_later(self) -> None:
        dialog = self._dialog()
        try:
            dialog._filter_edit.setText("0001001")
            dialog.append_prepared_batch(
                [_row(0), _row(1)], [{}, {}], total_count=3
            )
            dialog.append_prepared_batch([_row(2)], [{}], total_count=3)
            self.assertTrue(dialog.table.isRowHidden(0))
            self.assertFalse(dialog.table.isRowHidden(1))
            self.assertTrue(dialog.table.isRowHidden(2))
            self.assertIn("残りのデータ", dialog.loading_status_label.text())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_customer_auto_selection_is_deterministic_for_match_and_no_match(self) -> None:
        dialog = RegistrationPreviewDialog(
            rows=[],
            master=[],
            shukka_options=[],
            customer_labels={"得意先1": "得意先1"},
            customer_match_patterns={"得意先1": "エレベータ"},
            progressive_loading=True,
            generation=40,
        )
        try:
            with self.assertLogs("tks_to_kintone_app", level="INFO") as logs:
                dialog.append_prepared_batch(
                    [_row(0, "東芝エレベータ株式会社"), _row(1, "一般硝子株式会社")],
                    [{}, {}],
                    total_count=2,
                )
            self.assertEqual(dialog._state.customer_key_by_row, ["得意先1", "selected"])
            self.assertEqual(dialog._customer_widgets[0].currentData(), "得意先1")
            self.assertEqual(dialog._customer_widgets[1].currentData(), "selected")
            output = "\n".join(logs.output)
            self.assertIn("phase=selection_applied", output)
            self.assertIn("reason=elevator_match", output)
            self.assertIn("phase=selection_cleared", output)
            self.assertIn("reason=no_elevator", output)
            self.assertIn("generation=40", output)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_background_existing_result_does_not_overwrite_auto_selection(self) -> None:
        dialog = RegistrationPreviewDialog(
            rows=[],
            master=[],
            shukka_options=[],
            customer_labels={"得意先1": "得意先1", "得意先2": "得意先2"},
            customer_match_patterns={"得意先1": "エレベータ"},
            progressive_loading=True,
        )
        try:
            source = _row(0, "東芝エレベータ株式会社")
            dialog.append_prepared_batch([source], [{}], total_count=1)
            merged = dict(source)
            merged["得意先選択"] = "得意先2"
            dialog.apply_existing_check_result([merged], [{"得意先選択": "得意先2"}])
            self.assertEqual(dialog._state.customer_key_by_row, ["得意先1"])
            self.assertEqual(dialog._customer_widgets[0].currentData(), "得意先1")
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_programmatic_existing_reflection_does_not_look_like_user_change(self) -> None:
        dialog = RegistrationPreviewDialog(
            rows=[],
            master=[],
            shukka_options=[],
            customer_labels={"得意先1": "得意先1", "得意先2": "得意先2"},
            customer_match_patterns={"得意先1": "エレベータ"},
            progressive_loading=True,
        )
        try:
            source = _row(0, "一般硝子株式会社")
            dialog.append_prepared_batch([source], [{}], total_count=1)
            merged = dict(source)
            merged["得意先選択"] = "得意先2"
            dialog.apply_existing_check_result([merged], [{"得意先選択": "得意先2"}])
            self.assertEqual(
                dialog._customer_selection_source_by_order[source["受注No"]],
                "auto_cleared",
            )
            self.assertEqual(dialog._state.customer_key_by_row, ["selected"])
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_prepare_worker_emits_batches_not_individual_rows(self) -> None:
        worker = RegistrationPrepareWorkerThread(
            7, [_row(i) for i in range(23)], [], {}
        )
        batches: list[tuple[int, list[dict[str, str]]]] = []
        completed: list[int] = []
        worker.batch_ready.connect(
            lambda generation, rows, _existing: batches.append((generation, rows))
        )
        worker.succeeded.connect(lambda generation, _metrics: completed.append(generation))
        worker.start()
        deadline = time.monotonic() + 3.0
        while not completed and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.001)
        worker.wait(1000)
        self.app.processEvents()
        self.assertEqual([len(rows) for _, rows in batches], [10, 10, 3])
        self.assertTrue(all(generation == 7 for generation, _ in batches))
        self.assertEqual(
            [row["受注No"] for _, rows in batches for row in rows],
            [_row(i)["受注No"] for i in range(23)],
        )

    def test_error_keeps_displayed_rows_and_registration_disabled(self) -> None:
        dialog = self._dialog()
        try:
            dialog.append_prepared_batch(
                [_row(i) for i in range(10)], [{} for _ in range(10)], total_count=30
            )
            dialog.fail_progressive_loading("認証エラー")
            self.assertEqual(dialog.table.rowCount(), 10)
            self.assertTrue(dialog.table.isEnabled())
            self.assertFalse(dialog.register_button.isEnabled())
            self.assertIn("10件を表示しました", dialog.loading_status_label.text())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_main_window_close_uses_cooperative_cancel_without_wait(self) -> None:
        source = Path("app/gui.py").read_text(encoding="utf-8")
        main_start = source.index("class MainWindow")
        close_start = source.index("    def closeEvent", main_start)
        show_start = source.index("    def showEvent", close_start)
        close_source = source[close_start:show_start]
        self.assertIn("requestInterruption()", close_source)
        self.assertIn("_detach_running_thread(worker)", close_source)
        self.assertNotIn(".wait(", close_source)
        self.assertNotIn(".terminate(", close_source)

    def test_stable_mapping_and_master_use_process_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            mapping_path = base / "field_mapping.json"
            mapping_path.write_text('{"受注No": "order_no"}', encoding="utf-8")
            first_mapping, first_hit = load_field_mapping_cached(mapping_path)
            second_mapping, second_hit = load_field_mapping_cached(mapping_path)
            self.assertFalse(first_hit)
            self.assertTrue(second_hit)
            self.assertEqual(first_mapping, second_mapping)

            master_path = base / "master.csv"
            master_path.write_text(
                ",".join(KAKOU_MASTER_HEADERS) + "\n",
                encoding="utf-8-sig",
            )
            first_master, first_master_hit = load_master_cached(master_path)
            second_master, second_master_hit = load_master_cached(master_path)
            self.assertFalse(first_master_hit)
            self.assertTrue(second_master_hit)
            self.assertEqual(first_master, second_master)


if __name__ == "__main__":
    unittest.main()
