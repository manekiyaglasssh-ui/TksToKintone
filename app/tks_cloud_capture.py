"""TKSCloud8「受注入力（見出）」画面から受注No欄の値を画面部品として直接読み取る。

OCRは使わない。取得は次の優先順位で行う:
  1. UI Automation で「受注No」ラベル付近の入力欄（Edit / ValuePattern）を特定して値を読む
  2. 取得できない場合は Win32 の子ウィンドウ列挙で値を持つコントロールを探す
  3. それでも取得できない場合は失敗理由付きで None を返す

設計方針:
- Windows 以外・対象ウィンドウ未検出・例外発生のいずれでも None を返し、アプリを落とさない。
- フォーカスを奪う処理は行わない（読み取りのみ。Ctrl+A / Ctrl+C 方式は使わない）。
- 受注No欄の特定は次の条件で行う:
    * 「受注No」ラベルの右側・同一行にある入力欄を最優先
    * 候補値が7桁程度の数字であること（「010」など短い営業所コードは採用しない）
    * 値が複数ある場合は受注No欄の画面位置に最も近いものを採用
- 診断（デバッグ時のみ）: 対象ウィンドウ・子ウィンドウ一覧・UIA要素一覧・権限情報を
  work/debug/order_capture_controls_YYYYMMDD_HHMMSS.json に保存する。
- 機密値（パスワード等）はログに出さない（TKSCloud8の対象画面にパスワード欄は無い想定）。
  受注Noの生値は診断に含めてよい。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.path_utils import get_comtypes_gen_dir, get_order_capture_debug_dir

_LOGGER = logging.getLogger("tks_to_kintone_app")

# TKSCloud8 の受注入力（見出）画面タイトル（部分一致で照合する）。
WINDOW_TITLE_KEYWORD = "受注入力（見出）"
# 受注No欄を特定するためのラベル文字列。
ORDER_NO_LABEL_KEYWORD = "受注No"
# TKSCloud8 本体・検証環境のプロセス名（前面判定・実行検知の対象）。
# タイトル完全一致だけに依存せず、前面ウィンドウのプロセス名でも対象を判定する。
TARGET_PROCESS_NAMES = ("TKSCloud8.exe", "TKSCloud8_KENSHO.exe")

# 同一行とみなす縦方向の許容ピクセル差（ラベルと入力欄の対応付けに使う）。
_SAME_ROW_TOLERANCE = 40
# 受注No候補として採用する最小桁数（「010」など営業所コードを除外する）。
_ORDER_NO_MIN_CANDIDATE_DIGITS = 7
# 受注Noの想定桁数（この桁数以上の候補を優先する）。
_ORDER_NO_PREFERRED_DIGITS = 7

# ── 失敗理由 ──────────────────────────────────────────────────────────────────
REASON_OK = "ok"
REASON_NOT_WINDOWS = "not_windows"
REASON_WINDOW_NOT_FOUND = "window_not_found"
REASON_FIELD_NOT_FOUND = "field_not_found"
REASON_NO_CANDIDATE = "no_candidate"
REASON_PRIVILEGE = "privilege"
REASON_UIA_SKIPPED = "uia_skipped"
REASON_ERROR = "error"

_REASON_MESSAGES = {
    REASON_NOT_WINDOWS: "WindowsでのみTKSCloud8から受注Noを取得できます",
    REASON_WINDOW_NOT_FOUND: "TKSCloud8の受注入力画面が見つかりません",
    REASON_FIELD_NOT_FOUND: "受注No欄を特定できません",
    REASON_NO_CANDIDATE: "受注No候補が見つかりません",
    REASON_PRIVILEGE: "権限差の可能性があります（TKSCloud8とTksToKintoneの権限を揃えてください）",
    REASON_ERROR: "受注Noの取得中にエラーが発生しました（診断JSONを確認してください）",
}

# 直近の取得失敗メッセージ（画面側の詳細表示に使う）。成功・非Windowsでは空にする。
_LAST_FAILURE_MESSAGE = ""


@dataclass
class _OrderFieldCache:
    kind: str
    window_hwnd: int
    control_hwnd: int
    window_rect: tuple[int, int, int, int]
    control_rect: tuple[int, int, int, int]
    window_name: str = ""
    uia_element: object | None = None
    failures: int = 0


@dataclass
class OrderReadAttempt:
    value: str | None
    used_fast_path: bool = False
    fast_path_failed: bool = False
    full_scan_used: bool = False
    cache_updated: bool = False
    cache_cleared: bool = False
    reason: str = ""


_ORDER_FIELD_CACHE: _OrderFieldCache | None = None
_CACHE_MAX_RECT_DELTA = 80
_CACHE_MAX_FAILURES = 2


def capture_failure_message(reason: str) -> str:
    """失敗理由キーから画面表示用の日本語メッセージを返す。"""
    return _REASON_MESSAGES.get(reason, "")


def get_last_capture_failure_message() -> str:
    """直近の取得で記録された失敗メッセージ（無ければ空文字）。"""
    return _LAST_FAILURE_MESSAGE


def _process_name_matches_target(name: object) -> bool:
    """プロセス名が TKSCloud8.exe / TKSCloud8_KENSHO.exe のいずれかか（大小無視）。"""
    if not name:
        return False
    lower = str(name).strip().lower()
    return lower in {n.lower() for n in TARGET_PROCESS_NAMES}


def get_foreground_window_info() -> dict:
    """前面ウィンドウのタイトル・PID・プロセス名を返す（診断・前面判定に使う）。

    Windows 以外・取得失敗・例外のいずれでも空値（title=""/pid=None/process_name=None）
    を返し、アプリを落とさない。フォーカスやキー入力は奪わない（読み取りのみ）。
    """
    info: dict = {"title": "", "pid": None, "process_name": None}
    if sys.platform != "win32":
        return info
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return info
        info["title"] = _win32_window_text(user32, hwnd, ctypes)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            info["pid"] = int(pid.value)
            name, _denied = _process_name(int(pid.value), ctypes, wintypes)
            info["process_name"] = name
    except Exception:  # noqa: BLE001 - 前面情報取得失敗でアプリを落とさない
        return info
    return info


def is_target_window_foreground() -> bool:
    """TKSCloud8「受注入力（見出）」画面が現在の前面ウィンドウかどうかを返す。

    判定はタイトル部分一致だけに依存しない。前面ウィンドウのプロセス名が
    TKSCloud8.exe / TKSCloud8_KENSHO.exe の場合も対象とする（タイトルが取れない
    権限差ケースでも実行検知できるようにするため）。

    Windows 以外・取得失敗・例外のいずれでも False を返し、アプリを落とさない。
    フォーカスやキー入力は奪わない（前面ウィンドウの情報を読み取るだけ）。
    """
    if sys.platform != "win32":
        return False
    try:
        info = get_foreground_window_info()
        if WINDOW_TITLE_KEYWORD in (info.get("title") or ""):
            return True
        return _process_name_matches_target(info.get("process_name"))
    except Exception:  # noqa: BLE001 - 前面判定失敗でアプリを落とさない
        return False


def is_tks_order_entry_window_running() -> bool:
    """TKSCloud8「受注入力（見出）」画面が存在するかを軽量に返す。

    自動取得の間隔調整用。UI Automation の深い走査は行わず、Win32 のトップ
    レベルウィンドウタイトルだけを見る。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]

        found = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if WINDOW_TITLE_KEYWORD in _win32_window_text(user32, hwnd, ctypes):
                found.append(True)
                return False
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        return bool(found)
    except Exception:  # noqa: BLE001 - 間隔調整に失敗しても通常間隔へ戻す
        return False


