from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import load_app_config, update_values_in_config
from app.path_utils import (
    VOUCHER_OUTPUT_DIR_ENV_KEY,
    ensure_voucher_output_dir,
    get_default_voucher_output_dir,
    get_voucher_output_dir,
)


class ConfigLoadTest(unittest.TestCase):
    def test_first_launch_creates_samples_and_loads_without_restart(self) -> None:
        previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["TKS_TO_KINTONE_HOME"] = temp_dir
            try:
                config = load_app_config()
            finally:
                if previous_home is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous_home

            base_dir = Path(temp_dir)
            self.assertEqual(config.paths.base_dir, base_dir)
            self.assertTrue((base_dir / "config.env").exists())
            self.assertTrue((base_dir / "field_mapping.json").exists())
            self.assertEqual(config.tks_client_mode, "http")
            self.assertEqual(config.tks_base_url, "https://www.ap.tkscloud8.aga-sys.com")
            self.assertEqual(config.cleanup_retention_days, 7)
            self.assertTrue(config.tks_voucher_olap_disable_op_fields)

    def test_voucher_output_dir_defaults_to_programdata_work_dir_when_unset(self) -> None:
        previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["TKS_TO_KINTONE_HOME"] = temp_dir
            try:
                config = load_app_config()
                output_dir = get_voucher_output_dir(config)
            finally:
                if previous_home is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous_home

            self.assertEqual(output_dir, Path(temp_dir) / "work" / "voucher_output")

    def test_voucher_output_dir_config_value_is_preferred_and_reloaded(self) -> None:
        previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["TKS_TO_KINTONE_HOME"] = temp_dir
            try:
                config = load_app_config()
                selected = Path(temp_dir) / "selected_pdf"
                update_values_in_config(config.paths.config_env, {VOUCHER_OUTPUT_DIR_ENV_KEY: str(selected)})
                reloaded = load_app_config()
            finally:
                if previous_home is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous_home

            self.assertEqual(get_voucher_output_dir(reloaded), selected)

    def test_frozen_default_does_not_use_internal_base_dir(self) -> None:
        previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["TKS_TO_KINTONE_HOME"] = temp_dir
            try:
                with patch("sys.frozen", True, create=True), patch("sys._MEIPASS", str(Path(temp_dir) / "_internal"), create=True):
                    output_dir = get_default_voucher_output_dir(Path(temp_dir) / "_internal")
            finally:
                if previous_home is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous_home

            self.assertEqual(output_dir, Path(temp_dir) / "work" / "voucher_output")
            self.assertNotIn("_internal", str(output_dir))

    def test_ensure_voucher_output_dir_creates_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "missing" / "voucher_output"
            ensured = ensure_voucher_output_dir(output_dir)
            self.assertEqual(ensured, output_dir)
            self.assertTrue(output_dir.is_dir())

    def test_program_files_output_dir_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            ensure_voucher_output_dir(Path(r"C:\Program Files (x86)\Manekiya\TksToKintone_internal\work\voucher_output"))
        self.assertIn("Program Files", str(ctx.exception))

    def test_cleanup_retention_days_invalid_value_falls_back_to_7(self) -> None:
        previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["TKS_TO_KINTONE_HOME"] = temp_dir
            try:
                load_app_config()
                config_path = Path(temp_dir) / "config.env"
                text = config_path.read_text(encoding="utf-8")
                config_path.write_text(text.replace("CLEANUP_RETENTION_DAYS=7", "CLEANUP_RETENTION_DAYS=0"), encoding="utf-8")
                config = load_app_config()
            finally:
                if previous_home is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous_home

            self.assertEqual(config.cleanup_retention_days, 7)

    def test_voucher_olap_op_fields_can_be_enabled_in_config(self) -> None:
        previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["TKS_TO_KINTONE_HOME"] = temp_dir
            try:
                load_app_config()
                config_path = Path(temp_dir) / "config.env"
                text = config_path.read_text(encoding="utf-8")
                config_path.write_text(
                    text.replace(
                        "TKS_VOUCHER_OLAP_DISABLE_OP_FIELDS=1",
                        "TKS_VOUCHER_OLAP_DISABLE_OP_FIELDS=0",
                    )
                    + "\nTKS_VOUCHER_OLAP_ENABLED_OP_FIELDS=OP区分,商品コード\n",
                    encoding="utf-8",
                )
                config = load_app_config()
            finally:
                if previous_home is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous_home

            self.assertFalse(config.tks_voucher_olap_disable_op_fields)
            self.assertEqual(config.tks_voucher_olap_enabled_op_fields, ["OP区分", "商品コード"])


