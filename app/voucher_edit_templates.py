"""指図書編集オブジェクトの反映先伝票テンプレート（要件4）。

編集オブジェクトごとに「どの伝票へ印刷するか（target_vouchers）」を持たせる。
その組み合わせをテンプレートとして登録・再利用できるようにする。

- 組み込みテンプレート（標準/全伝票/指図書のみ/梱包のみ）は常に提供する。
- ユーザー定義テンプレートはアプリ設定ディレクトリ配下の JSON に保存する。
  既存の設定保存方針（app data ディレクトリ）に合わせる。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.path_utils import get_app_data_dir

# 旧データ互換の既定反映先（指図書(1)/指図書(2)/梱包明細書）。
DEFAULT_TARGET_VOUCHERS: list[str] = ["03", "04", "05"]

# 固定（ロック）テンプレート。削除・名前変更ともに不可（要件3・7）。
# 表示時はロックバッヂを付けるが、内部キー（name）は変更しない（要件10）。
LOCKED_REFLECT_TARGETS: set[str] = {"標準", "全伝票"}


def is_locked_template(name: str) -> bool:
    """固定テンプレート（削除・名前変更不可）かどうか（要件3・7）。"""
    return name in LOCKED_REFLECT_TARGETS

# 組み込みテンプレート（要件4）。name をキーに扱う。
BUILTIN_TEMPLATES: list[dict[str, Any]] = [
    {"key": "standard", "name": "標準", "target_vouchers": ["03", "04", "05"], "color": "#1976d2", "badge": "標"},
    {"key": "all_vouchers", "name": "全伝票",
     "target_vouchers": ["01", "02", "03", "04", "05", "06", "07", "08"],
     "color": "#7b1fa2", "badge": "全"},
    {"key": "instruction_only", "name": "指図書のみ", "target_vouchers": ["03", "04"], "color": "#00897b", "badge": "図"},
    {"key": "packing_only", "name": "梱包のみ", "target_vouchers": ["05"], "color": "#2e7d32", "badge": "梱"},
]

_FILENAME = "voucher_edit_templates.json"


def templates_path(base_dir: Path | None = None) -> Path:
    base = base_dir or (get_app_data_dir() / "work")
    return base / _FILENAME


def _normalize_template(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    targets = raw.get("target_vouchers")
    if not name or not isinstance(targets, list):
        return None
    targets = [str(v).strip() for v in targets if str(v).strip()]
    if not targets:
        return None
    color = str(raw.get("color") or "#607d8b").strip() or "#607d8b"
    badge = str(raw.get("badge") or name[:1]).strip() or name[:1]
    key = str(raw.get("key") or "").strip()
    return {
        "key": key,
        "name": name,
        "target_vouchers": targets,
        "color": color,
        "badge": badge,
    }


def load_user_templates(base_dir: Path | None = None) -> list[dict[str, Any]]:
    """ユーザー定義テンプレートのみを読み込む。無ければ空リスト。"""
    path = templates_path(base_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("templates") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    result: list[dict[str, Any]] = []
    migrated = False
    seen_keys: set[str] = set()
    for raw in items:
        normalized = _normalize_template(raw)
        if normalized is not None:
            if not normalized["key"] or normalized["key"] in seen_keys:
                normalized["key"] = f"user-{uuid.uuid4()}"
                migrated = True
            seen_keys.add(normalized["key"])
            result.append(normalized)
    if migrated:
        save_user_templates(
            result,
            base_dir,
            deleted_builtins=load_deleted_builtin_names(base_dir),
        )
    return result


def load_deleted_builtin_names(base_dir: Path | None = None) -> list[str]:
    """削除済みの組み込みテンプレート名（指図書のみ/梱包のみ等）を読み込む（要件8）。

    固定テンプレート（LOCKED_REFLECT_TARGETS）は常に除外する。
    """
    path = templates_path(base_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("deleted_builtins") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    builtin_names = {t["name"] for t in BUILTIN_TEMPLATES}
    result: list[str] = []
    for raw in items:
        name = str(raw).strip()
        if name and name in builtin_names and name not in LOCKED_REFLECT_TARGETS:
            result.append(name)
    return result


def save_user_templates(
    templates: list[dict[str, Any]],
    base_dir: Path | None = None,
    deleted_builtins: list[str] | None = None,
) -> Path:
    """ユーザー定義テンプレートを保存（上書き）する。

    deleted_builtins を省略した場合は既存の削除済み組み込みテンプレート名を維持する。
    固定テンプレート（LOCKED_REFLECT_TARGETS）は削除対象に含めない（要件7）。
    """
    normalized = [t for t in (_normalize_template(t) for t in templates) if t is not None]
    seen_keys: set[str] = set()
    for template in normalized:
        if not template["key"] or template["key"] in seen_keys:
            template["key"] = f"user-{uuid.uuid4()}"
        seen_keys.add(template["key"])
    if deleted_builtins is None:
        deleted_builtins = load_deleted_builtin_names(base_dir)
    builtin_names = {t["name"] for t in BUILTIN_TEMPLATES}
    deleted = []
    for raw in deleted_builtins:
        name = str(raw).strip()
        if name and name in builtin_names and name not in LOCKED_REFLECT_TARGETS and name not in deleted:
            deleted.append(name)
    path = templates_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"templates": normalized, "deleted_builtins": deleted}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def delete_template(name: str, base_dir: Path | None = None) -> bool:
    """テンプレートを削除して永続化する（要件8）。

    - 固定テンプレート（標準/全伝票）は削除しない（戻り値 False）。
    - ユーザー定義テンプレートは一覧から除外する。
    - 組み込みテンプレート（指図書のみ/梱包のみ）は削除済みとして記録し、
      再起動後も復活しないようにする。
    """
    if is_locked_template(name):
        return False
    user = load_user_templates(base_dir)
    deleted = load_deleted_builtin_names(base_dir)
    builtin_names = {t["name"] for t in BUILTIN_TEMPLATES}
    removed = False
    if any(t["name"] == name for t in user):
        user = [t for t in user if t["name"] != name]
        removed = True
    if name in builtin_names and name not in deleted:
        deleted.append(name)
        removed = True
    if not removed:
        return False
    save_user_templates(user, base_dir, deleted_builtins=deleted)
    return True


def load_templates(base_dir: Path | None = None) -> list[dict[str, Any]]:
    """組み込み＋ユーザー定義テンプレートを結合して返す（同名はユーザー定義優先）。

    削除済みの組み込みテンプレートは除外する（要件8）。
    """
    deleted = set(load_deleted_builtin_names(base_dir))
    builtin = [dict(t) for t in BUILTIN_TEMPLATES if t["name"] not in deleted]
    user = load_user_templates(base_dir)
    by_name: dict[str, dict[str, Any]] = {t["name"]: t for t in builtin}
    ordered: list[dict[str, Any]] = list(builtin)
    for t in user:
        if t["name"] in by_name:
            # 同名はユーザー定義で上書きする（並び順は組み込み位置を維持）。
            idx = next(i for i, x in enumerate(ordered) if x["name"] == t["name"])
            # 組み込み名を上書きしても、反映処理と順序保存に使う正式 key は維持する。
            t = {**t, "key": ordered[idx]["key"]}
            ordered[idx] = t
        else:
            ordered.append(t)
    return ordered
