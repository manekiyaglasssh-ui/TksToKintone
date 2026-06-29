"""PreviewState のテスト — Qt 不要の純粋ロジック検証。

登録前確認ダイアログのデータモデルが正しく動作することを確認する。
"""
from __future__ import annotations

import unittest

from app.preview_state import (
    DEFAULT_CUSTOMER_KEY,
    DEFAULT_KAKOU_TYPE,
    KAKOU_TYPE_NAMES,
    PROCESSING_TYPE,
    PreviewState,
    compute_kakou_mm,
    kakou_type_from_product_name,
    kakou_type_label,
)

# ── テスト用マスタ ────────────────────────────────────────

_MASTER = [
    {
        "掛率集計コード": "0300", "掛率集計名称": "エッチング", "加工名": "エッチング",
        "得意先1": "A社向け", "得意先2": "", "得意先3": "", "得意先4": "",
        "メーカー識別掛率集計コード": "MK0300", "メーカー識別コード": "MK", "掛率集計略称": "",
    },
    {
        "掛率集計コード": "0400", "掛率集計名称": "広幅", "加工名": "広幅",
        "得意先1": "", "得意先2": "B社向け", "得意先3": "", "得意先4": "",
        "メーカー識別掛率集計コード": "MK0400", "メーカー識別コード": "MK", "掛率集計略称": "",
    },
]

# ── テスト用 CSV 行ファクトリ ─────────────────────────────

def _row(order_no: str, row_type: str, code: str = "0300", name: str = "エッチング",
         shiage: str = "2026-06-01", shukka: str = "AM") -> dict[str, str]:
    return {
        "受注No": order_no,
        "硝/加工": row_type,
        "掛率集計コード": code,
        "掛率集計名称": name,
        "仕上日": shiage,
        "出荷区分": shukka,
    }


# ── テストクラス ─────────────────────────────────────────

class PreviewStateInitTest(unittest.TestCase):
    def test_initial_values_copied_from_rows(self) -> None:
        rows = [_row("1000", "2", shiage="2026-06-10", shukka="PM")]
        state = PreviewState(rows=rows)
        self.assertEqual(state.shiage_by_row[0], "2026-06-10")
        self.assertEqual(state.shukka_by_row[0], "PM")
        self.assertEqual(state.customer_key_by_row[0], DEFAULT_CUSTOMER_KEY)

    def test_row_count_matches(self) -> None:
        rows = [_row("1000", "1"), _row("1000", "2"), _row("1001", "2")]
        state = PreviewState(rows=rows)
        self.assertEqual(len(state.shiage_by_row), 3)
        self.assertEqual(len(state.shukka_by_row), 3)
        self.assertEqual(len(state.customer_key_by_row), 3)


class PreviewStateShiageTest(unittest.TestCase):
    def test_set_shiage_propagates_to_same_order(self) -> None:
        """仕上日変更は同一受注No の全行（非表示行相当）に反映される。"""
        rows = [
            _row("1000", "1", shiage="2026-06-01"),
            _row("1000", "2", shiage="2026-06-01"),  # same order
            _row("1001", "2", shiage="2026-06-05"),  # different order
        ]
        state = PreviewState(rows=rows)
        state.set_shiage(0, "2026-07-01")
        self.assertEqual(state.shiage_by_row[0], "2026-07-01")
        self.assertEqual(state.shiage_by_row[1], "2026-07-01")  # 同じ受注No → 反映
        self.assertEqual(state.shiage_by_row[2], "2026-06-05")  # 別受注No → 変わらない

    def test_set_shiage_from_hidden_row_also_propagates(self) -> None:
        """絞り込みで非表示になった行（row_idx=1）から変更しても全行に反映される。"""
        rows = [_row("1000", "1", shiage="A"), _row("1000", "2", shiage="A")]
        state = PreviewState(rows=rows)
        state.set_shiage(1, "2026-09-01")  # row 1 が非表示と仮定
        self.assertEqual(state.shiage_by_row[0], "2026-09-01")
        self.assertEqual(state.shiage_by_row[1], "2026-09-01")


class PreviewStateShukkaTest(unittest.TestCase):
    def test_set_shukka_propagates_to_same_order(self) -> None:
        """出荷区分変更は同一受注No の全行に反映される。"""
        rows = [_row("1000", "1", shukka="AM"), _row("1000", "2", shukka="AM"), _row("1001", "2")]
        state = PreviewState(rows=rows)
        state.set_shukka(0, "PM")
        self.assertEqual(state.shukka_by_row[0], "PM")
        self.assertEqual(state.shukka_by_row[1], "PM")
        self.assertEqual(state.shukka_by_row[2], "AM")  # 別受注No


