from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path

from tks_to_kintone.csv_io import read_csv_dicts, write_quoted_csv
from tks_to_kintone.transform import OUTPUT_HEADERS, transform_files


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
