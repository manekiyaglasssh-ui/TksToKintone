"""OLAPキャッシュ（受注Noごと保存・保存期間・期限切れ削除）のテスト。"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path


class TestVoucherCache(unittest.TestCase):
    def test_sanitize_order_no_removes_invalid_chars(self) -> None:
        from app import voucher_cache

        self.assertEqual(voucher_cache.sanitize_order_no("52/18*86?9"), "52_18_86_9")
        self.assertEqual(voucher_cache.sanitize_order_no("  "), "_unknown_")

    def test_save_creates_per_order_file(self) -> None:
        from app import voucher_cache

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            path = voucher_cache.save_olap_cache(
                "5218869",
                raw_rows=[{"6": "5218869"}],
                pages=[{"order_no": "5218869"}],
                request_conditions={"order_no": "5218869"},
                row_settings={"finish_date": date(2026, 6, 11), "am_pm": "PM",
                              "process_checks": {"広幅": True},
                              "voucher_checks": {"03": True}},
                cache_dir=cache_dir,
            )
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "5218869.json")

    def test_saved_content_is_jsonable_and_keyed_by_order_no(self) -> None:
        from app import voucher_cache

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            voucher_cache.save_olap_cache(
                "5218869",
                pages=[{"order_no": "5218869"}],
                row_settings={"finish_date": date(2026, 6, 11)},
                cache_dir=cache_dir,
            )
            data = json.loads((cache_dir / "5218869.json").read_text(encoding="utf-8"))
            self.assertEqual(data["order_no"], "5218869")
            self.assertIn("fetched_at", data)
            # date は文字列化されている
            self.assertEqual(data["row_settings"]["finish_date"], "2026-06-11")

    def test_load_returns_none_when_missing(self) -> None:
        from app import voucher_cache

        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(voucher_cache.load_olap_cache("nope", cache_dir=Path(tmp)))

    def test_cleanup_removes_expired_only(self) -> None:
        from app import voucher_cache

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            now = time.time()
            # 10日前のキャッシュ（期限切れ）
            old = voucher_cache.save_olap_cache("old", cache_dir=cache_dir,
                                                now=now - 10 * 86400)
            # mtime を10日前へ
            import os
            os.utime(old, (now - 10 * 86400, now - 10 * 86400))
            # 直近のキャッシュ（残す）
            fresh = voucher_cache.save_olap_cache("fresh", cache_dir=cache_dir, now=now)

            deleted = voucher_cache.cleanup_expired_cache(7, cache_dir=cache_dir, now=now)
            self.assertEqual(deleted, 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