class PreviewStateCustomerKeyTest(unittest.TestCase):
    def test_set_customer_key_affects_only_one_row(self) -> None:
        rows = [_row("1000", "2"), _row("1000", "2")]
        state = PreviewState(rows=rows)
        state.set_customer_key(0, "得意先1")
        self.assertEqual(state.customer_key_by_row[0], "得意先1")
        self.assertEqual(state.customer_key_by_row[1], DEFAULT_CUSTOMER_KEY)

    def test_set_customer_key_for_order_propagates_to_all(self) -> None:
        """一括変更は同一受注No 全行（非表示行相当）に反映される。"""
        rows = [
            _row("1000", "1"),
            _row("1000", "2"),
            _row("1001", "2"),
        ]
        state = PreviewState(rows=rows)
        state.set_customer_key_for_order(0, "得意先2")
        self.assertEqual(state.customer_key_by_row[0], "得意先2")
        self.assertEqual(state.customer_key_by_row[1], "得意先2")  # 同じ受注No
        self.assertEqual(state.customer_key_by_row[2], DEFAULT_CUSTOMER_KEY)  # 別受注No

    def test_set_customer_key_for_order_updates_hidden_rows(self) -> None:
        """得意先選択変更は非表示行（絞り込み非対象行）にも反映される。"""
        rows = [
            _row("1000", "2", code="0300"),  # 表示中（先頭行）
            _row("1000", "2", code="0300"),  # 非表示（同一受注No 2行目）
            _row("1000", "2", code="0300"),  # 非表示（同一受注No 3行目）
            _row("1001", "2", code="0400"),  # 別受注No
        ]
        state = PreviewState(rows=rows)
        state.set_customer_key_for_order(0, "得意先1")
        self.assertEqual(state.customer_key_by_row[0], "得意先1")
        self.assertEqual(state.customer_key_by_row[1], "得意先1")  # 非表示行に反映
        self.assertEqual(state.customer_key_by_row[2], "得意先1")  # 非表示行に反映
        self.assertEqual(state.customer_key_by_row[3], DEFAULT_CUSTOMER_KEY)  # 別受注No

    def test_set_customer_key_for_order_kakou_name_updated_per_row(self) -> None:
        """得意先選択変更後、同一受注No 内の各行が行ごとの加工名を返す。"""
        rows = [
            _row("1000", "2", code="0300", name="エッチング"),
            _row("1000", "2", code="0400", name="広幅"),
        ]
        state = PreviewState(rows=rows)
        state.set_customer_key_for_order(0, "得意先1")
        # 行ごとに加工名が異なる（受注No単位でまとめない）
        self.assertEqual(state.compute_kakou_name(0, _MASTER), "A社向け")
        self.assertEqual(state.compute_kakou_name(1, _MASTER), "広幅")  # 得意先1が空欄 → fallback


class PreviewStateKakouNameTest(unittest.TestCase):
    def test_glass_row_always_empty(self) -> None:
        """硝/加工 = 1 の行は掛率集計コードが存在しても加工名は空欄。"""
        rows = [_row("1000", "1", code="0300", name="エッチング")]
        state = PreviewState(rows=rows)
        self.assertEqual(state.compute_kakou_name(0, _MASTER), "")

    def test_processing_row_with_master_match(self) -> None:
        """硝/加工 = 2 でマスタ一致 → 加工名が返される。"""
        rows = [_row("1000", "2", code="0300", name="エッチング")]
        state = PreviewState(rows=rows)
        self.assertEqual(state.compute_kakou_name(0, _MASTER), "エッチング")

    def test_processing_row_unregistered_code_is_empty(self) -> None:
        """マスタ未登録コードは加工名が空欄になる（登録は止めない）。"""
        rows = [_row("1000", "2", code="9999", name="未登録")]
        state = PreviewState(rows=rows)
        self.assertEqual(state.compute_kakou_name(0, _MASTER), "")

    def test_customer_key_selects_different_column(self) -> None:
        rows = [_row("1000", "2", code="0300")]
        state = PreviewState(rows=rows)
        state.set_customer_key(0, "得意先1")
        self.assertEqual(state.compute_kakou_name(0, _MASTER), "A社向け")

    def test_customer_key_fallback_when_column_empty(self) -> None:
        """得意先列が空欄のときは「加工名」列にフォールバックする。"""
        rows = [_row("1000", "2", code="0400")]
        state = PreviewState(rows=rows)
        state.set_customer_key(0, "得意先1")  # 得意先1 は空欄 → fallback
        self.assertEqual(state.compute_kakou_name(0, _MASTER), "広幅")


