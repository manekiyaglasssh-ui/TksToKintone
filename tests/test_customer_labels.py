"""得意先ヘッダー表示名設定機能のテスト — Qt 不要の純粋ロジック検証。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.config import (
    CUSTOMER_LABEL_DEFAULTS,
    CUSTOMER_LABEL_MAX_LEN,
    load_app_config,
    update_customer_labels_in_config,
    validate_customer_label,
)
from app.kakou_master import CUSTOMER_KEYS, KAKOU_MASTER_HEADERS, load_master, save_master


class ValidateCustomerLabelTest(unittest.TestCase):
    def test_valid_label_returns_none(self) -> None:
        self.assertIsNone(validate_customer_label("吉田硝子"))

    def test_empty_label_returns_none(self) -> None:
        self.assertIsNone(validate_customer_label(""))

    def test_exactly_max_length_returns_none(self) -> None:
        self.assertIsNone(validate_customer_label("あ" * CUSTOMER_LABEL_MAX_LEN))

    def test_over_max_length_returns_error(self) -> None:
        result = validate_customer_label("あ" * (CUSTOMER_LABEL_MAX_LEN + 1))
        self.assertIsNotNone(result)
        self.assertIn(str(CUSTOMER_LABEL_MAX_LEN), result)

    def test_internal_keys_are_unchanged(self) -> None:
        """内部キーは 得意先1〜4 のまま変わらない。"""
        self.assertIn("得意先1", CUSTOMER_LABEL_DEFAULTS)
        self.assertIn("得意先2", CUSTOMER_LABEL_DEFAULTS)
        self.assertIn("得意先3", CUSTOMER_LABEL_DEFAULTS)
        self.assertIn("得意先4", CUSTOMER_LABEL_DEFAULTS)


class UpdateCustomerLabelsNewFileTest(unittest.TestCase):
    """config.env に CUSTOMER_LABEL_N が存在しない場合のテスト。"""

    def test_keys_are_appended_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            path.write_text("TKS_COMPANY_CODE=TEST\n", encoding="utf-8")
            labels = {
                "得意先1": "吉田硝子",
                "得意先2": "標準A",
                "得意先3": "特注",
                "得意先4": "予備",
            }
            update_customer_labels_in_config(path, labels)
            text = path.read_text(encoding="utf-8")
            self.assertIn("CUSTOMER_LABEL_1=吉田硝子", text)
            self.assertIn("CUSTOMER_LABEL_2=標準A", text)
            self.assertIn("CUSTOMER_LABEL_3=特注", text)
            self.assertIn("CUSTOMER_LABEL_4=予備", text)

    def test_existing_keys_are_preserved(self) -> None:
        """他の設定値は消えない。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            path.write_text(
                "TKS_COMPANY_CODE=TEST\n"
                "KINTONE_DOMAIN=example.cybozu.com\n"
                "KINTONE_APP_ID=123\n",
                encoding="utf-8",
            )
            update_customer_labels_in_config(path, {"得意先1": "A"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("TKS_COMPANY_CODE=TEST", text)
            self.assertIn("KINTONE_DOMAIN=example.cybozu.com", text)
            self.assertIn("KINTONE_APP_ID=123", text)

    def test_non_existent_file_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            self.assertFalse(path.exists())
            update_customer_labels_in_config(path, {"得意先1": "新規"})
            self.assertTrue(path.exists())
            self.assertIn("CUSTOMER_LABEL_1=新規", path.read_text(encoding="utf-8"))


class UpdateCustomerLabelsExistingKeysTest(unittest.TestCase):
    """config.env に CUSTOMER_LABEL_N が既に存在する場合のテスト。"""

    def test_existing_value_is_updated_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            path.write_text(
                "TKS_COMPANY_CODE=TEST\n"
                "CUSTOMER_LABEL_1=得意先1\n"
                "CUSTOMER_LABEL_2=得意先2\n"
                "CUSTOMER_LABEL_3=得意先3\n"
                "CUSTOMER_LABEL_4=得意先4\n",
                encoding="utf-8",
            )
            update_customer_labels_in_config(
                path,
                {"得意先1": "吉田硝子", "得意先2": "標準A", "得意先3": "特注", "得意先4": "予備"},
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("CUSTOMER_LABEL_1=吉田硝子", text)
            self.assertIn("CUSTOMER_LABEL_2=標準A", text)
            self.assertIn("CUSTOMER_LABEL_3=特注", text)
            self.assertIn("CUSTOMER_LABEL_4=予備", text)
            # 古い値は残らない
            self.assertNotIn("CUSTOMER_LABEL_1=得意先1\n", text)

    def test_other_keys_not_touched_when_updating(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            path.write_text(
                "TKS_COMPANY_CODE=MYCODE\n"
                "CUSTOMER_LABEL_1=旧ラベル\n"
                "KINTONE_API_TOKEN=secret\n",
                encoding="utf-8",
            )
            update_customer_labels_in_config(path, {"得意先1": "新ラベル"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("TKS_COMPANY_CODE=MYCODE", text)
            self.assertIn("KINTONE_API_TOKEN=secret", text)
            self.assertIn("CUSTOMER_LABEL_1=新ラベル", text)

    def test_duplicate_keys_not_created(self) -> None:
        """既存キーを更新しても同名キーが重複しない。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            path.write_text("CUSTOMER_LABEL_1=旧\n", encoding="utf-8")
            update_customer_labels_in_config(path, {"得意先1": "新"})
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("CUSTOMER_LABEL_1="), 1)

    def test_commented_lines_not_treated_as_existing(self) -> None:
        """コメント行は既存キーとみなさず末尾に追記される。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            path.write_text(
                "# CUSTOMER_LABEL_1=コメント\n"
                "TKS_COMPANY_CODE=TEST\n",
                encoding="utf-8",
            )
            update_customer_labels_in_config(path, {"得意先1": "A"})
            text = path.read_text(encoding="utf-8")
            # コメント行はそのまま残る
            self.assertIn("# CUSTOMER_LABEL_1=コメント", text)
            # 新しいキーが追記される
            self.assertIn("CUSTOMER_LABEL_1=A", text)


class UpdateCustomerLabelsDefaultsTest(unittest.TestCase):
    """空欄・デフォルト値のテスト。"""

    def test_empty_label_falls_back_to_default(self) -> None:
        """空欄のラベルはデフォルト値 得意先N に戻る。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            path.write_text("", encoding="utf-8")
            update_customer_labels_in_config(path, {"得意先1": "", "得意先2": "B"})
            text = path.read_text(encoding="utf-8")
            # 空欄 → デフォルト
            self.assertIn("CUSTOMER_LABEL_1=得意先1", text)
            # 非空欄 → そのまま
            self.assertIn("CUSTOMER_LABEL_2=B", text)

    def test_missing_label_key_uses_default(self) -> None:
        """ラベル辞書に存在しないキーはデフォルト値を使う。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            path.write_text("", encoding="utf-8")
            # 得意先3、得意先4 を渡さない
            update_customer_labels_in_config(path, {"得意先1": "A", "得意先2": "B"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("CUSTOMER_LABEL_3=得意先3", text)
            self.assertIn("CUSTOMER_LABEL_4=得意先4", text)


class CustomerLabelMaxLenTest(unittest.TestCase):
    """文字数制限のテスト。"""

    def test_max_len_is_20(self) -> None:
        self.assertEqual(CUSTOMER_LABEL_MAX_LEN, 20)

    def test_20_char_label_is_valid(self) -> None:
        label = "あ" * 20
        self.assertIsNone(validate_customer_label(label))

    def test_21_char_label_is_invalid(self) -> None:
        label = "あ" * 21
        self.assertIsNotNone(validate_customer_label(label))

    def test_error_message_mentions_max_length(self) -> None:
        error = validate_customer_label("x" * 25)
        self.assertIsNotNone(error)
        self.assertIn("20", error)


class CustomerLabelInternalKeyTest(unittest.TestCase):
    """内部キーが変わらないことを確認するテスト。"""

    def test_internal_keys_in_written_file(self) -> None:
        """config.env に書かれる環境変数名は CUSTOMER_LABEL_N 形式のまま。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            update_customer_labels_in_config(
                path,
                {"得意先1": "A", "得意先2": "B", "得意先3": "C", "得意先4": "D"},
            )
            text = path.read_text(encoding="utf-8")
            # 環境変数名は CUSTOMER_LABEL_N のまま
            self.assertIn("CUSTOMER_LABEL_1=", text)
            self.assertIn("CUSTOMER_LABEL_2=", text)
            self.assertIn("CUSTOMER_LABEL_3=", text)
            self.assertIn("CUSTOMER_LABEL_4=", text)
            # 内部キー名 得意先1 等は env ファイルに書かれない
            self.assertNotIn("得意先1=", text)
            self.assertNotIn("得意先2=", text)

    def test_default_dict_keys_are_internal_keys(self) -> None:
        """CUSTOMER_LABEL_DEFAULTS の辞書キーは内部キー名。"""
        self.assertEqual(set(CUSTOMER_LABEL_DEFAULTS.keys()), {"得意先1", "得意先2", "得意先3", "得意先4"})

    def test_customer_keys_constant_unchanged(self) -> None:
        """kakou_master.CUSTOMER_KEYS は 得意先1〜4 のまま変わらない。"""
        self.assertEqual(CUSTOMER_KEYS, ["得意先1", "得意先2", "得意先3", "得意先4"])

    def test_kakou_master_headers_use_internal_keys(self) -> None:
        """KAKOU_MASTER_HEADERS の得意先列は内部キー名のまま。"""
        for key in ("得意先1", "得意先2", "得意先3", "得意先4"):
            self.assertIn(key, KAKOU_MASTER_HEADERS)


# ── 確認点1: 登録前確認画面の得意先選択プルダウンへの反映 ────────────────────

class CustomerLabelReloadTest(unittest.TestCase):
    """保存後に load_app_config() を呼ぶと customer_labels が更新される。

    MainWindow.open_customer_label_settings は保存後に load_app_config() を呼ぶため、
    次回 RegistrationPreviewDialog を開く際には最新の customer_labels が渡される。
    """

    def _with_temp_home(self, fn: object) -> None:
        prev = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as d:
            os.environ["TKS_TO_KINTONE_HOME"] = d
            try:
                fn(Path(d))
            finally:
                if prev is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = prev

    def test_customer_labels_reflected_after_save_and_reload(self) -> None:
        """update + load_app_config() で customer_labels が更新される。"""
        def run(base: Path) -> None:
            cfg = load_app_config()
            # 初回起動時の得意先1既定値は「東芝・日立・フジテック」。
            self.assertEqual(cfg.customer_labels["得意先1"], "東芝・日立・フジテック")

            new_labels = {
                "得意先1": "吉田硝子",
                "得意先2": "標準A",
                "得意先3": "特注",
                "得意先4": "予備",
            }
            update_customer_labels_in_config(cfg.paths.config_env, new_labels)
            cfg2 = load_app_config()
            self.assertEqual(cfg2.customer_labels["得意先1"], "吉田硝子")
            self.assertEqual(cfg2.customer_labels["得意先2"], "標準A")
            self.assertEqual(cfg2.customer_labels["得意先3"], "特注")
            self.assertEqual(cfg2.customer_labels["得意先4"], "予備")

        self._with_temp_home(run)

    def test_customer_match_patterns_saved_and_reloaded(self) -> None:
        """得意先判定文字列も config.env に保存され、load_app_config で復元される。"""
        def run(base: Path) -> None:
            cfg = load_app_config()
            update_customer_labels_in_config(
                cfg.paths.config_env,
                {"得意先1": "吉田硝子", "得意先2": "標準A"},
                {"得意先1": "東芝,日立", "得意先2": "フジテック"},
            )
            cfg2 = load_app_config()
            self.assertEqual(cfg2.customer_match_patterns["得意先1"], "東芝,日立")
            self.assertEqual(cfg2.customer_match_patterns["得意先2"], "フジテック")
            self.assertEqual(cfg2.customer_match_patterns["得意先3"], "")

        self._with_temp_home(run)

    def test_customer_labels_kakou_options_uses_labels(self) -> None:
        """RegistrationPreviewDialog が受け取る customer_labels で _kakou_options が作られる。

        RegistrationPreviewDialog.__init__ の実装:
            for key in CUSTOMER_KEYS:
                self._kakou_options.append((key, customer_labels.get(key, key)))
        これにより得意先選択プルダウンの表示名が customer_labels に従う。
        """
        labels = {"得意先1": "吉田硝子", "得意先2": "標準A", "得意先3": "特注", "得意先4": "予備"}
        # _kakou_options の構築ロジックをここで検証
        kakou_options = [("selected", "選択なし")]
        for key in CUSTOMER_KEYS:
            kakou_options.append((key, labels.get(key, key)))
        # 内部キーは 得意先1〜4 のまま（第1要素）
        self.assertEqual(kakou_options[1][0], "得意先1")
        self.assertEqual(kakou_options[2][0], "得意先2")
        # 表示名は labels の値（第2要素）
        self.assertEqual(kakou_options[1][1], "吉田硝子")
        self.assertEqual(kakou_options[2][1], "標準A")
        self.assertEqual(kakou_options[3][1], "特注")
        self.assertEqual(kakou_options[4][1], "予備")


class CustomerAutoMatchTest(unittest.TestCase):
    def test_keyword_match_selects_customer_key(self) -> None:
        from app.gui import customer_key_from_name

        patterns = {"得意先1": "東芝,日立,フジテック", "得意先2": ""}
        self.assertEqual(customer_key_from_name("東芝エレベータ株式会社", patterns), "得意先1")

    def test_younger_customer_key_wins_when_keywords_overlap(self) -> None:
        from app.gui import customer_key_from_name

        patterns = {"得意先1": "東芝", "得意先2": "東芝", "得意先3": "東芝"}
        self.assertEqual(customer_key_from_name("東芝エレベータ株式会社", patterns), "得意先1")

    def test_empty_keywords_ignored_and_no_match_is_selected(self) -> None:
        from app.gui import DEFAULT_CUSTOMER_KEY, customer_key_from_name

        self.assertEqual(customer_key_from_name("東芝エレベータ株式会社", {"得意先1": ""}), DEFAULT_CUSTOMER_KEY)
        self.assertEqual(customer_key_from_name("一致なし", {"得意先1": "東芝"}), DEFAULT_CUSTOMER_KEY)

    def test_full_width_comma_and_spaces_split_keywords(self) -> None:
        from app.gui import customer_key_from_name

        patterns = {"得意先1": "東芝、 日立　フジテック"}
        self.assertEqual(customer_key_from_name("日立ビルシステム", patterns), "得意先1")


# ── 確認点2: config.env が存在しない場合 ─────────────────────────────────────

class CustomerLabelNoConfigTest(unittest.TestCase):
    """config.env が存在しない場合に CUSTOMER_LABEL_1〜4 を含むファイルを作成できる。"""

    def test_creates_file_with_all_four_keys(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            self.assertFalse(path.exists())
            update_customer_labels_in_config(
                path,
                {"得意先1": "吉田硝子", "得意先2": "標準A", "得意先3": "特注", "得意先4": "予備"},
            )
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn("CUSTOMER_LABEL_1=吉田硝子", text)
            self.assertIn("CUSTOMER_LABEL_2=標準A", text)
            self.assertIn("CUSTOMER_LABEL_3=特注", text)
            self.assertIn("CUSTOMER_LABEL_4=予備", text)


# ── 確認点3: コメントアウトされた行がある場合 ────────────────────────────────

class CustomerLabelCommentedLineTest(unittest.TestCase):
    """既存のコメント行を残したままアクティブな CUSTOMER_LABEL_N を追記する。"""

    def test_commented_lines_kept_active_lines_appended(self) -> None:
        """コメント行は保持され、アクティブなキーが別途追記される。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            path.write_text(
                "# CUSTOMER_LABEL_1=得意先1\n"
                "# CUSTOMER_LABEL_2=得意先2\n"
                "# CUSTOMER_LABEL_3=得意先3\n"
                "# CUSTOMER_LABEL_4=得意先4\n"
                "TKS_COMPANY_CODE=TEST\n",
                encoding="utf-8",
            )
            update_customer_labels_in_config(
                path,
                {"得意先1": "吉田硝子", "得意先2": "標準A", "得意先3": "特注", "得意先4": "予備"},
            )
            text = path.read_text(encoding="utf-8")
            # コメント行が残っている
            self.assertIn("# CUSTOMER_LABEL_1=得意先1", text)
            self.assertIn("# CUSTOMER_LABEL_2=得意先2", text)
            self.assertIn("# CUSTOMER_LABEL_3=得意先3", text)
            self.assertIn("# CUSTOMER_LABEL_4=得意先4", text)
            # アクティブなキーが追記されている
            self.assertIn("CUSTOMER_LABEL_1=吉田硝子", text)
            self.assertIn("CUSTOMER_LABEL_2=標準A", text)
            self.assertIn("CUSTOMER_LABEL_3=特注", text)
            self.assertIn("CUSTOMER_LABEL_4=予備", text)
            # 他の設定は保持
            self.assertIn("TKS_COMPANY_CODE=TEST", text)

    def test_comment_and_active_do_not_merge(self) -> None:
        """コメント行とアクティブ行が混在してもコメントは上書きされない。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            path.write_text(
                "# CUSTOMER_LABEL_1=得意先1\n"
                "TKS_COMPANY_CODE=TEST\n",
                encoding="utf-8",
            )
            update_customer_labels_in_config(path, {"得意先1": "新ラベル"})
            text = path.read_text(encoding="utf-8")
            # コメント行はそのまま
            self.assertIn("# CUSTOMER_LABEL_1=得意先1", text)
            # アクティブなキーが追記
            self.assertIn("CUSTOMER_LABEL_1=新ラベル", text)
            # 合計2行（コメント1 + アクティブ1）
            self.assertEqual(text.count("CUSTOMER_LABEL_1="), 2)

    def test_sample_template_style_works(self) -> None:
        """config.env.sample 形式（全キーがコメントアウト）から正しく追記される。"""
        sample_style = (
            "# 得意先ヘッダー表示名\n"
            "# CUSTOMER_LABEL_1=得意先1\n"
            "# CUSTOMER_LABEL_2=得意先2\n"
            "# CUSTOMER_LABEL_3=得意先3\n"
            "# CUSTOMER_LABEL_4=得意先4\n"
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.env"
            path.write_text(sample_style, encoding="utf-8")
            update_customer_labels_in_config(
                path, {"得意先1": "A", "得意先2": "B", "得意先3": "C", "得意先4": "D"}
            )
            text = path.read_text(encoding="utf-8")
            # コメント行保持
            self.assertIn("# CUSTOMER_LABEL_1=得意先1", text)
            # アクティブ追記
            self.assertIn("CUSTOMER_LABEL_1=A", text)
            self.assertIn("CUSTOMER_LABEL_4=D", text)


# ── 確認点5: 加工名マスタCSVの列名は内部キーのまま ──────────────────────────

class KakouMasterCsvColumnNameTest(unittest.TestCase):
    """save_master / load_master で得意先列名は 得意先1〜4 のまま。"""

    def _make_row(self, toku1: str = "A", toku2: str = "B") -> dict[str, str]:
        return {
            "メーカー識別掛率集計コード": "MK0300",
            "メーカー識別コード": "MK",
            "掛率集計コード": "0300",
            "掛率集計名称": "エッチング",
            "掛率集計略称": "",
            "加工名": "エッチング",
            "得意先1": toku1,
            "得意先2": toku2,
            "得意先3": "",
            "得意先4": "",
        }

    def test_csv_fieldnames_are_internal_keys(self) -> None:
        """CSV の列名は 得意先1〜4（表示名ではない）。"""
        import csv as csv_module
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "master.csv"
            save_master(path, [self._make_row()])
            with open(path, encoding="utf-8-sig", newline="") as f:
                reader = csv_module.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
            self.assertIn("得意先1", fieldnames)
            self.assertIn("得意先2", fieldnames)
            self.assertIn("得意先3", fieldnames)
            self.assertIn("得意先4", fieldnames)

    def test_custom_display_name_does_not_affect_csv_columns(self) -> None:
        """得意先ヘッダー設定でカスタム表示名にしても、CSV列名は変わらない。"""
        import csv as csv_module
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "master.csv"
            # カスタム表示名で保存（KakouMasterDialogはdisplay_headersを表示に使うが
            # _table_to_rows()はKAKOU_MASTER_HEADERSキーを使うため内部キーは変わらない）
            save_master(path, [self._make_row(toku1="吉田硝子向け")])
            rows = load_master(path)
            # load_master はKAKOU_MASTER_HEADERSキーで辞書を作るため内部キーが保たれる
            self.assertIn("得意先1", rows[0])
            self.assertEqual(rows[0]["得意先1"], "吉田硝子向け")
            # 表示名 "吉田硝子" のキーは存在しない
            self.assertNotIn("吉田硝子", rows[0])

    def test_kakou_master_headers_order(self) -> None:
        """KAKOU_MASTER_HEADERS に得意先1〜4 が含まれ、順序も確認。"""
        idx1 = KAKOU_MASTER_HEADERS.index("得意先1")
        idx2 = KAKOU_MASTER_HEADERS.index("得意先2")
        idx3 = KAKOU_MASTER_HEADERS.index("得意先3")
        idx4 = KAKOU_MASTER_HEADERS.index("得意先4")
        self.assertLess(idx1, idx2)
        self.assertLess(idx2, idx3)
        self.assertLess(idx3, idx4)


if __name__ == "__main__":
    unittest.main()
