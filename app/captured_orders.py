"""TKS受注No取込の保存ストレージ。

TKSCloud8 から取得した受注Noを、伝票一覧に直接入れず、まず work 配下の
専用JSON（captured_order_numbers.json）へ蓄積する。

設計方針:
- 保存ファイルが壊れていても・存在しなくてもアプリが落ちないようにする（読み込みは安全側）。
- 空欄・空白のみの受注Noは保存しない。
- 同じ受注Noの重複保存を防ぐ。

軽量化方針（自動保存が重くならないように）:
- 受注No一覧をメモリキャッシュ（_cache / _cache_set）で保持し、自動保存のたびに
  ファイルを読み直さない。重複判定は正規化済みの set で O(1) 判定する。
- 保存要求は「メモリへ即時反映（stage_*）」と「ディスクへの書き込み（flush）」に分離する。
- flush は通常は軽量（fsync/ディレクトリfsync を省略）に、閉じる/終了などの重要時のみ
  durable（fsync まで）で行う。dirty フラグが立っている時だけ書き込む。
- 書き込み失敗時は dirty を残し、呼び出し側で再試行できるようにする。
"""
from __future__ import annotations

import json
import logging
import os
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from app.path_utils import get_captured_order_numbers_path

_LOGGER = logging.getLogger("tks_to_kintone_app")

SOURCE_TKSCLOUD8 = "TKSCloud8"
ORDER_NO_MIN_DIGITS = 7

# ── メモリキャッシュ（自動保存のたびにディスクを読み書きしないための一次保持） ──
# _cache: 受注No一覧（dictのリスト）。None は未ロード。
# _cache_set: 正規化済み受注Noの集合（重複判定を O(1) にする）。
# _cache_path: キャッシュが対応する保存ファイルのパス。パスが変わったら読み直す
#              （テストの一時HOME切り替えや保存先変更でも整合を壊さない）。
# _dirty: メモリ上の変更がまだディスクへ書き込まれていないか。
_cache: list[dict] | None = None
_cache_set: set[str] = set()
_cache_path: Path | None = None
_dirty: bool = False


def _normalize(order_no: object) -> str:
    return str(order_no or "").strip()


def normalize_captured_order_no(value: object) -> str | None:
    """画面から取得した受注Noを保存可能な形へ正規化する。

    - 前後空白を除去
    - 全角数字を半角数字へ変換（NFKC 正規化）
    - 数字以外が混ざる場合、または7桁未満の場合は保存不可として None を返す
    - 空欄は None

    機密値を残さないため、警告ログには取得値そのものを出さない。
    """
    if value is None:
        return None
    # NFKC で全角数字（１２３…）を半角（123…）へ変換しつつ前後空白を除去する。
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return None
    if not text.isdigit():
        _LOGGER.warning("取得した受注Noに数字以外の文字が含まれるため無効としました。")
        return None
    if len(text) < ORDER_NO_MIN_DIGITS:
        return None
    return text


