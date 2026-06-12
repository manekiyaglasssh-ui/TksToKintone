from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.cleanup_service import cleanup_old_files, normalize_retention_days
from app.models import AppPaths


class CleanupServiceTest(unittest.TestCase):
    def test_cleanup_deletes_only_old_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            paths = _paths(base)
            _mkdirs(paths)

            old_log = paths.log_dir / "old.log"
            new_log = paths.log_dir / "new.log"
            old_input = paths.work_dir / "input_20260101_000000.csv"
            fixed_output = paths.work_dir / "outputTksToKintone.csv"
            fixed_kakou = paths.work_dir / "kakou_extract.csv"
            fixed_soba = paths.work_dir / "soba_extract.csv"
            old_debug_txt = paths.work_dir / "debug" / "old.txt"
            old_debug_json = paths.work_dir / "debug" / "old.json"
            old_backup = paths.kakou_master_backup_dir / "kakou_master_20260101.csv.bak"
            old_error = paths.error_dir / "failed_20260101.csv"
            config = paths.config_env
            mapping = paths.field_mapping_json
            master = paths.kakou_master_csv

            files = [
                old_log,
                new_log,
                old_input,
                fixed_output,
                fixed_kakou,
                fixed_soba,
                old_debug_txt,
                old_debug_json,
                old_backup,
                old_error,
                config,
                mapping,
                master,
            ]
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")

            now = time.time()
            old = now - (8 * 24 * 60 * 60)
            recent = now - (1 * 24 * 60 * 60)
            for path in files:
                os.utime(path, (old, old))
            os.utime(new_log, (recent, recent))

            result = cleanup_old_files(paths, retention_days=7, now=now)

            self.assertEqual(result.failed_count, 0)
            self.assertFalse(old_log.exists())
            self.assertTrue(new_log.exists())
            self.assertFalse(old_input.exists())
            self.assertFalse(old_debug_txt.exists())
            self.assertFalse(old_debug_json.exists())
            self.assertFalse(old_backup.exists())
            self.assertFalse(old_error.exists())
            self.assertTrue(config.exists())
            self.assertTrue(mapping.exists())
            self.assertTrue(master.exists())
            self.assertTrue(fixed_output.exists())
            self.assertTrue(fixed_kakou.exists())
            self.assertTrue(fixed_soba.exists())

    def test_cleanup_failure_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            _mkdirs(paths)
            old_log = paths.log_dir / "old.log"
            old_log.write_text("x", encoding="utf-8")
            now = time.time()
            old = now - (8 * 24 * 60 * 60)
            os.utime(old_log, (old, old))

            with patch("app.cleanup_service._unlink_file", side_effect=OSError("locked")):
                result = cleanup_old_files(paths, retention_days=7, now=now)

            self.assertEqual(result.target_count, 1)
            self.assertEqual(result.deleted_count, 0)
            self.assertEqual(result.failed_count, 1)
            self.assertTrue(old_log.exists())

    def test_normalize_retention_days_falls_back_to_7(self) -> None:
        self.assertEqual(normalize_retention_days(""), 7)
        self.assertEqual(normalize_retention_days("abc"), 7)
        self.assertEqual(normalize_retention_days("0"), 7)
        self.assertEqual(normalize_retention_days("-1"), 7)
        self.assertEqual(normalize_retention_days("10"), 10)


def _paths(base: Path) -> AppPaths:
    return AppPaths(
        base_dir=base,
        config_env=base / "config.env",
        field_mapping_json=base / "field_mapping.json",
        work_dir=base / "work",
        log_dir=base / "logs",
        error_dir=base / "error",
        kakou_master_csv=base / "kakou_master.csv",
        kakou_master_backup_dir=base / "kakou_master_backup",
    )


def _mkdirs(paths: AppPaths) -> None:
    for directory in (
        paths.work_dir,
        paths.work_dir / "debug",
        paths.log_dir,
        paths.error_dir,
        paths.kakou_master_backup_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    unittest.main()
