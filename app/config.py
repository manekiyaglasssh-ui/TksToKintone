from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

from dotenv import dotenv_values

from app.cleanup_service import normalize_retention_days
from app.models import AppConfig, AppPaths
from app.path_utils import VOUCHER_OUTPUT_DIR_ENV_KEY, get_app_data_dir

_LOGGER = logging.getLogger("tks_to_kintone_app")

_OP_FIELDS_KEY = "TKS_VOUCHER_OLAP_ENABLED_OP_FIELDS"
_OP_FIELDS_APPEND_BLOCK = (
    "\n"
    "# 伝票作成・印刷用 OLAP追加項目\n"
    "# OP区分 と 商品コード のみOLAPから取得します。\n"
    "# 02時平米 / 02時総平米 / 00時ケース・ロット平米 はアプリ側で計算するため、OLAPリクエストには含めません。\n"
    "TKS_VOUCHER_OLAP_ENABLED_OP_FIELDS=OP区分,商品コード\n"
)


class ConfigError(RuntimeError):
    pass


CUSTOMER_LABEL_MAX_LEN = 20

CUSTOMER_LABEL_DEFAULTS: dict[str, str] = {
    "得意先1": "得意先1",
    "得意先2": "得意先2",
    "得意先3": "得意先3",
    "得意先4": "得意先4",
}

_INTERNAL_TO_ENV_KEY: dict[str, str] = {
    "得意先1": "CUSTOMER_LABEL_1",
    "得意先2": "CUSTOMER_LABEL_2",
    "得意先3": "CUSTOMER_LABEL_3",
    "得意先4": "CUSTOMER_LABEL_4",
}
_ENV_TO_INTERNAL_KEY: dict[str, str] = {v: k for k, v in _INTERNAL_TO_ENV_KEY.items()}
_INTERNAL_TO_MATCH_ENV_KEY: dict[str, str] = {
    "得意先1": "CUSTOMER_MATCH_1",
    "得意先2": "CUSTOMER_MATCH_2",
    "得意先3": "CUSTOMER_MATCH_3",
    "得意先4": "CUSTOMER_MATCH_4",
}
_MATCH_ENV_TO_INTERNAL_KEY: dict[str, str] = {v: k for k, v in _INTERNAL_TO_MATCH_ENV_KEY.items()}

KINTONE_LOGIN_ID_ENV_KEY = "KINTONE_LOGIN_ID"
KINTONE_PASSWORD_ENV_KEY = "KINTONE_PASSWORD"


def validate_customer_label(value: str) -> str | None:
    """得意先表示名を検証してエラーメッセージを返す。問題なければ None。"""
    if len(value) > CUSTOMER_LABEL_MAX_LEN:
        return f"{CUSTOMER_LABEL_MAX_LEN}文字以内で入力してください（現在{len(value)}文字）"
    return None


def update_customer_labels_in_config(
    config_path: Path,
    labels: dict[str, str],
    match_patterns: dict[str, str] | None = None,
) -> None:
    """config.env の CUSTOMER_LABEL_1〜4 を更新または末尾に追記する。

    - 既存の他のキーはそのまま保持する
    - 対象キーが存在すれば値だけ上書きする
    - 存在しなければ末尾に追記する
    - コメント行（# で始まる行）は変更しない
    """
    if config_path.exists():
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
    else:
        lines = []

    updated_env_keys: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if "=" in stripped:
            env_key = stripped.split("=", 1)[0].strip()
            if env_key in _ENV_TO_INTERNAL_KEY or env_key in _MATCH_ENV_TO_INTERNAL_KEY:
                if env_key in _ENV_TO_INTERNAL_KEY:
                    internal_key = _ENV_TO_INTERNAL_KEY[env_key]
                    value = labels.get(internal_key) or CUSTOMER_LABEL_DEFAULTS[internal_key]
                else:
                    internal_key = _MATCH_ENV_TO_INTERNAL_KEY[env_key]
                    value = (match_patterns or {}).get(internal_key, "")
                new_lines.append(f"{env_key}={value}\n")
                updated_env_keys.add(env_key)
                continue
        new_lines.append(line)

    for internal_key, env_key in _INTERNAL_TO_ENV_KEY.items():
        if env_key not in updated_env_keys:
            value = labels.get(internal_key) or CUSTOMER_LABEL_DEFAULTS[internal_key]
            new_lines.append(f"{env_key}={value}\n")
    for internal_key, env_key in _INTERNAL_TO_MATCH_ENV_KEY.items():
        if env_key not in updated_env_keys:
            value = (match_patterns or {}).get(internal_key, "")
            new_lines.append(f"{env_key}={value}\n")

    config_path.write_text("".join(new_lines), encoding="utf-8")