def is_tkscloud_window_running() -> bool:
    """TKSCloud8 / TKSCloud8_KENSHO のトップレベル画面が存在するかを軽量に返す。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]

        found = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _win32_window_text(user32, hwnd, ctypes)
            if WINDOW_TITLE_KEYWORD in title:
                found.append(True)
                return False
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                name, _denied = _process_name(int(pid.value), ctypes, wintypes)
                if _process_name_matches_target(name):
                    found.append(True)
                    return False
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        return bool(found)
    except Exception:  # noqa: BLE001
        return False


def read_tkscloud_window_rect() -> tuple[int, int, int, int] | None:
    """TKSCloud8 / TKSCloud8_KENSHO のトップレベル画面矩形を返す（受注入力画面に限定しない）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]

        found = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _win32_window_text(user32, hwnd, ctypes)
            if WINDOW_TITLE_KEYWORD in title:
                found.append(_win32_rect(user32, hwnd, ctypes, wintypes))
                return False
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                name, _denied = _process_name(int(pid.value), ctypes, wintypes)
                if _process_name_matches_target(name):
                    found.append(_win32_rect(user32, hwnd, ctypes, wintypes))
                    return False
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        return found[0] if found else None
    except Exception:  # noqa: BLE001
        return None


def read_tkscloud_window_title() -> str:
    """TKSCloud8 / TKSCloud8_KENSHO のトップレベル画面タイトルを返す。"""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]

        found = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _win32_window_text(user32, hwnd, ctypes)
            if WINDOW_TITLE_KEYWORD in title:
                found.append(title)
                return False
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                name, _denied = _process_name(int(pid.value), ctypes, wintypes)
                if _process_name_matches_target(name):
                    found.append(title)
                    return False
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        return found[0] if found else ""
    except Exception:  # noqa: BLE001
        return ""


@dataclass
class CaptureResult:
    value: str | None
    reason: str
    message: str = ""
    window_found: bool = False
    diagnostics: dict = field(default_factory=dict)


def read_order_no_from_tkscloud8(*, debug: bool = False) -> str | None:
    """TKSCloud8 の受注No欄の生値（正規化前）を返す。取得できなければ None。

    例外は内部で握りつぶし、失敗理由はログへ出す。
    """
    return read_order_no_attempt_from_tkscloud8(debug=debug).value


def read_order_no_attempt_from_tkscloud8(*, debug: bool = False) -> OrderReadAttempt:
    """TKSCloud8 の受注No欄を読み、fast path/full scan の利用状況も返す。"""
    before_cache = _ORDER_FIELD_CACHE
    try:
        # 既存テスト・外部利用のため _read_cached_order_no の差し替え互換を残す。
        if getattr(_read_cached_order_no, "__module__", __name__) != __name__:
            cached_value = _read_cached_order_no()
            if cached_value:
                return OrderReadAttempt(cached_value, used_fast_path=True, reason="ok")
        cached = _read_cached_order_no_attempt()
        if cached.value:
            _LOGGER.info("TKSCloud8 から受注No欄を取得しました（reason=fast_path）。")
            return cached
        result = capture_order_no(debug=debug)
    except Exception:  # noqa: BLE001 - 取得失敗でアプリを落とさない
        _LOGGER.warning("受注No取得処理で予期しない例外が発生しました。", exc_info=True)
        _set_last_failure_message(REASON_ERROR)
        return OrderReadAttempt(
            None,
            fast_path_failed=bool(before_cache),
            full_scan_used=True,
            cache_cleared=before_cache is not None and _ORDER_FIELD_CACHE is None,
            reason=REASON_ERROR,
        )
    if result.value:
        _LOGGER.info("TKSCloud8 から受注No欄を取得しました（reason=%s）。", result.reason)
    else:
        _LOGGER.info(
            "TKSCloud8 から受注Noを取得できませんでした（reason=%s）。", result.reason
        )
    return OrderReadAttempt(
        result.value,
        fast_path_failed=bool(before_cache) or cached.fast_path_failed,
        full_scan_used=True,
        cache_updated=result.value is not None and _ORDER_FIELD_CACHE is not None and _ORDER_FIELD_CACHE is not before_cache,
        cache_cleared=cached.cache_cleared or (before_cache is not None and _ORDER_FIELD_CACHE is None),
        reason=result.reason,
    )


def capture_order_no(*, debug: bool = False) -> CaptureResult:
    """受注No取得を実行し、値・失敗理由・診断を含む結果を返す。"""
    debug = _debug_enabled(debug)

    if sys.platform != "win32":
        _LOGGER.info(
            "TKSCloud8 受注No取得は Windows でのみ動作します（現在の環境: %s）。", sys.platform
        )
        result = CaptureResult(
            None, REASON_NOT_WINDOWS, _REASON_MESSAGES[REASON_NOT_WINDOWS], window_found=False
        )
        _store_message(result)
        return result

    diagnostics: dict = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "platform": sys.platform,
        "debug": debug,
        "window_title_keyword": WINDOW_TITLE_KEYWORD,
    }

    win = _safe_capture(_win32_capture, debug, "win32")
    uia = _safe_capture(_uia_capture, debug, "uia")
    diagnostics["win32"] = win.diagnostics
    diagnostics["uia"] = uia.diagnostics

    # 仕様の優先順位: UI Automation → Win32。
    value = uia.value or win.value
    window_found = bool(win.window_found or uia.window_found)

    privilege = win.diagnostics.get("privilege", {}) or {}
    access_denied = bool(privilege.get("access_denied")) and not privilege.get("self_is_admin")
    candidate_count = int(win.diagnostics.get("candidate_count", 0) or 0) + int(
        uia.diagnostics.get("candidate_count", 0) or 0
    )

    reason = _classify_result(
        value_found=bool(value),
        window_found=window_found,
        access_denied=access_denied,
        candidate_count=candidate_count,
    )
    diagnostics["result"] = {
        "value_found": bool(value),
        "reason": reason,
        "window_found": window_found,
        "candidate_count": candidate_count,
    }

    debug_path = write_capture_diagnostics(diagnostics, debug=debug)
    if debug_path is not None:
        _LOGGER.info("受注No取得の診断を書き出しました: %s", debug_path)

    result = CaptureResult(
        value=value,
        reason=reason,
        message=_REASON_MESSAGES.get(reason, ""),
        window_found=window_found,
        diagnostics=diagnostics,
    )
    _store_message(result)
    return result


