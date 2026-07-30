"""更新補助プロセス。

PowerShell を使わず、通常のプロセス起動とファイル操作だけで更新を完了させる。

処理の流れ:
    1. 親プロセス（アプリ本体）の終了を待つ（タイムアウトあり）
    2. インストーラファイルの存在を確認する
    3. インストーラを起動する（通常起動 or サイレント起動を切り替え可能）
    4. インストーラの終了を待つ
    5. アプリ本体 EXE が存在すれば再起動する

ビルド後は ``tks_update_helper.exe`` として実行される。コマンドラインには
パスワードやトークンなどの秘密情報を渡さない。ダウンロードは本体側で完了済み。

止まっている場所を特定できるよう、各段階を
``%LOCALAPPDATA%\\Manekiya\\TksToKintone\\logs\\update_helper.log`` へ記録する。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

POLL_INTERVAL_SECONDS = 0.5
# 親プロセスが終了しないまま放置されても無限ループしないための上限。
PARENT_WAIT_TIMEOUT_SECONDS = 60
# Inno Setup のサイレントインストール引数。権限昇格はインストーラ側に任せる。
INSTALLER_SILENT_ARGS = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
# 通常起動（インストーラ画面を表示する）。DeepInstinct 確認やテスト時に使う。
INSTALLER_NORMAL_ARGS: list[str] = []
# ヘルパーログの保存先（本体アプリのログとは別ファイルにする）。
HELPER_LOG_SUBDIR = Path("Manekiya") / "TksToKintone" / "logs"
HELPER_LOG_NAME = "update_helper.log"


def _process_alive(pid: int) -> bool:
    """指定 PID のプロセスが生存しているかを安全に判定する（プロセスを終了させない）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_parent_exit(pid: int, timeout: float, log: object) -> bool:
    """親プロセスの終了を待つ。終了を確認できたら True、タイムアウトしたら False。"""
    if pid <= 0:
        return True
    deadline = time.monotonic() + timeout
    while _process_alive(pid):
        if time.monotonic() >= deadline:
            log("parent process did not exit within timeout")
            return False
        time.sleep(POLL_INTERVAL_SECONDS)
    return True


def _run_installer(installer_path: Path, silent: bool, log: object) -> int:
    import subprocess

    args = INSTALLER_SILENT_ARGS if silent else INSTALLER_NORMAL_ARGS
    log(f"installer args: {' '.join(args) if args else '(通常起動)'}")
    process = subprocess.Popen([str(installer_path), *args], close_fds=True)
    process.wait()
    log(f"installer exit code {process.returncode}")
    return process.returncode


def _restart_app(app_exe_path: Path, log: object) -> None:
    import subprocess

    if not app_exe_path.exists():
        log(f"アプリ本体が見つからないため再起動をスキップします: {app_exe_path}")
        return
    subprocess.Popen([str(app_exe_path)], close_fds=True)
    log(f"app restart: {app_exe_path}")


def _helper_executable_path() -> Path:
    """このヘルパー自身の実行ファイルパスを返す。

    ビルド後は実行中の EXE（一時フォルダの runner コピー）、開発実行時はこの
    スクリプトのパス。インストール先から起動していないことの確認に使う。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def _default_log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.environ.get("TMP")
    if base:
        return Path(base) / HELPER_LOG_SUBDIR
    return Path.cwd() / "logs"


def _make_logger(log_dir: Path):
    log_path = log_dir / HELPER_LOG_NAME

    def log(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    return log


def run_update(
    installer_path: Path,
    app_exe_path: Path,
    parent_pid: int,
    silent: bool = False,
    log_dir: Path | None = None,
) -> int:
    log = _make_logger(log_dir or _default_log_dir())
    log("helper started")
    helper_exe = _helper_executable_path()
    log(f"helper_executable_path {helper_exe}")
    log(f"current_working_directory {Path.cwd()}")
    log(f"parent_pid {parent_pid}")
    log(f"installer_path {installer_path}")
    log(f"app_exe_path {app_exe_path}")

    # 自身がインストール先（本体 EXE と同じフォルダ配下）から起動していると、
    # インストーラが helper 自身を上書きできずファイルロックの原因になる。
    # 本来は本体側で一時コピーから起動しているはずなので、ここでは警告のみ。
    try:
        install_dir = app_exe_path.resolve().parent
        helper_exe.relative_to(install_dir)
        log(
            "WARNING: update helper is running from install directory. "
            "This may block installer file replacement."
        )
    except ValueError:
        pass

    log("waiting parent process")
    if not _wait_for_parent_exit(parent_pid, PARENT_WAIT_TIMEOUT_SECONDS, log):
        # 本体が終了しないままインストールを始めるとファイルロックで失敗するため中止する。
        log("親プロセスが終了しなかったためインストーラ起動を中止します。")
        return 3
    log("parent process exited")

    installer_exists = installer_path.exists()
    log(f"installer exists {str(installer_exists).lower()}")
    if not installer_exists:
        log(f"インストーラが見つかりません: {installer_path}")
        return 2

    log("installer start")
    exit_code = _run_installer(installer_path, silent, log)
    if exit_code != 0:
        log("インストーラが異常終了したため再起動しません。")
        return exit_code

    _restart_app(app_exe_path, log)
    log("helper finished")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TksToKintone 更新補助プロセス")
    parser.add_argument("--installer-path", required=True)
    parser.add_argument("--app-exe-path", required=True)
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument(
        "--silent",
        action="store_true",
        help="インストーラをサイレント起動する。未指定時は通常起動（画面表示）。",
    )
    parser.add_argument("--log-dir", default=None, help="ヘルパーログの保存先ディレクトリ。")
    args = parser.parse_args(argv)
    return run_update(
        Path(args.installer_path),
        Path(args.app_exe_path),
        args.parent_pid,
        silent=args.silent,
        log_dir=Path(args.log_dir) if args.log_dir else None,
    )


if __name__ == "__main__":
    sys.exit(main())
