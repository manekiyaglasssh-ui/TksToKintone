from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path

IMPORT_ENCODINGS = ["utf-8-sig", "utf-8", "cp932", "shift_jis"]

KAKOU_MASTER_HEADERS = [
    "メーカー識別掛率集計コード",
    "メーカー識別コード",
    "掛率集計コード",
    "掛率集計名称",
    "掛率集計略称",
    "加工名",
    "得意先1",
    "得意先2",
    "得意先3",
    "得意先4",
]

CUSTOMER_KEYS = ["得意先1", "得意先2", "得意先3", "得意先4"]
DEFAULT_MAKER_CODE = "MK"


class CsvEncodingError(ValueError):
    """サポートされているどの文字コードでもCSVを読み込めなかった場合の例外。"""

    def __init__(self, tried_encodings: list[str]) -> None:
        self.tried_encodings = tried_encodings
        enc_list = ", ".join(tried_encodings)
        super().__init__(f"CSVの読み込みに失敗しました。\n対応文字コード:\n{enc_list}")


def read_csv_with_auto_encoding(path: Path) -> tuple[list[dict[str, str]], str]:
    """
    IMPORT_ENCODINGS の順に文字コードを試してCSVを読み込む。
    戻り値: (DictReader行リスト, 成功した文字コード)
    すべて失敗した場合は CsvEncodingError を raise する。
    """
    for encoding in IMPORT_ENCODINGS:
        try:
            with path.open("r", encoding=encoding, newline="") as fp:
                reader = csv.DictReader(fp)
                rows = [dict(row) for row in reader]
            return rows, encoding
        except (UnicodeDecodeError, LookupError):
            continue
    raise CsvEncodingError(IMPORT_ENCODINGS)


def load_master(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        return [{h: str(row.get(h, "") or "") for h in KAKOU_MASTER_HEADERS} for row in reader]


def save_master(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=KAKOU_MASTER_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in KAKOU_MASTER_HEADERS})


def backup_master(path: Path, backup_dir: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = backup_dir / f"kakou_master_{timestamp}.csv.bak"
    shutil.copy2(str(path), str(backup))
    return backup


def restore_master(backup_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(backup_path), str(target_path))


def ensure_master_file(path: Path) -> None:
    """マスタCSVが存在しない場合、ヘッダー付き空CSVを作成する。"""
    if path.exists():
        return
    save_master(path, [])


def load_default_master(default_csv_path: Path) -> list[dict[str, str]]:
    """同梱のデフォルト加工名マスタCSVを読み込む（UTF-8 BOM等を自動判定）。

    ファイルが無い場合は空リストを返す。
    """
    if not default_csv_path.exists():
        return []
    rows, _encoding = read_csv_with_auto_encoding(default_csv_path)
    return [{h: str(row.get(h, "") or "") for h in KAKOU_MASTER_HEADERS} for row in rows]


def ensure_default_kakou_master(path: Path, default_csv_path: Path) -> bool:
    """初回起動時のみ、同梱デフォルトCSVを加工名マスタへ投入する。

    投入するのは「マスタファイルが存在しない、または0件の場合」だけ。
    既にユーザーがマスタを登録・編集済み（1件以上データがある）なら一切上書きしない。

    戻り値: デフォルトを投入したら True。
    """
    if path.exists() and load_master(path):
        # 1件以上データがある＝ユーザー登録済みとみなして触らない。
        return False
    rows = load_default_master(default_csv_path)
    if not rows:
        # デフォルトCSVが無い/空なら従来どおりヘッダーのみの空マスタを用意する。
        ensure_master_file(path)
        return False
    save_master(path, rows)
    return True


def apply_kakou_names_to_rows(
    rows: list[dict[str, str]],
    master: list[dict[str, str]],
    column_key: str = "selected",
) -> None:
    """全行に受注No単位の加工名を設定する（旧方式、後方互換のため保持）。"""
    rows_by_order: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        rows_by_order.setdefault(row.get("受注No", ""), []).append(row)
    for order_no, order_rows in rows_by_order.items():
        kakou_name = compute_kakou_name_for_order(order_rows, master, column_key)
        for row in order_rows:
            row["加工名"] = kakou_name


def apply_kakou_names_per_row(
    rows: list[dict[str, str]],
    master: list[dict[str, str]],
    column_key: str = "selected",
    processing_type: str = "2",
) -> None:
    """各行に行単位の加工名を設定する。
    硝/加工 = processing_type の行のみマスタを参照し、それ以外は空欄にする。
    """
    for row in rows:
        if row.get("硝/加工") == processing_type:
            code = row.get("掛率集計コード", "").strip()
            name = row.get("掛率集計名称", "").strip()
            master_row = _lookup_by_code(master, code) if code else _lookup_by_name(master, name) if name else None
            row["加工名"] = get_kakou_name(master_row, column_key)
        else:
            row["加工名"] = ""


def _lookup_by_code(master: list[dict[str, str]], code: str) -> dict[str, str] | None:
    for row in master:
        if row.get("掛率集計コード", "").strip() == code.strip():
            return row
    return None


def _lookup_by_name(master: list[dict[str, str]], name: str) -> dict[str, str] | None:
    for row in master:
        if row.get("掛率集計名称", "").strip() == name.strip():
            return row
    return None


def lookup(master: list[dict[str, str]], code: str, name: str) -> dict[str, str] | None:
    """コードが空でなければコードで検索、空なら名称でフォールバック。"""
    if code.strip():
        return _lookup_by_code(master, code)
    if name.strip():
        return _lookup_by_name(master, name)
    return None


def get_kakou_name(master_row: dict[str, str] | None, column_key: str) -> str:
    """
    マスタ行から加工名を取得する。
    column_key: "selected" (加工名列) or "得意先1"〜"得意先4"
    得意先列が空の場合は「加工名」にフォールバック。
    """
    if master_row is None:
        return ""
    if column_key == "selected":
        return master_row.get("加工名", "").strip()
    value = master_row.get(column_key, "").strip()
    return value or master_row.get("加工名", "").strip()


def compute_kakou_name_for_order(
    rows_for_order: list[dict[str, str]],
    master: list[dict[str, str]],
    column_key: str,
) -> str:
    """1受注Noの行から加工名を収集し、重複除去して「、」結合する。"""
    names: list[str] = []
    seen: set[str] = set()
    for row in rows_for_order:
        code = row.get("掛率集計コード", "").strip()
        name = row.get("掛率集計名称", "").strip()
        master_row = lookup(master, code, name)
        value = get_kakou_name(master_row, column_key)
        if value and value not in seen:
            seen.add(value)
            names.append(value)
    return "、".join(names)


def find_unregistered(
    rows_for_order: list[dict[str, str]],
    master: list[dict[str, str]],
) -> list[str]:
    """マスタに存在しない掛率集計コード/名称の警告テキスト一覧を返す。"""
    warnings: list[str] = []
    seen: set[str] = set()
    for row in rows_for_order:
        code = row.get("掛率集計コード", "").strip()
        name = row.get("掛率集計名称", "").strip()
        if not code and not name:
            continue
        master_row = lookup(master, code, name)
        if master_row is None:
            if code:
                key = f"MK{code}"
                display = f"{key} / {name}" if name else key
            else:
                key = name
                display = name
            if key and key not in seen:
                seen.add(key)
                warnings.append(display)
    return warnings
