"""初回起動時のデフォルト初期データ投入をまとめるサービス層。

- 加工名マスタ（同梱CSV）
- 得意先ヘッダー設定（config.env）
- Teams Webhook URL（QSettings）

いずれも「未設定・空のときだけ」既定値を投入し、利用者が設定済みの値は上書きしない。
冪等なので何度呼んでも安全。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.customer_header import ensure_default_customer_headers
from app.kakou_master import ensure_default_kakou_master
from app.teams_notifier import (
    default_teams_webhook_url_prod,
    default_teams_webhook_url_test,
)

_LOGGER = logging.getLogger("tks_to_kintone_app")

# gui.py と同じ QSettings キー（重複定義だが循環import回避のためここに持つ）。
SETTINGS_TEAMS_WEBHOOK_URL_TEST = "teams/webhook_url_test"
SETTINGS_TEAMS_WEBHOOK_URL_PROD = "teams/webhook_url_prod"

# 加工名マスタの同梱デフォルトCSV（dev/exe 双方で resource_path から解決）。
DEFAULT_KAKOU_MASTER_RESOURCE = "docs/kakou_master_20260618_132327.csv"

# 初期CSVの探索候補（優先順）。dev実行・PyInstaller onedir(_internal配下) の
# 双方で解決できるよう、resource_path とカレント相対の両方を候補に含める。
DEFAULT_KAKOU_MASTER_RESOURCES = (
    "docs/kakou_master_20260618_132327.csv",
    "templates/kakou_master_default.csv",
)


def default_kakou_master_csv_candidates() -> list[Path]:
    """初期CSVの探索候補パスを優先順で返す（存在有無は問わない）。"""
    from app.config import resource_path

    candidates: list[Path] = []
    for relative in DEFAULT_KAKOU_MASTER_RESOURCES:
        # PyInstaller の _MEIPASS(_internal) / 開発時のリポジトリルート基準。
        candidates.append(resource_path(relative))
        # カレントディレクトリ基準（リポジトリ直下から実行した場合のフォールバック）。
        candidates.append(Path(relative))
    return candidates


def find_default_kakou_master_csv() -> Path | None:
    """初期CSVを探索し、存在する最初の候補（非空）を返す。

    見つからない場合は None を返し、探索した候補パスを WARNING ログへ出す。
    起動時自動投入・「初期値に戻す」・テストはすべてこの関数を使う。
    """
    candidates = default_kakou_master_csv_candidates()
    for path in candidates:
        try:
            if path.exists() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    _LOGGER.warning(
        "加工名マスタ初期CSVが見つかりません。候補:\n%s",
        "\n".join(f"- {p}" for p in candidates),
    )
    return None


def default_kakou_master_csv_path() -> Path:
    """同梱デフォルト加工名マスタCSVの絶対パスを返す。

    実在する候補があればそれを返す。無い場合は先頭候補（resource_path 基準）を返す。
    """
    from app.config import resource_path

    return find_default_kakou_master_csv() or resource_path(DEFAULT_KAKOU_MASTER_RESOURCE)


def ensure_default_kakou_master_from_bundle(kakou_master_csv: Path) -> bool:
    """同梱CSVを既定マスタとして投入する（未投入時のみ）。"""
    default_csv = find_default_kakou_master_csv()
    if default_csv is None:
        # 候補が無くても従来どおりヘッダーのみの空マスタを用意する。
        return ensure_default_kakou_master(kakou_master_csv, Path(DEFAULT_KAKOU_MASTER_RESOURCE))
    return ensure_default_kakou_master(kakou_master_csv, default_csv)


def ensure_default_webhook_urls(settings: Any) -> bool:
    """QSettings に Webhook URL が未設定なら既定値を書き込む。

    既存値（利用者が設定済み）は上書きしない。
    URL自体はログへ出さない（設定済みかどうかだけ記録する）。
    戻り値: 1つでも書き込んだら True。
    """
    wrote = False
    if not settings.contains(SETTINGS_TEAMS_WEBHOOK_URL_TEST):
        settings.setValue(SETTINGS_TEAMS_WEBHOOK_URL_TEST, default_teams_webhook_url_test())
        wrote = True
    if not settings.contains(SETTINGS_TEAMS_WEBHOOK_URL_PROD):
        settings.setValue(SETTINGS_TEAMS_WEBHOOK_URL_PROD, default_teams_webhook_url_prod())
        wrote = True
    if wrote:
        settings.sync()
        # URLは出力しない（設定済みであることだけ記録）。
        _LOGGER.info("Teams Webhook URL is configured")
    return wrote


def ensure_default_initial_data(
    kakou_master_csv: Path,
    config_path: Path,
    settings: Any | None = None,
) -> None:
    """初回起動時のデフォルト初期データをまとめて投入する。

    呼び出しはアプリ起動直後・設定ロード後を想定。冪等。
    settings を渡さない場合は Webhook URL の投入をスキップする。
    """
    ensure_default_kakou_master_from_bundle(kakou_master_csv)
    ensure_default_customer_headers(config_path)
    if settings is not None:
        ensure_default_webhook_urls(settings)
