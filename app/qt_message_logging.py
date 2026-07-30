from __future__ import annotations

import logging
import sys
import threading
from collections import deque

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

_LOGGER_NAME = "tks_to_kintone_app"
_PENDING: deque[tuple[str, str]] = deque(maxlen=200)
_LOCK = threading.RLock()
_LOCAL = threading.local()
_INSTALLED = False
_PREVIOUS_HANDLER = None

_TYPE_NAMES = {
    QtMsgType.QtDebugMsg: "debug",
    QtMsgType.QtInfoMsg: "info",
    QtMsgType.QtWarningMsg: "warning",
    QtMsgType.QtCriticalMsg: "critical",
    QtMsgType.QtFatalMsg: "fatal",
}


def _safe_message(message: str) -> str:
    """Do not persist Qt messages that look like application secrets."""
    lowered = message.lower()
    if any(word in lowered for word in ("api_token", "filekey", "x-cybozu-api-token")):
        return "[機密情報を含む可能性があるQtメッセージを省略しました]"
    # A bare 64-character hexadecimal value may be the update SHA-256 body.
    words = message.replace("=", " ").replace(":", " ").split()
    if any(len(word) == 64 and all(char in "0123456789abcdefABCDEF" for char in word) for word in words):
        return "[SHA-256本文を含む可能性があるQtメッセージを省略しました]"
    return message.replace("\r", " ").replace("\n", " ")


def _flush_logging() -> None:
    for logger in (logging.getLogger(), logging.getLogger(_LOGGER_NAME)):
        for handler in logger.handlers:
            try:
                handler.flush()
            except Exception:
                pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass


def _qt_message_handler(msg_type: QtMsgType, context: object, message: str) -> None:
    global _PREVIOUS_HANDLER
    if getattr(_LOCAL, "handling", False):
        if msg_type == QtMsgType.QtFatalMsg:
            _flush_logging()
        return
    _LOCAL.handling = True
    try:
        type_name = _TYPE_NAMES.get(msg_type, "unknown")
        safe = _safe_message(str(message))
        logger = logging.getLogger(_LOGGER_NAME)
        if logger.handlers:
            level = {
                "fatal": logging.CRITICAL,
                "critical": logging.CRITICAL,
                "warning": logging.WARNING,
                "info": logging.INFO,
                "debug": logging.DEBUG,
            }.get(type_name, logging.WARNING)
            logger.log(level, "event=qt_message type=%s message=%s", type_name, safe)
        else:
            with _LOCK:
                _PENDING.append((type_name, safe))
        if msg_type == QtMsgType.QtFatalMsg:
            _flush_logging()
        # Preserve any handler installed before ours. Qt performs its mandatory
        # fatal termination after the installed handler returns.
        if _PREVIOUS_HANDLER is not None:
            _PREVIOUS_HANDLER(msg_type, context, message)
    finally:
        _LOCAL.handling = False


def install_qt_message_handler() -> None:
    global _INSTALLED, _PREVIOUS_HANDLER
    with _LOCK:
        if _INSTALLED:
            return
        _PREVIOUS_HANDLER = qInstallMessageHandler(_qt_message_handler)
        _INSTALLED = True


def flush_pending_qt_messages() -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    with _LOCK:
        pending = list(_PENDING)
        _PENDING.clear()
    for type_name, message in pending:
        level = logging.CRITICAL if type_name in {"fatal", "critical"} else logging.WARNING
        logger.log(level, "event=qt_message type=%s message=%s buffered=true", type_name, message)
