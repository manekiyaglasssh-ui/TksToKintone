from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path
from typing import Sequence

from app.csv_column_settings import CsvColumn, STANDARD_CSV_COLUMNS
from tks_to_kintone.csv_io import read_csv_dicts, write_quoted_csv
from tks_to_kintone.transform import OUTPUT_HEADERS, transform_files

# 登録前確認画面の「CSV作成」で出力する列。
# kintone登録時に送信する OUTPUT_HEADERS に加えて、登録前確認で行ごとに判定・付与する
# 加工名・加工mm・加工種類・得意先選択を末尾に追加し、登録ボタン押下時と同じ最終データを表す。
REGISTRATION_EXPORT_HEADERS = OUTPUT_HEADERS + ["加工名", "加工mm", "加工種類", "得意先選択"]


def backup_existing(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}.bak")
    shutil.move(str(path), str(backup))
    return backup


def write_input_history(path: Path, denpyo_numbers: list[str], shiage_date: str, shukka_kbn: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=["denpyo_no", "shiage_date", "shukka_kbn"])
        writer.writeheader()
        for denpyo_no in denpyo_numbers:
            writer.writerow({"denpyo_no": denpyo_no, "shiage_date": shiage_date, "shukka_kbn": shukka_kbn})


def create_output_csv(
    glass_csv: Path,
    processing_csv: Path,
    output_csv: Path,
    shiage_date: str,
    shukka_kbn: str,
) -> list[dict[str, str]]:
    backup_existing(output_csv)
    return transform_files(glass_csv, processing_csv, output_csv, shiage_date=shiage_date, shukka_kbn=shukka_kbn)


def read_output_rows(output_csv: Path) -> list[dict[str, str]]:
    return read_csv_dicts(output_csv)


def write_failed_csv(path: Path, rows: list[dict[str, str]]) -> None:
    write_quoted_csv(path, OUTPUT_HEADERS, rows)


def write_output_csv(path: Path, rows: list[dict[str, str]]) -> None:
    write_quoted_csv(path, OUTPUT_HEADERS, rows)


def export_registration_records_to_csv(
    rows: list[dict[str, str]],
    output_path: Path,
    columns: Sequence[CsvColumn] | None = None,
) -> Path:
    """登録前確認の登録用データを確認用CSVとして出力する。

    kintoneへは送信せず、登録ボタン押下時に送信される最終データをそのまま書き出す。
    Excelで開きやすいよう UTF-8 BOM付き（utf-8-sig）で出力する。
    columns 未指定時の列は標準順（OUTPUT_HEADERS + 加工名・加工mm・加工種類・得意先選択）。
    空欄もそのまま出力する。
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    export_columns = list(columns) if columns is not None else list(STANDARD_CSV_COLUMNS)
    headers = [column.header for column in export_columns]
    with output_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=headers,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
            lineterminator="\r\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({header: row.get(header, "") for header in headers})
    return output_path


def unique_timestamp_csv_path(output_dir: Path, timestamp: str) -> Path:
    """yyyyMMdd_HHmmss.csv のパスを返す。同名が存在する場合のみ連番を付ける。"""
    output_dir = Path(output_dir)
    candidate = output_dir / f"{timestamp}.csv"
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = output_dir / f"{timestamp}_{index}.csv"
        if not candidate.exists():
            return candidate
        index += 1