class PreviewStateBuildRegistrationRowsTest(unittest.TestCase):
    def _make_state(self) -> PreviewState:
        rows = [
            _row("1000", "1", code="0300", name="エッチング"),  # 素板
            _row("1000", "2", code="0300", name="エッチング"),  # 加工
            _row("1001", "2", code="0400", name="広幅"),         # 別受注
        ]
        return PreviewState(rows=rows)

    def test_returns_all_rows_regardless_of_filter(self) -> None:
        """絞り込みフィルタに関係なく全 CSV レコードが返される。"""
        state = self._make_state()
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(len(result), 3)

    def test_glass_row_kakou_is_empty(self) -> None:
        """硝/加工 = 1 の行は加工名が空欄。"""
        state = self._make_state()
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(result[0]["加工名"], "")  # 素板行

    def test_processing_row_kakou_is_set(self) -> None:
        """硝/加工 = 2 の行に加工名が設定される。"""
        state = self._make_state()
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(result[1]["加工名"], "エッチング")
        self.assertEqual(result[2]["加工名"], "広幅")

    def test_different_kakou_per_row_in_same_order(self) -> None:
        """同じ受注No 内で行ごとに異なる加工名が登録される。"""
        rows = [
            _row("1000", "2", code="0300", name="エッチング"),
            _row("1000", "2", code="0400", name="広幅"),
        ]
        state = PreviewState(rows=rows)
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(result[0]["加工名"], "エッチング")
        self.assertEqual(result[1]["加工名"], "広幅")
        # 同じ受注Noでも加工名は行ごとに独立
        self.assertNotEqual(result[0]["加工名"], result[1]["加工名"])

    def test_shiage_change_reflected_in_all_same_order_rows(self) -> None:
        """仕上日変更後 build_registration_rows が非表示行にも反映する。"""
        rows = [_row("1000", "1"), _row("1000", "2"), _row("1001", "2")]
        state = PreviewState(rows=rows)
        # row 0 を変更（row 1 は非表示と仮定）
        state.set_shiage(0, "2026-12-31")
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(result[0]["仕上日"], "2026-12-31")
        self.assertEqual(result[1]["仕上日"], "2026-12-31")  # 同じ受注No → 反映
        self.assertNotEqual(result[2]["仕上日"], "2026-12-31")  # 別受注No

    def test_batch_customer_change_reflected_in_hidden_rows(self) -> None:
        """一括変更が非表示行相当の内部データにも反映される。"""
        rows = [
            _row("1000", "2", code="0300"),
            _row("1000", "2", code="0300"),  # 仮に非表示
        ]
        state = PreviewState(rows=rows)
        state.set_customer_key_for_order(0, "得意先1")
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(result[0]["加工名"], "A社向け")
        self.assertEqual(result[1]["加工名"], "A社向け")  # 非表示行も反映

    def test_unregistered_code_does_not_block_registration(self) -> None:
        """未登録コードが含まれていても registration_rows が例外なく全行を返す。"""
        rows = [
            _row("1000", "2", code="9999", name="未登録"),
            _row("1001", "2", code="0300", name="エッチング"),
        ]
        state = PreviewState(rows=rows)
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["加工名"], "")      # 未登録 → 空欄
        self.assertEqual(result[1]["加工名"], "エッチング")

    def test_shiage_and_shukka_set_in_result(self) -> None:
        rows = [_row("1000", "2", shiage="2026-06-15", shukka="PM")]
        state = PreviewState(rows=rows)
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(result[0]["仕上日"], "2026-06-15")
        self.assertEqual(result[0]["出荷区分"], "PM")


class PreviewStateFirstIndicesByOrderTest(unittest.TestCase):
    """first_indices_by_order のテスト — 受注No先頭行ウィジェット配置の基準。"""

    def test_single_row_per_order(self) -> None:
        """受注Noが全て異なる場合、全行が先頭行。"""
        rows = [_row("A", "1"), _row("B", "2"), _row("C", "2")]
        state = PreviewState(rows=rows)
        self.assertEqual(state.first_indices_by_order(), {0, 1, 2})

    def test_multiple_rows_same_order(self) -> None:
        """同一受注Noが連続する場合、最初の行のみ先頭行。"""
        rows = [_row("1000", "1"), _row("1000", "2"), _row("1000", "2")]
        state = PreviewState(rows=rows)
        self.assertEqual(state.first_indices_by_order(), {0})

    def test_mixed_orders(self) -> None:
        """複数受注Noが混在する場合、各受注Noの最初の行が先頭行。"""
        rows = [
            _row("A", "1"),
            _row("B", "2"),
            _row("A", "2"),  # A の 2行目 → 先頭行ではない
            _row("C", "2"),
            _row("B", "2"),  # B の 2行目 → 先頭行ではない
        ]
        state = PreviewState(rows=rows)
        self.assertEqual(state.first_indices_by_order(), {0, 1, 3})

    def test_widget_shown_only_for_first_rows(self) -> None:
        """先頭行インデックスでないインデックスはウィジェットを持たない（2行目以降は空欄表示）。"""
        rows = [
            _row("1000", "1"),  # 先頭行 → ウィジェットあり
            _row("1000", "2"),  # 2行目 → ウィジェットなし
            _row("1001", "2"),  # 別受注No先頭行 → ウィジェットあり
        ]
        state = PreviewState(rows=rows)
        first = state.first_indices_by_order()
        self.assertIn(0, first)
        self.assertNotIn(1, first)
        self.assertIn(2, first)


class PreviewStateOrderGroupIndexTest(unittest.TestCase):
    """order_group_index のテスト — 背景色交互表示の基準。"""

    def test_single_order(self) -> None:
        rows = [_row("1000", "1"), _row("1000", "2")]
        state = PreviewState(rows=rows)
        groups = state.order_group_index()
        self.assertEqual(groups, [0, 0])

    def test_two_orders(self) -> None:
        rows = [_row("A", "1"), _row("B", "2"), _row("A", "2")]
        state = PreviewState(rows=rows)
        groups = state.order_group_index()
        self.assertEqual(groups[0], 0)  # A → group 0
        self.assertEqual(groups[1], 1)  # B → group 1
        self.assertEqual(groups[2], 0)  # A → group 0（最初に出た順）

    def test_alternating_groups(self) -> None:
        """受注Noが交互に変わる場合、グループインデックスも交互に変わる。"""
        rows = [
            _row("X", "2"),
            _row("X", "2"),
            _row("Y", "2"),
            _row("Y", "2"),
            _row("Z", "2"),
        ]
        state = PreviewState(rows=rows)
        groups = state.order_group_index()
        self.assertEqual(groups, [0, 0, 1, 1, 2])

    def test_group_count_equals_distinct_orders(self) -> None:
        """グループインデックスの最大値 + 1 = ユニーク受注No数。"""
        rows = [_row("A", "1"), _row("B", "2"), _row("C", "2"), _row("A", "2")]
        state = PreviewState(rows=rows)
        groups = state.order_group_index()
        self.assertEqual(max(groups) + 1, 3)  # A, B, C の3グループ


