from __future__ import annotations

import time
import unittest
from pathlib import Path

from app.logger import cleanup_old_logs, setup_logger


class LoggerCleanupTest(unittest.TestCase):
    def test_setup_logs_actual_application_log_path(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            logger, log_file = setup_logger(Path(temp_dir))
            for handler in logger.handlers:
                handler.flush()
            content = log_file.read_text(encoding="utf-8")
            self.assertIn("event=application_log_ready", content)
            self.assertIn(str(log_file.resolve()), content)

    def test_cleanup_old_logs_deletes_only_expired_app_logs(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            old_log = log_dir / "tks_to_kintone_20260101_000000.log"
            new_log = log_dir / "tks_to_kintone_20260521_000000.log"
            other_file = log_dir / "other.log"
            for path in (old_log, new_log, other_file):
                path.write_text("log", encoding="utf-8")

            now = time.time()
            old_mtime = now - (10 * 24 * 60 * 60)
            new_mtime = now - (1 * 24 * 60 * 60)
            old_log.touch()
            new_log.touch()
            other_file.touch()
            import os

            os.utime(old_log, (old_mtime, old_mtime))
            os.utime(new_log, (new_mtime, new_mtime))
            os.utime(other_file, (old_mtime, old_mtime))

            deleted = cleanup_old_logs(log_dir, retention_days=7)

            self.assertEqual(deleted, 1)
            self.assertFalse(old_log.exists())
            self.assertTrue(new_log.exists())
            self.assertTrue(other_file.exists())


if __name__ == "__main__":
    unittest.main()
