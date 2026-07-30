"""伝票に表示する加工名の設定。

内部キーと従来名は固定し、ここで解決した表示名は UI/PDF の描画時だけ使用する。
"""
from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from collections import OrderedDict
from typing import Any, Mapping

from PySide6.QtCore import QSettings

_LOG = logging.getLogger("tks_to_kintone_app")

SETTINGS_KEY = "voucher/processing_display_names"

# 並び順、stable internal key、従来の正式表示名。
PROCESSING_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("edging", "エッジング"),
    ("wide", "広幅"),
    ("factory_cut", "工場切"),
    ("hand_processing", "手加工"),
    ("dm_10", "DM-10"),
    ("pull_handle", "引手"),
    ("multi", "マルチ"),
    ("cleaning", "洗浄"),
    ("bob", "BOB"),
    ("printing", "印刷"),
    ("film_lamination", "フィルム貼"),
    ("rounding", "Rとり"),
)
DEFAULT_PROCESSING_DISPLAY_NAMES = OrderedDict(PROCESSING_DEFINITIONS)
PROCESSING_KEYS = tuple(DEFAULT_PROCESSING_DISPLAY_NAMES)
DEFAULT_NAME_TO_KEY = {
    name: key for key, name in PROCESSING_DEFINITIONS
}


def processing_name_display_width(value: str) -> int:
    """表示幅を半角=1、全角=2、結合文字=0の単位で返す。"""
    total = 0
    for char in unicodedata.normalize("NFC", str(value)):
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in {"W", "F", "A"} else 1
    return total


def validate_processing_display_name(value: str) -> str:
    """正規化・検証済みの表示名を返す。違反時は ValueError。"""
    normalized = unicodedata.normalize("NFC", str(value)).strip()
    if any(char in "\r\n\t" or unicodedata.category(char) == "Cc"
           for char in normalized):
        raise ValueError("加工名に改行・タブ・制御文字は使用できません。")
    if processing_name_display_width(normalized) > 12:
        raise ValueError("加工名は全角6文字相当までで入力してください。")
    return normalized


def _decode_settings(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("processing display names must be a JSON object")
    return decoded


def load_processing_display_names(
    settings: QSettings | None = None,
) -> dict[str, str]:
    """設定を読み、壊れた項目だけ既定名へフォールバックする。"""
    store = settings or QSettings()
    raw = store.value(SETTINGS_KEY, "")
    try:
        saved = _decode_settings(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        _LOG.warning("伝票加工名設定が壊れているため既定値を使用します。", exc_info=True)
        saved = {}
    result: dict[str, str] = {}
    for key, default in PROCESSING_DEFINITIONS:
        try:
            result[key] = validate_processing_display_name(saved.get(key, default))
        except (TypeError, ValueError):
            _LOG.warning("伝票加工名設定の項目 %s が不正なため既定値を使用します。", key)
            result[key] = default
    return result


def save_processing_display_names(
    values: Mapping[str, Any], settings: QSettings | None = None,
) -> dict[str, str]:
    """全項目を先に検証してからJSON辞書として一括保存する。"""
    validated = {
        key: validate_processing_display_name(values.get(key, ""))
        for key in PROCESSING_KEYS
    }
    store = settings or QSettings()
    store.setValue(
        SETTINGS_KEY,
        json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    store.sync()
    return validated


def resolve_processing_display_name(
    processing_key_or_default_name: str,
    values: Mapping[str, str] | None = None,
) -> str:
    """stable keyまたは従来名から描画用表示名を解決する。"""
    source = values if values is not None else load_processing_display_names()
    key = (
        processing_key_or_default_name
        if processing_key_or_default_name in DEFAULT_PROCESSING_DISPLAY_NAMES
        else DEFAULT_NAME_TO_KEY.get(processing_key_or_default_name)
    )
    if key is None:
        return str(processing_key_or_default_name)
    return str(source.get(key, DEFAULT_PROCESSING_DISPLAY_NAMES[key]))


def processing_display_names_revision(
    values: Mapping[str, str] | None = None,
) -> str:
    resolved = values if values is not None else load_processing_display_names()
    payload = json.dumps(
        {key: resolved.get(key, default) for key, default in PROCESSING_DEFINITIONS},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
