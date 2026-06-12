from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.models import AppPaths


DEFAULT_CLEANUP_RETENTION_DAYS = 7
PROTECTED_WORK_FILENAMES = {
    "outputTksToKintone.csv",
    "kakou_extract.csv",
    "soba_extract.csv",
}


@dataclass(frozen=True)
class CleanupResult:
    target_count: int = 0
    deleted_count: int = 0
    failed_count: int = 0


def normalize_retention_days(value: object, default: int = DEFAULT_CLEANUP_RETENTION_DAYS) -> int:
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return days if days > 0 else default


def cleanup_old_files(
    paths: AppPaths,
    retention_days: int,
    logger: logging.Logger | None = None,
    now: float | None = None,
) -> CleanupResult:
    """Delete old generated files. Deletion failures are logged and ignored."""
    retention_days = normalize_retention_days(retention_days)
    cutoff = (time.time() if now is None else now) - (retention_days * 24 * 60 * 60)
    target_count = 0
    deleted_count = 0
    failed_count = 0

    for path in _candidate_files(paths):
        try:
            if not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            target_count += 1
            _unlink_file(path)
            deleted_count += 1
        except OSError as exc:
            failed_count += 1
            if logger is not None:
                logger.warning("古いファイル削除失敗: %s (%s)", path, exc)

    if logger is not None:
        logger.info("古いファイル削除: 対象=%s, 削除=%s, 失敗=%s", target_count, deleted_count, failed_count)

    return CleanupResult(target_count=target_count, deleted_count=deleted_count, failed_count=failed_count)


def _candidate_files(paths: AppPaths) -> Iterable[Path]:
    yield from _glob(paths.log_dir, ["*.log"])
    yield from _glob(paths.work_dir, ["input_*.csv", "outputTksToKintone_*.csv.bak"])
    yield from _glob(paths.work_dir / "debug", ["*.txt", "*.json"])
    yield from _glob(paths.kakou_master_backup_dir, ["*.bak", "*.csv.bak"])
    yield from _glob(paths.error_dir, ["*"])


def _glob(directory: Path, patterns: list[str]) -> Iterable[Path]:
    if not directory.exists():
        return
    for pattern in patterns:
        for path in directory.glob(pattern):
            if path.name in PROTECTED_WORK_FILENAMES:
                continue
            yield path


def _unlink_file(path: Path) -> None:
    path.unlink()