def _classify_result(
    *, value_found: bool, window_found: bool, access_denied: bool, candidate_count: int
) -> str:
    if value_found:
        return REASON_OK
    if not window_found:
        return REASON_WINDOW_NOT_FOUND
    if access_denied:
        return REASON_PRIVILEGE
    if candidate_count > 0:
        return REASON_FIELD_NOT_FOUND
    return REASON_NO_CANDIDATE


def _debug_enabled(explicit: bool) -> bool:
    if explicit:
        return True
    return os.environ.get("TKS_ORDER_CAPTURE_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _store_message(result: CaptureResult) -> None:
    global _LAST_FAILURE_MESSAGE
    if result.reason in (REASON_OK, REASON_NOT_WINDOWS):
        _LAST_FAILURE_MESSAGE = ""
    else:
        _LAST_FAILURE_MESSAGE = result.message


def _rect_delta(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    return max(abs(int(x) - int(y)) for x, y in zip(a, b))


def _clear_order_field_cache() -> bool:
    global _ORDER_FIELD_CACHE
    had_cache = _ORDER_FIELD_CACHE is not None
    _ORDER_FIELD_CACHE = None
    return had_cache


def _cache_failure() -> None:
    global _ORDER_FIELD_CACHE
    if _ORDER_FIELD_CACHE is None:
        return
    _ORDER_FIELD_CACHE.failures += 1
    if _ORDER_FIELD_CACHE.failures >= _CACHE_MAX_FAILURES:
        _clear_order_field_cache()


def _read_cached_order_no() -> str | None:
    """前回特定済みの入力欄から値だけを読む fast path。"""
    return _read_cached_order_no_attempt().value


def _read_cached_order_no_attempt() -> OrderReadAttempt:
    """キャッシュ済みの受注No欄から値だけを読む fast path。"""
    cache = _ORDER_FIELD_CACHE
    if sys.platform != "win32" or cache is None:
        return OrderReadAttempt(None, reason="cache_missing")
    if cache.kind == "uia":
        return _read_cached_order_no_uia(cache)
    if cache.kind != "win32":
        return OrderReadAttempt(None, reason="cache_kind_unsupported")
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        _configure_user32(user32, ctypes, wintypes)
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        window_hwnd = wintypes.HWND(cache.window_hwnd)
        control_hwnd = wintypes.HWND(cache.control_hwnd)
        if not user32.IsWindow(window_hwnd) or not user32.IsWindow(control_hwnd):
            cleared = _clear_order_field_cache()
            return OrderReadAttempt(None, fast_path_failed=True, cache_cleared=cleared, reason="window_gone")
        if WINDOW_TITLE_KEYWORD not in _win32_window_text(user32, window_hwnd, ctypes):
            cleared = _clear_order_field_cache()
            return OrderReadAttempt(None, fast_path_failed=True, cache_cleared=cleared, reason="window_changed")
        current_window_rect = _win32_rect(user32, window_hwnd, ctypes, wintypes)
        if _rect_delta(cache.window_rect, current_window_rect) > _CACHE_MAX_RECT_DELTA:
            cleared = _clear_order_field_cache()
            return OrderReadAttempt(None, fast_path_failed=True, cache_cleared=cleared, reason="window_moved")
        value = _digits_value(_win32_control_text(user32, control_hwnd, ctypes))
        if value is None or len(value) < _ORDER_NO_MIN_CANDIDATE_DIGITS:
            _cache_failure()
            return OrderReadAttempt(None, fast_path_failed=True, reason="invalid_value")
        cache.failures = 0
        return OrderReadAttempt(value, used_fast_path=True, reason="ok")
    except Exception:  # noqa: BLE001 - fast path 失敗時は full scan へ戻す
        _cache_failure()
        return OrderReadAttempt(None, fast_path_failed=True, reason="exception")


def _read_cached_order_no_uia(cache: _OrderFieldCache) -> OrderReadAttempt:
    if cache.uia_element is None:
        cleared = _clear_order_field_cache()
        return OrderReadAttempt(None, fast_path_failed=True, cache_cleared=cleared, reason="element_missing")
    try:
        comtypes = _import_comtypes()
        if comtypes is None:
            return OrderReadAttempt(None, fast_path_failed=True, reason="comtypes_missing")
        _configure_comtypes_gen_dir(comtypes)
        co_initialized = _uia_co_initialize(comtypes)
        try:
            iuia, uia_module = _create_uia(comtypes)
            window = _uia_find_window(iuia, uia_module)
            if window is None:
                cleared = _clear_order_field_cache()
                return OrderReadAttempt(None, fast_path_failed=True, cache_cleared=cleared, reason="window_gone")
            window_name = _uia_safe_name(window)
            if cache.window_name and window_name != cache.window_name:
                cleared = _clear_order_field_cache()
                return OrderReadAttempt(None, fast_path_failed=True, cache_cleared=cleared, reason="window_changed")
            window_rect = _uia_rect(window)
            if _rect_delta(cache.window_rect, window_rect) > _CACHE_MAX_RECT_DELTA:
                cleared = _clear_order_field_cache()
                return OrderReadAttempt(None, fast_path_failed=True, cache_cleared=cleared, reason="window_moved")
            value, _has_value = _uia_value(cache.uia_element, uia_module)
            digits = _digits_value(value)
            if digits is None or len(digits) < _ORDER_NO_MIN_CANDIDATE_DIGITS:
                _cache_failure()
                return OrderReadAttempt(None, fast_path_failed=True, reason="invalid_value")
            cache.failures = 0
            return OrderReadAttempt(digits, used_fast_path=True, reason="ok")
        finally:
            if co_initialized:
                _uia_co_uninitialize(comtypes)
    except Exception:  # noqa: BLE001 - fast path 失敗時は full scan へ戻す
        _cache_failure()
        return OrderReadAttempt(None, fast_path_failed=True, reason="exception")


def _update_order_field_cache_from_win32(
    *,
    window_hwnd: int,
    window_rect: tuple[int, int, int, int],
    picked: tuple[dict, str, str] | None,
) -> None:
    global _ORDER_FIELD_CACHE
    if picked is None:
        _clear_order_field_cache()
        return
    control = picked[0]
    try:
        control_hwnd = int(control.get("hwnd") or 0)
    except Exception:  # noqa: BLE001
        control_hwnd = 0
    control_rect = _rect_tuple(control.get("rect"))
    if not control_hwnd or control_rect is None:
        _clear_order_field_cache()
        return
    _ORDER_FIELD_CACHE = _OrderFieldCache(
        kind="win32",
        window_hwnd=int(window_hwnd),
        control_hwnd=control_hwnd,
        window_rect=tuple(int(v) for v in window_rect),
        control_rect=control_rect,
    )


def _update_order_field_cache_from_uia(
    *,
    window_name: str,
    window_rect: tuple[int, int, int, int] | list[int] | None,
    picked: tuple[dict, str, str] | None,
) -> None:
    global _ORDER_FIELD_CACHE
    if picked is None:
        _clear_order_field_cache()
        return
    rect = _rect_tuple(window_rect)
    control_rect = _rect_tuple(picked[0].get("rect"))
    element = picked[0].get("_element")
    if rect is None or control_rect is None or element is None:
        _clear_order_field_cache()
        return
    _ORDER_FIELD_CACHE = _OrderFieldCache(
        kind="uia",
        window_hwnd=0,
        control_hwnd=0,
        window_rect=rect,
        control_rect=control_rect,
        window_name=window_name,
        uia_element=element,
    )


def _set_last_failure_message(reason: str) -> None:
    global _LAST_FAILURE_MESSAGE
    _LAST_FAILURE_MESSAGE = _REASON_MESSAGES.get(reason, "")


def _safe_capture(func, debug: bool, label: str) -> CaptureResult:
    try:
        return func(debug)
    except Exception:  # noqa: BLE001 - 片方の方式が失敗してももう片方を試す
        _LOGGER.warning("受注No取得に失敗しました（方式=%s）。", label, exc_info=True)
        return CaptureResult(None, REASON_ERROR, diagnostics={"error": "exception"})


def write_capture_diagnostics(diagnostics: dict, *, debug: bool) -> Path | None:
    """診断情報をJSONへ書き出す。debug=False なら何もせず None を返す。"""
    if not debug:
        return None
    try:
        debug_dir = get_order_capture_debug_dir()
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = debug_dir / f"order_capture_controls_{ts}.json"
        path.write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
    except Exception:  # noqa: BLE001 - 診断書き出し失敗で取得処理を止めない
        _LOGGER.warning("受注No取得の診断JSON書き出しに失敗しました。", exc_info=True)
        return None


# ── 値の選択ロジック（プラットフォーム非依存・テスト可能） ─────────────────────
def _digits_value(text: object) -> str | None:
    """コントロールの表示文字列を半角数字へ正規化する。数字のみでなければ None。"""
    if text is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(text)).strip()
    if not normalized or not normalized.isdigit():
        return None
    return normalized


def _digit_candidates(controls: list[dict]) -> list[tuple[dict, str]]:
    """受注No候補（数字のコントロール）を抽出する。短い営業所コードは除外。"""
    out: list[tuple[dict, str]] = []
    for c in controls:
        digits = _digits_value(c.get("text"))
        if digits is None:
            continue
        if len(digits) < _ORDER_NO_MIN_CANDIDATE_DIGITS:
            # 「010」などの短い営業所コードは受注Noとして採用しない。
            continue
        out.append((c, digits))
    return out


def _compact_label_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return "".join(ch for ch in text if ch.isalnum())


def _is_order_no_label_text(value: object) -> bool:
    compact = _compact_label_text(value)
    return bool(
        compact
        and "受注" in compact
        and ("no" in compact or "番号" in compact)
    )


def _order_no_labels(controls: list[dict]) -> list[dict]:
    return [
        c
        for c in controls
        if _is_order_no_label_text(c.get("text")) or _is_order_no_label_text(c.get("name"))
    ]


def _order_no_label(controls: list[dict]) -> dict | None:
    labels = _order_no_labels(controls)
    if not labels:
        return None
    return min(labels, key=lambda c: ((c.get("rect") or (0, 0, 0, 0))[1], (c.get("rect") or (0, 0, 0, 0))[0]))


def _rect_tuple(rect: object) -> tuple[int, int, int, int] | None:
    if not rect:
        return None
    try:
        left, top, right, bottom = (int(v) for v in rect)
    except Exception:  # noqa: BLE001
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _controls_bounds(controls: list[dict]) -> tuple[int, int, int, int] | None:
    rects = [_rect_tuple(c.get("rect")) for c in controls]
    rects = [r for r in rects if r is not None]
    if not rects:
        return None
    return (
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    )


def _target_order_no_region(
    controls: list[dict], window_rect: tuple[int, int, int, int] | list[int] | None = None
) -> tuple[int, int, int, int] | None:
    base = _rect_tuple(window_rect)
    if base is None:
        return None
    left, top, right, bottom = base
    width = right - left
    height = bottom - top
    return (
        left + int(width * 0.02),
        top + int(height * 0.04),
        left + int(width * 0.46),
        top + int(height * 0.34),
    )


def _rect_center(rect: object) -> tuple[int, int] | None:
    r = _rect_tuple(rect)
    if r is None:
        return None
    return ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)


