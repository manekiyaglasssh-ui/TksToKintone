"""Kintone既存レコードを登録前確認画面の行データへ突合・反映する共通処理。

実行（OLAP取得・変換・加工名判定）後、登録前確認画面を作る直前に呼び出す。
既にKintone側で登録済み・編集済みの値（仕上日・出荷区分・加工名・加工mm など）を
登録前確認画面へ反映するために使う。
ただし ㎡ / 総㎡ は計算項目のため反映対象外（常に OP区分 から再計算する）。

突合は「検索キー」優先（行単位）。仕上日・出荷区分は受注No単位で、
同一受注No内の最初に値が入っているレコードを採用して全行へ反映する（要件6）。

ここでは TKS/OLAP を正とする基本情報（商品名・数量・寸法など）は上書きしない。
人が確認画面やKintone側で編集する可能性のある項目のみ反映する（要件5）。
"""
from __future__ import annotations

# 受注No単位で反映する項目（先頭行のみ表示・同一受注No全行へ反映）。
# 得意先選択は標準のfield_mappingにkintoneフィールドが無い場合があり、
# 値が取得できなければ自動判定（既定）を保持する。
ORDER_LEVEL_FIELDS = ("仕上日", "出荷区分", "得意先選択")

# 行単位で反映する項目（検索キー一致で対応）。
# 加工名・判定加工名・加工mm はOLAPデータと加工名マスタから再判定する方が安全なため反映しない。
# ㎡ / 総㎡ は計算項目であり、過去の不具合で Kintone側に 1 が残っていると
# 再計算した正しい面積を汚染するため反映しない（常にOLAP再計算を優先）。
ROW_LEVEL_FIELDS = ("加工種類",)


def group_existing_records_by_order(existing_records: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """既存レコードを受注Noごとにまとめる。"""
    grouped: dict[str, list[dict[str, str]]] = {}
    for record in existing_records:
        order_no = str(record.get("受注No", "")).strip()
        if not order_no:
            continue
        grouped.setdefault(order_no, []).append(record)
    return grouped


def _first_non_empty_order_values(records: list[dict[str, str]]) -> dict[str, str]:
    """同一受注No内で最初に値が入っているレコードの受注No単位項目を返す（要件6）。"""
    values: dict[str, str] = {}
    for field_name in ORDER_LEVEL_FIELDS:
        for record in records:
            value = str(record.get(field_name, "")).strip()
            if value:
                values[field_name] = value
                break
    return values


def merge_existing_kintone_records_into_preview_rows(
    preview_rows: list[dict[str, str]],
    existing_records: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """既存Kintoneレコードを登録前確認の行へ突合・反映する。

    Returns:
        (merged_rows, existing_by_row)
        - merged_rows: 受注No単位項目（仕上日・出荷区分・得意先選択）を反映した行リスト
          （OLAP由来の他項目は不変）。
        - existing_by_row: 各行に対応する既存Kintone行の行単位反映値（検索キー一致、無ければ {}）。
          反映するのは加工種類のみ。PreviewState 側で「Kintoneに値があればそれを優先」するために使う。
          加工名・加工mm・㎡・総㎡は反映せず、常にOLAP/マスタから再判定・再計算する。
    """
    existing_by_key: dict[str, dict[str, str]] = {}
    for record in existing_records:
        key = str(record.get("検索キー", "")).strip()
        if key:
            existing_by_key.setdefault(key, record)

    grouped = group_existing_records_by_order(existing_records)
    order_values = {order_no: _first_non_empty_order_values(records) for order_no, records in grouped.items()}

    merged_rows: list[dict[str, str]] = []
    existing_by_row: list[dict[str, str]] = []
    for row in preview_rows:
        new_row = dict(row)
        order_no = str(row.get("受注No", "")).strip()
        for field_name, value in order_values.get(order_no, {}).items():
            new_row[field_name] = value
        merged_rows.append(new_row)

        key = str(row.get("検索キー", "")).strip()
        matched = existing_by_key.get(key) if key else None
        existing_by_row.append(_row_level_overrides(matched) if matched else {})

    return merged_rows, existing_by_row


def _row_level_overrides(record: dict[str, str]) -> dict[str, str]:
    """既存レコードから行単位反映項目のうち非空のものだけを抜き出す。"""
    overrides: dict[str, str] = {}
    for field_name in ROW_LEVEL_FIELDS:
        value = str(record.get(field_name, "")).strip()
        if value:
            overrides[field_name] = value
    return overrides


def summarize_existing_reflection(existing_records: list[dict[str, str]]) -> str:
    """反映結果の表示用メッセージを返す。既存データが無ければ空文字。"""
    grouped = group_existing_records_by_order(existing_records)
    if not grouped:
        return ""
    if len(grouped) == 1:
        order_no, records = next(iter(grouped.items()))
        return f"Kintone既存データを反映しました：{order_no}（{len(records)}件）"
    total = sum(len(records) for records in grouped.values())
    return f"Kintone既存データを反映しました：{len(grouped)}件の受注No、{total}レコード"
