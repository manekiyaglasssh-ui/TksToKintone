"""指図書系(03-06)の表右端「受入日」列フォントサイズの回帰テスト。

要件: 03 指図書(1) / 04 指図書(2) / 05 梱包明細書 / 06 配送指示書 の表右端
（受入日）列の文字サイズを、売上伝票(01)の表「摘要」列と同じにする。

PDF生成時の setFont 命令を捕捉し、右端列に描かれる文字（DUMMY_DATA の
note_lines 由来「倉庫ま」「加」など）のフォントサイズを実測して検証する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import reportlab.pdfgen.canvas as rlc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_RealCanvas = rlc.Canvas


class _RecordingCanvas:
    """実カンバスを包み、drawString 系の直前フォントサイズを記録する。"""

    def __init__(self, *args, **kwargs):
        self._c = _RealCanvas(*args, **kwargs)
        self._fs = None
        self.records: list[tuple[str, float | None]] = []

    def setFont(self, name, size, *args, **kwargs):
        self._fs = size
        return self._c.setFont(name, size, *args, **kwargs)

    def drawString(self, x, y, text, *args, **kwargs):
        self.records.append((text, self._fs))
        return self._c.drawString(x, y, text, *args, **kwargs)

    def drawRightString(self, x, y, text, *args, **kwargs):
        self.records.append((text, self._fs))
        return self._c.drawRightString(x, y, text, *args, **kwargs)

    def drawCentredString(self, x, y, text, *args, **kwargs):
        self.records.append((text, self._fs))
        return self._c.drawCentredString(x, y, text, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._c, name)


def _render_font_for(voucher_id: str, probe: str) -> float:
    """指定伝票を生成し、probe 文字列を描いたときのフォントサイズを返す。"""
    from app.voucher_service import build_vouchers_pdf_bytes
    from app.voucher_templates import DUMMY_DATA

    captured: list[_RecordingCanvas] = []
    orig_init = _RecordingCanvas.__init__

    def _init(self, *a, **k):
        orig_init(self, *a, **k)
        captured.append(self)

    with patch.object(rlc, "Canvas", _RecordingCanvas), \
            patch.object(_RecordingCanvas, "__init__", _init):
        build_vouchers_pdf_bytes([voucher_id], DUMMY_DATA, base_dir=PROJECT_ROOT)

    sizes = {fs for canvas in captured for text, fs in canvas.records if text == probe}
    if not sizes:
        raise AssertionError(f"伝票 {voucher_id} に probe '{probe}' が描画されませんでした")
    if len(sizes) > 1:
        raise AssertionError(f"伝票 {voucher_id} の probe '{probe}' に複数サイズ: {sizes}")
    return sizes.pop()


class TestRemarkColumnFontSize(unittest.TestCase):
    """右端列フォントサイズ＝売上伝票 摘要列フォントサイズの検証。"""

    PROBE = "倉庫ま"  # DUMMY_DATA の note_lines 由来。表右端列に描かれる。

    @classmethod
    def setUpClass(cls):
        from app import voucher_service
        voucher_service._ensure_font()
        # 売上伝票(01) 表「摘要」列の基準フォントサイズ
        cls.sales_remark_fs = _render_font_for("01", cls.PROBE)

    def test_sales_remark_uses_detail_note_font(self) -> None:
        """売上伝票の摘要列フォントが DETAIL_NOTE_FONT_SIZE（変更しないこと）。"""
        from app import voucher_service
        self.assertAlmostEqual(self.sales_remark_fs,
                               voucher_service.DETAIL_NOTE_FONT_SIZE)

    def test_shizu1_right_column_matches_sales_remark(self) -> None:
        """指図書(1) 受入日列＝売上伝票 摘要列。"""
        self.assertAlmostEqual(_render_font_for("03", self.PROBE),
                               self.sales_remark_fs)

    def test_shizu2_right_column_matches_sales_remark(self) -> None:
        """指図書(2) 受入日列＝売上伝票 摘要列。"""
        self.assertAlmostEqual(_render_font_for("04", self.PROBE),
                               self.sales_remark_fs)

    def test_konpou_right_column_matches_sales_remark(self) -> None:
        """梱包明細書 受入日列＝売上伝票 摘要列。"""
        self.assertAlmostEqual(_render_font_for("05", self.PROBE),
                               self.sales_remark_fs)

    def test_haisou_right_column_matches_sales_remark(self) -> None:
        """配送指示書 受入日列＝売上伝票 摘要列。"""
        self.assertAlmostEqual(_render_font_for("06", self.PROBE),
                               self.sales_remark_fs)

    def test_table_remark_constant_equals_detail_note(self) -> None:
        """共通定数 TABLE_REMARK_FONT_SIZE が摘要列フォントと一致すること。"""
        from app import voucher_service
        self.assertAlmostEqual(voucher_service.TABLE_REMARK_FONT_SIZE,
                               voucher_service.DETAIL_NOTE_FONT_SIZE)

    def test_delivery_vouchers_unaffected(self) -> None:
        """納品書(07)・受領書(08)は本変更の影響を受けず生成できること。"""
        from app.voucher_service import build_vouchers_pdf_bytes
        from app.voucher_templates import DUMMY_DATA
        self.assertGreater(
            len(build_vouchers_pdf_bytes(["07", "08"], DUMMY_DATA, base_dir=PROJECT_ROOT)),
            0)


if __name__ == "__main__":
    unittest.main()