def _point_in_region(point: tuple[int, int] | None, region: tuple[int, int, int, int] | None) -> bool:
    if point is None or region is None:
        return False
    x, y = point
    left, top, right, bottom = region
    return left <= x <= right and top <= y <= bottom


def _distance_from_region(rect: object, region: tuple[int, int, int, int] | None) -> int | None:
    center = _rect_center(rect)
    if center is None or region is None:
        return None
    x, y = center
    left, top, right, bottom = region
    dx = 0 if left <= x <= right else min(abs(x - left), abs(x - right))
    dy = 0 if top <= y <= bottom else min(abs(y - top), abs(y - bottom))
    return dx + dy


def _candidate_label_metrics(candidate: dict, label: dict | None) -> dict:
    rect = candidate.get("rect") or (0, 0, 0, 0)
    if label is None:
        return {
            "label_distance": None,
            "row_distance_from_order_label": None,
            "right_of_order_label": False,
            "same_row_as_order_label": False,
        }
    label_rect = label.get("rect") or (0, 0, 0, 0)
    try:
        cx, cy = int(rect[0]), int(rect[1])
        lx, ly = int(label_rect[0]), int(label_rect[1])
    except Exception:  # noqa: BLE001
        return {
            "label_distance": None,
            "row_distance_from_order_label": None,
            "right_of_order_label": False,
            "same_row_as_order_label": False,
        }
    row_distance = abs(cy - ly)
    label_distance = abs(cx - lx) + row_distance
    return {
        "label_distance": label_distance,
        "row_distance_from_order_label": row_distance,
        "right_of_order_label": cx >= lx,
        "same_row_as_order_label": row_distance < _SAME_ROW_TOLERANCE,
    }


