from __future__ import annotations

import os
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from app.voucher_range import (
    LARGE_RANGE_CONFIRM_COUNT,
    MAX_ORDER_RANGE_COUNT,
    VoucherRangeDialog,
    VoucherRangeResultDialog,
    VoucherRangeWorker,
    iter_order_no_range,
    iter_order_range,
    parse_range_order_no,
    validate_order_no_range,
    validate_order_range,
)


class _Owner:
    def reflect_range_fetch_result(self, order_no, data):
        return "新規登録"


class TestOrderRangeValidation(unittest.TestCase):
    def test_normal_equal_fullwidth_and_leading_zero(self):
        self.assertEqual(validate_order_range("1400001", "1400003").count, 3)
        self.assertEqual(validate_order_range("7", "7").count, 1)
        result = validate_order_range("００１", "００３")
        self.assertTrue(result.valid)
        self.assertEqual(list(iter_order_range(result.start, result.end)), ["001", "002", "003"])

    def test_prefixed_ranges_preserve_prefix_case_and_leading_zeroes(self):
        parsed = parse_range_order_no("ＡＢ００１")
        self.assertEqual((parsed.prefix, parsed.number, parsed.number_text, parsed.width), ("AB", 1, "001", 3))
        self.assertEqual(
            list(iter_order_no_range("C405113", "C405115")),
            ["C405113", "C405114", "C405115"],
        )
        self.assertEqual(list(iter_order_no_range("AB001", "AB003")), ["AB001", "AB002", "AB003"])
        self.assertEqual(list(iter_order_no_range("c405113", "C405114")), ["c405113", "c405114"])
        fullwidth = validate_order_no_range("Ｃ４０５１１３", "Ｃ４０５１１４")
        self.assertTrue(fullwidth.valid)
        self.assertEqual(fullwidth.count, 2)
        self.assertEqual(list(iter_order_no_range(fullwidth.start, fullwidth.end)), ["C405113", "C405114"])

    def test_prefixed_range_validation_errors_are_specific(self):
        cases = (
            ("C405113", "D405114", "英字部分が一致していません"),
            ("C405120", "C405113", "終了受注No以下"),
            ("ABC", "ABD", "末尾には連番となる数字"),
            ("C40A5113", "C405114", "英字の後に数字が続く形式"),
            ("", "C405114", "開始受注Noと終了受注Noを入力"),
        )
        for start, end, message in cases:
            with self.subTest(start=start, end=end):
                result = validate_order_no_range(start, end)
                self.assertFalse(result.valid)
                self.assertIn(message, result.error)

    def test_invalid_inputs(self):
        for start, end in (("", "2"), ("1", ""), ("a", "2"), ("3", "2")):
            self.assertFalse(validate_order_range(start, end).valid)

    def test_boundaries_do_not_materialize_range(self):
        self.assertEqual(MAX_ORDER_RANGE_COUNT, 500)
        self.assertTrue(validate_order_range("1", "100").valid)
        self.assertTrue(validate_order_range("1", "101").valid)
        self.assertTrue(validate_order_range("1", "500").valid)
        exceeded = validate_order_range("1", "501")
        self.assertFalse(exceeded.valid)
        self.assertTrue(exceeded.limit_exceeded)
        huge = validate_order_range("1", "999999999999999999999999")
        self.assertTrue(huge.limit_exceeded)

    def test_prefixed_boundaries_use_only_numeric_suffix(self):
        self.assertEqual(validate_order_range("C001", "C100").count, 100)
        self.assertEqual(validate_order_range("C001", "C101").count, 101)
        self.assertEqual(validate_order_range("C001", "C500").count, 500)
        exceeded = validate_order_range("C001", "C501")
        self.assertTrue(exceeded.limit_exceeded)
        self.assertNotIsInstance(iter_order_no_range("C001", "C500"), list)


class TestVoucherRangeDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self):
        return VoucherRangeDialog(_Owner(), mock.Mock(return_value={}), lambda value: str(value).strip())

    def test_100_starts_without_confirmation(self):
        dialog = self.dialog()
        dialog.start_edit.setText("1")
        dialog.end_edit.setText("100")
        with mock.patch.object(dialog, "_start_worker") as start, mock.patch.object(
            QMessageBox, "question", side_effect=AssertionError("100件では確認しない")
        ):
            self.assertTrue(dialog.start_fetch())
        start.assert_called_once()

    def test_prefixed_range_starts_worker_with_complete_order_numbers(self):
        dialog = self.dialog()
        dialog.start_edit.setText("C405113")
        dialog.end_edit.setText("C405114")
        with mock.patch.object(dialog, "_start_worker") as start:
            self.assertTrue(dialog.start_fetch())
        validation = start.call_args.args[0]
        self.assertEqual((validation.start, validation.end, validation.count), ("C405113", "C405114", 2))
        self.assertIn("C405113", dialog.example_label.text())

    def test_101_and_500_start_only_after_confirmation(self):
        for count in (LARGE_RANGE_CONFIRM_COUNT, MAX_ORDER_RANGE_COUNT):
            dialog = self.dialog()
            dialog.start_edit.setText("1")
            dialog.end_edit.setText(str(count))
            with mock.patch.object(dialog, "_start_worker") as start, mock.patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
            ) as question:
                self.assertTrue(dialog.start_fetch())
            question.assert_called_once()
            start.assert_called_once()

    def test_large_confirmation_cancel_does_not_start(self):
        dialog = self.dialog()
        dialog.start_edit.setText("1")
        dialog.end_edit.setText("101")
        with mock.patch.object(dialog, "_start_worker") as start, mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel
        ):
            self.assertFalse(dialog.start_fetch())
        start.assert_not_called()

    def test_501_never_creates_worker_or_calls_olap_and_can_be_corrected(self):
        fetch = mock.Mock(return_value={})
        dialog = VoucherRangeDialog(_Owner(), fetch, lambda value: str(value).strip())
        dialog.start_edit.setText("1")
        dialog.end_edit.setText("501")
        with mock.patch.object(dialog, "_start_worker") as start:
            self.assertFalse(dialog.start_fetch())
        start.assert_not_called()
        fetch.assert_not_called()
        self.assertFalse(dialog.fetch_button.isEnabled())
        dialog.end_edit.setText("500")
        self.assertTrue(dialog.fetch_button.isEnabled())

    def test_result_dialog_left_success_right_failure_and_one_click_copy(self):
        dialog = VoucherRangeResultDialog(
            [("C405113", "新規登録"), ("C405114", "登録済み")],
            [("C405115", "対象データなし")],
        )
        self.assertEqual(dialog.success_table.item(0, 0).text(), "C405113")
        self.assertEqual(dialog.failure_table.item(0, 0).text(), "C405115")
        dialog.success_copy.click()
        self.assertEqual(QApplication.clipboard().text(), "C405113\nC405114")
        dialog.failure_copy.click()
        self.assertEqual(QApplication.clipboard().text(), "C405115")

    def test_zero_result_copy_buttons_are_disabled(self):
        dialog = VoucherRangeResultDialog([], [])
        self.assertFalse(dialog.success_copy.isEnabled())
        self.assertFalse(dialog.failure_copy.isEnabled())

    def test_qthread_fetch_keeps_gui_timer_responsive_and_cleans_up(self):
        def slow_fetch(_number):
            time.sleep(0.08)
            return {}

        dialog = VoucherRangeDialog(_Owner(), slow_fetch, lambda value: str(value).strip())
        dialog.start_edit.setText("1")
        dialog.end_edit.setText("1")
        timer_fired = []
        QTimer.singleShot(0, lambda: timer_fired.append(True))
        with mock.patch.object(VoucherRangeResultDialog, "exec", return_value=0):
            self.assertTrue(dialog.start_fetch())
            deadline = time.monotonic() + 2
            while dialog._thread is not None and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.005)
            QApplication.processEvents()
        self.assertTrue(timer_fired)
        self.assertIsNone(dialog._thread)
        self.assertEqual(dialog.progress_bar.value(), 1)


class TestVoucherRangeWorker(unittest.TestCase):
    def test_mixed_results_continue_and_emit_progress_items(self):
        def fetch(order_no):
            if order_no == "C002":
                raise RuntimeError("対象データが見つかりません")
            return {"order_no": order_no}

        worker = VoucherRangeWorker("C001", "C003", fetch)
        successes, failures, finished = [], [], []
        worker.item_fetched.connect(lambda number, data: successes.append(number))
        worker.item_failed.connect(lambda number, reason, kind: failures.append((number, kind)))
        worker.finished.connect(lambda cancelled, processed: finished.append((cancelled, processed)))
        worker.run()
        self.assertEqual(successes, ["C001", "C003"])
        self.assertEqual(failures, [("C002", "not_found")])
        self.assertEqual(finished, [(False, 3)])

    def test_prefixed_order_number_is_passed_to_fetch_unchanged(self):
        fetch = mock.Mock(return_value={})
        worker = VoucherRangeWorker("c405113", "C405114", fetch)
        worker.run()
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["c405113", "c405114"])

    def test_cancel_is_cooperative(self):
        worker = VoucherRangeWorker("1", "3", lambda number: {})
        worker.request_cancel()
        finished = []
        worker.finished.connect(lambda cancelled, processed: finished.append((cancelled, processed)))
        worker.run()
        self.assertEqual(finished, [(True, 0)])


if __name__ == "__main__":
    unittest.main()