class PreviewStateIndicesForOrderTest(unittest.TestCase):
    def test_returns_all_indices_with_same_order(self) -> None:
        rows = [_row("A", "1"), _row("B", "2"), _row("A", "2"), _row("C", "2")]
        state = PreviewState(rows=rows)
        self.assertEqual(state.indices_for_order(0), [0, 2])
        self.assertEqual(state.indices_for_order(1), [1])
        self.assertEqual(state.indices_for_order(3), [3])

    def test_indices_for_order_includes_hidden_rows(self) -> None:
        """絞り込みで非表示になっている行インデックスも indices_for_order に含まれる。"""
        rows = [
            _row("1000", "1"),  # 表示中
            _row("1000", "2"),  # 仮に非表示
            _row("1000", "2"),  # 仮に非表示
        ]
        state = PreviewState(rows=rows)
        # 表示行 (row_idx=0) から取得しても非表示行 (1, 2) が含まれる
        self.assertEqual(state.indices_for_order(0), [0, 1, 2])


class PreviewStateRowIdxConsistencyTest(unittest.TestCase):
    """画面 row_idx と PreviewState 内部 row_idx の一致性を確認する。

    QTableWidget で setSortingEnabled(False) の場合、
    table.rowAt() が返す論理行インデックスは setRowHidden() の影響を受けず
    PreviewState の行インデックスと1対1で対応する。
    本テストはその前提を PreviewState 側から検証する。
    """

    def test_row_indices_are_stable_regardless_of_filter(self) -> None:
        """フィルタ（非表示）を考慮しても内部 row_idx は変化しない。"""
        rows = [
            _row("1000", "1"),
            _row("1001", "2"),
            _row("1000", "2"),
        ]
        state = PreviewState(rows=rows)
        # row_idx=0 のデータは常に "1000" 素板行
        self.assertEqual(state.rows[0]["受注No"], "1000")
        self.assertEqual(state.rows[0]["硝/加工"], "1")
        # row_idx=1 は "1001" 加工行
        self.assertEqual(state.rows[1]["受注No"], "1001")
        # row_idx=2 は "1000" 加工行
        self.assertEqual(state.rows[2]["受注No"], "1000")

    def test_set_shiage_via_any_visible_row_still_updates_hidden_rows(self) -> None:
        """フィルタで見えている行（row_idx=2）経由の変更が非表示行（row_idx=0）へ反映される。"""
        rows = [
            _row("1000", "1", shiage="2026-01-01"),  # 仮に非表示（絞り込みで隠れている）
            _row("1001", "2", shiage="2026-06-01"),  # 別受注、表示中
            _row("1000", "2", shiage="2026-01-01"),  # 表示中（row_idx=2 が rowAt() で返される）
        ]
        state = PreviewState(rows=rows)
        # rowAt() は論理 row_idx=2 を返す → set_shiage(2, ...) を呼ぶ
        state.set_shiage(2, "2026-12-31")
        # 非表示の row_idx=0（同じ受注No）にも反映される
        self.assertEqual(state.shiage_by_row[0], "2026-12-31")
        self.assertEqual(state.shiage_by_row[2], "2026-12-31")
        # 別受注 row_idx=1 は変わらない
        self.assertEqual(state.shiage_by_row[1], "2026-06-01")

    def test_context_menu_batch_change_uses_all_order_indices(self) -> None:
        """右クリック一括変更で indices_for_order が非表示行を含む全行を返す。"""
        rows = [
            _row("1000", "1"),  # 表示中（右クリック対象と仮定）
            _row("1001", "2"),  # 別受注
            _row("1000", "2"),  # 非表示（絞り込み）
            _row("1000", "2"),  # 非表示（絞り込み）
        ]
        state = PreviewState(rows=rows)
        # right-click は row_idx=0（表示中の受注No=1000 行）から発火
        indices = state.indices_for_order(0)
        self.assertEqual(set(indices), {0, 2, 3})  # 非表示行 2, 3 も含まれる

        # 一括変更を適用
        state.set_customer_key_for_order(0, "得意先1")
        self.assertEqual(state.customer_key_by_row[0], "得意先1")
        self.assertEqual(state.customer_key_by_row[2], "得意先1")  # 非表示行
        self.assertEqual(state.customer_key_by_row[3], "得意先1")  # 非表示行
        self.assertEqual(state.customer_key_by_row[1], DEFAULT_CUSTOMER_KEY)  # 別受注


