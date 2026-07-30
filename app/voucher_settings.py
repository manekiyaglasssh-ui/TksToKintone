"""伝票作成・印刷画面の永続設定。

config.env に保存する2つの設定を扱う。

- VOUCHER_DEFAULT_PRINT_TYPES: 新規行の「印刷する伝票」初期チェック状態。
  伝票種別ID（01〜08）のカンマ区切り。
- VOUCHER_CACHE_RETENTION_DAYS: OLAP取得データの保存期間（日数）。
- VOUCHER_RECORD_RETENTION_DAYS: 伝票作成・印刷一覧の保存期間（日数）。

load_app_config() は必須キー欠落時に例外を投げるため、ここでは設定読み込みの
堅牢性を優先して dotenv_values でゆるく読み取る。保存は既存の
update_values_in_config を使い、他のキーやコメントを壊さない。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values
from PySide6.QtCore import QSettings

from app.config import update_values_in_config, user_config_path
from app.voucher_templates import VOUCHER_IDS

VOUCHER_DEFAULT_PRINT_TYPES_KEY = "VOUCHER_DEFAULT_PRINT_TYPES"
VOUCHER_DEFAULT_FINISH_DATE_NONE_KEY = "VOUCHER_DEFAULT_FINISH_DATE_NONE"
VOUCHER_DEFAULT_AMPM_KEY = "VOUCHER_DEFAULT_AMPM"
VOUCHER_CACHE_RETENTION_DAYS_KEY = "VOUCHER_CACHE_RETENTION_DAYS"
VOUCHER_RECORD_RETENTION_DAYS_KEY = "VOUCHER_RECORD_RETENTION_DAYS"
# タブレット編集モードで指図書編集ウィンドウを移動する先のディスプレイ名（QScreen.name）。
VOUCHER_TABLET_SCREEN_KEY = "VOUCHER_TABLET_SCREEN"

SETTINGS_ORG = "Manekiya"
SETTINGS_APP = "TksToKintone"
VOUCHER_PRINT_PRINTER_NAME = "VOUCHER_PRINT_PRINTER_NAME"
VOUCHER_PRINT_PAPER_SIZE = "VOUCHER_PRINT_PAPER_SIZE"
VOUCHER_PRINT_ORIENTATION = "VOUCHER_PRINT_ORIENTATION"
VOUCHER_PRINT_COLOR_MODE = "VOUCHER_PRINT_COLOR_MODE"
VOUCHER_PRINT_COPIES = "VOUCHER_PRINT_COPIES"
VOUCHER_PRINT_SCALE_MODE = "VOUCHER_PRINT_SCALE_MODE"
VOUCHER_PRINT_BACKEND = "VOUCHER_PRINT_BACKEND"
VOUCHER_PRINT_ACROBAT_PATH = "VOUCHER_PRINT_ACROBAT_PATH"
VOUCHER_PRINT_ACROBAT_HIDE_WINDOW = "VOUCHER_PRINT_ACROBAT_HIDE_WINDOW"
VOUCHER_PRINT_ACROBAT_CLOSE_AFTER_PRINT = "VOUCHER_PRINT_ACROBAT_CLOSE_AFTER_PRINT"
VOUCHER_PRINT_ACROBAT_CLOSE_DELAY_SECONDS = "VOUCHER_PRINT_ACROBAT_CLOSE_DELAY_SECONDS"
VOUCHER_PRINT_ACROBAT_ALLOW_FORCE_KILL = "VOUCHER_PRINT_ACROBAT_ALLOW_FORCE_KILL"
VOUCHER_PRINT_ACROBAT_HIDE_WATCH_ENABLED = "VOUCHER_PRINT_ACROBAT_HIDE_WATCH_ENABLED"
VOUCHER_PRINT_ACROBAT_HIDE_WATCH_SECONDS = "VOUCHER_PRINT_ACROBAT_HIDE_WATCH_SECONDS"
VOUCHER_PRINT_SUMATRA_PATH = "VOUCHER_PRINT_SUMATRA_PATH"
VOUCHER_PRINT_SUMATRA_SETTINGS = "VOUCHER_PRINT_SUMATRA_SETTINGS"
VOUCHER_PRINT_SUMATRA_PAPERKIND = "VOUCHER_PRINT_SUMATRA_PAPERKIND"
VOUCHER_PRINT_SUMATRA_PROFILE_NAME = "VOUCHER_PRINT_SUMATRA_PROFILE_NAME"
VOUCHER_PRINT_SUMATRA_PROFILES = "VOUCHER_PRINT_SUMATRA_PROFILES"
VOUCHER_PRINT_SUMATRA_SCALING_MODE = "VOUCHER_PRINT_SUMATRA_SCALING_MODE"
VOUCHER_PRINT_SUMATRA_PAPER_MODE = "VOUCHER_PRINT_SUMATRA_PAPER_MODE"
VOUCHER_PRINT_SUMATRA_PAPER_VALUE = "VOUCHER_PRINT_SUMATRA_PAPER_VALUE"
VOUCHER_PRINT_SUMATRA_MONOCHROME = "VOUCHER_PRINT_SUMATRA_MONOCHROME"
VOUCHER_PRINT_SUMATRA_CENTER = "VOUCHER_PRINT_SUMATRA_CENTER"
VOUCHER_PRINT_SUMATRA_AUTO_ROTATION = "VOUCHER_PRINT_SUMATRA_AUTO_ROTATION"
VOUCHER_PRINT_SUMATRA_BIN = "VOUCHER_PRINT_SUMATRA_BIN"
VOUCHER_PRINT_SUMATRA_EXTRA_OPTIONS = "VOUCHER_PRINT_SUMATRA_EXTRA_OPTIONS"
VOUCHER_PRINT_SUMATRA_WAIT_TIMEOUT_SECONDS = "VOUCHER_PRINT_SUMATRA_WAIT_TIMEOUT_SECONDS"
VOUCHER_PRINT_SUMATRA_ALLOW_FORCE_KILL = "VOUCHER_PRINT_SUMATRA_ALLOW_FORCE_KILL"
# 印刷位置・余白補正（SumatraPDF印刷時のみ適用する印刷補正PDF用の設定）。
VOUCHER_PRINT_ADJUSTMENT_ENABLED = "VOUCHER_PRINT_ADJUSTMENT_ENABLED"
VOUCHER_PRINT_ADJUSTMENT_MARGIN_LEFT_MM = "VOUCHER_PRINT_ADJUSTMENT_MARGIN_LEFT_MM"
VOUCHER_PRINT_ADJUSTMENT_MARGIN_RIGHT_MM = "VOUCHER_PRINT_ADJUSTMENT_MARGIN_RIGHT_MM"
VOUCHER_PRINT_ADJUSTMENT_MARGIN_TOP_MM = "VOUCHER_PRINT_ADJUSTMENT_MARGIN_TOP_MM"
VOUCHER_PRINT_ADJUSTMENT_MARGIN_BOTTOM_MM = "VOUCHER_PRINT_ADJUSTMENT_MARGIN_BOTTOM_MM"
VOUCHER_PRINT_ADJUSTMENT_SCALE_X_PERCENT = "VOUCHER_PRINT_ADJUSTMENT_SCALE_X_PERCENT"
VOUCHER_PRINT_ADJUSTMENT_SCALE_Y_PERCENT = "VOUCHER_PRINT_ADJUSTMENT_SCALE_Y_PERCENT"
VOUCHER_PRINT_ADJUSTMENT_OFFSET_X_MM = "VOUCHER_PRINT_ADJUSTMENT_OFFSET_X_MM"
VOUCHER_PRINT_ADJUSTMENT_OFFSET_Y_MM = "VOUCHER_PRINT_ADJUSTMENT_OFFSET_Y_MM"
VOUCHER_PRINT_ADJUSTMENT_SAVE_PDF = "VOUCHER_PRINT_ADJUSTMENT_SAVE_PDF"
# 行の「印刷」ボタン押下時に、印刷用PDFに加えてPDF出力先へも通常PDFを保存するか。
VOUCHER_PRINT_SAVE_PDF_ON_PRINT = "VOUCHER_PRINT_SAVE_PDF_ON_PRINT"
# 「PDF作成」ボタン押下後に「作成しました」ダイアログを表示するか（共通印刷設定）。
VOUCHER_SHOW_PDF_CREATED_DIALOG = "VOUCHER_SHOW_PDF_CREATED_DIALOG"
# 「PDF作成」ボタン押下後に作成したPDFを自動で開くか。
VOUCHER_OPEN_PDF_AFTER_CREATE = "VOUCHER_OPEN_PDF_AFTER_CREATE"

# 単価・明細金額・金額列合計の表示モード。既存ユーザー維持のため既定は conditional。
VOUCHER_PRICE_DISPLAY_MODE_KEY = "voucher/price_display_mode"
PRICE_DISPLAY_CONDITIONAL = "conditional"
PRICE_DISPLAY_ALWAYS_SHOW = "always_show"
PRICE_DISPLAY_ALWAYS_HIDE = "always_hide"
PRICE_DISPLAY_MODES = (
    PRICE_DISPLAY_CONDITIONAL,
    PRICE_DISPLAY_ALWAYS_SHOW,
    PRICE_DISPLAY_ALWAYS_HIDE,
)

# 既定では全伝票（01〜08）を印刷対象にする（従来の全ONと同じ挙動）。
DEFAULT_PRINT_TYPES: list[str] = list(VOUCHER_IDS)
DEFAULT_FINISH_DATE_NONE = False
DEFAULT_AMPM = "am"
DEFAULT_CACHE_RETENTION_DAYS = 60
DEFAULT_RECORD_RETENTION_DAYS = 1095
DEFAULT_PRINT_PAPER_SIZE = "B5"
DEFAULT_PRINT_ORIENTATION = "landscape"
DEFAULT_PRINT_COLOR_MODE = "grayscale"
DEFAULT_PRINT_COPIES = 1
DEFAULT_PRINT_SCALE_MODE = "actual_size"
DEFAULT_PRINT_BACKEND = "sumatra"
DEFAULT_ACROBAT_HIDE_WINDOW = True
DEFAULT_ACROBAT_CLOSE_AFTER_PRINT = True
DEFAULT_ACROBAT_CLOSE_DELAY_SECONDS = 10
DEFAULT_ACROBAT_ALLOW_FORCE_KILL = False
DEFAULT_ACROBAT_HIDE_WATCH_ENABLED = True
DEFAULT_ACROBAT_HIDE_WATCH_SECONDS = 10
# 空欄は「Windowsへ独立インストールされたSumatraPDFを実行時に自動検出」を表す。
# 既存の明示設定値がある場合は、アプリ側の探索で最優先する。
DEFAULT_SUMATRA_PATH = ""
# SumatraPDF経由印刷の既定 -print-settings。PDFページサイズ（B5横）を優先するため paper=auto。
DEFAULT_SUMATRA_PRINT_SETTINGS = "noscale,monochrome,paper=auto,bin=auto,center"
SUMATRA_B5_PAPERKIND_PLACEHOLDER = "182"
SUMATRA_PRINT_SETTINGS_PRESETS = (
    ("既定", "noscale,monochrome,paper=auto,bin=auto,center"),
    ("fit", "fit,monochrome,paper=auto,bin=auto,center"),
    ("shrink", "shrink,monochrome,paper=auto,bin=auto,center"),
    ("B5 paperkind指定", "noscale,monochrome,paperkind=<B5の番号>,bin=auto,center"),
    ("自動回転なし", "noscale,monochrome,paper=auto,bin=auto,center,disable-auto-rotation"),
    ("カスタム", ""),
)
# 新規環境・既定に戻す時に選択する SumatraPDFプリセット名（SUMATRA_PRINT_SETTINGS_PRESETS の先頭）。
DEFAULT_SUMATRA_PRESET = "既定"
DEFAULT_SUMATRA_PAPERKIND = ""
DEFAULT_SUMATRA_PROFILE_NAME = "標準"
DEFAULT_SUMATRA_SCALING_MODE = "noscale"
DEFAULT_SUMATRA_PAPER_MODE = "auto"
DEFAULT_SUMATRA_PAPER_VALUE = ""
DEFAULT_SUMATRA_MONOCHROME = True
DEFAULT_SUMATRA_CENTER = True
DEFAULT_SUMATRA_AUTO_ROTATION = True
DEFAULT_SUMATRA_BIN = "auto"
DEFAULT_SUMATRA_EXTRA_OPTIONS = ""
# 印刷要求送信（Popen成功）後に SumatraPDF の終了コード確認を待つ秒数。
# 送信後は UI が復帰しているため、この待機は必ずバックグラウンドで行う。
DEFAULT_SUMATRA_WAIT_TIMEOUT_SECONDS = 15
DEFAULT_SUMATRA_ALLOW_FORCE_KILL = False
# 印刷位置・余白補正の既定値と入力範囲。
# 新規環境・既定に戻す時は印刷補正 ON・左右4mm/上3mm/下1.5mmを既定にする。
DEFAULT_ADJUSTMENT_ENABLED = True
# 汎用の余白既定（0mm）。SumatraPrintProfile など個別既定を持たない箇所で使う。
DEFAULT_ADJUSTMENT_MARGIN_MM = 0.0
# 印刷補正の余白既定（各辺個別）。VoucherPrinterSettings の新規既定に使う。
DEFAULT_ADJUSTMENT_MARGIN_LEFT_MM = 4.0
DEFAULT_ADJUSTMENT_MARGIN_RIGHT_MM = 4.0
DEFAULT_ADJUSTMENT_MARGIN_TOP_MM = 3.0
DEFAULT_ADJUSTMENT_MARGIN_BOTTOM_MM = 1.5
DEFAULT_ADJUSTMENT_SCALE_PERCENT = 100.0
DEFAULT_ADJUSTMENT_OFFSET_MM = 0.0
DEFAULT_ADJUSTMENT_SAVE_PDF = False
# 印刷時にPDFも作成する（PDF出力先への保存）の既定は OFF。
DEFAULT_SAVE_PDF_ON_PRINT = False
# PDF作成完了ダイアログの表示既定は ON（従来どおり「作成しました」を表示する）。
DEFAULT_SHOW_PDF_CREATED_DIALOG = True
# PDF作成後の自動オープン既定は ON（従来の行PDF/選択PDFの自動オープン挙動を維持）。
DEFAULT_OPEN_PDF_AFTER_CREATE = True
ADJUSTMENT_MARGIN_MIN_MM = -20.0
ADJUSTMENT_MARGIN_MAX_MM = 20.0
ADJUSTMENT_OFFSET_MIN_MM = -20.0
ADJUSTMENT_OFFSET_MAX_MM = 20.0
ADJUSTMENT_SCALE_MIN_PERCENT = 95.0
ADJUSTMENT_SCALE_MAX_PERCENT = 105.0
PRINT_BACKEND_ACROBAT = "acrobat"
PRINT_BACKEND_SUMATRA = "sumatra"
PRINT_BACKEND_QT = "qt"
PRINT_SCALE_MODE_ACTUAL_SIZE = "actual_size"
PRINT_SCALE_MODE_FIT_TO_PAGE = "fit_to_page"


def normalize_price_display_mode(value: object) -> str:
    """単価表示モードを正規化する。未保存・未知値・破損値は従来条件へ戻す。"""
    text = str(value or "").strip().lower()
    return text if text in PRICE_DISPLAY_MODES else PRICE_DISPLAY_CONDITIONAL


def load_price_display_mode(settings: QSettings | None = None) -> str:
    target = settings or QSettings(SETTINGS_ORG, SETTINGS_APP)
    mode = normalize_price_display_mode(target.value(VOUCHER_PRICE_DISPLAY_MODE_KEY, None))
    logging.getLogger("tks_to_kintone_app").info(
        "voucher_price_display_mode_loaded mode=%s", mode
    )
    return mode


def save_price_display_mode(mode: object, settings: QSettings | None = None) -> str:
    normalized = normalize_price_display_mode(mode)
    target = settings or QSettings(SETTINGS_ORG, SETTINGS_APP)
    target.setValue(VOUCHER_PRICE_DISPLAY_MODE_KEY, normalized)
    target.sync()
    return normalized


@dataclass(frozen=True)
class VoucherPrinterSettings:
    printer_name: str = ""
    paper_size: str = DEFAULT_PRINT_PAPER_SIZE
    orientation: str = DEFAULT_PRINT_ORIENTATION
    color_mode: str = DEFAULT_PRINT_COLOR_MODE
    copies: int = DEFAULT_PRINT_COPIES
    scale_mode: str = DEFAULT_PRINT_SCALE_MODE
    print_backend: str = DEFAULT_PRINT_BACKEND
    acrobat_path: str = ""
    acrobat_hide_window: bool = DEFAULT_ACROBAT_HIDE_WINDOW
    acrobat_close_after_print: bool = DEFAULT_ACROBAT_CLOSE_AFTER_PRINT
    acrobat_close_delay_seconds: int = DEFAULT_ACROBAT_CLOSE_DELAY_SECONDS
    acrobat_allow_force_kill: bool = DEFAULT_ACROBAT_ALLOW_FORCE_KILL
    acrobat_hide_watch_enabled: bool = DEFAULT_ACROBAT_HIDE_WATCH_ENABLED
    acrobat_hide_watch_seconds: int = DEFAULT_ACROBAT_HIDE_WATCH_SECONDS
    sumatra_path: str = DEFAULT_SUMATRA_PATH
    sumatra_print_settings: str = DEFAULT_SUMATRA_PRINT_SETTINGS
    sumatra_paperkind: str = DEFAULT_SUMATRA_PAPERKIND
    sumatra_profile_name: str = DEFAULT_SUMATRA_PROFILE_NAME
    sumatra_scaling_mode: str = DEFAULT_SUMATRA_SCALING_MODE
    sumatra_paper_mode: str = DEFAULT_SUMATRA_PAPER_MODE
    sumatra_paper_value: str = DEFAULT_SUMATRA_PAPER_VALUE
    sumatra_monochrome: bool = DEFAULT_SUMATRA_MONOCHROME
    sumatra_center: bool = DEFAULT_SUMATRA_CENTER
    sumatra_auto_rotation: bool = DEFAULT_SUMATRA_AUTO_ROTATION
    sumatra_bin: str = DEFAULT_SUMATRA_BIN
    sumatra_extra_options: str = DEFAULT_SUMATRA_EXTRA_OPTIONS
    sumatra_wait_timeout_seconds: int = DEFAULT_SUMATRA_WAIT_TIMEOUT_SECONDS
    sumatra_allow_force_kill: bool = DEFAULT_SUMATRA_ALLOW_FORCE_KILL
    # 印刷位置・余白補正（SumatraPDF印刷時のみ適用）。
    print_adjustment_enabled: bool = DEFAULT_ADJUSTMENT_ENABLED
    print_adjustment_margin_left_mm: float = DEFAULT_ADJUSTMENT_MARGIN_LEFT_MM
    print_adjustment_margin_right_mm: float = DEFAULT_ADJUSTMENT_MARGIN_RIGHT_MM
    print_adjustment_margin_top_mm: float = DEFAULT_ADJUSTMENT_MARGIN_TOP_MM
    print_adjustment_margin_bottom_mm: float = DEFAULT_ADJUSTMENT_MARGIN_BOTTOM_MM
    print_adjustment_scale_x_percent: float = DEFAULT_ADJUSTMENT_SCALE_PERCENT
    print_adjustment_scale_y_percent: float = DEFAULT_ADJUSTMENT_SCALE_PERCENT
    print_adjustment_offset_x_mm: float = DEFAULT_ADJUSTMENT_OFFSET_MM
    print_adjustment_offset_y_mm: float = DEFAULT_ADJUSTMENT_OFFSET_MM
    print_adjustment_save_pdf: bool = DEFAULT_ADJUSTMENT_SAVE_PDF
    # 印刷時にPDFも作成する（行の印刷ボタン押下時、PDF出力先へ通常PDFを保存）。
    save_pdf_on_print: bool = DEFAULT_SAVE_PDF_ON_PRINT
    # PDF作成完了ダイアログを表示する（共通印刷設定・既定ON）。
    show_pdf_created_dialog: bool = DEFAULT_SHOW_PDF_CREATED_DIALOG
    # PDF作成後にPDFを自動で開く（PDF作成ボタン専用・既定ON）。
    open_pdf_after_create: bool = DEFAULT_OPEN_PDF_AFTER_CREATE


@dataclass(frozen=True)
class SumatraPrintProfile:
    profile_name: str
    print_settings: str
    paperkind: str = ""
    memo: str = ""
    updated_at: str = ""
    # 印刷位置・余白補正もプロファイルに含めて保存・読込できるようにする。
    adjustment_enabled: bool = DEFAULT_ADJUSTMENT_ENABLED
    margin_left_mm: float = DEFAULT_ADJUSTMENT_MARGIN_MM
    margin_right_mm: float = DEFAULT_ADJUSTMENT_MARGIN_MM
    margin_top_mm: float = DEFAULT_ADJUSTMENT_MARGIN_MM
    margin_bottom_mm: float = DEFAULT_ADJUSTMENT_MARGIN_MM
    scale_x_percent: float = DEFAULT_ADJUSTMENT_SCALE_PERCENT
    scale_y_percent: float = DEFAULT_ADJUSTMENT_SCALE_PERCENT
    offset_x_mm: float = DEFAULT_ADJUSTMENT_OFFSET_MM
    offset_y_mm: float = DEFAULT_ADJUSTMENT_OFFSET_MM


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


def normalize_bool_setting(value: object, default: bool = False) -> bool:
    """設定値を安全に bool 化する。

    bool("false") が True になる事故を避けるため、文字列は明示的な
    true/false 値だけを採用する。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().strip("\"'").lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def normalize_finish_date_none(value: object) -> bool:
    return normalize_bool_setting(value, DEFAULT_FINISH_DATE_NONE)


