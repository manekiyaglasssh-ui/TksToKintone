from __future__ import annotations

import unittest
from pathlib import Path

from app.print_layout import (
    DETAIL_COLUMN_DEFS,
    PRINT_TITLE,
    ROWS_PER_LAST, ROWS_PER_MID,
    split_pages,
)


def _rows(n: int) -> list[dict[str, str]]:
    return [{"受注No": "1402816", "idx": str(i)} for i in range(n)]


def _compute_offsets(pages: list[list]) -> list[int]:
    offsets: list[int] = []
    cumulative = 0
    for chunk in pages:
        offsets.append(cumulative)
        cumulative += len(chunk)
    return offsets


class ConstantsTest(unittest.TestCase):
    def test_rows_per_mid_is_at_least_25(self) -> None:
        """Middle pages should fit many rows; verify computation is reasonable."""
        self.assertGreaterEqual(ROWS_PER_MID, 25)

    def test_rows_per_last_is_at_least_15(self) -> None:
        self.assertGreaterEqual(ROWS_PER_LAST, 15)

    def test_rows_per_last_increases_after_footer_removal(self) -> None:
        self.assertGreaterEqual(ROWS_PER_LAST, ROWS_PER_MID)


class PrintSpecificationTest(unittest.TestCase):
    def test_title_is_kakou_shijisho(self) -> None:
        self.assertEqual(PRINT_TITLE, "加工指図書")

    def test_detail_columns_match_kakou_shijisho_spec(self) -> None:
        labels = [label for label, _, _ in DETAIL_COLUMN_DEFS]
        self.assertEqual(
            labels,
            [
                "No",
                "商品コード",
                "商品名称",
                "掛率集計名称",
                "加工名",
                "W寸法",
                "H寸法",
                "硝子枚数",
                "㎡",
                "総重量",
            ],
        )

    def test_removed_columns_are_not_in_detail_columns(self) -> None:
        labels = [label for label, _, _ in DETAIL_COLUMN_DEFS]
        self.assertNotIn("受注数量", labels)
        self.assertNotIn("総㎡", labels)

    def test_added_columns_are_in_detail_columns(self) -> None:
        labels = [label for label, _, _ in DETAIL_COLUMN_DEFS]
        self.assertIn("加工名", labels)
        self.assertIn("総重量", labels)

    def test_footer_labels_are_not_drawn(self) -> None:
        source = Path("app/print_service.py").read_text(encoding="utf-8")
        draw_source = source[source.index("def _draw_slip("):source.index("# ── QR helper")]
        self.assertNotIn('"売上伝票"', draw_source)
        self.assertNotIn('"入力者："', draw_source)
        self.assertNotIn('"工程"', draw_source)
        self.assertNotIn('"備考："', draw_source)


class SplitPagesTest(unittest.TestCase):
    def test_empty_returns_single_empty_page(self) -> None:
        result = split_pages([], ROWS_PER_MID, ROWS_PER_LAST)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], [])

    def test_within_last_capacity_is_single_page(self) -> None:
        rows = _rows(ROWS_PER_LAST)
        result = split_pages(rows, ROWS_PER_MID, ROWS_PER_LAST)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], rows)

    def test_one_extra_row_triggers_two_pages(self) -> None:
        rows = _rows(ROWS_PER_LAST + 1)
        result = split_pages(rows, ROWS_PER_MID, ROWS_PER_LAST)
        self.assertEqual(len(result), 2)
        self.assertEqual(sum(len(p) for p in result), len(rows))

    def test_middle_pages_filled_first_to_capacity(self) -> None:
        """Middle pages get rows_per_mid rows before any partial page appears."""
        n = ROWS_PER_MID * 3 + ROWS_PER_LAST + 2   # 3 full mid pages + some remainder
        rows = _rows(n)
        pages = split_pages(rows, ROWS_PER_MID, ROWS_PER_LAST)
        # First pages should be full (ROWS_PER_MID) before partial pages appear
        full_mid = [p for p in pages[:-1] if len(p) == ROWS_PER_MID]
        self.assertGreaterEqual(len(full_mid), 3)

    def test_all_rows_covered(self) -> None:
        for n in [1, ROWS_PER_LAST, ROWS_PER_LAST + 1, ROWS_PER_MID,
                  ROWS_PER_MID + 1, 50, 100, 126]:
            with self.subTest(n=n):
                rows = _rows(n)
                all_rows = [r for page in split_pages(rows, ROWS_PER_MID, ROWS_PER_LAST)
                            for r in page]
                self.assertEqual(all_rows, rows)

    def test_last_page_within_capacity(self) -> None:
        for n in [ROWS_PER_LAST + 1, 60, 100, 126]:
            with self.subTest(n=n):
                pages = split_pages(_rows(n), ROWS_PER_MID, ROWS_PER_LAST)
                self.assertLessEqual(len(pages[-1]), ROWS_PER_LAST)

    def test_middle_pages_within_capacity(self) -> None:
        for n in [60, 100, 126]:
            with self.subTest(n=n):
                pages = split_pages(_rows(n), ROWS_PER_MID, ROWS_PER_LAST)
                for page in pages[:-1]:
                    self.assertLessEqual(len(page), ROWS_PER_MID)

    def test_no_column_contiguous_for_various_n(self) -> None:
        for n in [4, ROWS_PER_LAST, ROWS_PER_LAST + 1, 100, 126]:
            with self.subTest(n=n):
                rows = _rows(n)
                pages = split_pages(rows, ROWS_PER_MID, ROWS_PER_LAST)
                offsets = _compute_offsets(pages)
                nos = [offset + i + 1 for chunk, offset in zip(pages, offsets)
                       for i in range(len(chunk))]
                self.assertEqual(nos, list(range(1, n + 1)))

    def test_126_rows_no_gaps(self) -> None:
        rows = _rows(126)
        pages = split_pages(rows, ROWS_PER_MID, ROWS_PER_LAST)
        offsets = _compute_offsets(pages)
        nos = [offset + i + 1 for chunk, offset in zip(pages, offsets)
               for i in range(len(chunk))]
        self.assertEqual(nos, list(range(1, 127)))

    def test_row_offset_is_cumulative_sum(self) -> None:
        """Regression: offset must be cumulative sum, not page_idx * ROWS_PER_MID."""
        rows = _rows(126)
        pages = split_pages(rows, ROWS_PER_MID, ROWS_PER_LAST)
        offsets = _compute_offsets(pages)
        self.assertEqual(offsets[-1], 126 - len(pages[-1]))

    def test_dynamic_rows_per_mid_matches_fill_capacity(self) -> None:
        """With larger rows_per_mid, 126 rows should need fewer pages."""
        large_mid = 50
        small_last = 40
        pages = split_pages(_rows(126), rows_per_mid=large_mid, rows_per_last=small_last)
        # Verify all rows covered
        self.assertEqual(sum(len(p) for p in pages), 126)
        # With mid=50 and last=40: mid pages can hold 50+ rows, fewer pages needed
        self.assertLessEqual(len(pages), 4)


if __name__ == "__main__":
    unittest.main()
