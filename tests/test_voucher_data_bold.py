"""伝票PDFの取得データ太字化テスト。

データ（OLAP取得値・入力値）は擬似太字（横方向の多重描画）で描き、固定ラベル・
見出しは従来フォントのまま1回描画であることを、PDF生成時の描画命令を捕捉して検証する。
"""
from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import reportlab.pdfgen.canvas as rlc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_RealCanvas = rlc.Canvas


class _CountingCanvas:
    """実カンバスを包み、drawString系の描画テキストの回数を数える。

    太字データは _emit_text により同一テキストが2回描画されるため、回数で
    太字/非太字を判定できる。
    """

    def __init__(self, *args, **kwargs):
        self._c = _RealCanvas(*args, **kwargs)
        self.draw_counts: Counter[str] = Counter()
        self._last_font = ""
        # テキストごとの (x, font) 記録。太字強度・フォント検証用。
        self.draws: dict[str, list[tuple[float, str]]] = {}

    def setFont(self, name, size, *args, **kwargs):
        self._last_font = name
        return self._c.setFont(name, size, *args, **kwargs)

    def _record(self, x, text):
        self.draw_counts[text] += 1
        self.draws.setdefault(text, []).append((x, self._last_font))

    def drawString(self, x, y, text, *args, **kwargs):
        self._record(x, text)
        return self._c.drawString(x, y, text, *args, **kwargs)

    def drawRightString(self, x, y, text, *args, **kwargs):
        self._record(x, text)
        return self._c.drawRightString(x, y, text, *args, **kwargs)

    def drawCentredString(self, x, y, text, *args, **kwargs):
        self._record(x, text)
        return self._c.drawCentredString(x, y, text, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._c, name)


def _render_counts(voucher_id: str) -> Counter:
    from app.voucher_service import build_vouchers_pdf_bytes
    from app.voucher_templates import DUMMY_DATA

    captured: list[_CountingCanvas] = []
    orig_init = _CountingCanvas.__init__

    def _init(self, *a, **k):
        orig_init(self, *a, **k)
        captured.append(self)

    with patch.object(rlc, "Canvas", _CountingCanvas), \
            patch.object(_CountingCanvas, "__init__", _init):
        build_vouchers_pdf_bytes([voucher_id], DUMMY_DATA, base_dir=PROJECT_ROOT)

    total: Counter = Counter()
    for canvas in captured:
        total.update(canvas.draw_counts)
    return total


def _render_draws(voucher_id: str) -> dict:
    """テキストごとの (x, font) 記録を全カンバス分マージして返す。"""
    from app.voucher_service import build_vouchers_pdf_bytes
    from app.voucher_templates import DUMMY_DATA

    captured: list[_CountingCanvas] = []
    orig_init = _CountingCanvas.__init__

    def _init(self, *a, **k):
        orig_init(self, *a, **k)
        captured.append(self)

    with patch.object(rlc, "Canvas", _CountingCanvas), \
            patch.object(_CountingCanvas, "__init__", _init):
        build_vouchers_pdf_bytes([voucher_id], DUMMY_DATA, base_dir=PROJECT_ROOT)

    merged: dict[str, list[tuple[float, str]]] = {}
    for canvas in captured:
        for text, entries in canvas.draws.items():
            merged.setdefault(text, []).extend(entries)
    return merged


class _FakeCanvas:
    """描画命令を記録する軽量フェイク（ヘルパー単体テスト用）。"""

    def __init__(self):
        self.calls: list[tuple[str, float, float, str, str, float]] = []
        self._font = ""
        self._size = 0.0

    def setFont(self, name, size, *a, **k):
        self._font = name
        self._size = size

    def stringWidth(self, text, font, size):
        # 1文字 = size 相当の単純幅（縮小/クリップ判定を安定させる）。
        return len(text) * size

    def drawString(self, x, y, text, *a, **k):
        self.calls.append(("l", x, y, text, self._font, self._size))

    def drawCentredString(self, x, y, text, *a, **k):
        self.calls.append(("c", x, y, text, self._font, self._size))

    def drawRightString(self, x, y, text, *a, **k):
        self.calls.append(("r", x, y, text, self._font, self._size))


