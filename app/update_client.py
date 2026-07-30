from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import re
import subprocess
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.update_kintone_config import resolve_update_kintone_config

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - packaged Windows builds include requests.
    requests = None  # type: ignore[assignment]


_LOGGER = logging.getLogger("tks_to_kintone_app")

UPDATE_APP_NAME = "TksToKintone"
UPDATE_KINTONE_DOMAIN = "manekiya.cybozu.com"
UPDATE_KINTONE_APP_ID = "250"
UPDATE_KINTONE_API_TOKEN = "foskzpcU5hS5mPZgWo86UC1rNGrzRCr6bHeKsUKg"
UPDATE_TIMEOUT_SECONDS = 60
UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 600
UPDATE_DOWNLOAD_SUBDIR = Path("Manekiya") / "TksToKintone" / "updates"
UPDATE_LOG_SUBDIR = Path("Manekiya") / "TksToKintone" / "logs"
DEFAULT_INSTALLER_FILE_NAME = "tks-to-kintone-setup.exe"
INSTALLER_SILENT_ARGS = [
    "/SILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/SP-",
    "/RELAUNCHAPP=1",
]
INSTALLER_START_TIMEOUT_SECONDS = 15.0
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
DOWNLOAD_CHUNK_SIZE = 1024 * 256
SHA256_CHUNK_SIZE = 1024 * 1024
SHA256_STALL_TIMEOUT_SECONDS = 30.0
UPDATE_SHA256_FIELD_CODES = (
    "SHA-256",
    "SHA_256",
    "SHA256",
    "sha256",
)

ProgressCallback = Callable[[int, int], None]
StageCallback = Callable[[str, str], None]
CancelCheck = Callable[[], bool]


class UpdateCancelled(RuntimeError):
    """The user cancelled before installer launch became irreversible."""


class ElevationCancelled(RuntimeError):
    """The user rejected the Windows elevation prompt."""


class InstallerLaunchError(RuntimeError):
    """The verified installer could not be started."""


@dataclass(frozen=True)
class UpdateInfo:
    version_name: str
    version_code: int
    file_key: str
    file_name: str
    file_size: int
    release_notes: str = ""
    sha256: str = ""
    # Safe update diagnostics. Never store the digest, token, fileKey or headers here.
    connection_source: str = ""
    app_id: str = ""
    record_id: str = ""
    record_field_codes: tuple[str, ...] = ()
    sha256_field_code: str = ""
    sha256_source_length: int = 0
    sha256_source_valid: bool = False
    sha256_before_update_info_length: int = 0


