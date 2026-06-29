"""登録前確認画面の状態管理 — Qt 不依存の純粋データモデル。

RegistrationPreviewDialog はこのクラスを内部で保持する。
Qt ウィジェット側の変更は本クラスのメソッドを経由してデータを更新し、
registration_rows() は本クラスの build_registration_rows() から結果を得る。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.area_calculator import apply_area_values
from app.kakou_master import CUSTOMER_KEYS, get_kakou_name, lookup

PROCESSING_TYPE = "2"
DEFAULT_CUSTOMER_KEY = "selected"

# 加工種類コード -> 表示名（要件5）。硝/加工 = '2' の行のみ意味を持つ。
KAKOU_TYPE_NAMES: dict[str, str] = {
    "1": "四方",
    "2": "長2",
    "3": "短2",
    "4": "長2短1",
    "5": "長1短2",
    "6": "長1短1",
    "7": "長1",
    "8": "短1",
    "9": "1方",
    "10": "2方",
    "11": "3方",
}
DEFAULT_KAKOU_TYPE = "1"
KAKOU_TYPE_CODES = tuple(KAKOU_TYPE_NAMES.keys())

# 9〜11 は W をそのまま使う倍率（長辺・短辺判定を行わない。要件2）。
WIDTH_BASED_KAKOU_FORMULAS: dict[str, int] = {
    "9": 1,
    "10": 2,
    "11": 3,
}

# 商品名称から加工種類を判定するルール（要件4・6）。
# 判定前に商品名称を正規化（全角数字→半角・空白除去）してから部分一致する。
# 部分一致の誤判定を防ぐため、組み合わせ・長短指定を先に並べる（順序が重要）。
# 「長2方」「短2方」等は 1方/2方/3方 より先に長短指定を優先する（要件6）。
# 各タプルは (正規化後に判定する候補文字列の並び, 加工種類コード)。
PRODUCT_NAME_KAKOU_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("長2", "短2"), "1"),
    (("長2", "短1"), "4"),
    (("長1", "短2"), "5"),
    (("長1", "短1"), "6"),
    (("四方",), "1"),
    (("長2",), "2"),
    (("短2",), "3"),
    (("長1",), "7"),
    (("短1",), "8"),
    (("1方",), "9"),
    (("2方",), "10"),
    (("3方",), "11"),
)

# 全角数字 → 半角数字の変換テーブル（商品名称正規化用）。
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _leading_code(value: str) -> str:
    """「2：長2」「2」などの先頭の加工種類コード（数字）を取り出す。"""
    text = str(value or "").strip()
    digits = ""
    for char in text:
        if char.isdigit():
            digits += char
        else:
            break
    return digits


def _normalize_product_name(product_name: str) -> str:
    """商品名称を判定用に正規化する（要件6）。

    全角数字を半角化し、半角・全角空白を除去する。これにより
    「長２方」「長2 方」なども「長2方」として長短指定を優先判定できる。
    """
    text = str(product_name or "").translate(_FULLWIDTH_DIGITS)
    return text.replace(" ", "").replace("　", "")


def kakou_type_label(code: str) -> str:
    """加工種類コードを「1：四方」形式の表示文字列に変換する。未知コードは空文字。"""
    code = str(code).strip()
    name = KAKOU_TYPE_NAMES.get(code)
    return f"{code}：{name}" if name else ""


def kakou_type_from_product_name(product_name: str) -> str | None:
    """商品名称に含まれる文字列から加工種類コードを判定する（要件4・6）。

    商品名称を正規化したうえで、長い文字列・長短指定を先に判定し、
    部分一致による誤判定（例: 長2短1 → 長2、長2方 → 2方）を防ぐ。
    対象文字列が含まれない場合は None を返す。
    """
    text = _normalize_product_name(product_name)
    for keywords, code in PRODUCT_NAME_KAKOU_RULES:
        if all(keyword in text for keyword in keywords):
            return code
    return None


def _to_dimension(value: str) -> Decimal | None:
    """W/H 寸法文字列を Decimal に変換する。空欄・0・不正値は None。"""
    text = str(value or "").strip().replace(",", "")
    if text in {"", "-"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if number <= 0:
        return None
    return number


def _format_mm(number: Decimal) -> str:
    """加工mm 値を文字列化する。整数は小数点なし、小数は末尾0を除去。"""
    normalized = number.normalize()
    text = format(normalized, "f")
    return "0" if text == "-0" else text


def compute_kakou_mm(kakou_type: str, width: str, height: str) -> str:
    """加工種類コードと W/H から加工mmを計算して文字列で返す（要件7）。

    1〜8 は W/H の大きい方を長辺、小さい方を短辺として計算する。
    9〜11 は長辺・短辺判定を行わず、データ上の W をそのまま使う（要件2）。
    空欄・0・不正値の W/H、未知コードの場合は空文字を返す（不正登録防止）。
    """
    code = str(kakou_type).strip()
    w = _to_dimension(width)
    # 9〜11 は W のみを使用する（H が逆でも W 基準で計算）。
    if code in WIDTH_BASED_KAKOU_FORMULAS:
        if w is None:
            return ""
        return _format_mm(w * WIDTH_BASED_KAKOU_FORMULAS[code])
    h = _to_dimension(height)
    if w is None or h is None:
        return ""
    long_side = max(w, h)
    short_side = min(w, h)
    formulas = {
        "1": long_side * 2 + short_side * 2,
        "2": long_side * 2,
        "3": short_side * 2,
        "4": long_side * 2 + short_side,
        "5": long_side + short_side * 2,
        "6": long_side + short_side,
        "7": long_side,
        "8": short_side,
    }
    if code not in formulas:
        return ""
    return _format_mm(formulas[code])


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
    kakou_type_by_row: list[str] = field(default_factory=list)
    # Kintone既存データの行単位反映値（行インデックス→{CSV列名: 値}）。
    # 加工種類のみKintone値で優先表示・登録するために使う。
    # 加工名・加工mm・㎡・総㎡はKintone値を使わず常に再判定・再計算する。
    kintone_existing_by_row: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.kintone_existing_by_row:
            self.kintone_existing_by_row = [{} for _ in self.rows]
        if not self.shiage_by_row:
            self.shiage_by_row = [row.get("仕上日", "") for row in self.rows]
        if not self.shukka_by_row:
            self.shukka_by_row = [row.get("出荷区分", "") for row in self.rows]
        if not self.customer_key_by_row:
            # Kintone既存の得意先選択（受注No単位でmerged行へ反映済み）があれば初期値に使う。
            self.customer_key_by_row = [self._initial_customer_key(row) for row in self.rows]
        if not self.kakou_type_by_row:
            # 既定加工種類（要件4・5）。硝/加工 = '2' の行のみ意味を持つ。
            # 商品名称に対象文字列があればその加工種類を、無ければ 1：四方 を初期値にする。
            # Kintoneに既存の加工種類があればそれを初期値として優先する。
            self.kakou_type_by_row = [
                self._initial_kakou_type(i, row) for i, row in enumerate(self.rows)
            ]

    @staticmethod
    def _initial_customer_key(row: dict[str, str]) -> str:
        """得意先選択の初期値。Kintone既存値が既知のキーなら採用、無ければ既定。"""
        value = str(row.get("得意先選択", "")).strip()
        if value in CUSTOMER_KEYS or value == DEFAULT_CUSTOMER_KEY:
            return value
        return DEFAULT_CUSTOMER_KEY

    def _existing(self, row_idx: int) -> dict[str, str]:
        """行のKintone既存反映値を返す（無ければ空dict）。"""
        if 0 <= row_idx < len(self.kintone_existing_by_row):
            return self.kintone_existing_by_row[row_idx]
        return {}

    def _initial_kakou_type(self, row_idx: int, row: dict[str, str]) -> str:
        """加工種類の初期値。Kintone既存値があれば優先、無ければ自動判定。"""
        if row.get("硝/加工") != PROCESSING_TYPE:
            return DEFAULT_KAKOU_TYPE
        existing = self._existing(row_idx).get("加工種類", "").strip()
        if existing:
            code = _leading_code(existing)
            if code in KAKOU_TYPE_CODES:
                return code
        return self._default_kakou_type(row)

    @staticmethod
    def _default_kakou_type(row: dict[str, str]) -> str:
        """1行の加工種類初期値を決める（要件5）。

        硝/加工 = '2' かつ商品名称に対象文字列がある場合のみ自動判定し、
        それ以外は既定 1：四方 とする（硝/加工 ≠ '2' の値は表示・計算で無視される）。
        """
        if row.get("硝/加工") != PROCESSING_TYPE:
            return DEFAULT_KAKOU_TYPE
        return kakou_type_from_product_name(row.get("商品名称", "")) or DEFAULT_KAKOU_TYPE

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

    # ── 加工種類 ──────────────────────────────────────────

    def set_kakou_type(self, row_idx: int, code: str) -> None:
        """1行の加工種類コードを設定する（行ごとに独立。要件4）。"""
        self.kakou_type_by_row[row_idx] = str(code).strip()

    def compute_kakou_mm(self, row_idx: int) -> str:
        """1行の加工mmを返す。硝/加工 ≠ '2' は空文字（要件6・7）。

        加工mmはKintone既存値を使わず、常に加工種類とW/Hから再計算する。
        """
        row = self.rows[row_idx]
        if row.get("硝/加工") != PROCESSING_TYPE:
            return ""
        return compute_kakou_mm(
            self.kakou_type_by_row[row_idx],
            row.get("W寸法", ""),
            row.get("H寸法", ""),
        )

    # ── 判定加工名 ────────────────────────────────────────

    def compute_kakou_name(self, row_idx: int, master: list[dict[str, str]]) -> str:
        """1行の判定加工名を返す。硝/加工 ≠ '2' またはマスタ未登録は空文字。

        加工名はKintone既存値を使わず、常にOLAPデータと加工名マスタから再判定する
        （古い不具合データでの再汚染を避けるため）。
        """
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
            # 硝/加工 = '2' の行のみ加工mmを計算。それ以外は空欄（不正登録防止。要件6）。
            new_row["加工mm"] = self.compute_kakou_mm(i)
            # 加工種類コードも登録データへ含める。硝/加工 ≠ '2' は意味を持たないため空欄。
            # （加工mm の算出根拠となる値で、CSV出力で登録時と同じ加工種類を確認できるようにする。）
            new_row["加工種類"] = (
                self.kakou_type_by_row[i] if row.get("硝/加工") == PROCESSING_TYPE else ""
            )
            # 得意先選択は受注No単位の選択値。kintone登録・CSV出力に含める。
            # 既定（selected=選択なし）はkintoneの選択肢に無いため空欄として送る（不正登録防止）。
            customer_key = self.customer_key_by_row[i]
            new_row["得意先選択"] = customer_key if customer_key in CUSTOMER_KEYS else ""
            # ㎡ / 総㎡ は計算項目。Kintone既存値（過去の不具合で 1 が残っている可能性あり）は
            # 反映せず、必ず OP区分から再計算した値を最終値とする。
            # Kintone既存データ反映より後（最後）に実行することで上書き汚染を防ぐ。
            apply_area_values(new_row)
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
