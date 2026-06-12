from __future__ import annotations

import csv
import json
from collections import OrderedDict
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .csv_io import write_quoted_csv


OLAP_TARGET = "OLAP_T01-03 受注入力明細データ"
OLAP_CSV_HEADERS = [
    "受注No",
    "受注行No",
    "得意先コード",
    "得意先名称",
    "納品書No",
    "納品書行No",
    "納品日",
    "売上日",
    "発注日",
    "入庫日",
]


@dataclass(frozen=True)
class TksConfig:
    base_url: str
    contract_company_code: str
    login_id: str
    password: str
    login_auth_type: str
    device_id: str
    computer_name: str
    ip_address: str

    @classmethod
    def from_json(cls, path: Path) -> "TksConfig":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return cls(
            base_url=str(data["BaseUrl"]).rstrip("/"),
            contract_company_code=str(data["ContractCompanyCode"]),
            login_id=str(data["LoginId"]),
            password=str(data["Password"]),
            login_auth_type=str(data["LoginAuthType"]),
            device_id=str(data["DeviceId"]),
            computer_name=str(data["ComputerName"]),
            ip_address=str(data["IpAddress"]),
        )


class TksOlapClient:
    def __init__(self, config: TksConfig) -> None:
        self.config = config
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def login(self) -> dict:
        body = OrderedDict(
            [
                ("契約会社コード", self.config.contract_company_code),
                ("ログインID", self.config.login_id),
                ("パスワード", self.config.password),
                ("ログイン認証区分", self.config.login_auth_type),
                ("端末識別ID", self.config.device_id),
                ("コンピュータ名", self.config.computer_name),
                ("IPアドレス", self.config.ip_address),
                ("ScreenName", 0),
            ]
        )
        response = self._request_json("POST", "/c/ログイン認証", body)
        response_data = response.get("ResponseData")
        if not response_data:
            raise RuntimeError("ログインに失敗しました。ResponseData がありません。")
        if response_data.get("Ｘ0") != "00":
            raise RuntimeError("ログインに失敗しました。X0 が 00 ではありません。")
        if not any(cookie.name == ".ASPXAUTH" for cookie in self.cookies):
            raise RuntimeError("ログインCookie .ASPXAUTH が取得できていません。")
        return response

    def fetch_order_rows(self, order_numbers: list[str]) -> list[dict[str, str]]:
        if not order_numbers:
            raise ValueError("受注Noが指定されていません。")
        body = build_olap_body(",".join(order_numbers))
        response = self._request_json("PUT", "/c/OLAPデータ", body)
        response_data = response.get("ResponseData")
        if response_data is None:
            raise RuntimeError("OLAPデータ抽出に失敗しました。ResponseData がありません。")
        return parse_olap_rows(response_data.get("R1List"))

    def _request_json(self, method: str, path: str, body: object) -> dict:
        url = self.config.base_url + quote(path, safe="/")
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            url,
            data=payload,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with self.opener.open(request) as response:
                content = response.read().decode("utf-8-sig")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"TKS API request failed: HTTP {exc.code}: {detail}") from exc
        return json.loads(content)


def extract_olap_csv(config_path: Path, order_no_path: Path, output_csv: Path, output_json: Path | None = None) -> list[dict[str, str]]:
    order_numbers = read_order_numbers(order_no_path)
    client = TksOlapClient(TksConfig.from_json(config_path))
    client.login()
    rows = client.fetch_order_rows(order_numbers)
    write_quoted_csv(output_csv, OLAP_CSV_HEADERS, rows)
    if output_json is not None:
        output_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def read_order_numbers(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def parse_olap_rows(rows_object: object) -> list[dict[str, str]]:
    if not isinstance(rows_object, dict):
        return []
    rows: list[dict[str, str]] = []
    for key in sorted(rows_object, key=lambda value: int(value)):
        row = rows_object[key]
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "受注No": str(row.get("1", "")),
                "受注行No": str(row.get("2", "")),
                "得意先コード": str(row.get("3", "")),
                "得意先名称": str(row.get("4", "")),
                "納品書No": str(row.get("5", "")),
                "納品書行No": str(row.get("6", "")),
                "納品日": str(row.get("7", "")),
                "売上日": str(row.get("8", "")),
                "発注日": str(row.get("9", "")),
                "入庫日": str(row.get("10", "")),
            }
        )
    return rows


