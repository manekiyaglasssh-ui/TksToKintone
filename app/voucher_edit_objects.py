"""指図書編集オブジェクトの受注Noごとの永続化。

指図書編集画面でプレビュー上に追加したオブジェクト（テキスト・線・四角形）を
受注Noごとに1ファイル（JSON）で保存し、後から再編集・削除できるようにする。

座標は編集画面の scene 座標系（原点=ページ左上、単位pt、ページ PAGE_W x PAGE_H）で保存する。
PDF生成時だけ reportlab 座標（原点=左下）へ変換して重ね描きする。

オブジェクトスキーマ:
- 共通: id, type, x/y/width/height または x1/y1/x2/y2, line_width,
        stroke_color, fill_color, created_at, updated_at
- text/図形内text: text, font_family, font_size, font_bold, font_italic,
        font_underline, font_strikeout, text_color, text_align, vertical_align
- symbol_text: x/y(中心アンカー), text, font_family, font_size, font_bold,
        font_italic, font_underline, font_strikeout, text_color, anchor

旧形式（id 無し・text 無し・w/h 無しの rectangle 等）も読み込めるよう、
読み込み時に id を付与し text を空文字で補完する（要件9）。同一IDが複数あっても
最初の1件だけを採用し、重複を防ぐ（要件2）。

OLAPキャッシュとは別ディレクトリに保存する（要件7・11）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.line_decorations import normalize_line_type
from app.path_utils import get_voucher_edit_objects_dir
from app.voucher_cache import sanitize_order_no

OBJECT_TYPES = (
    "text", "symbol_text", "line", "rectangle", "ellipse", "image",
    "freehand", "freehand_layer",
)
DEFAULT_LINE_TYPE = "line"
DEFAULT_FONT_FAMILY = "Yu Gothic UI"
DEFAULT_FONT_SIZE = 12.0
DEFAULT_LINE_WIDTH = 1.0
# 手書きペンの既定の太さ（タブレット編集レイヤー用）。
DEFAULT_PEN_WIDTH = 3.0
DEFAULT_STROKE_COLOR = "#000000"
DEFAULT_TEXT_COLOR = "#000000"
DEFAULT_TEXT_WIDTH = 60.0
DEFAULT_TEXT_HEIGHT = 18.0
# 反映先伝票の既定（旧データ互換: 指図書(1)/指図書(2)/梱包明細書）。
DEFAULT_TARGET_VOUCHERS = ["03", "04", "05"]
COORDINATE_ORIGIN = "scene_top_left"
GEOMETRY_BASIS = "object_geometry_v2"
_log = logging.getLogger("tks_to_kintone_app")
SCHEMA_VERSION = 3
EMPTY_VOUCHER_KEY = "__tks_empty_voucher_no__"
COMMON_EDIT_KEY = "__tks_common_edit__"


def canonical_edit_objects_json(objects: object) -> str:
    """編集内容の安定比較用JSON。保存時刻と実行時だけの内部属性は除外する。"""
    def cleaned(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): cleaned(item)
                for key, item in value.items()
                if key not in {"created_at", "updated_at"}
                and not str(key).startswith("_")
            }
        if isinstance(value, list):
            return [cleaned(item) for item in value]
        if isinstance(value, tuple):
            return [cleaned(item) for item in value]
        return value

    return json.dumps(
        cleaned(objects), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    )


def edit_objects_sha256(objects: object) -> str:
    """フォント・装飾・座標を含む編集オブジェクト内容のSHA-256を返す。"""
    return hashlib.sha256(
        canonical_edit_objects_json(objects).encode("utf-8")
    ).hexdigest()


def normalize_voucher_no(value: object) -> str:
    """伝票Noを文字列のまま正規化する（stripのみ。数値化しない）。"""
    return str(value or "").strip()


def voucher_key_for(value: object) -> str:
    """保存用キーを返す。空欄は通常の伝票Noと区別できる予約キーにする。"""
    normalized = normalize_voucher_no(value)
    return normalized if normalized else EMPTY_VOUCHER_KEY


def unique_voucher_numbers(values: list[object] | tuple[object, ...] | None) -> list[str]:
    """元データの出現順を保ち、同じ伝票Noを1件にまとめる。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        normalized = normalize_voucher_no(value)
        key = voucher_key_for(normalized)
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result or [""]


def edit_objects_path_for(order_no: str, base_dir: Path | None = None) -> Path:
    base = base_dir or get_voucher_edit_objects_dir()
    return base / f"{sanitize_order_no(order_no)}.json"