def update_values_in_config(config_path: Path, values: dict[str, str]) -> None:
    """config.env の指定キーを更新または末尾に追記する。"""
    if config_path.exists():
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
    else:
        lines = []

    remaining = dict(values)
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        env_key = stripped.split("=", 1)[0].strip()
        if env_key in remaining:
            new_lines.append(f"{env_key}={remaining.pop(env_key)}\n")
        else:
            new_lines.append(line)

    for env_key, value in remaining.items():
        new_lines.append(f"{env_key}={value}\n")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("".join(new_lines), encoding="utf-8")


def user_config_path() -> Path:
    """ProgramData 配下の config.env パスを返し、sample から未作成分を補完する。"""
    config_path = default_base_dir() / "config.env"
    _ensure_from_sample(config_path, resource_path("templates/config.env.sample"))
    return config_path


COMMON_REQUIRED_KEYS = (
    "TKS_COMPANY_CODE",
    "KINTONE_DOMAIN",
    "KINTONE_APP_ID",
    "KINTONE_API_TOKEN",
)

MOCK_REQUIRED_KEYS = (
    "TKS_KAKOU_CSV_URL",
    "TKS_SOBA_CSV_URL",
)

HTTP_REQUIRED_KEYS = (
    "TKS_BASE_URL",
    "TKS_LOGIN_AUTH_KBN",
    "TKS_TERMINAL_ID",
    "TKS_COMPUTER_NAME",
    "TKS_IP_ADDRESS",
    "TKS_SCREEN_NAME",
    "TKS_KAKOU_REQUEST_TEMPLATE",
    "TKS_SOBA_REQUEST_TEMPLATE",
)