class PreviewStateAggregateKakouForPrintTest(unittest.TestCase):
    """印刷用の加工名集約ロジックを検証する。"""

    def test_empty_names_are_excluded(self) -> None:
        """空欄の加工名は集約結果に含まれない。"""
        rows = [
            _row("1000", "1", code="0300"),  # 硝/加工=1 → 空欄
            _row("1000", "2", code="0300"),  # エッチング
        ]
        state = PreviewState(rows=rows)
        result = state.aggregate_kakou_for_print(_MASTER)
        self.assertEqual(result["1000"], "エッチング")
        self.assertNotIn("", result["1000"].split("、"))

    def test_duplicates_are_removed(self) -> None:
        """重複する加工名は1回だけ表示される。"""
        rows = [
            _row("1000", "2", code="0300"),  # エッチング
            _row("1000", "2", code="0300"),  # エッチング（重複）
            _row("1000", "2", code="0400"),  # 広幅
        ]
        state = PreviewState(rows=rows)
        result = state.aggregate_kakou_for_print(_MASTER)
        self.assertEqual(result["1000"], "エッチング、広幅")

    def test_csv_row_order_is_preserved(self) -> None:
        """CSV 行順（挿入順）を維持する（set で順番が崩れないことを確認）。"""
        rows = [
            _row("1000", "2", code="0300", name="エッチング"),  # エッチング（1番目）
            _row("1000", "2", code="0400", name="広幅"),          # 広幅（2番目）
            _row("1000", "2", code="0300", name="エッチング"),  # エッチング（重複→除外）
        ]
        state = PreviewState(rows=rows)
        result = state.aggregate_kakou_for_print(_MASTER)
        # 挿入順: エッチング → 広幅（逆順や set 由来の並び替えではない）
        self.assertEqual(result["1000"], "エッチング、広幅")
        names = result["1000"].split("、")
        self.assertEqual(names[0], "エッチング")
        self.assertEqual(names[1], "広幅")

    def test_full_example_from_spec(self) -> None:
        """仕様例: エッチング → DM-10 → エッチング → 広幅 → 空欄 → エッチング、DM-10、広幅"""
        # DM-10 を追加マスタに含む
        master_with_dm10 = list(_MASTER) + [{
            "掛率集計コード": "DM10", "掛率集計名称": "DM-10", "加工名": "DM-10",
            "得意先1": "", "得意先2": "", "得意先3": "", "得意先4": "",
            "メーカー識別掛率集計コード": "MKDM10", "メーカー識別コード": "MK", "掛率集計略称": "",
        }]
        rows = [
            _row("1000", "2", code="0300", name="エッチング"),   # エッチング
            _row("1000", "2", code="DM10", name="DM-10"),         # DM-10
            _row("1000", "2", code="0300", name="エッチング"),   # エッチング（重複）
            _row("1000", "2", code="0400", name="広幅"),           # 広幅
            _row("1000", "1", code="0300", name="エッチング"),   # 硝/加工=1 → 空欄
        ]
        state = PreviewState(rows=rows)
        result = state.aggregate_kakou_for_print(master_with_dm10)
        self.assertEqual(result["1000"], "エッチング、DM-10、広幅")

    def test_delimiter_is_japanese_comma(self) -> None:
        """区切り文字が「、」（日本語読点）であることを確認。"""
        rows = [
            _row("1000", "2", code="0300"),
            _row("1000", "2", code="0400"),
        ]
        state = PreviewState(rows=rows)
        result = state.aggregate_kakou_for_print(_MASTER)
        self.assertIn("、", result["1000"])
        self.assertNotIn(",", result["1000"])

    def test_all_empty_returns_empty_string(self) -> None:
        """全行が空欄のとき結果は空文字（または未登録）。"""
        rows = [_row("1000", "1", code="0300")]  # 硝/加工=1 → 空欄
        state = PreviewState(rows=rows)
        result = state.aggregate_kakou_for_print(_MASTER)
        self.assertEqual(result.get("1000", ""), "")

    def test_multiple_orders_aggregated_independently(self) -> None:
        """異なる受注Noは独立して集約される。"""
        rows = [
            _row("1000", "2", code="0300"),  # 受注No 1000: エッチング
            _row("1001", "2", code="0400"),  # 受注No 1001: 広幅
            _row("1000", "2", code="0400"),  # 受注No 1000: 広幅
        ]
        state = PreviewState(rows=rows)
        result = state.aggregate_kakou_for_print(_MASTER)
        self.assertEqual(result["1000"], "エッチング、広幅")
        self.assertEqual(result["1001"], "広幅")

    def test_unregistered_code_produces_no_kakou_name(self) -> None:
        """マスタ未登録コードは空欄として扱われ集約結果に含まれない。"""
        rows = [
            _row("1000", "2", code="9999", name="未登録"),  # 未登録 → 空欄
            _row("1000", "2", code="0300", name="エッチング"),  # 登録済み
        ]
        state = PreviewState(rows=rows)
        result = state.aggregate_kakou_for_print(_MASTER)
        self.assertEqual(result["1000"], "エッチング")


