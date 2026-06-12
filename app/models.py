from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RunInput:
    company_code: str
    olap_login_id: str
    olap_password: str
    denpyo_numbers: list[str]
    shiage_date: str
    shukka_kbn: str


@dataclass(frozen=True)
class AppPaths:
    base_dir: Path
    config_env: Path
    field_mapping_json: Path
    work_dir: Path
    log_dir: Path
    error_dir: Path
    kakou_master_csv: Path = field(default_factory=lambda: Path("kakou_master.csv"))
    kakou_master_backup_dir: Path = field(default_factory=lambda: Path("kakou_master_backup"))


@dataclass(frozen=True)
class AppConfig:
    paths: AppPaths
    company_code: str
    kintone_domain: str
    kintone_app_id: str
    kintone_api_token: str
    csv_encoding: str
    shukka_kbn_options: list[str]
    cleanup_retention_days: int
    tks_client_mode: str = "mock"
    tks_base_url: str = ""
    tks_screen_name: str = "0"
    tks_login_auth_type: str = "0"
    tks_device_id: str = ""
    tks_computer_name: str = ""
    tks_ip_address: str = ""
    tks_kakou_csv_url: str = ""
    tks_soba_csv_url: str = ""
    tks_kakou_olap_output_layout: str = "0"
    tks_kakou_olap_target_data: str = ""
    tks_kakou_request_template: Path | None = None
    tks_soba_olap_output_layout: str = "0"
    tks_soba_olap_target_data: str = ""
    tks_soba_request_template: Path | None = None
    tks_kakou_r2_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    tks_soba_r2_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    tks_voucher_olap_disable_op_fields: bool = True
    tks_voucher_olap_enabled_op_fields: list[str] = field(default_factory=list)
    customer_labels: dict[str, str] = field(default_factory=lambda: {
        "得意先1": "得意先1",
        "得意先2": "得意先2",
        "得意先3": "得意先3",
        "得意先4": "得意先4",
    })
    preview_color_theme: str = "light"
    voucher_output_dir: Path | None = None


@dataclass
class KintoneResult:
    success_count: int = 0
    failure_count: int = 0
    failed_records: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ProcessResult:
    output_csv: Path
    output_count: int
    kintone_success_count: int
    kintone_failure_count: int
    has_error: bool
    log_file: Path
    error_csv: Path | None = None


@dataclass
class PendingRegistration:
    output_csv: Path
    rows: list[dict[str, str]]
    output_count: int
    log_file: Path
    timestamp: str


@dataclass
class TksDebugResult:
    message: str
    log_file: Path
