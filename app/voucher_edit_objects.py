"""指図書編集オブジェクトの受注Noごとの永続化。

指図書編集画面でプレビュー上に追加したオブジェクト（テキスト・線・四角形）を
受注Noごとに1ファイル（JSON）で保存し、後から再編集・削除できるようにする。

座標は編集画面の scene 座標系（原点=ページ左上、単位pt、ページ PAGE_W x PAGE_H）で保存する。
PDF生成時だけ reportlab 座標（原点=左下）へ変換して重ね描きする。

オブジェクトスキーマ:
- 共通: id, type, x/y/width/height または x1/y1/x2/y2, line_width,
        stroke_color, fill_color, created_at, updated_at
- text/図形内text: text, font_family, font_size, font_bold, font_italic,
        text_color, text_align, vertical_align
- symbol_text: x/y(中心アンカー), text, font_family, font_size, font_bold,
        font_italic, text_color, anchor

旧形式（id 無し・text 無し・w/h 無しの rectangle 等）も読み込めるよう、
読み込み時に id を付与し text を空文字で補完する（要件9）。同一IDが複数あっても
最初の1件だけを採用し、重複を防ぐ（要件2）。

OLAPキャッシュとは別ディレクトリに保存する（要件7・11）。
"""
from __future__ import annotations

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


def edit_objects_path_for(order_no: str, base_dir: Path | None = None) -> Path:
    base = base_dir or get_voucher_edit_objects_dir()
    return base / f"{sanitize_order_no(order_no)}.json"


def load_edit_objects(order_no: str, base_dir: Path | None = None) -> list[dict[str, Any]]:
    """受注Noの編集オブジェクト一覧を読み込む。無ければ空リスト。

    旧形式互換のため id/text を補完し、同一IDは1件に重複排除する。
    """
    path = edit_objects_path_for(order_no, base_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    objects = data.get("objects") if isinstance(data, dict) else data
    if not isinstance(objects, list):
        return []
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        item = dict(obj)
        obj_id = str(item.get("id") or "").strip()
        if not obj_id:
            obj_id = str(uuid.uuid4())
        if obj_id in seen_ids:
            # 同じIDが二重に保存されていても1件だけ採用する（要件2）。
            continue
        seen_ids.add(obj_id)
        item["id"] = obj_id
        if item.get("coordinate_origin") != COORDINATE_ORIGIN:
            _log.warning("旧形式の編集オブジェクト座標を読み込みました。位置がずれる場合は再保存してください。")
        elif item.get("geometry_basis") != GEOMETRY_BASIS:
            _log.warning(
                "旧基準の編集オブジェクト座標を読み込みました。以前のsceneBoundingRect基準のため"
                "位置がずれる場合があります。必要に応じて該当受注Noの編集JSONを削除して作り直してください。"
            )
        # 旧形式に text が無ければ空文字として扱う（要件9）。
        if item.get("type") in ("text", "symbol_text", "rectangle", "ellipse"):
            item.setdefault("text", "")
        item = _with_compat_defaults(item)
        result.append(item)
    return result


def save_edit_objects(
    order_no: str,
    objects: list[dict[str, Any]],
    base_dir: Path | None = None,
) -> Path:
    """受注Noの編集オブジェクト一覧を保存（上書き）する。"""
    base = base_dir or get_voucher_edit_objects_dir()
    base.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    normalized = [_normalize_object(obj, now) for obj in objects]
    payload = {
        "order_no": order_no,
        "updated_at": now,
        "objects": normalized,
    }
    path = edit_objects_path_for(order_no, base)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


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
        obj["font_bold"] = bool(obj.get("font_bold", False))
        obj["font_italic"] = bool(obj.get("font_italic", False))
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
        obj["font_bold"] = bool(obj.get("font_bold", False))
        obj["font_italic"] = bool(obj.get("font_italic", False))
        obj.setdefault("text_color", DEFAULT_TEXT_COLOR)
        obj["anchor"] = "center" if obj.get("anchor") != "center" else "center"
        obj.pop("width", None)
        obj.pop("height", None)
        obj.pop("w", None)
        obj.pop("h", None)
        obj.pop("vertical_align", None)
    return obj
