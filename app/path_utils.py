from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


VOUCHER_OUTPUT_DIR_ENV_KEY = "VOUCHER_OUTPUT_DIR"


def get_app_data_dir() -> Path:
    """Return the writable application data directory."""
    override = os.environ.get("TKS_TO_KINTONE_HOME")
    if override:
        return Path(override)
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        return Path(program_data) / "Manekiya" / "TksToKintone"
    if os.name == "nt":
        return Path(r"C:\ProgramData\Manekiya\TksToKintone")
    return Path.cwd() / ".programdata" / "Manekiya" / "TksToKintone"


def get_voucher_cache_dir() -> Path:
    """OLAP取得データ（受注Noごと）のキャッシュ保存ディレクトリ。"""
    return get_app_data_dir() / "work" / "voucher_cache"


def get_voucher_edit_objects_dir() -> Path:
    """指図書編集オブジェクト（受注Noごと）の保存ディレクトリ。"""
    return get_app_data_dir() / "work" / "voucher_edit_objects"


def get_default_voucher_output_dir(base_dir: Path | None = None) -> Path:
    """Return the default voucher PDF output directory.

    During normal source-tree execution, callers that pass a project base_dir keep
    the historical project-local work/voucher_output behavior. Frozen/exe builds
    must use writable app data, never PyInstaller's extraction/internal directory.
    """
    if base_dir is not None and not _is_frozen():
        return Path(base_dir) / "work" / "voucher_output"
    return get_app_data_dir() / "work" / "voucher_output"


def get_voucher_output_dir(config: object | None = None, base_dir: Path | None = None) -> Path:
    configured = ""
    if config is not None:
        configured = str(getattr(config, "voucher_output_dir", "") or "").strip()
    if configured:
        return Path(configured)
    return get_default_voucher_output_dir(base_dir)


def ensure_voucher_output_dir(path: str | Path) -> Path:
    raw = str(path).strip()
    if not raw:
        raise RuntimeError("PDF出力先が空です。出力先を指定してください。")

    output_dir = Path(raw).expanduser()
    if _is_unsafe_runtime_dir(output_dir):
        raise RuntimeError(
            "PDF出力先に使用できない場所が指定されています。\n"
            "Program Files 配下や _internal 配下ではない出力先を指定してください。\n\n"
            f"対象パス:\n{output_dir}"
        )

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            "PDF出力先フォルダを作成できません。\n"
            "出力先を変更してください。\n\n"
            f"対象パス:\n{output_dir}\n\n詳細:\n{exc}"
        ) from exc

    if not output_dir.is_dir():
        raise RuntimeError(
            "PDF出力先がフォルダではありません。\n"
            "出力先を変更してください。\n\n"
            f"対象パス:\n{output_dir}"
        )

    try:
        with tempfile.NamedTemporaryFile(prefix=".write_test_", suffix=".tmp", dir=output_dir, delete=False) as fp:
            test_path = Path(fp.name)
            fp.write(b"ok")
            fp.flush()
            os.fsync(fp.fileno())
        test_path.unlink(missing_ok=True)
    except OSError as exc:
        try:
            test_path.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
        except Exception:
            pass
        raise RuntimeError(
            "PDF出力先に書き込みできません。\n"
            "出力先を変更してください。\n\n"
            f"対象パス:\n{output_dir}\n\n詳細:\n{exc}"
        ) from exc

    return output_dir


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _is_unsafe_runtime_dir(path: Path) -> bool:
    resolved = _resolve_for_compare(path)
    resolved_text = str(resolved).lower().replace("/", "\\")
    parts = {part.lower() for part in resolved.parts}
    if "_internal" in parts:
        return True
    if "_internal" in resolved_text:
        return True

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and _is_relative_to(resolved, _resolve_for_compare(Path(meipass))):
        return True

    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(env_key)
        if value and _is_relative_to(resolved, _resolve_for_compare(Path(value))):
            return True

    return "\\program files" in resolved_text


def _resolve_for_compare(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
