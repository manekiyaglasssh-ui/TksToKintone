"""TKS受注No取込の小画面。

TKSCloud8 の受注入力画面に表示されている受注Noを、TksToKintone アプリ内へ
保管するための「常に手前に表示する小画面」。

仕様:
- 常に手前に表示（WindowStaysOnTopHint）し、コンパクトな小ウィンドウ。
- LauncherWindow とは別ウィンドウ。閉じてもアプリ全体は終了しない。
- 複数起動しない（LauncherWindow 側が単一インスタンスを保持する）。
- 受注Noは伝票一覧へ直接入れず、まず work 配下の専用JSONへ保存する。

自動処理は UI スレッドをブロックしないよう、専用の QThread ワーカーで行う:
- 自動取得ワーカー（_CaptureWorker）: 表示中に定期的に受注Noを取得する（保存しない）。
- 実行検知ワーカー（_ExecuteWorker）: 自動保存ON時に F12キー／「F12 実行」ボタンの
  クリックエッジを監視する。
いずれも結果は signal/slot で UI スレッドへ返し、ワーカーから UI 部品には触らない。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
import atexit
import weakref
from collections import OrderedDict
from datetime import datetime

from PySide6.QtCore import (
    QMetaObject,
    QObject,
    QProcess,
    Qt,
    QThread,
    QTimer,
    QSettings,
    Signal,
    Slot,
)
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config import resource_path
from app import captured_orders
from app.path_utils import get_order_capture_debug_dir
from app.theme_utils import apply_windows_title_bar_theme, current_title_bar_is_dark
from app.window_geometry import clamp_window_to_available_geometry

_LOGGER = logging.getLogger("tks_to_kintone_app")
_STOPPING_THREADS: list[QThread] = []
_LIVE_CAPTURE_WINDOWS: "weakref.WeakSet[TksOrderCaptureWindow]" = weakref.WeakSet()
try:
    import shiboken6
except Exception:  # noqa: BLE001 - shiboken が無い環境でも通常の例外保護で継続する
    shiboken6 = None


def _shutdown_live_capture_windows() -> None:
    """Process teardown fallback for tests and abnormal app shutdown paths."""
    windows = list(_LIVE_CAPTURE_WINDOWS)
    for window in windows:
        try:
            window._shutdown_for_app_quit()
        except Exception:
            _LOGGER.exception("order_import_process_exit_shutdown_failed")

    app = QApplication.instance()
    deadline = time.monotonic() + 3.0
    while _STOPPING_THREADS and time.monotonic() < deadline:
        for thread in list(_STOPPING_THREADS):
            try:
                if not thread.isRunning():
                    _STOPPING_THREADS.remove(thread)
                    continue
                thread.wait(50)
            except RuntimeError:
                try:
                    _STOPPING_THREADS.remove(thread)
                except ValueError:
                    pass
        if app is not None:
            try:
                app.processEvents()
            except RuntimeError:
                break


atexit.register(_shutdown_live_capture_windows)

_CAPTURE_BUTTON_STYLE = """
QPushButton#captureButton {
    background-color: #2563EB;
    color: white;
    border: 1px solid #1D4ED8;
    border-radius: 4px;
    padding: 5px 10px;
}
QPushButton#saveButton {
    background-color: #1F7A4D;
    color: white;
    border: 1px solid #17613D;
    border-radius: 4px;
    padding: 5px 10px;
}
QPushButton#addButton {
    background-color: #0F766E;
    color: white;
    border: 1px solid #0D5F59;
    border-radius: 4px;
    padding: 5px 10px;
}
QPushButton#listButton {
    background-color: #4B5563;
    color: white;
    border: 1px solid #374151;
    border-radius: 4px;
    padding: 5px 10px;
}
QPushButton#closeButton {
    background-color: #5F6673;
    color: white;
    border: 1px solid #4B5563;
    border-radius: 4px;
    padding: 5px 10px;
}
QPushButton:disabled,
QPushButton#captureButton:disabled,
QPushButton#saveButton:disabled,
QPushButton#addButton:disabled,
QPushButton#listButton:disabled,
QPushButton#closeButton:disabled {
    background-color: #D1D5DB;
    color: #6B7280;
    border: 1px solid #C4C9D1;
}
QPushButton:disabled:hover,
QPushButton:disabled:pressed,
QPushButton#captureButton:disabled:hover,
QPushButton#captureButton:disabled:pressed,
QPushButton#saveButton:disabled:hover,
QPushButton#saveButton:disabled:pressed,
QPushButton#addButton:disabled:hover,
QPushButton#addButton:disabled:pressed,
QPushButton#listButton:disabled:hover,
QPushButton#listButton:disabled:pressed,
QPushButton#closeButton:disabled:hover,
QPushButton#closeButton:disabled:pressed {
    background-color: #D1D5DB;
    color: #6B7280;
    border: 1px solid #C4C9D1;
}
"""

_SETTINGS_ORG = "Manekiya"
_SETTINGS_APP = "TksToKintone"
# 旧キー（互換維持・移行元）。実行時保存のON/OFFを保持していた。
_SETTINGS_AUTO_SAVE = "tks_capture/auto_save"
# 新キー。自動取得と自動保存を別々に保持する。
_SETTINGS_AUTO_CAPTURE = "tks_capture/auto_capture"
_SETTINGS_AUTO_SAVE_ON_EXECUTE = "tks_capture/auto_save_on_execute"
# 「常に手前に表示」チェックのON/OFFを保持する（要件3）。初期値ON（従来は固定でON）。
_SETTINGS_ALWAYS_ON_TOP = "tks_capture/always_on_top"
# TKS側F12キー／「F12 実行」ボタンのクリック座標監視を明示的にONにする設定（既定OFF）。
# 既定では TKS側保存トリガーは「受注入力（見出）→（明細）」の画面遷移だけで扱い、
# マウス座標や高頻度キーポーリングでは拾わない（要件2・4）。
_SETTINGS_F12_MONITOR = "tks_capture/f12_monitor_enabled"
# 実行検知ワーカーのポーリング間隔（ms）。UIとは別スレッドで回す。
# 旧実装は 50ms の高頻度ポーリングで Win32/UIA/前面/マウス取得を回し、長時間稼働で
# クラッシュ・GIL競合の原因になっていた。最短500msに引き上げ、重いWin32/UIAは毎tick
# 実行しない（要件2）。
_EXECUTE_MONITOR_MIN_INTERVAL_MS = 500
_EXECUTE_POLL_INTERVAL_MS = 500
_EXECUTE_RECT_CACHE_INTERVAL_MS = 500
_EXECUTE_RECT_CACHE_TTL_MS = 3000
_EXECUTE_TRANSITION_DEBOUNCE_MS = 3000
_F12_SAVE_DEBOUNCE_MS = 300
# 仮想キーコード: F12 / 左マウスボタン。
_VK_F12 = 0x7B
_VK_LBUTTON = 0x01
# 受注Noの自動取得間隔（ms）。取込画面が表示されている間だけ稼働する（保存はしない）。
# 以前は 700/200ms と短く、取得処理（win32列挙・UIA）が重い環境で tick が詰まり、
# GIL 競合で UI スレッドのクリック処理まで遅延していた。最低間隔を設けて緩和する。
_AUTO_CAPTURE_INTERVAL_MS = 1000
_AUTO_CAPTURE_ACTIVE_INTERVAL_MS = 500
# 自動取得の最短間隔（これより短くしない）。処理時間がこれを超える環境では詰まりを防ぐ。
_AUTO_CAPTURE_MIN_INTERVAL_MS = 500
# 受注No取込画面の自動取得timer間隔（別プロセスhelper経路・要件6/7）。
# 通常1000〜2000ms。TKS受注入力画面が見えているときはやや短く、見出で受注No保持中
# （自動保存ONで見出→明細遷移を待っている間）はさらに短い fast poll にして体感遅延を減らす。
# 自動取得ペースを上げる（要件1・B案の高速化）。通常800〜1000ms、見出で受注No保持中
# かつ自動保存ONの fast poll は 500〜800ms。timeout/失敗時のみ 2000〜3000ms へ backoff。
_AUTO_CAPTURE_NORMAL_INTERVAL_MS = 900
_AUTO_CAPTURE_SCREEN_INTERVAL_MS = 800
_AUTO_CAPTURE_FAST_POLL_INTERVAL_MS = 600
# slow tick 検知時に間隔へ加算するバックオフ量とその上限（ms）。
_AUTO_CAPTURE_BACKOFF_STEP_MS = 500
_AUTO_CAPTURE_BACKOFF_MAX_MS = 2000
# tick 所要時間の警告しきい値（ms）。
_SLOW_TICK_WARN_MS = 200
_SLOW_TICK_STRONG_WARN_MS = 500
# 実行検知ワーカーで、重い前面/タイトル/対象ウィンドウ判定を再取得する最短間隔（ms）。
# キー/マウスのエッジ検知は毎ポーリング（軽量）で行い、重い win32 列挙のみ間引く。
_EXECUTE_CONTEXT_REFRESH_MS = 300
# 自動取得ワーカーが perf スナップショットを出す間隔（ms）。
_PERF_SNAPSHOT_INTERVAL_MS = 10000
# 自動取得の連続失敗がこの回数に達したときだけ状態表示を更新する（最新値はすぐ消さない）。
_AUTO_CAPTURE_FAILURE_STATUS_THRESHOLD = 5
# 自動保存の連続要求をまとめてから1回だけディスクへ書き込むデバウンス時間（ms）。
# 受注No検出後は即メモリ反映し、この時間後にまとめて軽量flushする（UIを固めない）。
_SAVE_FLUSH_DEBOUNCE_MS = 250
# 受注No取込画面表示中に内部状態だけを出す heartbeat 間隔（ms・要件6）。
# UIA/Win32探索はせず、常駐処理の生存・thread数だけを軽量に記録する。
_HEARTBEAT_INTERVAL_MS = 30000
_WORKER_DEBUG_LOG_PATH = None
_EXECUTE_DEBUG_LOG_PATH = None
# 診断JSONLの行数上限（長時間稼働でファイルが無制限に肥大化しないための安全弁）。
_DEBUG_LOG_MAX_LINES = 20000
_WORKER_DEBUG_LOG_LINES = 0
_EXECUTE_DEBUG_LOG_LINES = 0
_DPI_AWARENESS_ATTEMPTED = False
_DPI_AWARENESS_RESULT: dict = {}
# クラッシュ追跡の多重インストール防止と faulthandler ファイルの寿命保持（要件6）。
_CRASH_TRACKING_INSTALLED = False
_FAULTHANDLER_FILE = None
# ── 受注No取得helper（別プロセス化・要件2〜5） ─────────────────────────────────
# UIA/COM/Win32探索は本体プロセスで直接呼ばず、短命の別プロセスhelperで実行する。
# helperがネイティブクラッシュしても本体は「取得不可」として継続できる。
_CAPTURE_HELPER_FLAG = "--tks-order-capture-helper"
# 1回の取得(helper)のタイムアウト（ms）。これを超えたらkillして取得不可扱い。
# TKS/UIA/helper起動込みでは2秒は短すぎ、結果JSONが返る前にkillしていた（前回の不具合）。
# source ごとに分け、autoは余裕を持たせ、manualはユーザー操作なのでさらに長く待つ。
_CAPTURE_HELPER_AUTO_TIMEOUT_MS = 5000
_CAPTURE_HELPER_MANUAL_TIMEOUT_MS = 10000
# 後方互換の既定値（sync helper 経路など source 未指定時に使う）。auto と同じにする。
_CAPTURE_HELPER_TIMEOUT_MS = _CAPTURE_HELPER_AUTO_TIMEOUT_MS
# 連続失敗（timeout等）でバックオフ間隔へ切り替えるしきい値と、そのときの間隔（ms・要件7）。
# backoff中でも永久停止せず、この間隔で再試行を続ける（上限は5秒程度）。
_CAPTURE_BACKOFF_FAILURE_THRESHOLD = 5
_CAPTURE_BACKOFF_INTERVAL_MS = 2500
_CAPTURE_BACKOFF_INTERVAL_MAX_MS = 3000
# 解決済みhelper起動コマンドのキャッシュ（毎tickのパス探索を避ける）。
_CAPTURE_HELPER_COMMAND_CACHE: list[str] | None = None
_CAPTURE_HELPER_COMMAND_RESOLVED = False
_CAPTURE_HELPER_LOG_LIMIT = 4096


def _truncate_helper_stream(value: str, *, limit: int = _CAPTURE_HELPER_LOG_LIMIT) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"


def _resolve_capture_helper_command() -> list[str] | None:
    """受注No取得helperを起動するコマンド配列を解決する（開発/frozen両対応・要件5）。

    - frozen環境: 本体exeを `<exe> --tks-order-capture-helper` として呼び直す。
    - 開発環境: `python -m app.tks_order_capture_helper` を起動する。
    見つからない場合は None を返し、呼び出し側は「取得不可」として継続する。
    結果はプロセス内でキャッシュする（毎tickのパス探索/存在チェックを避ける）。
    """
    global _CAPTURE_HELPER_COMMAND_CACHE, _CAPTURE_HELPER_COMMAND_RESOLVED
    if _CAPTURE_HELPER_COMMAND_RESOLVED:
        return list(_CAPTURE_HELPER_COMMAND_CACHE) if _CAPTURE_HELPER_COMMAND_CACHE else None
    _CAPTURE_HELPER_COMMAND_RESOLVED = True
    command: list[str] | None = None
    try:
        if getattr(sys, "frozen", False):
            command = [sys.executable, _CAPTURE_HELPER_FLAG]
            _LOGGER.info("order_import_capture_helper_frozen_mode executable=%s", sys.executable)
            _LOGGER.info("order_import_capture_helper_command_resolved command=%s", command)
        else:
            helper_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tks_order_capture_helper.py")
            if os.path.exists(helper_path):
                command = [sys.executable, "-m", "app.tks_order_capture_helper"]
                _LOGGER.info("order_import_capture_helper_dev_mode path=%s", helper_path)
                _LOGGER.info("order_import_capture_helper_command_resolved command=%s", command)
            else:
                _LOGGER.warning("order_import_capture_helper_path_missing path=%s", helper_path)
                _LOGGER.warning("order_import_capture_helper_command_missing path=%s", helper_path)
    except Exception:  # noqa: BLE001 - パス解決失敗でアプリを落とさない
        _LOGGER.warning("order_import_capture_helper_path_missing", exc_info=True)
        _LOGGER.warning("order_import_capture_helper_command_missing", exc_info=True)
        command = None
    _CAPTURE_HELPER_COMMAND_CACHE = list(command) if command else None
    return list(command) if command else None


def reset_capture_helper_command_cache() -> None:
    """helper起動コマンドのキャッシュを破棄する（テスト・設定変更時の即時反映用）。"""
    global _CAPTURE_HELPER_COMMAND_CACHE, _CAPTURE_HELPER_COMMAND_RESOLVED
    _CAPTURE_HELPER_COMMAND_CACHE = None
    _CAPTURE_HELPER_COMMAND_RESOLVED = False


def _parse_capture_helper_output(raw: str) -> dict:
    """helperのstdout(JSON)を安全な結果dictへ変換する（不正JSONは例外にする）。"""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("helper_output_not_object")
    return {
        "ok": bool(data.get("ok", False)),
        "screen_type": str(data.get("screen_type") or "unknown"),
        "order_no": str(data.get("order_no") or ""),
        "reason": str(data.get("reason") or ""),
        "elapsed_ms": data.get("elapsed_ms"),
    }


def run_capture_via_helper(
    *,
    command: list[str] | None = None,
    debug: bool = False,
    timeout_ms: int = _CAPTURE_HELPER_TIMEOUT_MS,
    on_proc=None,
) -> dict:
    """受注No取得helperをsubprocessで1回だけ実行し、正規化結果dictを返す（要件6）。

    手動取得(_on_capture)・自動取得(_CaptureOnceWorker)の**唯一の共通経路**。
    UIA/COM/Win32 は本体では直接呼ばず、この helper 起動へ集約する（要件2）。
    戻り値 keys: order_no / error / screen_type / reason / elapsed_ms。
    timeout・非0 returncode・不正JSON・helper未解決・例外のいずれでも必ず dict を返し、
    本体プロセスを落とさない（要件4）。

    on_proc: 実行中/終了時の Popen を受け取る callback（worker が kill 用に握るため）。
    """
    started = time.monotonic()
    order_no = ""
    error = ""
    screen_type = "unknown"
    reason = ""
    timeout_ms = int(timeout_ms)
    _LOGGER.info("order_import_capture_helper_timeout_ms timeout_ms=%s", timeout_ms)
    if not command:
        _LOGGER.warning("order_import_capture_helper_path_missing")
        _LOGGER.warning("order_import_capture_helper_command_missing")
        return {
            "order_no": "",
            "error": "helper_unavailable",
            "screen_type": "unknown",
            "reason": "helper_unavailable",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }
    argv = list(command) + (["--debug"] if debug else [])
    _LOGGER.info("order_import_capture_helper_command_resolved command=%s", argv)
    _LOGGER.info("order_import_capture_helper_started")
    timeout_s = max(0.2, timeout_ms / 1000.0)
    rc = None
    raw_out = ""
    try:
        import subprocess

        creationflags = 0
        if sys.platform == "win32":
            # helperのコンソール窓を出さない。
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        if on_proc is not None:
            try:
                on_proc(proc)
            except Exception:  # noqa: BLE001
                pass
        try:
            out_bytes, err_bytes = proc.communicate(timeout=timeout_s)
            rc = proc.returncode
            raw_out = (out_bytes or b"").decode("utf-8", "replace").strip()
            raw_err = (err_bytes or b"").decode("utf-8", "replace").strip()
            _LOGGER.info(
                "order_import_capture_helper_stdout stdout=%r",
                _truncate_helper_stream(raw_out),
            )
            _LOGGER.info(
                "order_import_capture_helper_stderr stderr=%r",
                _truncate_helper_stream(raw_err),
            )
        except subprocess.TimeoutExpired:
            # タイムアウト: kill して取得不可扱い（本体は落とさない・要件4/7）。
            try:
                proc.kill()
                proc.communicate(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
            error = "timeout"
            _LOGGER.warning("order_import_capture_helper_timeout timeout_s=%s", timeout_s)
        finally:
            if on_proc is not None:
                try:
                    on_proc(None)
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001 - helper起動失敗でも本体を落とさない
        error = f"helper_spawn_failed: {type(exc).__name__}: {exc}"
        _LOGGER.warning("order_import_capture_helper_failed", exc_info=True)
        if isinstance(exc, FileNotFoundError):
            _LOGGER.warning("order_import_capture_helper_command_missing command=%s", argv)

    if not error:
        _LOGGER.info("order_import_capture_helper_returncode rc=%s", rc)
        if rc not in (0, None):
            error = f"helper_returncode_{rc}"
            _LOGGER.warning("order_import_capture_helper_failed rc=%s", rc)
        else:
            try:
                if not raw_out:
                    error = "empty_stdout"
                    _LOGGER.warning("order_import_capture_helper_empty_stdout")
                    raise ValueError("empty_stdout")
                parsed = _parse_capture_helper_output(raw_out)
                missing = [
                    key
                    for key in ("ok", "screen_type", "order_no", "reason")
                    if key not in json.loads(raw_out)
                ]
                if missing:
                    _LOGGER.warning(
                        "order_import_capture_helper_json_missing_fields missing=%s",
                        missing,
                    )
                _LOGGER.info("order_import_capture_helper_json_parsed")
                order_no = parsed["order_no"]
                screen_type = parsed["screen_type"]
                reason = parsed["reason"]
                if reason == "helper_mode_not_available":
                    _LOGGER.warning("order_import_capture_helper_mode_not_available")
                helper_elapsed = parsed.get("elapsed_ms")
                if helper_elapsed is not None:
                    _LOGGER.info(
                        "order_import_capture_helper_elapsed_ms helper_elapsed_ms=%s",
                        helper_elapsed,
                    )
            except Exception:  # noqa: BLE001 - 不正JSONでも本体を落とさない
                if error != "empty_stdout":
                    error = "invalid_json"
                    _LOGGER.warning(
                        "order_import_capture_helper_invalid_json raw=%r",
                        _truncate_helper_stream(raw_out),
                    )

    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
    _LOGGER.info("order_import_capture_helper_finished elapsed_ms=%s", elapsed_ms)
    return {
        "order_no": str(order_no or ""),
        "error": str(error or ""),
        "screen_type": str(screen_type or "unknown"),
        "reason": str(reason or ""),
        "elapsed_ms": elapsed_ms,
    }


def _set_process_dpi_awareness() -> dict:
    """Windowsの座標取得をできるだけ物理ピクセル系に揃える。失敗しても続行する。"""
    global _DPI_AWARENESS_ATTEMPTED, _DPI_AWARENESS_RESULT
    if _DPI_AWARENESS_ATTEMPTED:
        return dict(_DPI_AWARENESS_RESULT)
    _DPI_AWARENESS_ATTEMPTED = True
    result = {
        "coordinate_space": "unknown",
        "exception_type": None,
        "exception_message": None,
    }
    if sys.platform != "win32":
        result["coordinate_space"] = "non_windows"
        _DPI_AWARENESS_RESULT = result
        return dict(result)
    try:
        import ctypes

        user32 = ctypes.windll.user32
        # PER_MONITOR_AWARE_V2 = -4. Older Windows may not expose this API.
        try:
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                result["coordinate_space"] = "per_monitor_aware_v2"
                _DPI_AWARENESS_RESULT = result
                return dict(result)
        except Exception:  # noqa: BLE001
            pass
        try:
            shcore = ctypes.windll.shcore
            # PROCESS_PER_MONITOR_DPI_AWARE = 2
            hr = shcore.SetProcessDpiAwareness(2)
            if hr in (0, -2147024891):  # S_OK / E_ACCESSDENIED (already set)
                result["coordinate_space"] = "per_monitor_aware"
                _DPI_AWARENESS_RESULT = result
                return dict(result)
        except Exception:  # noqa: BLE001
            pass
        try:
            if user32.SetProcessDPIAware():
                result["coordinate_space"] = "system_dpi_aware"
                _DPI_AWARENESS_RESULT = result
                return dict(result)
        except Exception:  # noqa: BLE001
            pass
        result["coordinate_space"] = "dpi_awareness_unset"
    except Exception as exc:  # noqa: BLE001
        result["coordinate_space"] = "dpi_awareness_failed"
        result["exception_type"] = type(exc).__name__
        result["exception_message"] = str(exc)
    _DPI_AWARENESS_RESULT = result
    return dict(result)


def _f12_key_is_down() -> bool:
    """F12 キーが現在押下されているかを返す（Windows以外・例外は False）。

    GetAsyncKeyState は状態を読むだけでキー入力を奪わない（フックは張らない）。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.GetAsyncKeyState.restype = ctypes.c_short
        return bool(user32.GetAsyncKeyState(_VK_F12) & 0x8000)
    except Exception:  # noqa: BLE001 - キー状態取得失敗でアプリを落とさない
        return False


def _left_mouse_is_down() -> bool:
    """左マウスボタンが現在押下されているか（Windows以外・例外は False）。

    GetAsyncKeyState は状態を読むだけでクリックを奪わない（フックは張らない）。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.GetAsyncKeyState.restype = ctypes.c_short
        return bool(user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000)
    except Exception:  # noqa: BLE001 - マウス状態取得失敗でアプリを落とさない
        return False


def _get_cursor_pos() -> tuple[int, int] | None:
    """現在のマウスカーソル座標 (x, y) を返す（Windows以外・例外は None）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        pt = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return (int(pt.x), int(pt.y))
        return None
    except Exception:  # noqa: BLE001 - カーソル座標取得失敗でアプリを落とさない
        return None


