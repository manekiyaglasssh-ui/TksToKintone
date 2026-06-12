"""伝票作成・印刷画面の永続設定。

config.env に保存する2つの設定を扱う。

- VOUCHER_DEFAULT_PRINT_TYPES: 新規行の「印刷する伝票」初期チェック状態。
  伝票種別ID（01〜08）のカンマ区切り。
- VOUCHER_CACHE_RETENTION_DAYS: OLAP取得データの保存期間（日数）。

load_app_config() は必須キー欠落時に例外を投げるため、ここでは設定読み込みの
堅牢性を優先して dotenv_values でゆるく読み取る。保存は既存の
update_values_in_config を使い、他のキーやコメントを壊さない。
"""
from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

from app.config import update_values_in_config, user_config_path
from app.voucher_templates import VOUCHER_IDS

VOUCHER_DEFAULT_PRINT_TYPES_KEY = "VOUCHER_DEFAULT_PRINT_TYPES"
VOUCHER_CACHE_RETENTION_DAYS_KEY = "VOUCHER_CACHE_RETENTION_DAYS"

# 既定では全伝票（01〜08）を印刷対象にする（従来の全ONと同じ挙動）。
DEFAULT_PRINT_TYPES: list[str] = list(VOUCHER_IDS)
DEFAULT_CACHE_RETENTION_DAYS = 7


def _config_path() -> Path:
    return user_config_path()


def _read_values() -> dict[str, str | None]:
    try:
        return dict(dotenv_values(_config_path()))
    except Exception:
        return {}


def parse_print_types(raw: str | None) -> list[str]:
    """カンマ区切り文字列を有効な伝票IDのリストへ正規化する。

    未知のIDは除外し、VOUCHER_IDS の並び順を保つ。空・不正なら既定を返す。
    """
    if raw is None:
        return list(DEFAULT_PRINT_TYPES)
    requested = {item.strip() for item in str(raw).split(",") if item.strip()}
    if not requested:
        return []
    return [vid for vid in VOUCHER_IDS if vid in requested]


def load_default_print_types() -> list[str]:
    """新規行の印刷する伝票初期チェック（伝票IDリスト）を読み込む。"""
    values = _read_values()
    if VOUCHER_DEFAULT_PRINT_TYPES_KEY not in values:
        return list(DEFAULT_PRINT_TYPES)
    return parse_print_types(values.get(VOUCHER_DEFAULT_PRINT_TYPES_KEY))


def save_default_print_types(ids: list[str]) -> None:
    """印刷する伝票初期チェックを config.env へ保存する。"""
    ordered = [vid for vid in VOUCHER_IDS if vid in set(ids)]
    update_values_in_config(
        _config_path(),
        {VOUCHER_DEFAULT_PRINT_TYPES_KEY: ",".join(ordered)},
    )


def normalize_cache_retention_days(value: object) -> int:
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_CACHE_RETENTION_DAYS
    return days if days > 0 else DEFAULT_CACHE_RETENTION_DAYS


def load_cache_retention_days() -> int:
    """OLAPキャッシュ保存期間（日数）を読み込む。既定7日。"""
    values = _read_values()
    if VOUCHER_CACHE_RETENTION_DAYS_KEY not in values:
        return DEFAULT_CACHE_RETENTION_DAYS
    return normalize_cache_retention_days(values.get(VOUCHER_CACHE_RETENTION_DAYS_KEY))


def save_cache_retention_days(days: int) -> None:
    """OLAPキャッシュ保存期間を config.env へ保存する。"""
    normalized = normalize_cache_retention_days(days)
    update_values_in_config(
        _config_path(),
        {VOUCHER_CACHE_RETENTION_DAYS_KEY: str(normalized)},
    )
