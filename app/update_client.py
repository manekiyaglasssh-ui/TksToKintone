from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - dependency is installed in packaged Windows builds.
    requests = None  # type: ignore[assignment]


# 本体アプリのロガーへ相乗りする（SecretFilter によりトークン等は自動的に伏せられる）。
_LOGGER = logging.getLogger("tks_to_kintone_app")

UPDATE_APP_NAME = "TksToKintone"
UPDATE_KINTONE_DOMAIN = "manekiya.cybozu.com"
UPDATE_KINTONE_APP_ID = "250"
UPDATE_KINTONE_API_TOKEN = "foskzpcU5hS5mPZgWo86UC1rNGrzRCr6bHeKsUKg"
UPDATE_TIMEOUT_SECONDS = 60
UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 600
UPDATE_DOWNLOAD_SUBDIR = Path("Manekiya") / "TksToKintone" / "updates"
UPDATE_LOG_SUBDIR = Path("Manekiya") / "TksToKintone" / "logs"
# 配布インストーラの既定ファイル名（installer/tks-to-kintone.iss の OutputBaseFilename と一致）。
DEFAULT_INSTALLER_FILE_NAME = "tks-to-kintone-setup.exe"
INSTALLER_SILENT_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
STALE_UPDATE_SCRIPT_NAME = "run_update.ps1"


@dataclass(frozen=True)
class UpdateInfo:
    version_name: str
    version_code: int
    file_key: str
    file_name: str
    file_size: int
    release_notes: str = ""


class UpdateClient:
    def check_for_update(self, current_version_code: int) -> UpdateInfo | None:
        if requests is None:
            raise RuntimeError("更新確認には requests が必要です。requirements.txt をインストールしてください。")

        response = requests.get(
            f"https://{UPDATE_KINTONE_DOMAIN}/k/v1/records.json",
            headers={"X-Cybozu-API-Token": UPDATE_KINTONE_API_TOKEN},
            params={
                "app": UPDATE_KINTONE_APP_ID,
                "query": f'アプリ名 in ("{UPDATE_APP_NAME}") order by バージョンコード desc limit 500',
            },
            timeout=UPDATE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        records = data.get("records", []) if isinstance(data, dict) else []
        candidates = [_record_to_update_info(record) for record in records if isinstance(record, dict)]
        newer = [info for info in candidates if info is not None and info.version_code > current_version_code]
        if not newer:
            return None
        return max(newer, key=lambda info: info.version_code)


def default_update_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.environ.get("TMP")
    if base:
        return Path(base) / UPDATE_DOWNLOAD_SUBDIR
    return Path.cwd() / "updates"


def launch_external_update(info: UpdateInfo, update_dir: Path, app_exe_path: Path) -> bool:
    """外部スクリプト/helper EXE を使わずに更新インストーラを起動する。

    1. 本体プロセス（Python/EXE）でインストーラを updates フォルダへダウンロードする。
    2. 本体プロセスから Inno Setup インストーラを直接サイレント起動する。
    3. 呼び出し側はインストーラ起動成功（戻り値 True）時だけ本体を終了する。

    インストーラを起動できなかった場合は False を返す。呼び出し側はエラーを
    表示し、本体を終了しないこと（終了すると更新もされず再起動もされないため）。
    """
    installer_path = download_installer(info, update_dir)
    return launch_installer_for_update(installer_path, app_exe_path)


def download_installer(info: UpdateInfo, update_dir: Path) -> Path:
    """インストーラを本体プロセス内で updates フォルダへダウンロードする。"""
    if requests is None:
        raise RuntimeError("更新には requests が必要です。requirements.txt をインストールしてください。")

    update_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_update_script(update_dir)
    file_name = _safe_file_name(info.file_name) or DEFAULT_INSTALLER_FILE_NAME
    if not _looks_like_installer(Path(file_name)):
        raise RuntimeError(
            "自動更新には署名済みインストーラが必要です。"
            f"配布管理には setup/installer 名のインストーラを登録してください: {file_name}"
        )

    installer_path = update_dir / file_name
    partial_path = installer_path.with_name(installer_path.name + ".part")
    download_url = f"https://{UPDATE_KINTONE_DOMAIN}/k/v1/file.json"

    for stale in (partial_path, installer_path):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass

    with requests.get(
        download_url,
        headers={"X-Cybozu-API-Token": UPDATE_KINTONE_API_TOKEN},
        params={"fileKey": info.file_key},
        stream=True,
        timeout=UPDATE_DOWNLOAD_TIMEOUT_SECONDS,
    ) as response:
        response.raise_for_status()
        with partial_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)

    os.replace(partial_path, installer_path)
    return installer_path