def _candidate_diagnostics(
    controls: list[dict],
    candidates: list[tuple[dict, str]],
    *,
    window_rect: tuple[int, int, int, int] | list[int] | None = None,
    selected: tuple[dict, str] | None = None,
) -> list[dict]:
    label = _order_no_label(controls)
    region = _target_order_no_region(controls, window_rect)
    selected_control = selected[0] if selected is not None else None
    out: list[dict] = []
    for c, digits in candidates:
        metrics = _candidate_label_metrics(c, label)
        in_region = _point_in_region(_rect_center(c.get("rect")), region)
        reject_reason = ""
        if selected_control is c:
            reject_reason = ""
        elif label is not None and not (
            metrics["same_row_as_order_label"] and metrics["right_of_order_label"]
        ):
            reject_reason = "not_same_row_right_of_order_label"
        elif label is None and not in_region:
            reject_reason = "outside_expected_order_no_region"
        elif label is None and in_region:
            reject_reason = "not_selected_expected_region_candidate"
        else:
            reject_reason = "not_selected"
        out.append(
            {
                "value": digits,
                "candidate_value": digits,
                "normalized_value": digits,
                "is_ascii_digits": digits.isascii() and digits.isdigit(),
                "digit_length": len(digits),
                "rect": list(c.get("rect") or (0, 0, 0, 0)),
                "candidate_rect": list(c.get("rect") or (0, 0, 0, 0)),
                "control_type": c.get("control_type"),
                "candidate_control_type": c.get("control_type"),
                "class": c.get("class"),
                "name": c.get("name") or c.get("text") or "",
                "candidate_name": c.get("name") or c.get("text") or "",
                "distance_from_order_label": metrics["label_distance"],
                "distance_from_expected_region": _distance_from_region(c.get("rect"), region),
                "reject_reason": reject_reason,
                **metrics,
            }
        )
    return out


def _pick_order_no_candidate(
    controls: list[dict],
    window_rect: tuple[int, int, int, int] | list[int] | None = None,
) -> tuple[dict, str, str] | None:
    candidates = _digit_candidates(controls)
    if not candidates:
        return None

    label = _order_no_label(controls)
    if label is not None:
        lx, ly = label["rect"][0], label["rect"][1]
        same_row_right: list[tuple[dict, str]] = []
        for c, digits in candidates:
            cx, cy = c["rect"][0], c["rect"][1]
            if abs(cy - ly) < _SAME_ROW_TOLERANCE and cx >= lx:
                same_row_right.append((c, digits))
        if same_row_right:
            def score(item: tuple[dict, str]) -> tuple:
                c, digits = item
                cx, cy = c["rect"][0], c["rect"][1]
                return (abs(cy - ly), abs(cx - lx), 0 if len(digits) >= _ORDER_NO_PREFERRED_DIGITS else 1)

            best = min(same_row_right, key=score)
            return best[0], best[1], "order_label_same_row_right"

    region = _target_order_no_region(controls, window_rect)
    region_candidates = [
        (c, digits)
        for c, digits in candidates
        if _point_in_region(_rect_center(c.get("rect")), region)
    ]
    if not region_candidates:
        return None

    def region_score(item: tuple[dict, str]) -> tuple:
        c, digits = item
        rect = _rect_tuple(c.get("rect")) or (0, 0, 0, 0)
        return (
            _distance_from_region(c.get("rect"), region) or 0,
            rect[1],
            rect[0],
            0 if len(digits) >= _ORDER_NO_PREFERRED_DIGITS else 1,
        )

    best = min(region_candidates, key=region_score)
    return best[0], best[1], "expected_region_fallback"


def _pick_order_no_value(
    controls: list[dict],
    window_rect: tuple[int, int, int, int] | list[int] | None = None,
) -> str | None:
    """列挙したコントロール群から受注No欄の値を選ぶ。

    controls の各要素: {"class": str, "text": str, "rect": (left, top, right, bottom)}
    クラス名に依存せず、数字を持つコントロールを候補とする。
      1. 「受注No」ラベルの右側・同一行にある候補を最優先
      2. 7桁程度の数字を優先（短い営業所コードは候補から除外済み）
      3. 値が複数ある場合は受注No欄の画面位置（ラベル）に最も近いものを採用
    """
    picked = _pick_order_no_candidate(controls, window_rect)
    return picked[1] if picked is not None else None


# ── 「F12 実行」ボタンの矩形取得（実行操作のクリック判定に使う） ────────────────
# UIA診断では name="F12\n実行"（改行込み）・control_type=50000（Button）として見える。
def _is_execute_button_name(name: object) -> bool:
    """要素名が「F12 実行」ボタンを指すか（改行・空白・全角半角を無視して判定）。"""
    if not name:
        return False
    normalized = unicodedata.normalize("NFKC", str(name))
    compact = "".join(normalized.split())  # 改行・空白を除去して連結
    return ("F12" in compact) and ("実行" in compact)


def _pick_execute_button_rect(elements: list[dict]) -> tuple[int, int, int, int] | None:
    """UIA要素一覧から「F12 実行」ボタンの矩形 (left, top, right, bottom) を返す。

    見つからない・矩形が空（面積0）なら None。プラットフォーム非依存・テスト可能。
    """
    for e in elements:
        if not _is_execute_button_name(e.get("name")):
            continue
        rect = e.get("rect") or (0, 0, 0, 0)
        try:
            left, top, right, bottom = (int(v) for v in rect)
        except Exception:  # noqa: BLE001 - 矩形が壊れていても他要素を探す
            continue
        if right > left and bottom > top:
            return (left, top, right, bottom)
    return None


def read_execute_button_rect(*, debug: bool = False) -> tuple[int, int, int, int] | None:
    """TKSCloud8「受注入力（見出）」画面の「F12 実行」ボタンの画面矩形を返す。

    UI Automation で要素を列挙し、name が「F12 実行」のボタンの矩形を返す。
    Windows 以外・対象未検出・comtypes未導入・例外のいずれでも None（落とさない）。
    """
    if sys.platform != "win32":
        return None
    try:
        return _uia_execute_button_rect(debug=debug)
    except Exception:  # noqa: BLE001 - 取得失敗でアプリを落とさない
        _LOGGER.warning("「F12 実行」ボタン矩形の取得で例外が発生しました。", exc_info=True)
        return None