class PreviewStateRegistrationRowsCompletenessTest(unittest.TestCase):
    """registration_rows が絞り込みに関係なく全行を返すことを確認する。"""

    def test_all_rows_returned_even_if_logically_filtered(self) -> None:
        """フィルタで一部行が非表示でも build_registration_rows は全行を返す。"""
        rows = [
            _row("1000", "1"),
            _row("1001", "2"),
            _row("1000", "2"),
            _row("1002", "2"),
        ]
        state = PreviewState(rows=rows)
        # フィルタで 1001 だけ表示中と仮定（内部状態は変わらない）
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(len(result), 4)  # フィルタに関係なく全件

    def test_kakou_name_is_per_row_not_per_order(self) -> None:
        """kintone 登録は受注No 単位でまとめず、行ごとの加工名を設定する。"""
        rows = [
            _row("1000", "2", code="0300", name="エッチング"),
            _row("1000", "2", code="0400", name="広幅"),
        ]
        state = PreviewState(rows=rows)
        result = state.build_registration_rows(_MASTER)
        # 同じ受注Noの2行が異なる加工名を持つ
        self.assertEqual(result[0]["加工名"], "エッチング")
        self.assertEqual(result[1]["加工名"], "広幅")
        # 合算された値ではない
        self.assertNotIn("、", result[0]["加工名"])
        self.assertNotIn("、", result[1]["加工名"])

    def test_customer_key_per_order_reflects_all_rows_in_registration(self) -> None:
        """得意先選択を先頭行で変更すると、非先頭行も registration_rows に反映される。"""
        rows = [
            _row("1000", "2", code="0300", name="エッチング"),  # 先頭行
            _row("1000", "2", code="0300", name="エッチング"),  # 2行目（先頭行ウィジェットのみ存在）
        ]
        state = PreviewState(rows=rows)
        # 先頭行 (row_idx=0) から得意先選択を変更
        state.set_customer_key_for_order(0, "得意先1")
        result = state.build_registration_rows(_MASTER)
        # 両行とも同じ得意先選択が適用されている
        self.assertEqual(result[0]["加工名"], "A社向け")
        self.assertEqual(result[1]["加工名"], "A社向け")

    def test_glass_row_is_always_empty_kakou_name(self) -> None:
        """硝/加工=1 の行は加工名が必ず空欄になる（得意先選択によらず）。"""
        rows = [
            _row("1000", "1", code="0300", name="エッチング"),  # 硝/加工=1
            _row("1000", "2", code="0300", name="エッチング"),  # 硝/加工=2
        ]
        state = PreviewState(rows=rows)
        state.set_customer_key_for_order(0, "得意先1")
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(result[0]["加工名"], "")        # 硝/加工=1 → 空欄
        self.assertEqual(result[1]["加工名"], "A社向け")  # 硝/加工=2 → 行ごとに判定

    def test_processing_row_kakou_per_row_different_code(self) -> None:
        """硝/加工=2 の行は掛率集計コードごとに行単位で加工名が決まる。"""
        rows = [
            _row("2000", "2", code="0300", name="エッチング"),
            _row("2000", "2", code="0400", name="広幅"),
        ]
        state = PreviewState(rows=rows)
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(result[0]["加工名"], "エッチング")
        self.assertEqual(result[1]["加工名"], "広幅")


class KakouTypeLabelTest(unittest.TestCase):
    """加工種類コード→表示名の変換を検証する（要件5）。"""

    def test_all_codes_have_labels(self) -> None:
        expected = {
            "1": "1：四方", "2": "2：長2", "3": "3：短2", "4": "4：長2短1",
            "5": "5：長1短2", "6": "6：長1短1", "7": "7：長1", "8": "8：短1",
            "9": "9：1方", "10": "10：2方", "11": "11：3方",
        }
        for code, label in expected.items():
            self.assertEqual(kakou_type_label(code), label)

    def test_invalid_code_returns_empty(self) -> None:
        self.assertEqual(kakou_type_label("12"), "")
        self.assertEqual(kakou_type_label("0"), "")
        self.assertEqual(kakou_type_label(""), "")

    def test_extended_codes_present(self) -> None:
        """9〜11 が KAKOU_TYPE_NAMES に存在する（要件1）。"""
        self.assertEqual(KAKOU_TYPE_NAMES["9"], "1方")
        self.assertEqual(KAKOU_TYPE_NAMES["10"], "2方")
        self.assertEqual(KAKOU_TYPE_NAMES["11"], "3方")


