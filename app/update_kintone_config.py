"""更新確認先Kintoneの本番設定／デバッグoverrideを一元解決する。"""
from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass

from PySide6.QtCore import QSettings

from app.credential_store import load_update_debug_kintone_api_token

_LOGGER = logging.getLogger("tks_to_kintone_app")

SETTINGS_ORG = "Manekiya"
SETTINGS_APP = "TksToKintone"
SETTINGS_DEBUG_VISIBLE = "ui/debug_visible"
SETTINGS_UPDATE_DEBUG_KINTONE_APP_ID = "update/debug_kintone_app_id"


@dataclass(frozen=True)
class UpdateKintoneConfig:
    app_id: str
    api_token: str
    source: str


def setting_is_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_update_kintone_app_id(value: object) -> str:
    """正の整数のアプリIDをNFKC正規化して返す。無効値はValueError。"""
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not normalized:
        return ""
    if not normalized.isascii() or not normalized.isdecimal():
        raise ValueError("アプリIDは正の整数で入力してください。")
    if int(normalized) <= 0:
        raise ValueError("アプリIDは1以上で入力してください。")
    return str(int(normalized))


def normalize_update_kintone_api_token(value: object) -> str:
    """APIトークンを検証する。秘密値自体は例外へ含めない。"""
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if any(
        char in "\r\n\t" or unicodedata.category(char) == "Cc"
        for char in normalized
    ):
        raise ValueError("APIトークンに改行、タブ、制御文字は使用できません。")
    return normalized


def resolve_update_kintone_config(
    *,
    settings: QSettings | None = None,
    production_app_id: object,
    production_api_token: str,
) -> UpdateKintoneConfig:
    """OFF/両方未設定は本番、両方有効はデバッグ、不完全設定はエラー。"""
    store = settings or QSettings(SETTINGS_ORG, SETTINGS_APP)
    if not setting_is_enabled(store.value(SETTINGS_DEBUG_VISIBLE, "0")):
        _LOGGER.info(
            "event=update_kintone_config_resolved source=production "
            "reason=debug_disabled app_id=%s",
            production_app_id,
        )
        return UpdateKintoneConfig(
            str(production_app_id), str(production_api_token), "production"
        )

    raw_app_id = store.value(SETTINGS_UPDATE_DEBUG_KINTONE_APP_ID, "")
    api_token = load_update_debug_kintone_api_token()
    try:
        app_id = normalize_update_kintone_app_id(raw_app_id)
        api_token = normalize_update_kintone_api_token(api_token)
    except ValueError as exc:
        _LOGGER.warning(
            "event=update_kintone_debug_config_invalid reason=invalid_value"
        )
        raise ValueError(
            "更新確認用Kintoneのデバッグ設定が不正です。設定画面で確認してください。"
        ) from exc

    if app_id and api_token:
        _LOGGER.info(
            "event=update_kintone_config_resolved source=debug_override app_id=%s",
            app_id,
        )
        return UpdateKintoneConfig(app_id, api_token, "debug_override")

    if app_id or api_token:
        reason = "missing_api_token" if app_id else "missing_app_id"
        _LOGGER.warning(
            "event=update_kintone_debug_config_invalid reason=%s", reason
        )
        raise ValueError(
            "更新確認用KintoneのアプリIDとAPIトークンを両方設定してください。"
        )

    reason = "debug_override_unset"
    _LOGGER.info(
        "event=update_kintone_config_resolved source=production reason=%s app_id=%s",
        reason,
        production_app_id,
    )
    return UpdateKintoneConfig(
        str(production_app_id), str(production_api_token), "production"
    )