def load_default_finish_date_none() -> bool:
    """新規行の仕上日「なし」初期値を読み込む。未設定なら従来どおりOFF。"""
    values = _read_values()
    if VOUCHER_DEFAULT_FINISH_DATE_NONE_KEY not in values:
        return DEFAULT_FINISH_DATE_NONE
    return normalize_finish_date_none(values.get(VOUCHER_DEFAULT_FINISH_DATE_NONE_KEY))


def save_default_finish_date_none(enabled: bool) -> None:
    """新規行の仕上日「なし」初期値を config.env へ保存する。"""
    normalized = normalize_finish_date_none(enabled)
    update_values_in_config(
        _config_path(),
        {VOUCHER_DEFAULT_FINISH_DATE_NONE_KEY: "true" if normalized else "false"},
    )


def normalize_ampm_default(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"none", "なし", "no", "empty"}:
        return "none"
    if text == "pm":
        return "pm"
    if text == "am":
        return "am"
    return DEFAULT_AMPM


def load_default_ampm() -> str:
    """新規行のAM/PM初期値を読み込む。未設定なら従来どおりAM。"""
    values = _read_values()
    if VOUCHER_DEFAULT_AMPM_KEY not in values:
        return DEFAULT_AMPM
    return normalize_ampm_default(values.get(VOUCHER_DEFAULT_AMPM_KEY))