class KakouTypeFromProductNameTest(unittest.TestCase):
    """商品名称から加工種類を判定するロジックを検証する（要件4）。"""

    def test_basic_matches(self) -> None:
        cases = {
            "四方": "1",
            "長2": "2",
            "短2": "3",
            "長1": "7",
            "短1": "8",
            "１方": "9",
            "２方": "10",
            "３方": "11",
        }
        for text, code in cases.items():
            self.assertEqual(kakou_type_from_product_name(f"ガラス {text} 仕上"), code, text)

    def test_combination_matches_before_single_matches(self) -> None:
        """組み合わせ判定が単独判定より先に実行される（誤判定防止）。"""
        cases = {
            "特注 長2 短2 品": "1",
            "特注 長２ 短２ 品": "1",
            "特注 長2短1 品": "4",
            "特注 長２短１ 品": "4",
            "特注 長1短2 品": "5",
            "特注 長１短２ 品": "5",
            "特注 長1短1 品": "6",
        }
        for text, code in cases.items():
            self.assertEqual(kakou_type_from_product_name(text), code, text)

    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(kakou_type_from_product_name("無地ガラス"))
        self.assertIsNone(kakou_type_from_product_name(""))

    def test_halfwidth_houkou(self) -> None:
        """半角 1方/2方/3方 も判定できる。"""
        self.assertEqual(kakou_type_from_product_name("加工 1方"), "9")
        self.assertEqual(kakou_type_from_product_name("加工 2方"), "10")
        self.assertEqual(kakou_type_from_product_name("加工 3方"), "11")

    def test_chouhen_tanpen_priority_over_houkou(self) -> None:
        """長/短付きは 1方/2方/3方 より優先する（要件6）。全角・半角数字とも。"""
        cases = {
            "長2方": "2", "長２方": "2",
            "短2方": "3", "短２方": "3",
            "長1方": "7", "長１方": "7",
            "短1方": "8", "短１方": "8",
        }
        for text, code in cases.items():
            self.assertEqual(kakou_type_from_product_name(f"強化 {text} 磨き"), code, text)

    def test_houkou_alone_still_matches(self) -> None:
        """長/短が付かない ２方 ３方 単独は従来どおり 10/11。"""
        self.assertEqual(kakou_type_from_product_name("加工 ２方"), "10")
        self.assertEqual(kakou_type_from_product_name("加工 ３方"), "11")

    def test_chouhen_tanpen_not_misjudged_as_houkou(self) -> None:
        """長2方 / 短2方 が 10：2方 にならないこと（要件6）。"""
        self.assertNotEqual(kakou_type_from_product_name("長2方"), "10")
        self.assertNotEqual(kakou_type_from_product_name("短2方"), "10")


class ComputeKakouMmFormulaTest(unittest.TestCase):
    """加工mm計算式を検証する（要件7・10）。W=1303, H=1061。"""

    EXPECTED = {
        "1": "4728",
        "2": "2606",
        "3": "2122",
        "4": "3667",
        "5": "3425",
        "6": "2364",
        "7": "1303",
        "8": "1061",
    }

    def test_each_code(self) -> None:
        for code, expected in self.EXPECTED.items():
            self.assertEqual(compute_kakou_mm(code, "1303", "1061"), expected, code)

    def test_w_h_swapped_same_result(self) -> None:
        """W/H が逆でも長辺・短辺判定が正しい。"""
        for code, expected in self.EXPECTED.items():
            self.assertEqual(compute_kakou_mm(code, "1061", "1303"), expected, code)

    def test_blank_dimension_returns_empty(self) -> None:
        self.assertEqual(compute_kakou_mm("1", "", "1061"), "")
        self.assertEqual(compute_kakou_mm("1", "1303", ""), "")

    def test_zero_or_invalid_dimension_returns_empty(self) -> None:
        self.assertEqual(compute_kakou_mm("1", "0", "1061"), "")
        self.assertEqual(compute_kakou_mm("1", "abc", "1061"), "")

    def test_invalid_code_returns_empty(self) -> None:
        self.assertEqual(compute_kakou_mm("12", "1303", "1061"), "")
        self.assertEqual(compute_kakou_mm("0", "1303", "1061"), "")

    def test_width_based_codes_use_w(self) -> None:
        """9〜11 は長辺・短辺ではなく W をそのまま使う（要件2）。"""
        self.assertEqual(compute_kakou_mm("9", "1303", "1061"), "1303")
        self.assertEqual(compute_kakou_mm("10", "1303", "1061"), "2606")
        self.assertEqual(compute_kakou_mm("11", "1303", "1061"), "3909")

    def test_width_based_codes_ignore_h_order(self) -> None:
        """W/H が逆でも 9〜11 は W 基準で計算される（長辺・短辺判定なし）。"""
        # W=1061, H=1303 でも W=1061 をそのまま使う。
        self.assertEqual(compute_kakou_mm("9", "1061", "1303"), "1061")
        self.assertEqual(compute_kakou_mm("10", "1061", "1303"), "2122")
        self.assertEqual(compute_kakou_mm("11", "1061", "1303"), "3183")

    def test_width_based_codes_blank_w_returns_empty(self) -> None:
        """9〜11 でも W が空欄・0・不正なら加工mmは空欄（要件8）。"""
        self.assertEqual(compute_kakou_mm("9", "", "1061"), "")
        self.assertEqual(compute_kakou_mm("10", "0", "1061"), "")
        self.assertEqual(compute_kakou_mm("11", "abc", "1061"), "")

    def test_decimal_dimension(self) -> None:
        """小数寸法も扱える。"""
        self.assertEqual(compute_kakou_mm("7", "1303.5", "1061"), "1303.5")

    def test_comma_dimension(self) -> None:
        """カンマ付き寸法も数値として扱う。"""
        self.assertEqual(compute_kakou_mm("7", "1,303", "1,061"), "1303")


def _row_wh(order_no: str, row_type: str, width: str = "1303", height: str = "1061",
            code: str = "0300", name: str = "エッチング") -> dict[str, str]:
    return {
        "受注No": order_no,
        "硝/加工": row_type,
        "掛率集計コード": code,
        "掛率集計名称": name,
        "W寸法": width,
        "H寸法": height,
    }