class UpdateClient:
    def check_for_update(self, current_version_code: int) -> UpdateInfo | None:
        if requests is None:
            raise RuntimeError("更新確認には requests が必要です。requirements.txt をインストールしてください。")

        connection = _resolved_update_kintone_config()
        try:
            response = requests.get(
                f"https://{UPDATE_KINTONE_DOMAIN}/k/v1/records.json",
                headers={"X-Cybozu-API-Token": connection.api_token},
                params={
                    "app": connection.app_id,
                    "query": f'アプリ名 in ("{UPDATE_APP_NAME}") order by バージョンコード desc limit 500',
                },
                timeout=UPDATE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            _raise_update_kintone_communication_error(connection.source, exc)
        data = response.json()
        records = data.get("records", []) if isinstance(data, dict) else []
        _LOGGER.info(
            "event=update_records_received source=%s app_id=%s record_count=%s",
            connection.source, connection.app_id, len(records),
        )
        candidates = []
        for record in records:
            if not isinstance(record, dict):
                continue
            info = _record_to_update_info(
                record,
                connection_source=connection.source,
                app_id=str(connection.app_id),
            )
            if info is not None:
                candidates.append((record, info))
        newer = [
            (record, info)
            for record, info in candidates
            if info.version_code > current_version_code
        ]
        if not newer:
            return None
        selected_record, selected_info = max(
            newer, key=lambda candidate: candidate[1].version_code
        )
        _log_selected_update_record(connection, selected_record, selected_info)
        return selected_info


def _resolved_update_kintone_config():
    """Resolve the production/debug Kintone target for every update request."""
    return resolve_update_kintone_config(
        production_app_id=UPDATE_KINTONE_APP_ID,
        production_api_token=UPDATE_KINTONE_API_TOKEN,
    )


def _raise_update_kintone_communication_error(
    source: str,
    cause: Exception,
) -> None:
    """Convert requests errors without leaking URLs, headers, tokens or response bodies."""
    target = "デバッグ接続先" if source == "debug_override" else "本番接続先"
    _LOGGER.warning(
        "event=update_kintone_request_failed source=%s error_type=%s",
        source,
        type(cause).__name__,
    )
    raise RuntimeError(
        f"更新確認用Kintoneの{target}への通信に失敗しました。"
        "認証情報、権限、アプリIDを確認してください。"
    ) from None


def _log_selected_update_record(
    connection: object,
    record: dict[str, object],
    info: UpdateInfo,
) -> None:
    """Log update-record metadata without exposing credentials or the digest."""
    sha256_field_code, sha256_value = _select_sha256_field(record)
    field_codes = ",".join(sorted(str(field_code) for field_code in record))
    _LOGGER.info(
        "event=update_record_selected source=%s app_id=%s record_id=%s "
        "version_name=%s version_code=%s file_name=%s field_codes=[%s] "
        "sha256_field=%s sha256_field_present=%s sha256_length=%s sha256_valid=%s "
        "before_update_info_length=%s update_info_sha256_length=%s",
        getattr(connection, "source", ""),
        getattr(connection, "app_id", ""),
        _field_value(record, "$id") or "",
        info.version_name,
        info.version_code,
        info.file_name,
        field_codes,
        sha256_field_code or "none",
        str(bool(sha256_field_code)).lower(),
        len(sha256_value),
        str(re.fullmatch(r"[0-9a-fA-F]{64}", sha256_value) is not None).lower(),
        info.sha256_before_update_info_length,
        len(info.sha256),
    )


def default_update_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.environ.get("TMP")
    return Path(base) / UPDATE_DOWNLOAD_SUBDIR if base else Path.cwd() / "updates"


def prepare_installer(
    info: UpdateInfo,
    update_dir: Path,
    *,
    progress_callback: ProgressCallback | None = None,
    stage_callback: StageCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> Path:
    """Download to ``.part``, verify size/hash/PE format, then atomically publish it."""
    if requests is None:
        raise RuntimeError("更新には requests が必要です。requirements.txt をインストールしてください。")

    update_dir.mkdir(parents=True, exist_ok=True)
    file_name = _safe_file_name(info.file_name) or DEFAULT_INSTALLER_FILE_NAME
    if not _looks_like_installer(Path(file_name)):
        raise RuntimeError(
            "自動更新にはセットアップ用インストーラが必要です。"
            f"配布管理には setup/installer 名のインストーラを登録してください: {file_name}"
        )
    _LOGGER.info(
        "event=update_sha256_worker_handoff source=%s app_id=%s record_id=%s "
        "update_info_sha256_length=%s worker_sha256_length=%s",
        info.connection_source, info.app_id, info.record_id, len(info.sha256), len(info.sha256),
    )
    expected_sha256 = _normalize_sha256(info.sha256)
    _LOGGER.info(
        "event=update_sha256_normalized source=%s app_id=%s record_id=%s "
        "input_length=%s normalized_length=%s",
        info.connection_source, info.app_id, info.record_id, len(info.sha256), len(expected_sha256),
    )
    if not expected_sha256:
        raise InvalidUpdateSha256Error(info)

    installer_path = update_dir / file_name
    partial_path = installer_path.with_name(installer_path.name + ".part")
    for stale in (partial_path, installer_path):
        _unlink_quietly(stale)

    _emit_stage(stage_callback, "preparing", "更新ファイルを準備しています")
    _check_cancelled(cancel_check)
    connection = _resolved_update_kintone_config()
    received = 0
    response_total = 0
    last_logged_percent = -1
    last_logged_at = time.monotonic()
    _emit_stage(stage_callback, "download", "ダウンロード中")
    try:
        with requests.get(
            f"https://{UPDATE_KINTONE_DOMAIN}/k/v1/file.json",
            headers={"X-Cybozu-API-Token": connection.api_token},
            params={"fileKey": info.file_key},
            stream=True,
            timeout=UPDATE_DOWNLOAD_TIMEOUT_SECONDS,
        ) as response:
            response.raise_for_status()
            content_type = _response_header(response, "Content-Type").lower()
            if "text/html" in content_type:
                raise RuntimeError("更新サーバーからインストーラではなくHTML応答が返されました。")
            response_total = _positive_int(_response_header(response, "Content-Length"))
            total = response_total or max(info.file_size, 0)
            with partial_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    _check_cancelled(cancel_check)
                    if not chunk:
                        continue
                    handle.write(chunk)
                    received += len(chunk)
                    if progress_callback is not None:
                        progress_callback(received, total)
                    percent = min(100, int(received * 100 / total)) if total > 0 else -1
                    now = time.monotonic()
                    if (
                        percent == 100
                        or percent >= last_logged_percent + 1
                        or now - last_logged_at >= 1.0
                    ):
                        _LOGGER.info(
                            "event=update_progress stage=download received_bytes=%s "
                            "total_bytes=%s percent=%s",
                            received,
                            total,
                            percent if percent >= 0 else "unknown",
                        )
                        last_logged_percent = percent
                        last_logged_at = now
    except UpdateCancelled:
        _unlink_quietly(partial_path)
        raise
    except requests.RequestException as exc:
        _unlink_quietly(partial_path)
        _raise_update_kintone_communication_error(connection.source, exc)
    except Exception:
        _unlink_quietly(partial_path)
        raise

    try:
        # Both context managers above have exited here.  On Windows this is
        # important: close the response/stream and writer before reopening the
        # same file for verification.
        _emit_stage(stage_callback, "verify_file", "ダウンロード完了・ファイル確認中")
        _validate_downloaded_size(partial_path, received, response_total, info.file_size)
        _check_cancelled(cancel_check)

        _emit_stage(stage_callback, "verify_sha256", "ダウンロード完了・ファイル確認中")
        actual_sha256 = _verify_sha256_file(partial_path, expected_sha256)
        if actual_sha256 != expected_sha256:
            raise RuntimeError("更新ファイルのSHA-256が一致しません。ファイルを破棄しました。")

        _emit_stage(stage_callback, "verify_pe", "ファイル形式確認中")
        _LOGGER.info("event=update_verify_pe_started path=%s", partial_path)
        _validate_pe_file(partial_path)
        _LOGGER.info("event=update_verify_pe_finished path=%s valid=true", partial_path)

        _emit_stage(stage_callback, "installer_ready", "インストールを準備しています")
        _LOGGER.info(
            "event=update_installer_publish_started source=%s target=%s",
            partial_path, installer_path,
        )
        os.replace(partial_path, installer_path)
        _LOGGER.info("event=update_installer_publish_finished target=%s", installer_path)
        return installer_path
    except Exception:
        _unlink_quietly(partial_path)
        _unlink_quietly(installer_path)
        raise


def download_installer(
    info: UpdateInfo,
    update_dir: Path,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> Path:
    """Compatibility entry point; all downloads now include mandatory verification."""
    return prepare_installer(
        info,
        update_dir,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )


def discard_prepared_installer(installer_path: Path | None) -> None:
    """Remove a verified installer when the pre-install exit check is rejected."""
    if installer_path is not None:
        _unlink_quietly(installer_path)


def launch_external_update(info: UpdateInfo, update_dir: Path, app_exe_path: Path) -> bool:
    installer_path = prepare_installer(info, update_dir)
    return launch_installer_for_update(installer_path, app_exe_path)


def installer_log_dir() -> Path:
    base = os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.environ.get("TMP")
    return Path(base) / UPDATE_LOG_SUBDIR if base else Path.cwd() / "logs"


def installer_log_path(log_dir: Path | None = None) -> Path:
    directory = log_dir or installer_log_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # The random suffix also prevents two attempts made by the same process in
    # one second from ever reusing an earlier Setup log.
    suffix = uuid.uuid4().hex[:8]
    return directory / f"update_installer_{timestamp}_{os.getpid()}_{suffix}.log"


def installer_command(installer_path: Path, log_path: Path | None = None) -> list[str]:
    actual_log_path = log_path or installer_log_path()
    return [str(installer_path), *INSTALLER_SILENT_ARGS, f"/LOG={actual_log_path}"]


def start_installer_for_update(
    installer_path: Path,
    app_exe_path: Path,
    log_dir: Path | None = None,
) -> int | str:
    """Start Setup normally and return after launch, never after installation.

    Setup requests its own elevation through PrivilegesRequired=admin.  Starting
    it from the original process token lets Inno Setup retain that user token
    for the subsequent runasoriginaluser [Run] entry.
    """
    log_path = installer_log_path(log_dir)
    if not installer_path.exists():
        raise InstallerLaunchError("検証済み更新インストーラが見つかりません。")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallerLaunchError("更新ログフォルダを作成できませんでした。") from exc

    if log_path.exists():  # Defensive: a previous attempt must never prove this launch.
        raise InstallerLaunchError("今回の更新ログ名が既に使用されています。")
    command = installer_command(installer_path, log_path)
    _LOGGER.info(
        "event=update_installer_start_requested verb=open file=%s "
        "parameters=%s fmask=0x%08X installer_log=%s",
        installer_path.name,
        subprocess.list2cmdline(command[1:]),
        SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC,
        log_path,
    )
    try:
        process_id = _start_installer_process(command, log_path)
    except ElevationCancelled:
        _LOGGER.warning("event=update_installer_failed reason=elevation_cancelled")
        raise
    except OSError as exc:
        _LOGGER.error(
            "event=update_installer_failed reason=launch_error error_type=%s",
            type(exc).__name__,
        )
        raise InstallerLaunchError("更新インストーラを起動できませんでした。") from exc
    _LOGGER.info(
        "event=update_installer_launch_confirmed silent_mode=silent relaunch=true installer=%s log=%s pid=%s",
        installer_path.name,
        log_path,
        process_id,
    )
    return process_id


def launch_installer_for_update(
    installer_path: Path,
    app_exe_path: Path,
    log_dir: Path | None = None,
) -> bool:
    try:
        start_installer_for_update(installer_path, app_exe_path, log_dir)
    except (InstallerLaunchError, ElevationCancelled):
        return False
    return True


def _wait_for_setup_log(
    log_path: Path,
    process_has_exited: Callable[[], bool],
    timeout_seconds: float = INSTALLER_START_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if log_path.is_file() and log_path.stat().st_size > 0:
                return
        except OSError:
            pass
        if process_has_exited():
            raise InstallerLaunchError(
                "更新インストーラがログを作成する前に終了しました。"
            )
        time.sleep(0.1)
    raise InstallerLaunchError("更新インストーラの開始を確認できませんでした。")


def _validate_shell_execute_result(
    shell_execute_ok: bool,
    error_code: int,
    hinstapp: int,
    process_handle_present: bool,
) -> None:
    if not shell_execute_ok:
        if error_code == 1223:  # ERROR_CANCELLED
            raise ElevationCancelled("管理者の確認がキャンセルされました。")
        raise OSError(error_code, "ShellExecuteExW failed")
    if hinstapp <= 32:
        raise InstallerLaunchError(
            f"更新インストーラの起動結果が無効です（hInstApp={hinstapp}）。"
        )
    if not process_handle_present:
        raise InstallerLaunchError(
            "更新インストーラのプロセスハンドルを取得できませんでした。"
        )


def _start_installer_process(command: list[str], log_path: Path) -> int | str:
    if os.name != "nt":
        process = subprocess.Popen(command, close_fds=True)
        _wait_for_setup_log(log_path, lambda: process.poll() is not None)
        return getattr(process, "pid", "unknown")

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    info = SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFO)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    # Do not pre-elevate Setup with the ``runas`` verb. Its manifest (generated
    # from PrivilegesRequired=admin) displays UAC while preserving Inno Setup's
    # original-user launch context for the post-update [Run] entry.
    info.lpVerb = "open"
    info.lpFile = command[0]
    info.lpParameters = subprocess.list2cmdline(command[1:])
    info.nShow = 1  # SW_SHOWNORMAL: Inno Setup owns the real install progress UI.
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    # COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE
    com_result = ole32.CoInitializeEx(None, 0x2 | 0x4)
    com_initialized = com_result in (0, 1)  # S_OK or S_FALSE
    if com_result < 0 and (com_result & 0xFFFFFFFF) != 0x80010106:
        raise OSError(com_result & 0xFFFFFFFF, "CoInitializeEx failed")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessId.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFO)]
        shell32.ShellExecuteExW.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        shell_execute_ok = bool(shell32.ShellExecuteExW(ctypes.byref(info)))
        error_code = ctypes.get_last_error()
        hinstapp = int(info.hInstApp or 0)
        handle_present = bool(info.hProcess)
        _LOGGER.info(
            "event=update_installer_launch_result shell_execute_ok=%s verb=open "
            "file=%s parameters=%s fmask=0x%08X hinstapp=%s "
            "process_handle_present=%s get_last_error=%s setup_log_created=%s "
            "shutdown_committed=false",
            str(shell_execute_ok).lower(), Path(command[0]).name,
            subprocess.list2cmdline(command[1:]), info.fMask, hinstapp,
            str(handle_present).lower(), error_code,
            str(log_path.exists()).lower(),
        )
        _validate_shell_execute_result(
            shell_execute_ok, error_code, hinstapp, handle_present
        )
        process_id = kernel32.GetProcessId(info.hProcess)
        if not process_id:
            raise InstallerLaunchError("更新インストーラのプロセスを確認できませんでした。")
        _wait_for_setup_log(
            log_path,
            lambda: kernel32.WaitForSingleObject(info.hProcess, 0) == 0,
        )
        _LOGGER.info(
            "event=update_installer_launch_result shell_execute_ok=true hinstapp=%s "
            "process_handle_present=true setup_log_created=true setup_log_size=%s "
            "shutdown_committed=false pid=%s",
            hinstapp, log_path.stat().st_size, process_id,
        )
        return process_id
    finally:
        if info.hProcess:
            kernel32.CloseHandle(info.hProcess)
        if com_initialized:
            ole32.CoUninitialize()


class InvalidUpdateSha256Error(RuntimeError):
    """A safe public failure carrying metadata-only diagnostics."""

    def __init__(self, info: UpdateInfo) -> None:
        super().__init__("更新情報のSHA-256を取得できませんでした。")
        self.diagnostic = format_sha256_diagnostic(info, worker_sha256_length=len(info.sha256))


def format_sha256_diagnostic(info: UpdateInfo, *, worker_sha256_length: int) -> str:
    field = info.sha256_field_code or "なし"
    return "\n".join(
        (
            f"接続元: {info.connection_source or '不明'}",
            f"アプリID: {info.app_id or '不明'}",
            f"レコードID: {info.record_id or '不明'}",
            f"バージョン: {info.version_name}",
            f"バージョンコード: {info.version_code}",
            f"添付ファイル: {info.file_name}",
            f"フィールドコード: {','.join(info.record_field_codes)}",
            f"SHA-256フィールド: {field}",
            f"取得文字数: {info.sha256_source_length}",
            f"64文字の16進数として有効: {'はい' if info.sha256_source_valid else 'いいえ'}",
            f"UpdateInfo直前: {info.sha256_before_update_info_length}",
            f"UpdateInfo生成後: {len(info.sha256)}",
            f"worker受渡し時: {worker_sha256_length}",
        )
    )


def _record_to_update_info(
    record: dict[str, object],
    *,
    connection_source: str = "",
    app_id: str = "",
) -> UpdateInfo | None:
    version_code = _parse_version_code(_field_value(record, "バージョンコード"))
    if version_code is None:
        return None
    files = _field_value(record, "APKファイル")
    if not isinstance(files, list) or not files or not isinstance(files[0], dict):
        return None
    file_info = files[0]
    file_key = str(file_info.get("fileKey") or "")
    if not file_key:
        return None
    sha256_field_code, sha256_value = _select_sha256_field(record)
    if not sha256_field_code:
        sha256_value = str(file_info.get("sha256") or "")
    return UpdateInfo(
        version_name=str(_field_value(record, "バージョン名") or ""),
        version_code=version_code,
        file_key=file_key,
        file_name=str(file_info.get("name") or f"TksToKintone_{version_code}.exe"),
        file_size=_parse_int(file_info.get("size")) or 0,
        release_notes=str(_field_value(record, "リリースノート") or ""),
        sha256=sha256_value,
        connection_source=connection_source,
        app_id=app_id,
        record_id=str(_field_value(record, "$id") or ""),
        record_field_codes=tuple(sorted(str(code) for code in record)),
        sha256_field_code=sha256_field_code,
        sha256_source_length=len(sha256_value),
        sha256_source_valid=re.fullmatch(r"[0-9a-fA-F]{64}", sha256_value) is not None,
        sha256_before_update_info_length=len(sha256_value),
    )


def _field_value(record: dict[str, object], field_code: str) -> object:
    field = record.get(field_code)
    return field.get("value") if isinstance(field, dict) else None


def _select_sha256_field(record: dict[str, object]) -> tuple[str, str]:
    """Return the first non-empty SHA-256 field in the shared priority order."""
    for field_code in UPDATE_SHA256_FIELD_CODES:
        if field_code not in record:
            continue
        value = str(_field_value(record, field_code) or "")
        if value:
            return field_code, value
    return "", ""


def _parse_version_code(value: object) -> int | None:
    parsed = _parse_int(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _normalize_sha256(value: str) -> str:
    compact = str(value).strip()
    return compact.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", compact) else ""


def _safe_file_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(value).name)


def _looks_like_installer(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower() == ".exe" and any(
        keyword in name for keyword in ("setup", "installer", "install")
    )


def _response_header(response: object, name: str) -> str:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return ""
    return str(headers.get(name, "") or "")


def _validate_downloaded_size(
    path: Path, received: int, content_length: int, metadata_size: int
) -> None:
    if received <= 0 or not path.exists():
        raise RuntimeError("更新ファイルをダウンロードできませんでした。")
    if content_length and received != content_length:
        raise RuntimeError(
            f"更新ファイルの受信サイズがContent-Lengthと一致しません（受信 {received} / 予定 {content_length} bytes）。"
        )
    if metadata_size and received != metadata_size:
        raise RuntimeError(
            f"更新ファイルのサイズが配布情報と一致しません（受信 {received} / 予定 {metadata_size} bytes）。"
        )


def _validate_pe_file(path: Path) -> None:
    with path.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise RuntimeError("ダウンロードしたファイルは有効なWindowsインストーラではありません。")


def _verify_sha256_file(
    path: Path,
    expected_sha256: str,
    *,
    stall_timeout_seconds: float = SHA256_STALL_TIMEOUT_SECONDS,
) -> str:
    """Hash a closed download using a finite EOF loop and a stall watchdog."""
    total = path.stat().st_size
    started = time.monotonic()
    state_lock = threading.Lock()
    watchdog_done = threading.Event()
    state = {"processed": 0, "updated": started, "done": False, "stalled": False}

    def watch_progress() -> None:
        while True:
            if watchdog_done.wait(min(1.0, max(stall_timeout_seconds / 4.0, 0.01))):
                return
            with state_lock:
                if state["done"]:
                    return
                if time.monotonic() - float(state["updated"]) >= stall_timeout_seconds:
                    state["stalled"] = True
                    _LOGGER.error(
                        "event=update_verify_sha256_watchdog error_type=TimeoutError "
                        "error_stage=verify_sha256 error_message=sha256_progress_stalled "
                        "processed_bytes=%s total_bytes=%s",
                        state["processed"], total,
                    )
                    return

    _LOGGER.info(
        "event=update_verify_sha256_started path=%s file_size=%s "
        "expected_hash_length=%s expected_hash_prefix=%s",
        path, total, len(expected_sha256), expected_sha256[:8],
    )
    watchdog = threading.Thread(
        target=watch_progress, name="update-sha256-watchdog", daemon=True
    )
    watchdog.start()
    hasher = hashlib.sha256()
    processed = 0
    last_percent = -10
    last_logged_at = started
    try:
        with open(path, "rb") as handle:
            while True:
                read_started = time.monotonic()
                chunk = handle.read(SHA256_CHUNK_SIZE)
                if not chunk:
                    break
                processed += len(chunk)
                hasher.update(chunk)
                now = time.monotonic()
                with state_lock:
                    state["processed"] = processed
                    state["updated"] = now
                    stalled = bool(state["stalled"])
                if stalled or now - read_started >= stall_timeout_seconds:
                    raise TimeoutError("SHA-256検証のファイル読み取り進捗が停止しました。")
                percent = min(100, int(processed * 100 / total)) if total else 100
                if percent >= last_percent + 10 or now - last_logged_at >= 1.0 or processed == total:
                    _LOGGER.info(
                        "event=update_verify_sha256_progress processed_bytes=%s "
                        "total_bytes=%s percent=%s", processed, total, percent,
                    )
                    last_percent = percent
                    last_logged_at = now
        actual = hasher.hexdigest().lower()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _LOGGER.info(
            "event=update_verify_sha256_finished elapsed_ms=%s actual_hash_prefix=%s matched=%s",
            elapsed_ms, actual[:8], str(actual == expected_sha256).lower(),
        )
        return actual
    except Exception as exc:
        safe_message = _safe_error_message(exc)
        safe_traceback = _redact_sensitive_text(traceback.format_exc())
        _LOGGER.error(
            "event=update_verify_failed error_type=%s error_stage=verify_sha256 "
            "error_message=%s traceback=%s",
            type(exc).__name__, safe_message, safe_traceback,
        )
        raise
    finally:
        with state_lock:
            state["done"] = True
        watchdog_done.set()


def _safe_error_message(exc: BaseException) -> str:
    return _redact_sensitive_text(str(exc) or type(exc).__name__)


def _redact_sensitive_text(value: str) -> str:
    value = re.sub(r"(?i)(X-Cybozu-API-Token|fileKey)\s*[=:]\s*\S+", r"\1=<redacted>", value)
    return re.sub(r"\b[0-9a-fA-F]{64}\b", "<sha256-redacted>", value)


def _check_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None and cancel_check():
        raise UpdateCancelled("更新をキャンセルしました。")


def _emit_stage(callback: StageCallback | None, stage: str, message: str) -> None:
    if callback is not None:
        callback(stage, message)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _LOGGER.warning(
            "event=update_cleanup_failed file=%s error_type=%s",
            path.name,
            type(exc).__name__,
        )
