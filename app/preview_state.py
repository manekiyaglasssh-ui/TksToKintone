"""登録前確認画面の状態管理 — Qt 不依存の純粋データモデル。

RegistrationPreviewDialog はこのクラスを内部で保持する。
Qt ウィジェット側の変更は本クラスのメソッドを経由してデータを更新し、
registration_rows() は本クラスの build_registration_rows() から結果を得る。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.kakou_master import get_kakou_name, lookup

PROCESSING_TYPE = "2"
DEFAULT_CUSTOMER_KEY = "selected"


@dataclass
class PreviewState:
    """登録前確認ダイアログの行データを保持するデータモデル。

    rows           : 元の CSV 行リスト（変更しない）
    shiage_by_row  : 仕上日（行インデックス→文字列）
    shukka_by_row  : 出荷区分（行インデックス→文字列）
    customer_key_by_row: 得意先選択キー（行インデックス→キー文字列）
    """

    rows: list[dict[str, str]]
    shiage_by_row: list[str] = field(default_factory=list)
    shukka_by_row: list[str] = field(default_factory=list)
    customer_key_by_row: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.shiage_by_row:
            self.shiage_by_row = [row.get("仕上日", "") for row in self.rows]
        if not self.shukka_by_row:
            self.shukka_by_row = [row.get("出荷区分", "") for row in self.rows]
        if not self.customer_key_by_row:
            self.customer_key_by_row = [DEFAULT_CUSTOMER_KEY] * len(self.rows)

    # ── 仕上日 ────────────────────────────────────────────

    def set_shiage(self, row_idx: int, new_date: str) -> None:
        """同一受注No の全行（非表示行を含む）に仕上日を設定する。"""
        order_no = self.rows[row_idx].get("受注No", "")
        for i, row in enumerate(self.rows):
            if row.get("受注No", "") == order_no:
                self.shiage_by_row[i] = new_date

    # ── 出荷区分 ──────────────────────────────────────────

    def set_shukka(self, row_idx: int, new_shukka: str) -> None:
        """同一受注No の全行（非表示行を含む）に出荷区分を設定する。"""
        order_no = self.rows[row_idx].get("受注No", "")
        for i, row in enumerate(self.rows):
            if row.get("受注No", "") == order_no:
                self.shukka_by_row[i] = new_shukka

    # ── 得意先選択 ────────────────────────────────────────

    def set_customer_key(self, row_idx: int, new_key: str) -> None:
        """1行の得意先選択を設定する。"""
        self.customer_key_by_row[row_idx] = new_key

    def set_customer_key_for_order(self, row_idx: int, new_key: str) -> None:
        """同一受注No の全行（非表示行を含む）に得意先選択を設定する。"""
        order_no = self.rows[row_idx].get("受注No", "")
        for i, row in enumerate(self.rows):
            if row.get("受注No", "") == order_no:
                self.customer_key_by_row[i] = new_key

    # ── 判定加工名 ────────────────────────────────────────

    def compute_kakou_name(self, row_idx: int, master: list[dict[str, str]]) -> str:
        """1行の判定加工名を返す。硝/加工 ≠ '2' またはマスタ未登録は空文字。"""
        row = self.rows[row_idx]
        if row.get("硝/加工") != PROCESSING_TYPE:
            return ""
        code = row.get("掛率集計コード", "").strip()
        name = row.get("掛率集計名称", "").strip()
        master_row = lookup(master, code, name)
        return get_kakou_name(master_row, self.customer_key_by_row[row_idx])

    # ── 登録用データ生成 ──────────────────────────────────

    def build_registration_rows(self, master: list[dict[str, str]]) -> list[dict[str, str]]:
        """全CSVレコードの登録用データを返す。

        絞り込み表示状態に関係なく、内部保持している全行を対象とする。
        各行に 仕上日・出荷区分・加工名 を設定して返す。
        """
        result = []
        for i, row in enumerate(self.rows):
            new_row = dict(row)
            new_row["仕上日"] = self.shiage_by_row[i]
            new_row["出荷区分"] = self.shukka_by_row[i]
            new_row["加工名"] = self.compute_kakou_name(i, master)
            result.append(new_row)
        return result

    # ── 同一受注No インデックス一覧 ───────────────────────

    def indices_for_order(self, row_idx: int) -> list[int]:
        """指定行と同じ受注No を持つ全行のインデックスリストを返す。"""
        order_no = self.rows[row_idx].get("受注No", "")
        return [i for i, row in enumerate(self.rows) if row.get("受注No", "") == order_no]

    def first_indices_by_order(self) -> set[int]:
        """各受注Noの先頭行インデックスの集合を返す（内部データ順基準）。"""
        seen: set[str] = set()
        result: set[int] = set()
        for i, row in enumerate(self.rows):
            order_no = row.get("受注No", "")
            if order_no not in seen:
                seen.add(order_no)
                result.add(i)
        return result

    def order_group_index(self) -> list[int]:
        """各行の受注Noグループインデックス（0始まり）のリストを返す。

        同じ受注Noは同じグループインデックスを持つ。
        グループインデックスは受注Noの初出現順に付与される。
        """
        order_to_group: dict[str, int] = {}
        result: list[int] = []
        for row in self.rows:
            order_no = row.get("受注No", "")
            if order_no not in order_to_group:
                order_to_group[order_no] = len(order_to_group)
            result.append(order_to_group[order_no])
        return result

    # ── 印刷用集約 ────────────────────────────────────────

    def aggregate_kakou_for_print(self, master: list[dict[str, str]]) -> dict[str, str]:
        """印刷用に受注No 単位で加工名を集約して返す。

        仕様:
        - 空欄の加工名を除外する
        - 重複する加工名は1回だけ表示する（set ではなくリストで順序維持）
        - 表示順は CSV 行順を維持する
        - 区切りは「、」

        例: エッチング → DM-10 → エッチング → 広幅 → 空欄
            → "エッチング、DM-10、広幅"
        """
        names_by_order: dict[str, list[str]] = {}
        for i, row in enumerate(self.rows):
            order_no = row.get("受注No", "")
            name = self.compute_kakou_name(i, master)
            if not name:
                continue
            if order_no not in names_by_order:
                names_by_order[order_no] = []
            if name not in names_by_order[order_no]:
                names_by_order[order_no].append(name)
        return {order_no: "、".join(names) for order_no, names in names_by_order.items()}
