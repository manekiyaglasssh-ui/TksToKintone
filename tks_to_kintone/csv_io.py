from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable


INPUT_ENCODINGS = ("cp932", "utf-8-sig", "utf-8")


def read_text_auto(path: Path) -> str:
    data = path.read_bytes()
    last_error: UnicodeDecodeError | None = None
    for encoding in INPUT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def make_unique_headers(headers: Iterable[str]) -> list[str]:
    seen: dict[str, int] = {}
    unique: list[str] = []
    for header in headers:
        count = seen.get(header, 0)
        seen[header] = count + 1
        unique.append(header if count == 0 else f"{header}_{count}")
    return unique


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    text = read_text_auto(path)
    reader = csv.reader(text.splitlines())
    try:
        headers = make_unique_headers(next(reader))
    except StopIteration:
        return []

    rows: list[dict[str, str]] = []
    for values in reader:
        row = {header: "" for header in headers}
        for header, value in zip(headers, values):
            row[header] = value
        rows.append(row)
    return rows


def write_quoted_csv(path: Path, headers: list[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="cp932", newline="") as fp:
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