class TestDataBoldStrength(unittest.TestCase):
    def test_bold_offset_is_half_point_fifteen(self) -> None:
        # 1. 擬似太字オフセットは 0.15pt（0.3 の半分）に弱める。
        from app import voucher_service as vs

        self.assertAlmostEqual(vs.DATA_BOLD_OFFSET_PT, 0.15)

    def test_bold_sentinel_resolves_to_base_cid_font(self) -> None:
        # 2. DATA_BOLD_FONT_NAME は実描画時に従来CIDフォントへ解決される。
        from app import voucher_service as vs

        self.assertEqual(vs._resolve_base_font(vs.DATA_BOLD_FONT_NAME), vs._FONT_NAME)
        self.assertEqual(vs._FONT_NAME, "HeiseiKakuGo-W5")
        # ラベル用も同じ従来CIDフォント（フォント変更はしない）。
        self.assertEqual(vs._resolve_base_font(vs.LABEL_FONT_NAME), vs._FONT_NAME)
        self.assertEqual(vs.DATA_FONT_NAME, vs._FONT_NAME)

    def test_bold_second_strike_offset_matches_constant(self) -> None:
        # 4. 太字は二重描画だが、2打目の x は 1打目 + 0.15pt のみ。
        from app import voucher_service as vs

        c = _FakeCanvas()
        vs._str(c, "値", 10.0, 20.0, 8.0)
        xs = [x for _, x, _, t, _, _ in c.calls if t == "値"]
        self.assertEqual(len(xs), 2)
        self.assertAlmostEqual(xs[0], 10.0)
        self.assertAlmostEqual(xs[1] - xs[0], vs.DATA_BOLD_OFFSET_PT)
        # ベースライン（y）は動かさない。
        ys = {y for _, _, y, t, _, _ in c.calls if t == "値"}
        self.assertEqual(ys, {20.0})


class TestDataBoldHelpers(unittest.TestCase):
    def test_str_data_default_is_bold_double_strike(self) -> None:
        from app import voucher_service as vs

        c = _FakeCanvas()
        vs._str(c, "データ値", 10.0, 20.0, 8.0)
        texts = [t for _, _, _, t, _, _ in c.calls]
        # 太字は同一テキストを2回（アンカー + 微小オフセット）描画する。
        self.assertEqual(texts.count("データ値"), 2)
        # 実際に setFont へ渡るのは登録済みCIDフォント。
        fonts = {f for *_, f, _ in c.calls}
        self.assertEqual(fonts, {vs._FONT_NAME})

    def test_str_label_font_is_single_strike(self) -> None:
        from app import voucher_service as vs

        c = _FakeCanvas()
        vs._str(c, "固定ラベル", 10.0, 20.0, 8.0, font_name=vs.LABEL_FONT_NAME)
        texts = [t for _, _, _, t, _, _ in c.calls]
        self.assertEqual(texts.count("固定ラベル"), 1)

    def test_rstr_and_cstr_data_are_bold(self) -> None:
        from app import voucher_service as vs

        c = _FakeCanvas()
        vs._rstr(c, "123", 50.0, 20.0, 8.0)
        vs._cstr(c, "中央", 50.0, 20.0, 8.0)
        counts = Counter(t for _, _, _, t, _, _ in c.calls)
        self.assertEqual(counts["123"], 2)
        self.assertEqual(counts["中央"], 2)

    def test_str_name_autoshrink_still_bold(self) -> None:
        # 商品名の自動縮小が働く長い名称でも太字（多重描画）が維持される。
        from app import voucher_service as vs

        c = _FakeCanvas()
        long_name = "とても長い商品名称ABCDEFGHIJKLMNOP"
        vs._str_name(c, long_name, 10.0, 20.0, 8.0, max_w=20.0)
        texts = [t for _, _, _, t, _, _ in c.calls]
        self.assertEqual(texts.count(long_name), 2)
        # 縮小後フォントサイズは元より小さい（自動縮小ロジック維持）。
        used_sizes = [s for *_, s in c.calls]
        self.assertLess(min(used_sizes), 8.0)

    def test_draw_text_fit_width_bold_returns_reduced_size(self) -> None:
        from app import voucher_service as vs

        c = _FakeCanvas()
        fs = vs.draw_text_fit_width(
            c, "long-text-value", 0.0, 0.0, 10.0, vs.DATA_BOLD_FONT_NAME, 8.0, 5.0
        )
        self.assertLessEqual(fs, 8.0)
        texts = [t for _, _, _, t, _, _ in c.calls]
        self.assertEqual(texts.count("long-text-value"), 2)


