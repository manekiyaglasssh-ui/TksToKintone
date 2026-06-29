"""ログイン情報の保存・読み込みを担う共通処理。

OLAP / kintone のログインID・パスワードを、ログインチェック成功後にのみ保存し、
次回起動時に読み込めるようにする。

保存方式:
    - パスワードは可能であれば keyring（Windows では資格情報マネージャー）に保存する。
    - keyring が利用できない場合は QSettings（既存方式）にフォールバックする。
    - ログインIDは秘密情報ではないため QSettings に保存する。

安全のための原則:
    - ログインチェック成功後にのみ保存する（呼び出し側の責務）。
    - 空文字では既存の保存値を上書きしない。
    - 読み込み失敗時は空のまま返し、例外を呼び出し側に伝播させない。
    - 保存失敗時はログに警告だけ残し、例外を伝播させない。
    - パスワード本文は一切ログに出力しない。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QSettings

try:  # keyring は任意依存。未導入でも QSettings にフォールバックして動作する。
    import keyring
except Exception:  # pragma: no cover - 環境によって未導入
    keyring = None  # type: ignore[assignment]

_LOGGER = logging.getLogger("tks_to_kintone_app")

_SETTINGS_ORG = "Manekiya"
_SETTINGS_APP = "TksToKintone"
_KEYRING_SERVICE = "TksToKintone"

# QSettings キー（既存の保存値と互換）。
_KEY_OLAP_ID = "olap/login_id"
_KEY_OLAP_PASSWORD = "olap/password"
_KEY_KINTONE_ID = "kintone/login_id"
_KEY_KINTONE_PASSWORD = "kintone/password"

# keyring 上のユーザー名（パスワード保存先の識別子）。
_KR_OLAP_PASSWORD = "olap_password"
_KR_KINTONE_PASSWORD = "kintone_password"


@dataclass
class SavedCredentials:
    """保存済みログイン情報。未保存の項目は空文字。"""

    olap_login_id: str = ""
    olap_password: str = ""
    kintone_login_id: str = ""
    kintone_password: str = ""


def _settings() -> QSettings:
    return QSettings(_SETTINGS_ORG, _SETTINGS_APP)


def load_saved_credentials() -> SavedCredentials:
    """保存済みのログイン情報を読み込む。失敗しても例外を投げず空欄で返す。"""
    try:
        settings = _settings()
        return SavedCredentials(
            olap_login_id=str(settings.value(_KEY_OLAP_ID, "") or ""),
            olap_password=_load_password(_KR_OLAP_PASSWORD, _KEY_OLAP_PASSWORD),
            kintone_login_id=str(settings.value(_KEY_KINTONE_ID, "") or ""),
            kintone_password=_load_password(_KR_KINTONE_PASSWORD, _KEY_KINTONE_PASSWORD),
        )
    except Exception:
        _LOGGER.warning("保存済みログイン情報の読み込みに失敗しました。空欄で起動します。")
        return SavedCredentials()


def save_olap_credentials(login_id: str, password: str) -> None:
    """OLAPログイン情報を保存する。空文字は既存値を上書きしない。"""
    try:
        if login_id:
            _store_id(_KEY_OLAP_ID, login_id)
        if password:
            _store_password(_KR_OLAP_PASSWORD, _KEY_OLAP_PASSWORD, password)
    except Exception:
        # パスワード本文を含めないよう、例外内容はログに出さない。
        _LOGGER.warning("OLAPログイン情報の保存に失敗しました。")


def save_kintone_credentials(login_id: str, password: str) -> None:
    """kintoneログイン情報を保存する。空文字は既存値を上書きしない。"""
    try:
        if login_id:
            _store_id(_KEY_KINTONE_ID, login_id)
        if password:
            _store_password(_KR_KINTONE_PASSWORD, _KEY_KINTONE_PASSWORD, password)
    except Exception:
        _LOGGER.warning("kintoneログイン情報の保存に失敗しました。")


def clear_saved_credentials() -> None:
    """保存済みのログイン情報をすべて削除する。"""
    try:
        settings = _settings()
        for key in (_KEY_OLAP_ID, _KEY_OLAP_PASSWORD, _KEY_KINTONE_ID, _KEY_KINTONE_PASSWORD):
            settings.remove(key)
        settings.sync()
        if keyring is not None:
            for user in (_KR_OLAP_PASSWORD, _KR_KINTONE_PASSWORD):
                try:
                    keyring.delete_password(_KEYRING_SERVICE, user)
                except Exception:
                    pass
    except Exception:
        _LOGGER.warning("保存済みログイン情報の削除に失敗しました。")


def _store_id(key: str, value: str) -> None:
    settings = _settings()
    settings.setValue(key, value)
    settings.sync()


def _store_password(keyring_user: str, settings_key: str, value: str) -> None:
    """パスワードを keyring（利用可能なら）に、無ければ QSettings に保存する。"""
    if keyring is not None:
        try:
            keyring.set_password(_KEYRING_SERVICE, keyring_user, value)
            # keyring に保存できたら平文フォールバックは残さない。
            settings = _settings()
            settings.remove(settings_key)
            settings.sync()
            return
        except Exception:
            # 失敗内容にパスワードが含まれうるため exc_info は付けない。
            _LOGGER.warning("keyring へのパスワード保存に失敗したため設定ストアに保存します。")
    settings = _settings()
    settings.setValue(settings_key, value)
    settings.sync()


def _load_password(keyring_user: str, settings_key: str) -> str:
    if keyring is not None:
        try:
            value = keyring.get_password(_KEYRING_SERVICE, keyring_user)
            if value:
                return value
        except Exception:
            _LOGGER.warning("keyring からのパスワード読み込みに失敗しました。設定ストアを確認します。")
    return str(_settings().value(settings_key, "") or "")