def _uia_execute_button_rect(*, debug: bool) -> tuple[int, int, int, int] | None:
    """UIA経由でTKSCloud8内の「F12 実行」ボタン矩形を取得する。取得不可なら None。"""
    comtypes = _import_comtypes()
    if comtypes is None:
        return None
    _configure_comtypes_gen_dir(comtypes)
    co_initialized = _uia_co_initialize(comtypes)
    try:
        iuia, uia_module = _create_uia(comtypes)
        for window in _uia_find_tkscloud_windows(iuia, uia_module):
            elements, _errors = _uia_extract_elements(iuia, uia_module, window)
            rect = _pick_execute_button_rect(elements)
            if rect is not None:
                return rect
        return None
    finally:
        if co_initialized:
            _uia_co_uninitialize(comtypes)


# ── UI Automation 経由 ────────────────────────────────────────────────────────
def _import_comtypes():
    """comtypes（と comtypes.client）を import して返す。未導入なら None。"""
    try:
        import comtypes  # noqa: F401
        import comtypes.client  # noqa: F401

        return comtypes
    except Exception:  # noqa: BLE001 - comtypes 未導入環境では UIA をスキップする
        return None


def _create_uia(comtypes):
    """UIAutomation の COM オブジェクトと型モジュールを生成して返す。"""
    uia_module = comtypes.client.GetModule("UIAutomationCore.dll")
    iuia = comtypes.client.CreateObject(
        "{ff48dba4-60ef-4201-aa87-54103eef594e}",  # CUIAutomation
        interface=uia_module.IUIAutomation,
    )
    return iuia, uia_module


def _uia_environment_diagnostics(comtypes) -> dict:
    """UIA例外解析用の環境情報（デバッグ時のみJSONへ出す）。"""
    info: dict = {
        "comtypes_imported": comtypes is not None,
        "platform": sys.platform,
        "python_executable": sys.executable,
        "sys_frozen": bool(getattr(sys, "frozen", False)),
        "sys_meipass": getattr(sys, "_MEIPASS", None),
    }
    if comtypes is not None:
        info["comtypes_version"] = getattr(comtypes, "__version__", None)
        info["comtypes_path"] = getattr(comtypes, "__file__", None)
        try:
            info["comtypes_client_gen_dir"] = getattr(comtypes.client, "gen_dir", None)
        except Exception:  # noqa: BLE001 - client 未import等でも環境情報収集を止めない
            info["comtypes_client_gen_dir"] = None
    return info


def _configure_comtypes_gen_dir(comtypes) -> str | None:
    """comtypes.gen の生成先を書き込み可能ディレクトリへ向ける。設定できたパスを返す。"""
    try:
        gen_dir = get_comtypes_gen_dir()
        gen_dir.mkdir(parents=True, exist_ok=True)
        comtypes.client.gen_dir = str(gen_dir)
        return str(gen_dir)
    except Exception:  # noqa: BLE001 - 書き込み不可ならメモリ生成へフォールバック
        try:
            comtypes.client.gen_dir = None
        except Exception:  # noqa: BLE001
            pass
        return None


def _uia_co_initialize(comtypes) -> bool:
    """UIA処理スレッドで COM を初期化する。解放が必要なら True を返す。

    既に初期化済み（S_FALSE / RPC_E_CHANGED_MODE）でも落とさず続行する。
    GUIスレッド・ワーカースレッドのどちらから呼んでも安全にする。
    """
    try:
        comtypes.CoInitialize()
        return True
    except Exception:  # noqa: BLE001 - 既に初期化済み等は握りつぶす
        return False


def _uia_co_uninitialize(comtypes) -> None:
    try:
        comtypes.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


def _uia_capture(debug: bool) -> CaptureResult:
    """UI Automation 経由で受注No欄を読む。例外は握りつぶし、診断へ原因を残す。"""
    diag: dict = {}
    stage = "import_comtypes"
    try:
        comtypes = _import_comtypes()
        diag["comtypes_imported"] = comtypes is not None
        if debug:
            diag.update(_uia_environment_diagnostics(comtypes))
        if comtypes is None:
            _LOGGER.info("UIA skipped: comtypes not installed")
            diag["skipped"] = "comtypes not installed"
            return CaptureResult(
                None, REASON_UIA_SKIPPED, window_found=False, diagnostics=diag
            )

        stage = "set_gen_dir"
        gen_dir = _configure_comtypes_gen_dir(comtypes)
        if debug:
            diag["comtypes_client_gen_dir"] = gen_dir

        stage = "co_initialize"
        co_initialized = _uia_co_initialize(comtypes)
        if debug:
            diag["co_initialized"] = co_initialized

        try:
            stage = "create_uia"
            iuia, uia_module = _create_uia(comtypes)

            stage = "find_window"
            window = _uia_find_window(iuia, uia_module)
            if window is None:
                diag["window_found"] = False
                return CaptureResult(
                    None, REASON_WINDOW_NOT_FOUND, window_found=False, diagnostics=diag
                )

            stage = "walk_elements"
            elements, extract_errors = _uia_extract_elements(iuia, uia_module, window)
            diag["extract_error_count"] = extract_errors

            stage = "extract_value"
            window_name = _uia_safe_name(window)
            window_rect = _uia_rect(window)

            stage = "select_candidate"
            return _build_uia_result(
                elements, debug, window_name=window_name, window_rect=window_rect, base_diag=diag
            )
        finally:
            if co_initialized:
                _uia_co_uninitialize(comtypes)
    except Exception as exc:  # noqa: BLE001 - UIA例外でアプリを落とさず原因を診断へ残す
        _LOGGER.warning("UIA経路で例外が発生しました（stage=%s）。", stage, exc_info=True)
        diag["error"] = "exception"
        diag["uia_stage"] = stage
        if debug:
            diag["exception_type"] = type(exc).__name__
            diag["exception_message"] = str(exc)
            diag["traceback"] = traceback.format_exc()
        return CaptureResult(
            None,
            REASON_ERROR,
            window_found=bool(diag.get("window_found", False)),
            diagnostics=diag,
        )


def _uia_elements_to_controls(elements: list[dict]) -> list[dict]:
    """UIA要素の記述（plain dict）を受注No選択用のコントロール一覧へ変換する。"""
    controls: list[dict] = []
    for e in elements:
        is_edit = bool(e.get("is_edit"))
        has_value_pattern = bool(e.get("has_value_pattern"))
        # Edit/ValuePattern を持つ要素は値、それ以外はラベル名を text とする。
        text = e.get("value", "") if (is_edit or has_value_pattern) else e.get("name", "")
        controls.append(
            {
                "_element": e.get("_element"),
                "class": "edit" if is_edit else "text",
                "name": e.get("name", ""),
                "control_type": e.get("control_type"),
                "text": text,
                "rect": tuple(e.get("rect", (0, 0, 0, 0))),
            }
        )
    return controls