def installer_log_dir() -> Path:
    base = os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.environ.get("TMP")
    if base:
        return Path(base) / UPDATE_LOG_SUBDIR
    return Path.cwd() / "logs"


def installer_log_path(log_dir: Path | None = None) -> Path:
    target_dir = log_dir or installer_log_dir()
    return target_dir / "update_installer.log"


def installer_command(installer_path: Path, log_path: Path | None = None) -> list[str]:
    """Inno Setup のサイレント更新起動コマンドを返す。"""
    actual_log_path = log_path or installer_log_path()
    args = [*INSTALLER_SILENT_ARGS, f"/LOG={actual_log_path}"]
    return [str(installer_path), *args]


def launch_installer_for_update(
    installer_path: Path,
    app_exe_path: Path,
    log_dir: Path | None = None,
) -> bool:
    """更新インストーラを直接起動する。外部スクリプト/helper EXE は使わない。

    起動に成功したら True、インストーラが存在しない・起動に失敗した場合は
    False を返す。トークンなどの秘密情報は一切ログに出さない。
    """
    installer_exists = installer_path.exists()
    log_path = installer_log_path(log_dir)

    _LOGGER.info("更新開始準備 installer_path=%s", installer_path)
    _LOGGER.info("更新開始準備 app_exe_path=%s", app_exe_path)
    _LOGGER.info("更新開始準備 installer_log_path=%s", log_path)
    _LOGGER.info("更新開始準備 installer exists %s", str(installer_exists).lower())

    if not installer_exists:
        _LOGGER.error("インストーラが存在しないため更新を中止します: %s", installer_path)
        return False

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOGGER.error("更新ログフォルダを作成できませんでした: %s", exc)
        return False

    command = installer_command(installer_path, log_path)
    try:
        pid = _start_installer_process(command)
    except OSError as exc:
        _LOGGER.error("更新インストーラの起動に失敗しました: %s", exc)
        return False

    _LOGGER.info("更新開始 installer pid=%s", pid)
    return True


def _start_installer_process(command: list[str]) -> int | str:
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    process = subprocess.Popen(command, close_fds=True, creationflags=creationflags)
    return getattr(process, "pid", "unknown")


def cleanup_stale_update_script(update_dir: Path) -> None:
    stale_script = update_dir / STALE_UPDATE_SCRIPT_NAME
    try:
        stale_script.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _LOGGER.warning("古い更新スクリプトを削除できませんでした: %s (%s)", stale_script, exc)
    else:
        _LOGGER.info("古い更新スクリプトを削除しました: %s", stale_script)


def _record_to_update_info(record: dict[str, object]) -> UpdateInfo | None:
    version_code = _parse_version_code(_field_value(record, "バージョンコード"))
    if version_code is None:
        return None
    files = _field_value(record, "APKファイル")
    if not isinstance(files, list) or not files:
        return None
    file_info = files[0]
    if not isinstance(file_info, dict):
        return None
    file_key = str(file_info.get("fileKey") or "")
    if not file_key:
        return None
    return UpdateInfo(
        version_name=str(_field_value(record, "バージョン名") or ""),
        version_code=version_code,
        file_key=file_key,
        file_name=str(file_info.get("name") or f"TksToKintone_{version_code}.exe"),
        file_size=_parse_int(file_info.get("size")) or 0,
        release_notes=str(_field_value(record, "リリースノート") or ""),
    )


def _field_value(record: dict[str, object], field_code: str) -> object:
    field = record.get(field_code)
    return field.get("value") if isinstance(field, dict) else None


def _parse_version_code(value: object) -> int | None:
    parsed = _parse_int(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def _safe_file_name(value: str) -> str:
    name = Path(value).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


def _looks_like_installer(path: Path) -> bool:
    name = path.name.lower()
    return any(keyword in name for keyword in ("setup", "installer", "install"))
