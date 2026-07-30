from __future__ import annotations

import sys
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# 受注No取込のUIA/COM取得helperモード。frozen環境では本体exeを
# `TksToKintone.exe --tks-order-capture-helper` として呼び直すため、GUIを起動する前に
# ここで捕捉して helper を実行し即終了する（PySide等の重い初期化を避ける）。
_HELPER_FLAG = "--tks-order-capture-helper"
_POST_UPDATE_FLAG = "--post-update"


def _run_capture_helper_if_requested() -> int | None:
    if _HELPER_FLAG not in sys.argv:
        return None
    from app.tks_order_capture_helper import main as helper_main

    return helper_main(sys.argv[1:])


def main() -> int:
    helper_result = _run_capture_helper_if_requested()
    if helper_result is not None:
        return helper_result
    # Install before importing the GUI so Qt diagnostics emitted during module
    # loading are retained until the normal file logger becomes available.
    from app.qt_message_logging import install_qt_message_handler

    install_qt_message_handler()
    from app.gui import run_gui

    post_update = _POST_UPDATE_FLAG in sys.argv[1:]
    if post_update:
        sys.argv.remove(_POST_UPDATE_FLAG)
    return run_gui(post_update=post_update)


if __name__ == "__main__":
    raise SystemExit(main())