def resource_path(relative_path: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return root / relative_path


def default_base_dir() -> Path:
    return get_app_data_dir()


# ProgramData 側に配置する OLAPリクエストテンプレート。
OLAP_TEMPLATE_NAMES: tuple[str, ...] = (
    "kakou_request_template.json",
    "soba_request_template.json",
)


def olap_template_source_dirs() -> list[Path]:
    """アプリ同梱側 docs/olap の候補ディレクトリ（探索順）。

    PyInstaller実行時（_MEIPASS / _internal 配下）と開発環境の両方で
    参照できるようにする。
    """
    return [
        resource_path("docs/olap"),
        resource_path("_internal/docs/olap"),
        Path(__file__).resolve().parents[1] / "docs" / "olap",
    ]


def find_bundled_olap_template(name: str) -> Path | None:
    """同梱側 docs/olap から指定テンプレートを探す。見つからなければ None。"""
    for source_dir in olap_template_source_dirs():
        candidate = source_dir / name
        if candidate.exists():
            return candidate
    return None


def _should_copy_template(source: Path, target: Path) -> bool:
    """ProgramData側へコピーすべきか判定する。

    - 配置先が無ければコピーする
    - 同名でも同梱側の方が新しければ上書きする
    """
    if not target.exists():
        return True
    try:
        return source.stat().st_mtime > target.stat().st_mtime
    except OSError:
        return True


def olap_template_dir(base_dir: Path | None = None) -> Path:
    """ProgramData 側の docs/olap ディレクトリパスを返す。"""
    base = base_dir or default_base_dir()
    return base / "docs" / "olap"


def ensure_olap_templates_installed(base_dir: Path | None = None) -> Path:
    """ProgramData 側の docs/olap に最新の OLAPテンプレートを配置する。

    - docs/olap フォルダが無ければ作成する
    - 同梱テンプレートが ProgramData 側に無ければコピーする
    - 同名でも同梱側の方が新しければ上書きする
    - コピー元が見つからない場合は候補パスをすべてログに出す

    起動時・更新時の古いテンプレート削除後・OLAP取得直前に呼ぶことを想定。
    戻り値は ProgramData 側の docs/olap ディレクトリ。
    """
    target_dir = olap_template_dir(base_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    for name in OLAP_TEMPLATE_NAMES:
        target = target_dir / name
        source = find_bundled_olap_template(name)
        if source is None:
            searched = [str(directory / name) for directory in olap_template_source_dirs()]
            _LOGGER.warning(
                "OLAPテンプレートのコピー元が見つかりません: %s\n探索したパス:\n  %s",
                name,
                "\n  ".join(searched),
            )
            continue
        if _should_copy_template(source, target):
            shutil.copyfile(source, target)
            _LOGGER.info("OLAPテンプレートを配置しました: %s -> %s", source, target)
    return target_dir


def load_app_config() -> AppConfig:
    base_dir = default_base_dir()
    base_dir.mkdir(parents=True, exist_ok=True)

    # 起動時に必ず ProgramData 側の OLAPテンプレートを最新状態へ復旧する。
    # 更新時に古いテンプレートを削除しても、ここで同梱側から再配置される。
    ensure_olap_templates_installed(base_dir)

    config_path = base_dir / "config.env"
    mapping_path = base_dir / "field_mapping.json"
    created = _ensure_from_sample(config_path, resource_path("templates/config.env.sample"))
    _ensure_from_sample(mapping_path, resource_path("templates/field_mapping.json.sample"))
    appended = _ensure_op_fields_in_config(config_path)

    _LOGGER.info("config.env path=%s", config_path)
    _LOGGER.info("config.env created=%s", created)
    if appended:
        _LOGGER.info("config.env: %s を追記しました", _OP_FIELDS_KEY)

    values = dotenv_values(config_path)
    output_dir = str(values.get("OUTPUT_DIR") or "work")
    log_dir = str(values.get("LOG_DIR") or "logs")
    error_dir = str(values.get("ERROR_DIR") or "error")
    voucher_output_dir = str(values.get(VOUCHER_OUTPUT_DIR_ENV_KEY) or "").strip()

    paths = AppPaths(
        base_dir=base_dir,
        config_env=config_path,
        field_mapping_json=mapping_path,
        work_dir=_resolve_child(base_dir, output_dir),
        log_dir=_resolve_child(base_dir, log_dir),
        error_dir=_resolve_child(base_dir, error_dir),
        kakou_master_csv=base_dir / "kakou_master.csv",
        kakou_master_backup_dir=base_dir / "kakou_master_backup",
    )
    for directory in (paths.work_dir, paths.log_dir, paths.error_dir, paths.kakou_master_backup_dir):
        directory.mkdir(parents=True, exist_ok=True)

    from app.settings_service import ensure_default_initial_data

    ensure_default_initial_data(paths.kakou_master_csv, config_path)
    values = dotenv_values(config_path)

    tks_client_mode = str(values.get("TKS_CLIENT_MODE") or "mock").strip().lower()
    if tks_client_mode not in {"mock", "http"}:
        raise ConfigError(f"TKS_CLIENT_MODE は mock または http を指定してください: {tks_client_mode}")

    required_keys = [*COMMON_REQUIRED_KEYS, *(HTTP_REQUIRED_KEYS if tks_client_mode == "http" else MOCK_REQUIRED_KEYS)]
    missing = [key for key in required_keys if not values.get(key)]
    if missing:
        raise ConfigError(
            "config.env の設定が不足しています: "
            + ", ".join(missing)
            + f"\n設定ファイル: {config_path}"
        )

    customer_labels = {
        "得意先1": str(values.get("CUSTOMER_LABEL_1") or "得意先1"),
        "得意先2": str(values.get("CUSTOMER_LABEL_2") or "得意先2"),
        "得意先3": str(values.get("CUSTOMER_LABEL_3") or "得意先3"),
        "得意先4": str(values.get("CUSTOMER_LABEL_4") or "得意先4"),
    }
    customer_match_patterns = {
        "得意先1": str(values.get("CUSTOMER_MATCH_1") or ""),
        "得意先2": str(values.get("CUSTOMER_MATCH_2") or ""),
        "得意先3": str(values.get("CUSTOMER_MATCH_3") or ""),
        "得意先4": str(values.get("CUSTOMER_MATCH_4") or ""),
    }

    raw_preview_theme = str(values.get("PREVIEW_COLOR_THEME") or "light").strip().lower()
    preview_color_theme = raw_preview_theme if raw_preview_theme in {"auto", "light", "dark"} else "light"

    cfg = AppConfig(
        paths=paths,
        company_code=str(values["TKS_COMPANY_CODE"]),
        kintone_domain=str(values["KINTONE_DOMAIN"]).replace("https://", "").replace("http://", "").strip("/"),
        kintone_app_id=str(values["KINTONE_APP_ID"]),
        kintone_api_token=str(values["KINTONE_API_TOKEN"]),
        csv_encoding=str(values.get("CSV_ENCODING") or "cp932"),
        shukka_kbn_options=_split_options(str(values.get("SHUKKA_KBN_OPTIONS") or "AM,PM")),
        cleanup_retention_days=normalize_retention_days(values.get("CLEANUP_RETENTION_DAYS")),
        tks_client_mode=tks_client_mode,
        tks_base_url=str(values.get("TKS_BASE_URL") or "").rstrip("/"),
        tks_screen_name=str(values.get("TKS_SCREEN_NAME") or "0"),
        tks_login_auth_type=str(values.get("TKS_LOGIN_AUTH_KBN") or values.get("TKS_LOGIN_AUTH_TYPE") or "0"),
        tks_device_id=str(values.get("TKS_TERMINAL_ID") or values.get("TKS_DEVICE_ID") or ""),
        tks_computer_name=str(values.get("TKS_COMPUTER_NAME") or ""),
        tks_ip_address=str(values.get("TKS_IP_ADDRESS") or ""),
        tks_kakou_csv_url=str(values.get("TKS_KAKOU_CSV_URL") or ""),
        tks_soba_csv_url=str(values.get("TKS_SOBA_CSV_URL") or ""),
        tks_kakou_olap_output_layout=str(values.get("TKS_KAKOU_OLAP_OUTPUT_LAYOUT") or "0"),
        tks_kakou_olap_target_data=str(values.get("TKS_KAKOU_OLAP_TARGET_DATA") or ""),
        tks_kakou_request_template=_resolve_existing_template(str(values.get("TKS_KAKOU_REQUEST_TEMPLATE") or ""), base_dir),
        tks_soba_olap_output_layout=str(values.get("TKS_SOBA_OLAP_OUTPUT_LAYOUT") or "0"),
        tks_soba_olap_target_data=str(values.get("TKS_SOBA_OLAP_TARGET_DATA") or ""),
        tks_soba_request_template=_resolve_existing_template(str(values.get("TKS_SOBA_REQUEST_TEMPLATE") or ""), base_dir),
        tks_voucher_olap_disable_op_fields=_to_bool(str(values.get("TKS_VOUCHER_OLAP_DISABLE_OP_FIELDS") or "1")),
        tks_voucher_olap_enabled_op_fields=_split_options(str(values.get("TKS_VOUCHER_OLAP_ENABLED_OP_FIELDS") or "")),
        customer_labels=customer_labels,
        customer_match_patterns=customer_match_patterns,
        preview_color_theme=preview_color_theme,
        voucher_output_dir=Path(voucher_output_dir) if voucher_output_dir else None,
    )
    _LOGGER.info("%s=%s", _OP_FIELDS_KEY, ",".join(cfg.tks_voucher_olap_enabled_op_fields))
    return cfg


def _ensure_from_sample(target: Path, sample: Path) -> bool:
    if target.exists():
        return False
    if not sample.exists():
        raise ConfigError(f"sampleファイルが見つかりません: {sample}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sample, target)
    return True


def _ensure_op_fields_in_config(config_path: Path) -> bool:
    """config.env に TKS_VOUCHER_OLAP_ENABLED_OP_FIELDS が無ければ末尾に追記する。"""
    if not config_path.exists():
        return False
    text = config_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() == _OP_FIELDS_KEY:
            return False
    if text and not text.endswith("\n"):
        text += "\n"
    config_path.write_text(text + _OP_FIELDS_APPEND_BLOCK, encoding="utf-8")
    return True


def _resolve_child(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def _resolve_existing_template(value: str, base_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    candidates = [path] if path.is_absolute() else [base_dir / path, resource_path(value), Path.cwd() / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _split_options(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