def save_default_ampm(value: object) -> None:
    """新規行のAM/PM初期値を config.env へ保存する。"""
    update_values_in_config(
        _config_path(),
        {VOUCHER_DEFAULT_AMPM_KEY: normalize_ampm_default(value)},
    )


def normalize_cache_retention_days(value: object) -> int:
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_CACHE_RETENTION_DAYS
    return days if days > 0 else DEFAULT_CACHE_RETENTION_DAYS


def normalize_record_retention_days(value: object) -> int:
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_RECORD_RETENTION_DAYS
    return days if days > 0 else DEFAULT_RECORD_RETENTION_DAYS


def load_cache_retention_days() -> int:
    """OLAPキャッシュ保存期間（日数）を読み込む。既定60日。"""
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


def load_record_retention_days() -> int:
    """伝票作成・印刷一覧の保存期間（日数）を読み込む。既定1095日。"""
    values = _read_values()
    if VOUCHER_RECORD_RETENTION_DAYS_KEY not in values:
        return DEFAULT_RECORD_RETENTION_DAYS
    return normalize_record_retention_days(values.get(VOUCHER_RECORD_RETENTION_DAYS_KEY))


def save_record_retention_days(days: int) -> None:
    """伝票作成・印刷一覧の保存期間を config.env へ保存する。"""
    normalized = normalize_record_retention_days(days)
    update_values_in_config(
        _config_path(),
        {VOUCHER_RECORD_RETENTION_DAYS_KEY: str(normalized)},
    )


