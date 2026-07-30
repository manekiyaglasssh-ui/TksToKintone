from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


SECRET_WORDS = ("password", "パスワード", "api_token", "token", "トークン")


class GuiLogHandler(logging.Handler):
    def __init__(self, emit_text: Callable[[str], None]) -> None:
        super().__init__()
        self.emit_text = emit_text

    def emit(self, record: logging.LogRecord) -> None:
        self.emit_text(self.format(record))


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        lowered = message.lower()
        if any(word in lowered for word in SECRET_WORDS):
            record.msg = "[機密情報を含む可能性があるログを省略しました]"
            record.args = ()
        return True


def setup_logger(log_dir: Path, gui_callback: Callable[[str], None] | None = None) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"tks_to_kintone_{datetime.now():%Y%m%d_%H%M%S}.log"

    logger = logging.getLogger("tks_to_kintone_app")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    secret_filter = SecretFilter()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(secret_filter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(secret_filter)
    logger.addHandler(stream_handler)

    if gui_callback is not None:
        gui_handler = GuiLogHandler(gui_callback)
        gui_handler.setFormatter(formatter)
        gui_handler.addFilter(secret_filter)
        logger.addHandler(gui_handler)

    logger.info("event=application_log_ready log_path=%s", log_file.resolve())
    # Imported lazily so non-GUI helpers do not need to initialize Qt.
    try:
        from app.qt_message_logging import flush_pending_qt_messages

        flush_pending_qt_messages()
    except Exception as exc:  # pragma: no cover - diagnostics must never block startup.
        logger.warning("event=qt_message_buffer_flush_failed error_type=%s", type(exc).__name__)

    return logger, log_file


def cleanup_old_logs(log_dir: Path, retention_days: int) -> int:
    if retention_days < 1 or not log_dir.exists():
        return 0

    cutoff = time.time() - (retention_days * 24 * 60 * 60)
    deleted_count = 0
    for path in log_dir.glob("tks_to_kintone_*.log"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted_count += 1
        except OSError:
            continue
    return deleted_count
