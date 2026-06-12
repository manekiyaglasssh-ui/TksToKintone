from __future__ import annotations

import argparse
from pathlib import Path

from .olap import extract_olap_csv
from .transform import transform_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tks-to-kintone")
    subparsers = parser.add_subparsers(dest="command", required=True)

    transform_parser = subparsers.add_parser("transform", help="TKS抽出CSVをkintone取込CSVへ変換します")
    transform_parser.add_argument("--glass", required=True, type=Path, help="素板抽出ロジックCSV")
    transform_parser.add_argument("--processing", required=True, type=Path, help="加工抽出ロジックCSV")
    transform_parser.add_argument("--output", required=True, type=Path, help="出力CSV")

    olap_parser = subparsers.add_parser("olap-extract", help="TKS OLAPから受注明細CSVを取得します")
    olap_parser.add_argument("--config", required=True, type=Path, help="TKSログイン設定JSON")
    olap_parser.add_argument("--order-no", required=True, type=Path, help="受注No一覧テキスト")
    olap_parser.add_argument("--output-csv", required=True, type=Path, help="出力CSV")
    olap_parser.add_argument("--output-json", type=Path, help="任意のデバッグJSON出力")

    args = parser.parse_args(argv)
    if args.command == "transform":
        rows = transform_files(args.glass, args.processing, args.output)
        print(f"出力完了: {args.output} ({len(rows)}件)")
        return 0
    if args.command == "olap-extract":
        rows = extract_olap_csv(args.config, args.order_no, args.output_csv, args.output_json)
        print(f"OLAP抽出完了: {args.output_csv} ({len(rows)}件)")
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