def _read_from_disk(path: Path) -> list[dict]:
    """保存済みの受注No一覧をディスクから読み込む（壊れていても空リスト・例外を投げない）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        _LOGGER.warning("受注No保存ファイルを読み込めません: %s", path, exc_info=True)
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        _LOGGER.warning("受注No保存ファイルが壊れています（無視します）: %s", path)
        return []
    if not isinstance(data, list):
        _LOGGER.warning("受注No保存ファイルの形式が不正です（無視します）: %s", path)
        return []
    result: list[dict] = []
    for item in data:
        if isinstance(item, dict) and _normalize(item.get("order_no")):
            result.append(item)
    return result


def _rebuild_cache_set() -> None:
    global _cache_set
    _cache_set = {
        normalized
        for normalized in (_normalize(item.get("order_no")) for item in (_cache or []))
        if normalized
    }


def _ensure_loaded() -> None:
    """キャッシュが未ロード、または保存先パスが変わっていたら読み直す。"""
    global _cache, _cache_path, _dirty
    path = get_captured_order_numbers_path()
    if _cache is not None and _cache_path == path:
        return
    _cache = _read_from_disk(path)
    _cache_path = path
    _dirty = False
    _rebuild_cache_set()


def reset_cache() -> None:
    """メモリキャッシュを破棄する（主にテスト用。次回アクセスで読み直す）。"""
    global _cache, _cache_set, _cache_path, _dirty
    _cache = None
    _cache_set = set()
    _cache_path = None
    _dirty = False


def is_dirty() -> bool:
    """メモリ上に未書き込みの変更があるか。"""
    return _dirty


def load_captured_orders() -> list[dict]:
    """保存済みの受注No一覧を返す（キャッシュ経由・壊れていても空リスト）。

    外部からの誤ったインプレース変更でキャッシュが壊れないよう、コピーを返す。
    """
    _ensure_loaded()
    return [dict(item) for item in (_cache or [])]


def _write_to_disk(orders: list[dict], path: Path, *, durable: bool) -> dict:
    """受注No一覧を一時ファイル経由で書き込む（破損防止）。所要時間の内訳を返す。

    durable=True のときのみ fsync とディレクトリfsync を行う（重い）。
    durable=False（自動保存の通常flush）は flush + os.replace のみで軽量に済ませる。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    metrics = {
        "file_write_elapsed_ms": 0.0,
        "fsync_elapsed_ms": 0.0,
        "replace_elapsed_ms": 0.0,
        "dir_fsync_elapsed_ms": 0.0,
    }
    payload = json.dumps(orders, ensure_ascii=False, indent=2)
    started = time.monotonic()
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        metrics["file_write_elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        if durable:
            fsync_started = time.monotonic()
            os.fsync(handle.fileno())
            metrics["fsync_elapsed_ms"] = round((time.monotonic() - fsync_started) * 1000, 2)
    replace_started = time.monotonic()
    os.replace(tmp, path)
    metrics["replace_elapsed_ms"] = round((time.monotonic() - replace_started) * 1000, 2)
    if durable:
        dir_started = time.monotonic()
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        metrics["dir_fsync_elapsed_ms"] = round((time.monotonic() - dir_started) * 1000, 2)
    return metrics


def flush(*, durable: bool = False, reason: str = "") -> dict:
    """dirty のときだけメモリ上の一覧をディスクへ書き込む。所要時間の内訳を返す。

    - durable=False（既定）: 軽量flush（fsync/ディレクトリfsync を省略）。自動保存向け。
    - durable=True: 重要flush（fsync まで）。画面close時・アプリ終了時向け。
    - 書き込み失敗時は例外を送出し、dirty は True のまま残す（呼び出し側で再試行可能）。
    """
    global _dirty
    _ensure_loaded()
    result = {
        "wrote": False,
        "durable": durable,
        "reason": reason,
        "count": len(_cache or []),
        "elapsed_ms": 0.0,
        "file_write_elapsed_ms": 0.0,
        "fsync_elapsed_ms": 0.0,
        "replace_elapsed_ms": 0.0,
        "dir_fsync_elapsed_ms": 0.0,
    }
    if not _dirty:
        return result
    path = _cache_path or get_captured_order_numbers_path()
    started = time.monotonic()
    _LOGGER.info("order_import_save_atomic_started path=%s count=%s durable=%s", path, len(_cache or []), durable)
    metrics = _write_to_disk(_cache or [], path, durable=durable)
    result.update(metrics)
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    result["wrote"] = True
    _dirty = False
    _LOGGER.info("order_import_save_atomic_finished path=%s count=%s durable=%s", path, len(_cache or []), durable)
    return result


def stage_order(
    order_no: str,
    *,
    source: str = SOURCE_TKSCLOUD8,
    method: str = "manual",
) -> tuple[bool, str]:
    """受注Noをメモリキャッシュへ即時追加する（ディスクへは書かない）。

    戻り値 (saved, reason):
    - (False, "empty"): 空欄・空白のみ・桁不足のため保存しない
    - (False, "duplicate"): 既にキャッシュ上に存在するため保存しない
    - (True, "saved"): メモリへ追加した（要 flush）
    """
    global _dirty
    normalized = normalize_captured_order_no(order_no)
    if not normalized:
        return False, "empty"
    _ensure_loaded()
    if normalized in _cache_set:
        return False, "duplicate"
    assert _cache is not None
    _cache.append(
        {
            "order_no": normalized,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "method": method,
            "added_to_voucher": False,
            "olap_fetched": False,
        }
    )
    _cache_set.add(normalized)
    _dirty = True
    return True, "saved"


def add_captured_order(
    order_no: str,
    *,
    source: str = SOURCE_TKSCLOUD8,
    method: str = "manual",
) -> tuple[bool, str]:
    """受注Noを保存する（メモリ追加＋即時 durable flush）。

    低頻度の呼び出し（手動テスト・非UIコード）向けの互換API。高頻度の自動保存は
    stage_order + まとめ flush を使うこと。
    """
    saved, reason = stage_order(order_no, source=source, method=method)
    if saved:
        flush(durable=True, reason="add_captured_order")
    return saved, reason


def stage_mark_added(order_no: str, *, olap_fetched: bool = False) -> bool:
    """指定受注Noを「伝票一覧に追加済み」としてメモリ上に記録する（ディスクへは書かない）。"""
    global _dirty
    normalized = normalize_captured_order_no(order_no)
    if not normalized:
        return False
    _ensure_loaded()
    changed = False
    for item in (_cache or []):
        if _normalize(item.get("order_no")) == normalized:
            item["added_to_voucher"] = True
            if olap_fetched:
                item["olap_fetched"] = True
            changed = True
    if changed:
        _dirty = True
    return changed


def mark_added_to_voucher(order_no: str, *, olap_fetched: bool = False) -> None:
    """指定受注Noを「伝票一覧に追加済み」として記録する（メモリ反映＋durable flush）。"""
    if stage_mark_added(order_no, olap_fetched=olap_fetched):
        flush(durable=True, reason="mark_added_to_voucher")


def stage_remove(order_nos: set[str]) -> int:
    """正規化済み受注No集合に一致する保存行をメモリ上から削除する（ディスクへは書かない）。"""
    global _dirty
    normalized_targets = {
        normalized
        for normalized in (normalize_captured_order_no(value) for value in order_nos)
        if normalized
    }
    if not normalized_targets:
        return 0
    _ensure_loaded()
    kept: list[dict] = []
    removed = 0
    for item in (_cache or []):
        normalized = normalize_captured_order_no(item.get("order_no"))
        if normalized and normalized in normalized_targets:
            removed += 1
            continue
        kept.append(item)
    if removed:
        assert _cache is not None
        _cache[:] = kept
        _rebuild_cache_set()
        _dirty = True
    return removed


def remove_captured_orders_by_order_no(order_nos: set[str]) -> int:
    """正規化済み受注No集合に一致する保存行を削除する（メモリ反映＋durable flush）。"""
    removed = stage_remove(order_nos)
    if removed:
        flush(durable=True, reason="remove_captured_orders")
    return removed


def save_captured_orders(orders: list[dict], *, durable: bool = True) -> None:
    """受注No一覧を一覧全体で置き換えて保存する（一覧画面の編集・削除で使用）。

    キャッシュを与えられた内容へ差し替え、durable flush で書き込む。
    """
    global _cache, _cache_path, _dirty
    _cache = [dict(order) for order in orders]
    _cache_path = get_captured_order_numbers_path()
    _rebuild_cache_set()
    _dirty = True
    flush(durable=durable, reason="save_captured_orders")


def get_captured_path() -> Path:
    return get_captured_order_numbers_path()