def _normalize_objects(objects: object) -> list[dict[str, Any]]:
    """保存値を互換補完し、ID重複を除いた独立モデルとして返す。"""
    if not isinstance(objects, list):
        return []
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        item = copy.deepcopy(obj)
        obj_id = str(item.get("id") or "").strip() or str(uuid.uuid4())
        if obj_id in seen_ids:
            continue
        seen_ids.add(obj_id)
        item["id"] = obj_id
        if item.get("coordinate_origin") != COORDINATE_ORIGIN:
            _log.warning("旧形式の編集オブジェクト座標を読み込みました。位置がずれる場合は再保存してください。")
        elif item.get("geometry_basis") != GEOMETRY_BASIS:
            _log.warning("旧基準の編集オブジェクト座標を読み込みました。位置がずれる場合があります。")
        if item.get("type") in ("text", "symbol_text", "rectangle", "ellipse"):
            item.setdefault("text", "")
        result.append(_with_compat_defaults(item))
    return result


def load_voucher_edit_state(
    order_no: str,
    voucher_nos: list[object] | tuple[object, ...] | None = None,
    base_dir: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """全伝票の編集モデルを読み込む。キーは正規化済みの内部キー。"""
    numbers = unique_voucher_numbers(voucher_nos)
    path = edit_objects_path_for(order_no, base_dir)
    if not path.is_file():
        return {voucher_key_for(no): [] for no in numbers}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {voucher_key_for(no): [] for no in numbers}

    result: dict[str, list[dict[str, Any]]] = {}
    if isinstance(data, dict) and isinstance(data.get("voucher_edits"), dict):
        raw_edits = data["voucher_edits"]
        raw_order = data.get("voucher_order")
        stored_keys = [str(v) for v in raw_order] if isinstance(raw_order, list) else list(raw_edits)
        for raw_key in stored_keys:
            entry = raw_edits.get(raw_key)
            objects = entry.get("objects") if isinstance(entry, dict) else entry
            result[raw_key] = _normalize_objects(objects)
        _log.info("voucher_edit_state_loaded_by_voucher_no order_no=%s vouchers=%d", order_no, len(result))
    else:
        # 旧形式は全伝票へ複製せず、元データで最初の伝票へだけ割り当てる。
        legacy_objects = data.get("objects") if isinstance(data, dict) else data
        first_key = voucher_key_for(numbers[0])
        result[first_key] = _normalize_objects(legacy_objects)
        _log.info("voucher_edit_legacy_state_detected order_no=%s", order_no)
        _log.info("voucher_edit_legacy_state_migrated order_no=%s voucher_no=%s", order_no, numbers[0])
    for no in numbers:
        result.setdefault(voucher_key_for(no), [])
    return copy.deepcopy(result)


def load_voucher_edit_document(
    order_no: str,
    voucher_nos: list[object] | tuple[object, ...] | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """schema v3 の共通・伝票別モデルを独立した deep copy として返す。

    schema v2 以前は従来の伝票別移行を先に適用し、共通モデルを空で補完する。
    """
    path = edit_objects_path_for(order_no, base_dir)
    common: list[dict[str, Any]] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            entry = data.get("common_edit")
            if isinstance(entry, dict):
                common = _normalize_objects(entry.get("objects"))
            elif isinstance(entry, list):
                common = _normalize_objects(entry)
    return {
        "common_edit": copy.deepcopy(common),
        "voucher_edits": load_voucher_edit_state(order_no, voucher_nos, base_dir),
    }


def load_edit_objects(
    order_no: str,
    base_dir: Path | None = None,
    *,
    voucher_no: object | None = None,
) -> list[dict[str, Any]]:
    """受注Noの編集オブジェクト一覧を読み込む。無ければ空リスト。

    旧形式互換のため id/text を補完し、同一IDは1件に重複排除する。
    """
    numbers = [voucher_no] if voucher_no is not None else None
    document = load_voucher_edit_document(order_no, numbers, base_dir)
    state = document["voucher_edits"]
    common = document["common_edit"]
    def tagged(objects: list[dict[str, Any]], scope: str,
               number: object | None) -> list[dict[str, Any]]:
        result = copy.deepcopy(objects)
        for obj in result:
            obj["_edit_scope"] = scope
            obj["_edit_voucher_no"] = "" if number is None else str(number)
        return result
    if voucher_no is not None:
        return tagged(common, "common", voucher_no) + tagged(
            state.get(voucher_key_for(voucher_no), []), "individual", voucher_no)
    first_key = next(iter(state), "")
    return tagged(common, "common", first_key) + tagged(
        state.get(first_key, []), "individual", first_key)


def save_voucher_edit_state(
    order_no: str,
    voucher_edits: dict[str, list[dict[str, Any]]],
    voucher_nos: list[object] | tuple[object, ...] | None = None,
    base_dir: Path | None = None,
    *,
    common_objects: list[dict[str, Any]] | None = None,
) -> Path:
    """全伝票分を一時ファイル経由で一括保存する。"""
    base = base_dir or get_voucher_edit_objects_dir()
    base.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    existing: dict[str, Any] | None = None
    existing_path = edit_objects_path_for(order_no, base)
    if existing_path.is_file():
        try:
            loaded_existing = json.loads(existing_path.read_text(encoding="utf-8"))
            existing = loaded_existing if isinstance(loaded_existing, dict) else None
        except (OSError, ValueError):
            existing = None
    if common_objects is None:
        if existing is not None:
            entry = existing.get("common_edit")
            common_objects = _normalize_objects(
                entry.get("objects") if isinstance(entry, dict) else entry)
        else:
            common_objects = []
    ordered_keys = [voucher_key_for(no) for no in unique_voucher_numbers(voucher_nos)]
    for key in voucher_edits:
        if key not in ordered_keys:
            ordered_keys.append(key)
    payload_edits: dict[str, dict[str, Any]] = {}
    for key in ordered_keys:
        payload_edits[key] = {
            "voucher_no": "" if key == EMPTY_VOUCHER_KEY else key,
            "objects": [_normalize_object(obj, now) for obj in copy.deepcopy(voucher_edits.get(key, []))],
        }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "edit_revision": int((existing or {}).get("edit_revision") or 0) + 1,
        "order_no": str(order_no),
        "updated_at": now,
        "voucher_order": ordered_keys,
        "common_edit": {
            "objects": [
                _normalize_object(obj, now)
                for obj in copy.deepcopy(common_objects)
            ],
        },
        "voucher_edits": payload_edits,
    }
    payload["edit_objects_sha256"] = edit_objects_sha256({
        "common_edit": payload["common_edit"]["objects"],
        "voucher_edits": {
            key: entry["objects"] for key, entry in payload_edits.items()
        },
    })
    path = edit_objects_path_for(order_no, base)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    _log_saved_text_styles(order_no, payload)
    _log.info(
        "voucher_edit_state_saved_by_voucher_no order_no=%s vouchers=%d "
        "edit_revision=%s edit_objects_sha256=%s",
        order_no, len(payload_edits), payload["edit_revision"],
        payload["edit_objects_sha256"],
    )
    return path


def load_edit_document_metadata(
    order_no: str, base_dir: Path | None = None,
) -> dict[str, Any]:
    """保存直後のrevisionと内容hashを、PDF要求・保存通知用に再読込する。"""
    path = edit_objects_path_for(order_no, base_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"edit_revision": 0, "edit_objects_sha256": edit_objects_sha256([])}
    if not isinstance(data, dict):
        return {"edit_revision": 0, "edit_objects_sha256": edit_objects_sha256([])}
    common_entry = data.get("common_edit")
    common = common_entry.get("objects") if isinstance(common_entry, dict) else []
    edits = data.get("voucher_edits")
    revision_objects = {
        "common_edit": common if isinstance(common, list) else [],
        "voucher_edits": {
            str(key): (
                entry.get("objects") if isinstance(entry, dict) else entry
            )
            for key, entry in edits.items()
        } if isinstance(edits, dict) else {},
    }
    return {
        "edit_revision": int(data.get("edit_revision") or 0),
        "edit_objects_sha256": edit_objects_sha256(revision_objects),
        "updated_at": str(data.get("updated_at") or ""),
    }


def save_edit_objects(
    order_no: str,
    objects: list[dict[str, Any]],
    base_dir: Path | None = None,
    *,
    voucher_no: object | None = None,
) -> Path:
    """互換API。伝票指定時は個別、未指定時は全伝票共通だけを更新する。"""
    document = load_voucher_edit_document(
        order_no, [voucher_no] if voucher_no is not None else None, base_dir)
    state = document["voucher_edits"]
    if voucher_no is None:
        return save_voucher_edit_state(
            order_no, state, None, base_dir, common_objects=copy.deepcopy(objects))
    key = voucher_key_for(voucher_no)
    state[key] = copy.deepcopy(objects)
    return save_voucher_edit_state(
        order_no, state, [voucher_no] if voucher_no is not None else None,
        base_dir, common_objects=document["common_edit"])


def save_voucher_edit_document(
    order_no: str,
    common_objects: list[dict[str, Any]],
    voucher_edits: dict[str, list[dict[str, Any]]],
    voucher_nos: list[object] | tuple[object, ...] | None = None,
    base_dir: Path | None = None,
) -> Path:
    """共通・伝票別モデルを schema v3 として一括保存する。"""
    return save_voucher_edit_state(
        order_no, voucher_edits, voucher_nos, base_dir,
        common_objects=common_objects,
    )


def _log_saved_text_styles(order_no: str, payload: dict[str, Any]) -> None:
    """保存された文字書式を、本文を含めず診断ログへ残す。"""
    groups: list[tuple[str, object]] = [
        ("common", payload.get("common_edit")),
    ]
    edits = payload.get("voucher_edits")
    if isinstance(edits, dict):
        groups.extend((str(key), value) for key, value in edits.items())
    for group, entry in groups:
        objects = entry.get("objects") if isinstance(entry, dict) else None
        if not isinstance(objects, list):
            continue
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("type") not in {
                    "text", "symbol_text", "rectangle", "ellipse"}:
                continue
            _log.info(
                "voucher_edit_text_style_saved order_no=%s group=%s object_id=%s "
                "font_family=%r font_size=%s font_bold=%s font_italic=%s "
                "font_underline=%s font_strikeout=%s",
                order_no, group, obj.get("id"), obj.get("font_family"),
                obj.get("font_size"), obj.get("font_bold"),
                obj.get("font_italic"), obj.get("font_underline"),
                obj.get("font_strikeout"),
            )


def clone_edit_objects(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保存モデルをdeep copyし、コピー先用IDを全件再発行する。"""
    cloned = copy.deepcopy(objects)
    for obj in cloned:
        obj["id"] = str(uuid.uuid4())
        if obj.get("type") == "freehand_layer":
            obj["layer_id"] = obj["id"]
    return cloned


def _normalize_object(obj: dict[str, Any], now: str) -> dict[str, Any]:
    out = _with_compat_defaults(dict(obj))
    out["coordinate_origin"] = COORDINATE_ORIGIN
    out["geometry_basis"] = GEOMETRY_BASIS
    out.setdefault("created_at", now)
    out["updated_at"] = now
    color = out.get("color")
    if not (isinstance(color, (list, tuple)) and len(color) == 3):
        out["color"] = [0.0, 0.0, 0.0]
    else:
        out["color"] = [float(c) for c in color]
    return out


def _normalize_target_vouchers(value: Any) -> list[str]:
    """target_vouchers を正規化する。未設定/不正なら既定（03/04/05）扱い（要件3・7）。"""
    if isinstance(value, list):
        cleaned = [str(v).strip() for v in value if str(v).strip()]
        if cleaned:
            return cleaned
    return list(DEFAULT_TARGET_VOUCHERS)


def _normalize_stroke_points(points: Any) -> list[list[float]]:
    """ストロークの points を [[x, y], ...] の float ペア配列へ正規化する。"""
    result: list[list[float]] = []
    if not isinstance(points, (list, tuple)):
        return result
    for p in points:
        try:
            x = float(p[0])
            y = float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        result.append([x, y])
    return result


def _normalize_freehand_layer(obj: dict[str, Any]) -> dict[str, Any]:
    """freehand_layer の欠落属性を補完し strokes を正規化する（要件2）。"""
    layer_id = str(obj.get("layer_id") or obj.get("id") or "").strip()
    if not layer_id:
        layer_id = str(uuid.uuid4())
    obj["layer_id"] = layer_id
    obj.setdefault("layer_name", "レイヤー")
    obj["pen_width"] = float(obj.get("pen_width") or obj.get("line_width")
                             or DEFAULT_PEN_WIDTH)
    obj["line_width"] = obj["pen_width"]
    obj.setdefault("stroke_color", DEFAULT_STROKE_COLOR)
    obj["visible"] = bool(obj.get("visible", True))
    obj["locked"] = bool(obj.get("locked", False))
    strokes: list[dict[str, Any]] = []
    raw_strokes = obj.get("strokes")
    if isinstance(raw_strokes, list):
        for s in raw_strokes:
            if not isinstance(s, dict):
                continue
            pts = _normalize_stroke_points(s.get("points"))
            if not pts:
                continue
            strokes.append({
                "points": pts,
                "pen_width": float(s.get("pen_width") or obj["pen_width"]),
                "stroke_color": str(s.get("stroke_color") or obj["stroke_color"]),
            })
    obj["strokes"] = strokes
    return obj


def _with_compat_defaults(obj: dict[str, Any]) -> dict[str, Any]:
    """旧JSONの欠落属性を現行スキーマの既定値で補完する。"""
    kind = obj.get("type")
    # 反映先伝票は全オブジェクト共通で補完する（旧データは ["03","04","05"]）。
    obj["target_vouchers"] = _normalize_target_vouchers(obj.get("target_vouchers"))
    if kind == "freehand_layer":
        return _normalize_freehand_layer(obj)
    if kind in ("text", "rectangle", "ellipse"):
        width = obj.get("width", obj.get("w", DEFAULT_TEXT_WIDTH))
        height = obj.get("height", obj.get("h", DEFAULT_TEXT_HEIGHT))
        obj["width"] = float(width or DEFAULT_TEXT_WIDTH)
        obj["height"] = float(height or DEFAULT_TEXT_HEIGHT)
        # 旧コード・既存テスト互換のため w/h も保持する。
        obj["w"] = obj["width"]
        obj["h"] = obj["height"]
        obj.setdefault("text", "")
        obj.setdefault("font_family", DEFAULT_FONT_FAMILY)
        obj["font_size"] = float(obj.get("font_size") or DEFAULT_FONT_SIZE)
        obj["font_bold"] = bool(obj.get("font_bold", obj.get("bold", False)))
        obj["bold"] = obj["font_bold"]
        obj["font_italic"] = bool(obj.get("font_italic", obj.get("italic", False)))
        obj["font_underline"] = bool(obj.get("font_underline", obj.get("underline", False)))
        obj["font_strikeout"] = bool(obj.get("font_strikeout", obj.get("strikeout", False)))
        obj["italic"] = obj["font_italic"]
        obj["underline"] = obj["font_underline"]
        obj["strikeout"] = obj["font_strikeout"]
        obj.setdefault("text_color", DEFAULT_TEXT_COLOR)
        if kind == "text":
            if obj.get("text_align") != "left" or obj.get("vertical_align") != "top":
                _log.info("単独テキストの配置基準を left/top に補正しました。")
            obj["text_align"] = "left"
            obj["vertical_align"] = "top"
            obj["auto_fit"] = bool(obj.get("auto_fit", True))
            obj["manual_resized"] = bool(obj.get("manual_resized", False))
        else:
            obj.setdefault("text_align", "center")
            obj.setdefault("vertical_align", "middle")
    if kind in ("text", "line", "rectangle", "ellipse"):
        obj["line_width"] = float(obj.get("line_width") or DEFAULT_LINE_WIDTH)
        obj.setdefault("stroke_color", DEFAULT_STROKE_COLOR)
    if kind == "line":
        # 旧データ（line_type 無し）は通常の直線として扱う（要件: 互換）。
        obj["line_type"] = normalize_line_type(obj.get("line_type"))
    if kind in ("text", "rectangle", "ellipse"):
        obj.setdefault("fill_color", None)
    if kind == "image":
        # 画像オブジェクトは矩形ジオメトリと base64 画像データを保持する（要件2-3）。
        width = obj.get("width", obj.get("w", DEFAULT_TEXT_WIDTH))
        height = obj.get("height", obj.get("h", DEFAULT_TEXT_HEIGHT))
        obj["width"] = float(width or DEFAULT_TEXT_WIDTH)
        obj["height"] = float(height or DEFAULT_TEXT_HEIGHT)
        obj["w"] = obj["width"]
        obj["h"] = obj["height"]
        obj.setdefault("image_format", "png")
        obj.setdefault("image_data", "")
    if kind == "symbol_text":
        obj.setdefault("text", "")
        obj.setdefault("font_family", DEFAULT_FONT_FAMILY)
        obj["font_size"] = float(obj.get("font_size") or DEFAULT_FONT_SIZE)
        obj["font_bold"] = bool(obj.get("font_bold", obj.get("bold", False)))
        obj["bold"] = obj["font_bold"]
        obj["font_italic"] = bool(obj.get("font_italic", obj.get("italic", False)))
        obj["font_underline"] = bool(obj.get("font_underline", obj.get("underline", False)))
        obj["font_strikeout"] = bool(obj.get("font_strikeout", obj.get("strikeout", False)))
        obj["italic"] = obj["font_italic"]
        obj["underline"] = obj["font_underline"]
        obj["strikeout"] = obj["font_strikeout"]
        obj.setdefault("text_color", DEFAULT_TEXT_COLOR)
        obj["anchor"] = "center" if obj.get("anchor") != "center" else "center"
        obj.pop("width", None)
        obj.pop("height", None)
        obj.pop("w", None)
        obj.pop("h", None)
        obj.pop("vertical_align", None)
    return obj