def load_tablet_screen_name() -> str | None:
    """タブレット編集モードの表示先ディスプレイ名を読み込む。

    未設定・空なら None を返す（初回は外部ディスプレイへ自動移動する）。
    """
    values = _read_values()
    raw = values.get(VOUCHER_TABLET_SCREEN_KEY)
    if raw is None:
        return None
    name = str(raw).strip()
    return name or None


def save_tablet_screen_name(name: str | None) -> None:
    """タブレット編集モードの表示先ディスプレイ名を config.env へ保存する。"""
    update_values_in_config(
        _config_path(),
        {VOUCHER_TABLET_SCREEN_KEY: (name or "").strip()},
    )


def _qsettings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


# 伝票作成・印刷画面の一覧テーブルの列表示/非表示（表示設定）。
# 表示する列 key のリストを JSON 文字列で保存する。存在しない古い key は読込時に無視する。
VOUCHER_VISIBLE_COLUMNS_KEY = "voucher_window/visible_columns"


def load_visible_column_keys(settings: QSettings | None = None) -> list[str] | None:
    """表示する列 key のリストを読み込む。未設定・不正なら None（＝既定に従う）を返す。"""
    store = settings or _qsettings()
    raw = store.value(VOUCHER_VISIBLE_COLUMNS_KEY, "")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    return [str(item) for item in data]


def save_visible_column_keys(keys: list[str], settings: QSettings | None = None) -> None:
    """表示する列 key のリストを QSettings へ保存する。"""
    store = settings or _qsettings()
    store.setValue(VOUCHER_VISIBLE_COLUMNS_KEY, json.dumps([str(k) for k in keys]))
    store.sync()


def normalize_print_paper_size(value: object) -> str:
    text = str(value or "").strip().upper()
    return "B5" if text != "B5" else text


def normalize_print_orientation(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"portrait", "縦"}:
        return "portrait"
    return "landscape"


def normalize_print_color_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"color", "colour", "カラー"}:
        return "color"
    return "grayscale"


def normalize_print_copies(value: object) -> int:
    try:
        copies = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_PRINT_COPIES
    return copies if copies > 0 else DEFAULT_PRINT_COPIES


def normalize_print_scale_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {PRINT_SCALE_MODE_FIT_TO_PAGE, "fit", "用紙に合わせる"}:
        return PRINT_SCALE_MODE_FIT_TO_PAGE
    return PRINT_SCALE_MODE_ACTUAL_SIZE


def bundled_sumatra_available() -> bool:
    """互換API。Windowsへインストール済みのSumatraPDFが存在するか返す。"""
    try:
        from app.sumatra_detection import is_sumatra_pdf_installed

        return is_sumatra_pdf_installed()
    except Exception:
        return False


def default_print_backend_for_environment() -> str:
    """新規環境の既定印刷方式。

    既存環境（保存済み backend あり）はこの関数を使わず保存値を優先する。
    通常運用では高速で画面表示が出にくい SumatraPDF 経由を既定にする。
    """
    return DEFAULT_PRINT_BACKEND


def normalize_print_backend(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {PRINT_BACKEND_SUMATRA, "sumatrapdf", "sumatra pdf", "sumatrapdf経由"}:
        return PRINT_BACKEND_SUMATRA
    if text in {PRINT_BACKEND_QT, "direct", "qt直接印刷"}:
        return PRINT_BACKEND_QT
    return PRINT_BACKEND_ACROBAT


def normalize_sumatra_print_settings(value: object) -> str:
    """SumatraPDF の -print-settings 文字列を正規化する。空なら既定を使う。"""
    text = str(value or "").strip()
    return text or DEFAULT_SUMATRA_PRINT_SETTINGS


def normalize_sumatra_scaling_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"noscale", "fit", "shrink"} else DEFAULT_SUMATRA_SCALING_MODE


def normalize_sumatra_paper_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"auto", "paperkind", "paper"} else DEFAULT_SUMATRA_PAPER_MODE


