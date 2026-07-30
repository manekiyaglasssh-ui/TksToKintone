"""登録前確認画面の列構成・加工種類入力・画面幅を検証する（要件1・3・7・8）。

Qt ウィジェットを使うため offscreen プラットフォームで実行する。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - 環境にPySide6が無い場合はスキップ
    _QT_AVAILABLE = False

if _QT_AVAILABLE:
    from app.gui import (
        KAKOU_TYPE_LEGEND_TEXT,
        PREVIEW_ROW_HEADERS,
        KakouTypeEdit,
        RegistrationPreviewDialog,
        _COL_KAKOU,
        _COL_KAKURITSU_CODE,
        _COL_ORDER_NO,
        _COL_PRODUCT,
        _COL_WARNING,
    )


def _row(order_no: str, row_type: str, product_name: str) -> dict[str, str]:
    return {
        "受注No": order_no,
        "硝/加工": row_type,
        "商品名称": product_name,
        "掛率集計コード": "0300",
        "掛率集計名称": "エッチング",
        "W寸法": "1303",
        "H寸法": "1061",
    }


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できない環境")
class PreviewColumnTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_product_column_between_order_and_code(self) -> None:
        """「受注No」と「掛率集計コード」の間に「商品名称」列がある（要件3）。"""
        self.assertEqual(PREVIEW_ROW_HEADERS[_COL_ORDER_NO], "受注No")
        self.assertEqual(PREVIEW_ROW_HEADERS[_COL_PRODUCT], "商品名称")
        self.assertEqual(PREVIEW_ROW_HEADERS[_COL_KAKURITSU_CODE], "掛率集計コード")
        self.assertEqual(_COL_PRODUCT, _COL_ORDER_NO + 1)
        self.assertEqual(_COL_KAKURITSU_CODE, _COL_PRODUCT + 1)

    def test_legend_includes_extended_types(self) -> None:
        """上部凡例に 9〜11 が含まれる（要件6）。"""
        self.assertIn("9=1方", KAKOU_TYPE_LEGEND_TEXT)
        self.assertIn("10=2方", KAKOU_TYPE_LEGEND_TEXT)
        self.assertIn("11=3方", KAKOU_TYPE_LEGEND_TEXT)
        for old_label in ("9=１方", "10=２方", "11=３方"):
            self.assertNotIn(old_label, KAKOU_TYPE_LEGEND_TEXT)

    def test_dialog_shows_product_name_and_width(self) -> None:
        """商品名称が表示され、画面幅が商品名称列追加に対応している（要件3・7）。"""
        rows = [_row("1000", "2", "強化 長2 磨き"), _row("1000", "1", "素板")]
        for theme in ("light", "dark"):
            dlg = RegistrationPreviewDialog(
                rows=rows,
                master=[],
                shukka_options=["AM", "PM"],
                customer_labels={},
                preview_color_theme=theme,
            )
            try:
                item = dlg.table.item(0, _COL_PRODUCT)
                self.assertIsNotNone(item)
                self.assertEqual(item.text(), "強化 長2 磨き")
                # 全列幅合計が初期表示幅に概ね収まる（極端に潰れない）。
                total = sum(
                    dlg.table.columnWidth(c) for c in range(dlg.table.columnCount())
                )
                self.assertGreater(total, 1000)
                screen = dlg.screen() or QApplication.primaryScreen()
                if screen is not None:
                    self.assertLessEqual(dlg.width(), screen.availableGeometry().width())
                self.assertGreaterEqual(dlg.width(), min(760, total))
            finally:
                dlg.deleteLater()

    def test_processing_row_auto_type_from_product_name(self) -> None:
        """硝/加工=2 の行で商品名称から加工種類が自動入力される（要件4・5）。"""
        rows = [_row("1000", "2", "強化 長2短1 磨き")]
        dlg = RegistrationPreviewDialog(
            rows=rows, master=[], shukka_options=[], customer_labels={},
        )
        try:
            self.assertEqual(dlg._state.kakou_type_by_row[0], "4")
        finally:
            dlg.deleteLater()

    def test_product_column_width_is_1_5x(self) -> None:
        """商品名称列の幅が以前の1.5倍（180→270）になっている（要件7）。"""
        dlg = RegistrationPreviewDialog(
            rows=[_row("1000", "2", "品")], master=[], shukka_options=[],
            customer_labels={},
        )
        try:
            self.assertEqual(dlg.table.columnWidth(_COL_PRODUCT), 270)
            # 商品名称は他のデータ列より広い。
            self.assertGreater(
                dlg.table.columnWidth(_COL_PRODUCT),
                dlg.table.columnWidth(_COL_ORDER_NO),
            )
        finally:
            dlg.deleteLater()

    def test_dialog_width_widened(self) -> None:
        """登録前確認画面は画面外にはみ出さない範囲で広く表示する。"""
        dlg = RegistrationPreviewDialog(
            rows=[_row("1000", "2", "品")], master=[], shukka_options=[],
            customer_labels={},
        )
        try:
            screen = dlg.screen() or QApplication.primaryScreen()
            available_width = dlg.width()
            if screen is not None:
                available = screen.availableGeometry()
                available_width = available.width()
                self.assertLessEqual(dlg.width(), available.width())
                self.assertLessEqual(dlg.minimumWidth(), available.width())
            self.assertGreaterEqual(dlg.minimumWidth(), min(760, available_width))
            # 右端の未登録警告列も列順の末尾にある。
            self.assertEqual(_COL_WARNING, dlg.table.columnCount() - 1)
        finally:
            dlg.deleteLater()

    def test_confirm_window_height_reduced(self) -> None:
        """登録前確認画面の初期高さが従来（700px）より約1.5cm狭い（要件1）。"""
        dlg = RegistrationPreviewDialog(
            rows=[_row("1000", "2", "品")], master=[], shukka_options=[],
            customer_labels={},
        )
        try:
            screen = dlg.screen() or QApplication.primaryScreen()
            # 従来の初期高さ700pxより明確に小さい（画面が十分広ければ約55px減）。
            self.assertLess(dlg.height(), 700)
            if screen is not None:
                available = screen.availableGeometry()
                # 125%相当（availableGeometry）を超えない。
                self.assertLessEqual(dlg.height(), available.height())
                self.assertLessEqual(dlg.minimumHeight(), available.height())
        finally:
            dlg.deleteLater()

    def test_confirm_window_width_matches_table_content(self) -> None:
        """初期ウィンドウ幅が表の実列幅合計に近く、右側に大きな余白を残さない（要件1）。"""
        rows = [_row(str(1000 + i), "2", "強化 長2 磨き") for i in range(6)]
        dlg = RegistrationPreviewDialog(
            rows=rows, master=[], shukka_options=["AM", "PM"], customer_labels={},
        )
        try:
            header_length = dlg.table.horizontalHeader().length()
            screen = dlg.screen() or QApplication.primaryScreen()
            # 表の実幅に対して右側の余白が大きく残らない（実幅＋わずかな余白以内）。
            self.assertLessEqual(dlg.width() - header_length, 60)
            # 旧固定初期幅（1520px）より狭い。
            self.assertLess(dlg.width(), 1520)
            # availableGeometry（125%相当を含む）を超えない。
            if screen is not None:
                self.assertLessEqual(dlg.width(), screen.availableGeometry().width())
            # 下部ボタン・CSV欄・参照ボタンが見切れない最小幅は確保する。
            self.assertGreaterEqual(dlg.minimumWidth(), min(760, dlg.width()))
            # 判定加工名・未登録警告列の縮小は維持される。
            self.assertLess(dlg.table.columnWidth(_COL_KAKOU), 280)
            self.assertLess(dlg.table.columnWidth(_COL_WARNING), 185)
        finally:
            dlg.deleteLater()

    def test_detected_process_and_warning_columns_narrowed(self) -> None:
        """判定加工名・未登録警告列が以前より狭く、他列より広がりすぎない（要件2）。"""
        dlg = RegistrationPreviewDialog(
            rows=[_row("1000", "2", "品")], master=[], shukka_options=[],
            customer_labels={},
        )
        try:
            kakou_w = dlg.table.columnWidth(_COL_KAKOU)
            warn_w = dlg.table.columnWidth(_COL_WARNING)
            # 判定加工名: 旧Stretch(約280)より狭い。
            self.assertLess(kakou_w, 280)
            self.assertGreaterEqual(kakou_w, 150)
            # 未登録警告: 旧185より狭い。
            self.assertLess(warn_w, 185)
            self.assertGreaterEqual(warn_w, 90)
            # 商品名称列(270)は潰れず、狭めた2列より広いまま。
            self.assertGreater(dlg.table.columnWidth(_COL_PRODUCT), warn_w)
        finally:
            dlg.deleteLater()

    def test_startup_emits_timing_logs(self) -> None:
        """登録前確認画面の起動ステップ所要時間ログが出力される（要件3）。"""
        import logging

        rows = [_row(str(1000 + i), "2", "強化 長2 磨き") for i in range(4)]
        logger = logging.getLogger("tks_to_kintone_app")
        with self.assertLogs(logger, level="INFO") as logs:
            dlg = RegistrationPreviewDialog(
                rows=rows, master=[], shukka_options=["AM", "PM"], customer_labels={},
            )
        try:
            text = "\n".join(logs.output)
            for event in (
                "kintone_confirm_init_started",
                "kintone_confirm_init_elapsed_ms",
                "kintone_confirm_existing_check_elapsed_ms",
                "kintone_confirm_table_populate_elapsed_ms",
                "kintone_confirm_cell_widgets_elapsed_ms",
                "kintone_confirm_build_ui_elapsed_ms",
                "kintone_confirm_geometry_elapsed_ms",
                "kintone_confirm_resize_applied",
                "kintone_confirm_target_dialog_width",
            ):
                self.assertIn(event, text)
        finally:
            dlg.deleteLater()

    def test_populate_suppresses_table_updates(self) -> None:
        """テーブル生成中は setUpdatesEnabled(False)→(True) で再描画が抑制される（要件3）。"""
        # populate 完了後は updates 有効に戻っている（生成中のみ抑制）。
        dlg = RegistrationPreviewDialog(
            rows=[_row("1000", "2", "品")], master=[], shukka_options=[],
            customer_labels={},
        )
        try:
            self.assertTrue(dlg.table.updatesEnabled())
            # セルウィジェット数が記録され、全セルに widget を敷き詰めていない。
            # 12列 × 行数の総セル数より十分少ない（表示専用列は QTableWidgetItem）。
            total_cells = dlg.table.rowCount() * dlg.table.columnCount()
            self.assertLess(dlg._cell_widget_count, total_cells)
        finally:
            dlg.deleteLater()

    def test_initial_width_forced_and_released_after_show(self) -> None:
        """初期表示中は maximumWidth で幅を強制し、show後に解除する（要件4）。"""
        rows = [_row(str(1000 + i), "2", "強化 長2 磨き") for i in range(6)]
        dlg = RegistrationPreviewDialog(
            rows=rows, master=[], shukka_options=["AM", "PM"], customer_labels={},
        )
        try:
            column_sum = sum(
                dlg.table.columnWidth(c) for c in range(dlg.table.columnCount())
            )
            # 初期は maximumWidth が有限（幅を強制している）。
            self.assertLess(dlg.maximumWidth(), 16777215)
            # 旧固定幅1520pxより明確に狭い。
            self.assertLess(dlg.width(), 1520)
            # 右側余白（ウィンドウ幅 − 列幅合計）が小さい。
            screen = dlg.screen() or QApplication.primaryScreen()
            if screen is not None and dlg.width() >= column_sum:
                self.assertLessEqual(dlg.width() - column_sum, 80)
            dlg.show()
            # show 後は maximumWidth 制約が解除され、手動で広げられる。
            self.assertEqual(dlg.maximumWidth(), 16777215)
            self.assertFalse(dlg.isMaximized())
        finally:
            dlg.deleteLater()

    def test_narrowed_columns_have_tooltip(self) -> None:
        """狭めた列は長い内容をtooltipで確認できる（要件2）。"""
        rows = [_row("1000", "2", "強化 長2 磨き")]
        master = [{"掛率集計コード": "9999", "加工名": "特殊加工"}]
        dlg = RegistrationPreviewDialog(
            rows=[{**rows[0], "掛率集計コード": "9999"}],
            master=master, shukka_options=[], customer_labels={},
        )
        try:
            kakou_label = dlg._kakou_labels[0]
            # 判定加工名ラベルにtooltip（本文と一致）が設定されている。
            self.assertEqual(kakou_label.toolTip(), kakou_label.text())
        finally:
            dlg.deleteLater()


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できない環境")
class PreviewThemeColorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_dark_theme_uses_dark_palette(self) -> None:
        """ダークテーマでは表・ヘッダーがダーク寄り、ライトでは明るい配色（要件9）。"""
        dark = RegistrationPreviewDialog._preview_colors(True)
        light = RegistrationPreviewDialog._preview_colors(False)
        # 配色キーが揃っている。
        for key in ("table_bg", "header_bg", "header_fg", "gridline"):
            self.assertIn(key, dark)
            self.assertIn(key, light)

        def _lightness(hex_color: str) -> int:
            from PySide6.QtGui import QColor
            return QColor(hex_color).lightness()

        # ダークの表背景はライトの表背景より暗い。
        self.assertLess(_lightness(dark["table_bg"]), _lightness(light["table_bg"]))
        # ダークの表背景・ヘッダーはともに暗色（ライト寄りにならない）。
        self.assertLess(_lightness(dark["table_bg"]), 128)
        self.assertLess(_lightness(dark["header_bg"]), 128)
        # ヘッダーは表背景と区別できる別色。
        self.assertNotEqual(dark["header_bg"], dark["table_bg"])
        # ダークの文字色は明るい（白〜薄いグレー）。
        self.assertGreater(_lightness(dark["fg_hex"]), 128)
        # ライトの文字色は暗い（黒寄り）。
        self.assertLess(_lightness(light["fg_hex"]), 128)

    def test_theme_applied_to_table_stylesheet(self) -> None:
        """テーマ配色が QTableWidget のスタイルシートに反映される（要件3）。"""
        dlg = RegistrationPreviewDialog(
            rows=[_row("1000", "2", "品")], master=[], shukka_options=[],
            customer_labels={}, preview_color_theme="dark",
        )
        try:
            ss = dlg.table.styleSheet()
            self.assertIn("QHeaderView::section", ss)
            self.assertIn("gridline-color", ss)
            # ダークの表背景色が含まれる。
            self.assertIn(RegistrationPreviewDialog._preview_colors(True)["table_bg"], ss)
        finally:
            dlg.deleteLater()

    def test_widget_stylesheet_covers_qlineedit(self) -> None:
        """編集ウィジェットの配色に QLineEdit が含まれ、ダークでも文字色が明るい（要件2・3）。"""
        dark = RegistrationPreviewDialog._preview_colors(True)
        light = RegistrationPreviewDialog._preview_colors(False)
        # KakouTypeEdit は QLineEdit なので QLineEdit ルールが必須。
        self.assertIn("QLineEdit", dark["widget_ss"])
        self.assertIn("QComboBox", dark["widget_ss"])
        self.assertIn("QDateEdit", dark["widget_ss"])
        self.assertIn("QLineEdit", light["widget_ss"])
        # ダークの編集欄は暗い背景＋明るい文字。
        self.assertIn("#2F343A", dark["widget_ss"])
        self.assertIn("#F0F0F0", dark["widget_ss"])
        # ライトの編集欄は白背景＋黒文字。
        self.assertIn("#FFFFFF", light["widget_ss"])
        self.assertIn("#000000", light["widget_ss"])

    def test_kakou_type_widget_has_visible_theme_style(self) -> None:
        """ダークテーマで KakouTypeEdit に文字色付きスタイルが適用され、値が表示される（要件2）。"""
        rows = [_row("1000", "2", "強化 長2 磨き")]
        dlg = RegistrationPreviewDialog(
            rows=rows, master=[], shukka_options=[], customer_labels={},
            preview_color_theme="dark",
        )
        try:
            widget = dlg._kakou_type_widgets[0]
            self.assertIsNotNone(widget)
            ss = widget.styleSheet()
            # 文字色・背景色が明示され、背景と同化しない。
            self.assertIn("QLineEdit", ss)
            self.assertIn("color:", ss)
            # ダーク文字色 #F0F0F0 が含まれる。
            self.assertIn("#F0F0F0", ss)
            # 加工種類の表示値（2：長2）が判読可能なテキストとして入っている。
            self.assertEqual(widget.text(), "2：長2")
        finally:
            dlg.deleteLater()

    def test_light_theme_table_is_light(self) -> None:
        """ライトテーマでは表背景が明るい配色（要件3）。"""
        from PySide6.QtGui import QColor

        dlg = RegistrationPreviewDialog(
            rows=[_row("1000", "2", "品")], master=[], shukka_options=[],
            customer_labels={}, preview_color_theme="light",
        )
        try:
            ss = dlg.table.styleSheet()
            light = RegistrationPreviewDialog._preview_colors(False)
            self.assertIn(light["table_bg"], ss)
            self.assertGreater(QColor(light["table_bg"]).lightness(), 200)
        finally:
            dlg.deleteLater()


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できない環境")
class KakouTypeEditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _commit(self, edit: "KakouTypeEdit", text: str) -> None:
        edit.setText(text)
        edit._commit()

    def test_extended_codes_commit(self) -> None:
        edit = KakouTypeEdit("1")
        self._commit(edit, "9")
        self.assertEqual(edit.code(), "9")
        self.assertEqual(edit.text(), "9：1方")
        self._commit(edit, "10")
        self.assertEqual(edit.code(), "10")
        self.assertEqual(edit.text(), "10：2方")
        self._commit(edit, "11")
        self.assertEqual(edit.code(), "11")
        self.assertEqual(edit.text(), "11：3方")

    def test_invalid_code_reverts(self) -> None:
        edit = KakouTypeEdit("9")
        self._commit(edit, "12")
        self.assertEqual(edit.code(), "9")  # 元の値へ戻る
        self.assertEqual(edit.text(), "9：1方")
        self._commit(edit, "")
        self.assertEqual(edit.code(), "9")


if __name__ == "__main__":
    unittest.main()