def _build_uia_result(
    elements: list[dict],
    debug: bool,
    *,
    window_name: str = "",
    window_rect: tuple[int, int, int, int] | list[int] | None = None,
    base_diag: dict | None = None,
) -> CaptureResult:
    """UIA要素一覧から受注No値・診断・結果を組み立てる（COM非依存・テスト可能）。"""
    controls = _uia_elements_to_controls(elements)
    candidates = _digit_candidates(controls)
    picked = _pick_order_no_candidate(controls, window_rect)
    value = _pick_order_no_value(controls, window_rect)
    _update_order_field_cache_from_uia(
        window_name=window_name,
        window_rect=window_rect,
        picked=picked if value else None,
    )
    candidate_diag = _candidate_diagnostics(controls, candidates, window_rect=window_rect, selected=(picked[0], picked[1]) if picked is not None else None)
    diag = dict(base_diag or {})
    diag.update(
        {
            "window_found": True,
            "window_name": window_name,
            "window_title": window_name,
            "window_rect": list(window_rect) if window_rect else None,
            "candidate_count": len(candidates),
            "select_reason": _selection_reason(controls, value),
            "selection_reason": picked[2] if picked is not None else "no_order_label_candidate",
            "selected_candidate": (
                _candidate_diagnostics(controls, [(picked[0], picked[1])], window_rect=window_rect, selected=(picked[0], picked[1]))[0]
                if picked is not None
                else None
            ),
            "target_order_no_region": list(_target_order_no_region(controls, window_rect) or ()),
            "labels_detected": [
                {"name": c.get("name") or c.get("text") or "", "rect": list(c.get("rect") or ())}
                for c in controls
                if c.get("class") == "text"
            ],
            "order_label_candidates": [
                {"name": c.get("name") or c.get("text") or "", "rect": list(c.get("rect") or ())}
                for c in _order_no_labels(controls)
            ],
            "value_candidates": candidate_diag,
            "detected_candidates": candidate_diag,
            "rejected_candidates": [c for c in candidate_diag if c.get("reject_reason")],
        }
    )
    if debug:
        diag["elements"] = [_json_safe_uia_element(e) for e in elements]
        diag["candidate_values"] = [d for _, d in candidates]
    return CaptureResult(
        value,
        REASON_OK if value else REASON_FIELD_NOT_FOUND,
        window_found=True,
        diagnostics=diag,
    )


def _json_safe_uia_element(element: dict) -> dict:
    return {k: v for k, v in element.items() if k != "_element"}


def _selection_reason(controls: list[dict], value: str | None) -> str:
    """受注No候補の選定理由を人間可読な文字列で返す（診断用）。"""
    if not value:
        return "no_candidate"
    label = _order_no_label(controls)
    for c in controls:
        if _digits_value(c.get("text")) != value:
            continue
        cx, cy = c["rect"][0], c["rect"][1]
        if label is not None:
            lx, ly = label["rect"][0], label["rect"][1]
            if abs(cy - ly) < _SAME_ROW_TOLERANCE and cx >= lx:
                return f"label_neighbour(digits={len(value)})"
        return f"fallback_top_left(digits={len(value)})"
    return "selected"


def _uia_find_window(iuia, uia_module):
    for element in _uia_top_level_windows(iuia, uia_module):
        try:
            name = element.CurrentName or ""
        except Exception:  # noqa: BLE001
            name = ""
        if WINDOW_TITLE_KEYWORD in name:
            return element
    return None


def _uia_find_tkscloud_windows(iuia, uia_module) -> list:
    """受注入力画面に限定せず、TKSCloud8プロセスのトップレベル画面を返す。"""
    windows = []
    for element in _uia_top_level_windows(iuia, uia_module):
        name = _uia_safe_name(element)
        if WINDOW_TITLE_KEYWORD in name:
            windows.append(element)
            continue
        pid = _uia_process_id(element)
        if pid is None:
            continue
        try:
            import ctypes
            from ctypes import wintypes

            process_name, _denied = _process_name(pid, ctypes, wintypes)
        except Exception:  # noqa: BLE001
            process_name = None
        if _process_name_matches_target(process_name):
            windows.append(element)
    return windows


def _uia_top_level_windows(iuia, uia_module) -> list:
    root = iuia.GetRootElement()
    true_cond = iuia.CreateTrueCondition()
    children = root.FindAll(uia_module.TreeScope_Children, true_cond)
    out = []
    for i in range(children.Length):
        try:
            out.append(children.GetElement(i))
        except Exception:  # noqa: BLE001
            continue
    return out


def _uia_process_id(element) -> int | None:
    try:
        pid = int(element.CurrentProcessId)
    except Exception:  # noqa: BLE001
        return None
    return pid if pid > 0 else None


def _uia_extract_elements(iuia, uia_module, window) -> tuple[list[dict], int]:
    """対象ウィンドウ配下の全UIA要素を plain dict のリストとして取り出す。

    1要素の取得失敗（COM例外）で全体を止めず、失敗件数を返す。
    Name/Value/BoundingRectangle の個別失敗は各ヘルパ側で空値へフォールバックする。
    """
    true_cond = iuia.CreateTrueCondition()
    all_elements = window.FindAll(uia_module.TreeScope_Descendants, true_cond)
    edit_type_id = _uia_edit_type_id(uia_module)

    elements: list[dict] = []
    error_count = 0
    for i in range(all_elements.Length):
        try:
            element = all_elements.GetElement(i)
            control_type = _uia_control_type(element)
            value, has_value_pattern = _uia_value(element, uia_module)
            elements.append(
                {
                    "_element": element,
                    "name": _uia_safe_name(element),
                    "control_type": control_type,
                    "is_edit": control_type == edit_type_id,
                    "has_value_pattern": has_value_pattern,
                    "value": value,
                    "rect": list(_uia_rect(element)),
                }
            )
        except Exception:  # noqa: BLE001 - 1要素の取得失敗で全体を止めない
            error_count += 1
            continue
    return elements, error_count


def _uia_safe_name(element) -> str:
    try:
        return element.CurrentName or ""
    except Exception:  # noqa: BLE001
        return ""


def _uia_control_type(element) -> int:
    try:
        return int(element.CurrentControlType)
    except Exception:  # noqa: BLE001
        return 0