def _get_physical_cursor_pos() -> tuple[int, int] | None:
    """物理ピクセル座標のカーソル位置を返す。未対応・失敗時は None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        pt = wintypes.POINT()
        getter = getattr(ctypes.windll.user32, "GetPhysicalCursorPos", None)
        if getter and getter(ctypes.byref(pt)):
            return (int(pt.x), int(pt.y))
        return None
    except Exception:  # noqa: BLE001
        return None


def _point_in_rect(pos, rect) -> bool:
    """座標 pos=(x, y) が矩形 rect=(left, top, right, bottom) の内側かどうか。"""
    if not pos or not rect:
        return False
    x, y = pos
    left, top, right, bottom = rect
    return left <= x <= right and top <= y <= bottom


def _rect_tuple(value: object) -> tuple[int, int, int, int] | None:
    if not value:
        return None
    try:
        left, top, right, bottom = (int(v) for v in value)
    except Exception:  # noqa: BLE001
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _rect_size(rect: tuple[int, int, int, int] | None) -> tuple[int, int]:
    if rect is None:
        return (0, 0)
    return (rect[2] - rect[0], rect[3] - rect[1])


def _rect_near_outer(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int], *, tolerance: int = 24) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _get_virtual_screen_rect() -> tuple[int, int, int, int] | None:
    if sys.platform != "win32":
        return None


def _monitor_info_for_point(pos: object) -> dict:
    point = None
    try:
        if pos:
            point = tuple(int(v) for v in pos)
    except Exception:  # noqa: BLE001
        point = None
    info = {
        "monitor_handle": None,
        "monitor_rect": None,
        "dpi_scale_x": 1.0,
        "dpi_scale_y": 1.0,
    }
    if sys.platform != "win32" or point is None:
        return info
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        pt = wintypes.POINT(point[0], point[1])
        monitor = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
        if monitor:
            info["monitor_handle"] = int(monitor)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
                info["monitor_rect"] = (
                    int(mi.rcMonitor.left),
                    int(mi.rcMonitor.top),
                    int(mi.rcMonitor.right),
                    int(mi.rcMonitor.bottom),
                )
            try:
                shcore = ctypes.windll.shcore
                dpi_x = ctypes.c_uint(96)
                dpi_y = ctypes.c_uint(96)
                if shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
                    info["dpi_scale_x"] = round(float(dpi_x.value) / 96.0, 4)
                    info["dpi_scale_y"] = round(float(dpi_y.value) / 96.0, 4)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return info
    return info


def _scale_point_from_monitor(point: tuple[int, int], monitor_rect: object, scale_x: float, scale_y: float) -> tuple[int, int]:
    rect = _rect_tuple(monitor_rect) or (0, 0, 0, 0)
    return (
        rect[0] + int(round((point[0] - rect[0]) * scale_x)),
        rect[1] + int(round((point[1] - rect[1]) * scale_y)),
    )


def _coordinate_snapshot(
    *,
    mouse_pos: object,
    execute_button_rect: object,
    tkscloud_window_rect: object,
) -> dict:
    raw_mouse = tuple(mouse_pos) if mouse_pos else None
    physical_mouse = _get_physical_cursor_pos()
    raw_rect = _rect_tuple(execute_button_rect)
    raw_window = _rect_tuple(tkscloud_window_rect)
    monitor_seed = physical_mouse or raw_mouse or (
        ((raw_window[0] + raw_window[2]) // 2, (raw_window[1] + raw_window[3]) // 2)
        if raw_window
        else None
    )
    monitor = _monitor_info_for_point(monitor_seed)
    normalized_mouse = physical_mouse or raw_mouse
    coordinate_space = "physical_cursor" if physical_mouse else "raw_screen"
    if (
        physical_mouse is None
        and raw_mouse is not None
        and (monitor.get("dpi_scale_x") not in (None, 1.0) or monitor.get("dpi_scale_y") not in (None, 1.0))
    ):
        normalized_mouse = _scale_point_from_monitor(
            raw_mouse,
            monitor.get("monitor_rect"),
            float(monitor.get("dpi_scale_x") or 1.0),
            float(monitor.get("dpi_scale_y") or 1.0),
        )
        coordinate_space = "scaled_from_monitor_dpi"
    return {
        "raw_mouse_pos": list(raw_mouse) if raw_mouse else None,
        "normalized_mouse_pos": list(normalized_mouse) if normalized_mouse else None,
        "raw_execute_button_rect": list(raw_rect) if raw_rect else None,
        "normalized_execute_button_rect": list(raw_rect) if raw_rect else None,
        "raw_tkscloud_window_rect": list(raw_window) if raw_window else None,
        "normalized_tkscloud_window_rect": list(raw_window) if raw_window else None,
        "monitor_handle": monitor.get("monitor_handle"),
        "monitor_rect": list(monitor["monitor_rect"]) if monitor.get("monitor_rect") else None,
        "dpi_scale_x": monitor.get("dpi_scale_x"),
        "dpi_scale_y": monitor.get("dpi_scale_y"),
        "coordinate_space": coordinate_space,
    }
    try:
        import ctypes

        user32 = ctypes.windll.user32
        left = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        top = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        if width <= 0 or height <= 0:
            return None
        return (left, top, left + width, top + height)
    except Exception:  # noqa: BLE001
        return None


def _valid_execute_button_rect(
    rect: object,
    *,
    tkscloud_window_rect: object = None,
    screen_rect: object = None,
) -> tuple[bool, tuple[int, int, int, int] | None, str]:
    rect_tuple = _rect_tuple(rect)
    if rect_tuple is None:
        return False, None, "invalid_rect"
    width, height = _rect_size(rect_tuple)
    if width < 40 or width > 220 or height < 20 or height > 120:
        return False, rect_tuple, "invalid_size"
    window = _rect_tuple(tkscloud_window_rect)
    if window is not None and not _rect_near_outer(rect_tuple, window):
        return False, rect_tuple, "outside_tkscloud_window"
    screen = _rect_tuple(screen_rect) or _get_virtual_screen_rect()
    if screen is not None and not _rect_near_outer(rect_tuple, screen, tolerance=0):
        return False, rect_tuple, "outside_screen"
    return True, rect_tuple, ""


def _infer_execute_button_rect_from_window(
    window_rect: object,
) -> tuple[int, int, int, int] | None:
    window = _rect_tuple(window_rect)
    if window is None:
        return None
    left, top, right, bottom = window
    width, height = right - left, bottom - top
    if width < 300 or height < 180:
        return None
    button_width = min(110, max(76, int(width * 0.08)))
    button_height = min(58, max(36, int(height * 0.065)))
    margin_right = max(8, min(24, int(width * 0.012)))
    margin_bottom = max(6, min(18, int(height * 0.012)))
    return (
        right - margin_right - button_width,
        bottom - margin_bottom - button_height,
        right - margin_right,
        bottom - margin_bottom,
    )


def _tkscloud8_is_foreground() -> bool:
    """TKSCloud8「受注入力（見出）」画面が前面かどうか（例外は False）。"""
    try:
        from app.tks_cloud_capture import is_target_window_foreground

        return bool(is_target_window_foreground())
    except Exception:  # noqa: BLE001
        return False


def _tks_order_entry_window_running() -> bool:
    """TKSCloud8「受注入力（見出）」画面の存在確認（軽量・例外はFalse）。"""
    try:
        from app.tks_cloud_capture import is_tks_order_entry_window_running

        return bool(is_tks_order_entry_window_running())
    except Exception:  # noqa: BLE001
        return False


def _tkscloud_window_running() -> bool:
    """TKSCloud8系画面が存在するか（診断用。受注入力画面に限定しない）。"""
    try:
        from app.tks_cloud_capture import is_tkscloud_window_running

        return bool(is_tkscloud_window_running())
    except Exception:  # noqa: BLE001
        return False


def _tkscloud_window_rect() -> tuple[int, int, int, int] | None:
    try:
        from app.tks_cloud_capture import read_tkscloud_window_rect

        return read_tkscloud_window_rect()
    except Exception:  # noqa: BLE001
        return None


def _tkscloud_window_title() -> str:
    try:
        from app.tks_cloud_capture import read_tkscloud_window_title

        return read_tkscloud_window_title()
    except Exception:  # noqa: BLE001
        return ""


def _foreground_window_info() -> dict:
    """前面ウィンドウのタイトル・PID・プロセス名（診断用。例外は空値）。"""
    try:
        from app.tks_cloud_capture import get_foreground_window_info

        return get_foreground_window_info()
    except Exception:  # noqa: BLE001
        return {"title": "", "pid": None, "process_name": None}


def execute_button_rect_from_tkscloud8() -> tuple[int, int, int, int] | None:
    """TKSCloud8「F12 実行」ボタンの画面矩形を取得する（取得不可・例外は None）。"""
    try:
        from app.tks_cloud_capture import read_execute_button_rect

        return read_execute_button_rect(debug=_capture_debug_enabled())
    except Exception:  # noqa: BLE001 - 取得失敗でアプリを落とさない
        _LOGGER.warning("「F12 実行」ボタン矩形の取得で例外が発生しました。", exc_info=True)
        return None


# デバッグ判定は QSettings 生成＋ディスク/レジストリ読取を伴い重い。ワーカーは
# 秒間数十回呼ぶため、短時間キャッシュして I/O とオブジェクト生成を抑える。
_DEBUG_ENABLED_CACHE: dict = {"value": False, "checked_at": 0.0}
_DEBUG_ENABLED_TTL_S = 2.0


def _capture_debug_enabled(*, force_refresh: bool = False) -> bool:
    """受注No取得の診断出力を有効にするか（環境変数 or デバッグ表示設定）。

    高頻度に呼ばれるため、QSettings 読取は TTL 付きでキャッシュする。
    """
    if os.environ.get("TKS_ORDER_CAPTURE_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    now = time.monotonic()
    cache = _DEBUG_ENABLED_CACHE
    if not force_refresh and now - cache["checked_at"] < _DEBUG_ENABLED_TTL_S:
        return bool(cache["value"])
    try:
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        raw = settings.value("ui/debug_visible", "0")
        value = str(raw).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:  # noqa: BLE001
        value = False
    cache["value"] = value
    cache["checked_at"] = now
    return value


def reset_debug_cache() -> None:
    """デバッグ判定キャッシュを破棄する（設定変更直後やテストで即時反映したい時）。"""
    _DEBUG_ENABLED_CACHE["value"] = False
    _DEBUG_ENABLED_CACHE["checked_at"] = 0.0


def capture_order_no_from_tkscloud8() -> str | None:
    """TKSCloud8「受注入力（見出）」画面の受注No欄の値を取得して返す。

    取得は UI Automation → Win32 子ウィンドウ列挙の順（app.tks_cloud_capture）。
    取得値は正規化（前後空白除去・全角数字→半角・数字以外は無効）してから返す。
    取得できない／例外時は None。OCR は未実装（将来フォールバック予定）。
    """
    try:
        from app.tks_cloud_capture import read_order_no_from_tkscloud8

        raw = read_order_no_from_tkscloud8(debug=_capture_debug_enabled())
    except Exception:  # noqa: BLE001 - 取得失敗でアプリを落とさない
        _LOGGER.warning("TKSCloud8 からの受注No取得処理で例外が発生しました。", exc_info=True)
        return None
    return captured_orders.normalize_captured_order_no(raw)


def capture_failure_detail() -> str:
    """直近の取得失敗の詳細メッセージ（無ければ空文字）。"""
    try:
        from app.tks_cloud_capture import get_last_capture_failure_message

        return get_last_capture_failure_message()
    except Exception:  # noqa: BLE001
        return ""


def _write_worker_debug_event(event: str, _debug: bool | None = None, **payload: object) -> None:
    # 呼び出し側が debug 状態を把握している場合は QSettings 読取を省く（ホットループ対策）。
    if not (_debug if _debug is not None else _capture_debug_enabled(force_refresh=True)):
        return
    global _WORKER_DEBUG_LOG_LINES
    if _WORKER_DEBUG_LOG_LINES >= _DEBUG_LOG_MAX_LINES:
        return
    try:
        global _WORKER_DEBUG_LOG_PATH
        debug_dir = get_order_capture_debug_dir()
        debug_dir.mkdir(parents=True, exist_ok=True)
        if _WORKER_DEBUG_LOG_PATH is None or _WORKER_DEBUG_LOG_PATH.parent != debug_dir:
            _WORKER_DEBUG_LOG_PATH = (
                debug_dir / f"order_capture_worker_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
            )
            _WORKER_DEBUG_LOG_LINES = 0
        path = _WORKER_DEBUG_LOG_PATH
        _WORKER_DEBUG_LOG_LINES += 1
        row = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            "thread_id": threading.get_ident(),
            **payload,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        _LOGGER.debug("自動取得worker診断ログの書き出しに失敗しました。", exc_info=True)


_CRASH_PROBE_LOG_PATH = None
_CRASH_PROBE_LOG_LINES = 0
# クラッシュ調査ログの上限（長時間稼働でも無制限に肥大化させない）。
_CRASH_PROBE_MAX_LINES = 100000


def _write_crash_probe_event(event: str, **payload: object) -> None:
    """受注No取込のクラッシュ調査ログを1行ごとに flush して追記する（要件5）。

    デバッグ設定に関係なく常に記録する。status更新前後・process cleanup前後・
    tick前後の節目に呼び、Aborted 直前の最後の処理を必ず残せるようにする。
    UI/Qt API は呼ばず、単純なファイル追記＋flush(+fsync)のみで完結させる。
    """
    global _CRASH_PROBE_LOG_PATH, _CRASH_PROBE_LOG_LINES
    if _CRASH_PROBE_LOG_LINES >= _CRASH_PROBE_MAX_LINES:
        return
    try:
        debug_dir = get_order_capture_debug_dir()
        debug_dir.mkdir(parents=True, exist_ok=True)
        if _CRASH_PROBE_LOG_PATH is None or _CRASH_PROBE_LOG_PATH.parent != debug_dir:
            _CRASH_PROBE_LOG_PATH = (
                debug_dir
                / f"order_capture_crash_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
            )
            _CRASH_PROBE_LOG_LINES = 0
        _CRASH_PROBE_LOG_LINES += 1
        row = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            "thread_id": threading.get_ident(),
            **payload,
        }
        with _CRASH_PROBE_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:  # noqa: BLE001 - fsync 非対応環境でも継続
                pass
    except Exception:  # noqa: BLE001 - クラッシュ調査ログの失敗でUIを落とさない
        pass


def _write_execute_debug_event(event: str, _debug: bool | None = None, **payload: object) -> None:
    """実行検知から保存までの診断を JSONL へ追記する（デバッグ時のみ）。"""
    if not (_debug if _debug is not None else _capture_debug_enabled(force_refresh=True)):
        return
    global _EXECUTE_DEBUG_LOG_LINES
    if _EXECUTE_DEBUG_LOG_LINES >= _DEBUG_LOG_MAX_LINES:
        return
    try:
        global _EXECUTE_DEBUG_LOG_PATH
        debug_dir = get_order_capture_debug_dir()
        debug_dir.mkdir(parents=True, exist_ok=True)
        if _EXECUTE_DEBUG_LOG_PATH is None or _EXECUTE_DEBUG_LOG_PATH.parent != debug_dir:
            _EXECUTE_DEBUG_LOG_PATH = (
                debug_dir / f"order_capture_execute_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
            )
            _EXECUTE_DEBUG_LOG_LINES = 0
        path = _EXECUTE_DEBUG_LOG_PATH
        _EXECUTE_DEBUG_LOG_LINES += 1
        row = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            "thread_id": threading.get_ident(),
            **payload,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        _LOGGER.debug("実行検知診断ログの書き出しに失敗しました。", exc_info=True)


# ── COM/UIA 寿命管理・画面種別判定・クラッシュ追跡（要件5/6） ────────────────────
def _com_initialize() -> bool:
    """呼び出しスレッドで COM(STA) を初期化する。成功時 True（要件5）。

    UIA 取得は下位（comtypes）でも per-call で CoInitialize/CoUninitialize するが、
    単発取得 worker のスレッド境界に合わせて明示初期化し、finally で必ず解放する
    ことで、長時間・高頻度な UIA 利用でのネイティブクラッシュを避ける。
    非Windows／comtypes未導入では何もしない（False）。
    """
    try:
        import comtypes
    except Exception:  # noqa: BLE001 - 非Windows/comtypes未導入ではCOMを使わない
        return False
    try:
        comtypes.CoInitialize()
        return True
    except Exception:  # noqa: BLE001
        _LOGGER.debug("COM初期化に失敗しました。", exc_info=True)
        return False


def _com_uninitialize() -> None:
    try:
        import comtypes
    except Exception:  # noqa: BLE001
        return
    try:
        comtypes.CoUninitialize()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("COM解放に失敗しました。", exc_info=True)


def _detect_tks_screen_type() -> str:
    """TKSCloud8 の現在画面種別を返す（worker内で安全な文字列のみ・要件3/5）。

    "header" = 受注入力（見出）／"detail" = 受注入力（明細）／
    "none" = TKS対象画面なし／"unknown" = TKSはあるが判定不可。
    """
    try:
        title = _tkscloud_window_title() or ""
    except Exception:  # noqa: BLE001
        return "unknown"
    if "受注入力（明細）" in title:
        return "detail"
    if "受注入力（見出）" in title:
        return "header"
    if title:
        return "unknown"
    try:
        running = _tkscloud_window_running()
    except Exception:  # noqa: BLE001
        running = False
    return "unknown" if running else "none"


def _install_crash_tracking() -> None:
    """未捕捉のPython/スレッド例外・Qtメッセージ・faulthandlerをログへ回す（要件6）。

    プロセスごと落ちる直前の状態を残すための保険。多重インストールしない。
    """
    global _CRASH_TRACKING_INSTALLED
    if _CRASH_TRACKING_INSTALLED:
        return
    _CRASH_TRACKING_INSTALLED = True

    previous_excepthook = sys.excepthook

    def _python_excepthook(exc_type, exc_value, exc_tb):
        try:
            _LOGGER.error(
                "order_import_unhandled_python_exception type=%s message=%s",
                getattr(exc_type, "__name__", exc_type),
                exc_value,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            previous_excepthook(exc_type, exc_value, exc_tb)
        except Exception:  # noqa: BLE001
            pass

    try:
        sys.excepthook = _python_excepthook
    except Exception:  # noqa: BLE001
        pass

    try:
        previous_thread_hook = threading.excepthook

        def _thread_excepthook(args):
            try:
                _LOGGER.error(
                    "order_import_unhandled_thread_exception type=%s message=%s thread=%s",
                    getattr(args.exc_type, "__name__", args.exc_type),
                    args.exc_value,
                    getattr(args.thread, "name", None),
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                previous_thread_hook(args)
            except Exception:  # noqa: BLE001
                pass

        threading.excepthook = _thread_excepthook
    except Exception:  # noqa: BLE001
        pass

    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        # 通常運用では QtWarningMsg の大量ログでログを埋めない（要件8）。
        # debug設定ON時のみ全メッセージを記録し、OFF時は Critical/Fatal のみ記録して
        # Warning は抑制する（抑制中である旨は最初の1回だけ残す）。
        debug_enabled = _capture_debug_enabled()
        suppressed_state = {"warning_notice_logged": False}

        def _qt_message_handler(mode, context, message):
            # 要件8: Warning/Critical では abort しない（Qtの既定 handler へ渡さず握る）。
            # Fatal はログを flush してから元の挙動（Qt側の abort）に任せる。
            # handler 内では例外を出さず、重い処理や Qt API 呼び出しもしない。
            try:
                if mode == QtMsgType.QtFatalMsg:
                    _write_crash_probe_event(
                        "order_import_qt_fatal_message_received",
                        message=str(message)[:500],
                    )
                    try:
                        for _handler in list(_LOGGER.handlers):
                            try:
                                _handler.flush()
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception:  # noqa: BLE001
                        pass
                    _LOGGER.error("order_import_qt_message mode=%s message=%s", mode, message)
                    return
                if debug_enabled:
                    _LOGGER.warning("order_import_qt_message mode=%s message=%s", mode, message)
                    return
                if mode == QtMsgType.QtCriticalMsg:
                    # Critical でも原則ログだけ（abort しない）。
                    _LOGGER.error("order_import_qt_message mode=%s message=%s", mode, message)
                    return
                # Warning/Info/Debug は運用ノイズとして抑制する。
                if not suppressed_state["warning_notice_logged"]:
                    suppressed_state["warning_notice_logged"] = True
                    _LOGGER.info("order_import_qt_warning_suppressed")
            except Exception:  # noqa: BLE001 - handler 内で例外を伝播させない
                try:
                    _write_crash_probe_event("order_import_qt_message_handler_exception_suppressed")
                except Exception:  # noqa: BLE001
                    pass

        qInstallMessageHandler(_qt_message_handler)
        _LOGGER.info("order_import_qt_message_handler_safe_mode debug=%s", debug_enabled)
        if debug_enabled:
            _LOGGER.info("order_import_qt_message_handler_enabled")
        else:
            _LOGGER.info("order_import_qt_message_handler_disabled")
    except Exception:  # noqa: BLE001
        pass

    try:
        import faulthandler

        debug_dir = get_order_capture_debug_dir()
        debug_dir.mkdir(parents=True, exist_ok=True)
        fault_path = debug_dir / "order_capture_faulthandler.log"
        global _FAULTHANDLER_FILE
        _FAULTHANDLER_FILE = fault_path.open("a", encoding="utf-8")
        faulthandler.enable(file=_FAULTHANDLER_FILE, all_threads=True)
        _LOGGER.info("order_import_faulthandler_enabled path=%s", fault_path)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("faulthandlerの有効化に失敗しました。", exc_info=True)


# ── 自動処理ワーカー（UIスレッドをブロックしない） ─────────────────────────────
class _CaptureWorker(QObject):
    """受注Noの自動取得を UI とは別スレッドで行うワーカー。

    - 有効な間だけ、定期的に TKSCloud8 から受注Noを取得する（保存はしない）。
    - 取得結果は captured / capture_failed シグナルで UI スレッドへ返す。
    - 重い取得が重なっても多重実行しないよう _busy で直列化する（要件17）。
    - ワーカーから UI 部品には一切触らない。
    """

    captured = Signal(str)
    capture_failed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True
        self._busy = False
        self._stopped = False
        self._debug = False
        self._timer: QTimer | None = None
        # 単一実行制御: 実行中に来た tick は _pending にし、完了後1回だけ再実行する。
        self._pending = False
        # slow tick 時のバックオフ量（ms）。処理が重い環境で間隔を自動的に広げる。
        self._backoff_ms = 0
        # 直近に emit した受注No。同値の連続 emit を抑止し UI 更新過多を防ぐ。
        self._last_emitted_value: str | None = None
        # perf スナップショットの前回出力時刻と累積カウンタ。
        self._perf_last_snapshot = 0.0
        self._tick_count = 0
        self._slow_tick_count = 0

    @Slot(bool)
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    @Slot(bool)
    def set_debug(self, debug: bool) -> None:
        self._debug = bool(debug)

    def _log(self, event: str, **payload: object) -> None:
        if self._debug:
            _write_worker_debug_event(
                event,
                _debug=True,
                auto_capture_enabled=self._enabled,
                interval_ms=self._timer.interval() if self._timer is not None else None,
                **payload,
            )

    def request_stop(self) -> None:
        # UIスレッドから安全に止めるためのフラグ（in-flight tick は早期return）。
        self._stopped = True
        self._log("worker_stop_requested")

    @Slot()
    def start(self) -> None:
        # QTimer はワーカーのスレッドで生成し、timeout もそのスレッドで動く。
        # 親をワーカー(self)にしておき、停止も同じスレッドで行えるようにする。
        # 画面につき1本だけ生成する（多重生成防止は呼び出し側 _start_workers のガード）。
        if self._timer is not None:
            return
        self._backoff_ms = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self._effective_interval())
        self._timer.timeout.connect(self.capture_once)
        self._timer.start()
        self._log("worker_start")

    def _effective_interval(self) -> int:
        """基準間隔（対象画面の有無で切替）＋バックオフを、最短間隔でクランプして返す。"""
        base = (
            _AUTO_CAPTURE_ACTIVE_INTERVAL_MS
            if _tks_order_entry_window_running()
            else _AUTO_CAPTURE_INTERVAL_MS
        )
        interval = base + self._backoff_ms
        return min(2000, max(_AUTO_CAPTURE_MIN_INTERVAL_MS, interval))

    def _refresh_interval(self) -> None:
        # タイマーは再生成せず interval だけ変更する（値が変わる時のみ）。
        if self._timer is None:
            return
        interval = self._effective_interval()
        current = self._timer.interval()
        if current != interval:
            self._timer.setInterval(interval)
            self._log(
                "order_capture_interval_changed",
                order_capture_interval_changed=interval,
                previous_interval_ms=current,
                backoff_ms=self._backoff_ms,
            )
        else:
            self._log("order_capture_interval_not_changed", interval_ms=interval)

    def _apply_slow_tick_backoff(self, elapsed_ms: int) -> None:
        """slow tick を検知したら間隔を広げ、速いときは徐々に戻す（要件6）。"""
        if elapsed_ms >= _SLOW_TICK_WARN_MS:
            self._slow_tick_count += 1
            level = "strong_warning" if elapsed_ms >= _SLOW_TICK_STRONG_WARN_MS else "warning"
            _LOGGER.warning(
                "order_capture_perf_slow_tick elapsed_ms=%s level=%s", elapsed_ms, level
            )
            self._log(
                "order_capture_perf_slow_tick",
                order_capture_perf_tick_elapsed_ms=elapsed_ms,
                level=level,
            )
            new_backoff = min(_AUTO_CAPTURE_BACKOFF_MAX_MS, self._backoff_ms + _AUTO_CAPTURE_BACKOFF_STEP_MS)
            if new_backoff != self._backoff_ms:
                self._backoff_ms = new_backoff
                self._log("order_capture_interval_backoff", backoff_ms=self._backoff_ms)
            self._refresh_interval()
        elif self._backoff_ms > 0:
            # 速い tick が続いたらバックオフを1段ずつ解消する。
            self._backoff_ms = max(0, self._backoff_ms - _AUTO_CAPTURE_BACKOFF_STEP_MS)
            self._refresh_interval()

    def _maybe_emit_perf_snapshot(self, now: float) -> None:
        if (now - self._perf_last_snapshot) * 1000 < _PERF_SNAPSHOT_INTERVAL_MS:
            return
        self._perf_last_snapshot = now
        self._log(
            "order_capture_perf_snapshot",
            order_capture_perf_timer_active_count=1 if self._timer is not None else 0,
            backoff_ms=self._backoff_ms,
            tick_count=self._tick_count,
            slow_tick_count=self._slow_tick_count,
        )

    @Slot()
    def stop(self) -> None:
        # ワーカー自身のスレッドで呼ばれ、タイマーを安全に停止する（別スレッド停止を避ける）。
        self._stopped = True
        self._pending = False
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None
        self._log("worker_stopped")

    @Slot()
    def capture_once(self) -> None:
        if self._stopped or not self._enabled:
            return
        if self._busy:
            # 実行中に来た tick は並列実行せず、完了後に1回だけ再実行する（要件2）。
            if not self._pending:
                self._pending = True
                self._log("order_capture_tick_pending_set")
            self._log("order_capture_tick_skipped_running", busy=True)
            return
        self._refresh_interval()
        self._busy = True
        self._tick_count += 1
        started = time.monotonic()
        self._log("order_capture_tick_started", busy=True)
        try:
            from app.tks_cloud_capture import read_order_no_attempt_from_tkscloud8

            self._log("capture_fast_path_start", busy=True, used_fast_path=True)
            attempt = read_order_no_attempt_from_tkscloud8(debug=self._debug)
            order_no = captured_orders.normalize_captured_order_no(attempt.value) or ""
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if attempt.used_fast_path:
                self._log(
                    "capture_fast_path_success",
                    elapsed_ms=elapsed_ms,
                    busy=True,
                    used_fast_path=True,
                    captured_order_no=order_no,
                    latest_order_no=order_no,
                )
            else:
                self._log(
                    "capture_fast_path_failed",
                    elapsed_ms=elapsed_ms,
                    busy=True,
                    used_fast_path=False,
                    captured_order_no="",
                    latest_order_no="",
                )
            if attempt.full_scan_used:
                self._log("capture_full_scan_start", busy=True, used_fast_path=False)
                self._log(
                    "capture_full_scan_success" if order_no else "capture_full_scan_failed",
                    elapsed_ms=elapsed_ms,
                    busy=True,
                    used_fast_path=False,
                    captured_order_no=order_no,
                    latest_order_no=order_no,
                )
            if attempt.cache_updated:
                self._log("capture_cache_updated", elapsed_ms=elapsed_ms, busy=True)
            if attempt.cache_cleared:
                self._log("capture_cache_cleared", elapsed_ms=elapsed_ms, busy=True)
        except Exception:  # noqa: BLE001 - 自動取得の失敗でアプリを落とさない
            _LOGGER.warning("受注Noの自動取得中に例外が発生しました。", exc_info=True)
            self._log(
                "capture_full_scan_failed",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                busy=True,
                used_fast_path=False,
                exception_type=sys.exc_info()[0].__name__ if sys.exc_info()[0] else "",
                exception_message=str(sys.exc_info()[1] or ""),
                traceback=traceback.format_exc(),
            )
            order_no = ""
        finally:
            self._busy = False
        finished_at = time.monotonic()
        elapsed_ms = int((finished_at - started) * 1000)
        self._log(
            "order_capture_tick_finished",
            order_capture_perf_tick_elapsed_ms=elapsed_ms,
            order_capture_perf_detect_elapsed_ms=elapsed_ms,
        )
        # slow tick 検知とバックオフ、perf スナップショット（間引き）。
        self._apply_slow_tick_backoff(elapsed_ms)
        self._maybe_emit_perf_snapshot(finished_at)
        if self._stopped:
            return
        if order_no:
            # 復旧優先: emit 抑止で初回表示・再表示が消えるリスクを避ける。
            self._last_emitted_value = order_no
            self.captured.emit(order_no)
            self._log("signal_emitted", signal="captured")
        else:
            # 取得失敗時は最新値をすぐ消さない UI 側ロジックのため毎回通知する
            # （間隔は最短500ms以上で、頻度は抑えられている）。次の成功で必ず再通知
            # されるよう、直近 emit 値をリセットする。
            self._last_emitted_value = None
            self.capture_failed.emit()
            self._log("signal_emitted", signal="capture_failed")
        # 実行中に積まれた再実行要求を、完了後に1回だけ消費する（要件2）。
        if self._pending and not self._stopped and self._enabled:
            self._pending = False
            self._log("order_capture_tick_pending_consumed")
            QTimer.singleShot(0, self.capture_once)


class _CaptureOnceWorker(QObject):
    """1回分の受注No検出＋画面種別判定を「別プロセスhelper」で実行する worker。

    - 本体プロセス内では UIA/COM/Win32 の重い探索を一切直接呼ばない（要件2/4）。
    - QThread 内では短命の helper を subprocess として起動し、stdout の JSON を待つだけ。
    - subprocess には必ずタイムアウトを設定し、超過時は kill して「取得不可」にする（要件4）。
    - helper が異常終了しても・不正JSONを返しても、本体プロセスは落とさない（要件4）。
    - 結果は文字列/数値などの安全なプリミティブへ正規化して返し、COM/UIA オブジェクトは
      一切 UI スレッドへ渡さない（要件5）。
    - QTimer は持たず、QWidget/UI 部品にも触らない（要件4）。
    """

    # order_no, error, elapsed_ms, screen_type, generation
    finished = Signal(str, str, float, str, int)

    def __init__(
        self,
        generation: int = 0,
        *,
        command: list[str] | None = None,
        debug: bool = False,
        timeout_ms: int = _CAPTURE_HELPER_TIMEOUT_MS,
    ) -> None:
        super().__init__()
        # 起動時の画面世代番号。close/hide後の結果はこの番号の不一致で破棄する（要件7）。
        self._generation = int(generation)
        self._command = list(command) if command else None
        self._debug = bool(debug)
        self._timeout_s = max(0.2, float(timeout_ms) / 1000.0)
        self._proc = None
        self._kill_requested = False

    def request_kill(self) -> None:
        """UIスレッドから実行中helperの停止を要求する（close/hide時・要件7）。

        subprocess.Popen.kill() はスレッド安全。callback は世代不一致で破棄される。
        """
        self._kill_requested = True
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
                _LOGGER.info("order_import_capture_helper_kill_on_close")
            except Exception:  # noqa: BLE001 - kill失敗でもUIを落とさない
                pass

    def _set_proc(self, proc) -> None:
        # 手動取得と共通の helper wrapper から実行中Popenを受け取り、kill 用に握る。
        self._proc = proc

    @Slot()
    def run(self) -> None:
        # 手動取得(_on_capture)と完全に同一の共通経路 run_capture_via_helper を使う（要件6）。
        result = run_capture_via_helper(
            command=self._command,
            debug=self._debug,
            timeout_ms=int(round(self._timeout_s * 1000)),
            on_proc=self._set_proc,
        )
        self.finished.emit(
            str(result.get("order_no") or ""),
            str(result.get("error") or ""),
            float(result.get("elapsed_ms") or 0.0),
            str(result.get("screen_type") or "unknown"),
            self._generation,
        )


class _ExecuteWorker(QObject):
    """TKSCloud8 の実行操作（F12キー／「F12 実行」ボタンクリック）を監視するワーカー。

    - 有効な間だけ、前面が TKSCloud8 かつ実行操作のエッジを検知する。
    - キー・クリックは奪わない（GetAsyncKeyState で状態を読むだけ）。
    - 検知したら execute_detected(source, diag) を UI スレッドへ返す（保存はUI側）。
    - デバッグ時は、保存に至らないエッジも edge_diagnostics で通知する。
    - ワーカーから UI 部品には一切触らない。例外でもスレッドを落とさない。
    """

    execute_detected = Signal(str, dict)
    edge_diagnostics = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._debug = False
        self._stopped = False
        self._stopping = False
        # TKS側F12キー／実行ボタンのクリック座標監視を行うか（既定OFF・要件2/4）。
        # OFFのときは「受注入力（見出）→（明細）」の画面遷移だけを保存トリガーにする。
        self._f12_monitor_enabled = False
        self._f12_was_down = False
        self._mouse_was_down = False
        self._timer: QTimer | None = None
        self._latest_order_no = ""
        self._input_order_no = ""
        self._cached_execute_button_rect: tuple[int, int, int, int] | None = None
        self._detected_execute_button_rect: tuple[int, int, int, int] | None = None
        self._inferred_execute_button_rect: tuple[int, int, int, int] | None = None
        self._tkscloud_window_rect: tuple[int, int, int, int] | None = None
        self._rect_cache_updated_at = 0.0
        self._rect_cache_refreshing = False
        self._last_rect_reject_reason = ""
        self._previous_tks_title = ""
        self._current_tks_title = ""
        self._last_transition_saved_key: tuple[str, str] | None = None
        self._last_transition_saved_at = 0.0
        # 重い前面/対象ウィンドウ/タイトル判定は毎ポーリング（50ms）せず、間引いた
        # 間隔で再取得してキャッシュする。キー/マウスのエッジ検知だけ毎回行う（要件3）。
        self._ctx_refreshed_at = 0.0
        self._ctx_foreground = False
        self._ctx_target_exists = False
        self._ctx_title = ""

    @Slot(bool)
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        # 有効化のたびにエッジ状態をリセットし、押しっぱなしを新規押下と誤検知しない。
        self._f12_was_down = False
        self._mouse_was_down = False

    @Slot(bool)
    def set_debug(self, debug: bool) -> None:
        self._debug = bool(debug)

    @Slot(bool)
    def set_f12_monitor(self, enabled: bool) -> None:
        """TKS側F12/実行ボタンのクリック座標監視のON/OFF（既定OFF・要件2/4）。"""
        self._f12_monitor_enabled = bool(enabled)
        # 監視の有効化時はエッジ状態をリセットし、押しっぱなしを誤検知しない。
        self._f12_was_down = False
        self._mouse_was_down = False

    @Slot(str, str)
    def set_order_context(self, latest_order_no: str, input_order_no: str) -> None:
        self._latest_order_no = (latest_order_no or "").strip()
        self._input_order_no = (input_order_no or "").strip()

    def request_stop(self) -> None:
        self._stopped = True
        self._stopping = True
        self._log("worker_stop_requested", **self._base_diag())

    def _log(self, event: str, **payload: object) -> None:
        if self._debug:
            row = {
                "auto_save_enabled": self._enabled,
                "interval_ms": self._timer.interval() if self._timer is not None else None,
                "latest_order_no": self._latest_order_no,
                "input_order_no": self._input_order_no,
                "cached_execute_button_rect": list(self._cached_execute_button_rect) if self._cached_execute_button_rect else None,
                "detected_execute_button_rect": list(self._detected_execute_button_rect) if self._detected_execute_button_rect else None,
                "inferred_execute_button_rect": list(self._inferred_execute_button_rect) if self._inferred_execute_button_rect else None,
                "tkscloud_window_rect": list(self._tkscloud_window_rect) if self._tkscloud_window_rect else None,
                "is_stopping": self._stopping or self._stopped,
                "is_window_alive": True,
                **payload,
            }
            _write_execute_debug_event(event, _debug=True, **row)

    def _base_diag(self) -> dict:
        fg = _foreground_window_info() if self._debug else {}
        foreground = _tkscloud8_is_foreground()
        return {
            "auto_save_enabled": self._enabled,
            "foreground_title": fg.get("title"),
            "foreground_process_name": fg.get("process_name"),
            "foreground_is_tkscloud8": foreground,
            "target_order_entry_window_exists": _tks_order_entry_window_running(),
            "target_window_exists": _tks_order_entry_window_running(),
            "tkscloud_window_exists": bool(self._tkscloud_window_rect) or foreground,
            "latest_order_no": self._latest_order_no,
            "input_order_no": self._input_order_no,
            "mouse_pos": None,
            "raw_mouse_pos": None,
            "normalized_mouse_pos": None,
            "raw_execute_button_rect": None,
            "normalized_execute_button_rect": None,
            "raw_tkscloud_window_rect": None,
            "normalized_tkscloud_window_rect": None,
            "monitor_handle": None,
            "monitor_rect": None,
            "dpi_scale_x": None,
            "dpi_scale_y": None,
            "coordinate_space": None,
            "cached_execute_button_rect": list(self._cached_execute_button_rect) if self._cached_execute_button_rect else None,
            "detected_execute_button_rect": list(self._detected_execute_button_rect) if self._detected_execute_button_rect else None,
            "inferred_execute_button_rect": list(self._inferred_execute_button_rect) if self._inferred_execute_button_rect else None,
            "tkscloud_window_rect": list(self._tkscloud_window_rect) if self._tkscloud_window_rect else None,
            "previous_tks_title": self._previous_tks_title,
            "current_tks_title": self._current_tks_title,
            "transition_detected": False,
            "click_inside_execute_button": False,
            "reject_reason": self._last_rect_reject_reason,
            "save_result": None,
            "save_message": None,
            "is_stopping": self._stopping or self._stopped,
            "is_window_alive": True,
            "exception_type": None,
            "exception_message": None,
            "traceback": None,
        }

    @Slot()
    def start(self) -> None:
        # 画面につき1本だけ生成する（多重生成防止）。
        if self._timer is not None:
            return
        dpi = _set_process_dpi_awareness()
        # 高頻度ポーリング（<500ms）は許可しない。指定が短すぎる場合は最短値へ引き上げる（要件2）。
        interval = _EXECUTE_POLL_INTERVAL_MS
        if interval < _EXECUTE_MONITOR_MIN_INTERVAL_MS:
            self._log(
                "order_import_execute_monitor_interval_rejected",
                requested_interval_ms=interval,
                applied_interval_ms=_EXECUTE_MONITOR_MIN_INTERVAL_MS,
            )
            interval = _EXECUTE_MONITOR_MIN_INTERVAL_MS
        self._timer = QTimer(self)
        self._timer.setInterval(interval)
        self._timer.timeout.connect(self.poll_once)
        self._timer.start()
        self._current_tks_title = _tkscloud_window_title()
        self._previous_tks_title = self._current_tks_title
        # コンテキストキャッシュも初期化し、初回ポーリングで誤検知の遷移を起こさない。
        self._ctx_title = self._current_tks_title
        dpi_diag = self._base_diag()
        dpi_diag.update(dpi)
        self._log("dpi_awareness_set", **dpi_diag)
        self._log("execute_worker_start")

    @Slot()
    def stop(self) -> None:
        # ワーカー自身のスレッドで呼ばれ、タイマーを安全に停止する。
        self._stopped = True
        self._stopping = True
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None
        self._log("worker_stopped", **self._base_diag())

    @Slot()
    def poll_once(self) -> None:
        if self._stopped or not self._enabled:
            if self._stopped:
                self._log("worker_stopping_skip", **self._base_diag())
            return
        try:
            self._poll()
        except Exception:  # noqa: BLE001 - 監視処理でスレッドを落とさない
            _LOGGER.warning("実行監視中に例外が発生しました。監視を停止します。", exc_info=True)
            self._log(
                "order_capture_worker_exception",
                exception_type=sys.exc_info()[0].__name__ if sys.exc_info()[0] else "",
                exception_message=str(sys.exc_info()[1] or ""),
                traceback=traceback.format_exc(),
            )
            self._log(
                "worker_exception",
                exception_type=sys.exc_info()[0].__name__ if sys.exc_info()[0] else "",
                exception_message=str(sys.exc_info()[1] or ""),
                traceback=traceback.format_exc(),
            )
            # 例外時は即停止し、壊れた状態でポーリングを続けない（要件2）。
            self._log("order_import_execute_monitor_stopped", reason="poll_exception")
            self.stop()

    def _refresh_execute_rect_cache_if_needed(self) -> None:
        if self._stopped or self._stopping:
            self._log("worker_stopping_skip", **self._base_diag())
            return
        now = time.monotonic()
        if (now - self._rect_cache_updated_at) * 1000 < _EXECUTE_RECT_CACHE_INTERVAL_MS:
            return
        if self._rect_cache_refreshing:
            return
        self._rect_cache_refreshing = True
        self._log("execute_rect_cache_start", **self._base_diag())
        try:
            window_rect = _tkscloud_window_rect()
            self._tkscloud_window_rect = window_rect
            detected = execute_button_rect_from_tkscloud8()
            if self._stopped or self._stopping:
                self._log("worker_stopping_skip", **self._base_diag())
                return
            self._detected_execute_button_rect = detected
            valid, rect, reason = _valid_execute_button_rect(
                detected,
                tkscloud_window_rect=window_rect,
            )
            if valid:
                self._cached_execute_button_rect = rect
                self._inferred_execute_button_rect = None
                self._last_rect_reject_reason = ""
                self._rect_cache_updated_at = now
                self._log("execute_rect_cache_success", **self._base_diag())
                return
            if detected is not None:
                self._last_rect_reject_reason = reason
                invalid_diag = self._base_diag()
                invalid_diag["reject_reason"] = reason
                self._log("execute_rect_cache_invalid", **invalid_diag)
            inferred = _infer_execute_button_rect_from_window(window_rect)
            self._inferred_execute_button_rect = inferred
            valid_inferred, inferred_rect, inferred_reason = _valid_execute_button_rect(
                inferred,
                tkscloud_window_rect=window_rect,
            )
            if valid_inferred:
                self._cached_execute_button_rect = inferred_rect
                self._last_rect_reject_reason = ""
                self._rect_cache_updated_at = now
                self._log("execute_rect_cache_success", **self._base_diag())
            else:
                self._cached_execute_button_rect = None
                self._last_rect_reject_reason = inferred_reason or reason or "rect_not_found"
                self._rect_cache_updated_at = now
                failed_diag = self._base_diag()
                failed_diag["reject_reason"] = self._last_rect_reject_reason
                self._log("execute_rect_cache_failed", **failed_diag)
        except Exception:  # noqa: BLE001
            self._cached_execute_button_rect = None
            self._last_rect_reject_reason = "exception"
            self._rect_cache_updated_at = now
            exception_diag = self._base_diag()
            exception_diag["exception_type"] = sys.exc_info()[0].__name__ if sys.exc_info()[0] else ""
            exception_diag["exception_message"] = str(sys.exc_info()[1] or "")
            exception_diag["traceback"] = traceback.format_exc()
            self._log("worker_exception", **exception_diag)
        finally:
            self._rect_cache_refreshing = False

    def _has_valid_order_context(self) -> bool:
        return bool(
            captured_orders.normalize_captured_order_no(self._latest_order_no)
            or captured_orders.normalize_captured_order_no(self._input_order_no)
        )

    def _transition_save_key(self) -> tuple[str, str] | None:
        order_no = (
            captured_orders.normalize_captured_order_no(self._latest_order_no)
            or captured_orders.normalize_captured_order_no(self._input_order_no)
        )
        if not order_no:
            return None
        return ("tks_screen_transition", order_no)

    def _transition_debounced(self, now: float) -> bool:
        key = self._transition_save_key()
        if key is None:
            return True
        if (
            self._last_transition_saved_key == key
            and (now - self._last_transition_saved_at) * 1000 < _EXECUTE_TRANSITION_DEBOUNCE_MS
        ):
            return True
        self._last_transition_saved_key = key
        self._last_transition_saved_at = now
        return False

    @staticmethod
    def _looks_like_execute_transition(previous: str, current: str) -> bool:
        if not current or current == previous:
            return False
        return "受注入力（明細）" in current

    def _check_screen_transition(self, diag: dict, now: float) -> bool:
        # タイトルは間引き取得済みのキャッシュ値を使う（毎ポーリングの win32 呼出を避ける）。
        current_title = self._ctx_title or _tkscloud_window_title()
        previous_title = self._current_tks_title
        self._previous_tks_title = previous_title
        self._current_tks_title = current_title
        transition = self._looks_like_execute_transition(previous_title, current_title)
        diag["previous_tks_title"] = previous_title
        diag["current_tks_title"] = current_title
        diag["transition_detected"] = transition
        if not transition:
            return False
        if not self._enabled:
            diag["reject_reason"] = "auto_save_disabled"
            self._log("execute_click_rejected", **diag)
            return False
        if not self._has_valid_order_context():
            diag["reject_reason"] = "no_order_no"
            self._log("execute_click_rejected", **diag)
            return False
        if self._transition_debounced(now):
            diag["reject_reason"] = "transition_debounced"
            self._log("execute_click_rejected", **diag)
            return False
        diag["source"] = "detail_screen_detected"
        diag["detected_source"] = "detail_screen_detected"
        self._log("order_import_detail_screen_detected", **diag)
        self._log("execute_signal_emitted", **diag)
        if not self._stopped and not self._stopping:
            self.execute_detected.emit("detail_screen_detected", diag)
        return True

    def _poll(self) -> None:
        # 「F12 実行」ボタン矩形の取得は重いWin32/UIA処理。クリック座標監視が
        # 明示ONのときだけ行う（既定OFF・要件2/4）。
        if self._f12_monitor_enabled:
            self._refresh_execute_rect_cache_if_needed()
        if self._stopped or self._stopping:
            self._log("worker_stopping_skip", **self._base_diag())
            return
        now = time.monotonic()
        # キー/マウスのエッジ検知は F12 monitor が明示ONのときだけ行う（既定OFF・要件2/4）。
        # OFFのときは画面遷移（見出→明細）だけを保存トリガーにする。
        if self._f12_monitor_enabled:
            f12_down = _f12_key_is_down()
            f12_edge = f12_down and not self._f12_was_down
            self._f12_was_down = f12_down
            mouse_down = _left_mouse_is_down()
            mouse_edge = mouse_down and not self._mouse_was_down
            self._mouse_was_down = mouse_down
        else:
            f12_down = mouse_down = False
            f12_edge = mouse_edge = False
            if self._debug:
                self._log("order_import_execute_monitor_poll_suppressed", reason="f12_monitor_disabled")

        # 重い前面/対象ウィンドウ/タイトル判定は間引いて再取得・キャッシュする（要件3）。
        context_due = (now - self._ctx_refreshed_at) * 1000 >= _EXECUTE_CONTEXT_REFRESH_MS
        if context_due:
            self._ctx_foreground = _tkscloud8_is_foreground()
            self._ctx_target_exists = _tks_order_entry_window_running()
            self._ctx_title = _tkscloud_window_title()
            self._ctx_refreshed_at = now
        foreground = self._ctx_foreground
        target_exists = self._ctx_target_exists

        # エッジも遷移評価もなく、debug でもなければ、重い diag 構築・ログを丸ごと省く。
        # これにより無操作時の1ポーリングは「キー/マウス状態読取のみ」になる。
        need_processing = self._debug or f12_edge or mouse_edge or context_due
        if not need_processing:
            return

        fg = _foreground_window_info() if self._debug else {}
        diag = {
            "source": None,
            "auto_save_enabled": self._enabled,
            "foreground_title": fg.get("title"),
            "foreground_process_name": fg.get("process_name"),
            "foreground_is_tkscloud8": foreground,
            "target_order_entry_window_exists": target_exists,
            "target_window_exists": target_exists,
            "tkscloud_window_exists": foreground or target_exists,
            "latest_order_no": self._latest_order_no,
            "input_order_no": self._input_order_no,
            "f12_key_down": f12_down,
            "f12_edge_detected": f12_edge,
            "left_mouse_down": mouse_down,
            "mouse_edge_detected": mouse_edge,
            "mouse_pos": None,
            "raw_mouse_pos": None,
            "normalized_mouse_pos": None,
            "raw_execute_button_rect": None,
            "normalized_execute_button_rect": None,
            "raw_tkscloud_window_rect": None,
            "normalized_tkscloud_window_rect": None,
            "monitor_handle": None,
            "monitor_rect": None,
            "dpi_scale_x": None,
            "dpi_scale_y": None,
            "coordinate_space": None,
            "execute_button_rect": list(self._cached_execute_button_rect) if self._cached_execute_button_rect else None,
            "cached_execute_button_rect": list(self._cached_execute_button_rect) if self._cached_execute_button_rect else None,
            "detected_execute_button_rect": list(self._detected_execute_button_rect) if self._detected_execute_button_rect else None,
            "inferred_execute_button_rect": list(self._inferred_execute_button_rect) if self._inferred_execute_button_rect else None,
            "tkscloud_window_rect": list(self._tkscloud_window_rect) if self._tkscloud_window_rect else None,
            "previous_tks_title": self._previous_tks_title,
            "current_tks_title": self._current_tks_title,
            "transition_detected": False,
            "click_inside_execute_button": False,
            "reject_reason": None,
            "detected_source": None,
            "save_result": None,
            "save_message": None,
            "is_stopping": self._stopping or self._stopped,
            "is_window_alive": True,
            "exception_type": None,
            "exception_message": None,
            "traceback": None,
        }
        self._log("execute_poll", **diag)
        self._log("foreground_checked", **diag)
        if f12_down:
            self._log("f12_key_down", **diag)
        if f12_edge:
            self._log("f12_edge_detected", **diag)
        if mouse_down:
            self._log("mouse_down", **diag)
        if mouse_edge:
            self._log("mouse_edge_detected", **diag)

        transition_emitted = self._check_screen_transition(diag, now)
        if transition_emitted:
            return

        if not (f12_edge or mouse_edge):
            return

        # 1. F12キー押下エッジ。前面判定は補助情報とし、保存候補があれば通知する。
        has_order_context = bool(self._latest_order_no or self._input_order_no)
        if f12_edge and (foreground or has_order_context):
            diag["detected_source"] = "f12_key"
            diag["source"] = "f12_key"
            self._log("order_import_f12_monitor_edge_detected", **diag)
            self._log("execute_key_detected", **diag)
            self._log("execute_signal_emitted", **diag)
            self.execute_detected.emit("f12_key", diag)
            return

        # 2. 「F12 実行」ボタンの左クリックエッジ。クリック位置がボタン上なら前面でなくても通知する。
        if mouse_edge:
            pos = _get_cursor_pos()
            rect = self._cached_execute_button_rect
            rect_age_ms = int((now - self._rect_cache_updated_at) * 1000) if self._rect_cache_updated_at else None
            valid, rect, reject_reason = _valid_execute_button_rect(
                rect,
                tkscloud_window_rect=self._tkscloud_window_rect,
            )
            if rect_age_ms is not None and rect_age_ms > _EXECUTE_RECT_CACHE_TTL_MS:
                valid = False
                reject_reason = "rect_cache_stale"
            coord = _coordinate_snapshot(
                mouse_pos=pos,
                execute_button_rect=rect,
                tkscloud_window_rect=self._tkscloud_window_rect,
            )
            normalized_pos = tuple(coord["normalized_mouse_pos"]) if coord.get("normalized_mouse_pos") else None
            normalized_rect = _rect_tuple(coord.get("normalized_execute_button_rect"))
            inside = _point_in_rect(normalized_pos, normalized_rect)
            diag.update(coord)
            self._log("coordinate_normalized", **diag)
            diag["execute_button_rect"] = list(rect) if rect else None
            diag["cached_execute_button_rect"] = list(self._cached_execute_button_rect) if self._cached_execute_button_rect else None
            diag["detected_execute_button_rect"] = list(self._detected_execute_button_rect) if self._detected_execute_button_rect else None
            diag["inferred_execute_button_rect"] = list(self._inferred_execute_button_rect) if self._inferred_execute_button_rect else None
            diag["tkscloud_window_rect"] = list(self._tkscloud_window_rect) if self._tkscloud_window_rect else None
            diag["mouse_pos"] = list(normalized_pos) if normalized_pos else (list(pos) if pos else None)
            diag["click_inside_execute_button"] = bool(valid and inside)
            if not pos:
                diag["reject_reason"] = "mouse_pos_missing"
            elif not rect:
                diag["reject_reason"] = "rect_missing"
            elif not valid:
                diag["reject_reason"] = reject_reason or "invalid_rect"
            elif not inside:
                diag["reject_reason"] = "click_outside_rect"
            else:
                diag["reject_reason"] = None
            if valid and inside:
                diag["detected_source"] = "execute_button_click"
                diag["source"] = "execute_button_click"
                self._log("execute_click_detected", **diag)
                self._log("execute_click_accepted", **diag)
                self._log("execute_signal_emitted", **diag)
                if not self._stopped and not self._stopping:
                    self.execute_detected.emit("execute_button_click", diag)
                return
            self._log(
                "execute_click_rejected",
                **diag,
            )

        # 保存に至らないエッジ。デバッグ時のみ診断を通知する（原因追跡用）。
        if self._debug and diag:
            self.edge_diagnostics.emit(diag)


class TksOrderCaptureWindow(QWidget):
    """TKS受注No取込の小画面（常に手前・別ウィンドウ・コンパクト）。"""

    # 画面が閉じられたことを起動元（LauncherWindow）へ通知し、参照を解放させる。
    closed = Signal()

    def __init__(self, voucher_window_provider=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 開いている伝票作成・印刷画面（VoucherWindow）を都度参照するコールバック。
        self._voucher_window_provider = voucher_window_provider
        self._settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        self._latest_order_no = ""
        # 実行ボタン/F12 が自動取得の完了を待たずに即保存できるよう、最後に検出できた
        # 有効な受注Noを保持する（要件7）。
        self._last_valid_order_no = ""
        self._latest_detected_order_no = ""
        self._closing = False
        # UI 更新過多を防ぐための直近表示値キャッシュ（変化時のみ setText/setEnabled）。
        self._last_status_text: str | None = None
        self._last_status_tooltip: str | None = None
        self._last_latest_label = ""
        self._last_count_text = ""
        self._last_add_enabled: bool | None = None
        self._last_add_tooltip: str | None = None
        # 同一イベントの連続ログを間引くための直近ログキー（要件4）。
        self._last_log_key: tuple | None = None
        # 実行ボタン／F12保存の多重実行を防ぐガード（例外時も必ず解除する。要件1）。
        self._saving = False
        self._manual_save_in_progress = False
        self._last_f12_save_requested_at = 0.0
        # 保存中に検出した自動保存要求のキュー（正規化済み受注No → (source, diag)）。
        # 最後の1件だけでなく全件を保持し、連続取得でも取りこぼさない（要件7）。
        self._pending_auto_save: "OrderedDict[str, tuple[str, dict]]" = OrderedDict()
        self._pending_auto_save_retry_scheduled = False
        # ディスクflushのデバウンス予約フラグ（多重予約を防ぐ）。
        self._flush_timer_scheduled = False
        # TKS「受注入力（明細）」検出保存の重複防止。
        self._last_detail_trigger_saved_order_no = ""
        self._detail_trigger_active = False
        self._last_seen_tks_screen_kind = ""
        # capture worker から得た画面種別で見出→明細遷移保存を扱うための状態（要件3）。
        # execute monitor 専用スレッドは既定で起動しないため、遷移保存はこの経路で行う。
        self._last_header_order_no = ""
        self._last_capture_transition_order_no = ""
        self._last_screen_type = "none"
        # 画面世代番号。close/hide のたびに増やし、在庫の worker 結果を破棄する（要件7）。
        self._generation = 0
        # 常駐処理（timer/worker/heartbeat）の二重起動防止フラグ（要件4）。
        self._workers_started = False
        self._heartbeat_timer: QTimer | None = None
        self._shown_at = 0.0
        # プロセスごとのクラッシュ追跡フックを一度だけ仕込む（要件6）。
        _install_crash_tracking()

        self.setWindowTitle("TKS受注No取込")
        self.setWindowIcon(QIcon(str(resource_path("assets/app_icon.ico"))))
        # トップレベルの独立ウィンドウ（親を持たない）。常に手前に出す小画面とし、
        # LauncherWindow とは Qt の親子関係を作らない（親の前面化に引きずられない）。
        # 「常に手前に表示」は保存済み設定で復元する（要件3。初期値ON=従来挙動）。
        self._always_on_top = self._load_always_on_top_setting()
        # 最小化・最大化は無効化し、閉じるボタンだけ残す（要件3/4）。
        base_flags = (
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        if self._always_on_top:
            base_flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(base_flags)
        self._log_order_event("order_import_window_minimize_disabled")
        self._log_order_event(
            "order_import_always_on_top_restored", always_on_top=self._always_on_top
        )
        # コンパクト化: 高さを抑え、横幅は多少広くてよい（要件・画面コンパクト化）。
        clamp_window_to_available_geometry(
            self,
            desired_width=400,
            desired_height=150,
            min_width=360,
            min_height=140,
        )

        # 状態表示（1行に集約: 最新／状態／件数）
        self._latest_label = QLabel("-")
        self._status_label = QLabel("未保存")
        self._status_label.setToolTip("未保存")
        self._count_label = QLabel("0 件")

        # 手入力欄（UI Automation 取得が未実装の間のフォールバック）
        self._order_input = QLineEdit()
        self._order_input.setPlaceholderText("")
        # 自動保存ON時の手入力禁止バッヂ（🔒）。
        self._lock_badge = QLabel("🔒")
        self._lock_badge.setToolTip("自動保存中は手入力できません")
        self._lock_badge.setVisible(False)

        # ボタン（横一列・短い文言）。アプリ側の手動「実行」保存ボタンは廃止した。
        # 保存は「自動保存ON時にTKS側で実行/F12して見出→明細へ進んだとき」だけ行う。
        self._capture_button = QPushButton("取得")
        self._capture_button.setObjectName("captureButton")
        self._save_button = QPushButton("保存")
        self._save_button.setObjectName("saveButton")
        self._add_to_voucher_button = QPushButton("追加")
        self._add_to_voucher_button.setObjectName("addButton")
        self._list_button = QPushButton("一覧")
        self._list_button.setObjectName("listButton")
        self._close_button = QPushButton("閉じる")
        self._close_button.setObjectName("closeButton")
        self.setStyleSheet(_CAPTURE_BUTTON_STYLE)

        # 自動設定: 取得（定期自動取得）／保存（実行時に自動保存）
        self._auto_capture_check = QCheckBox("取得")
        self._auto_capture_check.setToolTip(
            "TKSCloud8 の受注Noをこの画面へ自動表示します（保存はしません）。\n"
            "OFFにしても『保存』がONなら、見出→明細遷移の自動保存は継続します。"
        )
        self._auto_capture_check.setChecked(self._load_auto_capture_setting())
        self._auto_save_check = QCheckBox("保存")
        self._auto_save_check.setToolTip(
            "自動保存ONの場合、TKS側で実行/F12して「受注入力（見出）」→「受注入力（明細）」へ"
            "進んだ時に、直前に見出で検出していた受注Noを自動保存します。"
        )
        self._auto_save_check.setChecked(self._load_auto_save_setting())

        # 自動保存の意味が分かる案内文（アプリ側「実行」ボタン廃止に伴う説明）。
        self._auto_save_hint = QLabel(
            "自動保存ONの場合、TKS側で実行/F12後（見出→明細）に保存されます。"
        )
        self._auto_save_hint.setWordWrap(True)
        self._auto_save_hint.setStyleSheet("color: #6B7280; font-size: 11px;")

        # 「常に手前に表示」チェック（要件3）。自動ラベル行の右端に配置する。
        self._always_on_top_check = QCheckBox("常に手前に表示")
        self._always_on_top_check.setToolTip("この小画面を常に手前に表示します。")
        self._always_on_top_check.setChecked(self._always_on_top)

        # 自動取得の連続失敗カウンタ（最新値をすぐ消さないため）。
        self._auto_captured_value = ""
        self._auto_capture_failures = 0

        # 自動処理ワーカー（表示中のみ稼働）。
        self._auto_capture_timer: QTimer | None = None
        self._capture_tick_running = False
        self._auto_capture_initial_tick_scheduled = False
        # helper 起動中フラグ（要件2）。QThread は使わず QProcess で helper を回す。
        self._capture_process_running = False
        self._capture_rerun_requested = False
        self._manual_capture_rerun_requested = False
        self._capture_rerun_count = 0
        # helper連続失敗/timeout時のバックオフ状態（要件7）。
        self._capture_consecutive_failures = 0
        self._capture_backoff_active = False
        # 見出→明細遷移待ちの fast poll 状態（自動保存の体感遅延改善・要件7）。
        self._fast_poll_active = False
        # 受注No取得 helper を回す QProcess（main thread 所有・要件2）。QThread は廃止した。
        self._capture_process: QProcess | None = None
        self._capture_timeout_timer: QTimer | None = None
        self._capture_process_source = "auto"
        self._capture_process_generation = 0
        self._capture_process_started_at = 0.0
        self._capture_process_timeout_ms = _CAPTURE_HELPER_AUTO_TIMEOUT_MS
        self._capture_process_stdout = ""
        self._capture_process_stderr = ""
        # finished/timeout/error の全経路で結果は1回だけ配送する（二重配送防止）。
        self._capture_result_delivered = False
        # 旧QThread参照は保持しない（heartbeat/resource snapshot 互換のため属性だけ残す）。
        self._capture_thread: QThread | None = None
        self._capture_worker: _CaptureOnceWorker | None = None
        self._execute_thread: QThread | None = None
        self._execute_worker: _ExecuteWorker | None = None

        # 保存済み一覧画面（単一インスタンス）。
        self._list_window: QWidget | None = None

        self._build_layout()

        self._capture_button.clicked.connect(self._on_capture)
        self._save_button.clicked.connect(self._on_save)
        self._add_to_voucher_button.clicked.connect(self._on_add_to_voucher)
        self._list_button.clicked.connect(self._on_open_list)
        self._close_button.clicked.connect(self.close)
        self._auto_capture_check.toggled.connect(self._on_auto_capture_toggled)
        self._auto_save_check.toggled.connect(self._on_auto_save_toggled)
        self._always_on_top_check.toggled.connect(self._on_always_on_top_toggled)
        self._order_input.textChanged.connect(self._on_order_input_changed)
        # Enter でも手入力保存できるようにする（手動「取得」は別ボタンのため衝突しない・要件1）。
        self._order_input.returnPressed.connect(self._on_save)

        # アプリ側F12ショートカットは廃止した。TKS側のF12/実行は当然そのまま利用でき、
        # 保存は自動保存ON時の「受注入力（見出）→（明細）」遷移だけで行う。アプリ画面上の
        # F12キーが保存処理を起動しないよう、この画面には QShortcut を登録しない。

        self._refresh_count()
        self._refresh_add_to_voucher_enabled()
        self._apply_auto_save_ui()
        self._refresh_button_styles()
        self._apply_fixed_window_size()
        _LIVE_CAPTURE_WINDOWS.add(self)
        app = QApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.connect(self._shutdown_for_app_quit)
            except (RuntimeError, TypeError):
                # 既に破棄中の QApplication では接続できないことがある。
                pass

    @property
    def _capture_worker_running(self) -> bool:
        """helper 起動中フラグの互換アクセサ（実体は _capture_process_running）。

        QThread worker から QProcess へ移行したが、既存呼び出し側・テストが参照する
        `_capture_worker_running` を維持するためのエイリアス（要件2）。
        """
        return self._capture_process_running

    @_capture_worker_running.setter
    def _capture_worker_running(self, value: object) -> None:
        self._capture_process_running = bool(value)

    def _shutdown_for_app_quit(self) -> None:
        """QApplication 終了時に自動取得 timer/worker を必ず停止する。"""
        try:
            self._closing = True
            self._generation += 1
            self._stop_workers()
        except Exception as exc:  # noqa: BLE001
            self._log_slot_exception(
                "order_import_unhandled_slot_exception", exc, source="aboutToQuit"
            )

    def _on_order_input_changed(self, *_args: object) -> None:
        self._refresh_add_to_voucher_enabled()
        if self._status_label.text() == "取得不可" and self._has_valid_current_order_no():
            self._set_status("取得OK", "受注Noを取得できています")
        self._sync_execute_context()

    def _sync_execute_context(self) -> None:
        """実行検知 worker に、F12 緩和判定用の最新候補を渡す。"""
        worker = self._execute_worker
        if worker is None:
            return
        try:
            worker.set_order_context(self._latest_order_no, self._order_input.text().strip())
        except Exception:  # noqa: BLE001 - 診断用文脈の同期失敗でUIを落とさない
            _LOGGER.debug("実行検知workerへの受注No文脈同期に失敗しました。", exc_info=True)

    # ── レイアウト ────────────────────────────────────────────────────────────
    def _build_layout(self) -> None:
        # 1行目: 最新／状態／件数（コンパクトに横並び）
        info = QHBoxLayout()
        info.addWidget(QLabel("最新:"))
        info.addWidget(self._latest_label)
        info.addSpacing(8)
        info.addWidget(QLabel("状態:"))
        info.addWidget(self._status_label, 1)
        info.addSpacing(8)
        info.addWidget(QLabel("件数:"))
        info.addWidget(self._count_label)

        # 2行目: 受注No手入力欄 + 🔒バッヂ
        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("受注No"))
        input_row.addWidget(self._order_input, 1)
        input_row.addWidget(self._lock_badge)

        # 3行目: 自動設定（取得／保存）＋「常に手前に表示」（行の右端寄せ・要件3）
        auto_row = QHBoxLayout()
        auto_row.addWidget(QLabel("自動:"))
        auto_row.addWidget(self._auto_capture_check)
        auto_row.addWidget(self._auto_save_check)
        auto_row.addStretch(1)
        auto_row.addWidget(self._always_on_top_check)

        # 4行目: 操作ボタン（横一列）。手動「実行」ボタンは廃止済み。
        buttons = QHBoxLayout()
        buttons.addWidget(self._capture_button)
        buttons.addWidget(self._save_button)
        buttons.addWidget(self._add_to_voucher_button)
        buttons.addWidget(self._list_button)
        buttons.addWidget(self._close_button)

        root = QVBoxLayout()
        root.addLayout(info)
        root.addLayout(input_row)
        root.addLayout(auto_row)
        root.addWidget(self._auto_save_hint)
        root.addLayout(buttons)
        self.setLayout(root)

    # ── 取得・保存 ────────────────────────────────────────────────────────────
    def _resolve_order_no(self) -> str:
        """受注Noを決定する。UIA 取得 → 手入力欄の順で採用する。"""
        captured = ""
        try:
            captured = (capture_order_no_from_tkscloud8() or "").strip()
        except Exception:  # noqa: BLE001 - 取得失敗でも落とさない
            _LOGGER.warning("TKSCloud8 からの受注No取得に失敗しました。", exc_info=True)
            captured = ""
        if captured:
            return captured
        return self._order_input.text().strip()

    def _capture_failure_text(self) -> str:
        """取得失敗時の表示文字列。可能なら失敗理由の詳細を添える。"""
        base = "受注Noを取得できませんでした"
        # 手入力欄が空のときのみ、取得失敗の詳細理由を添える。
        if self._order_input.text().strip():
            return base
        detail = capture_failure_detail()
        return f"{base}（{detail}）" if detail else base

    def _has_valid_current_order_no(self) -> bool:
        return bool(
            captured_orders.normalize_captured_order_no(self._latest_order_no)
            or captured_orders.normalize_captured_order_no(self._order_input.text().strip())
        )

    # 高頻度で繰り返し得る（＝連続同一なら間引く）イベント。状態変化や保存など
    # 一度きりの節目イベントは対象外にし、常に記録する。
    _DEDUP_LOG_EVENTS = frozenset(
        {
            "order_import_status_reset_for_new_order",
            "order_import_detected_existing",
            "order_import_detected_new",
        }
    )

    def _log_order_event(self, event: str, **payload: object) -> None:
        """受注No取込の状態・保存イベントを標準ロガーへ記録する（常に実態に合わせる）。

        同一 event＋受注No の連続出力は間引き、無操作時のログ肥大を防ぐ（要件4）。
        """
        try:
            if event in self._DEDUP_LOG_EVENTS:
                key = (event, str(payload.get("order_no", "")))
                if key == self._last_log_key:
                    return
                self._last_log_key = key
            else:
                # 節目イベントが挟まったら dedup 基準をリセットする。
                self._last_log_key = None
            _LOGGER.info("%s %s", event, payload)
        except Exception:  # noqa: BLE001 - ログ失敗でUIを落とさない
            pass

    def _is_qobject_valid(self, obj: object) -> bool:
        if obj is None:
            return False
        if shiboken6 is not None:
            try:
                return bool(shiboken6.isValid(obj))
            except Exception:  # noqa: BLE001
                return False
        return True

    def _log_slot_exception(self, event: str, exc: BaseException, **payload: object) -> None:
        self._log_order_event(
            event,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            traceback=traceback.format_exc(),
            **payload,
        )
        self._log_order_event(
            "order_import_unhandled_slot_exception",
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            traceback=traceback.format_exc(),
            **payload,
        )

    def _order_no_already_saved(self, order_no: str) -> bool:
        """指定受注Noが保存済み一覧に既に存在するか（例外時は False）。"""
        normalized = captured_orders.normalize_captured_order_no(order_no)
        if not normalized:
            return False
        try:
            for entry in captured_orders.load_captured_orders():
                if str(entry.get("order_no", "")).strip() == normalized:
                    return True
        except Exception:  # noqa: BLE001 - 一覧読み込み失敗でUIを落とさない
            return False
        return False

    def _set_status(self, text: str, detail: str | None = None) -> None:
        detail = detail or text
        # 変化がなければ setText/setToolTip もログも行わない（UI更新過多を防ぐ・要件5）。
        if text == self._last_status_text and detail == self._last_status_tooltip:
            return
        previous = self._last_status_text if self._last_status_text is not None else self._status_label.text()
        if text == "取得不可":
            _write_worker_debug_event(
                "status_changed_to_capture_failed",
                previous_status=previous,
                new_status=text,
                latest_order_no=self._latest_order_no,
                input_order_no=self._order_input.text().strip(),
                consecutive_capture_failures=self._auto_capture_failures,
                reason=detail,
            )
            # クラッシュ調査（flush付き）: status更新の直前直後を必ず残す（要件5）。
            _write_crash_probe_event(
                "status_changed_to_capture_failed",
                previous_status=previous,
                consecutive_capture_failures=self._auto_capture_failures,
                reason=detail,
            )
            _write_crash_probe_event("order_import_crash_probe_before_status_update", text=text)
        self._status_label.setText(text)
        self._status_label.setToolTip(detail)
        if text == "取得不可":
            _write_crash_probe_event("order_import_crash_probe_after_status_update", text=text)
        self._last_status_text = text
        self._last_status_tooltip = detail
        if text != previous:
            self._log_order_event(
                "order_import_status_changed",
                previous_status=previous,
                new_status=text,
                detail=detail,
            )

    def _refresh_button_styles(self) -> None:
        """有効/無効変更後も stylesheet の disabled 表示を確実に再評価する。"""
        for button in (
            self._capture_button,
            self._save_button,
            self._add_to_voucher_button,
            self._list_button,
            self._close_button,
        ):
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def _reflect_detected_order_no(self, order_no: str) -> None:
        """取得できた受注Noを最新表示・手入力欄・状態表示へ反映する（要件4）。

        受注Noが変わったら前回の「保存OK/保存済み」状態を持ち越さない。
        既に保存済みなら「保存済み」、未保存なら「取得OK」と実態に合わせる。
        取得しただけ／受注Noが変わっただけでは保存しない（保存は実行/F12/自動保存時のみ）。
        """
        order_no = (order_no or "").strip()
        if not order_no:
            return
        # 実行ボタン/F12 が検出完了を待たずに保存できるよう、有効値を保持する（要件7）。
        # 取得は UIA→Win32 の順で、UIAで7桁以上のASCII数字が取れていれば、Win32側の
        # ウィンドウ列挙が失敗（window_found=false）でも有効な受注Noとして受け入れる（要件5）。
        normalized = captured_orders.normalize_captured_order_no(order_no)
        if normalized:
            self._last_valid_order_no = order_no
            self._latest_detected_order_no = order_no
            self._log_order_event(
                "order_import_detected_order_accepted_from_uia", order_no=normalized
            )
        else:
            self._log_order_event(
                "order_import_detected_order_rejected_reason",
                reason="not_ascii_7_digits",
            )
        changed = order_no != (self._latest_order_no or "").strip()
        self._set_latest_order_no(order_no)
        # 取得値を手入力欄にも反映し、確認・再保存しやすくする（readonlyでもプログラム更新は可）。
        if self._order_input.text().strip() != order_no:
            self._order_input.setText(order_no)
        if not changed:
            # 同じ受注No: 直近の保存状態などは維持し、未保存/取得不可/取得中のみ取得OKへ戻す。
            if self._status_label.text() in {"未保存", "取得不可", "取得中"}:
                self._set_status("取得OK", "受注Noを取得できました")
            return
        # 受注Noが変わったら前回状態を持ち越さない（stale「保存済み」対策・要件4）。
        self._log_order_event("order_import_status_reset_for_new_order", order_no=order_no)
        self._log_order_event("order_import_detected_new", order_no=order_no)
        self._set_status("取得OK", "受注Noを取得できました")

    def _capture_once_via_helper(self, *, timeout_ms: int = _CAPTURE_HELPER_TIMEOUT_MS) -> dict:
        """手動取得・自動取得が共通で使う helper 経路（要件6）。

        本体プロセスでは UIA/COM/Win32 を直接呼ばず、別プロセスhelperを1回起動して
        結果dict（order_no/screen_type/error/reason/elapsed_ms）を返す。
        """
        return run_capture_via_helper(
            command=_resolve_capture_helper_command(),
            debug=_capture_debug_enabled(),
            timeout_ms=timeout_ms,
        )

    def _on_capture(self) -> None:
        # 手動「取得」も自動取得と同一の単発 helper worker 経路を使う（要件6）。
        self._log_order_event("order_import_manual_capture_clicked")
        # 手動取得はユーザー操作。結果が返るまで待つ間、状態を「取得中」にする（要件4）。
        self._set_status("取得中", "受注Noを取得しています…")
        self._log_order_event("order_import_manual_capture_status_fetching")
        self._start_capture_worker_once(source="manual")

    # ── 自動取得ワーカーからの反映（表示中のみ・保存はしない） ────────────────────
    def _on_worker_captured(self, order_no: str) -> None:
        """自動取得ワーカーが受注Noを得たときの反映（保存はしない）。"""
        if self._closing:
            _write_worker_debug_event("signal_ignored_window_closed", signal="captured")
            return
        order_no = (order_no or "").strip()
        if not order_no:
            return
        self._auto_capture_failures = 0
        self._auto_captured_value = order_no
        started = time.monotonic()
        self._reflect_detected_order_no(order_no)
        self._log_order_event(
            "order_capture_perf_reflect_elapsed_ms",
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
        self._log_order_event("order_import_detected_order_no", order_no=order_no)
        self._log_order_event("order_import_detected_display_only", order_no=order_no)
        self._log_order_event("order_import_save_suppressed_on_header_detect", order_no=order_no)

    def _remember_valid_order_no_internal(self, order_no: str) -> None:
        """UI表示を更新せず、内部の有効受注No候補だけを保持する（要件2）。

        自動取得OFF/自動保存ON時に、見出→明細遷移保存で使う候補を内部保持する。
        画面のラベル・状態・手入力欄は更新しない。
        """
        order_no = (order_no or "").strip()
        if not order_no:
            return
        if captured_orders.normalize_captured_order_no(order_no):
            self._last_valid_order_no = order_no
            self._latest_detected_order_no = order_no

    def _on_worker_capture_failed(self) -> None:
        """自動取得ワーカーが取得失敗したときの状態表示（最新値はすぐ消さない）。

        取得失敗が何回続いてもアプリは落とさない（要件3）。取得不可表示は UI スレッド
        （このslot）で安全に行い、同じ「取得不可」は毎tickで再設定しない（_set_status の
        同値skip）。失敗が増えても process/timer は継続し、次tickで再試行する。
        """
        if self._closing:
            _write_worker_debug_event("signal_ignored_window_closed", signal="capture_failed")
            return
        self._auto_capture_failures += 1
        self._log_order_event(
            "order_import_capture_failure_count_incremented", count=self._auto_capture_failures
        )
        _write_crash_probe_event(
            "order_import_capture_failure_count_incremented", count=self._auto_capture_failures
        )
        threshold_reached = (
            self._auto_capture_failures >= _AUTO_CAPTURE_FAILURE_STATUS_THRESHOLD
        )
        if threshold_reached:
            self._log_order_event(
                "order_import_capture_failure_threshold_reached",
                count=self._auto_capture_failures,
            )
        if threshold_reached and not self._has_valid_current_order_no():
            if self._last_status_text == "取得不可":
                # 既に取得不可表示。毎tickでの再設定はしない（要件3）。
                self._log_order_event("order_import_capture_failed_status_unchanged_skip")
            else:
                self._log_order_event("order_import_capture_failed_status_update_entered")
                try:
                    self._set_status("取得不可", "受注Noを取得できていません")
                    self._log_order_event("order_import_capture_failed_status_update_finished")
                except Exception as exc:  # noqa: BLE001 - status更新失敗でアプリを落とさない
                    self._log_slot_exception(
                        "order_import_capture_failed_status_update_exception",
                        exc,
                        source="capture_failed",
                    )
        # 取得不可へ変更したあとも、次tickで安全に再試行する（要件3）。
        self._log_order_event(
            "order_import_capture_failure_retry_continue", count=self._auto_capture_failures
        )
        _write_crash_probe_event("order_import_capture_after_failure_recovery_ready")

    def _on_save(self) -> None:
        # 自動保存ON時は手入力保存を行わない（実行検知で自動保存されるため）。
        if self._auto_save_check.isChecked():
            QMessageBox.information(
                self, "TKS受注No取込", "自動保存中は手入力保存できません。"
            )
            self._set_status("手入力不可", "自動保存中は手入力保存できません")
            return
        # 自動取得OFF/自動保存OFFでも、受注No欄に手入力したテキストを保存する（要件1）。
        self._save_manual_input_order_no()

    def _save_manual_input_order_no(self) -> str:
        """受注No欄に手入力されたテキストだけを保存する（要件1）。

        自動取得OFF/自動保存OFFでも動作する手動保存の専用経路。保存対象は必ず現在の
        入力欄テキストであり、_last_valid_order_no / _latest_detected_order_no /
        _last_header_order_no などの内部候補値は使わない。
        helper/QProcess/自動取得timer は一切起動しない（自動取得・自動保存とは完全に分離）。
        戻り値: saved / duplicate / invalid / error。
        """
        self._log_order_event("order_import_manual_input_save_clicked")
        raw_text = self._order_input.text()
        self._log_order_event(
            "order_import_manual_input_save_text_read", order_no=raw_text.strip()
        )
        # 正規化（NFKC→ASCII数字のみ・7桁以上）に失敗する空欄・不正値は保存しない。
        normalized = captured_orders.normalize_captured_order_no(raw_text)
        if not normalized:
            self._log_order_event(
                "order_import_manual_input_save_invalid", order_no=raw_text.strip()
            )
            if not raw_text.strip():
                self._set_status("取得不可", "受注Noが空です（保存しません）")
            else:
                self._set_status("取得不可", "受注Noが不正です（7桁以上の数字のみ）")
            return "invalid"
        if self._order_no_already_saved(normalized):
            self._log_order_event(
                "order_import_manual_input_save_duplicate", order_no=normalized
            )
            self._set_status("重複", "保存済みです（重複）")
            return "duplicate"
        self._log_order_event(
            "order_import_manual_input_save_started", order_no=normalized
        )
        # 手入力保存では helper/QProcess/自動取得timer を一切起動しない（要件1）。
        self._log_order_event(
            "order_import_manual_input_save_no_helper_used", order_no=normalized
        )
        result = self._save_order_no(
            normalized, method="manual_input", source="manual_input"
        )
        if result == "saved":
            self._log_order_event(
                "order_import_manual_input_save_status_saved", order_no=normalized
            )
        self._log_order_event(
            "order_import_manual_input_save_finished",
            order_no=normalized,
            result=result,
        )
        return result

    def _request_auto_save_detected_order_no(self, order_no: str) -> str:
        """旧自動取得時保存ルート。現在は検出直後保存を明示的に禁止する。"""
        self._log_order_event(
            "order_import_unexpected_auto_save_blocked",
            order_no=captured_orders.normalize_captured_order_no(order_no) or order_no,
            reason="auto_detect_save_disabled",
        )
        return "disabled"

    def _save_order_no(
        self,
        order_no: str,
        *,
        method: str,
        source: str = "manual",
        saved_message: str = "保存しました",
        duplicate_message: str = "保存済みです（重複）",
    ) -> str:
        """受注Noを保存する共通関数。結果（saved/duplicate/empty/error）を返す。

        自動保存・手動保存・実行ボタン/F12保存は、いずれもこの関数へ集約する（要件1）。
        状態表示・ログ表示は常に実際の保存結果に合わせる（要件4）。
        """
        total_started = time.monotonic()
        stage_elapsed_ms = 0.0
        ui_started = time.monotonic()
        ui_elapsed_ms = 0.0
        flush_schedule_started = time.monotonic()
        flush_schedule_elapsed_ms = 0.0
        order_no = (order_no or "").strip()
        self._log_order_event(
            "order_import_save_order_entered",
            source=source,
            method=method,
            order_no=order_no,
        )
        if source == "auto" or method == "auto":
            self._log_order_event(
                "order_import_unexpected_auto_save_blocked",
                source=source,
                method=method,
                order_no=order_no,
            )
            return "blocked"
        if not order_no:
            self._set_status("取得不可", "受注Noが空です（保存しません）")
            self._log_order_event(
                "order_import_save_skipped_empty", source=source, method=method
            )
            return "empty"
        try:
            if method == "f12":
                self._log_order_event(
                    "order_import_auto_save_entered_save_func",
                    source=source,
                    order_no=order_no,
                )
            if source == "auto":
                self._log_order_event(
                    "order_import_auto_save_stage_started",
                    source=source,
                    order_no=order_no,
                )
            self._log_order_event(
                "order_import_save_stage_started",
                source=source,
                method=method,
                order_no=order_no,
            )
            # 受注No検出後、まずメモリ上の保存リストへ即時追加する（ディスクへは書かない）。
            # 実際のディスク書き込みは軽量flushへ分離し、まとめて非同期に行う（要件3・5）。
            if self._saving:
                self._log_order_event(
                    "order_import_save_deferred_reason",
                    reason="saving_guard_already_active_but_stage_immediate",
                    source=source,
                    method=method,
                    order_no=order_no,
                )
            self._saving = True
            self._log_order_event(
                "order_import_saving_guard_entered",
                source=source,
                method=method,
                order_no=order_no,
            )
            stage_started = time.monotonic()
            try:
                saved, reason = captured_orders.stage_order(order_no, method=method)
            finally:
                stage_elapsed_ms = round((time.monotonic() - stage_started) * 1000, 2)
                self._saving = False
                self._log_order_event(
                    "order_import_saving_guard_released",
                    source=source,
                    method=method,
                    order_no=order_no,
                    order_import_stage_elapsed_ms=stage_elapsed_ms,
                )
            self._log_order_event(
                "order_import_save_stage_finished",
                source=source,
                method=method,
                order_no=order_no,
                saved=saved,
                reason=reason,
                order_import_stage_elapsed_ms=stage_elapsed_ms,
            )
        except Exception:  # noqa: BLE001 - 保存失敗でも落とさない
            _LOGGER.warning("受注Noの保存に失敗しました: %s", order_no, exc_info=True)
            self._saving = False
            self._set_status("保存失敗", "保存に失敗しました")
            if source == "auto":
                self._log_order_event(
                    "order_import_auto_save_stage_failed",
                    source=source,
                    order_no=order_no,
                )
            self._log_order_event(
                "order_import_save_failed", source=source, method=method, order_no=order_no
            )
            self._log_order_event(
                "order_import_manual_save_failed",
                source=source,
                method=method,
                order_no=order_no,
            )
            return "error"
        saved_order_no = order_no if saved else None
        ui_started = time.monotonic()
        if saved:
            self._set_latest_order_no(order_no)
            self._set_status("保存OK", saved_message)
            self._log_order_event(
                "order_import_status_save_ok_set",
                source=source,
                method=method,
                order_no=order_no,
            )
            # メモリ上には既に追加済み。ディスクflushは軽量・集約して予約する（要件3）。
            flush_schedule_started = time.monotonic()
            self._schedule_saved_orders_flush()
            flush_schedule_elapsed_ms = round((time.monotonic() - flush_schedule_started) * 1000, 2)
            if source == "auto":
                self._log_order_event(
                    "order_import_auto_save_stage_succeeded",
                    source=source,
                    order_no=order_no,
                )
            self._log_order_event(
                "order_import_save_succeeded", source=source, method=method, order_no=order_no
            )
            self._log_order_event(
                "order_import_manual_save_succeeded",
                source=source,
                method=method,
                order_no=order_no,
            )
            result = "saved"
        elif reason == "duplicate":
            self._set_status("重複", duplicate_message)
            if source == "auto":
                self._log_order_event(
                    "order_import_auto_save_stage_duplicate",
                    source=source,
                    order_no=order_no,
                )
            self._log_order_event(
                "order_import_save_skipped_already_saved",
                source=source,
                method=method,
                order_no=order_no,
            )
            self._log_order_event(
                "order_import_manual_save_duplicate",
                source=source,
                method=method,
                order_no=order_no,
            )
            result = "duplicate"
        else:
            self._set_status("取得不可", "受注Noが空です（保存しません）")
            if source == "auto":
                self._log_order_event(
                    "order_import_auto_save_stage_failed",
                    source=source,
                    order_no=order_no,
                    reason=reason,
                )
            self._log_order_event(
                "order_import_save_skipped_empty", source=source, method=method
            )
            self._log_order_event(
                "order_import_manual_save_failed",
                source=source,
                method=method,
                reason=reason,
            )
            result = "empty"
        self._refresh_count()
        self._log_order_event(
            "order_import_saved_count_updated",
            source=source,
            method=method,
            order_no=order_no,
            count_text=self._count_label.text(),
        )
        self._refresh_list_window(new_order_no=saved_order_no)
        ui_elapsed_ms = round((time.monotonic() - ui_started) * 1000, 2)
        total_elapsed_ms = round((time.monotonic() - total_started) * 1000, 2)
        self._log_order_event(
            "order_import_save_timing",
            source=source,
            method=method,
            order_no=order_no,
            result=result,
            order_import_stage_elapsed_ms=stage_elapsed_ms,
            order_import_ui_update_elapsed_ms=ui_elapsed_ms,
            order_import_flush_schedule_elapsed_ms=flush_schedule_elapsed_ms,
            order_import_total_elapsed_ms=total_elapsed_ms,
        )
        return result

    # ── ディスクflush（軽量・集約） ──────────────────────────────────────────
    def _schedule_saved_orders_flush(self) -> None:
        """未書き込みの保存リストを、短時間後にまとめて1回だけディスクへ書き込む予約。"""
        if self._flush_timer_scheduled:
            return
        self._flush_timer_scheduled = True
        self._log_order_event(
            "order_import_flush_scheduled",
            delay_ms=_SAVE_FLUSH_DEBOUNCE_MS,
        )
        self._log_order_event("order_import_save_flush_scheduled")
        self._log_order_event("order_import_auto_save_flush_scheduled")
        QTimer.singleShot(_SAVE_FLUSH_DEBOUNCE_MS, self._on_flush_timer)

    def _on_flush_timer(self) -> None:
        try:
            self._flush_timer_scheduled = False
            if self._closing:
                self._log_order_event("order_import_flush_timer_ignored_closed")
                return
            self._flush_saved_orders_now(reason="debounce")
        except Exception as exc:  # noqa: BLE001
            self._log_slot_exception("order_import_unhandled_slot_exception", exc, source="flush_timer")

    def _flush_saved_orders_now(self, *, reason: str, durable: bool | None = None) -> bool:
        """dirty のときだけディスクへ書き込む。close時など重要時は durable=True。

        書き込み失敗時は dirty を残し（captured_orders 側で保持）、close以外なら再予約する。
        戻り値: 書き込み成功（または書き込み不要）なら True、失敗なら False。
        """
        if not captured_orders.is_dirty():
            return True
        if durable is None:
            durable = reason == "close"
        self._log_order_event(
            "order_import_save_perf_started", reason=reason, durable=durable
        )
        try:
            metrics = captured_orders.flush(durable=durable, reason=reason)
        except Exception as exc:  # noqa: BLE001 - 書き込み失敗でUIを落とさない
            _LOGGER.warning("保存リストのディスク書き込みに失敗しました。", exc_info=True)
            self._log_order_event(
                "order_import_save_perf_finished",
                reason=reason,
                ok=False,
                error=type(exc).__name__,
            )
            # dirty は captured_orders 側で残る。close以外は後で再試行する。
            if reason != "close":
                self._schedule_saved_orders_flush()
            return False
        self._log_order_event(
            "order_import_save_perf_finished",
            reason=reason,
            ok=True,
            wrote=metrics.get("wrote"),
            order_import_save_elapsed_ms=metrics.get("elapsed_ms"),
            order_import_save_file_write_elapsed_ms=metrics.get("file_write_elapsed_ms"),
            order_import_save_fsync_elapsed_ms=metrics.get("fsync_elapsed_ms"),
            order_import_save_replace_elapsed_ms=metrics.get("replace_elapsed_ms"),
            order_import_save_dir_fsync_elapsed_ms=metrics.get("dir_fsync_elapsed_ms"),
        )
        return True

    def _refresh_list_window(self, *, new_order_no: str | None = None) -> None:
        """保存済み一覧画面が開いていれば、その表示を即時更新する（要件1・8）。

        1件保存の場合は増分追加のみ行い、全再構築を避ける。開いていなければ何もしない
        （次回表示時に反映される）。
        """
        win = self._list_window
        self._log_order_event(
            "order_import_saved_list_update_requested",
            order_no=new_order_no or "",
            has_window=win is not None,
        )
        if win is None:
            self._log_order_event("order_import_saved_list_update_skipped_closed")
            return
        if not self._is_qobject_valid(win):
            self._list_window = None
            self._log_order_event("order_import_callback_target_invalid", target="saved_list")
            self._log_order_event("order_import_saved_list_update_skipped_closed")
            return
        try:
            if hasattr(win, "isVisible") and not win.isVisible():
                self._log_order_event("order_import_saved_list_update_skipped_closed")
                return
        except RuntimeError as exc:
            self._list_window = None
            self._log_slot_exception("order_import_saved_list_update_failed", exc)
            return
        started = time.monotonic()
        self._log_order_event("order_import_saved_list_ui_update_started")
        try:
            appended = False
            if new_order_no is not None:
                noter = getattr(win, "note_saved_order", None)
                if callable(noter):
                    noter(new_order_no)
                    appended = True
            if not appended:
                reloader = getattr(win, "_reload", None)
                if callable(reloader):
                    reloader()
        except Exception as exc:  # noqa: BLE001 - 一覧更新失敗でUIを落とさない
            _LOGGER.debug("保存済み一覧の即時更新に失敗しました。", exc_info=True)
            self._log_slot_exception("order_import_saved_list_update_failed", exc)
        self._log_order_event(
            "order_import_saved_list_ui_update_finished",
            order_import_saved_list_ui_update_elapsed_ms=round(
                (time.monotonic() - started) * 1000, 2
            ),
        )

    # ── TKS側F12検知保存（自動保存ONの見出→明細遷移／opt-inのF12 monitor経由のみ） ──
    def _request_manual_save_from_f12(
        self,
        source: str,
        *,
        require_auto_save: bool = False,
        diag: dict | None = None,
    ) -> str:
        """F12由来の保存要求をmain threadの単一入口へ集約する。"""
        diag = dict(diag or {})
        if self._closing or not self._is_qobject_valid(self):
            self._log_order_event("order_capture_worker_result_ignored_closed", source=source)
            return "closed"
        if require_auto_save and not self._auto_save_check.isChecked():
            self._log_order_event("order_import_auto_save_skipped_disabled", source=source)
            return "disabled"
        now = time.monotonic()
        if (now - self._last_f12_save_requested_at) * 1000 < _F12_SAVE_DEBOUNCE_MS:
            self._log_order_event("order_import_f12_duplicate_suppressed", source=source)
            return "duplicate_suppressed"
        if self._manual_save_in_progress:
            self._log_order_event("order_import_manual_save_reentrant_blocked", source=source)
            return "busy"
        self._last_f12_save_requested_at = now
        self._manual_save_in_progress = True
        self._log_order_event("order_import_f12_save_started", source=source)
        try:
            result = self._execute_and_save_current_order_no("f12")
            self._log_order_event("order_import_f12_save_finished", source=source, result=result)
            return result
        except Exception as exc:  # noqa: BLE001
            self._saving = False
            self._log_slot_exception("order_import_f12_exception", exc, source=source)
            self._log_order_event("order_import_f12_save_failed", source=source)
            return "error"
        finally:
            self._manual_save_in_progress = False

    def _execute_and_save_current_order_no(self, source: str) -> str:
        """実行ボタン/F12で、現在の受注Noを共通保存関数へ渡して必ず保存する（要件1）。

        - 有効な受注No（最新取得／手入力／再取得）があれば必ず保存する。
        - 自動保存のON/OFFや成否には依存しない。
        - 重い再取得・worker完了・ディスクflushを待たず、メモリstageまで即時に進む。
        """
        total_started = time.monotonic()
        select_started = time.monotonic()
        result = "error"
        self._log_order_event("order_import_manual_save_started", source=source)
        try:
            self._log_order_event("order_import_save_requested", source=source)
            try:
                order_no = self._resolve_execute_order_no()
            except Exception as exc:  # noqa: BLE001 - 解決失敗でも落とさない
                _LOGGER.warning("実行保存の受注No解決で例外が発生しました。", exc_info=True)
                self._log_order_event(
                    "order_import_save_failed", source=source, error=str(exc)
                )
                order_no = ""
            select_elapsed_ms = round((time.monotonic() - select_started) * 1000, 2)
            self._log_order_event(
                "order_import_select_order_elapsed_ms",
                source=source,
                elapsed_ms=select_elapsed_ms,
                order_import_select_order_elapsed_ms=select_elapsed_ms,
            )
            normalized = captured_orders.normalize_captured_order_no(order_no)
            if not normalized:
                self._set_status("取得不可", "受注Noを取得できませんでした（保存しません）")
                self._log_order_event("order_import_execute_no_valid_order", source=source)
                if source == "f12":
                    self._log_order_event("order_import_f12_no_valid_order")
                self._log_order_event("order_import_save_skipped_empty", source=source)
                result = "empty"
                return result
            if source == "f12":
                self._log_order_event("order_import_f12_target_selected", order_no=normalized)
            self._log_order_event(
                "order_import_manual_save_selected_order",
                source=source,
                order_no=normalized,
            )
            self._log_order_event(
                "order_import_execute_save_order_selected",
                source=source,
                order_no=normalized,
            )
            # 最新表示・手入力欄へ反映（自動保存中で readonly でも最新表示は更新する）。
            self._set_latest_order_no(normalized)
            if not self._order_input.isReadOnly() and self._order_input.text().strip() != normalized:
                self._order_input.setText(normalized)
            method = "f12" if source == "f12" else "manual"
            result = self._save_order_no(
                normalized,
                method=method,
                source=source,
                saved_message="保存しました",
                duplicate_message="既に保存済みです",
            )
            return result
        finally:
            elapsed_ms = round((time.monotonic() - total_started) * 1000, 2)
            self._log_order_event(
                "order_import_manual_save_elapsed_ms",
                source=source,
                result=result,
                elapsed_ms=elapsed_ms,
                order_import_manual_save_elapsed_ms=elapsed_ms,
            )
            self._log_order_event(
                "order_import_total_elapsed_ms",
                source=source,
                result=result,
                elapsed_ms=elapsed_ms,
                order_import_total_elapsed_ms=elapsed_ms,
            )
            self._log_order_event("order_import_manual_save_finished", source=source, result=result)

    def _set_latest_order_no(self, order_no: str) -> None:
        changed = order_no != self._latest_order_no
        self._latest_order_no = order_no
        # ラベルは実表示値が変わる時だけ setText（"-"↔値の変化も拾う）。
        label = order_no or "-"
        if label != self._last_latest_label:
            self._latest_label.setText(label)
            self._last_latest_label = label
        # 付随する重い更新（ボタン有効判定・worker文脈同期）は値が変わった時だけ（要件5）。
        if changed:
            self._refresh_add_to_voucher_enabled()
            self._sync_execute_context()

    def _refresh_count(self) -> None:
        try:
            count = len(captured_orders.load_captured_orders())
        except Exception:  # noqa: BLE001 - 破損しても件数表示で落とさない
            count = 0
        text = f"{count} 件"
        if text != self._last_count_text:
            self._count_label.setText(text)
            self._last_count_text = text

    # ── 伝票一覧への追加 ──────────────────────────────────────────────────────
    def _current_voucher_window(self):
        provider = self._voucher_window_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:  # noqa: BLE001
            return None

    def refresh_voucher_state(self) -> None:
        """伝票作成・印刷画面の開閉に追従してボタン有効/無効を更新する。"""
        self._refresh_add_to_voucher_enabled()
        if self._list_window is not None and hasattr(self._list_window, "refresh_voucher_state"):
            self._list_window.refresh_voucher_state()

    def _refresh_add_to_voucher_enabled(self) -> None:
        window = self._current_voucher_window()
        try:
            has_saved_orders = bool(captured_orders.load_captured_orders())
        except Exception:  # noqa: BLE001
            has_saved_orders = False
        has_order = bool(
            self._latest_order_no
            or self._order_input.text().strip()
            or has_saved_orders
        )
        enabled = window is not None and has_order
        tooltip = (
            "伝票作成・印刷画面を開いてください。"
            if window is None
            else "保管済みの受注Noを伝票一覧に追加し、OLAP取得まで行います。"
        )
        # 有効状態もツールチップも前回と同じなら、setEnabled/style再評価を行わない（要件5）。
        if enabled == self._last_add_enabled and tooltip == self._last_add_tooltip:
            return
        self._add_to_voucher_button.setEnabled(enabled)
        self._add_to_voucher_button.setToolTip(tooltip)
        self._last_add_enabled = enabled
        self._last_add_tooltip = tooltip
        self._refresh_button_styles()

    def _on_add_to_voucher(self) -> None:
        window = self._current_voucher_window()
        if window is None:
            QMessageBox.information(
                self, "TKS受注No取込", "伝票作成・印刷画面を開いてください。"
            )
            return
        order_no = (self._latest_order_no or self._order_input.text()).strip()
        adder = getattr(window, "add_order_no_and_fetch", None)
        if not callable(adder):
            QMessageBox.warning(self, "TKS受注No取込", "伝票一覧に追加できません。")
            return

        try:
            snapshot = list(captured_orders.load_captured_orders())
        except Exception:  # noqa: BLE001
            _LOGGER.warning("保存済み受注Noの読み込みに失敗しました。", exc_info=True)
            snapshot = []
        if not snapshot and order_no:
            snapshot = [{"order_no": order_no}]

        added = 0
        duplicates = 0
        invalid = 0
        failed = 0
        remove_targets: set[str] = set()
        self._log_order_event("order_import_add_all_requested", count=len(snapshot))

        for item in snapshot:
            target = captured_orders.normalize_captured_order_no(item.get("order_no"))
            if not target:
                invalid += 1
                self._log_order_event("order_import_add_one_failed_kept", order_no=item.get("order_no"), reason="invalid")
                continue
            try:
                result = adder(target)
            except Exception as exc:  # noqa: BLE001 - 追加失敗でも小画面は維持する
                failed += 1
                _LOGGER.warning("伝票一覧への追加に失敗しました: %s", target, exc_info=True)
                self._log_order_event(
                    "order_import_add_one_failed_kept",
                    order_no=target,
                    reason=type(exc).__name__,
                )
                continue
            status = (result or {}).get("status") if isinstance(result, dict) else None
            if status == "added":
                added += 1
                remove_targets.add(target)
                self._log_order_event("order_import_add_one_succeeded", order_no=target)
            elif status == "duplicate":
                duplicates += 1
                remove_targets.add(target)
                self._log_order_event("order_import_add_one_duplicate_removed", order_no=target)
            elif status == "invalid":
                invalid += 1
                self._log_order_event("order_import_add_one_failed_kept", order_no=target, reason="invalid")
            else:
                failed += 1
                self._log_order_event("order_import_add_one_failed_kept", order_no=target, reason=status or "unknown")

        removed = 0
        if remove_targets:
            try:
                removed = captured_orders.remove_captured_orders_by_order_no(remove_targets)
                self._log_order_event(
                    "order_import_saved_list_removed_after_add",
                    requested=len(remove_targets),
                    removed=removed,
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("追加済み受注Noの保存リスト削除に失敗しました。", exc_info=True)
                self._log_order_event(
                    "order_import_saved_list_remove_failed",
                    requested=len(remove_targets),
                    error=type(exc).__name__,
                )

        self._refresh_count()
        self._refresh_list_window()
        message = f"追加完了: {added}件 / 重複: {duplicates}件 / 削除: {removed}件"
        if invalid or failed:
            message += f" / 不正: {invalid}件 / 失敗: {failed}件"
        if added or duplicates:
            self._set_status("追加OK", message)
        elif invalid or failed:
            self._set_status("追加不可", message)
        else:
            self._set_status("追加なし", message)
        self._log_order_event(
            "order_import_add_all_finished",
            added=added,
            duplicate=duplicates,
            removed=removed,
            invalid=invalid,
            failed=failed,
        )

    # ── 保存済み受注No一覧画面 ────────────────────────────────────────────────
    def _on_open_list(self) -> None:
        """保存済み受注No一覧画面を開く（単一インスタンス。取込画面は閉じない）。"""
        if self._list_window is not None:
            self._list_window.showNormal()
            self._list_window.show()
            self._list_window.raise_()
            self._list_window.activateWindow()
            return
        from app.captured_orders_window import CapturedOrdersWindow

        win = CapturedOrdersWindow(voucher_window_provider=self._voucher_window_provider)
        win.saved.connect(self._refresh_count)
        win.closed.connect(self._on_list_closed)
        self._list_window = win
        win.show()
        win.raise_()
        win.activateWindow()

    def _on_list_closed(self, *_args: object) -> None:
        self._list_window = None

    # ── 自動保存（実行操作＝F12キー／「F12 実行」ボタン押下検知時に保存） ────────
    # 実行検知元ごとの保存成功メッセージ。
    _EXECUTE_SAVED_MESSAGES = {
        "f12_key": "F12実行時に保存しました",
        "execute_button_click": "実行ボタン押下時に保存しました",
    }

    def _load_bool_setting(self, key: str, default: bool, *, fallback_key: str | None = None) -> bool:
        raw = self._settings.value(key, None)
        if raw is None and fallback_key is not None:
            raw = self._settings.value(fallback_key, None)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _load_auto_capture_setting(self) -> bool:
        # 初期値ON。
        return self._load_bool_setting(_SETTINGS_AUTO_CAPTURE, True)

    def _load_auto_save_setting(self) -> bool:
        # 初期値OFF。旧キー tks_capture/auto_save があれば移行元として読む。
        return self._load_bool_setting(
            _SETTINGS_AUTO_SAVE_ON_EXECUTE, False, fallback_key=_SETTINGS_AUTO_SAVE
        )

    def _load_always_on_top_setting(self) -> bool:
        # 初期値ON（従来は常に手前固定だった。要件3）。
        return self._load_bool_setting(_SETTINGS_ALWAYS_ON_TOP, True)

    def _load_f12_monitor_setting(self) -> bool:
        # 初期値OFF。TKS側F12/実行ボタンのクリック座標監視を明示的にONにするための設定（要件2/4）。
        return self._load_bool_setting(_SETTINGS_F12_MONITOR, False)

    def _on_always_on_top_toggled(self, checked: bool) -> None:
        self._settings.setValue(_SETTINGS_ALWAYS_ON_TOP, "1" if checked else "0")
        self._settings.sync()
        self._apply_always_on_top(checked)
        self._log_order_event("order_import_always_on_top_changed", always_on_top=checked)

    def _apply_fixed_window_size(self) -> None:
        """サイズ変更を無効化する（要件4）。minimumSize == maximumSize の固定サイズにする。

        レイアウト確定後のサイズヒントと現在サイズの大きい方を採用し、DPI/125%でも
        内容が切れない固定サイズにする。
        """
        try:
            hint = self.sizeHint()
            current = self.size()
            width = max(current.width(), hint.width())
            height = max(current.height(), hint.height())
            self.setFixedSize(width, height)
            self._log_order_event(
                "order_import_window_resize_disabled", width=width, height=height
            )
            self._log_order_event(
                "order_import_window_fixed_size_applied", width=width, height=height
            )
        except Exception:  # noqa: BLE001 - 固定サイズ設定失敗でも画面は開く
            _LOGGER.debug("受注No取込画面の固定サイズ設定に失敗しました。", exc_info=True)

    def _apply_always_on_top(self, on: bool) -> None:
        """WindowStaysOnTopHint を切り替える。位置・サイズは維持し、画面は閉じない。"""
        self._always_on_top = bool(on)
        flags = self.windowFlags()
        if on:
            new_flags = flags | Qt.WindowType.WindowStaysOnTopHint
        else:
            new_flags = flags & ~Qt.WindowType.WindowStaysOnTopHint
        if new_flags == flags:
            return
        was_visible = self.isVisible()
        geometry = self.geometry()
        self.setWindowFlags(new_flags)
        # setWindowFlags は位置・サイズを変えうるため元の geometry を復元する。
        self.setGeometry(geometry)
        # setWindowFlags は可視ウィンドウを隠すので、表示中だった場合のみ再表示する。
        if was_visible:
            self.show()

    def _on_auto_capture_toggled(self, checked: bool) -> None:
        self._settings.setValue(_SETTINGS_AUTO_CAPTURE, "1" if checked else "0")
        self._settings.sync()
        # 自動取得と自動保存は別機能。scheduler は「自動取得ON または 自動保存ON」で回す（要件2）。
        self._sync_capture_scheduler()

    def _on_auto_save_toggled(self, checked: bool) -> None:
        self._settings.setValue(_SETTINGS_AUTO_SAVE_ON_EXECUTE, "1" if checked else "0")
        # 旧キーも同期しておき、旧バージョンとの互換を保つ。
        self._settings.setValue(_SETTINGS_AUTO_SAVE, "1" if checked else "0")
        self._settings.sync()
        self._apply_auto_save_ui()
        if checked:
            self._log_order_event("order_import_auto_save_enabled", source="toggle")
        else:
            self._log_order_event("order_import_auto_save_disabled_skip_transition", source="toggle")
        if self._execute_worker is not None:
            self._execute_worker.set_enabled(checked)
        # 自動保存ONで実行検知ワーカーを起動、OFF（かつF12 monitorもOFF）なら停止する（要件2/8）。
        self._maybe_start_execute_monitor()
        # 自動保存ON/OFFでも scheduler の要否が変わるため再評価する（要件2）。
        self._sync_capture_scheduler()

    def _sync_capture_scheduler(self) -> None:
        """自動取得/自動保存のON/OFFに応じて capture scheduler を起動/停止する（要件2）。"""
        if self._auto_capture_scheduler_needed() and self.isVisible() and not self._closing:
            self._start_auto_capture_timer()
        else:
            reason = (
                "closing"
                if self._closing
                else ("not_visible" if not self.isVisible() else "both_disabled")
            )
            if reason == "both_disabled":
                self._log_order_event("order_import_capture_scheduler_stopped_both_disabled")
            self._stop_auto_capture_timer(reason=reason)

    def _apply_auto_save_ui(self) -> None:
        """自動保存ON/OFFに応じて手入力欄の可否・🔒バッヂ・保存ボタンを切り替える。"""
        on = self._auto_save_check.isChecked()
        # 自動保存ON: 手入力欄は編集不可（既存の入力値は消さない）＋🔒バッヂ表示。
        self._order_input.setReadOnly(on)
        self._lock_badge.setVisible(on)
        # 手動「保存」は自動保存ON時は無効化する（意味が重複するため）。
        self._save_button.setEnabled(not on)
        if on:
            self._save_button.setToolTip("自動保存中は手入力保存できません")
            self._order_input.setToolTip("自動保存中は手入力できません（🔒）")
        else:
            self._save_button.setToolTip("")
            self._order_input.setToolTip("")
        self._refresh_button_styles()

    def _on_worker_execute_detected(self, source: str, diag: dict) -> None:
        """実行検知ワーカーからの通知を UI スレッドで受けて保存する。"""
        try:
            if self._closing or not self._is_qobject_valid(self):
                _write_worker_debug_event("signal_ignored_window_closed", signal="execute_detected")
                _write_execute_debug_event(
                    "signal_ignored_window_closed",
                    detected_source=source,
                    is_window_alive=False,
                    is_stopping=True,
                )
                return
            signal_diag = {
                **(diag or {}),
                "detected_source": source,
                "auto_save_enabled": self._auto_save_check.isChecked(),
                "auto_capture_enabled": self._auto_capture_check.isChecked(),
                "latest_order_no": self._latest_order_no,
                "input_order_no": self._order_input.text().strip(),
                "is_window_alive": True,
            }
            _write_execute_debug_event("execute_signal_received", **signal_diag)
            if source == "f12_key":
                self._log_order_event("order_import_f12_monitor_edge_detected")
                self._log_order_event("order_import_f12_source_monitor")
                self._request_manual_save_from_f12("monitor", require_auto_save=True, diag=signal_diag)
                return
            if source in {"tks_screen_transition", "detail_screen_detected"}:
                detail_diag = dict(diag or {})
                detail_diag.setdefault("source", source)
                self._save_current_order_no_from_detail_detected(diag=detail_diag)
                return
            self._on_execute_detected(source, diag=diag or None)
        except Exception as exc:  # noqa: BLE001
            self._saving = False
            self._manual_save_in_progress = False
            self._log_slot_exception("order_import_unhandled_slot_exception", exc, source=source)

    @staticmethod
    def _tks_screen_kind_from_title(title: str) -> str:
        title = title or ""
        if "受注入力（明細）" in title:
            return "detail"
        if "受注入力（見出）" in title:
            return "header"
        return ""

    def _resolve_execute_order_no(self) -> str:
        """実行検知時の保存候補受注No。

        優先順位: 1) 入力欄 2) 最後に検出した有効値 3) 最新取得済み 4) 再取得。
        自動取得が重くても、1〜3 はUIスレッドで即座に決まるため、実行ボタン/F12
        の保存は検出処理を待たずに完了する（要件7）。再取得(4)は最後の手段。
        """
        typed = self._order_input.text().strip()
        if captured_orders.normalize_captured_order_no(typed):
            self._log_order_event("order_import_manual_save_selected_input", order_no=typed)
            self._log_order_event("order_import_execute_save_selected_input", order_no=typed)
            return typed
        last_valid = (self._last_valid_order_no or "").strip()
        if captured_orders.normalize_captured_order_no(last_valid):
            self._log_order_event("order_import_manual_save_selected_last_valid", order_no=last_valid)
            self._log_order_event("order_import_execute_save_selected_last_valid", order_no=last_valid)
            return last_valid
        latest_detected = (self._latest_detected_order_no or "").strip()
        if captured_orders.normalize_captured_order_no(latest_detected):
            self._log_order_event(
                "order_import_manual_save_selected_latest_detected",
                order_no=latest_detected,
            )
            self._log_order_event(
                "order_import_execute_save_selected_latest_detected",
                order_no=latest_detected,
            )
            return latest_detected
        latest = (self._latest_order_no or "").strip()
        if captured_orders.normalize_captured_order_no(latest):
            self._log_order_event(
                "order_import_manual_save_selected_latest_order",
                order_no=latest,
                source="latest",
            )
            self._log_order_event(
                "order_import_execute_save_selected_latest_order",
                order_no=latest,
            )
            return latest
        self._log_order_event("order_import_manual_save_no_valid_order")
        self._log_order_event("order_import_execute_save_no_valid_order")
        return ""

    def _resolve_current_cached_order_no(self) -> str:
        """明細検出保存用。重いTKS再取得は行わず、保持済み候補だけを選ぶ。"""
        return self._resolve_execute_order_no()

    def _save_current_order_no_from_detail_detected(self, *, diag: dict | None = None) -> str:
        """TKS「受注入力（明細）」検出時だけ、保持済み受注Noを保存する。"""
        started = time.monotonic()
        result = "empty"
        diag = dict(diag or {})
        if not self._auto_save_check.isChecked():
            self._log_order_event(
                "order_import_auto_save_skipped_disabled",
                source=diag.get("source") or "detail_detected",
            )
            return "disabled"
        current_kind = self._tks_screen_kind_from_title(
            str(diag.get("current_tks_title") or diag.get("foreground_title") or "")
        )
        if not current_kind and diag.get("transition_detected"):
            current_kind = "detail"
        previous_kind = self._last_seen_tks_screen_kind
        if current_kind:
            self._last_seen_tks_screen_kind = current_kind
        if current_kind != "detail":
            self._detail_trigger_active = False
            self._log_order_event(
                "order_import_detail_save_skipped_no_order",
                reason="not_detail_screen",
                current_tks_title=diag.get("current_tks_title"),
            )
            return "empty"

        self._log_order_event(
            "order_import_detail_screen_detected",
            previous_tks_title=diag.get("previous_tks_title"),
            current_tks_title=diag.get("current_tks_title"),
            previous_screen_kind=previous_kind,
            current_screen_kind=current_kind,
        )
        order_no = self._resolve_current_cached_order_no()
        normalized = captured_orders.normalize_captured_order_no(order_no)
        if not normalized:
            self._log_order_event("order_import_detail_save_skipped_no_order")
            return "empty"
        if (
            self._detail_trigger_active
            and self._last_detail_trigger_saved_order_no == normalized
        ):
            self._log_order_event(
                "order_import_detail_save_skipped_already_triggered",
                order_no=normalized,
            )
            return "duplicate"

        self._detail_trigger_active = True
        self._last_detail_trigger_saved_order_no = normalized
        self._log_order_event("order_import_detail_save_triggered", order_no=normalized)
        self._log_order_event("order_import_detail_save_started", order_no=normalized)
        result = self._save_order_no(
            normalized,
            method="f12",
            source="detail_detected",
            saved_message="保存しました",
            duplicate_message="既に保存済みです",
        )
        self._log_order_event(
            "order_import_detail_save_stage_finished",
            order_no=normalized,
            result=result,
        )
        if result == "saved":
            self._log_order_event("order_import_detail_save_succeeded", order_no=normalized)
        elif result == "duplicate":
            self._log_order_event("order_import_detail_save_duplicate", order_no=normalized)
        elif result == "error":
            self._detail_trigger_active = False
            self._log_order_event("order_import_detail_save_failed", order_no=normalized)
        else:
            self._detail_trigger_active = False
            self._log_order_event(
                "order_import_detail_save_skipped_no_order",
                order_no=normalized,
                result=result,
            )
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        self._log_order_event(
            "order_import_detail_save_elapsed_ms",
            order_no=normalized,
            result=result,
            elapsed_ms=elapsed_ms,
            order_import_detail_save_elapsed_ms=elapsed_ms,
        )
        return result

    def _set_pending_auto_save(self, order_no: str, source: str, diag: dict) -> None:
        """保存中に検出した自動保存要求をキューへ積む（最後の1件で上書きしない・要件7）。"""
        normalized = captured_orders.normalize_captured_order_no(order_no)
        if not normalized:
            self._log_order_event(
                "order_import_auto_save_skipped_invalid", source=source, order_no=order_no
            )
            return
        queued_diag = dict(diag)
        queued_diag["pending_order_no"] = normalized
        # 同一受注Noは末尾へ入れ替え（最新の source/diag を採用・重複はキュー上で1件に）。
        if normalized in self._pending_auto_save:
            del self._pending_auto_save[normalized]
        self._pending_auto_save[normalized] = (source, queued_diag)
        self._log_order_event(
            "order_import_auto_save_pending_set",
            source=source,
            order_no=normalized,
            order_import_auto_save_pending_queue_size=len(self._pending_auto_save),
        )
        self._reschedule_pending_consume(source=source, order_no=normalized)

    def _reschedule_pending_consume(self, *, source: str = "", order_no: str = "") -> None:
        if self._pending_auto_save_retry_scheduled:
            return
        if not self._pending_auto_save:
            return
        self._pending_auto_save_retry_scheduled = True
        self._log_order_event(
            "order_import_auto_save_retry_scheduled", source=source, order_no=order_no
        )
        QTimer.singleShot(0, self._consume_pending_auto_save)

    def _consume_pending_auto_save(self) -> None:
        self._pending_auto_save_retry_scheduled = False
        if self._closing:
            # close時は _drain_pending_auto_save_on_close 側でまとめて処理する。
            return
        if not self._pending_auto_save:
            return
        if self._saving:
            # まだ保存処理中。次のイベントループで再試行する（要求は捨てない）。
            self._reschedule_pending_consume()
            return
        # FIFO で1件取り出して処理する。処理後、_on_execute_detected の finally が
        # 再度 _consume_pending_auto_save を呼び、残りを順次処理する。
        normalized, (source, diag) = next(iter(self._pending_auto_save.items()))
        del self._pending_auto_save[normalized]
        source = source or "pending_auto_save"
        diag = dict(diag)
        self._log_order_event(
            "order_import_auto_save_pending_consumed",
            source=source,
            order_no=normalized,
            order_import_auto_save_pending_queue_size=len(self._pending_auto_save),
        )
        diag["pending_consumed_order_no"] = normalized
        self._set_latest_order_no(normalized)
        self._on_execute_detected(source, diag=diag)

    def _drain_pending_auto_save_on_close(self) -> None:
        """close時、保留中の自動保存要求を確実にメモリ上の保存リストへ反映する（要件6）。"""
        if not self._pending_auto_save:
            return
        for normalized, (source, _diag) in list(self._pending_auto_save.items()):
            try:
                captured_orders.stage_order(normalized, method="f12")
            except Exception:  # noqa: BLE001 - close処理を落とさない
                _LOGGER.warning(
                    "close時の保留受注Noのメモリ反映に失敗しました: %s", normalized, exc_info=True
                )
            self._log_order_event(
                "order_import_close_pending_consumed", source=source, order_no=normalized
            )
        self._pending_auto_save.clear()

    def _on_execute_detected(self, source: str, *, diag: dict | None = None) -> None:
        """実行操作（F12キー／「F12 実行」ボタンクリック）検知時の共通保存処理。

        1. 自動保存ONか確認（OFFなら保存しない）
        2. 受注No候補を取得（最新取得→手入力→再取得）
        3. 空欄なら保存しない
        4. 重複なら二重保存しない
        5. 保存成功なら件数更新・状態表示
        例外でもアプリを落とさない。デバッグ時は診断JSONを出力する。
        """
        diag = dict(diag or {})
        diag["detected_source"] = source
        diag["source"] = source
        diag["auto_capture_enabled"] = self._auto_capture_check.isChecked()
        diag["auto_save_enabled"] = self._auto_save_check.isChecked()
        diag["input_order_no"] = self._order_input.text().strip()
        diag["manual_input_order_no"] = diag["input_order_no"]
        diag["latest_order_no"] = self._latest_order_no
        # setdefault の第2引数は常に評価されるため、重い win32 列挙は必要時だけ行う。
        if "target_order_entry_window_exists" not in diag:
            diag["target_order_entry_window_exists"] = _tks_order_entry_window_running()
        if "tkscloud_window_exists" not in diag:
            diag["tkscloud_window_exists"] = _tkscloud_window_running()
        needs_foreground_info = (
            "foreground_title" not in diag
            or "foreground_process_name" not in diag
        )
        fg = _foreground_window_info() if needs_foreground_info else {}
        if "foreground_title" not in diag:
            diag["foreground_title"] = fg.get("title")
        if "foreground_process_name" not in diag:
            diag["foreground_process_name"] = fg.get("process_name")
        if "foreground_is_tkscloud8" not in diag:
            diag["foreground_is_tkscloud8"] = _tkscloud8_is_foreground()
        diag.setdefault("execute_button_rect", None)
        diag.setdefault("mouse_pos", None)
        diag.setdefault("raw_mouse_pos", None)
        diag.setdefault("normalized_mouse_pos", None)
        diag.setdefault("raw_execute_button_rect", None)
        diag.setdefault("normalized_execute_button_rect", None)
        diag.setdefault("raw_tkscloud_window_rect", None)
        diag.setdefault("normalized_tkscloud_window_rect", None)
        diag.setdefault("monitor_rect", None)
        diag.setdefault("dpi_scale_x", None)
        diag.setdefault("dpi_scale_y", None)
        diag.setdefault("coordinate_space", None)
        diag.setdefault("cached_execute_button_rect", None)
        diag.setdefault("detected_execute_button_rect", None)
        diag.setdefault("inferred_execute_button_rect", None)
        diag.setdefault("tkscloud_window_rect", None)
        diag.setdefault("previous_tks_title", None)
        diag.setdefault("current_tks_title", None)
        diag.setdefault("transition_detected", False)
        diag.setdefault("click_inside_execute_button", False)
        diag.setdefault("reject_reason", None)
        diag.setdefault("is_stopping", self._closing)
        diag.setdefault("is_window_alive", not self._closing)
        diag.setdefault("exception_type", None)
        diag.setdefault("exception_message", None)
        diag.setdefault("traceback", None)

        if not self._auto_save_check.isChecked():
            diag["save_attempted"] = False
            diag["save_result"] = "skipped_auto_save_off"
            diag["save_message"] = "自動保存OFFのため保存しません"
            self._log_order_event("order_import_auto_save_skipped_disabled", source=source)
            self._write_execute_debug(diag)
            _write_execute_debug_event("save_disabled", **diag)
            return

        # 自動保存も手入力保存/F12保存と同じ共通保存関数（_save_order_no）を通す。
        # _saving は _save_order_no 内のメモリstage中だけ使い、worker/flush中の保存を
        # pending化しない。
        self._log_order_event("order_import_auto_save_enabled", source=source)
        if self._saving:
            self._log_order_event(
                "order_import_save_deferred_reason",
                reason="saving_guard_active_but_execute_stage_immediate",
                source=source,
            )
        result = "error"
        try:
            self._log_order_event("order_import_auto_save_requested", source=source)
            try:
                order_no = self._resolve_execute_order_no()
            except Exception as exc:  # noqa: BLE001 - 保存処理でアプリを落とさない
                _LOGGER.warning("実行検知後の受注No解決で例外が発生しました。", exc_info=True)
                order_no = ""
                diag["exception_type"] = type(exc).__name__
                diag["exception_message"] = str(exc)
                diag["traceback"] = traceback.format_exc()
            normalized_order_no = captured_orders.normalize_captured_order_no(order_no)
            diag["resolved_order_no"] = order_no
            diag["normalized_order_no"] = normalized_order_no
            _write_execute_debug_event("save_attempted", **diag)

            if not normalized_order_no:
                # 無効（空欄/桁不足/数字以外）は保存しない。状態は「保存失敗」ではなく
                # 「取得不可」とし、同じ実行操作を繰り返せば再試行できる。
                self._set_status("取得不可", "実行を検知しましたが、受注Noを取得できませんでした")
                diag["save_attempted"] = False
                diag["save_result"] = "empty"
                diag["empty"] = True
                diag["save_message"] = self._status_label.text()
                self._log_order_event(
                    "order_import_auto_save_skipped_invalid", source=source, order_no=order_no
                )
                self._write_execute_debug(diag)
                _write_execute_debug_event("save_empty", **diag)
                return

            self._set_latest_order_no(normalized_order_no)
            if (
                not self._order_input.isReadOnly()
                and self._order_input.text().strip() != normalized_order_no
            ):
                self._order_input.setText(normalized_order_no)
            # 重複は二重保存しない（保存済みなら状態表示のみ更新する）。
            result = self._save_order_no(
                normalized_order_no,
                method="f12",
                source=source,
                saved_message=self._EXECUTE_SAVED_MESSAGES.get(source, "実行時に保存しました"),
                duplicate_message="既に保存済みです",
            )
            diag["save_attempted"] = True
            diag["save_result"] = result
            diag["duplicate"] = result == "duplicate"
            diag["empty"] = result == "empty"
            diag["save_message"] = self._status_label.text()
            if result == "saved":
                self._log_order_event(
                    "order_import_auto_save_succeeded", source=source, order_no=normalized_order_no
                )
            elif result == "duplicate":
                self._log_order_event(
                    "order_import_auto_save_skipped_already_saved",
                    source=source, order_no=normalized_order_no
                )
            elif result == "error":
                # 保存失敗。状態は「保存失敗」（_save_order_no が設定）で保存済み扱いに
                # しない。同じ受注Noでも次の実行検知で再試行できる。
                self._log_order_event(
                    "order_import_auto_save_failed", source=source, order_no=normalized_order_no
                )
                self._log_order_event(
                    "order_import_auto_save_retry_allowed", source=source, order_no=normalized_order_no
                )
            else:
                self._log_order_event(
                    "order_import_auto_save_skipped_invalid", source=source, order_no=normalized_order_no
                )
            self._write_execute_debug(diag)
        finally:
            self._consume_pending_auto_save()
        event = {
            "saved": "save_success",
            "duplicate": "save_duplicate",
            "empty": "save_empty",
            "error": "save_exception",
        }.get(result, "save_exception")
        _write_execute_debug_event(event, **diag)

    def _write_execute_debug(self, diag: dict) -> None:
        """実行検知まわりの診断を work/debug 配下の JSONL へ出力する（デバッグ時のみ）。"""
        _write_execute_debug_event("execute_diagnostic", **diag)

    def _log_resource_snapshot(self, reason: str) -> None:
        """timer/thread/pending/ログ行数のスナップショットを出す（増殖の監視・要件8）。"""
        thread_count = sum(
            1 for t in (self._capture_thread, self._execute_thread) if t is not None
        ) + len(_STOPPING_THREADS)
        self._log_order_event(
            "order_capture_resource_snapshot",
            reason=reason,
            order_capture_thread_count=thread_count,
            order_capture_pending_queue_size=len(self._pending_auto_save),
            order_capture_log_line_count=_WORKER_DEBUG_LOG_LINES + _EXECUTE_DEBUG_LOG_LINES,
            stopping_threads=len(_STOPPING_THREADS),
            flush_scheduled=self._flush_timer_scheduled,
        )

    def _fast_poll_condition(self) -> bool:
        """自動保存ON かつ 見出で受注No保持中なら fast poll（見出→明細遷移待ち・要件7）。"""
        try:
            if not self._auto_save_check.isChecked():
                return False
            return bool(captured_orders.normalize_captured_order_no(self._last_header_order_no))
        except Exception:  # noqa: BLE001
            return False

    def _current_auto_capture_interval(self) -> int:
        """自動取得間隔（ms）。

        本体プロセスで Win32/UIA を直接呼ばないよう、対象画面の有無は「直近helperが
        返した画面種別」から推定する（要件2）。連続失敗/timeout時はバックオフ間隔へ広げ、
        成功したら通常間隔へ戻す（要件7）。自動保存ONで見出→明細遷移を待つ間は fast poll。
        """
        # backoff中でも永久停止せず、上限5秒程度で再試行を続ける（要件5）。
        if self._capture_backoff_active:
            return min(_CAPTURE_BACKOFF_INTERVAL_MAX_MS, _CAPTURE_BACKOFF_INTERVAL_MS)
        if self._fast_poll_condition():
            return _AUTO_CAPTURE_FAST_POLL_INTERVAL_MS
        active = self._last_screen_type in ("header", "detail")
        interval = (
            _AUTO_CAPTURE_SCREEN_INTERVAL_MS if active else _AUTO_CAPTURE_NORMAL_INTERVAL_MS
        )
        return min(2000, max(_AUTO_CAPTURE_MIN_INTERVAL_MS, interval))

    def _refresh_auto_capture_interval(self) -> None:
        """現在の間隔を再計算し、タイマーへ即時反映する（バックオフ/fast poll切替時など）。"""
        # fast poll のON/OFF遷移をログに残す（要件7）。
        fast = self._fast_poll_condition() and not self._capture_backoff_active
        if fast != self._fast_poll_active:
            self._fast_poll_active = fast
            if fast:
                self._log_order_event(
                    "order_import_auto_save_fast_poll_enabled",
                    interval_ms=_AUTO_CAPTURE_FAST_POLL_INTERVAL_MS,
                )
            else:
                self._log_order_event("order_import_auto_save_fast_poll_disabled")
        if self._auto_capture_timer is None:
            return
        interval = self._current_auto_capture_interval()
        if self._auto_capture_timer.interval() != interval:
            self._auto_capture_timer.setInterval(interval)
            # どの間隔へ切り替わったかを明示する（要件1/5）。
            if self._capture_backoff_active:
                self._log_order_event(
                    "order_import_auto_capture_backoff_interval_set", interval_ms=interval
                )
                self._log_order_event("order_import_capture_interval_backoff", interval_ms=interval)
            elif fast:
                self._log_order_event(
                    "order_import_auto_capture_fast_interval_set", interval_ms=interval
                )
                self._log_order_event("order_import_capture_interval_fast", interval_ms=interval)
            else:
                self._log_order_event(
                    "order_import_auto_capture_interval_set", interval_ms=interval
                )
                self._log_order_event("order_import_capture_interval_normal", interval_ms=interval)

    def _apply_capture_backoff(self, error: str) -> None:
        """helper結果に応じてバックオフ状態を更新する（連続失敗で伸ばし、成功で戻す・要件7）。"""
        if error:
            self._capture_consecutive_failures += 1
            if (
                not self._capture_backoff_active
                and self._capture_consecutive_failures >= _CAPTURE_BACKOFF_FAILURE_THRESHOLD
            ):
                self._capture_backoff_active = True
                self._log_order_event(
                    "order_import_capture_backoff_applied",
                    consecutive_failures=self._capture_consecutive_failures,
                    interval_ms=_CAPTURE_BACKOFF_INTERVAL_MS,
                )
                self._log_order_event(
                    "order_import_auto_capture_backoff_applied",
                    consecutive_failures=self._capture_consecutive_failures,
                    interval_ms=_CAPTURE_BACKOFF_INTERVAL_MS,
                )
                self._refresh_auto_capture_interval()
        else:
            self._capture_consecutive_failures = 0
            if self._capture_backoff_active:
                self._capture_backoff_active = False
                self._log_order_event(
                    "order_import_capture_backoff_reset", interval_ms=self._current_auto_capture_interval()
                )
                self._log_order_event(
                    "order_import_auto_capture_backoff_reset",
                    interval_ms=self._current_auto_capture_interval(),
                )
                # 成功したので通常/fast intervalへ戻す（要件5）。
                self._log_order_event(
                    "order_import_auto_capture_success_interval_reset",
                    interval_ms=self._current_auto_capture_interval(),
                )
                self._refresh_auto_capture_interval()

    def _auto_capture_scheduler_needed(self) -> bool:
        """capture scheduler(自動取得timer)を動かすべきか（要件2）。

        「自動取得」と「自動保存」は別機能。自動取得ONなら受注No表示更新のために、
        自動保存ONなら見出→明細遷移の内部監視・保存のために helper を回す必要がある。
        どちらか一方でもONなら scheduler が必要。両方OFFのときだけ停止する。
        """
        try:
            return bool(
                self._auto_capture_check.isChecked() or self._auto_save_check.isChecked()
            )
        except Exception:  # noqa: BLE001
            return False

    def _start_auto_capture_timer(self) -> None:
        """自動取得を画面所有の QTimer 1本で開始する（worker内timerは使わない・要件6）。

        起動条件は「自動取得ON または 自動保存ON」（要件2）。自動保存ONで自動取得OFFの
        場合も、見出→明細遷移の監視・保存のために scheduler を動かす。
        """
        self._log_order_event("order_import_auto_capture_start_requested")
        self._log_order_event("order_import_auto_capture_timer_start_requested")
        if not self._auto_capture_scheduler_needed() or self._closing:
            reason = (
                "auto_capture_and_auto_save_off"
                if not self._auto_capture_scheduler_needed()
                else "closing"
            )
            self._log_order_event("order_import_auto_capture_disabled_reason", reason=reason)
            self._log_order_event(
                "order_import_auto_capture_timer_not_started_reason", reason=reason
            )
            return
        if not self._auto_capture_check.isChecked() and self._auto_save_check.isChecked():
            # 自動取得OFFだが自動保存ONのため scheduler を動かす（要件2）。
            self._log_order_event("order_import_capture_scheduler_needed_for_auto_save")
        self._log_order_event("order_import_auto_capture_enabled")
        interval = self._current_auto_capture_interval()
        if self._auto_capture_timer is None:
            timer = QTimer(self)
            timer.setTimerType(Qt.TimerType.CoarseTimer)
            timer.timeout.connect(self._on_auto_capture_tick)
            self._auto_capture_timer = timer
        self._auto_capture_timer.setInterval(interval)
        if not self._auto_capture_timer.isActive():
            self._auto_capture_timer.start()
            self._log_order_event("order_import_auto_capture_timer_restarted", interval_ms=interval)
        self._log_order_event("order_capture_auto_timer_started", interval_ms=interval)
        self._log_order_event("order_import_auto_capture_timer_started", interval_ms=interval)
        self._log_order_event(
            "order_import_auto_capture_timer_active",
            active=self._auto_capture_timer.isActive(),
            interval_ms=self._auto_capture_timer.interval(),
        )

    def _stop_auto_capture_timer(self, *, reason: str = "hide_or_close") -> None:
        # 自動取得timerは hide/close/app終了時だけ止める。timeout/failure/manual失敗では
        # 絶対に止めない（要件3）。呼び出し理由をログに残す。
        self._log_order_event("order_import_auto_capture_timer_stop_requested_reason", reason=reason)
        if self._auto_capture_timer is not None:
            self._auto_capture_timer.stop()
        self._log_order_event("order_capture_timer_stopped_on_close")
        self._capture_tick_running = False
        self._auto_capture_initial_tick_scheduled = False
        self._capture_rerun_requested = False
        self._manual_capture_rerun_requested = False
        self._log_order_event("order_capture_auto_timer_stopped")

    def _ensure_auto_capture_timer_running(self, *, reason: str) -> bool:
        """表示中かつ auto有効なのに timer が停止していたら必ず再起動する（要件3）。

        timeout/failure/manual完了/heartbeat から呼ばれ、自動取得を止めたままにしない。
        再起動したら True を返す。
        """
        if self._closing or not self.isVisible():
            return False
        if not self._auto_capture_scheduler_needed():
            return False
        timer = self._auto_capture_timer
        if timer is not None and timer.isActive():
            return False
        self._log_order_event(
            "order_import_auto_capture_timer_inactive_detected_visible", reason=reason
        )
        self._start_auto_capture_timer()
        return True

    def _on_auto_capture_tick(self) -> None:
        """UI側timerのtick。重い検出は単発workerへ渡す。"""
        try:
            if self._closing or not self.isVisible() or not self._auto_capture_scheduler_needed():
                self._auto_capture_initial_tick_scheduled = False
                self._log_order_event("order_capture_delayed_tick_ignored_closed")
                return
            self._auto_capture_initial_tick_scheduled = False
            self._log_order_event("order_capture_tick_started")
            self._log_order_event("order_import_auto_capture_tick")
            if self._capture_worker_running:
                # helper実行中に次tickが来たらskip（多重起動防止・要件7）。
                self._capture_rerun_requested = True
                self._capture_rerun_count += 1
                self._log_order_event(
                    "order_import_capture_tick_skipped_helper_running",
                    order_capture_perf_rerun_count=self._capture_rerun_count,
                )
                self._log_order_event(
                    "order_import_auto_capture_tick_skipped_helper_running",
                    order_capture_perf_rerun_count=self._capture_rerun_count,
                )
                self._log_order_event(
                    "order_capture_worker_rerun_requested",
                    order_capture_perf_rerun_count=self._capture_rerun_count,
                )
                return
            self._start_auto_capture_worker_once()
        except Exception as exc:  # noqa: BLE001
            self._capture_tick_running = False
            self._log_slot_exception("order_import_unhandled_slot_exception", exc, source="auto_capture_tick")

    def _start_auto_capture_worker_once(self) -> None:
        self._start_capture_process_once(source="auto")

    def _start_capture_worker_once(self, *, source: str) -> None:
        # 互換エイリアス（旧QThread worker 経路→QProcess 経路・要件2）。
        self._start_capture_process_once(source=source)

    def _start_capture_process_once(self, *, source: str) -> None:
        source = "manual" if source == "manual" else "auto"
        if self._closing:
            self._log_order_event("order_capture_delayed_tick_ignored_closed")
            return
        if source == "manual" and not self.isVisible():
            self._log_order_event("order_import_manual_capture_started", mode="sync_hidden")
            result = self._capture_once_via_helper(
                timeout_ms=_CAPTURE_HELPER_MANUAL_TIMEOUT_MS
            )
            self._on_capture_worker_finished(
                str(result.get("order_no") or ""),
                str(result.get("error") or ""),
                float(result.get("elapsed_ms") or 0.0),
                str(result.get("screen_type") or "unknown"),
                self._generation,
                source="manual",
            )
            return
        if source == "auto" and (not self.isVisible() or not self._auto_capture_scheduler_needed()):
            self._log_order_event("order_capture_delayed_tick_ignored_closed")
            return
        if self._capture_worker_running:
            if source == "manual":
                self._manual_capture_rerun_requested = True
                self._log_order_event("order_import_manual_capture_started", queued=True)
            else:
                self._capture_rerun_requested = True
            self._capture_rerun_count += 1
            self._log_order_event(
                "order_import_capture_tick_skipped_helper_running",
                order_capture_perf_rerun_count=self._capture_rerun_count,
                source=source,
            )
            if source == "auto":
                self._log_order_event(
                    "order_import_auto_capture_tick_skipped_helper_running",
                    order_capture_perf_rerun_count=self._capture_rerun_count,
                )
            self._log_order_event(
                "order_capture_worker_rerun_requested",
                order_capture_perf_rerun_count=self._capture_rerun_count,
                source=source,
            )
            return
        self._log_order_event("order_import_capture_worker_started")
        self._log_order_event("order_capture_worker_start_requested")
        if source == "manual":
            self._log_order_event("order_import_manual_capture_started")
        else:
            self._log_order_event("order_import_auto_capture_helper_start_requested")
        # UIA/COM/Win32 は本体では呼ばず、別プロセスhelperを QProcess で起動する（要件2/4）。
        # QProcess は main thread 所有で非同期。QThread は使わない（Qt abort 対策・要件2）。
        command = _resolve_capture_helper_command()
        if not command:
            # helper未解決: プロセスを起動せず「取得不可」として即 finished 経路へ（要件4/5）。
            self._log_order_event(
                "order_import_capture_process_error", reason="helper_unavailable"
            )
            _write_crash_probe_event(
                "order_import_capture_process_error", reason="helper_unavailable"
            )
            self._on_capture_worker_finished(
                "", "helper_unavailable", 0.0, "unknown", self._generation, source=source
            )
            return
        debug = _capture_debug_enabled()
        argv = list(command) + (["--debug"] if debug else [])
        program = argv[0]
        args = argv[1:]
        process = QProcess(self)
        self._capture_process = process
        self._capture_process_source = source
        self._capture_process_generation = self._generation
        self._capture_process_stdout = ""
        self._capture_process_stderr = ""
        self._capture_result_delivered = False
        self._capture_process_started_at = time.monotonic()
        # source ごとの timeout（manual は長めに待つ・要件1/4）。helperが結果JSONを返す前に
        # killしないよう、2秒ではなく auto=5秒 / manual=10秒 とする。
        timeout_ms = (
            _CAPTURE_HELPER_MANUAL_TIMEOUT_MS
            if source == "manual"
            else _CAPTURE_HELPER_AUTO_TIMEOUT_MS
        )
        self._capture_process_timeout_ms = timeout_ms
        self._log_order_event(
            "order_import_capture_helper_timeout_ms", source=source, timeout_ms=timeout_ms
        )
        if source == "manual":
            self._log_order_event(
                "order_import_capture_manual_timeout_ms", timeout_ms=timeout_ms
            )
        else:
            self._log_order_event(
                "order_import_capture_auto_timeout_ms", timeout_ms=timeout_ms
            )
        process.readyReadStandardOutput.connect(self._on_capture_process_stdout)
        process.readyReadStandardError.connect(self._on_capture_process_stderr)
        process.finished.connect(self._on_capture_process_finished)
        process.errorOccurred.connect(self._on_capture_process_error)
        # timeout 管理は QThread ではなく main thread 所有の単発 QTimer で行う（要件2/4）。
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(timeout_ms)
        timer.timeout.connect(self._on_capture_process_timeout)
        self._capture_timeout_timer = timer
        self._capture_process_running = True
        self._capture_tick_running = True
        self._log_order_event("order_capture_worker_ref_held")
        try:
            process.start(program, args)
        except Exception as exc:  # noqa: BLE001 - 起動失敗でもアプリを落とさない
            self._log_slot_exception(
                "order_import_capture_process_error", exc, source="process_start"
            )
            self._deliver_capture_result(
                {"order_no": "", "error": "helper_spawn_failed", "screen_type": "unknown"}
            )
            return
        timer.start()
        self._log_order_event("order_import_capture_process_started", source=source)
        _write_crash_probe_event("order_import_capture_process_started", source=source)
        self._log_order_event("order_capture_worker_started")
        if source == "auto":
            self._log_order_event("order_import_auto_capture_helper_started")

    # ── QProcess ベースの helper 実行（main thread 所有・要件2） ──────────────────
    def _on_capture_process_stdout(self) -> None:
        proc = self._capture_process
        if proc is None:
            return
        try:
            self._capture_process_stdout += bytes(proc.readAllStandardOutput()).decode(
                "utf-8", "replace"
            )
        except Exception:  # noqa: BLE001
            pass
        self._log_order_event("order_import_capture_process_stdout_ready")

    def _on_capture_process_stderr(self) -> None:
        proc = self._capture_process
        if proc is None:
            return
        try:
            self._capture_process_stderr += bytes(proc.readAllStandardError()).decode(
                "utf-8", "replace"
            )
        except Exception:  # noqa: BLE001
            pass
        _LOGGER.info(
            "order_import_capture_process_stderr_ready stderr=%r",
            _truncate_helper_stream(self._capture_process_stderr),
        )
        self._log_order_event("order_import_capture_process_stderr_ready")

    def _collect_process_streams(self) -> None:
        """finished/timeout時に残りの stdout/stderr を必ず回収する（要件2）。

        readyReadStandardOutput が発火しなくても finished 時にここで stdout を読む。
        """
        proc = self._capture_process
        if proc is not None:
            try:
                self._capture_process_stdout += bytes(proc.readAllStandardOutput()).decode(
                    "utf-8", "replace"
                )
                self._capture_process_stderr += bytes(proc.readAllStandardError()).decode(
                    "utf-8", "replace"
                )
            except Exception:  # noqa: BLE001
                pass
        stdout = (self._capture_process_stdout or "").strip()
        stderr = (self._capture_process_stderr or "").strip()
        self._log_order_event(
            "order_import_capture_process_finished_stdout_collected", length=len(stdout)
        )
        self._log_order_event(
            "order_import_capture_process_finished_stderr_collected", length=len(stderr)
        )
        if stdout:
            self._log_order_event(
                "order_import_capture_helper_stdout_preview",
                stdout=_truncate_helper_stream(stdout),
            )
        else:
            self._log_order_event("order_import_capture_helper_empty_stdout")
        if stderr:
            self._log_order_event(
                "order_import_capture_helper_stderr_preview",
                stderr=_truncate_helper_stream(stderr),
            )
            _LOGGER.info(
                "order_import_capture_helper_stderr_preview stderr=%r",
                _truncate_helper_stream(stderr),
            )

    def _on_capture_process_finished(self, exit_code: int = 0, exit_status=None) -> None:
        self._log_order_event("order_import_capture_process_finished", exit_code=exit_code)
        _write_crash_probe_event("order_import_capture_process_finished", exit_code=exit_code)
        elapsed_ms = round((time.monotonic() - self._capture_process_started_at) * 1000, 2)
        self._log_order_event("order_import_capture_process_elapsed_ms", elapsed_ms=elapsed_ms)
        self._collect_process_streams()
        result = self._interpret_capture_stdout(exit_code, self._capture_process_stdout)
        self._deliver_capture_result(result)

    def _on_capture_process_error(self, error=None) -> None:
        self._log_order_event("order_import_capture_process_error", error=str(error))
        _write_crash_probe_event("order_import_capture_process_error", error=str(error))
        # finished と同時に来ることがある。結果は _capture_result_delivered で1回だけ配送する。
        self._deliver_capture_result(
            {"order_no": "", "error": "process_error", "screen_type": "unknown"}
        )

    def _on_capture_process_timeout(self) -> None:
        elapsed_ms = round((time.monotonic() - self._capture_process_started_at) * 1000, 2)
        self._log_order_event("order_import_capture_process_timeout")
        self._log_order_event(
            "order_import_capture_process_timeout_source",
            source=self._capture_process_source,
            timeout_ms=self._capture_process_timeout_ms,
            elapsed_ms=elapsed_ms,
        )
        self._log_order_event("order_import_capture_process_elapsed_ms", elapsed_ms=elapsed_ms)
        _write_crash_probe_event(
            "order_import_capture_process_timeout",
            source=self._capture_process_source,
            elapsed_ms=elapsed_ms,
        )
        # kill する前に、既に返っている stdout/stderr を回収してログへ残す（要件2）。
        self._collect_process_streams()
        proc = self._capture_process
        if proc is not None:
            self._log_order_event("order_import_capture_process_kill_requested")
            _write_crash_probe_event("order_import_capture_process_kill_requested")
            try:
                proc.kill()
                proc.waitForFinished(500)
            except Exception:  # noqa: BLE001 - kill失敗でもアプリを落とさない
                pass
        if self._capture_process_source == "manual":
            self._log_order_event("order_import_manual_capture_timeout")
        self._deliver_capture_result(
            {"order_no": "", "error": "timeout", "screen_type": "unknown"}
        )

    def _interpret_capture_stdout(self, exit_code, raw_out: str) -> dict:
        """helper の stdout(JSON) と returncode を安全な結果 dict へ変換する（要件4）。

        非0 returncode・空 stdout・不正JSON でも例外にせず error 付き dict を返す。
        """
        order_no = ""
        error = ""
        screen_type = "unknown"
        reason = ""
        raw = (raw_out or "").strip()
        if exit_code not in (0, None):
            error = f"helper_returncode_{exit_code}"
        elif not raw:
            # returncode=0 かつ stdout空（要件2）。
            error = "helper_empty_stdout"
            self._log_order_event("order_import_capture_helper_empty_stdout")
        else:
            try:
                parsed = _parse_capture_helper_output(raw)
                order_no = parsed["order_no"]
                screen_type = parsed["screen_type"]
                reason = parsed["reason"]
                self._log_order_event("order_import_capture_helper_json_parsed")
                self._log_order_event(
                    "order_import_capture_helper_result_summary",
                    screen_type=screen_type,
                    order_no=order_no,
                    reason=reason,
                )
            except Exception:  # noqa: BLE001 - 不正JSONでもアプリを落とさない
                error = "invalid_json"
                self._log_order_event(
                    "order_import_capture_helper_json_parse_failed",
                    raw=_truncate_helper_stream(raw),
                )
                _LOGGER.warning(
                    "order_import_capture_process_invalid_json raw=%r",
                    _truncate_helper_stream(raw),
                )
        return {
            "order_no": order_no,
            "error": error,
            "screen_type": screen_type,
            "reason": reason,
        }

    def _deliver_capture_result(self, result: dict) -> None:
        """finished/timeout/error の全経路で結果を1回だけ配送する（二重配送防止・要件2）。"""
        if self._capture_result_delivered:
            return
        self._capture_result_delivered = True
        source = self._capture_process_source
        generation = self._capture_process_generation
        elapsed_ms = round((time.monotonic() - self._capture_process_started_at) * 1000, 2)
        _write_crash_probe_event("order_import_crash_probe_before_cleanup")
        self._cleanup_capture_process()
        _write_crash_probe_event("order_import_crash_probe_after_cleanup")
        self._on_capture_worker_finished(
            str(result.get("order_no") or ""),
            str(result.get("error") or ""),
            elapsed_ms,
            str(result.get("screen_type") or "unknown"),
            generation,
            source=source,
        )

    def _cleanup_capture_process(self) -> None:
        """QProcess/timeout timer を安全に破棄し、フラグを解除する（全経路共通・要件2）。"""
        self._log_order_event("order_import_capture_process_cleanup_entered")
        _write_crash_probe_event("order_import_capture_process_cleanup_entered")
        try:
            timer = self._capture_timeout_timer
            self._capture_timeout_timer = None
            if timer is not None:
                try:
                    timer.stop()
                    timer.deleteLater()
                except Exception:  # noqa: BLE001
                    pass
            proc = self._capture_process
            self._capture_process = None
            if proc is not None:
                for signal in (
                    proc.readyReadStandardOutput,
                    proc.readyReadStandardError,
                    proc.finished,
                    proc.errorOccurred,
                ):
                    try:
                        signal.disconnect()
                    except Exception:  # noqa: BLE001 - 未接続でも継続
                        pass
                try:
                    running = int(proc.state()) != 0  # 0 == QProcess.NotRunning
                except Exception:  # noqa: BLE001
                    running = False
                if running:
                    self._log_order_event(
                        "order_import_capture_process_kill_requested", reason="cleanup"
                    )
                    try:
                        proc.kill()
                        proc.waitForFinished(300)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    proc.deleteLater()
                except Exception:  # noqa: BLE001
                    pass
            self._capture_process_running = False
            self._capture_tick_running = False
            self._log_order_event("order_import_capture_process_refs_cleared")
            _write_crash_probe_event("order_import_capture_process_refs_cleared")
        except Exception as exc:  # noqa: BLE001 - cleanup 失敗でもアプリを落とさない
            self._log_slot_exception(
                "order_import_capture_cleanup_exception", exc, source="cleanup_capture_process"
            )
        self._log_order_event("order_import_capture_process_cleanup_finished")
        _write_crash_probe_event("order_import_capture_process_cleanup_finished")

    def _stop_auto_capture_worker(self) -> None:
        """実行中の helper process を画面破棄時に安全に停止する（close/hide・要件2/7）。

        close/hide中に process finished/timeout が戻ってきても結果は破棄する
        （_capture_result_delivered=True でガード）。QThread は使わない。
        """
        self._log_order_event("order_capture_worker_cleanup_started")
        self._log_order_event("order_import_capture_worker_cleanup_entered")
        _write_crash_probe_event("order_import_capture_worker_cleanup_entered")
        try:
            self._capture_rerun_requested = False
            self._manual_capture_rerun_requested = False
            # 破棄後に戻ってくる結果は配送しない。
            self._capture_result_delivered = True
            if self._capture_process is not None or self._capture_timeout_timer is not None:
                self._cleanup_capture_process()
            else:
                self._capture_process_running = False
                self._capture_tick_running = False
            _write_crash_probe_event("order_import_capture_refs_cleared")
            self._log_order_event("order_import_capture_refs_cleared")
        except Exception as exc:  # noqa: BLE001 - cleanup 失敗でclose処理を止めない
            self._log_slot_exception(
                "order_import_capture_cleanup_exception", exc, source="stop_auto_capture_worker"
            )
        self._log_order_event("order_capture_worker_cleanup_finished")
        self._log_order_event("order_import_capture_worker_cleanup_finished")
        _write_crash_probe_event("order_import_capture_worker_cleanup_finished")
        _write_crash_probe_event("order_import_capture_after_failure_recovery_ready")

    def _on_auto_capture_worker_finished(
        self,
        order_no: str,
        error: str,
        elapsed_ms: float,
        screen_type: str = "unknown",
        generation: int | None = None,
    ) -> None:
        self._on_capture_worker_finished(
            order_no,
            error,
            elapsed_ms,
            screen_type,
            generation,
            source="auto",
        )

    def _on_capture_worker_finished(
        self,
        order_no: str,
        error: str,
        elapsed_ms: float,
        screen_type: str = "unknown",
        generation: int | None = None,
        *,
        source: str = "auto",
    ) -> None:
        try:
            source = "manual" if source == "manual" else "auto"
            # 成功/失敗/timeout/invalid_json の全経路で必ずフラグを解除する（要件6）。
            self._capture_worker_running = False
            self._capture_tick_running = False
            self._log_order_event("order_import_capture_flag_released", source=source)
            self._log_order_event("order_import_capture_worker_finished")
            if source == "manual":
                self._log_order_event("order_import_manual_capture_finished", has_error=bool(error))
            else:
                self._log_order_event("order_import_auto_capture_helper_finished", has_error=bool(error))
            if error:
                if source == "manual":
                    self._log_order_event("order_import_manual_capture_failed", error=error)
                else:
                    self._log_order_event("order_import_auto_capture_helper_failed", error=error)
            self._log_order_event("order_import_auto_capture_flag_released")
            self._log_order_event(
                "order_capture_worker_finished",
                order_capture_perf_worker_elapsed_ms=elapsed_ms,
                has_order=bool(order_no),
                has_error=bool(error),
                screen_type=screen_type,
            )
            if elapsed_ms >= 300:
                self._log_order_event(
                    "order_capture_perf_worker_elapsed_ms",
                    elapsed_ms=elapsed_ms,
                    slow=True,
                )
            if error:
                self._log_order_event("order_capture_worker_exception", error=error)
            # close/hide 後に戻ってきた在庫結果は世代不一致で破棄する（要件7）。
            if generation is not None and generation != self._generation:
                self._log_order_event(
                    "order_import_worker_result_generation_mismatch",
                    got=generation,
                    expected=self._generation,
                )
                self._log_order_event(
                    "order_import_auto_capture_result_ignored_reason", reason="generation_mismatch"
                )
                self._log_order_event(
                    "order_import_auto_capture_result_ignored",
                    reason="generation_mismatch",
                )
                self._capture_rerun_requested = False
                self._manual_capture_rerun_requested = False
                return
            if self._closing or not self._is_qobject_valid(self):
                self._log_order_event("order_import_worker_result_ignored_closed")
                self._log_order_event("order_capture_worker_result_ignored_closed")
                self._log_order_event(
                    "order_import_auto_capture_result_ignored_reason", reason="closing"
                )
                self._log_order_event(
                    "order_import_auto_capture_result_ignored",
                    reason="closing",
                )
                self._capture_rerun_requested = False
                self._manual_capture_rerun_requested = False
                return
            if source == "auto" and not self.isVisible():
                self._log_order_event("order_import_worker_result_ignored_hidden")
                self._log_order_event("order_capture_worker_result_ignored_closed")
                self._log_order_event(
                    "order_import_auto_capture_result_ignored_reason", reason="not_visible"
                )
                self._log_order_event(
                    "order_import_auto_capture_result_ignored",
                    reason="not_visible",
                )
                self._capture_rerun_requested = False
                return
            # helper連続失敗/timeoutでバックオフ、成功で解除する（要件7）。
            self._apply_capture_backoff(error)
            self._log_order_event("order_import_capture_elapsed_ms", elapsed_ms=elapsed_ms)
            ui_started = time.monotonic()
            # 自動取得OFF/自動保存ONのとき、UI表示更新は抑制し内部監視だけ動かす（要件2）。
            auto_capture_ui = self._auto_capture_check.isChecked()
            if order_no:
                self._log_order_event("order_capture_tick_detected", order_no=order_no)
                self._log_order_event(
                    "order_import_capture_result_reflected",
                    order_no=order_no,
                    source=source,
                )
                self._log_order_event(
                    "order_import_auto_capture_result_reflected", order_no=order_no
                )
                if source == "manual" or auto_capture_ui:
                    self._on_worker_captured(order_no)
                    if source == "manual":
                        # 手動取得成功: 即反映済み。failure_count は _on_worker_captured で0化済み。
                        self._log_order_event("order_import_manual_capture_success", order_no=order_no)
                        self._log_order_event("order_import_manual_capture_reset_failure_count")
                else:
                    # 自動保存のためだけに動作中。表示は更新せず、内部の有効値だけ保持する。
                    self._auto_capture_failures = 0
                    self._remember_valid_order_no_internal(order_no)
                    self._log_order_event(
                        "order_import_auto_capture_ui_update_skipped_disabled", order_no=order_no
                    )
                    self._log_order_event(
                        "order_import_auto_save_monitor_running_without_auto_capture"
                    )
            else:
                self._log_order_event("order_capture_tick_not_detected")
                if error:
                    self._log_order_event(
                        "order_import_auto_capture_result_ignored_reason", reason=error
                    )
                    self._log_order_event(
                        "order_import_auto_capture_result_ignored", reason=error
                    )
                    if error == "timeout":
                        self._log_order_event(
                            "order_import_auto_capture_retry_scheduled", reason=error
                        )
                        self._log_order_event(
                            "order_import_auto_capture_next_retry_scheduled", reason=error
                        )
                if source == "manual":
                    fallback_order_no = self._order_input.text().strip()
                    if fallback_order_no:
                        self._reflect_detected_order_no(fallback_order_no)
                    else:
                        reason = error or "empty"
                        self._log_order_event(
                            "order_import_capture_result_ignored_reason",
                            reason=reason,
                            source=source,
                        )
                        self._log_order_event(
                            "order_import_manual_capture_failed_reason", reason=reason
                        )
                        # 手動取得失敗は理由（timeout等）を画面へ出す（要件4）。
                        if error == "timeout":
                            self._set_status("取得失敗", "取得失敗: timeout（もう一度お試しください）")
                        else:
                            self._set_status("取得不可", self._capture_failure_text())
                elif auto_capture_ui:
                    self._on_worker_capture_failed()
                    # timeout/failure/window_not_found でも auto timer は止めない（要件3）。
                    self._log_order_event("order_import_auto_capture_timer_not_stopped_on_failure")
                else:
                    # 自動保存のためだけに動作中。取得不可のUI表示は出さない（要件2）。
                    self._log_order_event("order_import_auto_capture_ui_update_skipped_disabled")
            # 画面種別に応じて見出→明細の遷移保存を扱う（要件3）。
            self._handle_capture_screen_type(screen_type, order_no)
            reflect_elapsed = round((time.monotonic() - ui_started) * 1000, 2)
            self._log_order_event(
                "order_capture_perf_ui_elapsed_ms", elapsed_ms=reflect_elapsed
            )
            self._log_order_event(
                "order_import_capture_result_reflected_elapsed_ms", elapsed_ms=reflect_elapsed
            )
            _write_crash_probe_event("order_import_crash_probe_before_next_tick_schedule")
            if self._manual_capture_rerun_requested:
                self._manual_capture_rerun_requested = False
                self._capture_rerun_requested = False
                self._log_order_event("order_capture_worker_rerun_consumed", source="manual")
                QTimer.singleShot(0, lambda: self._start_capture_worker_once(source="manual"))
            elif self._capture_rerun_requested and self._auto_capture_scheduler_needed():
                self._capture_rerun_requested = False
                self._log_order_event("order_capture_worker_rerun_consumed")
                self._log_order_event(
                    "order_import_auto_capture_retry_scheduled",
                    reason="rerun_requested",
                )
                self._log_order_event(
                    "order_import_auto_capture_next_retry_scheduled",
                    reason="rerun_requested",
                )
                QTimer.singleShot(0, self._start_auto_capture_worker_once)
            else:
                self._capture_rerun_requested = False
            _write_crash_probe_event("order_import_crash_probe_after_next_tick_schedule")
            # 取得完了後、表示中なのに auto timer が停止していたら必ず再開する（要件3/5）。
            # timeout/failure/manual完了のいずれでも自動取得を止めたままにしない。
            recovered = self._ensure_auto_capture_timer_running(reason="worker_finished")
            if source == "manual" and self._auto_capture_check.isChecked():
                # 手動取得完了後は auto timer を必ず継続/再開する（要件4）。
                self._log_order_event("order_import_manual_capture_auto_timer_resumed")
                if recovered:
                    self._log_order_event("order_import_auto_capture_timer_restart_after_manual")
            self._log_order_event(
                "order_capture_perf_snapshot",
                order_capture_perf_rerun_count=self._capture_rerun_count,
                worker_running=self._capture_worker_running,
            )
        except Exception as exc:  # noqa: BLE001
            self._capture_worker_running = False
            self._capture_tick_running = False
            self._capture_rerun_requested = False
            self._manual_capture_rerun_requested = False
            self._log_order_event("order_import_auto_capture_flag_released")
            self._log_order_event("order_import_capture_flag_released", source=source)
            self._log_slot_exception("order_import_unhandled_slot_exception", exc, source="capture_worker_finished")

    def _handle_capture_screen_type(self, screen_type: str, order_no: str) -> None:
        """capture worker が返した画面種別で見出→明細の遷移保存を扱う（要件3）。

        - header 画面で有効受注Noを検出したら last_header_order_no に保持する。
        - detail 画面を検出したら（自動保存ON時のみ）last_header_order_no を保存する。
        - 別 worker/別 timer は起動せず、capture worker 1本の結果だけで状態遷移する。
        """
        transition_started = time.monotonic()
        screen_type = (screen_type or "unknown").strip() or "unknown"
        self._last_screen_type = screen_type
        auto_save_only = (
            self._auto_save_check.isChecked() and not self._auto_capture_check.isChecked()
        )
        if screen_type == "header":
            self._log_order_event("order_import_capture_screen_type_header")
            remembered = (order_no or "").strip() or (self._last_valid_order_no or "").strip()
            if captured_orders.normalize_captured_order_no(remembered):
                self._last_header_order_no = remembered
                self._log_order_event(
                    "order_import_header_order_remembered", order_no=remembered
                )
                if auto_save_only:
                    # 自動取得OFF/自動保存ONでも内部で直前受注Noを保持する（要件2）。
                    self._log_order_event(
                        "order_import_auto_save_monitor_header_remembered", order_no=remembered
                    )
            # 見出へ戻ったら、同一受注Noでも次の明細遷移で再保存できるよう抑止を解く。
            self._last_capture_transition_order_no = ""
        elif screen_type == "detail":
            self._log_order_event("order_import_capture_screen_type_detail")
            self._handle_detail_transition_from_capture()
        elif screen_type == "none":
            self._log_order_event("order_import_capture_screen_type_none")
            self._log_order_event("order_import_auto_capture_window_not_found_continue")
        # 見出で受注No保持に入ったら fast poll、遷移完了で解除…等をタイマーへ即反映（要件7）。
        self._refresh_auto_capture_interval()
        self._log_order_event(
            "order_import_transition_detect_elapsed_ms",
            elapsed_ms=round((time.monotonic() - transition_started) * 1000, 2),
        )

    def _handle_detail_transition_from_capture(self) -> None:
        """detail 検出時、保持済みの見出受注Noを（自動保存ONのときだけ）保存する（要件3）。

        検出→保存は main thread で即時に実行し、ディスクflush完了は待たない（要件7）。
        """
        if not self._auto_save_check.isChecked():
            self._log_order_event("order_import_transition_save_skipped_auto_save_off")
            return
        order_no = (self._last_header_order_no or "").strip()
        if not captured_orders.normalize_captured_order_no(order_no):
            # 見出を取り逃していても、保持済み候補（最新/最後の有効値）で保存する。
            order_no = self._resolve_current_cached_order_no()
        normalized = captured_orders.normalize_captured_order_no(order_no)
        if not normalized:
            self._log_order_event("order_import_transition_save_skipped_no_order")
            return
        if normalized == self._last_capture_transition_order_no:
            self._log_order_event(
                "order_import_transition_save_duplicate_suppressed", order_no=normalized
            )
            return
        self._log_order_event(
            "order_import_header_to_detail_transition_by_capture", order_no=normalized
        )
        self._log_order_event("order_import_transition_save_started", order_no=normalized)
        save_started = time.monotonic()
        # _save_order_no はメモリstage即時 → UI即更新 → flushは非同期予約（待たない）。
        result = self._save_order_no(
            normalized,
            method="f12",
            source="capture_transition",
            saved_message="保存しました",
            duplicate_message="既に保存済みです",
        )
        self._last_capture_transition_order_no = normalized
        self._log_order_event(
            "order_import_transition_save_elapsed_ms",
            order_no=normalized,
            result=result,
            elapsed_ms=round((time.monotonic() - save_started) * 1000, 2),
        )
        self._log_order_event(
            "order_import_transition_save_finished", order_no=normalized, result=result
        )
        if not self._auto_capture_check.isChecked():
            # 自動取得OFF/自動保存ONでの見出→明細保存（要件2）。
            self._log_order_event(
                "order_import_auto_save_monitor_detail_saved",
                order_no=normalized,
                result=result,
            )

    def _run_auto_capture_sync_for_test(self) -> None:
        """単体テスト用: workerを使わず1回分の結果反映経路だけ検証する。"""
        started = time.monotonic()
        try:
            order_no = (capture_order_no_from_tkscloud8() or "").strip()
            if order_no:
                self._log_order_event("order_capture_tick_detected", order_no=order_no)
                self._on_worker_captured(order_no)
            else:
                self._log_order_event("order_capture_tick_not_detected")
                self._on_worker_capture_failed()
        except Exception:  # noqa: BLE001 - 自動取得失敗で画面を落とさない
            _LOGGER.warning("受注Noの自動取得tickで例外が発生しました。", exc_info=True)
            self._on_worker_capture_failed()
        finally:
            self._capture_tick_running = False
            elapsed_ms = round((time.monotonic() - started) * 1000, 2)
            self._log_order_event(
                "order_capture_tick_finished",
                order_capture_perf_tick_elapsed_ms=elapsed_ms,
            )
            if self._auto_capture_timer is not None:
                interval = self._current_auto_capture_interval()
                if self._auto_capture_timer.interval() != interval:
                    self._auto_capture_timer.setInterval(interval)

    # ── 自動処理ワーカーの起動・停止 ──────────────────────────────────────────
    def _start_workers(self) -> None:
        """自動取得timer・heartbeatを起動する（表示中のみ・冪等・要件4）。

        常駐処理は「自動取得timer + single-flight capture worker + heartbeat」だけに絞る。
        showEventが複数回来ても二重にtimer/worker/signalを積まない。
        """
        if self._workers_started:
            # 既に起動済み。timer/worker/signalは増やさず、設定同期だけ行う。
            self._log_order_event("order_import_workers_already_started_skip")
            self._start_auto_capture_timer()
            self._maybe_start_execute_monitor()
            return
        self._log_order_event("order_import_workers_start_requested")
        self._workers_started = True
        self._start_auto_capture_timer()
        self._maybe_start_execute_monitor()
        self._start_heartbeat()
        self._log_resource_snapshot("workers_started")

    def _maybe_start_execute_monitor(self) -> None:
        """実行検知ワーカー（execute monitor）は原則起動しない（要件2）。

        安定性のため execute monitor 専用の常駐QThreadは既定で完全停止する。
        自動保存ONだけでは起動せず、見出→明細の遷移保存は capture worker の結果で扱う。
        起動できるのは tks_capture/f12_monitor_enabled=True を明示設定した場合のみ（既定OFF）。
        """
        if self._closing or not self.isVisible():
            return
        f12_monitor = self._load_f12_monitor_setting()
        if not f12_monitor:
            # 既定: execute monitor は起動しない。自動保存ONでも起動しない（クラッシュ源の停止）。
            self._log_order_event("order_import_execute_monitor_disabled_by_default")
            if self._auto_save_check.isChecked():
                self._log_order_event("order_import_execute_monitor_not_started_auto_save_only")
            self._log_order_event("order_import_execute_monitor_forced_off_for_stability")
            self._stop_execute_monitor()
            return
        if self._execute_thread is not None:
            # 明示ONで既に起動済み。有効/監視フラグだけ最新へ同期する。
            worker = self._execute_worker
            if worker is not None:
                worker.set_enabled(self._auto_save_check.isChecked())
                worker.set_f12_monitor(f12_monitor)
            return
        debug = _capture_debug_enabled()
        execute_thread = QThread()
        execute_worker = _ExecuteWorker()
        execute_worker.set_enabled(self._auto_save_check.isChecked())
        execute_worker.set_f12_monitor(f12_monitor)
        execute_worker.set_debug(debug)
        execute_worker.set_order_context(self._latest_order_no, self._order_input.text().strip())
        execute_worker.moveToThread(execute_thread)
        execute_thread.started.connect(execute_worker.start)
        execute_thread.finished.connect(execute_worker.deleteLater)
        execute_worker.execute_detected.connect(self._on_worker_execute_detected)
        execute_worker.edge_diagnostics.connect(self._write_execute_debug)
        self._execute_thread = execute_thread
        self._execute_worker = execute_worker
        execute_thread.start()
        self._log_order_event(
            "order_import_execute_monitor_started_explicit_only",
            auto_save=self._auto_save_check.isChecked(),
            f12_monitor=f12_monitor,
            interval_ms=_EXECUTE_POLL_INTERVAL_MS,
        )

    def _stop_workers(self) -> None:
        """ワーカーを安全に停止する（UIを固めないよう長時間 wait しない）。"""
        self._workers_started = False
        self._stop_heartbeat()
        self._stop_auto_capture_timer(reason="stop_workers")
        self._stop_auto_capture_worker()
        self._stop_execute_monitor()
        self._log_resource_snapshot("workers_stopped")

    def _start_heartbeat(self) -> None:
        """表示中、内部状態だけを一定間隔で記録するheartbeatを開始する（要件6）。"""
        if self._heartbeat_timer is None:
            timer = QTimer(self)
            timer.setTimerType(Qt.TimerType.VeryCoarseTimer)
            timer.setInterval(_HEARTBEAT_INTERVAL_MS)
            timer.timeout.connect(self._emit_heartbeat)
            self._heartbeat_timer = timer
        self._shown_at = time.monotonic()
        if not self._heartbeat_timer.isActive():
            self._heartbeat_timer.start()

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_timer is not None:
            self._heartbeat_timer.stop()

    def _emit_heartbeat(self) -> None:
        """落ちる直前の状態が分かるheartbeat（UIA/Win32探索はせず内部状態のみ・要件6）。"""
        try:
            qthread_count = sum(
                1 for t in (self._capture_thread, self._execute_thread) if t is not None
            ) + len(_STOPPING_THREADS)
            execute_running = bool(
                self._execute_thread is not None and self._execute_thread.isRunning()
            )
            elapsed_sec = round(time.monotonic() - self._shown_at, 1) if self._shown_at else 0.0
            state = {
                "elapsed_sec": elapsed_sec,
                "capture_timer_active": bool(
                    self._auto_capture_timer is not None and self._auto_capture_timer.isActive()
                ),
                "capture_worker_running": self._capture_worker_running,
                "capture_process_running": self._capture_process_running,
                "capture_thread_exists": self._capture_thread is not None,
                "failure_count": self._auto_capture_failures,
                "execute_monitor_running": execute_running,
                "active_thread_count": threading.active_count(),
                "active_qthread_count": qthread_count,
                "closing": self._closing,
                "visible": self.isVisible(),
                "last_screen_type": self._last_screen_type,
                "has_last_valid_order_no": bool((self._last_valid_order_no or "").strip()),
                "auto_save_on_execute": self._auto_save_check.isChecked(),
                "f12_monitor_enabled": self._load_f12_monitor_setting(),
            }
            self._log_order_event("order_import_window_heartbeat", **state)
            _write_crash_probe_event("order_import_window_heartbeat", **state)
            self._log_order_event("order_import_stability_state", **state)
            # visible=true / closing=false / auto有効 なのに timer inactive を検出したら
            # 必ず再起動する（要件3）。前回の不具合の恒久対策。
            if (
                not self._closing
                and self.isVisible()
                and self._auto_capture_check.isChecked()
                and not state["capture_timer_active"]
            ):
                if self._ensure_auto_capture_timer_running(reason="heartbeat"):
                    self._log_order_event("order_import_auto_capture_timer_recovered_by_heartbeat")
                    _write_crash_probe_event(
                        "order_import_auto_capture_timer_recovered_by_heartbeat"
                    )
            self._log_order_event("order_import_active_worker_count", count=qthread_count)
            self._log_order_event(
                "order_import_active_timer_count",
                count=sum(
                    1
                    for t in (self._auto_capture_timer, self._heartbeat_timer)
                    if t is not None and t.isActive()
                ),
            )
        except Exception as exc:  # noqa: BLE001 - heartbeat自体でUIを落とさない
            self._log_slot_exception("order_import_unhandled_slot_exception", exc, source="heartbeat")

    def _stop_execute_monitor(self) -> None:
        """実行検知ワーカーを安全に停止する（close/hide/自動保存OFF時。要件2/7）。"""
        if self._execute_thread is None and self._execute_worker is None:
            return
        self._log_order_event("order_capture_worker_cleanup_started", worker="execute")
        # まず停止フラグを立て、in-flight tick を早期returnさせる。
        if self._execute_worker is not None:
            self._execute_worker.request_stop()
        for thread, worker in (
            (self._execute_thread, self._execute_worker),
        ):
            if thread is None:
                continue
            # タイマー停止はワーカー自身のスレッドで行う（別スレッドからの停止を避ける）。
            worker_busy = (
                bool(getattr(worker, "_busy", False))
                or bool(getattr(worker, "_rect_cache_refreshing", False))
                if worker is not None
                else False
            )
            if worker is not None and thread.isRunning() and not worker_busy:
                QMetaObject.invokeMethod(worker, "stop", Qt.ConnectionType.BlockingQueuedConnection)
            thread.quit()
            # 非Windowsでは自動処理は即時に返るため短い待機で十分。長時間は待たない。
            if thread.wait(1000):
                thread.deleteLater()
            else:
                _write_worker_debug_event("worker_stop_timeout", is_window_alive=not self._closing)
                _STOPPING_THREADS.append(thread)

                def _release_thread(ref=thread):
                    try:
                        _STOPPING_THREADS.remove(ref)
                    except ValueError:
                        pass
                    ref.deleteLater()

                thread.finished.connect(_release_thread)
        self._execute_thread = None
        self._execute_worker = None
        self._log_order_event("order_capture_worker_cleanup_finished", worker="execute")
        self._log_order_event("order_import_execute_monitor_stopped", reason="stop_workers")

    # ── ウィンドウ挙動 ────────────────────────────────────────────────────────
    def showEvent(self, event) -> None:  # noqa: N802
        try:
            super().showEvent(event)
            self._closing = False
            apply_windows_title_bar_theme(self, current_title_bar_is_dark())
            self._refresh_count()
            self._refresh_add_to_voucher_enabled()
            # 表示中は自動処理ワーカーを稼働させる（UIスレッドはブロックしない）。
            self._start_workers()
        except Exception as exc:  # noqa: BLE001
            self._log_slot_exception("order_import_unhandled_slot_exception", exc, source="showEvent")

    def hideEvent(self, event) -> None:  # noqa: N802
        try:
            # 世代番号を進め、非表示後に戻ってくる在庫 worker 結果を破棄させる（要件7）。
            self._generation += 1
            # 非表示中は自動処理を止める（表示中のみ稼働）。
            self._stop_workers()
        except Exception as exc:  # noqa: BLE001
            self._log_slot_exception("order_import_unhandled_slot_exception", exc, source="hideEvent")
        finally:
            try:
                super().hideEvent(event)
            except Exception as exc:  # noqa: BLE001
                self._log_slot_exception("order_import_unhandled_slot_exception", exc, source="hideEvent_super")

    def closeEvent(self, event) -> None:  # noqa: N802
        # 小画面を閉じてもアプリ全体や他画面は終了させない。起動元へ通知のみ行う。
        try:
            self._closing = True
            # 世代番号を進め、close後に戻ってくる在庫 worker 結果を破棄させる（要件7）。
            self._generation += 1
            # flush予約タイマーは止め、close時に確実に書き込む（二重書き込みを避ける）。
            self._flush_timer_scheduled = False
            self._stop_workers()
            self._log_order_event("order_import_close_flush_started")
            # 保留中の自動保存要求をメモリ上の保存リストへ反映してから書き込む（要件6）。
            self._drain_pending_auto_save_on_close()
            flushed = self._flush_saved_orders_now(reason="close", durable=True)
            if flushed:
                self._log_order_event("order_import_close_flush_finished")
            else:
                # 書き込み失敗。dirty は残り、次回起動時に読み直された内容へは反映されないが
                # ログには残す（毎回の警告ダイアログは邪魔になるため出さない）。
                self._log_order_event("order_import_close_flush_failed")
            self.closed.emit()
        except Exception as exc:  # noqa: BLE001
            self._log_slot_exception("order_import_unhandled_slot_exception", exc, source="closeEvent")
        finally:
            try:
                super().closeEvent(event)
            except Exception as exc:  # noqa: BLE001
                self._log_slot_exception("order_import_unhandled_slot_exception", exc, source="closeEvent_super")
