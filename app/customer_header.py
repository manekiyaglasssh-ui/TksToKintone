"""得意先ヘッダー（表示名・判定文字列）の初期値投入。

得意先ヘッダー設定は config.env の CUSTOMER_LABEL_1〜4 / CUSTOMER_MATCH_1〜4 で管理する。
初回インストール直後（該当キーが config.env に存在しない場合）だけ、既定値を書き込む。
利用者が既に値を設定している場合は上書きしない。
"""
from __future__ import annotations

import logging
from pathlib import Path

_LOGGER = logging.getLogger("tks_to_kintone_app")

# 得意先ヘッダーの初期値。
# 得意先1 のみ実値を持ち、得意先2〜4 は既存仕様どおり既定（プレースホルダ／空）。
DEFAULT_CUSTOMER_HEADERS: dict[str, str] = {
    "customer1_label": "東芝・日立・フジテック",
    "customer1_keywords": "エレベータ",
    "customer2_label": "得意先2",
    "customer2_keywords": "",
    "customer3_label": "得意先3",
    "customer3_keywords": "",
    "customer4_label": "得意先4",
    "customer4_keywords": "",
}

# DEFAULT_CUSTOMER_HEADERS の内部キー → config.env のキー。
_LABEL_KEY_BY_INDEX = {i: f"CUSTOMER_LABEL_{i}" for i in range(1, 5)}
_MATCH_KEY_BY_INDEX = {i: f"CUSTOMER_MATCH_{i}" for i in range(1, 5)}


def _existing_env_values(config_path: Path) -> dict[str, str]:
    """config.env に「キー=値」の形で実在する設定値を返す。"""
    if not config_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in config_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def default_values_for_config() -> dict[str, str]:
    """config.env のキー名 → 既定値 の対応を返す。"""
    values: dict[str, str] = {}
    for index in range(1, 5):
        values[_LABEL_KEY_BY_INDEX[index]] = DEFAULT_CUSTOMER_HEADERS[f"customer{index}_label"]
        values[_MATCH_KEY_BY_INDEX[index]] = DEFAULT_CUSTOMER_HEADERS[f"customer{index}_keywords"]
    return values


def ensure_default_customer_headers(config_path: Path) -> bool:
    """未設定の得意先ヘッダーキーを既定値で補完する。

    設定ファイルが無い、得意先ヘッダーキーが無い、または全項目が空欄の場合は
    既定値を投入する。利用者が一部でも設定している場合、既存キーは上書きしない。
    戻り値: 1つでも書き込んだら True。
    """
    # 循環import回避のため遅延import。
    from app.config import update_values_in_config

    defaults = default_values_for_config()
    existing_values = _existing_env_values(config_path)
    existing_header_keys = {key for key in defaults if key in existing_values}
    if not config_path.exists() or not existing_header_keys:
        to_write = defaults
    elif all(not existing_values.get(key, "").strip() for key in existing_header_keys):
        to_write = defaults
    else:
        to_write = {key: value for key, value in defaults.items() if key not in existing_values}
    if not to_write:
        return False
    update_values_in_config(config_path, to_write)
    _LOGGER.info("得意先ヘッダー設定の初期値を補完しました: %s", ",".join(sorted(to_write)))
    return True