def normalize_sumatra_paperkind(value: object) -> str:
    """SumatraPDF の paperkind 番号を正規化する。数値以外・空は未指定扱い。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = int(text)
    except (TypeError, ValueError):
        return ""
    return str(number) if number > 0 else ""


def normalize_sumatra_bin(value: object) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_SUMATRA_BIN


def _split_sumatra_tokens(value: object) -> list[str]:
    return [token.strip() for token in str(value or "").split(",") if token.strip()]


def build_sumatra_print_settings(
    *,
    scaling_mode: object = DEFAULT_SUMATRA_SCALING_MODE,
    monochrome: object = DEFAULT_SUMATRA_MONOCHROME,
    paper_mode: object = DEFAULT_SUMATRA_PAPER_MODE,
    paperkind: object = "",
    paper_value: object = "",
    center: object = DEFAULT_SUMATRA_CENTER,
    auto_rotation: object = DEFAULT_SUMATRA_AUTO_ROTATION,
    bin_value: object = DEFAULT_SUMATRA_BIN,
    extra_options: object = "",
) -> str:
    tokens: list[str] = [normalize_sumatra_scaling_mode(scaling_mode)]
    if normalize_bool_setting(monochrome, DEFAULT_SUMATRA_MONOCHROME):
        tokens.append("monochrome")
    mode = normalize_sumatra_paper_mode(paper_mode)
    normalized_paperkind = normalize_sumatra_paperkind(paperkind)
    paper_text = str(paper_value or "").strip()
    if mode == "paperkind" and normalized_paperkind:
        tokens.append(f"paperkind={normalized_paperkind}")
    elif mode == "paper" and paper_text:
        tokens.append(f"paper={paper_text}")
    else:
        tokens.append("paper=auto")
    bin_text = normalize_sumatra_bin(bin_value)
    if bin_text:
        tokens.append(f"bin={bin_text}" if "=" not in bin_text else bin_text)
    if normalize_bool_setting(center, DEFAULT_SUMATRA_CENTER):
        tokens.append("center")
    if not normalize_bool_setting(auto_rotation, DEFAULT_SUMATRA_AUTO_ROTATION):
        tokens.append("disable-auto-rotation")
    tokens.extend(_split_sumatra_tokens(extra_options))
    return ",".join(tokens)


def parse_sumatra_print_settings(value: object) -> dict[str, object]:
    tokens = _split_sumatra_tokens(value)
    result: dict[str, object] = {
        "scaling_mode": DEFAULT_SUMATRA_SCALING_MODE,
        "monochrome": False,
        "paper_mode": DEFAULT_SUMATRA_PAPER_MODE,
        "paperkind": "",
        "paper_value": "",
        "center": False,
        "auto_rotation": True,
        "bin": DEFAULT_SUMATRA_BIN,
        "extra_options": "",
    }
    known_extra: list[str] = []
    for token in tokens:
        lower = token.lower()
        if lower in {"noscale", "fit", "shrink"}:
            result["scaling_mode"] = lower
        elif lower == "monochrome":
            result["monochrome"] = True
        elif lower == "center":
            result["center"] = True
        elif lower == "disable-auto-rotation":
            result["auto_rotation"] = False
        elif lower.startswith("paperkind="):
            result["paper_mode"] = "paperkind"
            result["paperkind"] = normalize_sumatra_paperkind(token.split("=", 1)[1])
        elif lower.startswith("paper="):
            paper = token.split("=", 1)[1].strip()
            result["paper_mode"] = "auto" if paper.lower() == "auto" else "paper"
            result["paper_value"] = "" if paper.lower() == "auto" else paper
        elif lower.startswith("bin="):
            result["bin"] = token.split("=", 1)[1].strip() or DEFAULT_SUMATRA_BIN
        else:
            known_extra.append(token)
    result["extra_options"] = ",".join(known_extra)
    return result


def normalize_sumatra_wait_timeout_seconds(value: object) -> int:
    """SumatraPDF 終了待ち秒数を 5〜120 に丸める。"""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_SUMATRA_WAIT_TIMEOUT_SECONDS
    return min(120, max(5, seconds))


def _normalize_float(value: object, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return float(default)


def _clamp_round(value: float, low: float, high: float) -> float:
    """値を [low, high] に丸め、小数第2位で四捨五入する（入力は小数1〜2桁）。"""
    return round(min(high, max(low, value)), 2)


def normalize_adjustment_margin_mm(value: object) -> float:
    """余白補正 mm を -20.0〜+20.0 に丸める。"""
    return _clamp_round(
        _normalize_float(value, DEFAULT_ADJUSTMENT_MARGIN_MM),
        ADJUSTMENT_MARGIN_MIN_MM,
        ADJUSTMENT_MARGIN_MAX_MM,
    )


def normalize_adjustment_offset_mm(value: object) -> float:
    """位置補正 mm を -20.0〜+20.0 に丸める。"""
    return _clamp_round(
        _normalize_float(value, DEFAULT_ADJUSTMENT_OFFSET_MM),
        ADJUSTMENT_OFFSET_MIN_MM,
        ADJUSTMENT_OFFSET_MAX_MM,
    )


def normalize_adjustment_scale_percent(value: object) -> float:
    """横倍率/縦倍率 % を 95.0〜105.0 に丸める。"""
    return _clamp_round(
        _normalize_float(value, DEFAULT_ADJUSTMENT_SCALE_PERCENT),
        ADJUSTMENT_SCALE_MIN_PERCENT,
        ADJUSTMENT_SCALE_MAX_PERCENT,
    )


def print_adjustment_summary_text(settings: "VoucherPrinterSettings") -> str:
    """印刷補正の現在内容を画面表示用に整形する（OFF時は簡潔表示）。"""
    if not bool(getattr(settings, "print_adjustment_enabled", False)):
        return "印刷補正:\nOFF"
    return (
        "印刷補正:\nON\n"
        f"左 {settings.print_adjustment_margin_left_mm:.1f}mm / "
        f"右 {settings.print_adjustment_margin_right_mm:.1f}mm / "
        f"上 {settings.print_adjustment_margin_top_mm:.1f}mm / "
        f"下 {settings.print_adjustment_margin_bottom_mm:.1f}mm\n"
        f"横倍率 {settings.print_adjustment_scale_x_percent:.1f}% / "
        f"縦倍率 {settings.print_adjustment_scale_y_percent:.1f}%\n"
        f"横位置 {settings.print_adjustment_offset_x_mm:+.1f}mm / "
        f"縦位置 {settings.print_adjustment_offset_y_mm:+.1f}mm"
    )


def normalize_acrobat_close_delay_seconds(value: object) -> int:
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_ACROBAT_CLOSE_DELAY_SECONDS
    return min(60, max(5, seconds))


def normalize_acrobat_hide_watch_seconds(value: object) -> int:
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_ACROBAT_HIDE_WATCH_SECONDS
    return min(30, max(1, seconds))


def default_voucher_printer_settings(printer_name: str = "") -> VoucherPrinterSettings:
    return VoucherPrinterSettings(printer_name=str(printer_name or "").strip())


def _path_is_nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _default_sumatra_path_object() -> Path:
    return Path(DEFAULT_SUMATRA_PATH)


def _should_skip_default_sumatra_restore_on_this_platform(path: Path) -> bool:
    text = str(path)
    return os.name != "nt" and (text.startswith("C:/") or text.startswith("C:\\"))


def _sumatra_bundle_restore_candidates() -> list[Path]:
    """旧API互換。ポータブル版の復旧元は廃止済み。"""
    return []


def ensure_default_sumatra_executable() -> bool:
    """旧API互換。ファイルはコピーせずインストール済み状態だけを確認する。"""
    logger = logging.getLogger("tks_to_kintone_app")
    try:
        from app.sumatra_detection import find_installed_sumatra_pdf_exe

        path, source = find_installed_sumatra_pdf_exe()
    except Exception as exc:  # noqa: BLE001
        logger.warning("voucher_print_sumatra_detection_failed error=%s", exc)
        return False
    if path:
        logger.info("voucher_print_sumatra_installed path=%s source=%s", path, source)
        return True
    logger.warning("voucher_print_sumatra_not_installed")
    return False


def normalize_sumatra_path(value: object) -> str:
    """明示設定を保持する。空欄は実行時の自動検出を表す。"""
    text = str(value or "").strip()
    return text


def _load_print_backend(store: QSettings) -> str:
    """印刷方式を読み込む。保存済みならそれを尊重し、未設定なら環境既定を使う。

    - 既存環境（保存済み backend あり）→ 保存値を優先。
    - 新規環境（未設定）→ SumatraPDF 経由。
    """
    if store.contains(VOUCHER_PRINT_BACKEND):
        return normalize_print_backend(store.value(VOUCHER_PRINT_BACKEND, DEFAULT_PRINT_BACKEND))
    return default_print_backend_for_environment()


def print_backend_default_source(settings: QSettings | None = None) -> str:
    """印刷方式の由来をログ用に返す。"""
    store = settings or _qsettings()
    if store.contains(VOUCHER_PRINT_BACKEND):
        return "saved"
    return "default_sumatra"


def load_voucher_printer_settings(settings: QSettings | None = None) -> VoucherPrinterSettings:
    """伝票の即時印刷設定を QSettings から読み込む。"""
    store = settings or _qsettings()
    return VoucherPrinterSettings(
        printer_name=str(store.value(VOUCHER_PRINT_PRINTER_NAME, "") or "").strip(),
        paper_size=normalize_print_paper_size(
            store.value(VOUCHER_PRINT_PAPER_SIZE, DEFAULT_PRINT_PAPER_SIZE)
        ),
        orientation=normalize_print_orientation(
            store.value(VOUCHER_PRINT_ORIENTATION, DEFAULT_PRINT_ORIENTATION)
        ),
        color_mode=normalize_print_color_mode(
            store.value(VOUCHER_PRINT_COLOR_MODE, DEFAULT_PRINT_COLOR_MODE)
        ),
        copies=normalize_print_copies(
            store.value(VOUCHER_PRINT_COPIES, DEFAULT_PRINT_COPIES)
        ),
        scale_mode=normalize_print_scale_mode(
            store.value(VOUCHER_PRINT_SCALE_MODE, DEFAULT_PRINT_SCALE_MODE)
        ),
        print_backend=_load_print_backend(store),
        acrobat_path=str(store.value(VOUCHER_PRINT_ACROBAT_PATH, "") or "").strip(),
        acrobat_hide_window=normalize_bool_setting(
            store.value(VOUCHER_PRINT_ACROBAT_HIDE_WINDOW, DEFAULT_ACROBAT_HIDE_WINDOW),
            DEFAULT_ACROBAT_HIDE_WINDOW,
        ),
        acrobat_close_after_print=normalize_bool_setting(
            store.value(VOUCHER_PRINT_ACROBAT_CLOSE_AFTER_PRINT, DEFAULT_ACROBAT_CLOSE_AFTER_PRINT),
            DEFAULT_ACROBAT_CLOSE_AFTER_PRINT,
        ),
        acrobat_close_delay_seconds=normalize_acrobat_close_delay_seconds(
            store.value(VOUCHER_PRINT_ACROBAT_CLOSE_DELAY_SECONDS, DEFAULT_ACROBAT_CLOSE_DELAY_SECONDS)
        ),
        acrobat_allow_force_kill=normalize_bool_setting(
            store.value(VOUCHER_PRINT_ACROBAT_ALLOW_FORCE_KILL, DEFAULT_ACROBAT_ALLOW_FORCE_KILL),
            DEFAULT_ACROBAT_ALLOW_FORCE_KILL,
        ),
        acrobat_hide_watch_enabled=normalize_bool_setting(
            store.value(VOUCHER_PRINT_ACROBAT_HIDE_WATCH_ENABLED, DEFAULT_ACROBAT_HIDE_WATCH_ENABLED),
            DEFAULT_ACROBAT_HIDE_WATCH_ENABLED,
        ),
        acrobat_hide_watch_seconds=normalize_acrobat_hide_watch_seconds(
            store.value(VOUCHER_PRINT_ACROBAT_HIDE_WATCH_SECONDS, DEFAULT_ACROBAT_HIDE_WATCH_SECONDS)
        ),
        sumatra_path=normalize_sumatra_path(
            store.value(VOUCHER_PRINT_SUMATRA_PATH, DEFAULT_SUMATRA_PATH)
        ),
        sumatra_print_settings=normalize_sumatra_print_settings(
            store.value(VOUCHER_PRINT_SUMATRA_SETTINGS, DEFAULT_SUMATRA_PRINT_SETTINGS)
        ),
        sumatra_paperkind=normalize_sumatra_paperkind(
            store.value(VOUCHER_PRINT_SUMATRA_PAPERKIND, DEFAULT_SUMATRA_PAPERKIND)
        ),
        sumatra_profile_name=str(
            store.value(VOUCHER_PRINT_SUMATRA_PROFILE_NAME, DEFAULT_SUMATRA_PROFILE_NAME) or ""
        ).strip() or DEFAULT_SUMATRA_PROFILE_NAME,
        sumatra_scaling_mode=normalize_sumatra_scaling_mode(
            store.value(VOUCHER_PRINT_SUMATRA_SCALING_MODE, DEFAULT_SUMATRA_SCALING_MODE)
        ),
        sumatra_paper_mode=normalize_sumatra_paper_mode(
            store.value(VOUCHER_PRINT_SUMATRA_PAPER_MODE, DEFAULT_SUMATRA_PAPER_MODE)
        ),
        sumatra_paper_value=str(
            store.value(VOUCHER_PRINT_SUMATRA_PAPER_VALUE, DEFAULT_SUMATRA_PAPER_VALUE) or ""
        ).strip(),
        sumatra_monochrome=normalize_bool_setting(
            store.value(VOUCHER_PRINT_SUMATRA_MONOCHROME, DEFAULT_SUMATRA_MONOCHROME),
            DEFAULT_SUMATRA_MONOCHROME,
        ),
        sumatra_center=normalize_bool_setting(
            store.value(VOUCHER_PRINT_SUMATRA_CENTER, DEFAULT_SUMATRA_CENTER),
            DEFAULT_SUMATRA_CENTER,
        ),
        sumatra_auto_rotation=normalize_bool_setting(
            store.value(VOUCHER_PRINT_SUMATRA_AUTO_ROTATION, DEFAULT_SUMATRA_AUTO_ROTATION),
            DEFAULT_SUMATRA_AUTO_ROTATION,
        ),
        sumatra_bin=normalize_sumatra_bin(
            store.value(VOUCHER_PRINT_SUMATRA_BIN, DEFAULT_SUMATRA_BIN)
        ),
        sumatra_extra_options=str(
            store.value(VOUCHER_PRINT_SUMATRA_EXTRA_OPTIONS, DEFAULT_SUMATRA_EXTRA_OPTIONS) or ""
        ).strip(),
        sumatra_wait_timeout_seconds=normalize_sumatra_wait_timeout_seconds(
            store.value(
                VOUCHER_PRINT_SUMATRA_WAIT_TIMEOUT_SECONDS, DEFAULT_SUMATRA_WAIT_TIMEOUT_SECONDS
            )
        ),
        sumatra_allow_force_kill=normalize_bool_setting(
            store.value(VOUCHER_PRINT_SUMATRA_ALLOW_FORCE_KILL, DEFAULT_SUMATRA_ALLOW_FORCE_KILL),
            DEFAULT_SUMATRA_ALLOW_FORCE_KILL,
        ),
        print_adjustment_enabled=normalize_bool_setting(
            store.value(VOUCHER_PRINT_ADJUSTMENT_ENABLED, DEFAULT_ADJUSTMENT_ENABLED),
            DEFAULT_ADJUSTMENT_ENABLED,
        ),
        print_adjustment_margin_left_mm=normalize_adjustment_margin_mm(
            store.value(VOUCHER_PRINT_ADJUSTMENT_MARGIN_LEFT_MM, DEFAULT_ADJUSTMENT_MARGIN_LEFT_MM)
        ),
        print_adjustment_margin_right_mm=normalize_adjustment_margin_mm(
            store.value(VOUCHER_PRINT_ADJUSTMENT_MARGIN_RIGHT_MM, DEFAULT_ADJUSTMENT_MARGIN_RIGHT_MM)
        ),
        print_adjustment_margin_top_mm=normalize_adjustment_margin_mm(
            store.value(VOUCHER_PRINT_ADJUSTMENT_MARGIN_TOP_MM, DEFAULT_ADJUSTMENT_MARGIN_TOP_MM)
        ),
        print_adjustment_margin_bottom_mm=normalize_adjustment_margin_mm(
            store.value(VOUCHER_PRINT_ADJUSTMENT_MARGIN_BOTTOM_MM, DEFAULT_ADJUSTMENT_MARGIN_BOTTOM_MM)
        ),
        print_adjustment_scale_x_percent=normalize_adjustment_scale_percent(
            store.value(VOUCHER_PRINT_ADJUSTMENT_SCALE_X_PERCENT, DEFAULT_ADJUSTMENT_SCALE_PERCENT)
        ),
        print_adjustment_scale_y_percent=normalize_adjustment_scale_percent(
            store.value(VOUCHER_PRINT_ADJUSTMENT_SCALE_Y_PERCENT, DEFAULT_ADJUSTMENT_SCALE_PERCENT)
        ),
        print_adjustment_offset_x_mm=normalize_adjustment_offset_mm(
            store.value(VOUCHER_PRINT_ADJUSTMENT_OFFSET_X_MM, DEFAULT_ADJUSTMENT_OFFSET_MM)
        ),
        print_adjustment_offset_y_mm=normalize_adjustment_offset_mm(
            store.value(VOUCHER_PRINT_ADJUSTMENT_OFFSET_Y_MM, DEFAULT_ADJUSTMENT_OFFSET_MM)
        ),
        print_adjustment_save_pdf=normalize_bool_setting(
            store.value(VOUCHER_PRINT_ADJUSTMENT_SAVE_PDF, DEFAULT_ADJUSTMENT_SAVE_PDF),
            DEFAULT_ADJUSTMENT_SAVE_PDF,
        ),
        save_pdf_on_print=normalize_bool_setting(
            store.value(VOUCHER_PRINT_SAVE_PDF_ON_PRINT, DEFAULT_SAVE_PDF_ON_PRINT),
            DEFAULT_SAVE_PDF_ON_PRINT,
        ),
        show_pdf_created_dialog=normalize_bool_setting(
            store.value(VOUCHER_SHOW_PDF_CREATED_DIALOG, DEFAULT_SHOW_PDF_CREATED_DIALOG),
            DEFAULT_SHOW_PDF_CREATED_DIALOG,
        ),
        open_pdf_after_create=normalize_bool_setting(
            store.value(VOUCHER_OPEN_PDF_AFTER_CREATE, DEFAULT_OPEN_PDF_AFTER_CREATE),
            DEFAULT_OPEN_PDF_AFTER_CREATE,
        ),
    )


def save_voucher_printer_settings(
    values: VoucherPrinterSettings, settings: QSettings | None = None
) -> None:
    """伝票の即時印刷設定を QSettings へ保存する。"""
    store = settings or _qsettings()
    normalized = VoucherPrinterSettings(
        printer_name=str(values.printer_name or "").strip(),
        paper_size=normalize_print_paper_size(values.paper_size),
        orientation=normalize_print_orientation(values.orientation),
        color_mode=normalize_print_color_mode(values.color_mode),
        copies=normalize_print_copies(values.copies),
        scale_mode=normalize_print_scale_mode(values.scale_mode),
        print_backend=normalize_print_backend(values.print_backend),
        acrobat_path=str(values.acrobat_path or "").strip(),
        acrobat_hide_window=normalize_bool_setting(
            values.acrobat_hide_window, DEFAULT_ACROBAT_HIDE_WINDOW
        ),
        acrobat_close_after_print=normalize_bool_setting(
            values.acrobat_close_after_print, DEFAULT_ACROBAT_CLOSE_AFTER_PRINT
        ),
        acrobat_close_delay_seconds=normalize_acrobat_close_delay_seconds(
            values.acrobat_close_delay_seconds
        ),
        acrobat_allow_force_kill=normalize_bool_setting(
            values.acrobat_allow_force_kill, DEFAULT_ACROBAT_ALLOW_FORCE_KILL
        ),
        acrobat_hide_watch_enabled=normalize_bool_setting(
            values.acrobat_hide_watch_enabled, DEFAULT_ACROBAT_HIDE_WATCH_ENABLED
        ),
        acrobat_hide_watch_seconds=normalize_acrobat_hide_watch_seconds(
            values.acrobat_hide_watch_seconds
        ),
        sumatra_path=normalize_sumatra_path(values.sumatra_path),
        sumatra_print_settings=normalize_sumatra_print_settings(values.sumatra_print_settings),
        sumatra_paperkind=normalize_sumatra_paperkind(values.sumatra_paperkind),
        sumatra_profile_name=str(values.sumatra_profile_name or "").strip() or DEFAULT_SUMATRA_PROFILE_NAME,
        sumatra_scaling_mode=normalize_sumatra_scaling_mode(values.sumatra_scaling_mode),
        sumatra_paper_mode=normalize_sumatra_paper_mode(values.sumatra_paper_mode),
        sumatra_paper_value=str(values.sumatra_paper_value or "").strip(),
        sumatra_monochrome=normalize_bool_setting(values.sumatra_monochrome, DEFAULT_SUMATRA_MONOCHROME),
        sumatra_center=normalize_bool_setting(values.sumatra_center, DEFAULT_SUMATRA_CENTER),
        sumatra_auto_rotation=normalize_bool_setting(values.sumatra_auto_rotation, DEFAULT_SUMATRA_AUTO_ROTATION),
        sumatra_bin=normalize_sumatra_bin(values.sumatra_bin),
        sumatra_extra_options=str(values.sumatra_extra_options or "").strip(),
        sumatra_wait_timeout_seconds=normalize_sumatra_wait_timeout_seconds(
            values.sumatra_wait_timeout_seconds
        ),
        sumatra_allow_force_kill=normalize_bool_setting(
            values.sumatra_allow_force_kill, DEFAULT_SUMATRA_ALLOW_FORCE_KILL
        ),
        print_adjustment_enabled=normalize_bool_setting(
            values.print_adjustment_enabled, DEFAULT_ADJUSTMENT_ENABLED
        ),
        print_adjustment_margin_left_mm=normalize_adjustment_margin_mm(
            values.print_adjustment_margin_left_mm
        ),
        print_adjustment_margin_right_mm=normalize_adjustment_margin_mm(
            values.print_adjustment_margin_right_mm
        ),
        print_adjustment_margin_top_mm=normalize_adjustment_margin_mm(
            values.print_adjustment_margin_top_mm
        ),
        print_adjustment_margin_bottom_mm=normalize_adjustment_margin_mm(
            values.print_adjustment_margin_bottom_mm
        ),
        print_adjustment_scale_x_percent=normalize_adjustment_scale_percent(
            values.print_adjustment_scale_x_percent
        ),
        print_adjustment_scale_y_percent=normalize_adjustment_scale_percent(
            values.print_adjustment_scale_y_percent
        ),
        print_adjustment_offset_x_mm=normalize_adjustment_offset_mm(
            values.print_adjustment_offset_x_mm
        ),
        print_adjustment_offset_y_mm=normalize_adjustment_offset_mm(
            values.print_adjustment_offset_y_mm
        ),
        print_adjustment_save_pdf=normalize_bool_setting(
            values.print_adjustment_save_pdf, DEFAULT_ADJUSTMENT_SAVE_PDF
        ),
        save_pdf_on_print=normalize_bool_setting(
            values.save_pdf_on_print, DEFAULT_SAVE_PDF_ON_PRINT
        ),
        show_pdf_created_dialog=normalize_bool_setting(
            values.show_pdf_created_dialog, DEFAULT_SHOW_PDF_CREATED_DIALOG
        ),
        open_pdf_after_create=normalize_bool_setting(
            values.open_pdf_after_create, DEFAULT_OPEN_PDF_AFTER_CREATE
        ),
    )
    store.setValue(VOUCHER_PRINT_PRINTER_NAME, normalized.printer_name)
    store.setValue(VOUCHER_PRINT_PAPER_SIZE, normalized.paper_size)
    store.setValue(VOUCHER_PRINT_ORIENTATION, normalized.orientation)
    store.setValue(VOUCHER_PRINT_COLOR_MODE, normalized.color_mode)
    store.setValue(VOUCHER_PRINT_COPIES, normalized.copies)
    store.setValue(VOUCHER_PRINT_SCALE_MODE, normalized.scale_mode)
    store.setValue(VOUCHER_PRINT_BACKEND, normalized.print_backend)
    store.setValue(VOUCHER_PRINT_ACROBAT_PATH, normalized.acrobat_path)
    store.setValue(VOUCHER_PRINT_ACROBAT_HIDE_WINDOW, normalized.acrobat_hide_window)
    store.setValue(VOUCHER_PRINT_ACROBAT_CLOSE_AFTER_PRINT, normalized.acrobat_close_after_print)
    store.setValue(VOUCHER_PRINT_ACROBAT_CLOSE_DELAY_SECONDS, normalized.acrobat_close_delay_seconds)
    store.setValue(VOUCHER_PRINT_ACROBAT_ALLOW_FORCE_KILL, normalized.acrobat_allow_force_kill)
    store.setValue(VOUCHER_PRINT_ACROBAT_HIDE_WATCH_ENABLED, normalized.acrobat_hide_watch_enabled)
    store.setValue(VOUCHER_PRINT_ACROBAT_HIDE_WATCH_SECONDS, normalized.acrobat_hide_watch_seconds)
    store.setValue(VOUCHER_PRINT_SUMATRA_PATH, normalized.sumatra_path)
    store.setValue(VOUCHER_PRINT_SUMATRA_SETTINGS, normalized.sumatra_print_settings)
    store.setValue(VOUCHER_PRINT_SUMATRA_PAPERKIND, normalized.sumatra_paperkind)
    store.setValue(VOUCHER_PRINT_SUMATRA_PROFILE_NAME, normalized.sumatra_profile_name)
    store.setValue(VOUCHER_PRINT_SUMATRA_SCALING_MODE, normalized.sumatra_scaling_mode)
    store.setValue(VOUCHER_PRINT_SUMATRA_PAPER_MODE, normalized.sumatra_paper_mode)
    store.setValue(VOUCHER_PRINT_SUMATRA_PAPER_VALUE, normalized.sumatra_paper_value)
    store.setValue(VOUCHER_PRINT_SUMATRA_MONOCHROME, normalized.sumatra_monochrome)
    store.setValue(VOUCHER_PRINT_SUMATRA_CENTER, normalized.sumatra_center)
    store.setValue(VOUCHER_PRINT_SUMATRA_AUTO_ROTATION, normalized.sumatra_auto_rotation)
    store.setValue(VOUCHER_PRINT_SUMATRA_BIN, normalized.sumatra_bin)
    store.setValue(VOUCHER_PRINT_SUMATRA_EXTRA_OPTIONS, normalized.sumatra_extra_options)
    store.setValue(
        VOUCHER_PRINT_SUMATRA_WAIT_TIMEOUT_SECONDS, normalized.sumatra_wait_timeout_seconds
    )
    store.setValue(VOUCHER_PRINT_SUMATRA_ALLOW_FORCE_KILL, normalized.sumatra_allow_force_kill)
    store.setValue(VOUCHER_PRINT_ADJUSTMENT_ENABLED, normalized.print_adjustment_enabled)
    store.setValue(VOUCHER_PRINT_ADJUSTMENT_MARGIN_LEFT_MM, normalized.print_adjustment_margin_left_mm)
    store.setValue(VOUCHER_PRINT_ADJUSTMENT_MARGIN_RIGHT_MM, normalized.print_adjustment_margin_right_mm)
    store.setValue(VOUCHER_PRINT_ADJUSTMENT_MARGIN_TOP_MM, normalized.print_adjustment_margin_top_mm)
    store.setValue(VOUCHER_PRINT_ADJUSTMENT_MARGIN_BOTTOM_MM, normalized.print_adjustment_margin_bottom_mm)
    store.setValue(VOUCHER_PRINT_ADJUSTMENT_SCALE_X_PERCENT, normalized.print_adjustment_scale_x_percent)
    store.setValue(VOUCHER_PRINT_ADJUSTMENT_SCALE_Y_PERCENT, normalized.print_adjustment_scale_y_percent)
    store.setValue(VOUCHER_PRINT_ADJUSTMENT_OFFSET_X_MM, normalized.print_adjustment_offset_x_mm)
    store.setValue(VOUCHER_PRINT_ADJUSTMENT_OFFSET_Y_MM, normalized.print_adjustment_offset_y_mm)
    store.setValue(VOUCHER_PRINT_ADJUSTMENT_SAVE_PDF, normalized.print_adjustment_save_pdf)
    store.setValue(VOUCHER_PRINT_SAVE_PDF_ON_PRINT, normalized.save_pdf_on_print)
    store.setValue(VOUCHER_SHOW_PDF_CREATED_DIALOG, normalized.show_pdf_created_dialog)
    store.setValue(VOUCHER_OPEN_PDF_AFTER_CREATE, normalized.open_pdf_after_create)
    store.sync()


def default_sumatra_print_profiles() -> list[SumatraPrintProfile]:
    presets = [
        ("標準", DEFAULT_SUMATRA_PRINT_SETTINGS, ""),
        ("B5 paperkind", f"noscale,monochrome,paperkind={SUMATRA_B5_PAPERKIND_PLACEHOLDER},bin=auto,center", SUMATRA_B5_PAPERKIND_PLACEHOLDER),
        ("noscale 自動回転なし", "noscale,monochrome,paper=auto,bin=auto,center,disable-auto-rotation", ""),
        ("fit", "fit,monochrome,paper=auto,bin=auto,center", ""),
        ("shrink", "shrink,monochrome,paper=auto,bin=auto,center", ""),
        ("ユーザー定義1", DEFAULT_SUMATRA_PRINT_SETTINGS, ""),
        ("ユーザー定義2", DEFAULT_SUMATRA_PRINT_SETTINGS, ""),
    ]
    now = datetime.now().isoformat(timespec="seconds")
    return [
        SumatraPrintProfile(profile_name=name, print_settings=settings, paperkind=paperkind, memo="", updated_at=now)
        for name, settings, paperkind in presets
    ]


def load_sumatra_print_profiles(settings: QSettings | None = None) -> list[SumatraPrintProfile]:
    store = settings or _qsettings()
    raw = str(store.value(VOUCHER_PRINT_SUMATRA_PROFILES, "") or "")
    if not raw:
        return default_sumatra_print_profiles()
    try:
        payload = json.loads(raw)
    except Exception:
        return default_sumatra_print_profiles()
    profiles: list[SumatraPrintProfile] = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("profile_name", "") or "").strip()
            if not name:
                continue
            profiles.append(
                SumatraPrintProfile(
                    profile_name=name,
                    print_settings=normalize_sumatra_print_settings(item.get("print_settings", "")),
                    paperkind=normalize_sumatra_paperkind(item.get("paperkind", "")),
                    memo=str(item.get("memo", "") or ""),
                    updated_at=str(item.get("updated_at", "") or ""),
                    adjustment_enabled=normalize_bool_setting(
                        item.get("adjustment_enabled", DEFAULT_ADJUSTMENT_ENABLED),
                        DEFAULT_ADJUSTMENT_ENABLED,
                    ),
                    margin_left_mm=normalize_adjustment_margin_mm(item.get("margin_left_mm", 0.0)),
                    margin_right_mm=normalize_adjustment_margin_mm(item.get("margin_right_mm", 0.0)),
                    margin_top_mm=normalize_adjustment_margin_mm(item.get("margin_top_mm", 0.0)),
                    margin_bottom_mm=normalize_adjustment_margin_mm(item.get("margin_bottom_mm", 0.0)),
                    scale_x_percent=normalize_adjustment_scale_percent(
                        item.get("scale_x_percent", DEFAULT_ADJUSTMENT_SCALE_PERCENT)
                    ),
                    scale_y_percent=normalize_adjustment_scale_percent(
                        item.get("scale_y_percent", DEFAULT_ADJUSTMENT_SCALE_PERCENT)
                    ),
                    offset_x_mm=normalize_adjustment_offset_mm(item.get("offset_x_mm", 0.0)),
                    offset_y_mm=normalize_adjustment_offset_mm(item.get("offset_y_mm", 0.0)),
                )
            )
    return profiles or default_sumatra_print_profiles()


def save_sumatra_print_profiles(
    profiles: list[SumatraPrintProfile], settings: QSettings | None = None
) -> None:
    store = settings or _qsettings()
    payload = [
        {
            "profile_name": str(profile.profile_name or "").strip(),
            "print_settings": normalize_sumatra_print_settings(profile.print_settings),
            "paperkind": normalize_sumatra_paperkind(profile.paperkind),
            "memo": str(profile.memo or ""),
            "updated_at": str(profile.updated_at or "") or datetime.now().isoformat(timespec="seconds"),
            "adjustment_enabled": bool(profile.adjustment_enabled),
            "margin_left_mm": normalize_adjustment_margin_mm(profile.margin_left_mm),
            "margin_right_mm": normalize_adjustment_margin_mm(profile.margin_right_mm),
            "margin_top_mm": normalize_adjustment_margin_mm(profile.margin_top_mm),
            "margin_bottom_mm": normalize_adjustment_margin_mm(profile.margin_bottom_mm),
            "scale_x_percent": normalize_adjustment_scale_percent(profile.scale_x_percent),
            "scale_y_percent": normalize_adjustment_scale_percent(profile.scale_y_percent),
            "offset_x_mm": normalize_adjustment_offset_mm(profile.offset_x_mm),
            "offset_y_mm": normalize_adjustment_offset_mm(profile.offset_y_mm),
        }
        for profile in profiles
        if str(profile.profile_name or "").strip()
    ]
    store.setValue(VOUCHER_PRINT_SUMATRA_PROFILES, json.dumps(payload, ensure_ascii=False))
    store.sync()