class PreviewStateKakouTypeTest(unittest.TestCase):
    """加工種類の状態管理と加工mm反映を検証する（要件4・6・9）。"""

    def test_default_kakou_type_is_one(self) -> None:
        rows = [_row_wh("1000", "2")]
        state = PreviewState(rows=rows)
        self.assertEqual(state.kakou_type_by_row[0], DEFAULT_KAKOU_TYPE)
        self.assertEqual(DEFAULT_KAKOU_TYPE, "1")

    def test_set_kakou_type_is_per_row(self) -> None:
        """加工種類は行ごとに独立して設定される（受注Noで伝播しない）。"""
        rows = [_row_wh("1000", "2"), _row_wh("1000", "2")]
        state = PreviewState(rows=rows)
        state.set_kakou_type(0, "2")
        self.assertEqual(state.kakou_type_by_row[0], "2")
        self.assertEqual(state.kakou_type_by_row[1], DEFAULT_KAKOU_TYPE)

    def test_compute_kakou_mm_default_type(self) -> None:
        rows = [_row_wh("1000", "2")]
        state = PreviewState(rows=rows)
        # 既定 1：四方 → 4728
        self.assertEqual(state.compute_kakou_mm(0), "4728")

    def test_compute_kakou_mm_reflects_type_change(self) -> None:
        """加工種類変更が加工mmに即時反映される（要件9）。"""
        rows = [_row_wh("1000", "2")]
        state = PreviewState(rows=rows)
        state.set_kakou_type(0, "7")
        self.assertEqual(state.compute_kakou_mm(0), "1303")

    def test_glass_row_no_kakou_mm(self) -> None:
        """硝/加工 ≠ 2 の行は加工mmを計算しない（要件6）。"""
        rows = [_row_wh("1000", "1")]
        state = PreviewState(rows=rows)
        self.assertEqual(state.compute_kakou_mm(0), "")

    def test_blank_wh_no_kakou_mm(self) -> None:
        """W/H が空欄の場合は加工mmが不正登録されない（要件8）。"""
        rows = [_row_wh("1000", "2", width="", height="")]
        state = PreviewState(rows=rows)
        self.assertEqual(state.compute_kakou_mm(0), "")

    def test_build_registration_rows_sets_kakou_mm(self) -> None:
        rows = [
            _row_wh("1000", "1"),  # 素板 → 加工mmなし
            _row_wh("1000", "2"),  # 加工 → 既定1：四方 → 4728
        ]
        state = PreviewState(rows=rows)
        state.set_kakou_type(1, "2")  # 長2 → 2606
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(result[0]["加工mm"], "")      # 硝/加工=1
        self.assertEqual(result[1]["加工mm"], "2606")  # 硝/加工=2

    def test_build_registration_rows_glass_row_blank_kakou_mm(self) -> None:
        """硝/加工 ≠ 2 の行に不要な加工mmが入らない。"""
        rows = [_row_wh("1000", "1")]
        state = PreviewState(rows=rows)
        result = state.build_registration_rows(_MASTER)
        self.assertEqual(result[0]["加工mm"], "")


class PreviewStateProductNameDefaultTest(unittest.TestCase):
    """商品名称からの加工種類自動入力を検証する（要件4・5）。"""

    @staticmethod
    def _row(row_type: str, product_name: str) -> dict[str, str]:
        return {
            "受注No": "1000",
            "硝/加工": row_type,
            "商品名称": product_name,
            "掛率集計コード": "0300",
            "掛率集計名称": "エッチング",
            "W寸法": "1303",
            "H寸法": "1061",
        }

    def test_processing_row_auto_detects(self) -> None:
        """硝/加工=2 かつ商品名称に対象文字列があれば加工種類を自動入力する。"""
        cases = {
            "強化 四方 磨き": "1",
            "ジャロジー加工 長2荒・短2糸": "1",
            "ジャロジー加工 長２荒・短２糸": "1",
            "強化 長2短1 磨き": "4",
            "強化 長２短１ 磨き": "4",
            "強化 長1短2 磨き": "5",
            "強化 長１短２ 磨き": "5",
            "強化 長1短1 磨き": "6",
            "強化 長2 磨き": "2",
            "強化 短2 磨き": "3",
            "強化 長1 磨き": "7",
            "強化 短1 磨き": "8",
            "強化 １方 磨き": "9",
            "強化 ３方 磨き": "11",
        }
        for name, code in cases.items():
            state = PreviewState(rows=[self._row("2", name)])
            self.assertEqual(state.kakou_type_by_row[0], code, name)

    def test_processing_row_no_match_defaults_to_one(self) -> None:
        """硝/加工=2 かつ対象文字列なしは 1：四方 を初期値にする。"""
        state = PreviewState(rows=[self._row("2", "無地ガラス")])
        self.assertEqual(state.kakou_type_by_row[0], DEFAULT_KAKOU_TYPE)

    def test_non_processing_row_default(self) -> None:
        """硝/加工 ≠ 2 では商品名称に依存せず既定値（表示・計算では無視される）。"""
        state = PreviewState(rows=[self._row("1", "強化 長2 磨き")])
        # 硝/加工 ≠ 2 の行は加工mm計算対象外（要件5）。
        self.assertEqual(state.compute_kakou_mm(0), "")


if __name__ == "__main__":
    unittest.main()