class TestVoucherPdfDataBold(unittest.TestCase):
    def _assert_data_bold(self, counts: Counter, value: str) -> None:
        self.assertGreaterEqual(
            counts.get(value, 0), 2, f"データ '{value}' が太字（多重描画）で描かれていません"
        )

    def _assert_label_normal(self, counts: Counter, label: str) -> None:
        self.assertEqual(
            counts.get(label, 0), 1, f"固定ラベル '{label}' は従来フォント（単一描画）のままにする"
        )

    def test_form01_header_data_bold_labels_normal(self) -> None:
        counts = _render_counts("01")
        # データ: コードNo値・得意先名・受注No。
        self._assert_data_bold(counts, "40630")
        self._assert_data_bold(counts, "株式会社たくみ硝子店")
        self._assert_data_bold(counts, "1405113")
        # 明細データ: 商品名。
        self._assert_data_bold(counts, "MT5 四方 磨き")
        # 固定ラベル。
        self._assert_label_normal(counts, "コードNo")
        self._assert_label_normal(counts, "得意先名")
        self._assert_label_normal(counts, "受注No")
        # 列見出し（全角スペース入り）。
        self._assert_label_normal(counts, "品　名")
        self._assert_label_normal(counts, "数　量")
        self._assert_label_normal(counts, "単　価")
        self._assert_label_normal(counts, "金　額")

    def _assert_bold_font_and_offset(self, draws: dict, value: str) -> None:
        from app import voucher_service as vs

        entries = draws.get(value, [])
        self.assertGreaterEqual(len(entries), 2, f"データ '{value}' が太字描画されていません")
        xs = [x for x, _ in entries]
        fonts = {f for _, f in entries}
        # 実描画フォントは従来CIDフォント（表内も同じ、フォント変更なし）。
        self.assertEqual(fonts, {vs._FONT_NAME})
        # 2打目のオフセットは 0.15pt。
        self.assertAlmostEqual(min(xs) + vs.DATA_BOLD_OFFSET_PT, max(xs))

    def test_table_and_header_data_same_font_and_offset(self) -> None:
        # 3/5. 表内データ（商品名）もヘッダーデータ（コードNo/得意先名）も
        # 従来CIDフォント・同じ0.15ptで太字化される。
        draws = _render_draws("01")
        self._assert_bold_font_and_offset(draws, "MT5 四方 磨き")   # 表内データ
        self._assert_bold_font_and_offset(draws, "40630")          # コードNo値
        self._assert_bold_font_and_offset(draws, "株式会社たくみ硝子店")  # 得意先名

    def test_all_forms_generate_and_labels_normal(self) -> None:
        from app.voucher_templates import VOUCHER_IDS

        for vid in VOUCHER_IDS:
            counts = _render_counts(vid)
            self.assertTrue(counts, f"伝票 {vid} が描画されませんでした")
            # 全伝票でコードNoラベルは単一描画（見出しは据え置き）。
            if "コードNo" in counts:
                self.assertEqual(counts["コードNo"], 1)


if __name__ == "__main__":
    unittest.main()
