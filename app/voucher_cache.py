"""OLAP取得データの受注Noごとのキャッシュ保存。

ボタン押下（指図書編集 / PDF作成 / 印刷 / 選択PDF作成 / 選択印刷）時に取得した
OLAPデータを受注Noごとに1ファイル（JSON）で保存する。保存期間（デフォルト7日）を
過ぎたキャッシュは削除する。

このキャッシュは「指図書編集オブジェクト」とは別ディレクトリに保存し、
キャッシュ削除で編集オブジェクトが消えないようにする（要件7）。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.path_utils import get_voucher_cache_dir

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_order_no(order_no: str) -> str:
    """受注Noをファイル名に使える文字列へ正規化する。

    Windowsで使えない文字を除去し、前後の空白・ドットを取り除く。
    空になった場合は固定名にフォールバックする。
    """
    cleaned = _INVALID_CHARS.sub("_", str(order_no or "").strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "_unknown_"


def cache_path_for(order_no: str, cache_dir: Path | None = None) -> Path:
    base = cache_dir or get_voucher_cache_dir()
    return base / f"{sanitize_order_no(order_no)}.json"


def save_olap_cache(
    order_no: str,
    *,
    raw_rows: list[dict[str, Any]] | None = None,
    pages: list[dict[str, Any]] | None = None,
    request_conditions: dict[str, Any] | None = None,
    row_settings: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    now: float | None = None,
) -> Path:
    """受注NoのOLAPデータをキャッシュへ保存（上書き）する。

    Args:
        order_no: 保存キーとなる受注No。
        raw_rows: OLAPレスポンスの正規化前（抽出直後）データ。
        pages: マッピング後の伝票データ（ページ単位）。
        request_conditions: 使用したリクエスト条件。
        row_settings: 行設定値（仕上日・AM/PM・加工名チェック・印刷する伝票チェック）。
        cache_dir: 保存先ディレクトリ（省略時は既定）。
        now: 取得日時に使うエポック秒（テスト用）。
    """
    base = cache_dir or get_voucher_cache_dir()
    base.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.fromtimestamp(time.time() if now is None else now)
    payload = {
        "order_no": order_no,
        "fetched_at": timestamp.isoformat(timespec="seconds"),
        "request_conditions": request_conditions or {},
        "row_settings": _jsonable(row_settings or {}),
        "raw_rows": raw_rows or [],
        "pages": _jsonable(pages or []),
    }
    path = cache_path_for(order_no, base)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_olap_cache(order_no: str, cache_dir: Path | None = None) -> dict[str, Any] | None:
    """受注Noのキャッシュを読み込む。無ければ None。"""
    path = cache_path_for(order_no, cache_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cleanup_expired_cache(
    retention_days: int,
    cache_dir: Path | None = None,
    now: float | None = None,
) -> int:
    """保存期間を過ぎたキャッシュファイルを削除し、削除件数を返す。"""
    base = cache_dir or get_voucher_cache_dir()
    if not base.exists():
        return 0
    days = retention_days if retention_days and retention_days > 0 else 7
    cutoff = (time.time() if now is None else now) - days * 24 * 60 * 60
    deleted = 0
    for path in base.glob("*.json"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted


def _jsonable(value: Any) -> Any:
    """date など JSON 非対応の値を文字列化して保存できる形にする。"""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