class ConfigEnvBootstrapTest(unittest.TestCase):
    """config.env 自動作成・追記・保護の動作確認。"""

    def _set_home(self, temp_dir: str) -> None:
        os.environ["TKS_TO_KINTONE_HOME"] = temp_dir

    def _restore_home(self, previous: str | None) -> None:
        if previous is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = previous

    def test_auto_created_when_absent(self) -> None:
        previous = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            self._set_home(temp_dir)
            try:
                config_path = Path(temp_dir) / "config.env"
                self.assertFalse(config_path.exists())
                load_app_config()
                self.assertTrue(config_path.exists())
            finally:
                self._restore_home(previous)

    def test_op_fields_present_in_created_config(self) -> None:
        previous = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            self._set_home(temp_dir)
            try:
                config = load_app_config()
            finally:
                self._restore_home(previous)
            self.assertEqual(config.tks_voucher_olap_enabled_op_fields, ["OP区分", "商品コード"])

    def test_existing_config_not_overwritten(self) -> None:
        previous = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            self._set_home(temp_dir)
            try:
                # 1回目: 自動作成
                load_app_config()
                config_path = Path(temp_dir) / "config.env"
                original_text = config_path.read_text(encoding="utf-8")
                sentinel = "# EXISTING_MARKER\n"
                config_path.write_text(original_text + sentinel, encoding="utf-8")
                # 2回目: 上書きされない
                load_app_config()
                result_text = config_path.read_text(encoding="utf-8")
            finally:
                self._restore_home(previous)
            self.assertIn(sentinel.strip(), result_text)

    def test_op_fields_appended_to_existing_config_without_it(self) -> None:
        previous = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            self._set_home(temp_dir)
            try:
                # 先にファイルを作成し、OP_FIELDS行を除去
                load_app_config()
                config_path = Path(temp_dir) / "config.env"
                text = config_path.read_text(encoding="utf-8")
                text_without = "\n".join(
                    line for line in text.splitlines()
                    if not (not line.strip().startswith("#") and line.strip().startswith("TKS_VOUCHER_OLAP_ENABLED_OP_FIELDS="))
                ) + "\n"
                config_path.write_text(text_without, encoding="utf-8")
                self.assertNotIn("TKS_VOUCHER_OLAP_ENABLED_OP_FIELDS=", text_without)
                # 再ロード: 追記される
                config = load_app_config()
                result_text = config_path.read_text(encoding="utf-8")
            finally:
                self._restore_home(previous)
            self.assertIn("TKS_VOUCHER_OLAP_ENABLED_OP_FIELDS=OP区分,商品コード", result_text)
            self.assertEqual(config.tks_voucher_olap_enabled_op_fields, ["OP区分", "商品コード"])

    def test_op_fields_not_duplicated_when_already_present(self) -> None:
        previous = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            self._set_home(temp_dir)
            try:
                load_app_config()
                load_app_config()
                config_path = Path(temp_dir) / "config.env"
                result_text = config_path.read_text(encoding="utf-8")
            finally:
                self._restore_home(previous)
            count = result_text.count("TKS_VOUCHER_OLAP_ENABLED_OP_FIELDS=")
            self.assertEqual(count, 1)

    def test_config_path_not_in_program_files_or_internal(self) -> None:
        previous = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            self._set_home(temp_dir)
            try:
                config = load_app_config()
            finally:
                self._restore_home(previous)
            config_str = str(config.paths.config_env).lower()
            self.assertNotIn("program files", config_str)
            self.assertNotIn("_internal", config_str)


class DebugVisibleConfigTest(unittest.TestCase):
    """NGS_DEBUG_VISIBLE の config.env 更新動作確認。"""

    def test_ngs_debug_visible_off_saved_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.env"
            update_values_in_config(config_path, {"NGS_DEBUG_VISIBLE": "0"})
            text = config_path.read_text(encoding="utf-8")
            self.assertIn("NGS_DEBUG_VISIBLE=0", text)

    def test_ngs_debug_visible_on_saved_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.env"
            update_values_in_config(config_path, {"NGS_DEBUG_VISIBLE": "1"})
            text = config_path.read_text(encoding="utf-8")
            self.assertIn("NGS_DEBUG_VISIBLE=1", text)

    def test_ngs_debug_visible_existing_line_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.env"
            config_path.write_text("NGS_DEBUG_VISIBLE=1\n", encoding="utf-8")
            update_values_in_config(config_path, {"NGS_DEBUG_VISIBLE": "0"})
            text = config_path.read_text(encoding="utf-8")
            self.assertIn("NGS_DEBUG_VISIBLE=0", text)
            self.assertNotIn("NGS_DEBUG_VISIBLE=1", text)

    def test_ngs_debug_visible_no_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.env"
            update_values_in_config(config_path, {"NGS_DEBUG_VISIBLE": "0"})
            update_values_in_config(config_path, {"NGS_DEBUG_VISIBLE": "1"})
            text = config_path.read_text(encoding="utf-8")
            self.assertEqual(text.count("NGS_DEBUG_VISIBLE="), 1)
            self.assertIn("NGS_DEBUG_VISIBLE=1", text)


if __name__ == "__main__":
    unittest.main()