def write_olap_rows_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, OLAP_CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def build_olap_body(order_no_value: str) -> OrderedDict:
    return OrderedDict(
        [
            ("OLAP出力レイアウト", "0"),
            ("OLAP対象データ", OLAP_TARGET),
            ("R1List", [_olap_column(*args) for args in _olap_columns()]),
            (
                "R2List",
                [
                    _olap_condition(1, "営業所コード", "010,040", "0"),
                    _olap_condition(2, "納品書発行区分", "1", "3"),
                    _olap_condition(3, "受注No", order_no_value, "0"),
                ],
            ),
            ("ScreenName", 0),
        ]
    )


def _olap_columns() -> list[tuple[int, str, str, str, int, int, int, str, str, str]]:
    return [
        (1, "受注No", "受注No", "1", 8, 0, 0, "0", "", "0"),
        (2, "受注行No", "受注行No", "2", 4, 3, 0, "3", "1", "1"),
        (3, "得意先コード", "得意先コード", "1", 7, 0, 0, "0", "", "0"),
        (4, "得意先名称", "得意先名称", "1", 30, 0, 0, "0", "", "0"),
        (5, "納品書No", "納品書No", "1", 8, 0, 0, "0", "", "0"),
        (6, "納品書行No", "納品書行No", "2", 4, 3, 0, "3", "2", "1"),
        (7, "納品日", "納品日", "1", 10, 0, 0, "0", "", "2"),
        (8, "売上日", "売上計上日", "1", 10, 0, 0, "0", "", "2"),
        (9, "発注日", "発注日", "1", 10, 0, 0, "0", "", "2"),
        (10, "入庫日", "入庫日", "1", 10, 0, 0, "0", "", "2"),
    ]


def _olap_column(
    no: int,
    display_name: str,
    field_name: str,
    data_type: str,
    width: int,
    digits: int,
    decimals: int,
    summary_method: str,
    formula_text: str,
    domain_type: str,
) -> OrderedDict:
    return OrderedDict(
        [
            ("OLAP表示No", no),
            ("OLAP表示名", display_name),
            ("OLAPデータ区分", data_type),
            ("エンティティ論理名", OLAP_TARGET),
            ("フィールド論理名", field_name),
            ("OLAP表示幅", width),
            ("OLAPフォントサイズ２", "0"),
            ("OLAP空白値表示", "-"),
            ("OLAP日付のフォーマットフラグ", "1"),
            ("OLAP数値の3桁区切りフラグ", "1"),
            ("OLAP桁数", digits),
            ("OLAP小数", decimals),
            ("OLAP丸め", "0"),
            ("OLAP出力順序No", None),
            ("OLAP出力順", "2"),
            ("OLAP空白値を先頭表示フラグ", "0"),
            ("OLAP集計方法", summary_method),
            ("OLAP合計表示フラグ", "0"),
            ("OLAP合計ラベル", "計"),
            ("OLAP合計ラベルのみ表示フラグ", None),
            ("OLAP重複を除くフラグ", "0"),
            ("OLAP演算式", None),
            ("OLAP演算式表記", formula_text),
            ("OLAPドメイン分類", domain_type),
            ("XupperRoutingItems", []),
        ]
    )


def _olap_condition(no: int, field_name: str, value: str, domain_type: str) -> OrderedDict:
    return OrderedDict(
        [
            ("OLAP表示No", no),
            ("OLAP一致指定フラグ", "1"),
            ("OLAP一致指定", "0"),
            ("OLAP除外指定フラグ", "0"),
            ("OLAP値", value),
            ("OLAP範囲指定フラグ", "0"),
            ("OLAP範囲_Fromフラグ", "1"),
            ("OLAP範囲Val_From", ""),
            ("OLAP範囲Sel_From", "0"),
            ("OLAP範囲_Toフラグ", "1"),
            ("OLAP範囲Val_To", ""),
            ("OLAP範囲Sel_To", "0"),
            ("OLAP月度指定フラグ", "0"),
            ("OLAP月度指定", "0"),
            ("OLAP条件グループ", "0"),
            ("OLAP空白", "1"),
            ("OLAPドメイン分類", domain_type),
            ("エンティティ論理名", OLAP_TARGET),
            ("フィールド論理名", field_name),
            ("XupperRoutingItems", []),
        ]
    )
