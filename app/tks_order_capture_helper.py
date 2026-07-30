"""TKS受注No取込のUIA/COM取得を本体プロセスから切り離す短命helper。

背景:
    TKSCloud8 画面検出・受注No取得・画面種別判定は UI Automation / COM / Win32 を
    使うため、長時間・高頻度に本体プロセス内の QThread で回すとネイティブ層で
    クラッシュし、try/except では防げず本体プロセスごと落ちることがある。

方針:
    本体はこの helper を「1回の取得ごとに起動して終了する短命プロセス」として呼ぶ。
    helper が UIA/COM/Win32 の重い探索を担い、結果は JSON で stdout に返して終了する。
    helper がクラッシュしても本体は「取得不可」として継続できる。

戻り値(JSON)例:
    {"ok": true,  "screen_type": "header", "order_no": "1392348", "reason": "ok",             "elapsed_ms": 123}
    {"ok": true,  "screen_type": "detail", "order_no": "",        "reason": "detail_detected", "elapsed_ms": 88}
    {"ok": false, "screen_type": "none",   "order_no": "",        "reason": "window_not_found", "elapsed_ms": 50}
    {"ok": false, "screen_type": "unknown","order_no": "",        "reason": "exception: ...",   "elapsed_ms": 50}

重要:
    - COM 初期化/解放は helper 内で完結する（COM/UIA オブジェクトを外へ出さない）。
    - stdout へ出すのは JSON 1件のみ（debug の controls dump は通常 poll では出さない）。
    - helper プロセスは1回の取得ごとに終了する（常駐しない）。
    - 取得タイムアウトは呼び出し側(subprocess timeout)で必ず設定する。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# `python app/tks_order_capture_helper.py` のように直接起動された場合でも
# `import app...` が解決できるよう、プロジェクトルートを sys.path へ加える。
if __package__ in (None, ""):
    _THIS_DIR = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(_THIS_DIR)
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)


def _com_initialize() -> bool:
    """呼び出しスレッドで COM(STA) を初期化する。成功時 True（非Windows/未導入はFalse）。"""
    try:
        import comtypes
    except Exception:  # noqa: BLE001 - 非Windows/comtypes未導入ではCOMを使わない
        return False
    try:
        comtypes.CoInitialize()
        return True
    except Exception:  # noqa: BLE001
        return False


def _com_uninitialize() -> None:
    try:
        import comtypes
    except Exception:  # noqa: BLE001
        return
    try:
        comtypes.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


def _detect_capture(*, debug: bool = False) -> dict:
    """TKSCloud8 の受注入力画面を探し、画面種別と受注Noを判定して dict で返す。

    見出画面なら受注Noを取得し、明細画面なら order_no は空で detail を返す。
    例外は握りつぶし、screen_type=unknown / reason=exception:... で返す。
    """
    started = time.monotonic()
    ok = False
    screen_type = "unknown"
    order_no = ""
    reason = "unknown"
    com_initialized = False
    try:
        com_initialized = _com_initialize()
        from app import captured_orders
        from app.tks_cloud_capture import (
            is_tkscloud_window_running,
            read_order_no_from_tkscloud8,
            read_tkscloud_window_title,
        )

        title = read_tkscloud_window_title() or ""
        if "受注入力（明細）" in title:
            ok = True
            screen_type = "detail"
            order_no = ""
            reason = "detail_detected"
        elif "受注入力（見出）" in title:
            ok = True
            screen_type = "header"
            raw = read_order_no_from_tkscloud8(debug=debug)
            order_no = captured_orders.normalize_captured_order_no(raw) or ""
            reason = "ok" if order_no else "order_no_not_found"
        elif title:
            ok = True
            screen_type = "unknown"
            reason = "unknown_title"
        else:
            running = False
            try:
                running = bool(is_tkscloud_window_running())
            except Exception:  # noqa: BLE001
                running = False
            if running:
                ok = True
                screen_type = "unknown"
                reason = "tks_running_no_order_window"
            else:
                ok = False
                screen_type = "none"
                reason = "window_not_found"
    except Exception as exc:  # noqa: BLE001 - helper自身の失敗でも本体へJSONを返す
        ok = False
        screen_type = "unknown"
        order_no = ""
        reason = f"exception: {type(exc).__name__}: {exc}"
    finally:
        if com_initialized:
            _com_uninitialize()
    elapsed_ms = int(round((time.monotonic() - started) * 1000))
    return {
        "ok": bool(ok),
        "screen_type": str(screen_type or "unknown"),
        "order_no": str(order_no or ""),
        "reason": str(reason or ""),
        "elapsed_ms": elapsed_ms,
    }


def run_capture(*, debug: bool = False) -> dict:
    """1回分の取得を実行して結果 dict を返す（例外でも必ず dict を返す）。"""
    started = time.monotonic()
    try:
        return _detect_capture(debug=debug)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "screen_type": "unknown",
            "order_no": "",
            "reason": f"exception: {type(exc).__name__}: {exc}",
            "elapsed_ms": int(round((time.monotonic() - started) * 1000)),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--debug", action="store_true")
    # 本体exeがhelperモードで自身を呼ぶための識別フラグ（frozen環境）。ここでは無視する。
    parser.add_argument("--tks-order-capture-helper", action="store_true")
    try:
        args, _unknown = parser.parse_known_args(argv)
        debug = bool(args.debug)
    except SystemExit:
        debug = False
    result = run_capture(debug=debug)
    try:
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