def _uia_edit_type_id(uia_module) -> int:
    try:
        return int(uia_module.UIA_EditControlTypeId)
    except Exception:  # noqa: BLE001
        return 50004  # Edit の既定 ControlType Id


def _uia_rect(element) -> tuple[int, int, int, int]:
    try:
        rect = element.CurrentBoundingRectangle
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:  # noqa: BLE001
        return (0, 0, 0, 0)


def _uia_value(element, uia_module) -> tuple[str, bool]:
    """ValuePattern の値と、ValuePattern を持つかどうかを返す。"""
    try:
        pattern = element.GetCurrentPattern(uia_module.UIA_ValuePatternId)
        if not pattern:
            return "", False
        value_pattern = pattern.QueryInterface(uia_module.IUIAutomationValuePattern)
        return (value_pattern.CurrentValue or ""), True
    except Exception:  # noqa: BLE001
        return "", False


# ── Win32 子ウィンドウ列挙経由 ────────────────────────────────────────────────
def _win32_capture(debug: bool) -> CaptureResult:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    _configure_user32(user32, ctypes, wintypes)

    hwnd = _win32_find_window(user32, ctypes, wintypes)
    if not hwnd:
        return CaptureResult(
            None, REASON_WINDOW_NOT_FOUND, window_found=False, diagnostics={"window_found": False}
        )

    diag: dict = {
        "window_found": True,
        "window_title": _win32_window_text(user32, hwnd, ctypes),
        "window_class": _win32_class_name(user32, hwnd, ctypes),
        "window_rect": list(_win32_rect(user32, hwnd, ctypes, wintypes)),
    }

    children = _win32_collect_controls(user32, hwnd, ctypes, wintypes)
    if debug:
        diag["children"] = children
    else:
        diag["children_count"] = len(children)

    diag["privilege"] = _privilege_info(user32, hwnd, ctypes, wintypes)

    controls = [
        {"hwnd": c.get("hwnd"), "class": c["class"], "text": c["text"], "rect": c["rect"]}
        for c in children
    ]
    candidates = _digit_candidates(controls)
    diag["candidate_count"] = len(candidates)
    picked = _pick_order_no_candidate(controls, diag["window_rect"])
    value = _pick_order_no_value(controls, diag["window_rect"])
    _update_order_field_cache_from_win32(
        window_hwnd=int(hwnd),
        window_rect=tuple(diag["window_rect"]),
        picked=picked if value else None,
    )
    candidate_diag = _candidate_diagnostics(
        controls,
        candidates,
        window_rect=diag["window_rect"],
        selected=(picked[0], picked[1]) if picked is not None else None,
    )
    diag["selection_reason"] = picked[2] if picked is not None else "no_order_label_candidate"
    diag["selected_hwnd"] = int(picked[0].get("hwnd") or 0) if picked is not None else None
    diag["selected_candidate"] = (
        _candidate_diagnostics(
            controls,
            [(picked[0], picked[1])],
            window_rect=diag["window_rect"],
            selected=(picked[0], picked[1]),
        )[0]
        if picked is not None
        else None
    )
    diag["target_order_no_region"] = list(_target_order_no_region(controls, diag["window_rect"]) or ())
    diag["labels_detected"] = [
        {"name": c.get("name") or c.get("text") or "", "rect": list(c.get("rect") or ())}
        for c in controls
        if c.get("class") != "edit"
    ]
    diag["order_label_candidates"] = [
        {"name": c.get("name") or c.get("text") or "", "rect": list(c.get("rect") or ())}
        for c in _order_no_labels(controls)
    ]
    diag["value_candidates"] = candidate_diag
    diag["detected_candidates"] = candidate_diag
    diag["rejected_candidates"] = [c for c in candidate_diag if c.get("reject_reason")]
    return CaptureResult(
        value,
        REASON_OK if value else REASON_FIELD_NOT_FOUND,
        window_found=True,
        diagnostics=diag,
    )


def _configure_user32(user32, ctypes, wintypes) -> None:
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        ctypes.c_void_p,
    ]
    user32.SendMessageW.restype = wintypes.LPARAM


def _win32_find_window(user32, ctypes, wintypes):
    found = []

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _win32_window_text(user32, hwnd, ctypes)
        if WINDOW_TITLE_KEYWORD in title:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return found[0] if found else None


def _win32_collect_controls(user32, parent_hwnd, ctypes, wintypes) -> list[dict]:
    controls: list[dict] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        controls.append(
            {
                "hwnd": int(hwnd),
                "class": _win32_class_name(user32, hwnd, ctypes),
                "text": _win32_control_text(user32, hwnd, ctypes),
                "rect": _win32_rect(user32, hwnd, ctypes, wintypes),
                "enabled": bool(user32.IsWindowEnabled(hwnd)),
                "visible": bool(user32.IsWindowVisible(hwnd)),
            }
        )
        return True

    user32.EnumChildWindows(parent_hwnd, enum_proc(callback), 0)
    return controls


def _win32_window_text(user32, hwnd, ctypes) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _win32_class_name(user32, hwnd, ctypes) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _win32_control_text(user32, hwnd, ctypes) -> str:
    # WM_GETTEXT は Edit に限らず、独自クラスでもプロセス跨ぎで読み取れることがある。
    wm_gettext = 0x000D
    wm_gettextlength = 0x000E
    length = int(user32.SendMessageW(hwnd, wm_gettextlength, 0, None))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.SendMessageW(hwnd, wm_gettext, length + 1, buf)
    return buf.value


def _win32_rect(user32, hwnd, ctypes, wintypes) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


# ── 権限差の確認 ──────────────────────────────────────────────────────────────
def _privilege_info(user32, hwnd, ctypes, wintypes) -> dict:
    info: dict = {
        "self_is_admin": None,
        "target_pid": None,
        "target_process_name": None,
        "access_denied": False,
    }
    try:
        info["self_is_admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        pass

    pid = wintypes.DWORD(0)
    try:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            info["target_pid"] = int(pid.value)
    except Exception:  # noqa: BLE001
        pass

    if info["target_pid"]:
        name, denied = _process_name(info["target_pid"], ctypes, wintypes)
        info["target_process_name"] = name
        info["access_denied"] = denied
    return info


def _process_name(pid: int, ctypes, wintypes) -> tuple[str | None, bool]:
    """プロセス名を取得する。アクセス拒否なら (None, True)。"""
    kernel32 = ctypes.windll.kernel32
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # 開けない場合は権限差の可能性が高い（TKSCloud8 が管理者起動など）。
        _LOGGER.info("TKSCloud8 プロセス(pid=%s)を開けません（権限差の可能性）。", pid)
        return None, True
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name, False
        return None, False
    finally:
        kernel32.CloseHandle(handle)
